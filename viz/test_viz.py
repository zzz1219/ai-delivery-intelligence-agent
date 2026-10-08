"""Plain-assert tests for the deterministic visualization layer. No model, no network, no plotting library.
Run from the delivery_agent folder:  python -m viz.test_viz"""
import copy
import json
import random
import sqlite3
from pathlib import Path

from . import materialize as mz
from . import select as sel
from . import spec as sp

ROOT = Path(__file__).resolve().parent.parent
RUN = ROOT / "benchmark_history/results_hybrid_v1/hyb_gemini_gemini-3_5-flash-lite_20261007_004003.json"


def T(cols, rows, tid="t1", desc="a task", status="ok"):
    return dict(task_id=tid, description=desc, status=status, columns=cols, rows=rows, row_count=len(rows))


def plan(task):
    spec = sel.select(task)
    assert sp.validate_spec(spec, {task["task_id"]: task}) == [], (spec, sp.validate_spec(spec, {task["task_id"]: task}))
    model = mz.materialize(spec, task)
    assert mz.check_fidelity(model, task, spec) == [], spec
    return spec, model


def _sql(sql):
    con = sqlite3.connect(f"file:{(ROOT / 'data/delivery.db').as_posix()}?mode=ro", uri=True)
    try:
        cur = con.execute(sql)
        return [d[0] for d in cur.description], [list(r) for r in cur.fetchall()]
    finally:
        con.close()


def test_rules_on_the_real_tasks_of_the_sealed_hybrid_run():
    run = json.loads(RUN.read_text(encoding="utf-8"))
    kinds = {}
    for r in run["results"]:
        for t in r["tasks"]:
            spec, model = plan(t)
            kinds[(r["id"], t["task_id"])] = spec["kind"]
    assert kinds == {("H1", "t1"): "bar", ("H1", "t2"): "kpi", ("H2", "t1"): "kpi", ("H3", "t1"): "kpi", ("H3", "t2"): "kpi",
                     ("H4", "t1"): "bar", ("H4", "t2"): "bar", ("H5", "t1"): "kpi", ("H5", "t2"): "table"}
    h4 = next(t for r in run["results"] if r["id"] == "H4" for t in r["tasks"] if t["task_id"] == "t1")
    shuffled = dict(h4, rows=[h4["rows"][2], h4["rows"][0], h4["rows"][1]])
    spec, model = plan(shuffled)
    assert spec["kind"] == "bar" and spec["sort"] == "x_asc" and model["x"] == ["2025 Q4", "2026 Q1", "2026 Q2"]   # chronological, whatever the row order
    assert model["series"][0]["y"] == [11, 40, 11]                                                              # 3 periods: a bar, not a 3-point "trend"
    h3 = next(t for r in run["results"] if r["id"] == "H3" for t in r["tasks"] if t["task_id"] == "t1")
    assert [k["column"] for k in plan(h3)[1]["kpis"]] == ["mean_resolution_time_hours", "total_closed_critical_incidents"]


