"""Frozen specification of the Hybrid pipeline and its scoring contract (hybrid-pipeline-1.0). Built and FROZEN before any model call.

Pipeline (one question): planner -> SQL tasks (the frozen SQL agent, unchanged) -> retrieval-query generator -> two-lane BM25
retrieval -> synthesis. The oracle-query retrieval is run only as a diagnostic and is never shown to any model.

Information boundaries (enforced by construction and checked by tests):
  planner          sees: the question
  SQL agent        sees: one task description + the public database schema (its own frozen prompts)
  query generator  sees: the question, the planner's retrieval purpose, the ACTUAL SQL results
  synthesis        sees: the question, the ACTUAL SQL results, the documents retrieved with the AGENT's queries
  nobody           sees: gold values, ground_truth files, oracle queries / oracle context, true root causes of open incidents

  python -m rag.build_hybrid_pipeline_spec            # build + freeze
  python -m rag.build_hybrid_pipeline_spec --check
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from .build_benchmark import load_frozen, sha256  # noqa: E402
from .build_hybrid_benchmark import BENCH_PATH as HYBRID_PATH, HASH_PATH as HYBRID_HASH  # noqa: E402

SPEC_PATH = ROOT / "benchmarks" / "hybrid_pipeline_v1.json"
SPEC_HASH = ROOT / "benchmarks" / "hybrid_pipeline_v1.sha256"

PLANNER_SYSTEM = (
    "You plan how to answer questions about enterprise project delivery. Two tools exist: (1) a SQL database with tables about "
    "incidents, projects, deployments, components and customers (severity, status, resolution times, root causes of closed incidents, "
    "environment types, versions); (2) a document corpus of historical incident case reports, troubleshooting guides and deployment "
    "guides.\n"
    "Reply with ONE JSON object and nothing else:\n"
    "{\"requires_sql\": true or false, \"requires_retrieval\": true or false, \"retrieval_depends_on_sql\": true or false, "
    "\"sql_tasks\": [{\"task_id\": \"t1\", \"description\": \"...\"}], \"retrieval_purpose\": \"...\"}\n"
    "Rules:\n"
    "- Each SQL task is one self-contained analytics question that a SQL agent can answer with a single query. State the period, "
    "filters, metric and grouping explicitly, and use the granularity the question asks for (for example per quarter, not per month).\n"
    "- At most 4 SQL tasks. Do not write SQL.\n"
    "- retrieval_depends_on_sql is true when what must be looked up in the documents depends on a SQL result (for example a root "
    "cause or an incident identified by SQL).\n"
    "- retrieval_purpose says in one sentence what should be looked up in the documents.")
PLANNER_USER = "Question: {question}"

QUERY_SYSTEM = (
    "You write search queries for a document corpus (historical incident cases and troubleshooting guides) from a question and SQL "
    "results. Reply with ONE JSON object and nothing else: {\"queries\": [\"...\"]}.\n"
    "Rules:\n"
    "- 1 to 4 queries, each at most 40 words.\n"
    "- Use the concrete entities, root causes, components, versions or symptom descriptions that appear in the SQL results.\n"
    "- One query per distinct thing to look up (for example one per incident).\n"
    "- Do not invent facts that are not in the SQL results.")
QUERY_USER = "Question: {question}\n\nWhat to look up: {purpose}\n\nSQL results:\n{sql_results}"

SYNTHESIS_SYSTEM = (
    "You answer questions about enterprise project delivery using ONLY the SQL results and the documents provided.\n"
    "Rules:\n"
    "1. Numbers and entities must come from the SQL results. After each SQL-derived statement cite its task id, for example [sql:t1].\n"
    "2. Cite document-derived statements with the document id exactly as written in its header, for example "
    "[troubleshooting_guides/crs_mismatch].\n"
    "3. Documents whose id starts with historical_cases/ describe PAST incidents. You may call a past case similar when its documented "
    "symptoms and failure mode are consistent with the symptoms observed in the incident. Sharing only the component, the project or the "
    "environment does not make a case similar, and a different data volume, project or environment alone does not make a similar case "
    "dissimilar. When the root cause of an incident is not recorded in the SQL results (for example an open incident), never state that "
    "its root cause is established or identical to the past case's root cause; describe the past case as a symptomatically similar precedent.\n"
    "4. If no provided case is similar to an incident, say explicitly that no documented similar case was found for it; you may still give "
    "the general checks recommended by the guides in the context.\n"
    "5. Distinguish association from causation: counts and co-occurrence do not establish that something caused something else. Do not "
    "state a cause as established unless a cited document supports it for this exact situation.\n"
    "6. Do not use knowledge from outside the SQL results and the documents. Keep the answer under 250 words.")
SYNTHESIS_USER = "Question: {question}\n\nSQL results:\n{sql_results}\n\nDocuments:\n\n{context}"

DOC_BLOCK = "### Document id: {doc_id}\n{text}"

DEPENDS_ON_SQL = {"H1-d1": ["H1-s4"], "H2-d1": ["H2-s1"], "H3-d1": ["H3-s3"], "H4-d1": ["H4-s5"],
                  "H5-d-INC_0185": ["H5-s3"], "H5-d-INC_0192": ["H5-s3"]}


def sql_agent_fingerprint():
    """SHA-256 over the frozen SQL agent's code (text mode, so CRLF/LF differences do not matter): the pipeline must reuse it unchanged."""
    import hashlib
    h = hashlib.sha256()
    for name in ("agent.py", "prompts.py", "tools.py"):
        h.update(name.encode() + b"\0" + (ROOT / "sql_agent" / name).read_text(encoding="utf-8").encode("utf-8") + b"\0")
    return h.hexdigest()


