"""Turn the SEALED Hybrid run (hybrid-1.0, first run) plus its FINAL human rubric into replay traces with an evaluation annotation.

  python -m demo.convert_sealed

Reads only sealed files (the run, the final rubric csv, the frozen specs). Nothing is re-run and no model is called. The evaluation section
carries the human scores, the frozen and the post-hoc attribution, and the diagnostic context of the standard queries; it exists only in
sealed-evaluation traces. Re-running the conversion rewrites the same bytes.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from rag import evaluate_hybrid as ev  # noqa: E402
from rag.build_benchmark import load_frozen  # noqa: E402
from rag.build_hybrid_benchmark import BENCH_PATH, HASH_PATH  # noqa: E402
from rag.build_hybrid_pipeline_spec import load_pipeline_spec  # noqa: E402
from rag.corpus import load_corpus, verify_corpus  # noqa: E402

from .gallery import rebuild_index  # noqa: E402
from .trace import build_trace, dump_json, validate_trace  # noqa: E402

HIST = ROOT / "benchmark_history" / "results_hybrid_v1"
STEM = "hyb_gemini_gemini-3_5-flash-lite_20261007_004003"
TITLES = {"H1": ("Database vs other: resolution time", "success_example"),
          "H2": ("Repeat incidents counted as pairs", "failure_case"),
          "H3": ("Critical incidents in distributed deployments", "success_example"),
          "H4": ("Map-service spike and the v3.2 association", "partial_example"),
          "H5": ("Open incidents and historical precedents", "mixed_case")}
CASE_NOTES = {
    "H1": "All SQL facts, the guide's first check and every citation are correct.",
    "H2": ("SQL semantic failure. The planner rewrote the question's definition of a repeat incident as 'pairs of incidents' and the SQL counted unordered "
           "pairs (crs_mismatch 37), not distinct repeat incidents (the frozen definition gives timeout 25). The answer is consistent with its own SQL result and "
           "with the retrieved case, so the trace shows an upstream error, not a generation hallucination. Frozen and post-hoc attribution agree: propagated from SQL."),
    "H3": "All SQL facts, the guide's first checks and every citation are correct.",
    "H4": ("Correct on the Q1 spike (40 incidents), on v3.2 (32 of 40), on the association-versus-causation statement and on a concrete failure mode (INC_0070). "
           "The 2026 Q3 count was never computed because the planner restricted the task to three quarters; the frozen rubric required it, although the question says "
           "'surrounding quarters', which is a specification defect disclosed in the sealed summary."),
    "H5": ("Right project and the right four incidents, but the SQL task has no project filter (the rows are right only because no other project has such incidents). "
           "INC_0192 found a symptomatically similar precedent (INC_0049). INC_0185 is a query-formulation failure: the model's query contained the project name, BM25 returned a "
           "same-project Tile Cache TIMEOUT case, the model rightly did not call it similar, while the standard-query context contains the real tile-cache precedent. "
           "INC_0188 and INC_0200 correctly report that no documented similar case was found and assert no root cause. The frozen attribution labels both documented facts "
           "'propagated from SQL' because of the SQL-method rejection; the post-hoc, result-based attribution shows the real mechanism.")}


def convert(out_dir=ROOT / "traces", run_path=HIST / f"{STEM}.json", rubric=HIST / f"rubric_{STEM}_FINAL.csv"):
    spec, sh = load_frozen(BENCH_PATH, HASH_PATH)
    pspec, ph = load_pipeline_spec()
    run = ev.load_run(run_path)
    corpus = load_corpus()
    if run["meta"]["hybrid_spec_sha256"] != sh or run["meta"]["pipeline_spec_sha256"] != ph or run["meta"]["corpus_sha256"] != verify_corpus(corpus):
        raise SystemExit("the sealed run does not match the frozen specifications or the corpus")
    docs = {d.doc_id: d for d in corpus}
    text = {k: d.text for k, d in docs.items()}
    ms = ev.machine_state(spec, pspec, run, text)
    human = ev.read_scores(rubric, spec)
    summ = ev.summarize(spec, pspec, run, ms, human)
    specs = dict(hybrid_benchmark_sha256=sh, pipeline_spec_sha256=ph, corpus_sha256=run["meta"]["corpus_sha256"],
                 sql_agent_code_sha256=pspec["depends_on"]["sql_agent_code_sha256"])
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    recs = {r["id"]: r for r in run["results"]}
    written = []
    for q in spec["questions"]:
        r, m = recs[q["id"]], ms[q["id"]]
        evaluation = dict(
            benchmark="hybrid-1.0", benchmark_sha256=sh, pipeline_spec_sha256=ph, run_file=Path(run_path).name, question_id=q["id"],
            scoring="final, sealed", provenance="human scores proposed by an external reviewer, verified against the run, adopted by the project author",
            route_compliant=m["route"]["compliant"],
            sql_facts=[dict(id=f["id"], statement=f["statement"], machine_matched_task=m["sql"][f["id"]]["matched_task"],
                            human_sql_ok=bool(human[(q["id"], f"sqlok:{f['id']}")]), answer_states_it=bool(human[(q["id"], f"says:{f['id']}")]))
                       for f in q["sql_facts"]],
            doc_facts=[dict(id=d["id"], statement=d["statement"], evidence_in_agent_context=m["evidence_agent"][d["id"]],
                            evidence_in_oracle_context=m["evidence_oracle"][d["id"]], covered=bool(human[(q["id"], f"covered:{d['id']}")]),
                            frozen_attribution=summ["attribution_per_fact"][q["id"]][d["id"]],
                            posthoc_attribution=summ["posthoc_result_based_per_fact"][q["id"]][d["id"]]) for d in q["doc_facts"]],
            requirements=[dict(id=y["id"], statement=y["statement"], met=bool(human[(q["id"], f"req:{y['id']}")])) for y in q.get("synthesis_requirements", [])],
            unsupported_claims=human[(q["id"], ev.COUNT_ITEM)],
            oracle_context_ids=r["oracle_context_ids"],
            oracle_note="documents the benchmark's standard queries retrieve; a diagnostic that was never shown to any model",
            case_note=CASE_NOTES[q["id"]])
        title, role = TITLES[q["id"]]
        trace = build_trace(r, trace_id=q["id"], kind="sealed_evaluation", title=title, model=run["meta"]["model"], specs=specs, docs_by_id=docs,
                            evaluation=evaluation, recorded_at=run["meta"]["started_at"], role=role)
        problems = validate_trace(trace, docs)
        if problems:
            raise SystemExit(f"{q['id']}: invalid trace: {problems}")
        (out / f"{q['id']}.json").write_bytes(dump_json(trace))
        written.append(q["id"])
    rebuild_index(out)
    return written


if __name__ == "__main__":
    print("wrote traces:", convert())
