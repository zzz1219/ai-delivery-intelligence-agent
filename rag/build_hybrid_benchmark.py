"""Specification of the Hybrid benchmark (hybrid-1.0): questions that need BOTH the SQL database and the document corpus.
Built and FROZEN before any model is run.

Design rules carried over from the earlier benchmarks:
  * required facts map one-to-one to what each question explicitly asks; useful extras are `bonus_facts`, reported separately
    and never part of the primary denominators
  * gold values are computed from the frozen database; unique winners with a clear margin are asserted at build time
  * question texts are distinct and do not leak gold values
  * a no-LLM ORACLE-PLAN audit: the gold SQL result is turned into retrieval queries by pre-registered templates, and the
    two-lane retriever must put the required evidence into the context. Planning/SQL failures and retrieval failures can then
    be told apart later (stage-wise scoring: route -> SQL -> retrieval -> synthesis).
  * the benchmark freezes stages and information boundaries, NOT the number of LLM calls (an implementation detail)

  python -m rag.build_hybrid_benchmark            # audit, build, freeze
  python -m rag.build_hybrid_benchmark --audit    # print the audit table only
  python -m rag.build_hybrid_benchmark --check    # a fresh build must equal the frozen file
"""
import argparse
import csv
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import generate_data as g  # noqa: E402
from .build_benchmark import sha256  # noqa: E402
from .build_generation_benchmark import GUIDE_OF_ROOT, _supports  # noqa: E402
from .corpus import load_corpus  # noqa: E402
from .lanes import TwoLaneRetriever  # noqa: E402

BENCH_PATH = ROOT / "benchmarks" / "hybrid_benchmark_v1.json"
HASH_PATH = ROOT / "benchmarks" / "hybrid_benchmark_v1.sha256"
GRID_CASES, GRID_GUIDES = (1, 2, 3), (1, 2, 3)

SQL = dict(
    h1_groups="""SELECT CASE WHEN category='database' THEN 'database' ELSE 'other' END AS grp,
  ROUND(AVG((julianday(resolved_at)-julianday(reported_at))*24),1) AS mean_hours, COUNT(*) AS n
FROM incidents WHERE status='closed' GROUP BY grp""",
    h1_root_causes="""SELECT root_cause, ROUND(AVG((julianday(resolved_at)-julianday(reported_at))*24),1) AS mean_hours, COUNT(*) AS n
FROM incidents WHERE status='closed' GROUP BY root_cause ORDER BY mean_hours DESC""",
    h2_repeat_by_root_cause="""-- repeat incident: a closed incident with another closed incident of the same project, component and root_cause within 90 days
WITH r AS (SELECT DISTINCT a.incident_id AS id, a.root_cause AS rc FROM incidents a JOIN incidents b
  ON a.project_id=b.project_id AND a.component_id=b.component_id AND a.root_cause=b.root_cause AND a.incident_id<>b.incident_id
  AND ABS(julianday(a.reported_at)-julianday(b.reported_at))<=90 WHERE a.status='closed' AND b.status='closed')
SELECT rc, COUNT(*) AS repeat_incidents FROM r GROUP BY rc ORDER BY repeat_incidents DESC""",
    h3_critical_distributed="""SELECT ROUND(AVG((julianday(i.resolved_at)-julianday(i.reported_at))*24),1) AS mean_hours, COUNT(*) AS n
FROM incidents i JOIN deployments d ON d.deployment_id=i.deployment_id
WHERE i.severity='critical' AND d.env_type='distributed' AND i.status='closed'""",
    h3_root_causes="""SELECT i.root_cause, COUNT(*) AS n FROM incidents i JOIN deployments d ON d.deployment_id=i.deployment_id
WHERE i.severity='critical' AND d.env_type='distributed' AND i.status='closed' GROUP BY i.root_cause ORDER BY n DESC""",
    h4_quarters="""SELECT strftime('%Y',reported_at)||'-Q'||((CAST(strftime('%m',reported_at) AS INTEGER)+2)/3) AS quarter, COUNT(*) AS incidents
FROM incidents WHERE category='map_service' GROUP BY quarter ORDER BY quarter""",
    h4_q1_versions="""SELECT d.version, COUNT(*) AS incidents FROM incidents i JOIN deployments d ON d.deployment_id=i.deployment_id
WHERE i.category='map_service' AND i.reported_at >= '2026-01-01' AND i.reported_at < '2026-04-01'
GROUP BY d.version ORDER BY incidents DESC""",
    h5_open_by_project="""SELECT project_id, COUNT(*) AS open_incidents FROM incidents WHERE status='open' GROUP BY project_id ORDER BY open_incidents DESC""",
    h5_incidents="""SELECT i.incident_id, i.component_id, c.name, d.env_type, i.symptom_summary FROM incidents i
JOIN components c ON c.component_id=i.component_id JOIN deployments d ON d.deployment_id=i.deployment_id
WHERE i.status='open' AND i.severity='high' AND i.category='map_service' AND i.project_id=:project_id ORDER BY i.incident_id""")