def test_reference_shapes_of_the_four_demo_analytics_questions():
    qs = {
        "A1 monthly incidents by category": "SELECT strftime('%Y-%m', reported_at) AS month, category, COUNT(*) AS incident_count FROM incidents GROUP BY 1, 2 ORDER BY 1, 2",
        "A2 closed incidents by root cause": "SELECT root_cause, COUNT(*) AS closed_incidents FROM incidents WHERE status='closed' GROUP BY 1 ORDER BY 2 DESC",
        "A3 average resolution hours by component": "SELECT c.name AS component, ROUND(AVG((julianday(i.resolved_at)-julianday(i.reported_at))*24),1) AS avg_resolution_hours "
                                                    "FROM incidents i JOIN components c USING(component_id) WHERE i.status='closed' GROUP BY 1 ORDER BY 2 DESC",
        "A4 open incidents by project": "SELECT p.name AS project, COUNT(*) AS open_incidents FROM incidents i JOIN projects p USING(project_id) WHERE i.status='open' GROUP BY 1 ORDER BY 2 DESC"}
    out = {}
    for name, sql in qs.items():
        cols, rows = _sql(sql)
        out[name] = plan(T(cols, rows, desc=name))
    a1, a2, a3, a4 = (out[k] for k in qs)
    assert a1[0]["kind"] == "line" and a1[0]["series"] == "category" and len(a1[1]["x"]) == 12 and len(a1[1]["series"]) == 6      # multi-series line
    assert a1[1]["x"] == sorted(a1[1]["x"]) and a1[0]["unit"] == "incidents"
    assert a2[0]["kind"] == "bar" and a2[0]["orientation"] == "h" and a2[0]["sort"] == "y_desc" and len(a2[1]["x"]) == 9
    ys = a2[1]["series"][0]["y"]
    assert ys == sorted(ys, reverse=True)
    assert a3[0]["kind"] == "bar" and a3[0]["unit"] == "hours" and len(a3[1]["x"]) == 8
    assert a4[0]["kind"] == "bar" and a4[0]["y"] == "open_incidents" and a4[1]["series"][0]["y"][0] == 9 and a4[1]["x"][0] == "Grid Asset Mapping"


def test_rule_table_edge_cases():
    P = lambda n: [f"2026-{m:02d}" for m in range(1, n + 1)]
    kind = lambda cols, rows, **kw: plan(T(cols, rows, **kw))[0]["kind"]
    assert kind(["month", "n"], [["2026-01", 5]]) == "kpi"                                                     # 1 point
    assert [kind(["month", "n"], [[p, i] for i, p in enumerate(P(k))]) for k in (2, 3, 4, 5, 8)] == ["bar", "bar", "bar", "line", "line"]
    assert kind(["c", "n"], [[f"c{i}", i] for i in range(12)]) == "bar" and kind(["c", "n"], [[f"c{i}", i] for i in range(13)]) == "table"
    assert kind(["c", "n"], [[f"c{i}", i] for i in range(31)]) == "table"
    assert kind(["c", "n"], [["a" * 60, 1], ["b", 2]]) == "table"                                                # long text
    assert kind(["a", "b"], [["x", "y"], ["z", "w"]]) == "table"                                                 # no numeric column
    assert kind(["a", "n"], [["x", 1], ["y", None]]) == "table"                                                  # a None makes the column non-numeric
    assert kind(["a", "n"], [["x", True], ["y", False]]) == "table"                                              # booleans are not numbers
    assert kind(["a", "n"], [["x", 1]], status="failed") == "empty" and kind(["a", "n"], []) == "empty"
    assert kind(["y", "n"], [[str(2023 + i), i] for i in range(5)]) == "line"                                    # year labels
    assert kind(["d", "n"], [[f"2026-03-{i:02d}", i] for i in range(1, 8)]) == "line"                            # daily dates
    assert kind(["q", "n"], [["2025 Q4", 1], ["2026-Q1", 2], ["2026 Q2", 3]]) == "bar"                           # 'YYYY Qn' and 'YYYY-Qn' are one family, 3 periods
    assert kind(["m", "n"], [["2025-10", 1], ["2025 Q4", 2], ["2026", 3]]) == "bar"                              # mixed families: not a time axis -> by value
    s, m = plan(T(["c", "n", "k"], [["a", 1, 10], ["b", 2, 20]]))
    assert s["y"] == "n" and "other measure" in s["reason"] and m["columns"] == ["c", "n", "k"]                  # the other measure stays in the table
    assert kind(["m", "n"], [["2026-01", 1], ["2026-01", 2], ["2026-02", 3], ["2026-03", 4], ["2026-04", 5], ["2026-05", 6]]) == "table"   # repeated periods, no series
    many = [[f"2026-{m:02d}", f"s{k}", m + k] for m in range(1, 7) for k in range(9)]
    assert kind(["m", "s", "n"], many) == "table"                                                                # 9 series: too many
    ok = [[f"2026-{m:02d}", f"s{k}", m + k] for m in range(1, 7) for k in range(3)]
    assert plan(T(["m", "s", "n"], ok))[0]["series"] == "s"
    tie = plan(T(["c", "n"], [["b", 5], ["a", 5], ["c", 9]]))[1]
    assert tie["x"] == ["c", "a", "b"]                                                                           # by value, ties alphabetical (deterministic)
    gap = [["2026-01", "a", 1], ["2026-02", "a", 2], ["2026-03", "a", 3], ["2026-04", "a", 4], ["2026-05", "a", 5], ["2026-05", "b", 9]]
    m = plan(T(["m", "s", "n"], gap))[1]
    assert m["series"][1]["y"] == [None, None, None, None, 9]                                                    # a missing combination is a gap, never a zero


