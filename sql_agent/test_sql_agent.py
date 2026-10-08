"""Plain-assert tests (pytest-compatible). Run from the delivery_agent directory:  python -m sql_agent.test_sql_agent"""
import hashlib
import json
import re
import tempfile
from datetime import datetime
from pathlib import Path

from . import evaluate, tools
from .agent import run_agent
from .llm import GeminiLLM, ScriptedLLM, is_provider_transient


def _hash():
    return hashlib.sha256(tools.DB_PATH.read_bytes()).hexdigest()


def test_schema_text():
    s = tools.get_schema_text()
    assert all(t in s for t in tools.ALLOWED_TABLES)
    assert "values: closed, open" in s and "sqlite_master" not in s


def test_checker_rejects_unsafe_queries():
    bad = ["DROP TABLE incidents", "DELETE FROM incidents", "UPDATE incidents SET status='x'",
           "INSERT INTO components VALUES ('X','y','z')", "SELECT 1; SELECT 2", "PRAGMA table_info(incidents)",
           "ATTACH DATABASE 'x.db' AS x", "SELECT * FROM sqlite_master", "SELECT * FROM no_such_table",
           "SELEC * FROM incidents", "WITH x AS (SELECT 1) DELETE FROM incidents", "",
           "SELECT load_extension('x')", "/* hi */ DROP TABLE incidents"]
    for sql in bad:
        assert not tools.check_query(sql)["ok"], f"should be rejected: {sql!r}"


def test_checker_accepts_valid_queries():
    good = ["SELECT COUNT(*) FROM incidents", "select * from incidents limit 3;",
            "WITH t AS (SELECT project_id FROM incidents) SELECT COUNT(*) FROM t",
            "-- note\nSELECT 1"]
    for sql in good:
        assert tools.check_query(sql)["ok"], f"should be accepted: {sql!r}"


def test_cte_variants():
    good = ["WITH a AS (SELECT project_id FROM incidents), b AS (SELECT project_id FROM a) SELECT COUNT(*) FROM b",
            "WITH t(pid, n) AS (SELECT project_id, COUNT(*) FROM incidents GROUP BY 1) SELECT * FROM t ORDER BY n DESC",
            "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 5) SELECT SUM(x) FROM c",
            "WITH q AS MATERIALIZED (SELECT 1 AS v) SELECT * FROM q"]
    for sql in good:
        r = tools.check_query(sql)
        assert r["ok"], f"should be accepted: {sql!r} -> {r['error']}"
    assert tools.execute_query(good[2])["rows"] == [[15]]
    # a CTE that merely shadows an internal name reads the CTE, not the real table: harmless, returns the constant
    assert tools.execute_query("WITH sqlite_master AS (SELECT 1) SELECT * FROM sqlite_master")["rows"] == [[1]]
    # a CTE name must never unlock the real internal tables
    for sql in ["WITH x AS (SELECT * FROM sqlite_master) SELECT * FROM x",
                "SELECT * FROM sqlite_schema",
                "WITH RECURSIVE r(n) AS (SELECT name FROM sqlite_master UNION ALL SELECT n FROM r) SELECT * FROM r"]:
        assert not tools.check_query(sql)["ok"], f"should be rejected: {sql!r}"


def test_execute_rowcap_and_timeout():
    r = tools.execute_query("SELECT * FROM incidents")
    assert r["ok"] and len(r["rows"]) == 200 and not r["truncated"]
    r = tools.execute_query("SELECT * FROM incidents", max_rows=10)
    assert r["truncated"] and len(r["rows"]) == 10
    r = tools.execute_query("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT COUNT(*) FROM c",
                            timeout=0.5)
    assert not r["ok"] and "timed out" in r["error"]


def test_blob_size_is_capped():
    r = tools.execute_query("SELECT length(zeroblob(500000000))")
    assert not r["ok"]