PLAN = dict(requires_sql=True, requires_retrieval=True, retrieval_depends_on_sql=True)
STAGES = dict(
    route="ROUTE COMPLIANCE (not routing accuracy): the structured plan must say that SQL is needed, that retrieval is needed, and that "
          "the retrieval query depends on the SQL result (expected_plan). All five questions expect the same plan, so a planner that "
          "always answers 'hybrid' would score 5/5; SQL-only / RAG-only / direct-answer controls are out of scope",
    sql="every sql_fact is found in the result of ONE SQL task (never in the union of all results), under the sql_binding rules; the "
        "SQL executed; machine matching is a screening step and the human rubric confirms each fact against the recorded task",
    retrieval="the evidence of every doc_fact is in the context built from the AGENT's own retrieval queries; the same context rule is "
              "also run deterministically on the ORACLE queries (built from the gold SQL result) as a diagnostic control that is never "
              "shown to the model",
    synthesis="the answer states the required facts, uses numbers consistent with the SQL results, cites only documents in the "
              "context, and respects the evidence boundary (association is not causation; no invented precedent)")
ATTRIBUTION = [
    "order of the failure taxonomy: planner / route compliance -> SQL task or execution -> retrieval-query formulation -> "
    "retriever / context configuration -> synthesis; reported in four stages (planning, SQL, retrieval, synthesis) with the retrieval "
    "stage decomposed into query formulation vs retriever / context",
    "if the SQL stage fails, downstream doc_facts are labelled 'propagated from SQL' and are not counted as retrieval failures",
    "SQL correct, evidence absent from the agent's context, oracle-query context contains it -> retrieval-query formulation failure",
    "SQL correct, evidence absent from the agent's context AND absent from the oracle-query context -> retriever / context failure",
    "evidence present in the agent's context but missing in the answer -> synthesis failure",
    "a doc_fact covered without evidence in the context is flagged (possible outside knowledge)"]
INFORMATION_BOUNDARY = [
    "the agent sees the question, the public database (open incidents have no root cause) and the document corpus",
    "the agent never sees ground_truth files, the true root causes of open incidents, or any gold value"]


def _rows(conn, sql, params=None):
    return conn.execute(sql, params or {}).fetchall()