def test_validator_rejects_invalid_specs():
    t = T(["month", "n"], [[p, i] for i, p in enumerate([f"2026-{m:02d}" for m in range(1, 7)])])
    tasks = {"t1": t}
    good = sel.select(t)
    assert sp.validate_spec(good, tasks) == []
    bad = {
        "values injected": dict(good, values=[1, 2, 3]),
        "data injected": dict(good, data={"2026-01": 99}),
        "unknown kind": dict(good, kind="pie"),
        "missing task": dict(good, task_id="t9"),
        "missing x column": dict(good, x="nope"),
        "text y column": dict(good, y="month"),
        "line sorted by value": dict(good, sort="y_desc"),
        "title too long": dict(good, title="x" * 121)}
    for label, spec in bad.items():
        assert sp.validate_spec(spec, tasks), label
    assert sp.validate_spec(dict(good, task_id="t1"), {"t1": dict(t, status="failed")})                          # a failed task cannot be drawn
    four = T(["month", "n"], [[f"2026-0{m}", m] for m in range(1, 5)])
    assert sp.validate_spec(dict(good, kind="line", task_id="t1"), {"t1": four})                                  # a 4-period line is invalid
    assert sp.validate_spec(dict(kind="bar", task_id="t1", x="month", y="n", sort="y_desc", orientation="v"), {"t1": four})   # a temporal bar must be chronological
    cat = T(["c", "n"], [["a", 1], ["b", 2]])
    assert sp.validate_spec(dict(kind="bar", task_id="t1", x="c", y="n", sort="x_asc", orientation="v"), {"t1": cat})
    assert sp.validate_spec(dict(kind="bar", task_id="t1", x="c", y="n", sort="y_desc", orientation="diagonal"), {"t1": cat})
    assert sp.validate_spec(dict(kind="kpi", task_id="t1", kpis=[dict(column="n")]), {"t1": cat})                  # a KPI needs one row
    assert sp.validate_spec(dict(kind="kpi", task_id="t1", kpis=[dict(column="c")]), {"t1": T(["c"], [["a"]])})   # and a numeric column
    dup = T(["c", "n"], [["a", 1], ["a", 2]])
    assert sp.validate_spec(dict(kind="bar", task_id="t1", x="c", y="n", sort="y_desc", orientation="v"), {"t1": dup})


def test_spec_never_carries_data_and_fidelity_detects_tampering():
    run = json.loads(RUN.read_text(encoding="utf-8"))
    for r in run["results"]:
        for t in r["tasks"]:
            spec = sel.select(t)
            assert set(spec) <= sp.ALLOWED_KEYS
            for k, v in spec.items():
                assert isinstance(v, (str, list)) or v is None, k
                if isinstance(v, list):
                    assert k == "kpis" and all(set(i) <= {"column", "label_columns"} for i in v)
    cols, rows = _sql("SELECT strftime('%Y-%m', reported_at) AS month, category, COUNT(*) AS n FROM incidents GROUP BY 1, 2 ORDER BY 1, 2")
    task = T(cols, rows)
    spec, model = plan(task)
    tamper = {
        "one value changed": lambda m: m["series"][0]["y"].__setitem__(next(i for i, v in enumerate(m["series"][0]["y"]) if v is not None), 10 ** 6),
        "a point dropped": lambda m: m["series"][0]["y"].__setitem__(next(i for i, v in enumerate(m["series"][0]["y"]) if v is not None), None),
        "a point invented": lambda m: m["series"][0]["y"].__setitem__(next(i for i, v in enumerate(m["series"][0]["y"]) if v is None) if None in m["series"][0]["y"] else 0, 123.5)}
    for label, f in tamper.items():
        m2 = copy.deepcopy(model)
        f(m2)
        assert mz.check_fidelity(m2, task, spec), label
    k_task = T(["root_cause", "n"], [["timeout", 25]])
    k_spec, k_model = plan(k_task)
    k_model["kpis"][0]["value"] = 26
    assert mz.check_fidelity(k_model, k_task, k_spec)
    t_task = T(["a"], [["long " * 12], ["x"]])
    t_spec, t_model = plan(t_task)
    t_model["rows"][0][0] = "changed"
    assert mz.check_fidelity(t_model, t_task, t_spec)


