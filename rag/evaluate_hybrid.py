"""Scoring of the Hybrid benchmark, stage by stage (route -> SQL -> retrieval -> synthesis). No composite score.

  1. python -m rag.evaluate_hybrid sheet --run eval_results\\hyb_<...>.json     -> rubric_hyb_<...>.csv + .md reading aid
  2. fill human_score (1 / 0 for every item; unsupported_claims is a count)
  3. python -m rag.evaluate_hybrid score --run ... --human rubric_hyb_<...>.csv

Machine parts: route compliance, SQL screening under the frozen sql_binding rules, evidence in the AGENT context and (diagnostically) in
the ORACLE context, citation validity, numbers not supported by any SQL result (a flag for review, not a verdict).
Human parts (primary): sqlok:<fact> confirms that a recorded SQL task really computes the quantity, says:<fact> that the answer states
it, covered:<doc fact>, req:<requirement>, unsupported_claims. The scorer never reads gold values from anywhere except the frozen spec.
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from .build_benchmark import load_frozen  # noqa: E402
from .build_hybrid_benchmark import BENCH_PATH, HASH_PATH, available  # noqa: E402
from .build_hybrid_pipeline_spec import load_pipeline_spec  # noqa: E402
from .corpus import load_corpus, verify_corpus  # noqa: E402

DOC_CITE = re.compile(r"\[((?:historical_cases|troubleshooting_guides|deployment_guides)/[A-Za-z0-9_]+)\]")
SQL_CITE = re.compile(r"\[sql:([A-Za-z0-9_\-]+)\]")
NUMBER = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w])")
INC = re.compile(r"INC_\d{4}")
COUNT_ITEM = "unsupported_claims"


def load_run(path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not doc["meta"].get("complete"):
        raise SystemExit("this run is incomplete (provider errors, circuit breaker or abort); incomplete runs are never scored")
    return doc


# ------------------------------------------------------------------ machine parts
def route_compliance(rec, expected):
    plan = rec.get("plan")
    if not plan:
        return dict(valid_plan=False, compliant=False, flags=None)
    flags = {k: plan[k] for k in expected}
    return dict(valid_plan=True, compliant=flags == expected, flags=flags)


def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _num_ok(cell, fact):
    if not _is_num(cell):
        return False
    m = fact["match"]
    return float(cell) == float(fact["gold"]) if m["kind"] == "integer" else abs(float(cell) - float(fact["gold"])) <= m.get("tolerance", 0)


def _str_ok(cell, fact):
    return isinstance(cell, str) and cell.strip().lower() == str(fact["gold"]).lower()


def screen_sql_facts(question, tasks, binding):
    """For every sql_fact: the first task whose OWN result satisfies the binding rules (never the union of results)."""
    facts = {f["id"]: f for f in question["sql_facts"]}
    pair = {}
    for a, b in binding["same_row"]:
        if a in facts and b in facts:
            pair[a], pair[b] = b, a
    out = {fid: dict(matched_task=None, rule=None) for fid in facts}
    ok_tasks = [t for t in tasks if t.get("status") == "ok"]
    for t in ok_tasks:
        rows = t["rows"]
        for a, b in binding["same_row"]:
            if a not in facts or b not in facts or out[a]["matched_task"]:
                continue
            ent, num = (facts[a], facts[b]) if facts[a]["match"]["kind"] == "string" else (facts[b], facts[a])
            if any(any(_str_ok(c, ent) for c in r) and any(_num_ok(c, num) for c in r) for r in rows):
                out[a] = out[b] = dict(matched_task=t["task_id"], rule="entity and number in the same row")
        for fid, f in facts.items():
            if out[fid]["matched_task"] or fid in pair:
                continue
            kind = f["match"]["kind"]
            if kind in ("number", "integer"):
                if len(rows) <= binding["max_rows_for_numbers"] and any(_num_ok(c, f) for r in rows for c in r):
                    out[fid] = dict(matched_task=t["task_id"], rule=f"standalone number in a result of at most {binding['max_rows_for_numbers']} rows")
            elif kind == "set":
                ids = set(INC.findall(" ".join(str(c) for r in rows for c in r)))
                if ids == set(f["gold"]):
                    out[fid] = dict(matched_task=t["task_id"], rule="id set equals the gold set exactly")
            elif kind == "string":
                if any(_str_ok(c, f) for r in rows for c in r):
                    out[fid] = dict(matched_task=t["task_id"], rule="string found in one task result")
    for fid, f in facts.items():
        if f["match"]["kind"] == "boolean":
            out[fid] = dict(matched_task=None, rule="derived: confirmed by the human only")
    return out


def citation_checks(rec):
    ans = rec.get("answer") or ""
    task_ids = {t["task_id"] for t in rec.get("tasks", [])}
    doc_cited = list(dict.fromkeys(DOC_CITE.findall(ans)))
    sql_cited = list(dict.fromkeys(SQL_CITE.findall(ans)))
    return dict(doc_cited=doc_cited, invalid_doc=[d for d in doc_cited if d not in rec.get("context_ids", [])],
                sql_cited=sql_cited, invalid_sql=[s for s in sql_cited if s not in task_ids],
                n_marks=len(DOC_CITE.findall(ans)) + len(SQL_CITE.findall(ans)))


def unsupported_numbers(rec, question, text_by_id, tol=0.06):
    """Numbers in the answer that appear in no SQL result, no retrieved document and not in the question (flags for review)."""
    ans = rec.get("answer") or ""
    cells = [float(c) for t in rec.get("tasks", []) if t.get("status") == "ok" for r in t["rows"] for c in r if _is_num(c)]
    docs = " ".join(text_by_id[d] for d in rec.get("context_ids", []))
    flagged = []
    for tok in NUMBER.findall(ans):
        if re.search(rf"(?<![\w.]){re.escape(tok)}(?![\w])", question) or tok in ("2025", "2026"):
            continue
        v = float(tok)
        if any(abs(v - c) <= tol for c in cells) or re.search(rf"(?<![\w.]){re.escape(tok)}(?![\w])", docs):
            continue
        flagged.append(tok)
    return flagged


# ------------------------------------------------------------------ human rubric
def items_for(q):
    items = []
    for f in q["sql_facts"]:
        items.append((f"sqlok:{f['id']}", f"SQL stage: one recorded SQL task correctly computes this quantity: {f['statement']}", "sqlok"))
        items.append((f"says:{f['id']}", f"The answer states this correctly: {f['statement']}", "says"))
    for d in q["doc_facts"]:
        items.append((f"covered:{d['id']}", f"The answer covers: {d['statement']}", "covered"))
    for y in q.get("synthesis_requirements", []):
        items.append((f"req:{y['id']}", y["statement"], "req"))
    items.append((COUNT_ITEM, "Number of statements in the answer that the SQL results and the provided documents do not support", "count"))
    return items


def machine_state(spec, pspec, run, text_by_id):
    binding = spec["scoring"]["sql_binding"]
    out = {}
    recs = {r["id"]: r for r in run["results"]}
    for q in spec["questions"]:
        r = recs[q["id"]]
        agent_av = available(q, r.get("context_ids", []), text_by_id)
        oracle_av = available(q, r.get("oracle_context_ids", []), text_by_id)
        out[q["id"]] = dict(
            route=route_compliance(r, q["expected_plan"]),
            sql=screen_sql_facts(q, r.get("tasks", []), binding),
            evidence_agent=agent_av, evidence_oracle=oracle_av,
            citations=citation_checks(r), unsupported_numbers=unsupported_numbers(r, q["question"], text_by_id))
    return out


def write_sheet(spec, run, ms, out_dir, stem, text_by_id, force=False):
    out_dir = Path(out_dir)
    csv_path, md_path = out_dir / f"rubric_{stem}.csv", out_dir / f"rubric_{stem}.md"
    if csv_path.exists() and not force:
        scored = [r for r in csv.DictReader(open(csv_path, encoding="utf-8-sig")) if (r.get("human_score") or "").strip()]
        if scored:
            raise SystemExit(f"{csv_path.name} already contains {len(scored)} scores; refusing to overwrite (use --force to start blank)")
    recs = {r["id"]: r for r in run["results"]}
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["question_id", "item_id", "item_text", "machine_note", "human_score", "notes"])
        for q in spec["questions"]:
            m = ms[q["id"]]
            for item_id, text, kind in items_for(q):
                note = ""
                if kind == "sqlok":
                    fid = item_id.split(":", 1)[1]
                    s = m["sql"][fid]
                    note = f"machine screening: matched task {s['matched_task']} ({s['rule']})" if s["matched_task"] else f"machine screening: no task result matches ({s['rule'] or 'binding rules'})"
                elif kind == "covered":
                    note = "evidence_in_agent_context=" + str(m["evidence_agent"].get(item_id.split(":", 1)[1]))
                w.writerow([q["id"], item_id, text, note, "", ""])
    lines = ["# Hybrid rubric reading aid", "",
             "Fill the `human_score` column: 1 = yes, 0 = no for every item; `unsupported_claims` is a count.",
             "sqlok: look at the recorded SQL task and decide whether it really computes the quantity (the machine note is only a screening).",
             "Judge ONLY against the SQL results and documents shown below. Do not use your own knowledge.", ""]
    for q in spec["questions"]:
        r, m = recs[q["id"]], ms[q["id"]]
        lines += [f"## {q['id']}", f"**Question:** {q['question']}", "", f"**Outcome:** {r['status']}"]
        if r.get("plan"):
            lines += ["", "**Plan:**", "```json", json.dumps({k: v for k, v in r["plan"].items() if k != "sql_tasks"}, indent=1), "```"]
        for t in r.get("tasks", []):
            lines += ["", f"**SQL task `{t['task_id']}`** ({t['status']}): {t['description']}", "```sql", str(t["sql"]), "```"]
            if t["status"] == "ok":
                lines += ["```", " | ".join(t["columns"])] + [" | ".join(str(v) for v in row) for row in t["rows"][:30]]
                lines += ([f"... {t['row_count'] - 30} more rows"] if t["row_count"] > 30 else []) + ["```"]
        lines += ["", f"**Retrieval queries written by the model** (query status: {r.get('query_status')}): " + json.dumps(r.get("queries", []), ensure_ascii=False),
                  "", "**Documents the model saw:** " + (", ".join(r.get("context_ids", [])) or "(none)"), ""]
        for d in r.get("context_ids", []):
            lines += [f"<details><summary>{d}</summary>", "", "```", text_by_id[d].strip(), "```", "", "</details>", ""]
        lines += ["**Answer:**", "", r.get("answer") or "(no answer: the plan failed)", "", "**Machine flags:**",
                  f"- invalid document citations: {m['citations']['invalid_doc']}; invalid sql citations: {m['citations']['invalid_sql']}",
                  f"- numbers in the answer that no SQL result or document supports: {m['unsupported_numbers']}", "", "**Items:**"]
        lines += [f"- `{i}`: {t}" for i, t, _ in items_for(q)] + [""]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, md_path


def read_scores(path, spec):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    expected = {(q["id"], i): kind for q in spec["questions"] for i, _, kind in items_for(q)}
    got = {}
    for r in rows:
        key = (r["question_id"], r["item_id"])
        raw = (r.get("human_score") or "").strip()
        if key not in expected:
            raise SystemExit(f"unknown item {key} in {path}")
        if raw == "":
            raise SystemExit(f"human_score is blank for {key}; every item must be scored")
        try:
            v = int(raw)
        except ValueError:
            raise SystemExit(f"human_score for {key} must be an integer, got {raw!r}")
        if v < 0 or (expected[key] != "count" and v not in (0, 1)):
            raise SystemExit(f"human_score for {key} must be 0 or 1 (count item: 0 or more), got {v}")
        got[key] = v
    if set(expected) - set(got):
        raise SystemExit(f"{len(set(expected) - set(got))} items missing in {path}")
    return got


# ------------------------------------------------------------------ attribution and summary
def attribute(q, rec, ms, human, pspec):
    """Failure attribution per doc fact, following the frozen taxonomy."""
    deps = pspec["scoring_contract"]["doc_fact_depends_on_sql"]
    out = {}
    for d in q["doc_facts"]:
        did = d["id"]
        if rec["status"] == "plan_failed":
            out[did] = "planner_failure"
            continue
        if any(human[(q["id"], f"sqlok:{s}")] == 0 for s in deps.get(did, [])):
            out[did] = "propagated_from_sql"
            continue
        covered = human[(q["id"], f"covered:{did}")] == 1
        in_agent, in_oracle = ms["evidence_agent"][did], ms["evidence_oracle"][did]
        if covered:
            out[did] = "covered" if in_agent else "covered_without_context_evidence"
        elif in_agent:
            out[did] = "synthesis_failure"
        elif in_oracle:
            out[did] = "query_formulation_failure"
        else:
            out[did] = "retriever_context_failure"
    return out


def attribute_result_based(q, rec, ms, human, pspec):
    """POST-HOC view (never replaces the frozen attribution). The frozen rule marks a doc fact 'propagated_from_sql' whenever the
    human rejected a dependency (sqlok = 0), even if that rejection concerns the METHOD of a task whose RESULT was right. Here a
    dependency propagates only when the SQL result itself was wrong: the human rejected it AND the machine screening did not
    find the gold value either (facts the machine cannot screen, such as booleans, rely on the human alone)."""
    deps = pspec["scoring_contract"]["doc_fact_depends_on_sql"]
    out = {}
    for d in q["doc_facts"]:
        did = d["id"]
        if rec["status"] == "plan_failed":
            out[did] = "planner_failure"
            continue
        wrong = [s for s in deps.get(did, []) if human[(q["id"], f"sqlok:{s}")] == 0 and not ms["sql"][s]["matched_task"]]
        if wrong:
            out[did] = "propagated_from_sql"
            continue
        covered = human[(q["id"], f"covered:{did}")] == 1
        in_agent, in_oracle = ms["evidence_agent"][did], ms["evidence_oracle"][did]
        out[did] = (("covered" if in_agent else "covered_without_context_evidence") if covered else
                    "synthesis_failure" if in_agent else "query_formulation_failure" if in_oracle else "retriever_context_failure")
    return out


def summarize(spec, pspec, run, ms, human):
    recs = {r["id"]: r for r in run["results"]}
    s = dict(query_status=dict(Counter(r.get("query_status") for r in recs.values())),
             route=dict(n=len(recs), compliant=sum(ms[i]["route"]["compliant"] for i in recs),
                        plan_failed=sum(r["status"] == "plan_failed" for r in recs.values()),
                        note="route compliance, not routing accuracy: all questions expect the same plan"))
    sql = dict(facts=0, machine_matched=0, human_confirmed=0, both_yes=0, both_no=0, machine_yes_human_no=0, machine_no_human_yes=0,
               per_question={})
    retr = dict(doc_facts=0, evidence_in_agent_context=0, evidence_in_oracle_context=0)
    attrib, per_fact, posthoc, posthoc_per_fact = Counter(), {}, Counter(), {}
    syn = dict(sql_facts_stated=0, doc_facts_covered=0, requirements=0, requirements_met=0)
    cit = dict(answers=0, invalid_doc_citations=0, invalid_sql_citations=0, answers_without_citation=0,
               answers_with_unsupported_numbers=0)
    unsupported = []
    for q in spec["questions"]:
        r, m = recs[q["id"]], ms[q["id"]]
        pq = []
        for f in q["sql_facts"]:
            mach = bool(m["sql"][f["id"]]["matched_task"])
            hum = human[(q["id"], f"sqlok:{f['id']}")] == 1
            sql["facts"] += 1
            sql["machine_matched"] += mach
            sql["human_confirmed"] += hum
            sql["both_yes" if mach and hum else "both_no" if not mach and not hum else "machine_yes_human_no" if mach else "machine_no_human_yes"] += 1
            syn["sql_facts_stated"] += human[(q["id"], f"says:{f['id']}")]
            pq.append(f"{f['id']}:{'ok' if hum else 'no'}")
        sql["per_question"][q["id"]] = pq
        for d in q["doc_facts"]:
            retr["doc_facts"] += 1
            retr["evidence_in_agent_context"] += m["evidence_agent"][d["id"]]
            retr["evidence_in_oracle_context"] += m["evidence_oracle"][d["id"]]
            syn["doc_facts_covered"] += human[(q["id"], f"covered:{d['id']}")]
        for y in q.get("synthesis_requirements", []):
            syn["requirements"] += 1
            syn["requirements_met"] += human[(q["id"], f"req:{y['id']}")]
        a = attribute(q, r, m, human, pspec)
        per_fact[q["id"]] = a
        attrib.update(a.values())
        posthoc.update(attribute_result_based(q, r, m, human, pspec).values())
        posthoc_per_fact[q["id"]] = attribute_result_based(q, r, m, human, pspec)
        c = m["citations"]
        cit["answers"] += 1
        cit["invalid_doc_citations"] += len(c["invalid_doc"])
        cit["invalid_sql_citations"] += len(c["invalid_sql"])
        cit["answers_without_citation"] += c["n_marks"] == 0
        cit["answers_with_unsupported_numbers"] += bool(m["unsupported_numbers"])
        unsupported.append(human[(q["id"], COUNT_ITEM)])
    s.update(sql_stage=sql, retrieval_stage=retr, failure_attribution=dict(attrib), attribution_per_fact=per_fact,
             posthoc_result_based_attribution=dict(posthoc), posthoc_result_based_per_fact=posthoc_per_fact,
             posthoc_note="post-hoc, does not replace the frozen attribution: a SQL dependency propagates only when the SQL RESULT was wrong "
                          "(human rejected it and the machine screening did not find the gold value either)",
             synthesis_stage=syn, citations=cit,
             unsupported_claims=dict(total=sum(unsupported), answers_with_any=sum(u > 0 for u in unsupported), n=len(unsupported)),
             note="no composite score; five questions form a smoke-level benchmark; H5 is a deliberately selected challenge slice")
    return s


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sheet")
    p.add_argument("--run", required=True)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("score")
    p.add_argument("--run", required=True)
    p.add_argument("--human", required=True)
    args = ap.parse_args()
    spec, sh = load_frozen(BENCH_PATH, HASH_PATH)
    pspec, ph = load_pipeline_spec()
    run = load_run(args.run)
    meta = run["meta"]
    if meta["hybrid_spec_sha256"] != sh or meta["pipeline_spec_sha256"] != ph:
        raise SystemExit("the run was produced with a different Hybrid benchmark or pipeline spec")
    corpus = load_corpus()
    if meta["corpus_sha256"] != verify_corpus(corpus):
        raise SystemExit("the run was produced on a different document corpus")
    text = {d.doc_id: d.text for d in corpus}
    ms = machine_state(spec, pspec, run, text)
    stem = Path(args.run).stem
    if args.cmd == "sheet":
        c, m = write_sheet(spec, run, ms, Path(args.run).parent, stem, text, force=args.force)
        print(f"rubric sheet: {c}\nreading aid : {m}\nFill human_score for every row.")
        return
    human = read_scores(args.human, spec)
    result = dict(run=Path(args.run).name, model=meta["model"], summary=summarize(spec, pspec, run, ms, human))
    out = Path(args.run).parent / f"score_{stem}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nsaved: {out}")


if __name__ == "__main__":
    main()
