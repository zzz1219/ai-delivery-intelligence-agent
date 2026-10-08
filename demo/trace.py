"""Trace schema trace-1.0: everything the UI needs to show one question, produced by the engine or by the sealed-run converter.

  question -> plan -> SQL tasks (sql, rows) -> viz specs -> retrieval (queries, documents) -> answer -> findings -> trust checks [-> evaluation]

Rules enforced by validate_trace:
  - viz specs equal what the deterministic selector returns for the recorded tasks, validate, and pass the fidelity check;
  - findings are verbatim sentences of the answer; trust checks are recomputed and must equal the stored ones;
  - the oracle context and all evaluation annotations live ONLY in `evaluation`, which only sealed-evaluation traces may carry;
  - no key-like secret anywhere in the trace.
"""
import json
import re

from rag import evaluate_hybrid as ev
from viz import materialize as vmat
from viz import select as vsel
from viz import spec as vspec

TRACE_VERSION = "trace-1.0"
KINDS = ("sealed_evaluation", "recorded_live")
LANE = {"historical_cases": "cases", "troubleshooting_guides": "guides", "deployment_guides": "guides"}
CASE_FIELDS = ("incident_id", "project", "component", "environment", "severity", "root_cause", "solution_pattern")
SECRET = re.compile(r"AIza[0-9A-Za-z_\-]{20,}|sk-[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9\-_.]{20,}|GEMINI_API_KEY\s*[=:]")
REQUIRED = ("trace_version", "trace_id", "kind", "title", "question", "model", "specs", "status", "plan", "tasks", "viz", "retrieval", "answer",
            "findings", "trust")