def build_questions(conn, truth, manifest, text):
    docs_by_root = {}
    for r in manifest:
        docs_by_root.setdefault(r["root_cause"], []).append(f"historical_cases/{r['incident_id']}")
    qs = []

    # ---------------- H1
    grp = {r[0]: r for r in _rows(conn, SQL["h1_groups"])}
    rc = _rows(conn, SQL["h1_root_causes"])
    assert rc[0][1] - rc[1][1] >= 3.0, "H1: highest-mean root cause must lead by >= 3 hours"
    top = rc[0][0]
    steps = g.TROUBLESHOOTING[f"{GUIDE_OF_ROOT[top].split('/')[1]}.md"][3]
    first_step = steps[0]
    qs.append(dict(
        id="H1", expected_plan=PLAN,
        question="Among closed incidents, are database incidents slower to resolve on average than non-database incidents? "
                 "Across closed incidents, which root cause has the highest mean resolution time, and what does its "
                 "troubleshooting guide recommend checking first?",
        sql_reference=["h1_groups", "h1_root_causes"],
        sql_facts=[
            dict(id="H1-s1", statement=f"The mean resolution time of closed database incidents is {grp['database'][1]} hours.", gold=grp["database"][1], match=dict(kind="number", tolerance=0.15)),
            dict(id="H1-s2", statement=f"The mean resolution time of closed non-database incidents is {grp['other'][1]} hours.", gold=grp["other"][1], match=dict(kind="number", tolerance=0.15)),
            dict(id="H1-s3", statement="Database incidents are slower to resolve than non-database incidents.", gold=True, match=dict(kind="boolean")),
            dict(id="H1-s4", statement=f"The root cause with the highest mean resolution time is {top}.", gold=top, match=dict(kind="string")),
            dict(id="H1-s5", statement=f"Its mean resolution time is {rc[0][1]} hours.", gold=rc[0][1], match=dict(kind="number", tolerance=0.15))],
        doc_facts=[dict(id="H1-d1", kind="guide_fact", statement=f"Its troubleshooting guide recommends checking first: {first_step}",
                        evidence_docs=[GUIDE_OF_ROOT[top]])],
        bonus_facts=["The remaining diagnostic steps and the common fixes of that guide."],
        oracle_queries=[f"troubleshooting guide diagnostic order first check for {top}"]))

    # ---------------- H2
    rep = _rows(conn, SQL["h2_repeat_by_root_cause"])
    assert rep[0][1] >= 1.5 * rep[1][1], "H2: top repeat root cause must lead by >= 1.5x"
    top2 = rep[0][0]
    lesson = g.LESSON[top2]
    cases2 = sorted(docs_by_root[top2])
    qs.append(dict(
        id="H2", expected_plan=PLAN,
        question="Define a repeat incident as a closed incident for which at least one other closed incident exists with the same "
                 "project, component and root cause within 90 days (before or after it). Under this definition, which root cause "
                 "has the most repeat incidents, and what lesson learned is recorded in similar historical cases?",
        sql_reference=["h2_repeat_by_root_cause"],
        sql_facts=[
            dict(id="H2-s1", statement=f"The root cause with the most repeat incidents is {top2}.", gold=top2, match=dict(kind="string")),
            dict(id="H2-s2", statement=f"It has {rep[0][1]} repeat incidents.", gold=rep[0][1], match=dict(kind="integer"))],
        doc_facts=[dict(id="H2-d1", kind="case_fact", statement=f"The lesson learned recorded in similar historical cases: {lesson}",
                        evidence=dict(all_of=[lesson]), relevant_cases=cases2)],
        bonus_facts=["The resolution approaches recorded in those cases."],
        oracle_queries=[f"lessons learned from historical {top2} incidents"]))

    # ---------------- H3
    h3 = _rows(conn, SQL["h3_critical_distributed"])[0]
    h3rc = _rows(conn, SQL["h3_root_causes"])
    assert h3rc[0][1] >= 2 * h3rc[1][1], "H3: most common root cause must lead by >= 2x"
    top3 = h3rc[0][0]
    first3 = g.TROUBLESHOOTING[f"{GUIDE_OF_ROOT[top3].split('/')[1]}.md"][3][0]
    qs.append(dict(
        id="H3", expected_plan=PLAN,
        question="For closed critical incidents in distributed deployments, what is the mean resolution time and how many such "
                 "incidents are there? What is the most common root cause among them, and what does its troubleshooting guide "
                 "recommend checking first?",
        sql_reference=["h3_critical_distributed", "h3_root_causes"],
        sql_facts=[
            dict(id="H3-s1", statement=f"The mean resolution time is {h3[0]} hours.", gold=h3[0], match=dict(kind="number", tolerance=0.15)),
            dict(id="H3-s2", statement=f"There are {h3[1]} such incidents.", gold=h3[1], match=dict(kind="integer")),
            dict(id="H3-s3", statement=f"The most common root cause among them is {top3}.", gold=top3, match=dict(kind="string")),
            dict(id="H3-s4", statement=f"It accounts for {h3rc[0][1]} of them.", gold=h3rc[0][1], match=dict(kind="integer"))],
        doc_facts=[dict(id="H3-d1", kind="guide_fact", statement=f"Its troubleshooting guide recommends checking first: {first3}",
                        evidence_docs=[GUIDE_OF_ROOT[top3]])],
        bonus_facts=["The remaining diagnostic steps and the common fixes of that guide."],
        oracle_queries=[f"troubleshooting guide diagnostic order first check for {top3}"]))

    # ---------------- H4
    quarters = dict(_rows(conn, SQL["h4_quarters"]))
    vers = _rows(conn, SQL["h4_q1_versions"])
    q1 = quarters["2026-Q1"]
    assert q1 >= 2 * max(v for k, v in quarters.items() if k != "2026-Q1")
    top_ver, top_ver_n = vers[0]
    markers = ["Root cause: tile_cache_misconfig", "Root cause: timeout", "Root cause: crs_mismatch"]    # concrete failure modes
    q1_ids = {r[0] for r in conn.execute("SELECT incident_id FROM incidents WHERE category='map_service' "
                                         "AND reported_at >= '2026-01-01' AND reported_at < '2026-04-01'")}
    h4_cases = sorted(f"historical_cases/{r['incident_id']}" for r in manifest if r["incident_id"] in q1_ids)
    supporting = h4_cases
    qs.append(dict(
        id="H4", expected_plan=PLAN,
        question="Did map-service incidents spike in 2026 Q1 compared with the surrounding quarters? Which deployment version was "
                 "associated with most of the Q1 incidents, does the available evidence establish that this version caused the "
                 "spike, and what concrete failure mode is documented in at least one relevant historical case?",
        sql_reference=["h4_quarters", "h4_q1_versions"],
        sql_facts=[
            dict(id="H4-s1", statement=f"Map-service incidents in 2025-Q4: {quarters['2025-Q4']}.", gold=quarters["2025-Q4"], match=dict(kind="integer")),
            dict(id="H4-s2", statement=f"Map-service incidents in 2026-Q1: {q1}.", gold=q1, match=dict(kind="integer")),
            dict(id="H4-s3", statement=f"Map-service incidents in 2026-Q2: {quarters['2026-Q2']}.", gold=quarters["2026-Q2"], match=dict(kind="integer")),
            dict(id="H4-s4", statement=f"Map-service incidents in 2026-Q3: {quarters['2026-Q3']}.", gold=quarters["2026-Q3"], match=dict(kind="integer")),
            dict(id="H4-s5", statement=f"The version associated with most Q1 incidents is {top_ver}.", gold=top_ver, match=dict(kind="string")),
            dict(id="H4-s6", statement=f"{top_ver} accounts for {top_ver_n} of the {q1} Q1 incidents.", gold=top_ver_n, match=dict(kind="integer"))],
        synthesis_requirements=[
            dict(id="H4-y1", statement="The answer says the spike coincides with / is associated with the version."),
            dict(id="H4-y2", statement="The answer does not present the version as the established cause of the spike (the incident counts alone "
                                       "establish association, not causation); citing a documented mechanism in individual cases as a "
                                       "plausible explanation is allowed.")],
        doc_facts=[dict(id="H4-d1", kind="case_fact",
                        statement="At least one documented map-service case from 2026 Q1 describes a concrete failure mode (for example "
                                  "tile-cache scale levels reset to defaults, request timeouts, or a coordinate-system mismatch).",
                        evidence=dict(any_of=markers), relevant_cases=supporting)],
        bonus_facts=["All documented failure modes of Q1 map-service cases (tile cache, timeouts, coordinate-system mismatch).",
                     "Some cases state that the problem first appeared shortly after the upgrade to v3.2 (temporal evidence only; it "
                     "does not by itself satisfy the failure-mode fact)."],
        oracle_queries=[f"map_service incidents after the upgrade to {top_ver} documented failure modes"]))

    # ---------------- H5
    openp = _rows(conn, SQL["h5_open_by_project"])
    assert openp[0][1] - openp[1][1] >= 2
    proj = openp[0][0]
    inc = _rows(conn, SQL["h5_incidents"], {"project_id": proj})
    ids = [r[0] for r in inc]
    roots = {i: truth[i]["true_root_cause"] for i in ids}
    nomatch = sorted(i for i in ids if not docs_by_root.get(roots[i]))
    match = sorted(i for i in ids if docs_by_root.get(roots[i]))
    assert len(nomatch) == 2 and len(match) == 2 and len({roots[i] for i in match}) == 2, (roots, nomatch, match)
    assert len({r[4] for r in inc}) == len(inc), "H5: the four incidents must have distinct symptom texts"
    queries = [f"{r[4]} Component: {r[2]}. Environment: {r[3].replace('_', ' ')}." for r in inc]
    doc_facts = []
    for r in inc:
        if r[0] in match:
            doc_facts.append(dict(id=f"H5-d-{r[0]}", kind="case_fact", incident_id=r[0],
                                  statement=f"For {r[0]} a documented similar historical case exists (same failure mode).",
                                  evidence={}, relevant_cases=sorted(docs_by_root[roots[r[0]]])))
    qs.append(dict(
        id="H5", expected_plan=PLAN,
        question="Which project currently has the most open incidents? For that project's open high-severity incidents in the "
                 "map-service layer, have we seen similar cases before, and if so which?",
        sql_reference=["h5_open_by_project", "h5_incidents"],
        sql_facts=[
            dict(id="H5-s1", statement=f"The project with the most open incidents is {proj}.", gold=proj, match=dict(kind="string")),
            dict(id="H5-s2", statement=f"It has {openp[0][1]} open incidents.", gold=openp[0][1], match=dict(kind="integer")),
            dict(id="H5-s3", statement="Its open high-severity incidents in the map-service layer are: " + ", ".join(ids) + ".",
                 gold=ids, match=dict(kind="set"))],
        doc_facts=doc_facts,
        synthesis_requirements=[dict(id=f"H5-y-{i}", statement=f"For {i} the answer states that no documented similar case was found "
                                                              "and does not present an unrelated case as a precedent or assert a root cause.",
                                     incident_id=i) for i in nomatch],
        bonus_facts=["General checks from the service-publication troubleshooting guide for the two incidents without a documented case."],
        oracle_queries=queries, scored_incidents=dict(with_documented_case=match, without_documented_case=nomatch)))
    return qs