def test_the_selector_always_emits_a_valid_faithful_spec_on_random_shapes():
    rng = random.Random(20261007)
    for _ in range(400):
        n_rows, n_cols = rng.randint(0, 40), rng.randint(1, 4)
        kinds = [rng.choice(["num", "cat", "period", "long", "none", "bool"]) for _ in range(n_cols)]
        cols = [f"c{i}" for i in range(n_cols)]

        def cell(kind, i):
            return {"num": rng.choice([rng.randint(-5, 500), round(rng.random() * 100, 2)]), "cat": rng.choice(["a", "b", "c", f"v{i}"]),
                    "period": rng.choice([f"2026-{rng.randint(1, 12):02d}", f"2025 Q{rng.randint(1, 4)}", "2026"]), "long": "x" * rng.randint(30, 60),
                    "none": None if rng.random() < 0.5 else 1, "bool": rng.random() < 0.5}[kind]
        rows = [[cell(k, i) for k in kinds] for i in range(n_rows)]
        task = T(cols, rows, status=rng.choice(["ok", "ok", "ok", "failed"]))
        plan(task)                                                                  # valid, and the render model reproduces the recorded cells


def test_mutations_of_the_layer_are_caught():
    def expect_failure(label, restore, test):
        try:
            test()
            raise AssertionError(f"mutation survived: {label}")
        except AssertionError as e:
            assert "mutation survived" not in str(e), str(e)
        finally:
            restore()
    # 1) chronological order broken
    orig = mz._sort_key_x
    mz._sort_key_x = lambda v: tuple(-x for x in (sp.period_key(v)[1] if sp.period_key(v) else (0,)))
    expect_failure("time axis sorted backwards", lambda: setattr(mz, "_sort_key_x", orig), test_rules_on_the_real_tasks_of_the_sealed_hybrid_run)
    # 2) the validator accepts injected values
    orig_allowed = sp.ALLOWED_KEYS
    sp.ALLOWED_KEYS = set(orig_allowed) | {"values", "data"}
    expect_failure("values allowed in a spec", lambda: setattr(sp, "ALLOWED_KEYS", orig_allowed), test_validator_rejects_invalid_specs)
    # 3) bars allowed beyond the category cap
    orig_bars = sel.MAX_BARS
    sel.MAX_BARS = 20
    expect_failure("bar cap raised", lambda: setattr(sel, "MAX_BARS", orig_bars), test_rule_table_edge_cases)
    # 4) a 3-point line instead of a bar
    orig_line = sel.MIN_LINE_PERIODS
    sel.MIN_LINE_PERIODS = 3
    expect_failure("line threshold lowered", lambda: setattr(sel, "MIN_LINE_PERIODS", orig_line), test_rules_on_the_real_tasks_of_the_sealed_hybrid_run)
    # 5) the fidelity check disabled
    orig_fid = mz.check_fidelity
    mz.check_fidelity = lambda *a, **k: []
    expect_failure("fidelity check disabled", lambda: setattr(mz, "check_fidelity", orig_fid), test_spec_never_carries_data_and_fidelity_detects_tampering)


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
