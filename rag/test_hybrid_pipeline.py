"""Plain-assert tests for the Hybrid pipeline and scorer. Every model is a fake; no network, no API call.
Run from the delivery_agent directory:  python -m rag.test_hybrid_pipeline"""
import csv
import json
import re
import tempfile
from datetime import datetime
from pathlib import Path

from . import build_hybrid_benchmark as bh
from . import build_hybrid_pipeline_spec as bp
from . import evaluate_hybrid as ev
from . import hybrid_pipeline as hp
from .build_benchmark import load_frozen
from .corpus import corpus_fingerprint, load_corpus

NOW = datetime(2026, 10, 7, 21, 0, 0)
SPEC, SPEC_HASH = load_frozen(bh.BENCH_PATH, bh.HASH_PATH)
PSPEC, PSPEC_HASH = bp.load_pipeline_spec()
CORPUS = load_corpus()
TEXT = {d.doc_id: d.text for d in CORPUS}
SQL = {k: v.replace(":project_id", "'P03'") for k, v in bh.SQL.items()}

PLANS = {
    "H1": [("t1", "Mean resolution hours of closed incidents, database versus non-database", "h1_groups"),
           ("t2", "Mean resolution hours by root cause across closed incidents", "h1_root_causes")],
    "H2": [("t1", "Repeat incident counts by root cause under the 90-day definition", "h2_repeat_by_root_cause")],
    "H3": [("t1", "Mean resolution hours and number of closed critical incidents in distributed deployments", "h3_critical_distributed"),
           ("t2", "Root cause counts among those incidents", "h3_root_causes")],
    "H4": [("t1", "Map-service incidents per quarter", "h4_quarters"),
           ("t2", "Q1 2026 map-service incidents per deployment version", "h4_q1_versions")],
    "H5": [("t1", "Open incidents per project", "h5_open_by_project"),
           ("t2", "Open high-severity map-service incidents of the project with the most open incidents", "h5_incidents")]}
DESC2SQL = {d: SQL[k] for tasks in PLANS.values() for _, d, k in tasks}