def _supports_fact(fact, text_by_id, doc):
    return _supports(fact, text_by_id[doc]) if "evidence" in fact else False


def available(question, ctx, text):
    out = {}
    cases = [d for d in ctx if d.startswith("historical_cases/")]
    for f in question["doc_facts"]:
        if f["kind"] == "guide_fact":
            out[f["id"]] = all(d in ctx for d in f["evidence_docs"])
        else:
            out[f["id"]] = any(d in f["relevant_cases"] and _supports(f, text[d]) for d in cases)
    return out


def context_for(retriever, queries):
    """Pre-registered union rule: for every query the case lane then the guide lane; all cases first (in query order), then guides."""
    cases, guides = [], []
    for q in queries:
        c, gd = retriever.retrieve_scored(q)
        cases += [h["doc_id"] for h in c if h["doc_id"] not in cases]
        guides += [h["doc_id"] for h in gd if h["doc_id"] not in guides]
    return cases + guides


def audit(questions, corpus):
    text = {d.doc_id: d.text for d in corpus}
    words = {d.doc_id: len(d.text.split()) for d in corpus}
    rows = []
    for kc in GRID_CASES:
        for kg in GRID_GUIDES:
            r = TwoLaneRetriever(corpus, kc, kg)
            ok_all, per_q, ctx_words = True, {}, []
            for q in questions:
                ctx = context_for(r, q["oracle_queries"])
                av = available(q, ctx, text)
                per_q[q["id"]] = sum(av.values()), len(av)
                ok_all &= all(av.values())
                ctx_words.append(sum(words[d] for d in ctx))
            rows.append(dict(cases_per_query=kc, guides_per_query=kg, total=kc + kg, mean_context_words=round(sum(ctx_words) / len(ctx_words)),
                             evidence=" ".join(f"{k}:{a}/{b}" for k, (a, b) in per_q.items()), sufficient=ok_all))
    ok = [r for r in rows if r["sufficient"]]
    pick = min(ok, key=lambda r: (r["total"], r["mean_context_words"], r["cases_per_query"])) if ok else None
    return rows, pick


