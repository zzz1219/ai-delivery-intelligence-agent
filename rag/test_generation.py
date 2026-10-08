"""Plain-assert tests for the answer-generation benchmark. No API calls: every model is a fake.
Run from the delivery_agent directory:  python -m rag.test_generation"""
import csv
import json
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from . import build_generation_benchmark as bg
from . import evaluate_generation as ev
from . import judge as jd
from .build_benchmark import load_frozen
from .corpus import corpus_fingerprint, load_corpus
from .generate import build_context, lanes_for, run_generation
from .lanes import TwoLaneRetriever

NOW = datetime(2026, 10, 6, 22, 0, 0)


def _spec():
    return load_frozen(bg.BENCH_PATH, bg.HASH_PATH)


class _Err(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


class FakeLLM:
    """Answers every question from the documents it is given: cites each document id found in the context."""
    name = "fake-gen"

    def __init__(self, fail=None, raise_on=None, canned=None):
        self.fail, self.raise_on, self.canned, self.calls = fail or {}, raise_on or {}, canned or {}, 0

    def complete(self, task, system, user):
        self.calls += 1
        q = re.search(r"Question: (.*)", user, re.S).group(1).strip()
        for needle, factory in self.raise_on.items():
            if needle in q:
                raise factory()
        for needle, text in self.canned.items():
            if needle in q:
                return text
        ids = re.findall(r"### Document id: (\S+)", user)
        return " ".join(f"Statement from the document [{d}]." for d in ids)


def _run(llm=None, ablation=False, out_dir=None):
    spec, h = _spec()
    corpus = load_corpus()
    d = out_dir or tempfile.mkdtemp()
    return spec, run_generation(spec, h, corpus, corpus_fingerprint(corpus), llm or FakeLLM(), ablation=ablation,
                                out_dir=d, now=NOW)


def test_spec_is_frozen_complete_and_reproducible():
    spec, h = _spec()
    assert spec["benchmark_version"] == "rag-generation-1.0" and len(spec["questions"]) == 15
    groups = [q["group"] for q in spec["questions"]]
    assert groups.count("knowledge") == groups.count("symptom_answerable") == groups.count("no_matching_case") == 5
    assert bg.build() == bg.BENCH_PATH.read_bytes()                      # a fresh build is byte-identical
    assert spec["context_sufficiency_audit"]["selected"] == {"cases": 1, "guides": 2}
    assert spec["context_sufficiency_audit"]["declared_ablation"]["cases"] == 3
    kinds = {f["kind"] for q in spec["questions"] for f in q["facts"]}
    assert kinds == {"guide_fact", "case_fact", "aggregate_fact"}
    gen = spec["pre_registered"]["generator"]
    assert gen["model"] == "gemini-3.5-flash-lite" and "[doc_id]" in gen["citation_format"]
    assert spec["pre_registered"]["scoring"]["no_composite_score"] is True


def test_audit_rule_picks_the_smallest_sufficient_configuration():
    spec, _ = _spec()
    grid = spec["context_sufficiency_audit"]["grid"]
    ok = [r for r in grid if r["sufficient"]]
    best = min(ok, key=lambda r: (r["total"], r["mean_context_words"], r["cases"]))
    assert (best["cases"], best["guides"]) == (1, 2) and not any(r["sufficient"] for r in grid if r["total"] < 3)
    assert all(r["knowledge_facts"] == "29/29" for r in grid)             # the evidence mapping no longer drifts


def test_every_guide_fact_is_contained_in_its_evidence_guide():
    corpus = load_corpus()
    qs, _ = bg.build_questions()                                          # build_questions verifies and would raise
    text = {d.doc_id: d.text for d in corpus}
    for q in qs:
        for f in q["facts"]:
            if f["kind"] == "guide_fact":
                assert f["evidence_docs"][0] in text
    broken = [dict(q, facts=[dict(f, statement="Guide: restart the datacenter and call the vendor") for f in q["facts"]
                             if f["kind"] == "guide_fact"][:1]) for q in qs if q["group"] == "knowledge"][:1]
    try:
        bg._verify_guide_facts(broken, corpus)
        raise AssertionError("an unsupported guide fact must be rejected")
    except AssertionError as e:
        assert "not supported" in str(e)


def test_two_lane_retrieval_orders_cases_before_guides():
    spec, _ = _spec()
    r = TwoLaneRetriever(load_corpus(), 1, 2)
    for q in spec["questions"]:
        ids = r.retrieve(q["retrieval_query"])
        assert len(ids) == 3 and ids[0].startswith("historical_cases/")
        assert all(not d.startswith("historical_cases/") for d in ids[1:])
    r3 = TwoLaneRetriever(load_corpus(), 3, 2)
    assert len(r3.retrieve("timeout gateway")) == 5


def test_runner_writes_a_complete_file_and_records_context():
    spec, (path, doc, code) = _run()
    assert code == 0 and path.name == "gen_fake-gen_c1g2_20261006_220000.json"
    assert [p.name for p in path.parent.iterdir()] == [path.name]          # progress file renamed away
    saved = json.loads(path.read_text())
    assert saved["meta"]["complete"] and saved["meta"]["role"] == "PRIMARY" and saved["meta"]["deviation_from_registered_model"] is False
    a = saved["answers"][0]
    assert len(a["context_ids"]) == 3 and a["retrieved"][0]["lane"] == "cases" and "[" in a["answer"]
    spec2, (p2, doc2, c2) = _run(ablation=True)
    assert json.loads(p2.read_text())["meta"]["role"] == "declared ablation" and len(doc2["answers"][0]["context_ids"]) == 5
    assert lanes_for(spec, True) == (3, 2)


def test_runner_breaker_and_runtime_abort():
    spec, (path, doc, code) = _run(FakeLLM(raise_on={"Have we seen service timeouts": lambda: _Err(503, "UNAVAILABLE"),
                                                      "connection pool exhaustion": lambda: _Err(429, "RESOURCE_EXHAUSTED")}))
    st = [a["status"] for a in doc["answers"]]
    assert st[:4] == ["ok", "ok", "error", "error"] and set(st[4:]) == {"skipped"} and code == 2
    assert path.name.endswith("_INCOMPLETE.json")
    spec, (path, doc, code) = _run(FakeLLM(raise_on={"coordinate-system": lambda: TypeError("sdk changed")}))
    assert code == 3 and doc["answers"][1]["error_class"] == "runtime" and doc["answers"][2]["status"] == "skipped"
    try:
        ev.load_run(path)
        raise AssertionError("an incomplete run must never be scored")
    except SystemExit as e:
        assert "incomplete" in str(e)


def test_machine_checks_catch_the_failure_modes():
    spec, _ = _spec()
    sym = [q for q in spec["questions"] if q["group"] == "symptom_answerable"][0]
    nom = [q for q in spec["questions"] if q["group"] == "no_matching_case"][0]
    spec_, (path, doc, code) = _run(FakeLLM(canned={
        nom["question"][:60]: "No documented matching case was found. Related case INC_0001 is unrelated.",
        sym["question"][:60]: "A similar case exists [historical_cases/INC_9999] and also [troubleshooting_guides/crs_mismatch]."}))
    run = json.loads(path.read_text())
    ch = ev.machine_checks(spec, run)
    assert ch[nom["id"]]["mentions_case_id"] and ch[nom["id"]]["precedent_flag"] and not ch[nom["id"]]["cited_any_case"]
    assert "historical_cases/INC_9999" in ch[sym["id"]]["invalid_citations"]             # not in the retrieved context
    assert ch[sym["id"]]["wrong_precedents"] == ["historical_cases/INC_9999"] and not ch[sym["id"]]["cited_relevant_case"]
    honest = ev.machine_checks(spec, json.loads(_run()[1][0].read_text()))               # FakeLLM cites exactly what it got
    assert all(not c["invalid_citations"] and c["has_citation"] for c in honest.values())
    knowledge = [q for q in spec["questions"] if q["group"] == "knowledge"][0]
    assert all(honest[knowledge["id"]]["evidence_in_context"].values())


def _filled(spec, path, human):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            r["human_score"] = human(r["question_id"], r["item_id"])
            w.writerow(r)


def test_rubric_sheet_items_and_input_validation():
    spec, _ = _spec()
    spec_, (path, doc, code) = _run()
    run = json.loads(path.read_text())
    ch = ev.machine_checks(spec, run)
    d = Path(tempfile.mkdtemp())
    c, md = ev.write_sheet(spec, run, ch, d, "t")
    rows = list(csv.DictReader(open(c, encoding="utf-8-sig")))
    per = {}
    for r in rows:
        per.setdefault(r["question_id"], []).append(r["item_id"])
    assert sum(len(v) for k, v in per.items() if k <= "G05") == 29 + 5               # 29 testable facts + 5 counts
    assert all(len(v) == 3 for k, v in per.items() if "G06" <= k <= "G10")             # 2 behaviours + count
    assert all(len(v) == 4 for k, v in per.items() if k >= "G11")                      # 3 behaviours + count
    md_text = md.read_text(encoding="utf-8")
    assert "aggregate fact" in md_text and "Documents the assistant saw" in md_text
    first = run["answers"][0]["context_ids"][0]
    body = {d.doc_id: d.text for d in load_corpus()}[first].strip()
    assert first in md_text and body in md_text                                                  # full context text is inline
    try:
        ev.read_scores(c, spec)
        raise AssertionError("blank scores must be refused")
    except SystemExit as e:
        assert "blank" in str(e)
    _filled(spec, c, lambda q, i: "2" if i != "unsupported_claims" else "0")
    try:
        ev.read_scores(c, spec)
        raise AssertionError("a binary item cannot be 2")
    except SystemExit as e:
        assert "0 or 1" in str(e)
    _filled(spec, c, lambda q, i: "1" if i != "unsupported_claims" else "0")
    assert len(ev.read_scores(c, spec)) == len(rows)
    try:
        ev.write_sheet(spec, run, ch, d, "t")                              # regenerating must not destroy scored work
        raise AssertionError("an existing scored sheet must not be overwritten")
    except SystemExit as e:
        assert "refusing to overwrite" in str(e)
    ev.write_sheet(spec, run, ch, d, "t", force=True)                      # explicit override gives a blank sheet again
    assert all(not r["human_score"] for r in csv.DictReader(open(c, encoding="utf-8-sig")))


def test_summary_evidence_split_and_no_composite():
    spec, _ = _spec()
    spec_, (path, doc, code) = _run()
    run = json.loads(path.read_text())
    ch = ev.machine_checks(spec, run)
    d = Path(tempfile.mkdtemp())
    c, _ = ev.write_sheet(spec, run, ch, d, "t")
    _filled(spec, c, lambda q, i: "0" if i in ("R1-f1", "precedent_claimed", "root_cause_asserted", "unsupported_claims") else "1")
    human = ev.read_scores(c, spec)
    s = ev.summarize(spec, run, ch, human)
    k = s["knowledge"]
    assert k["testable_facts"] == 29 and k["evidence_split"]["missed_context_available"] == 1      # R1-f1: the evidence was in the context
    assert k["evidence_split"]["covered"] == 28 and k["evidence_split"]["aggregate_not_scored"] == 5    # R3-f4 + 4 count companions
    assert k["coverage"] == "28/29" and k["per_question"]["G01"] == "4/5"
    # forge "evidence not retrieved" and "covered without context evidence" to check the other two categories
    ch2 = json.loads(json.dumps(ch))
    ch2["G02"]["evidence_in_context"]["R2-f1"] = False                                              # answer covers it anyway
    ch2["G01"]["evidence_in_context"]["R1-f1"] = False                                              # and is missing it too
    s2 = ev.summarize(spec, run, ch2, human)["knowledge"]["evidence_split"]
    assert s2["covered_without_context_evidence"] == 1 and s2["missed_evidence_not_retrieved"] == 1
    nm = s["no_matching_case"]
    assert nm["n"] == 5 and nm["precedent_claimed_human"] == 0 and nm["abstention_correct"] == 5
    assert "composite" in s["note"] and s["all_answers"]["n"] == 15 and s["all_answers"]["unsupported_claims_total"] == 0


def test_kappa_and_agreement():
    assert ev.kappa([1, 1, 1, 1], [1, 0, 1, 1]) is None                  # one rater uses a single label: undefined
    assert ev.kappa([1, 0, 1, 0], [1, 0, 1, 0]) == 1.0
    assert ev.kappa([1, 1, 0, 0], [1, 0, 1, 0]) == 0.0
    spec, _ = _spec()
    human = {(q["id"], i): (0 if k == "count" else 1) for q in spec["questions"] for i, _, k in ev.checklist(q)}
    for q in spec["questions"]:                                          # give the human some variation per kind
        if q["id"] in ("G02", "G03"):
            for i, _, k in ev.checklist(q):
                if k == "fact":
                    human[(q["id"], i)] = 0
        if q["id"] == "G06":
            human[("G06", "advice_consistent")] = 0
        if q["id"] == "G12":
            human[("G12", "precedent_claimed")] = 1
        if q["id"] == "G07":
            human[("G07", "unsupported_claims")] = 1
    rep = ev.agreement(spec, human, dict(human))
    assert rep["overall"]["raw_agreement"] == 1.0 and rep["fact"]["kappa"] == 1.0 and rep["behaviour"]["kappa"] == 1.0
    assert "not reported" in rep["overall"]["kappa"]
    judge = dict(human)
    judge[("G01", "R1-f1")] = 0                                           # judge disagrees on one fact (human: covered)
    judge[("G11", "unsupported_claims")] = 2                              # and invents an unsupported claim
    judge[("G12", "precedent_claimed")] = 0                               # and misses a precedent claim
    rep = ev.agreement(spec, human, judge)
    assert rep["fact"]["raw_agreement"] < 1.0 and rep["unsupported_any"]["raw_agreement"] < 1.0
    assert rep["behaviour"]["raw_agreement"] < 1.0 and 0 < rep["overall"]["raw_agreement"] < 1.0
    assert "source of truth" in rep["note"]
    allones = {k: (0 if k[1] == "unsupported_claims" else 1) for k in human}
    assert ev.agreement(spec, allones, dict(allones))["fact"]["kappa"] is None      # one label only: undefined


class FakeJudge:
    name = "fake-judge"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), 0

    def complete(self, task, system, user):
        self.calls += 1
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if callable(r):
            r = r(user)
        return r


