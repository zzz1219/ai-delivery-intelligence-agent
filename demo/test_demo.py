"""Plain-assert tests for the demo layer (trace schema, engine, converter, recorder, gallery). Fake models only: no network, no API key.
Run from the delivery_agent folder:  python -m demo.test_demo"""
import copy
import json
import random
import re
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from . import convert_sealed as cs
from . import gallery, record, trace
from .engine import Engine

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 7, 22, 0, 0)
ENGINE = Engine()
Q_ROOT = "Which root causes account for the most closed incidents?"
Q_MAP = "Which deployment version was most associated with map-service incidents in 2026 Q1, and what failure mode is documented?"
Q_TREND = "How have incident volumes changed month by month across service categories?"
SQL = {
    Q_ROOT: ("Count closed incidents per root cause, most frequent first",
             "SELECT root_cause, COUNT(*) AS closed_incidents FROM incidents WHERE status='closed' GROUP BY root_cause ORDER BY closed_incidents DESC", False),
    Q_MAP: ("Count map-service incidents per deployment version in 2026 Q1",
            "SELECT d.version, COUNT(*) AS incident_count FROM incidents i JOIN deployments d ON i.deployment_id=d.deployment_id "
            "WHERE i.category='map_service' AND i.reported_at >= '2026-01-01' AND i.reported_at < '2026-04-01' GROUP BY d.version ORDER BY incident_count DESC", True),
    Q_TREND: ("Count incidents per month and service category",
              "SELECT strftime('%Y-%m', reported_at) AS month, category, COUNT(*) AS incident_count FROM incidents GROUP BY 1, 2 ORDER BY 1, 2", False)}