def test_llm_exception_does_not_abort_evaluation():
    class Boom:
        name = "boom"

        def complete(self, task, system, user):
            raise RuntimeError("rate limited")
    case = evaluate.load_sql_cases()[0]
    r = evaluate.run_case(case, Boom())
    assert r["status"] == "error" and not r["strict"] and r["attempts"] is None
    assert r["error_class"] == "runtime" and "rate limited" in r["error_message"]   # a plain RuntimeError is not provider trouble


class _FakeGemini:
    """Mimics client.models.generate_content; raises queued errors first."""
    def __init__(self, errors, text="```sql\nSELECT 1\n```"):
        self.errors, self.text, self.calls = list(errors), text, 0
        self.models = self

    def generate_content(self, model, contents, config):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return type("R", (), {"text": self.text})()


class _Err(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def test_gemini_adapter_retries_rate_limits():
    import sys, types
    fake_types = types.SimpleNamespace(GenerateContentConfig=lambda **kw: kw)
    sys.modules.setdefault("google", types.ModuleType("google"))
    sys.modules["google.genai"] = types.SimpleNamespace(types=fake_types)
    sys.modules["google.genai.types"] = fake_types
    sleeps = []
    c = _FakeGemini([_Err(429, "RESOURCE_EXHAUSTED"), _Err(503, "UNAVAILABLE")])
    llm = GeminiLLM(model="m", client=c, sleep=sleeps.append, jitter=lambda: 1.0)
    assert "SELECT 1" in llm.complete("sql", "sys", "user") and c.calls == 3 and sleeps == [5, 10]
    c = _FakeGemini([_Err(400, "bad request")])
    try:
        GeminiLLM(model="m", client=c, sleep=sleeps.append).complete("sql", "s", "u")
        raise AssertionError("non-retryable error must propagate")
    except _Err:
        assert c.calls == 1
    c = _FakeGemini([_Err(429, "RESOURCE_EXHAUSTED")] * 10)
    try:
        GeminiLLM(model="m", client=c, max_retries=2, sleep=lambda s: None).complete("sql", "s", "u")
        raise AssertionError("must give up after max_retries")
    except _Err:
        assert c.calls == 3
    try:
        GeminiLLM(model="m", client=_FakeGemini([], text=""), sleep=lambda s: None).complete("sql", "s", "u")
        raise AssertionError("empty response must raise")
    except RuntimeError:
        pass
    try:
        GeminiLLM(client=c)
        raise AssertionError("model id is required")
    except ValueError:
        pass


def test_incomplete_run_is_flagged():
    ok = dict(status="ok", executed=True, strict=True, loose=True, id="S1")
    err = dict(status="error", executed=False, strict=False, loose=False, id="S2",
               error_type="ClientError", error_message="429 RESOURCE_EXHAUSTED")
    skip = dict(status="skipped", reason="circuit_breaker", id="S3")
    assert evaluate.summarize([ok, ok])["complete"]
    sm = evaluate.summarize([ok, err, skip])
    assert not sm["complete"] and sm["errors"] == ["S2"] and sm["skipped"] == ["S3"] and "429" in sm["first_error"]
    assert sm["n_scored"] == 1 and sm["n_total"] == 3      # error / skipped never enter the denominator


def test_results_never_overwrite_and_errors_are_detailed():
    from datetime import datetime
    a = evaluate.result_path("gemini:gemini-3.8-flash", True, datetime(2026, 10, 5, 22, 15, 0))
    b = evaluate.result_path("gemini:gemini-3.8-flash", True, datetime(2026, 10, 6, 9, 0, 0))
    c = evaluate.result_path("gemini:gemini-3.8-flash", False, datetime(2026, 10, 6, 9, 0, 0))
    assert a != b and a.name == "sql_gemini_gemini-3.8-flash_20261005_221500.json"
    assert c.name.endswith("_INCOMPLETE.json")

    class Boom:
        name = "boom"

        def complete(self, task, system, user):
            raise KeyError("provider changed response shape")
    r = evaluate.run_case(evaluate.load_sql_cases()[0], Boom())
    assert r["error_type"] == "KeyError" and "provider changed" in r["error_message"] and r["error_class"] == "runtime"
    assert "KeyError" in evaluate.summarize([r])["first_error"]


def test_provider_error_classification():
    assert is_provider_transient(_Err(503, "UNAVAILABLE"))
    assert is_provider_transient(_Err(429, "RESOURCE_EXHAUSTED"))
    assert is_provider_transient(TimeoutError("t")) and is_provider_transient(ConnectionError("c"))
    for exc in [TypeError("bad"), KeyError("x"), RuntimeError("503 in my own message"), _Err(400, "bad request"),
                AttributeError("'NoneType' has no attribute 'text'")]:
        assert not is_provider_transient(exc), repr(exc)


class _Flaky:
    """Oracle SQL, except that chosen questions raise the given exception."""
    name = "flaky"

    def __init__(self, cases, fail=None):
        self.inner = evaluate.make_llm("oracle", cases)
        self.fail = fail or {}                      # question id -> exception factory

    def complete(self, task, system, user):
        q = re.search(r"Question: (.*)", user).group(1).strip()
        for cid, factory in self.fail.items():
            if CASES_BY_ID[cid]["question"] == q:
                raise factory()
        return self.inner.complete(task, system, user)


CASES_BY_ID = {c["id"]: c for c in evaluate.load_sql_cases()}
NOW = datetime(2026, 10, 5, 22, 30, 0)


def _run(fail=None, threshold=2):
    cases = list(CASES_BY_ID.values())
    d = tempfile.mkdtemp()
    path, results, sm, code = evaluate.run_evaluation(cases, _Flaky(cases, fail), out_dir=d, now=NOW,
                                                      threshold=threshold)
    return Path(d), path, results, sm, code


def test_complete_run_finalizes_file():
    d, path, results, sm, code = _run()
    assert code == 0 and sm["complete"] and path.name == "sql_flaky_20261005_223000.json"
    assert [p.name for p in d.iterdir()] == [path.name]       # progress file renamed, nothing left behind
    assert json.loads(path.read_text())["meta"]["state"] == "complete"


def test_circuit_breaker_after_two_consecutive_provider_errors():
    d, path, results, sm, code = _run({"S2": lambda: _Err(503, "UNAVAILABLE"), "S3": lambda: _Err(503, "UNAVAILABLE")})
    assert [r["status"] for r in results] == ["ok", "error", "error", "skipped", "skipped"]
    assert results[3]["reason"] == "circuit_breaker" and "strict" not in results[3]
    assert code == 2 and not sm["complete"] and sm["n_scored"] == 1 and sm["skipped"] == ["S4", "S5"]
    assert path.name.endswith("_INCOMPLETE.json") and json.loads(path.read_text())["meta"]["state"] == "incomplete"


def test_non_consecutive_provider_errors_do_not_trip_the_breaker():
    d, path, results, sm, code = _run({"S2": lambda: _Err(503, "UNAVAILABLE"), "S4": lambda: _Err(429, "RESOURCE_EXHAUSTED")})
    assert [r["status"] for r in results] == ["ok", "error", "ok", "error", "ok"]
    assert code == 2 and not sm["complete"] and sm["skipped"] == []


def test_runtime_error_aborts_immediately():
    d, path, results, sm, code = _run({"S3": lambda: TypeError("sdk interface changed")})
    assert [r["status"] for r in results] == ["ok", "ok", "error", "skipped", "skipped"]
    assert results[2]["error_class"] == "runtime" and "TypeError" in results[2]["error_traceback"]
    assert results[3]["reason"] == "aborted_on_runtime_error" and code == 3
    assert json.loads(path.read_text())["meta"]["state"] == "aborted"


def test_progress_is_saved_when_interrupted():
    cases = list(CASES_BY_ID.values())
    d = tempfile.mkdtemp()
    try:
        evaluate.run_evaluation(cases, _Flaky(cases, {"S3": KeyboardInterrupt}), out_dir=d, now=NOW)
        raise AssertionError("KeyboardInterrupt must propagate")
    except KeyboardInterrupt:
        pass
    saved = json.loads((Path(d) / "sql_flaky_20261005_223000_INCOMPLETE.json").read_text())
    assert saved["meta"]["state"] == "interrupted" and [r["id"] for r in saved["results"]] == ["S1", "S2"]


def test_database_is_never_modified():
    before = _hash()
    conn = tools.connect_readonly(guarded=False)
    try:
        conn.execute("CREATE TABLE x(a)")
        raise AssertionError("write on read-only connection should fail")
    except Exception as e:
        assert "readonly" in str(e).lower() or "read-only" in str(e).lower()
    for sql in ["DROP TABLE incidents", "UPDATE incidents SET status='x'"]:
        tools.execute_query(sql)
    assert _hash() == before


def test_retry_after_forbidden_query():
    llm = ScriptedLLM(["```sql\nDROP TABLE incidents;\n```", "```sql\nSELECT COUNT(*) FROM incidents\n```"], answer="200 incidents")
    s = run_agent("How many incidents are there?", llm)
    assert s["status"] == "ok" and s["attempts"] == 2 and s["result"]["rows"] == [[200]]


def test_gives_up_after_three_bad_attempts():
    s = run_agent("anything", ScriptedLLM(["```sql\nDROP TABLE incidents\n```"]))
    assert s["status"] == "failed" and s["attempts"] == 3 and s["answer"].startswith("Could not")


def test_evaluator_oracle_passes_and_detects_wrong_sql():
    cases = {c["id"]: c for c in evaluate.load_sql_cases()}
    assert len(cases) == 5
    llm = evaluate.make_llm("oracle", list(cases.values()))
    for c in cases.values():
        r = evaluate.run_case(c, llm)
        assert r["strict"] and r["loose"], c["id"]
    wrong = ScriptedLLM(["```sql\nSELECT severity, COUNT(*) FROM incidents WHERE status='open' GROUP BY severity\n```"])
    assert not evaluate.run_case(cases["S5"], wrong)["strict"]
    wrong_s1 = ScriptedLLM(["```sql\nSELECT project_id, COUNT(*) FROM incidents GROUP BY project_id ORDER BY 2 ASC LIMIT 1\n```"])
    assert not evaluate.run_case(cases["S1"], wrong_s1)["strict"]


# ---- comparator regression: replay the SQL the real model produced in the v1.0 run, plus edge cases ----
V10_MODEL_SQL = {
    "S1": "SELECT \n    p.project_id,\n    p.name,\n    COUNT(i.incident_id) AS critical_incident_count\nFROM projects p\nJOIN incidents i ON p.project_id = i.project_id\nWHERE i.severity = 'critical'\n  AND i.reported_at >= '2026-07-01 00:00:00'\n  AND i.reported_at < '2026-10-01 00:00:00'\nGROUP BY p.project_id, p.name\nORDER BY critical_incident_count DESC\nLIMIT 1",
    "S2": "SELECT \n    category,\n    AVG((julianday(resolved_at) - julianday(reported_at)) * 24) AS avg_resolution_time_hours\nFROM incidents\nWHERE status = 'closed'\nGROUP BY category\nORDER BY avg_resolution_time_hours DESC",
    "S3": "SELECT \n    strftime('%Y-%m', i.reported_at) AS incident_month,\n    COUNT(i.incident_id) AS incident_count\nFROM incidents i\nJOIN components c ON i.component_id = c.component_id\nWHERE c.name = 'map_service' OR c.layer = 'map_service' OR i.category = 'map_service'\nGROUP BY strftime('%Y-%m', i.reported_at)\nORDER BY incident_month",
    "S4": "SELECT \n    d.env_type,\n    CAST(COUNT(i.incident_id) AS REAL) / COUNT(DISTINCT d.deployment_id) AS timeouts_per_deployment\nFROM deployments d\nJOIN incidents i ON d.deployment_id = i.deployment_id\nWHERE i.root_cause = 'timeout'\nGROUP BY d.env_type\nORDER BY timeouts_per_deployment DESC\nLIMIT 1",
    "S5": "SELECT \n    severity,\n    CAST(SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) AS REAL) / COUNT(*) AS open_share\nFROM incidents\nGROUP BY severity\nORDER BY severity"
}


def _score(cid, sql):
    case = {c["id"]: c for c in evaluate.load_sql_cases()}[cid]
    return evaluate.run_case(case, ScriptedLLM(["```sql\n" + sql + "\n```"]))


def test_v10_model_outputs_under_v11_contracts():
    expected = {"S1": (True, True), "S2": (True, True), "S3": (True, True),
                "S4": (False, True),      # LIMIT 1: right top entity, but the question now asks for every type
                "S5": (True, True)}       # fraction instead of percent is accepted for the proportion metric only
    for cid, (strict, loose) in expected.items():
        r = _score(cid, V10_MODEL_SQL[cid])
        assert (r["strict"], r["loose"]) == (strict, loose), (cid, r["score_note"])
    r = _score("S4", V10_MODEL_SQL["S4"])
    assert r["result_columns"] and r["result_row_count"] == 1 and r["result_rows"]   # columns and rows are saved


def test_s4_hidden_denominator_error_is_caught():
    wrong = """SELECT d.env_type, CAST(COUNT(i.incident_id) AS REAL)/COUNT(DISTINCT d.deployment_id) AS timeouts_per_deployment
               FROM deployments d JOIN incidents i ON d.deployment_id=i.deployment_id
               WHERE i.root_cause='timeout' GROUP BY d.env_type ORDER BY 2 DESC"""
    r = _score("S4", wrong)       # single_node 2.4 instead of 2.0, although the top entity is right
    assert not r["strict"] and r["loose"] and "differ" in r["score_note"]
    right = """SELECT d.env_type, 1.0*SUM(CASE WHEN i.root_cause='timeout' THEN 1 ELSE 0 END)/COUNT(DISTINCT d.deployment_id) AS rate
               FROM deployments d LEFT JOIN incidents i ON i.deployment_id=d.deployment_id GROUP BY d.env_type ORDER BY 2 DESC"""
    assert _score("S4", right)["strict"]


def test_proportion_scale_is_not_a_global_leniency():
    pct = "SELECT severity, ROUND(100.0*SUM(status='open')/COUNT(*),1) AS pct_open FROM incidents GROUP BY severity"
    frac = "SELECT severity, 1.0*SUM(status='open')/COUNT(*) AS open_share FROM incidents GROUP BY severity"
    off10 = "SELECT severity, 10.0*SUM(status='open')/COUNT(*) AS pct_open FROM incidents GROUP BY severity"
    assert _score("S5", pct)["scale_used"] == "percent" and _score("S5", frac)["scale_used"] == "fraction"
    assert not _score("S5", off10)["strict"]                       # a real order-of-magnitude error is still wrong
    # no other question may be answered on a different scale: S2 values divided by 100 must fail
    assert not _score("S2", "SELECT category, AVG((julianday(resolved_at)-julianday(reported_at))*24)/100 AS avg_hours "
                            "FROM incidents WHERE status='closed' GROUP BY category")["strict"]


def test_column_order_free_but_never_guessed():
    swapped = "SELECT 1.0*SUM(status='open')/COUNT(*) AS open_share, severity FROM incidents GROUP BY severity"
    assert _score("S5", swapped)["strict"]
    hinted = ("SELECT severity, SUM(status='open') AS open_count, COUNT(*) AS total_count, "
              "1.0*SUM(status='open')/COUNT(*) AS open_share FROM incidents GROUP BY severity")
    assert _score("S5", hinted)["strict"]                          # several numeric columns: picked by NAME
    vague = ("SELECT severity, SUM(status='open') AS a, 1.0*SUM(status='open')/COUNT(*) AS b "
             "FROM incidents GROUP BY severity")
    r = _score("S5", vague)
    assert not r["strict"] and r["score_note"] == "ambiguous value column"   # reported, not guessed
    assert not _score("S1", "SELECT project_id, COUNT(*) AS critical_incident_count FROM incidents "
                            "WHERE severity='critical' GROUP BY project_id ORDER BY 2 ASC LIMIT 1")["loose"]


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