class _Err(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


class FakeHybridLLM:
    """A well-behaved model: valid plans, the reference SQL for each task, the oracle queries, an answer that states every gold fact."""
    name = "fake-hyb"

    def __init__(self, plan_override=None, queries_override=None, raise_on=None, plan_text_override=None, queries_text_override=None):
        self.prompts, self.plan_override, self.queries_override = [], plan_override or {}, queries_override or {}
        self.raise_on, self.plan_text_override = raise_on or {}, plan_text_override or {}
        self.queries_text_override = queries_text_override or {}

    def _qid(self, user):
        for q in SPEC["questions"]:
            if q["question"] in user:
                return q["id"]
        raise AssertionError("question not found in the prompt")

    def complete(self, task, system, user):
        self.prompts.append((task, system, user))
        assert task != "answer", "the SQL agent's answer step must be skipped"
        if task == "sql":
            desc = re.search(r"Question: (.*)", user).group(1).strip()
            return f"```sql\n{DESC2SQL[desc]}\n```"
        qid = self._qid(user)
        if qid in self.raise_on and task == self.raise_on[qid][0]:
            raise self.raise_on[qid][1]()
        q = next(x for x in SPEC["questions"] if x["id"] == qid)
        if task == "plan":
            if qid in self.plan_text_override:
                return self.plan_text_override[qid]
            plan = dict(requires_sql=True, requires_retrieval=True, retrieval_depends_on_sql=True,
                        sql_tasks=[dict(task_id=t, description=d) for t, d, _ in PLANS[qid]], retrieval_purpose="look up the documents")
            plan.update(self.plan_override.get(qid, {}))
            return "```json\n" + json.dumps(plan) + "\n```"
        if task == "queries":
            if qid in self.queries_text_override:
                return self.queries_text_override[qid]
            return json.dumps({"queries": self.queries_override.get(qid, q["oracle_queries"])})
        assert task == "synthesis"
        ids = re.findall(r"### Document id: (\S+)", user)
        sql_part = " ".join(f"{f['statement']} [sql:t1]" for f in q["sql_facts"])
        doc_part = " ".join(f"{d['statement']} [{ids[0]}]" for d in q["doc_facts"] if ids)
        no_case = " ".join(y["statement"] for y in q.get("synthesis_requirements", []))
        return f"{sql_part} {doc_part} {no_case}".strip()


def _run(llm=None, out_dir=None, **kw):
    llm = llm or FakeHybridLLM()
    d = out_dir or tempfile.mkdtemp()
    path, doc, code = hp.run_hybrid(SPEC, SPEC_HASH, PSPEC, PSPEC_HASH, CORPUS, corpus_fingerprint(CORPUS), llm, out_dir=d, now=NOW, **kw)
    return llm, path, doc, code


def test_pipeline_spec_is_frozen_consistent_and_leak_free():
    assert bp.build() == bp.SPEC_PATH.read_bytes() and PSPEC["pipeline_version"] == "hybrid-pipeline-1.0"
    assert PSPEC["depends_on"]["hybrid_benchmark_sha256"] == SPEC_HASH and PSPEC["retrieval"]["cases_per_query"] == 1
    prompts = " ".join(str(v) for v in PSPEC["prompts"].values()).lower()
    for q in SPEC["questions"]:
        for f in q["sql_facts"]:
            if isinstance(f["gold"], str) and f["gold"] not in ("v3.2",) and not f["gold"].startswith("P0"):
                assert f["gold"].lower() not in prompts, f["gold"]                  # no root-cause name / gold entity in any prompt
        for oq in q["oracle_queries"]:
            assert oq.lower() not in prompts
    assert "oracle" not in prompts and "ground_truth" not in prompts
    assert "llm_calls" in PSPEC["limits"] and "not frozen" in PSPEC["limits"]["llm_calls"]
    assert "planner_failure" in " ".join(PSPEC["scoring_contract"]["attribution"])
    saved = bp.HYBRID_HASH
    try:                                                                            # frozen against another benchmark -> refused
        other = Path(tempfile.mkdtemp()) / "h.sha256"
        other.write_text("0" * 64 + "  x\n")
        bp.HYBRID_HASH = other
        bp.load_pipeline_spec()
        raise AssertionError("a different Hybrid benchmark must be refused")
    except SystemExit as e:
        assert "different Hybrid benchmark" in str(e)
    finally:
        bp.HYBRID_HASH = saved


def test_information_boundaries():
    llm, path, doc, code = _run()
    assert code == 0
    by = {}
    for task, system, user in llm.prompts:
        by.setdefault(task, []).append((system, user))
    gold_tokens = {str(f["gold"]) for q in SPEC["questions"] for f in q["sql_facts"] if isinstance(f["gold"], (str, float))}
    oracle = {oq for q in SPEC["questions"] for oq in q["oracle_queries"]}
    for system, user in by["plan"]:
        assert user.startswith("Question: ") and user.count("\n") == 0, "the planner sees only the question"
        assert not any(t in user or t in system for t in gold_tokens if not t.startswith("P0") and t not in ("v3.2",)), "no gold value reaches the planner"
    for task, calls in by.items():                                                  # no oracle query text in ANY prompt of ANY stage
        for system, user in calls:
            assert not any(oq in system or oq in user for oq in oracle), task
    for system, user in by["queries"]:                                              # sees question + purpose + ACTUAL SQL results
        assert "SQL results:" in user and "[sql:t1]" in user
    # the synthesis prompt contains exactly the documents retrieved with the AGENT's queries
    recs = {r["id"]: r for r in doc["results"]}
    for (system, user), r in zip(by["synthesis"], doc["results"]):
        assert re.findall(r"### Document id: (\S+)", user) == r["context_ids"]
    # a bad agent query: the context is empty although the oracle context is not; the oracle is never shown
    llm2, path2, doc2, _ = _run(FakeHybridLLM(queries_override={"H3": ["zzzz qqqq"]}))
    h3 = next(r for r in doc2["results"] if r["id"] == "H3")
    assert h3["context_ids"] == [] and h3["oracle_context_ids"]
    syn_h3 = next(u for t, s, u in llm2.prompts if t == "synthesis" and SPEC["questions"][2]["question"] in u)
    assert "(none retrieved)" in syn_h3 and not any(d in syn_h3 for d in h3["oracle_context_ids"])


def test_orchestration_lifecycle_and_records():
    llm, path, doc, code = _run()
    assert code == 0 and path.name == "hyb_fake-hyb_20261007_210000.json" and [p.name for p in path.parent.iterdir()] == [path.name]
    saved = json.loads(path.read_text())
    assert saved["meta"]["complete"] and saved["meta"]["hybrid_spec_sha256"] == SPEC_HASH and saved["meta"]["pipeline_spec_sha256"] == PSPEC_HASH
    assert [r["status"] for r in saved["results"]] == ["ok"] * 5
    h3 = saved["results"][2]
    assert [t["task_id"] for t in h3["tasks"]] == ["t1", "t2"] and h3["tasks"][0]["columns"] == ["mean_hours", "n"]
    assert h3["tasks"][0]["rows"][0][1] == 8 and h3["tasks"][0]["sql"].upper().startswith("SELECT")
    assert len(h3["queries"]) == 1 and h3["context_ids"] and h3["oracle_context_ids"] and h3["answer"]
    h5 = saved["results"][4]
    assert len(h5["queries"]) == 4 and h5["tasks"][1]["row_count"] == 4


def test_plan_validation_and_plan_failed_is_a_scored_outcome():
    L = PSPEC["limits"]
    good = dict(requires_sql=True, requires_retrieval=True, retrieval_depends_on_sql=True,
                sql_tasks=[dict(task_id="t1", description="x")], retrieval_purpose="p")
    assert hp.validate_plan(good, L)[0]["sql_tasks"][0]["task_id"] == "t1"
    for bad in [dict(good, requires_sql="yes"), dict(good, sql_tasks=[dict(task_id="t1", description="a"), dict(task_id="t1", description="b")]),
                dict(good, sql_tasks=[dict(task_id=f"t{i}", description="d") for i in range(5)]), dict(good, sql_tasks=[]),
                dict(good, sql_tasks=[dict(task_id="t1", description=" ")])]:
        assert hp.validate_plan(bad, L)[0] is None
    assert hp.validate_queries({"queries": ["a b c"]}, L)[0] == ["a b c"]
    assert hp.validate_queries({"queries": ["w " * 41]}, L)[0] is None and hp.validate_queries({"queries": []}, L)[0] is None
    llm, path, doc, code = _run(FakeHybridLLM(plan_text_override={"H2": "I think we should just answer."}))
    assert code == 0                                                                # a bad plan is an outcome, not an error
    h2 = doc["results"][1]
    assert h2["status"] == "plan_failed" and h2["tasks"] == [] and h2["oracle_context_ids"] and "no JSON" in h2["plan_error"]
    assert sum(1 for t, s, u in llm.prompts if t == "plan" and SPEC["questions"][1]["question"] in u) == 2     # two attempts, no more
    assert ev.route_compliance(h2, SPEC["questions"][1]["expected_plan"]) == dict(valid_plan=False, compliant=False, flags=None)


def test_similarity_rule_fits_open_incidents_and_query_status_is_explicit():
    rule = PSPEC["prompts"]["synthesis_system"]
    r3 = rule[rule.index("3. Documents"): rule.index("4. If no provided case")]
    assert "consistent with the symptoms observed" in r3 and "not recorded in the SQL results" in r3
    assert "never state that its root cause is established" in r3 and "symptomatically similar precedent" in r3
    assert "symptoms and root cause) matches" not in rule and "(symptoms and root cause)" not in rule     # the old, unsatisfiable criterion is gone
    assert "Sharing only the component" in r3                                                          # limits false precedents for H5's no-case incidents
    assert "no documented similar case was found" in rule
    llm, path, doc, code = _run(FakeHybridLLM(queries_text_override={"H2": "not json at all"},
                                              plan_override={"H3": dict(requires_retrieval=False)}))
    st = {r["id"]: r["query_status"] for r in doc["results"]}
    assert st == {"H1": "ok", "H2": "failed_validation", "H3": "not_requested", "H4": "ok", "H5": "ok"} and code == 0
    h2 = doc["results"][1]
    assert h2["status"] == "ok" and h2["queries"] == [] and h2["context_ids"] == [] and "no JSON" in h2["query_error"]
    assert h2["oracle_context_ids"]                                                                      # the diagnostic is still computed
    assert sum(1 for t, s, u in llm.prompts if t == "queries" and SPEC["questions"][1]["question"] in u) == 2   # two attempts
    assert "query status: failed_validation" in ev.write_sheet(SPEC, json.loads(path.read_text()), _state(json.loads(path.read_text())),
                                                               Path(tempfile.mkdtemp()), "q", TEXT)[1].read_text(encoding="utf-8")
    assert "SQL agent core" in PSPEC["depends_on"]["sql_agent"] and "llm.py" in PSPEC["depends_on"]["sql_agent"]


def test_provider_errors_runtime_abort_and_partial_records():
    llm, path, doc, code = _run(FakeHybridLLM(raise_on={"H2": ("plan", lambda: _Err(503, "UNAVAILABLE")),
                                                         "H3": ("queries", lambda: _Err(429, "RESOURCE_EXHAUSTED"))}))
    st = [r["status"] for r in doc["results"]]
    assert st == ["ok", "error", "error", "skipped", "skipped"] and code == 2 and path.name.endswith("_INCOMPLETE.json")
    assert doc["results"][2]["tasks"] and doc["results"][2]["error_class"] == "provider"      # partial work was kept
    llm, path, doc, code = _run(FakeHybridLLM(raise_on={"H2": ("synthesis", lambda: TypeError("sdk changed"))}))
    assert code == 3 and doc["results"][1]["error_class"] == "runtime" and doc["results"][2]["status"] == "skipped"
    try:
        ev.load_run(path)
        raise AssertionError("an incomplete run must never be scored")
    except SystemExit as e:
        assert "incomplete" in str(e)


def _state(run_doc):
    return ev.machine_state(SPEC, PSPEC, run_doc, TEXT)


def test_machine_screening_on_a_perfect_run():
    llm, path, doc, code = _run()
    run = json.loads(path.read_text())
    ms = _state(run)
    for q in SPEC["questions"]:
        m = ms[q["id"]]
        assert m["route"]["compliant"] and not m["citations"]["invalid_doc"] and not m["citations"]["invalid_sql"], q["id"]
        assert all(m["evidence_agent"].values()) and all(m["evidence_oracle"].values()), q["id"]
        assert not m["unsupported_numbers"], (q["id"], m["unsupported_numbers"])
        for f in q["sql_facts"]:
            if f["match"]["kind"] == "boolean":
                assert m["sql"][f["id"]]["matched_task"] is None and "human" in m["sql"][f["id"]]["rule"]
            else:
                assert m["sql"][f["id"]]["matched_task"], (q["id"], f["id"])
    assert ms["H5"]["sql"]["H5-s3"]["matched_task"] == "t2" and ms["H3"]["sql"]["H3-s2"]["matched_task"] == "t1"


def _task(tid, columns, rows, status="ok"):
    return dict(task_id=tid, description="d", status=status, sql="SELECT 1", columns=columns, rows=rows, row_count=len(rows), error=None)


def test_sql_binding_rejects_coincidences_and_accepts_valid_decompositions():
    b = SPEC["scoring"]["sql_binding"]
    h3 = next(q for q in SPEC["questions"] if q["id"] == "H3")
    # the count 8 only inside a 40-row dump, the mean only as a string: nothing standalone matches
    dump = _task("t1", ["incident_id", "k"], [[f"INC_{i:04d}", 8 if i == 5 else i + 100] for i in range(40)])
    res = ev.screen_sql_facts(h3, [dump], b)
    assert res["H3-s2"]["matched_task"] is None and res["H3-s1"]["matched_task"] is None
    # entity and number in DIFFERENT rows: the pair does not match; in the same row it does
    wrong = _task("t1", ["root_cause", "n"], [["timeout", 9], ["tile_cache_misconfig", 4]])
    right = _task("t1", ["root_cause", "n"], [["timeout", 4], ["tile_cache_misconfig", 1]])
    assert ev.screen_sql_facts(h3, [wrong], b)["H3-s3"]["matched_task"] is None
    assert ev.screen_sql_facts(h3, [right], b)["H3-s3"]["matched_task"] == "t1" == ev.screen_sql_facts(h3, [right], b)["H3-s4"]["matched_task"]
    # a valid decomposition into three tasks is accepted (mean, count, distribution), each fact from ITS task
    res = ev.screen_sql_facts(h3, [_task("m", ["mean_hours"], [[35.4]]), _task("c", ["n"], [[8]]), _task("d", ["root_cause", "n"], [["timeout", 4]])], b)
    assert (res["H3-s1"]["matched_task"], res["H3-s2"]["matched_task"], res["H3-s3"]["matched_task"]) == ("m", "c", "d")
    # a failed task never matches; the mean outside the tolerance does not match
    assert ev.screen_sql_facts(h3, [_task("m", ["x"], [[35.4]], status="failed")], b)["H3-s1"]["matched_task"] is None
    assert ev.screen_sql_facts(h3, [_task("m", ["x"], [[35.9]])], b)["H3-s1"]["matched_task"] is None
    # H5: the id set must be exact and come from ONE result
    h5 = next(q for q in SPEC["questions"] if q["id"] == "H5")
    ids = h5["sql_facts"][2]["gold"]
    ok = _task("t", ["incident_id"], [[i] for i in ids])
    extra = _task("t", ["incident_id"], [[i] for i in ids + ["INC_0196"]])
    split = [_task("a", ["incident_id"], [[ids[0]], [ids[1]]]), _task("b", ["incident_id"], [[ids[2]], [ids[3]]])]
    assert ev.screen_sql_facts(h5, [ok], b)["H5-s3"]["matched_task"] == "t"
    assert ev.screen_sql_facts(h5, [extra], b)["H5-s3"]["matched_task"] is None and ev.screen_sql_facts(h5, split, b)["H5-s3"]["matched_task"] is None
    # H1: root cause + mean in the same row, the right numbers in the wrong rows do not match
    h1 = next(q for q in SPEC["questions"] if q["id"] == "H1")
    top = h1["sql_facts"][3]["gold"]
    assert ev.screen_sql_facts(h1, [_task("t", ["rc", "m"], [[top, 41.1], ["x", 1.0]])], b)["H1-s4"]["matched_task"] == "t"
    assert ev.screen_sql_facts(h1, [_task("t", ["rc", "m"], [[top, 1.0], ["x", 41.1]])], b)["H1-s4"]["matched_task"] is None


def test_citation_and_number_flags():
    rec = dict(answer="The mean is 99.9 hours [sql:t1] and 38.4 [sql:t9]; see [troubleshooting_guides/crs_mismatch] and [historical_cases/INC_0001]. "
                      "In 2026 Q1 there were 11 incidents of version v3.2.",
               tasks=[_task("t1", ["m"], [[38.4], [11]])], context_ids=["troubleshooting_guides/crs_mismatch"])
    c = ev.citation_checks(rec)
    assert c["invalid_sql"] == ["t9"] and c["invalid_doc"] == ["historical_cases/INC_0001"] and c["n_marks"] == 4
    assert ev.unsupported_numbers(rec, "In 2026 Q1 how many?", TEXT) == ["99.9"]          # 38.4 and 11 supported; years / question excluded
    assert ev.unsupported_numbers(dict(rec, answer="9 incidents"), "q", TEXT) == ["9"]


def _full_human(spec, value=lambda q, i, k: 0 if k == "count" else 1):
    return {(q["id"], i): value(q["id"], i, k) for q in spec["questions"] for i, _, k in ev.items_for(q)}


def test_attribution_taxonomy():
    llm, path, doc, code = _run()
    run = json.loads(path.read_text())
    ms = _state(run)
    recs = {r["id"]: r for r in run["results"]}
    q = next(x for x in SPEC["questions"] if x["id"] == "H1")
    did = q["doc_facts"][0]["id"]
    human = _full_human(SPEC)
    att = lambda h, m=ms, r=recs: ev.attribute(q, r["H1"], m["H1"], h, PSPEC)[did]
    assert att(human) == "covered"
    h = dict(human); h[("H1", f"covered:{did}")] = 0
    assert att(h) == "synthesis_failure"                                           # evidence in the agent context, fact missing
    m2 = json.loads(json.dumps(ms)); m2["H1"]["evidence_agent"][did] = False
    assert att(h, m2) == "query_formulation_failure"                               # agent misses, oracle has it
    m3 = json.loads(json.dumps(m2)); m3["H1"]["evidence_oracle"][did] = False
    assert att(h, m3) == "retriever_context_failure"                               # both miss
    assert att(human, m2) == "covered_without_context_evidence"                    # covered although the agent context lacks it
    h4 = dict(h); h4[("H1", "sqlok:H1-s4")] = 0
    assert att(h4, m3) == "propagated_from_sql"                                    # the SQL dependency was rejected: not a retrieval failure
    failed = dict(recs, H1=dict(recs["H1"], status="plan_failed"))
    assert att(human, ms, failed) == "planner_failure"


def test_posthoc_attribution_separates_a_flawed_method_from_a_wrong_result():
    llm, path, doc, code = _run()
    run = json.loads(path.read_text())
    ms = _state(run)
    recs = {r["id"]: r for r in run["results"]}
    q = next(x for x in SPEC["questions"] if x["id"] == "H5")
    human = _full_human(SPEC)
    human[("H5", "sqlok:H5-s3")] = 0                         # a method flaw: the human rejects the task although its RESULT is right
    human[("H5", "covered:H5-d-INC_0185")] = 0               # and INC_0185's precedent was not covered
    m2 = json.loads(json.dumps(ms))
    m2["H5"]["evidence_agent"]["H5-d-INC_0185"] = False      # the agent context missed it, the oracle context has it
    frozen = ev.attribute(q, recs["H5"], m2["H5"], human, PSPEC)
    post = ev.attribute_result_based(q, recs["H5"], m2["H5"], human, PSPEC)
    assert frozen == {"H5-d-INC_0185": "propagated_from_sql", "H5-d-INC_0192": "propagated_from_sql"}   # the frozen rule's artefact
    assert post == {"H5-d-INC_0185": "query_formulation_failure", "H5-d-INC_0192": "covered"}
    m3 = json.loads(json.dumps(m2))
    m3["H5"]["sql"]["H5-s3"]["matched_task"] = None          # the SQL RESULT itself was wrong: both views propagate
    assert ev.attribute_result_based(q, recs["H5"], m3["H5"], human, PSPEC)["H5-d-INC_0185"] == "propagated_from_sql"
    s = ev.summarize(SPEC, PSPEC, run, ms, _full_human(SPEC))
    assert s["posthoc_result_based_attribution"] == s["failure_attribution"] == {"covered": 6} and "does not replace" in s["posthoc_note"]


def test_sheet_items_validation_and_summary():
    llm, path, doc, code = _run()
    run = json.loads(path.read_text())
    ms = _state(run)
    d = Path(tempfile.mkdtemp())
    c, md = ev.write_sheet(SPEC, run, ms, d, "t", TEXT)
    rows = list(csv.DictReader(open(c, encoding="utf-8-sig")))
    per = {}
    for r in rows:
        per.setdefault(r["question_id"], []).append(r["item_id"])
    assert {k: len(v) for k, v in per.items()} == {"H1": 12, "H2": 6, "H3": 10, "H4": 16, "H5": 11} and len(rows) == 55
    text = md.read_text(encoding="utf-8")
    assert "SQL task `t1`" in text and "Retrieval queries written by the model" in text and "Documents the model saw" in text
    assert "oracle" not in text.lower()                                             # the reading aid never shows the oracle context
    first = next(r for r in rows if r["item_id"].startswith("sqlok:"))
    assert "matched task" in first["machine_note"]
    try:
        ev.read_scores(c, SPEC)
        raise AssertionError("blank scores must be refused")
    except SystemExit as e:
        assert "blank" in str(e)

    def fill(fn):
        with open(c, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            for r in rows:
                r = dict(r)
                r["human_score"] = fn(r)
                w.writerow(r)
    fill(lambda r: "2" if r["item_id"] != "unsupported_claims" else "0")
    try:
        ev.read_scores(c, SPEC)
        raise AssertionError("a binary item cannot be 2")
    except SystemExit as e:
        assert "0 or 1" in str(e)
    fill(lambda r: "0" if r["item_id"] == "unsupported_claims" else "1")
    human = ev.read_scores(c, SPEC)
    try:
        ev.write_sheet(SPEC, run, ms, d, "t", TEXT)
        raise AssertionError("scored sheets must not be overwritten")
    except SystemExit as e:
        assert "refusing to overwrite" in str(e)
    s = ev.summarize(SPEC, PSPEC, run, ms, human)
    assert s["route"]["compliant"] == 5 and s["route"]["plan_failed"] == 0 and "not routing accuracy" in s["route"]["note"]
    assert s["sql_stage"]["facts"] == 20 and s["sql_stage"]["human_confirmed"] == 20 and s["sql_stage"]["machine_yes_human_no"] == 0
    assert s["sql_stage"]["machine_no_human_yes"] == 1                              # the boolean H1-s3 is derived, confirmed by the human only
    assert s["retrieval_stage"] == dict(doc_facts=6, evidence_in_agent_context=6, evidence_in_oracle_context=6)
    assert s["failure_attribution"] == {"covered": 6} and s["synthesis_stage"]["requirements"] == 4
    assert s["citations"]["invalid_doc_citations"] == 0 and s["unsupported_claims"]["total"] == 0 and "no composite" in s["note"]


def main():
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as e:  # noqa
            failed += 1
            print(f"FAIL {name}: {e!r}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