def _perfect(user):
    ids = re.findall(r"^(\S+): ", user.split("Checklist (id: meaning):")[1].split("Return JSON")[0], re.M)
    return "```json\n" + json.dumps({"items": {i: 1 for i in ids}, "unsupported_claims": 0}) + "\n```"


def test_judge_parsing_retry_and_failure_handling():
    spec, _ = _spec()
    items = ev.checklist(spec["questions"][0])
    ok, err = jd.parse_judgement(_perfect("Checklist (id: meaning):\n" + "\n".join(f"{i}: x" for i, _, k in items if k != "count") + "\nReturn JSON"), items)
    assert err is None and ok["unsupported_claims"] == 0 and ok["R1-f1"] == 1
    assert jd.parse_judgement("no json here", items)[1]
    assert "must be 0 or 1" in jd.parse_judgement(json.dumps({"items": {i: 2 for i, _, k in items if k != "count"}, "unsupported_claims": 0}), items)[1]
    assert jd.parse_judgement(json.dumps({"items": {}, "unsupported_claims": 0}), items)[0] is None
    spec_, (path, doc, code) = _run()
    run = json.loads(path.read_text())
    corpus = load_corpus()
    rows, log, stopped = jd.judge_run(spec, run, corpus, FakeJudge([_perfect]))
    assert all(r["judge_score"] != "" for r in rows) and not any(l["error"] for l in log) and stopped is None
    flaky = FakeJudge(["garbage", _perfect])                              # first reply invalid, the retry works
    rows, log, _ = jd.judge_run(spec, run, corpus, flaky)
    assert flaky.calls == 16 and not any(l["error"] for l in log)           # 15 answers + exactly one retry
    rows, log, _ = jd.judge_run(spec, run, corpus, FakeJudge(["garbage"]))
    assert all(r["judge_score"] == "" for r in rows) and all(l["error"] for l in log)


