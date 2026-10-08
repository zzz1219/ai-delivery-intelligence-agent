"""Run the Hybrid benchmark (hybrid-1.0) with the frozen pipeline (hybrid-pipeline-1.0) and ONE model.

  Windows PowerShell:
    $env:GEMINI_API_KEY="your-key"
    python -m rag.hybrid_pipeline --model gemini-3.5-flash-lite

Per question: planner -> SQL tasks (the frozen SQL agent, answer step skipped) -> retrieval-query generator -> two-lane BM25
retrieval on the AGENT's queries -> synthesis. The oracle-query retrieval is computed only for diagnostics and never shown to a model.
Writes a NEW file eval_results/hyb_<model>_<timestamp>.json (checkpointed after every question; renamed when complete, _INCOMPLETE
otherwise). Two consecutive provider errors stop the run; any other exception aborts it; a planner reply that is not a valid plan is a
scored outcome (plan_failed), not an error. Incomplete runs are never scored.
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sql_agent.agent import run_agent  # noqa: E402
from sql_agent.evaluate import _write_atomic  # noqa: E402
from sql_agent.llm import GeminiLLM, is_provider_transient  # noqa: E402
from sql_agent.tools import DB_PATH  # noqa: E402
from .build_benchmark import load_frozen  # noqa: E402
from .build_hybrid_benchmark import BENCH_PATH, HASH_PATH, context_for  # noqa: E402
from .build_hybrid_pipeline_spec import load_pipeline_spec  # noqa: E402
from .corpus import load_corpus, verify_corpus  # noqa: E402
from .lanes import TwoLaneRetriever  # noqa: E402


def extract_json(raw):
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None, "no JSON object in the reply"
    try:
        return json.loads(m.group(0)), None
    except ValueError as e:
        return None, f"invalid JSON: {e}"


def validate_plan(obj, limits):
    if not isinstance(obj, dict):
        return None, "the plan is not a JSON object"
    flags = {}
    for k in ("requires_sql", "requires_retrieval", "retrieval_depends_on_sql"):
        if not isinstance(obj.get(k), bool):
            return None, f"{k} must be true or false"
        flags[k] = obj[k]
    tasks = obj.get("sql_tasks", [])
    if not isinstance(tasks, list) or len(tasks) > limits["max_sql_tasks"]:
        return None, f"sql_tasks must be a list of at most {limits['max_sql_tasks']} tasks"
    clean, seen = [], set()
    for t in tasks:
        if not (isinstance(t, dict) and isinstance(t.get("task_id"), str) and isinstance(t.get("description"), str)
                and t["task_id"].strip() and t["description"].strip()):
            return None, "every SQL task needs a task_id and a description"
        if t["task_id"] in seen:
            return None, f"duplicate task_id {t['task_id']}"
        seen.add(t["task_id"])
        clean.append(dict(task_id=t["task_id"].strip(), description=t["description"].strip()))
    if flags["requires_sql"] and not clean:
        return None, "requires_sql is true but there is no SQL task"
    purpose = obj.get("retrieval_purpose", "")
    return dict(flags, sql_tasks=clean, retrieval_purpose=purpose if isinstance(purpose, str) else ""), None


def validate_queries(obj, limits):
    qs = obj.get("queries") if isinstance(obj, dict) else None
    if not isinstance(qs, list) or not 1 <= len(qs) <= limits["max_queries"] or not all(isinstance(q, str) and q.strip() for q in qs):
        return None, f"queries must be a list of 1 to {limits['max_queries']} non-empty strings"
    if any(len(q.split()) > limits["max_query_words"] for q in qs):
        return None, f"a query exceeds {limits['max_query_words']} words"
    return [q.strip() for q in qs], None


class SqlOnlyLLM:
    """The frozen SQL agent asks for an answer after the SQL ran; the hybrid synthesis writes the answer, so that call is skipped."""

    def __init__(self, inner):
        self.inner = inner
        self.name = getattr(inner, "name", "llm")

    def complete(self, task, system, user):
        if task == "answer":
            return "(the answer is written by the hybrid synthesis step)"
        return self.inner.complete(task, system, user)


def format_sql_results(tasks, rows_cap):
    if not tasks:
        return "(no SQL task was run)"
    blocks = []
    for t in tasks:
        head = f"[sql:{t['task_id']}] {t['description']}"
        if t["status"] != "ok":
            blocks.append(f"{head}\n(the SQL task failed: {t.get('error')})")
            continue
        lines = [head, f"SQL: {t['sql']}", " | ".join(t["columns"])]
        lines += [" | ".join(str(v) for v in r) for r in t["rows"][:rows_cap]]
        if t["row_count"] > rows_cap:
            lines.append(f"(... {t['row_count'] - rows_cap} more rows not shown)")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def run_question(q, pspec, retriever, text, llm, db_path, record):
    """Mutates `record` step by step so a provider error in the middle still leaves the partial work saved."""
    P, L = pspec["prompts"], pspec["limits"]
    plan, err, raw = None, None, None
    for _ in range(L["plan_attempts"]):
        raw = llm.complete("plan", P["planner_system"], P["planner_user"].format(question=q["question"]))
        obj, err = extract_json(raw)
        if obj is not None:
            plan, err = validate_plan(obj, L)
        if plan:
            break
    record["plan_raw"] = raw
    record["oracle_context_ids"] = context_for(retriever, q["oracle_queries"])      # diagnostic only, never shown to a model
    if not plan:
        record.update(status="plan_failed", plan_error=err)
        return
    record["plan"] = plan
    for t in plan["sql_tasks"] if plan["requires_sql"] else []:
        st = run_agent(t["description"], SqlOnlyLLM(llm), db_path)
        res = st.get("result") or {}
        ok = st["status"] == "ok" and res.get("ok")
        record["tasks"].append(dict(task_id=t["task_id"], description=t["description"], status="ok" if ok else "failed",
                                    sql=st.get("sql"), attempts=st["attempts"], columns=res.get("columns", []) if ok else [],
                                    rows=res.get("rows", []) if ok else [], row_count=len(res.get("rows", [])) if ok else 0,
                                    error=None if ok else st.get("last_error")))
    sql_text = format_sql_results(record["tasks"], L["rows_per_task_in_prompts"])
    queries = []
    record["query_status"] = "not_requested"
    if plan["requires_retrieval"]:
        for _ in range(L["query_attempts"]):
            raw = llm.complete("queries", P["query_system"], P["query_user"].format(
                question=q["question"], purpose=plan["retrieval_purpose"] or "(not stated)", sql_results=sql_text))
            obj, err = extract_json(raw)
            queries, err = validate_queries(obj, L) if obj is not None else (None, err)
            if queries:
                break
        record["query_status"] = "ok" if queries else "failed_validation"
        if not queries:
            record["query_error"] = err
            queries = []
    record["queries"] = queries
    record["context_ids"] = context_for(retriever, queries) if queries else []
    ctx = "\n\n".join(P["doc_block"].format(doc_id=d, text=text[d].strip()) for d in record["context_ids"]) or "(none retrieved)"
    record["answer"] = llm.complete("synthesis", P["synthesis_system"], P["synthesis_user"].format(
        question=q["question"], sql_results=sql_text, context=ctx)).strip()
    record["status"] = "ok"


def run_hybrid(spec, spec_hash, pspec, pspec_hash, corpus, corpus_sha256, llm, db_path=DB_PATH, out_dir=None, now=None,
               threshold=2, deviation=False):
    """Returns (path, run_document, exit_code)."""
    sel = pspec["retrieval"]
    retriever = TwoLaneRetriever(corpus, sel["cases_per_query"], sel["guides_per_query"])
    text = {d.doc_id: d.text for d in corpus}
    out_dir = Path(out_dir or ROOT / "eval_results")
    out_dir.mkdir(parents=True, exist_ok=True)
    started = now or datetime.now()
    name = getattr(llm, "name", "llm").replace(":", "_").replace("/", "_")
    stem = f"hyb_{name}_{started:%Y%m%d_%H%M%S}"
    progress, final = out_dir / f"{stem}_INCOMPLETE.json", out_dir / f"{stem}.json"
    results, consecutive, stop = [], 0, None

    def save(state):
        complete = state == "complete"
        _write_atomic(final if complete else progress, dict(
            meta=dict(model=getattr(llm, "name", "llm"), state=state, complete=complete, hybrid_spec_sha256=spec_hash,
                      pipeline_spec_sha256=pspec_hash, corpus_sha256=corpus_sha256, started_at=started.isoformat(timespec="seconds"),
                      lanes=dict(cases_per_query=sel["cases_per_query"], guides_per_query=sel["guides_per_query"]),
                      deviation_from_registered_model=deviation, registered_model=pspec["models"]["all_stages"]),
            results=results))

    try:
        for idx, q in enumerate(spec["questions"]):
            rec = dict(id=q["id"], question=q["question"], status="running", plan=None, plan_raw=None, tasks=[], queries=[], query_status=None,
                       context_ids=[], oracle_context_ids=[], answer=None)
            try:
                run_question(q, pspec, retriever, text, llm, db_path, rec)
                consecutive = 0
            except Exception as e:  # noqa: BLE001 - provider-transient vs everything else
                provider = is_provider_transient(e)
                rec.update(status="error", error_class="provider" if provider else "runtime", error_type=type(e).__name__,
                           error_message=str(e)[:500])
                if not provider:
                    stop = "aborted_on_runtime_error"
                else:
                    consecutive += 1
                    if consecutive >= threshold:
                        stop = "circuit_breaker"
            results.append(rec)
            if stop:
                results.extend(dict(id=r["id"], status="skipped", reason=stop) for r in spec["questions"][idx + 1:])
            save("running")
            if stop:
                break
    except KeyboardInterrupt:
        save("interrupted")
        raise
    done = len(results) == len(spec["questions"]) and all(r["status"] in ("ok", "plan_failed") for r in results)
    if done:
        save("complete")
        progress.unlink(missing_ok=True)
        return final, dict(results=results), 0
    save("aborted" if stop == "aborted_on_runtime_error" else "incomplete")
    return progress, dict(results=results), 3 if stop == "aborted_on_runtime_error" else 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--allow-other-model", action="store_true")
    args = ap.parse_args()
    spec, h = load_frozen(BENCH_PATH, HASH_PATH)
    pspec, ph = load_pipeline_spec()
    registered = pspec["models"]["all_stages"]
    model = args.model or registered
    if model != registered and not args.allow_other_model:
        raise SystemExit(f"the pre-registered model is {registered}; use --allow-other-model only for availability reasons")
    corpus = load_corpus()
    path, doc, code = run_hybrid(spec, h, pspec, ph, corpus, verify_corpus(corpus), GeminiLLM(model=model), deviation=model != registered)
    ok = sum(r["status"] == "ok" for r in doc["results"])
    pf = sum(r["status"] == "plan_failed" for r in doc["results"])
    print(f"model {model}: {ok} answered, {pf} plan_failed, of {len(spec['questions'])} questions\nresult file: {path}")
    if code:
        bad = [r for r in doc["results"] if r["status"] == "error"]
        print("INCOMPLETE run. First error:", bad[0]["error_type"], bad[0]["error_message"] if bad else "")
        print("Do not score an incomplete run. Rerun, or switch model for the WHOLE run (availability reasons only).")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