class _Err(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


class FakeFreeLLM:
    name = "fake-free"

    def __init__(self, raise_on=None, garbage_plan=False):
        self.prompts, self.raise_on, self.garbage_plan = [], raise_on or {}, garbage_plan

    def complete(self, task, system, user):
        self.prompts.append((task, system, user))
        assert task != "answer"
        if task == "sql":
            desc = re.search(r"Question: (.*)", user).group(1).strip()
            return "```sql\n" + next(v[1] for v in SQL.values() if v[0] == desc) + "\n```"
        q = next(k for k in SQL if k in user)
        if q in self.raise_on and self.raise_on[q][0] == task:
            raise self.raise_on[q][1]()
        desc, _, retrieval = SQL[q]
        if task == "plan":
            if self.garbage_plan:
                return "I would rather just answer."
            return json.dumps(dict(requires_sql=True, requires_retrieval=retrieval, retrieval_depends_on_sql=retrieval,
                                   sql_tasks=[dict(task_id="t1", description=desc)], retrieval_purpose="look up documented failure modes"))
        if task == "queries":
            return json.dumps({"queries": ["map tiles loaded slowly tile cache scale levels stale after upgrade"]})
        assert task == "synthesis"
        rows = user.split("SQL: ", 1)[1].splitlines()
        first = rows[2].split(" | ") if len(rows) > 2 else []
        docs = re.findall(r"### Document id: (\S+)", user)
        if q == Q_ROOT:
            return f"The most frequent root cause among closed incidents is {first[0]} with {first[1]} incidents [sql:t1]."
        if q == Q_MAP:
            return (f"Version {first[0]} was associated with {first[1]} map-service incidents in 2026 Q1 [sql:t1]. "
                    f"A documented case describes a tile cache misconfiguration [{docs[0]}].")
        return "Monthly volumes by service category are shown in the chart [sql:t1]."


def _answer(q, llm=None, **kw):
    llm = llm or FakeFreeLLM()
    return llm, ENGINE.answer(q, llm, trace_id="X1", now=NOW, **kw)


def test_engine_runs_the_frozen_pipeline_on_a_sql_only_question():
    llm, t = _answer(Q_ROOT)
    assert trace.validate_trace(t, ENGINE.docs) == [] and t["kind"] == "recorded_live" and t["status"] == "ok" and "evaluation" not in t
    assert t["recorded_at"] == "2026-10-07T22:00:00" and t["model"] == "fake-free" and t["specs"] == ENGINE.specs()
    assert t["retrieval"]["status"] == "not_requested" and t["retrieval"]["documents"] == [] and t["trust"]["documents_in_context"] == 0
    assert [v["kind"] for v in t["viz"]] == ["bar"] and t["viz"][0]["orientation"] == "h" and t["viz"][0]["sort"] == "y_desc"
    assert t["trust"]["sql_tasks_ok"] == 1 and t["trust"]["invalid_sql_citations"] == [] and t["trust"]["numbers_not_in_any_result"] == []
    assert "deterministic rules" in t["trust"]["visualization"] and len(t["findings"]) == 1 and t["findings"][0]["sql_tasks"] == ["t1"]
    P = ENGINE.pspec["prompts"]                                                      # the evaluated prompts, byte for byte
    by = {task: (s, u) for task, s, u in llm.prompts}
    assert by["plan"][0] == P["planner_system"] and by["plan"][1] == P["planner_user"].format(question=Q_ROOT)
    assert by["synthesis"][0] == P["synthesis_system"] and "(none retrieved)" in by["synthesis"][1]
    assert not any(task == "answer" for task, _, _ in llm.prompts)


def test_hybrid_question_gets_document_cards_lanes_and_no_oracle_fields():
    llm, t = _answer(Q_MAP)
    assert trace.validate_trace(t, ENGINE.docs) == [] and t["retrieval"]["status"] == "ok" and t["retrieval"]["queries"]
    cards = t["retrieval"]["documents"]
    assert [c["doc_id"] for c in cards] == t["retrieval"]["context_ids"] and {c["lane"] for c in cards} <= {"cases", "guides"}
    case = next(c for c in cards if c["lane"] == "cases")
    assert case["fields"]["root_cause"] and "symptoms" in case["sections"] and "overview" not in case["sections"]
    assert any(c["lane"] == "guides" and c["sections"] for c in cards)
    assert not any("oracle" in k.lower() for k in trace._keys(t)) and t["trust"]["invalid_doc_citations"] == []
    assert [f["docs"] for f in t["findings"]] == [[], [cards[0]["doc_id"]]] and [f["sql_tasks"] for f in t["findings"]] == [["t1"], []]


def test_trend_question_becomes_a_multi_series_line():
    _, t = _answer(Q_TREND)
    assert trace.validate_trace(t, ENGINE.docs) == [] and t["viz"][0]["kind"] == "line" and t["viz"][0]["series"] == "category" and t["findings"][0]["sql_tasks"] == ["t1"]


def test_question_checks_run_before_any_model_call():
    assert trace.check_question("  What   is\tthe\x00 status? ") == "What is the status?"
    for bad, needle in (("", "empty"), ("   ", "empty"), ("x" * 301, "longer"), ("my key AIza" + "A" * 30, "API key"), ("GEMINI_API_KEY=abc", "API key")):
        llm = FakeFreeLLM()
        try:
            ENGINE.answer(bad, llm, trace_id="X")
            raise AssertionError("must refuse: " + bad[:20])
        except ValueError as e:
            assert needle in str(e) and llm.prompts == []


def test_a_bad_plan_is_a_trace_and_a_provider_error_is_an_error():
    llm, t = _answer(Q_ROOT, FakeFreeLLM(garbage_plan=True))
    assert t["status"] == "plan_failed" and t["plan"] is None and t["tasks"] == [] and t["viz"] == [] and t["answer"] is None and t["findings"] == []
    assert trace.validate_trace(t, ENGINE.docs) == [] and t["trust"]["plan_valid"] is False
    try:
        _answer(Q_ROOT, FakeFreeLLM(raise_on={Q_ROOT: ("synthesis", lambda: _Err(503, "UNAVAILABLE"))}))
        raise AssertionError("a provider error must propagate to the caller")
    except _Err:
        pass


def test_findings_are_verbatim_cited_sentences_only():
    ans = ("Intro without a citation. Version v3.2 had 32 incidents [sql:t1]. \n- For INC_1 a precedent exists [historical_cases/INC_0049] and [sql:t2].\n"
           "1. Check pool metrics first [troubleshooting_guides/connection_pool_exhausted].\nNo cite here.")
    f = trace.extract_findings(ans)
    assert [x["text"] for x in f] == ["Version v3.2 had 32 incidents [sql:t1].", "For INC_1 a precedent exists [historical_cases/INC_0049] and [sql:t2].",
                                      "Check pool metrics first [troubleshooting_guides/connection_pool_exhausted]."]
    assert f[1]["sql_tasks"] == ["t2"] and f[1]["docs"] == ["historical_cases/INC_0049"] and trace.extract_findings("") == [] and trace.extract_findings(None) == []
    rng = random.Random(7)
    parts = ["Alpha beta.", "Gamma [sql:t1].", "- Delta [historical_cases/INC_0001].", "Epsilon (x) [sql:t2]!", "3) Zeta [deployment_guides/distributed_deployment]."]
    for _ in range(200):
        text = rng.choice([" ", "\n", "  "]).join(rng.sample(parts, rng.randint(0, 5)))
        assert all(x["text"] in text and trace._CITE.search(x["text"]) for x in trace.extract_findings(text))


def test_converter_builds_valid_sealed_traces_that_match_the_final_scores():
    d = Path(tempfile.mkdtemp())
    assert cs.convert(d) == ["H1", "H2", "H3", "H4", "H5"]
    ts = {i: json.loads((d / f"{i}.json").read_text(encoding="utf-8")) for i in ["H1", "H2", "H3", "H4", "H5"]}
    for i, t in ts.items():
        assert trace.validate_trace(t, ENGINE.docs) == [] and t["kind"] == "sealed_evaluation" and t["evaluation"]["question_id"] == i
    frozen, post = {}, {}
    for t in ts.values():
        for f in t["evaluation"]["doc_facts"]:
            frozen[f["frozen_attribution"]] = frozen.get(f["frozen_attribution"], 0) + 1
            post[f["posthoc_attribution"]] = post.get(f["posthoc_attribution"], 0) + 1
    assert frozen == {"covered": 3, "propagated_from_sql": 3} and post == {"covered": 4, "propagated_from_sql": 1, "query_formulation_failure": 1}
    ev_ = [t["evaluation"] for t in ts.values()]
    assert sum(f["human_sql_ok"] for e in ev_ for f in e["sql_facts"]) == 16 and sum(f["answer_states_it"] for e in ev_ for f in e["sql_facts"]) == 17
    assert sum(y["met"] for e in ev_ for y in e["requirements"]) == 4 and sum(e["unsupported_claims"] for e in ev_) == 0 and all(e["route_compliant"] for e in ev_)
    assert [v["kind"] for v in ts["H4"]["viz"]] == ["bar", "bar"] and ts["H4"]["viz"][0]["sort"] == "x_asc" and "pairs" in ts["H2"]["evaluation"]["case_note"]
    h5 = ts["H5"]["evaluation"]
    assert "historical_cases/INC_0026" in h5["oracle_context_ids"] and "historical_cases/INC_0026" not in ts["H5"]["retrieval"]["context_ids"]
    assert [(f["id"], f["evidence_in_agent_context"], f["evidence_in_oracle_context"]) for f in h5["doc_facts"]] == [("H5-d-INC_0185", False, True), ("H5-d-INC_0192", True, True)]
    assert not any("oracle" in k.lower() for t in ts.values() for k in trace._keys({a: b for a, b in t.items() if a != "evaluation"}))
    for i in ts:                                                                    # deterministic, and the committed traces are the fresh conversion
        assert (d / f"{i}.json").read_bytes() == (ROOT / "traces" / f"{i}.json").read_bytes()


def test_json_files_are_written_with_lf_newlines_on_every_platform():
    """Regression: text-mode writes produce CRLF on Windows and broke the byte-for-byte comparison with the committed traces."""
    import pathlib
    orig = pathlib.Path.write_text

    def windows_write_text(self, data, encoding=None, errors=None, newline=None):    # what Python does on Windows when newline is not given
        data = data.replace("\n", "\r\n") if newline is None else data
        with open(self, "w", encoding=encoding, errors=errors, newline="") as f:
            return f.write(data)
    pathlib.Path.write_text = windows_write_text
    try:
        d = Path(tempfile.mkdtemp())
        cs.convert(d)
        rd, qs = _records()
        record.record_questions(ENGINE, FakeFreeLLM(), qs[:1], out_dir=rd, now=NOW)
    finally:
        pathlib.Path.write_text = orig
    for p in list(d.glob("*.json")) + list(rd.glob("*.json")):
        assert b"\r" not in p.read_bytes(), p.name
    for i in ("H1", "H2", "H3", "H4", "H5"):
        assert (d / f"{i}.json").read_bytes() == (ROOT / "traces" / f"{i}.json").read_bytes()
    assert trace.dump_json({"a": "é"}) == b'{\n "a": "\xc3\xa9"\n}\n'


def test_validator_catches_tampering():
    base = json.loads((ROOT / "traces" / "H5.json").read_text(encoding="utf-8"))
    assert trace.validate_trace(base, ENGINE.docs) == []

    def bad(label, mutate, needle):
        t = copy.deepcopy(base)
        mutate(t)
        out = " | ".join(trace.validate_trace(t, ENGINE.docs))
        assert needle in out, (label, out[:200])
    bad("a required field removed", lambda t: t.pop("trust"), "missing field")
    bad("an oracle field outside the evaluation", lambda t: t.__setitem__("oracle_context_ids", ["historical_cases/INC_0026"]), "oracle fields")
    bad("an oracle field nested in retrieval", lambda t: t["retrieval"].__setitem__("oracle_context", []), "oracle fields")
    bad("a secret in the trace", lambda t: t.__setitem__("title", "key AIza" + "B" * 30), "secret")
    bad("an evaluation on a live trace", lambda t: t.__setitem__("kind", "recorded_live"), "only sealed-evaluation")
    bad("a viz spec changed", lambda t: t["viz"][0].__setitem__("kind", "table"), "differs from the deterministic selector")
    bad("a row changed so the shape changes", lambda t: t["tasks"][0].__setitem__("rows", t["tasks"][0]["rows"] * 2), "differs from the deterministic selector")
    bad("a finding that is not in the answer", lambda t: t["findings"][0].__setitem__("text", "Invented sentence [sql:t1]."), "verbatim")
    bad("a finding dropped", lambda t: t["findings"].pop(), "differ from the ones derived")
    bad("the answer edited", lambda t: t.__setitem__("answer", t["answer"] + " Extra claim [sql:t9]."), "key findings differ")
    bad("trust edited", lambda t: t["trust"].__setitem__("invalid_doc_citations", ["historical_cases/INC_0001"]), "trust checks differ")
    bad("a document card removed", lambda t: t["retrieval"]["documents"].pop(), "do not match the context ids")
    bad("an unknown document", lambda t: (t["retrieval"]["context_ids"].__setitem__(0, "historical_cases/INC_9999"),
                                          t["retrieval"]["documents"][0].__setitem__("doc_id", "historical_cases/INC_9999")), "does not exist")
    bad("a duplicate task id", lambda t: t["tasks"].append(copy.deepcopy(t["tasks"][0])), "duplicate task ids")
    bad("an answered trace without an answer", lambda t: (t.__setitem__("answer", None), t.__setitem__("findings", [])), "no answer")


def _records(**kw):
    d = Path(tempfile.mkdtemp())
    qs = [dict(id="A2", role="analytics", title="Root causes", question=Q_ROOT, expect_viz="bar"),
          dict(id="A1", role="analytics", title="Trend", question=Q_TREND, expect_viz="line")]
    return d, qs


def test_recorder_writes_valid_traces_never_overwrites_and_moves_old_ones_on_force():
    d, qs = _records()
    res = record.record_questions(ENGINE, FakeFreeLLM(), qs, out_dir=d, now=NOW)
    assert [(i, o) for i, o, _ in res] == [("A2", "written"), ("A1", "written")] and "expected bar" in res[0][2]
    for i in ("A1", "A2"):
        assert trace.validate_trace(json.loads((d / f"{i}.json").read_text(encoding="utf-8")), ENGINE.docs) == []
    before = (d / "A2.json").read_bytes()
    assert [o for _, o, _ in record.record_questions(ENGINE, FakeFreeLLM(), qs, out_dir=d, now=NOW)] == ["exists", "exists"] and (d / "A2.json").read_bytes() == before
    res = record.record_questions(ENGINE, FakeFreeLLM(), qs[:1], out_dir=d, force=True, now=datetime(2026, 10, 8, 9, 0, 0))
    assert res[0][1] == "written" and [p.name for p in (d / "_superseded").iterdir()] == ["A2_20261008_090000.json"]
    idx = json.loads((d / "index.json").read_text(encoding="utf-8"))
    assert [e["trace_id"] for e in idx["traces"]] == ["A1", "A2"] and [p["trace_id"] for p in idx["pending_recordings"]] == ["A3", "A4"]


def test_recorder_error_handling():
    d, qs = _records()
    more = qs + [dict(id="A9", role="analytics", title="Map", question=Q_MAP)]
    llm = FakeFreeLLM(raise_on={Q_ROOT: ("plan", lambda: _Err(503, "UNAVAILABLE")), Q_TREND: ("plan", lambda: _Err(429, "RESOURCE_EXHAUSTED"))})
    res = record.record_questions(ENGINE, llm, more, out_dir=d, now=NOW)
    assert [o for _, o, _ in res] == ["provider_error", "provider_error", "skipped"] and not list(d.glob("A*.json"))
    try:
        record.record_questions(ENGINE, FakeFreeLLM(raise_on={Q_ROOT: ("plan", lambda: TypeError("sdk changed"))}), qs, out_dir=d, now=NOW)
        raise AssertionError("a non-provider error must propagate")
    except TypeError:
        pass
    saved = record.validate_trace
    record.validate_trace = lambda *a, **k: ["forced problem"]
    try:
        res = record.record_questions(ENGINE, FakeFreeLLM(), qs[:1], out_dir=d, now=NOW)
    finally:
        record.validate_trace = saved
    assert res[0][1] == "invalid" and not (d / "A2.json").exists()                  # an invalid trace is reported, never written
    res = record.record_questions(ENGINE, FakeFreeLLM(garbage_plan=True), qs[:1], out_dir=d, now=NOW)
    assert res[0][1] == "plan_failed_written" and (d / "A2.json").exists()


def test_gallery_lists_traces_in_role_order_and_the_pending_recordings():
    idx = json.loads((ROOT / "traces" / "index.json").read_text(encoding="utf-8"))
    assert [e["trace_id"] for e in idx["traces"]] == ["A1", "A2", "A3", "A4", "H1", "H3", "H4", "H2", "H5"]       # analytics, successes, partial, failure, mixed
    assert idx["pending_recordings"] == [] and idx["format"] == "trace-gallery-1.0"                     # A1-A4 were recorded on 2026-10-07
    again = Path(tempfile.mkdtemp())
    for f in (ROOT / "traces").glob("*.json"):
        (again / f.name).write_bytes(f.read_bytes())
    gallery.rebuild_index(again)
    assert (again / "index.json").read_bytes() == (ROOT / "traces" / "index.json").read_bytes()         # the committed index is exactly what the traces produce
    qs = json.loads((ROOT / "demo" / "questions.json").read_text(encoding="utf-8"))["questions"]
    assert [q["id"] for q in qs] == ["A1", "A2", "A3", "A4"] and all(len(q["question"]) <= trace.MAX_QUESTION for q in qs)
    d = Path(tempfile.mkdtemp())
    (d / "A2.json").write_text(json.dumps(_answer(Q_ROOT, role="analytics")[1]), encoding="utf-8")
    out = gallery.rebuild_index(d)
    assert [e["trace_id"] for e in out["traces"]] == ["X1"] and [p["trace_id"] for p in out["pending_recordings"]] == ["A1", "A2", "A3", "A4"]   # keyed by trace id, not file name


def test_committed_traces_validate_and_contain_no_secrets():
    for i in ("A1", "A2", "A3", "A4", "H1", "H2", "H3", "H4", "H5"):
        t = json.loads((ROOT / "traces" / f"{i}.json").read_text(encoding="utf-8"))
        assert trace.validate_trace(t, ENGINE.docs) == [] and t["specs"] == ENGINE.specs()
        assert not trace.SECRET.search(json.dumps(t))


def _rec(answer, rows, question="Q"):
    return dict(question=question, answer=answer, tasks=[dict(task_id="t1", status="ok", rows=rows)], context_ids=[])


def test_numeric_check_ignores_list_ordinals_and_supported_dates_but_still_flags_real_numbers():
    rows = [["Tile Cache", 40.687], ["Map Service", 20.57]]
    assert trace.unsupported_numbers(_rec("1. **Tile Cache**: 40.69 hours [sql:t1]\n2. **Map Service**: 20.57 hours [sql:t1]", rows), {}) == []
    assert trace.unsupported_numbers(_rec("1. **Tile Cache**: 40.69 hours [sql:t1]\n2. **Map Service**: 99 hours [sql:t1]", rows), {}) == ["99"]
    dated = [["2025-10", "database", 5]]
    assert trace.unsupported_numbers(_rec("**2025-10:** database: 5 [sql:t1]", dated), {}) == []
    assert trace.unsupported_numbers(_rec("**2025-11:** database: 5 [sql:t1]", dated), {}) == ["11"]       # a month that is in no result is still flagged
    assert trace.unsupported_numbers(_rec("**2025-10:** database: 6 [sql:t1]", dated), {}) == ["6"]        # and a wrong count next to a supported date too
    assert trace.unsupported_numbers(_rec("2025-10 had 999 incidents [sql:t1]", dated), {}) == ["999"]      # the month is ignored, the count is not
    assert trace.unsupported_numbers(_rec("There were 8 incidents [sql:t1]", dated), {}) == ["8"]           # ordinary numbers in running text are still checked
    assert trace.unsupported_numbers(_rec("There were 8 incidents [sql:t1]", [["x", 8]]), {}) == []         # and accepted when a result has them


def test_retrust_changes_only_the_trust_field_and_check_mode_writes_nothing():
    from . import retrust
    d = Path(tempfile.mkdtemp())
    for f in (ROOT / "traces").glob("*.json"):
        (d / f.name).write_bytes(f.read_bytes())
    assert retrust.retrust(d, check_only=True) == []                                  # the committed traces are current
    a1 = json.loads((d / "A1.json").read_text(encoding="utf-8"))
    stale = dict(a1, trust=dict(a1["trust"], numbers_not_in_any_result=["10", "11", "12"]))
    (d / "A1.json").write_bytes(trace.dump_json(stale))
    before = (d / "A1.json").read_bytes()
    assert retrust.retrust(d, check_only=True) == ["A1"] and (d / "A1.json").read_bytes() == before
    assert retrust.retrust(d) == ["A1"] and retrust.retrust(d, check_only=True) == []
    after = json.loads((d / "A1.json").read_text(encoding="utf-8"))
    assert after == a1 and b"\r" not in (d / "A1.json").read_bytes()                     # only `trust` moved, and it moved back to the committed value


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