def test_judge_checkpoints_and_resumes_after_a_quota_stop():
    spec, _ = _spec()
    spec_, (path, doc, code) = _run()
    run = json.loads(path.read_text())
    corpus = load_corpus()

    class QuotaAfter(FakeJudge):
        def __init__(self, n):
            super().__init__([_perfect])
            self.n = n

        def complete(self, task, system, user):
            if self.calls >= self.n:
                raise _Err(429, "RESOURCE_EXHAUSTED")
            return super().complete(task, system, user)
    saved = {}
    rows, log, stopped = jd.judge_run(spec, run, corpus, QuotaAfter(7),
                                      on_question=lambda qid, r, e: saved.update({qid: dict(rows=r, log=e)}))
    assert stopped and "RESOURCE_EXHAUSTED" in stopped and len(saved) == 7 and len(log) == 7
    resumer = FakeJudge([_perfect])
    rows2, log2, stopped2 = jd.judge_run(spec, run, corpus, resumer, done=saved)
    assert stopped2 is None and resumer.calls == 8 and len(log2) == 15            # only the 8 missing answers are re-judged
    assert all(r["judge_score"] != "" for r in rows2)
    try:
        jd.judge_run(spec, run, corpus, FakeJudge([lambda u: (_ for _ in ()).throw(TypeError("bug"))]))
        raise AssertionError("a non-provider error must not be swallowed")
    except TypeError:
        pass