def build():
    hybrid, hybrid_hash = load_frozen(HYBRID_PATH, HYBRID_HASH)
    sel = hybrid["pre_registered"]["selected"]
    doc = dict(
        pipeline_version="hybrid-pipeline-1.0", frozen=True,
        depends_on=dict(hybrid_benchmark_sha256=hybrid_hash, corpus_sha256=hybrid["depends_on"]["corpus_sha256"],
                        sql_agent_code_sha256=sql_agent_fingerprint(),
                        sql_agent="the SQL agent core (agent.py, prompts.py, tools.py: prompts, checker, read-only execution, retry flow) is "
                                  "fingerprinted and must be unchanged; llm.py and evaluate.py are outside the fingerprint; its answer step is "
                                  "skipped (SqlOnlyLLM wrapper in the pipeline) because the hybrid synthesis writes the answer"),
        models=dict(all_stages="gemini-3.5-flash-lite",
                    rule="one model per run for every stage; a different model only for availability reasons, recorded as a deviation, "
                         "never because of scores", sampling="provider defaults"),
        limits=dict(max_sql_tasks=4, max_queries=4, max_query_words=40, rows_per_task_in_prompts=30, plan_attempts=2,
                    query_attempts=2, llm_calls="not frozen (implementation detail)"),
        retrieval=dict(lanes="BM25 two-lane (cases / guides), document granularity, k1=1.5, b=0.75",
                       cases_per_query=sel["cases_per_query"], guides_per_query=sel["guides_per_query"],
                       context_rule=hybrid["pre_registered"]["context_rule"],
                       oracle_diagnostic="the same rule on each question's oracle_queries; stored, never shown to a model"),
        prompts=dict(planner_system=PLANNER_SYSTEM, planner_user=PLANNER_USER, query_system=QUERY_SYSTEM, query_user=QUERY_USER,
                     synthesis_system=SYNTHESIS_SYSTEM, synthesis_user=SYNTHESIS_USER, doc_block=DOC_BLOCK,
                     citation_formats=["[doc_id]", "[sql:<task_id>]"]),
        outcomes=dict(query_status="per question: ok | failed_validation (no valid retrieval queries after 2 attempts; synthesis still runs "
                                   "with an empty context, which is a query-formulation failure to be scored) | not_requested (the plan did not ask for retrieval)",
                      ok="all stages ran", plan_failed="the planner reply was not a valid plan after the allowed attempts: scored as a "
                      "route-compliance failure, downstream stages not run", error="provider error after the adapter's retries",
                      skipped="not run after a circuit break or an abort"),
        run_rules=dict(provider_errors="two consecutive provider errors stop the run (circuit breaker); any other exception aborts it; "
                       "plan_failed is a scored outcome, not an error; incomplete runs are never scored"),
        scoring_contract=dict(
            route_compliance="plan equals expected_plan on requires_sql, requires_retrieval, retrieval_depends_on_sql",
            sql_stage="machine screening per hybrid-1.0 sql_binding (one task result, same row, <= 30 rows for standalone numbers, exact "
                      "H5 id set); human item sqlok:<fact> confirms that a recorded task really computes the quantity",
            retrieval_stage="evidence per doc_fact in the AGENT context and in the ORACLE context (machine, deterministic)",
            synthesis_stage="human items says:<sql fact>, covered:<doc fact>, req:<requirement>, unsupported_claims; machine checks: "
                            "citation validity for [doc_id] and [sql:task], numbers not supported by any SQL result flagged",
            doc_fact_depends_on_sql=DEPENDS_ON_SQL,
            attribution=[
                "question with outcome plan_failed -> planner_failure for every fact (downstream stages were not run)",
                "doc fact whose SQL dependency was rejected by the human (sqlok = 0) -> propagated_from_sql",
                "not covered, evidence in the agent context -> synthesis_failure",
                "not covered, evidence absent from the agent context but present in the oracle context -> query_formulation_failure",
                "not covered, evidence absent from both -> retriever_context_failure",
                "covered with evidence in the agent context -> covered; covered without it -> covered_without_context_evidence (flag)"],
            number_support=dict(tolerance=0.06, excluded="numbers that occur in the question text, years, and quarter digits",
                                meaning="a flag for human review, not a verdict"),
            no_composite_score=True, secondary_judge="not part of hybrid-pipeline-1.0"),
        disclosures=["Prompts were written after the Hybrid benchmark was frozen and before any model call.",
                     "Synthesis rule 3 defines similarity operationally as consistency of the OBSERVED symptoms and the documented failure mode, "
                     "not as an identical root cause: the root cause of an open incident is unknown to the agent, so a rule requiring a matching "
                     "root cause would force either over-caution or an unsupported root-cause claim (the G06 lesson). Found in a pre-run review; "
                     "the sealed generation prompt had the same weakness.",
                     "Similarity for open incidents rests on symptom / failure-mode consistency and does not establish the incident's root cause; "
                     "the stricter wording about shared components is there to limit false precedents for the incidents without a documented case.",
                     "Synthesis rule 5 states the association-versus-causation rule explicitly; H4 therefore measures compliance with that "
                     "prompt, not behaviour under a neutral prompt.",
                     "SQL results must come from SQL tasks at the granularity the question asks for; coarser results aggregated by the "
                     "synthesis step are not counted as SQL-stage successes."])
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def freeze():
    data = build()
    SPEC_PATH.write_bytes(data)
    SPEC_HASH.write_text(f"{sha256(data)}  {SPEC_PATH.name}\n", encoding="utf-8")
    return sha256(data)


def load_pipeline_spec():
    """Load the pipeline spec; it must still match its hash AND the frozen hybrid benchmark it depends on."""
    spec, h = load_frozen(SPEC_PATH, SPEC_HASH)
    hybrid_hash = HYBRID_HASH.read_text().split()[0]
    if spec["depends_on"]["hybrid_benchmark_sha256"] != hybrid_hash:
        raise SystemExit("the pipeline spec was frozen against a different Hybrid benchmark")
    if spec["depends_on"]["sql_agent_code_sha256"] != sql_agent_fingerprint():
        raise SystemExit("the SQL agent code changed since the pipeline spec was frozen; it must be reused unchanged")
    return spec, h


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.check:
        same = build() == SPEC_PATH.read_bytes() and sha256(SPEC_PATH.read_bytes()) == SPEC_HASH.read_text().split()[0]
        print("frozen hybrid pipeline spec matches a fresh build" if same else "MISMATCH")
        raise SystemExit(0 if same else 1)
    print("frozen sha256:", freeze())


if __name__ == "__main__":
    main()