MAX_QUESTION = 300
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z`\[(\d])")
_CITE = re.compile(r"\[(?:sql:[A-Za-z0-9_\-]+|(?:historical_cases|troubleshooting_guides|deployment_guides)/[A-Za-z0-9_]+)\]")


def dump_json(obj):
    """The ONLY way demo files are serialised: UTF-8 bytes with LF newlines on every platform (text mode would write CRLF on Windows)."""
    return (json.dumps(obj, ensure_ascii=False, indent=1) + "\n").encode("utf-8")


def _keys(obj):
    """Every dict key at any depth (a user question that merely contains the word 'oracle' is not a leak; a field named oracle_* is)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def check_question(question):
    """Clean a user question; refuse empty, over-long or secret-looking input (it would be stored in the trace)."""
    q = " ".join(str(question).replace("\x00", " ").split())
    if not q:
        raise ValueError("the question is empty")
    if len(q) > MAX_QUESTION:
        raise ValueError(f"the question is longer than {MAX_QUESTION} characters")
    if SECRET.search(q):
        raise ValueError("the question looks like it contains an API key; it will not be sent or stored")
    return q


def doc_card(doc):
    secs = {k: (v if isinstance(v, str) else str(v))[:600] for k, v in dict(doc.sections).items() if k != "overview"}
    card = dict(doc_id=doc.doc_id, lane=LANE[doc.doc_type], title=doc.title, sections=secs)
    if doc.doc_type == "historical_cases":
        card["fields"] = {k: doc.metadata.get(k) for k in CASE_FIELDS if doc.metadata.get(k)}
    return card


def extract_findings(answer):
    """Key findings = the sentences of the final answer that carry a citation, verbatim. Nothing is generated."""
    out = []
    for block in (answer or "").split("\n"):
        for sent in _SENT.split(block.strip()):
            sent = re.sub(r"^(?:[-*\u2022]|\d+[.)])\s+", "", sent.strip())          # a list marker is not part of the sentence
            marks = _CITE.findall(sent)
            if marks:
                out.append(dict(text=sent, sql_tasks=[m[5:-1] for m in marks if m.startswith("[sql:")],
                                docs=[m[1:-1] for m in marks if not m.startswith("[sql:")]))
    return out


_ORDINAL = re.compile(r"(?m)^[ \t]*(?:[-*\u2022][ \t]+)?\d+[.)][ \t]+")
_DATEISH = re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b")


def unsupported_numbers(record, text_by_id):
    """The frozen scorer's numeric check, minus two formatting artefacts that are not claims: the ordinal of a numbered list ('3. Tile Cache')
    and the month/day parts of a date such as 2025-10 that appears verbatim in a SQL result, the question or a retrieved document.
    Everything else is judged by the frozen rule itself (rag.evaluate_hybrid.unsupported_numbers), which is not modified."""
    ans = record.get("answer") or ""
    ok_text = [str(c) for t in record.get("tasks", []) if t.get("status") == "ok" for r in t["rows"] for c in r]
    ok_text += [record["question"]] + [text_by_id[d] for d in record.get("context_ids", [])]
    pool = "\n".join(ok_text)
    cleaned = _ORDINAL.sub("", ans)
    cleaned = _DATEISH.sub(lambda m: " DATE " if m.group(0) in pool else m.group(0), cleaned)
    return ev.unsupported_numbers(dict(record, answer=cleaned), record["question"], text_by_id)


def trust_checks(record, text_by_id):
    cit = ev.citation_checks(record)
    tasks = record.get("tasks", [])
    return dict(
        plan_valid=record.get("plan") is not None, sql_tasks_total=len(tasks), sql_tasks_ok=sum(t["status"] == "ok" for t in tasks),
        invalid_doc_citations=cit["invalid_doc"], invalid_sql_citations=cit["invalid_sql"], citation_marks=cit["n_marks"],
        numbers_not_in_any_result=unsupported_numbers(record, text_by_id) if record.get("answer") else [],
        retrieval_query_status=record.get("query_status"), documents_in_context=len(record.get("context_ids", [])),
        visualization="selected by deterministic rules (no model involved)")


def build_trace(record, *, trace_id, kind, title, model, specs, docs_by_id, evaluation=None, recorded_at=None, role=None):
    tasks = record.get("tasks", [])
    plan = record.get("plan")
    ctx = record.get("context_ids", [])
    text_by_id = {k: d.text for k, d in docs_by_id.items()}
    trace = dict(
        trace_version=TRACE_VERSION, trace_id=trace_id, kind=kind, title=title, role=role, question=record["question"], model=model, recorded_at=recorded_at,
        specs=specs, status=record["status"],
        plan=None if plan is None else dict(plan),
        tasks=[dict(t) for t in tasks], viz=[vsel.select(t) for t in tasks],
        retrieval=dict(status=record.get("query_status"), purpose=(plan or {}).get("retrieval_purpose", ""), queries=list(record.get("queries", [])),
                       context_ids=list(ctx), documents=[doc_card(docs_by_id[d]) for d in ctx]),
        answer=record.get("answer"), findings=extract_findings(record.get("answer")), trust=trust_checks(record, text_by_id))
    if evaluation is not None:
        trace["evaluation"] = evaluation
    return trace


def validate_trace(trace, docs_by_id=None):
    """Return a list of problems (empty = a trace the UI may render without further checks)."""
    P = [f"missing field {k}" for k in REQUIRED if k not in trace]
    if P:
        return P
    if trace["trace_version"] != TRACE_VERSION or trace["kind"] not in KINDS:
        P.append("unknown trace version or kind")
    if "evaluation" in trace and trace["kind"] != "sealed_evaluation":
        P.append("only sealed-evaluation traces may carry an evaluation section")
    if any("oracle" in k.lower() for k in _keys({k: v for k, v in trace.items() if k != "evaluation"})):
        P.append("oracle fields may only appear inside the evaluation section")
    if SECRET.search(json.dumps(trace, ensure_ascii=False)):
        P.append("the trace contains a key-like secret")
    tasks = {t["task_id"]: t for t in trace["tasks"]}
    if len(tasks) != len(trace["tasks"]):
        P.append("duplicate task ids")
    if [v["task_id"] for v in trace["viz"]] != [t["task_id"] for t in trace["tasks"]]:
        P.append("the visualization list does not match the tasks")
    for t, spec in zip(trace["tasks"], trace["viz"]):
        if spec != vsel.select(t):
            P.append(f"visualization for {t['task_id']} differs from the deterministic selector")
            continue
        bad = vspec.validate_spec(spec, tasks)
        if bad:
            P.append(f"visualization for {t['task_id']} is invalid: {bad}")
        elif vmat.check_fidelity(vmat.materialize(spec, t), t, spec):
            P.append(f"visualization for {t['task_id']} is not faithful to the recorded rows")
    ans = trace["answer"] or ""
    for f in trace["findings"]:
        if f["text"] not in ans:
            P.append("a key finding is not a verbatim part of the answer")
    if trace["findings"] != extract_findings(ans):
        P.append("key findings differ from the ones derived from the answer")
    ids = trace["retrieval"]["context_ids"]
    if [d["doc_id"] for d in trace["retrieval"]["documents"]] != ids:
        P.append("the retrieved documents do not match the context ids")
    if docs_by_id is not None:
        if any(i not in docs_by_id for i in ids):
            P.append("a retrieved document does not exist in the corpus")
        else:
            rec = dict(question=trace["question"], answer=trace["answer"], tasks=trace["tasks"], context_ids=ids, plan=trace["plan"],
                       query_status=trace["retrieval"]["status"])
            if trust_checks(rec, {k: d.text for k, d in docs_by_id.items()}) != trace["trust"]:
                P.append("the stored trust checks differ from the recomputed ones")
    if trace["status"] == "ok" and not trace["answer"]:
        P.append("an answered trace has no answer")
    return P