def test_case_facts_are_claims_a_single_retrieved_case_can_support():
    import re
    spec, _ = _spec()
    corpus = load_corpus()
    text = {d.doc_id: d.text for d in corpus}
    facts = [f for q in spec["questions"] for f in q["facts"]]
    testable = [f for f in facts if f["kind"] != "aggregate_fact"]
    assert len(testable) == 29 and {f["kind"] for f in testable} == {"guide_fact", "case_fact"}
    for q in spec["questions"]:
        for f in q["facts"]:
            if f["kind"] == "case_fact":
                assert q["relevant_cases"] and all(bg._supports(f, text[d]) for d in q["relevant_cases"]), f["id"]
    for f in testable:
        assert not re.search(r"\(\d+\)", f["statement"]) and "slow-query guide" not in f["statement"], f["statement"]
        assert not re.search(r"\b\d+ documented\b", f["statement"])
    agg = {f["id"] for f in facts if f["kind"] == "aggregate_fact"}
    assert agg == {"R1-f5-agg", "R2-f6-agg", "R3-f1-agg", "R3-f3-agg", "R3-f4"}
    assert any("9 documented" in f["statement"] for f in facts if f["id"] == "R3-f1-agg")
    r1f4 = next(f for f in facts if f["id"] == "R1-f4")
    assert r1f4["evidence_docs"] == ["troubleshooting_guides/bulk_import_performance"] and "source_statement" in r1f4
    # a retrieved case that lacks the evidence marker does NOT count as evidence
    q3 = next(q for q in spec["questions"] if q["id"] == "G03")
    case = q3["relevant_cases"][0]
    stripped = dict(text, **{case: text[case].replace("exceeding the configured timeout", "x")})
    f2 = next(f for f in q3["facts"] if f["id"] == "R3-f2")
    assert bg._available(q3, [case], text)["facts"]["R3-f2"] and not bg._available(q3, [case], stripped)["facts"]["R3-f2"]
    assert f2["evidence"]["all_of"]


def test_end_to_end_judge_vs_human_agreement():
    spec, _ = _spec()
    spec_, (path, doc, code) = _run()
    run = json.loads(path.read_text())
    ch = ev.machine_checks(spec, run)
    d = Path(tempfile.mkdtemp())
    c, _ = ev.write_sheet(spec, run, ch, d, "e2e")
    _filled(spec, c, lambda q, i: "0" if i == "unsupported_claims" else "1")
    rows, _, _ = jd.judge_run(spec, run, load_corpus(), FakeJudge([_perfect]))
    jc = d / "judge.csv"
    with open(jc, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["question_id", "item_id", "item_text", "judge_score"])
        w.writeheader()
        w.writerows(rows)
    rep = ev.agreement(spec, ev.read_scores(c, spec), ev.read_scores(jc, spec, "judge_score"))
    assert rep["overall"]["raw_agreement"] == 1.0


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