def build():
    corpus = load_corpus()
    text = {d.doc_id: d.text for d in corpus}
    truth = {r["incident_id"]: r for r in csv.DictReader(open(ROOT / "ground_truth/incidents_truth.csv", encoding="utf-8"))}
    manifest = list(csv.DictReader(open(ROOT / "ground_truth/doc_manifest.csv", encoding="utf-8")))
    conn = sqlite3.connect(f"file:{(ROOT / 'data/delivery.db').as_posix()}?mode=ro", uri=True)
    questions = build_questions(conn, truth, manifest, text)
    # every relevant case must support the single-case facts so that ANY retrieved relevant case does
    for q in questions:
        for f in q["doc_facts"]:
            if f["kind"] == "case_fact":
                assert f["relevant_cases"] and all(_supports(f, text[d]) for d in f["relevant_cases"]), f["id"]
            else:
                assert f["statement"].split(": ", 1)[1].rstrip(".") in text[f["evidence_docs"][0]], f["id"]
    rows, pick = audit(questions, corpus)
    if pick is None:
        raise SystemExit("no configuration in the grid is sufficient; review the oracle query templates before freezing")
    doc = dict(
        benchmark_version="hybrid-1.0", frozen=True,
        depends_on=dict(corpus_sha256=(ROOT / "benchmarks/corpus.sha256").read_text().split()[0], data_seed=g.FROZEN_SEED,
                        lanes="BM25 two-lane retrieval as in rag-generation-1.0"),
        stages=STAGES, failure_attribution=ATTRIBUTION, information_boundary=INFORMATION_BOUNDARY,
        scoring=dict(
            primary="stage-wise results with machine checks and a human rubric on all questions; no composite score",
            required_vs_bonus="sql_facts, doc_facts and synthesis_requirements are required and form the primary denominators; "
                              "bonus_facts are optional and reported separately",
            sql_fact_matching="number: |value - gold| <= tolerance; integer: equal; string: equal ignoring case; boolean: stated; set: equal as sets",
            sql_binding=dict(
                scope="each sql_fact must be found inside ONE SQL task result, never in the union of all results",
                same_row=[["H1-s4", "H1-s5"], ["H2-s1", "H2-s2"], ["H3-s3", "H3-s4"], ["H4-s5", "H4-s6"], ["H5-s1", "H5-s2"]],
                same_row_meaning="an entity and its number must appear in the same row of the same result",
                max_rows_for_numbers=30,
                max_rows_meaning="a standalone number only counts when its result set has at most 30 rows (aggregates, not raw incident dumps)",
                set_rule="H5-s3: the set of incident ids in one result equals the gold set exactly (no extra ids)",
                recorded_per_task=["task_id (chosen by the planner)", "description", "generated_sql", "columns", "rows"],
                why_not_planner_ids="task ids are invented by the planner, so facts are bound by content (single result, same row, small result) "
                                    "instead of by name; separate tasks for the mean and for the count are therefore both accepted",
                confirmation="machine matching is a screening step; the human rubric confirms every sql_fact against the recorded task"),
            route_metric="route compliance (requires_sql, requires_retrieval, retrieval_depends_on_sql); never reported as routing accuracy",
            retrieval_diagnostics="the retrieval stage is run twice: once on the agent's own queries (scored) and once, deterministically, on the "
                                  "oracle queries (diagnostic only, never shown to the model); no extra LLM call is needed",
            evidence_split_for_doc_facts=["covered", "missed_context_available (synthesis failure)",
                                          "missed_evidence_not_retrieved (retrieval failure)", "propagated_from_sql",
                                          "covered_without_context_evidence (flag)"],
            secondary="a frozen LLM judge may be added as a secondary experiment; agreement with the human rubric is reported",
            llm_calls="not frozen: the number of LLM calls per question is an implementation detail"),
        pre_registered=dict(
            oracle_plan="retrieval queries are built from the gold SQL result with the templates stored in each question's oracle_queries",
            context_rule="for every query: case lane top k then guide lane top k; all cases first (query order), then guides; duplicates removed",
            audit_rule="smallest k_cases + k_guides per query (ties: fewer words, then fewer cases) such that the evidence of EVERY doc_fact of "
                       "EVERY question is in the context; no model output is used",
            audit_grid=rows, selected=dict(cases_per_query=pick["cases_per_query"], guides_per_query=pick["guides_per_query"])),
        disclosures=[
            "H1-H3 were rewritten after the SQL benchmark and the generation benchmark were completed: the earlier H1-H3 could be answered "
            "from SQL alone, and their anchors were fragile (16 vs 15, 11 vs 9).",
            "Required facts follow what each question asks; lessons from the generation benchmark (checklist beyond the question wording) are applied.",
            "H5 is restricted by a natural SQL filter (open, high severity, map-service layer) that yields 2 incidents without any documented "
            "case and 2 with documented cases of distinct root causes; this was chosen after inspecting the open incidents of the top project.",
            "The repeat-incident definition is stated in H2's text and in its reference SQL (closed incidents only, +-90 days).",
            "H5 is a deliberately selected diagnostic (challenge) slice, not a representative sample of open incidents.",
            "Route compliance is not routing accuracy: all five questions expect the same plan; no SQL-only, RAG-only or direct-answer controls.",
            "H4's fact H4-d1 requires a concrete failure mode; the remark 'shortly after the upgrade to v3.2' is temporal evidence only and is a "
            "bonus fact (a first draft of the spec accepted it as sufficient; fixed before any model run).",
            "Five questions: a smoke-level benchmark, not a statistically meaningful sample."],
        sql_reference={k: v for k, v in SQL.items()}, questions=questions)
    return (json.dumps(doc, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8")


def freeze():
    data = build()
    BENCH_PATH.write_bytes(data)
    HASH_PATH.write_text(f"{sha256(data)}  {BENCH_PATH.name}\n", encoding="utf-8")
    return sha256(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.audit:
        doc = json.loads(build())
        for r in doc["pre_registered"]["audit_grid"]:
            print(f"cases {r['cases_per_query']} guides {r['guides_per_query']} total {r['total']} words {r['mean_context_words']:>5}  {r['evidence']}  sufficient={r['sufficient']}")
        print("selected:", doc["pre_registered"]["selected"])
        return
    if args.check:
        same = build() == BENCH_PATH.read_bytes() and sha256(BENCH_PATH.read_bytes()) == HASH_PATH.read_text().split()[0]
        print("frozen hybrid benchmark matches a fresh build" if same else "MISMATCH")
        raise SystemExit(0 if same else 1)
    print("frozen sha256:", freeze())


if __name__ == "__main__":
    main()
