"""Plain-assert tests for the BI export. No model, no network. Run from the delivery_agent folder:  python -m analytics.test_export_bi"""
import copy
import csv
import hashlib
import io
import json
import re
import sqlite3
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from . import export_bi as eb

TABLES = eb.build_tables()
FILES = eb.render()
FACT = TABLES["fact_incidents"]


def _read(name):
    name = name if name.endswith(".csv") else f"{name}.csv"
    return list(csv.DictReader(io.StringIO(FILES[name].decode("utf-8"), newline="")))


def _sql(sql, params=()):
    con = sqlite3.connect(f"file:{eb.DB_PATH.as_posix()}?mode=ro", uri=True)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def test_expected_files_row_counts_and_contract():
    assert sorted(FILES) == ["MANIFEST.json", "README.md", "dim_component.csv", "dim_date.csv", "dim_deployment.csv", "dim_project.csv", "fact_incidents.csv"]
    counts = {n: len(_read(f"{n}.csv")) for n in eb.COLUMNS}
    assert counts == {"fact_incidents": 200, "dim_project": 10, "dim_component": 8, "dim_deployment": 15, "dim_date": 365}
    m = json.loads(FILES["MANIFEST.json"])
    assert m["export_version"] == "bi-export-1.0" and m["footer"] == eb.FOOTER and m["source"]["public_data_only"] is True
    assert m["footer"] == ("Synthetic portfolio dataset with intentionally planted operational patterns; findings are demonstrations of "
                           "analytical behavior, not real business observations.")
    assert m["source"]["database_sha256"] == hashlib.sha256(eb.DB_PATH.read_bytes()).hexdigest()
    for name, t in m["tables"].items():
        data = FILES[f"{name}.csv"]
        assert t["sha256"] == hashlib.sha256(data).hexdigest() and t["rows"] == counts[name]
        assert [c["name"] for c in t["columns"]] == data.decode().splitlines()[0].split(",")      # header order = documented order
    assert eb.FOOTER in FILES["README.md"].decode() and "dim_deployment` has no `project_id`" in FILES["README.md"].decode()
    assert b"\r" not in FILES["fact_incidents.csv"]                                               # LF newlines on every OS
    assert "project_id" not in [c[0] for c in eb.COLUMNS["dim_deployment"]]                       # keeps the star schema unambiguous


def test_no_hidden_truth_leaks():
    src = Path(eb.__file__).read_text(encoding="utf-8")
    assert "ground_truth" not in src and "incidents_truth" not in src                              # the exporter never touches the hidden truth
    for name in eb.COLUMNS:
        header = FILES[f"{name}.csv"].decode().splitlines()[0].lower()
        assert "truth" not in header and "true_" not in header and "oracle" not in header
    open_rows = [r for r in _read("fact_incidents") if r["status"] == "open"]
    assert len(open_rows) == 15
    for r in open_rows:
        assert r["root_cause"] == r["solution_pattern"] == r["resolution_summary"] == r["resolved_at"] == r["resolution_hours"] == ""
        assert r["is_root_cause_known"] == "0" and r["is_repeat_90d"] == "0"
    # the true root causes of the open incidents (read ONLY here, in the test) must not appear anywhere in the exported rows of those incidents
    truth = {r["incident_id"]: r["true_root_cause"] for r in csv.DictReader(open(eb.ROOT / "ground_truth/incidents_truth.csv", encoding="utf-8"))}
    open_ids = {r["incident_id"] for r in open_rows}
    assert all(truth[i] for i in open_ids)
    for r in _read("fact_incidents"):                       # the public symptom text may use the word "timeout"; no OTHER field may carry the label
        if r["incident_id"] in open_ids:
            assert all(truth[r["incident_id"]] not in str(v) for k, v in r.items() if k != "symptom_summary"), r["incident_id"]


def test_resolution_hours_and_derived_columns_against_the_database():
    rows = _read("fact_incidents")
    db = {r[0]: r for r in _sql("SELECT incident_id, reported_at, resolved_at, status, severity FROM incidents")}
    for r in rows:
        iid, rep, res, status, sev = db[r["incident_id"]]
        assert r["status"] == status and r["severity"] == sev and r["reported_at"] == rep
        assert r["month"] == rep[:7] and r["date_key"] == rep[:10].replace("-", "")
        assert r["quarter"] == f"{rep[:4]}-Q{(int(rep[5:7]) - 1) // 3 + 1}"
        assert (r["is_open"], r["is_closed"]) == (("1", "0") if status == "open" else ("0", "1")) and r["is_critical"] == str(int(sev == "critical"))
        if status == "closed":
            hours = (datetime.strptime(res, eb.TS) - datetime.strptime(rep, eb.TS)).total_seconds() / 3600
            assert abs(float(r["resolution_hours"]) - hours) < 1e-3 and float(r["resolution_hours"]) > 0
    assert {r["severity"]: r["severity_sort"] for r in rows} == {"critical": "1", "high": "2", "medium": "3", "low": "4"}


def test_repeat_flag_against_an_independent_brute_force_and_the_sealed_gold():
    closed = [(r[0], r[1], r[2], r[3], datetime.strptime(r[4], eb.TS)) for r in
              _sql("SELECT incident_id, project_id, component_id, root_cause, reported_at FROM incidents WHERE status='closed'")]
    expected = set()
    for a in closed:                                         # all pairs, no grouping: a different code path from the exporter
        for b in closed:
            if a[0] != b[0] and a[1:4] == b[1:4] and abs(a[4] - b[4]) <= timedelta(days=90):
                expected.add(a[0])
    flagged = {r["incident_id"] for r in _read("fact_incidents") if r["is_repeat_90d"] == "1"}
    assert flagged == expected and len(flagged) > 50
    by_rc = {}
    for r in _read("fact_incidents"):
        if r["is_repeat_90d"] == "1":
            by_rc[r["root_cause"]] = by_rc.get(r["root_cause"], 0) + 1
    gold = eb._gold()
    assert max(by_rc, key=by_rc.get) == gold["H2-s1"] == "timeout" and by_rc["timeout"] == gold["H2-s2"] == 25
    assert sorted(by_rc.values(), reverse=True)[:2] == [25, 14]                                   # distinct incidents, not pairs (pairs would give 37 / 26)


def test_reconciliation_with_the_sealed_gold_values_and_the_database():
    gold = eb._gold()
    assert eb.reconcile(TABLES) == []
    rows = _read("fact_incidents")
    closed = [r for r in rows if r["is_closed"] == "1"]
    mean = lambda xs: sum(xs) / len(xs)
    assert round(mean([float(r["resolution_hours"]) for r in closed if r["category"] == "database"]), 1) == gold["H1-s1"] == 38.4
    assert round(mean([float(r["resolution_hours"]) for r in closed if r["category"] != "database"]), 1) == gold["H1-s2"] == 16.7
    sql_q = dict(_sql("""SELECT strftime('%Y',reported_at)||'-Q'||((CAST(strftime('%m',reported_at) AS INTEGER)+2)/3), COUNT(*)
                         FROM incidents WHERE category='map_service' GROUP BY 1"""))
    csv_q = {}
    for r in rows:
        if r["category"] == "map_service":
            csv_q[r["quarter"]] = csv_q.get(r["quarter"], 0) + 1
    assert csv_q == sql_q == {"2025-Q4": 11, "2026-Q1": 40, "2026-Q2": 11, "2026-Q3": 14}
    dep = {r["deployment_id"]: r for r in _read("dim_deployment")}
    crit = [float(r["resolution_hours"]) for r in closed if r["severity"] == "critical" and dep[r["deployment_id"]]["env_type"] == "distributed"]
    assert len(crit) == 8 and round(mean(crit), 1) == 35.4
    opens = {}
    for r in rows:
        if r["is_open"] == "1":
            opens[r["project_id"]] = opens.get(r["project_id"], 0) + 1
    assert max(opens, key=opens.get) == "P03" and opens["P03"] == 9 and len(rows) == 200


def test_star_schema_integrity_and_the_date_dimension():
    fact, proj, comp, dep, dd = (_read(n) for n in ("fact_incidents", "dim_project", "dim_component", "dim_deployment", "dim_date"))
    for rows, key in ((proj, "project_id"), (comp, "component_id"), (dep, "deployment_id"), (dd, "date_key"), (fact, "incident_id")):
        assert len({r[key] for r in rows}) == len(rows)                                          # primary keys are unique
    assert {r["project_id"] for r in fact} <= {r["project_id"] for r in proj} and {r["component_id"] for r in fact} <= {r["component_id"] for r in comp}
    assert {r["deployment_id"] for r in fact} <= {r["deployment_id"] for r in dep} and {r["date_key"] for r in fact} <= {r["date_key"] for r in dd}
    days = [datetime.strptime(r["date"], "%Y-%m-%d") for r in dd]
    assert days == sorted(days) and all((b - a).days == 1 for a, b in zip(days, days[1:])) and (days[0].date().isoformat(), days[-1].date().isoformat()) == ("2025-10-01", "2026-09-30")
    for r in dd:
        d = datetime.strptime(r["date"], "%Y-%m-%d")
        assert r["month"] == d.strftime("%Y-%m") and int(r["month_number"]) == d.month and r["month_name"] == d.strftime("%B")
        assert r["quarter"] == f"{d.year}-Q{(d.month - 1) // 3 + 1}" and r["date_key"] == d.strftime("%Y%m%d")
    assert {r["layer"] for r in comp} == {r["category"] for r in fact}                           # same vocabulary in the dimension and the fact
    assert all(r["industry"] and r["region"] and r["customer_name"] for r in proj)


def test_row_level_consistency_between_the_fact_table_and_the_dimensions_and_the_source():
    layer = {r["component_id"]: r["layer"] for r in _read("dim_component")}
    assert all(r["category"] == layer[r["component_id"]] for r in _read("fact_incidents"))      # per row, not just the same set of values
    assert not _sql("SELECT 1 FROM incidents i JOIN components c ON c.component_id=i.component_id WHERE i.category <> c.layer")
    assert not _sql("SELECT i.incident_id FROM incidents i JOIN deployments d ON i.deployment_id=d.deployment_id WHERE i.project_id <> d.project_id")
    # the old set-equality assertion could not catch a relabelled row; the reconciliation now does
    t = copy.deepcopy(TABLES)
    victim = next(r for r in t["fact_incidents"] if r["category"] == "map_service")
    victim["category"] = "database"
    assert {r["category"] for r in t["fact_incidents"]} == set(layer.values())                  # both value sets are still identical ...
    assert any("component's layer" in p for p in eb.reconcile(t))                                # ... and the row-level check reports it


def test_readme_documents_types_timelines_and_the_evidence_boundary():
    text = FILES["README.md"].decode("utf-8")
    for needle in ("Whole number", "Date/Time", "Decimal number", "dim_date[month]", "month_name", "never consumed by the", "Open incidents have no root cause"):
        assert needle in text, needle


def test_export_is_deterministic_and_check_detects_tampering():
    again = eb.render()
    assert again == FILES                                                                       # byte-identical, no timestamps
    d = Path(tempfile.mkdtemp())
    eb.write(d)
    assert all((d / n).read_bytes() == data for n, data in FILES.items())
    (d / "fact_incidents.csv").write_bytes((d / "fact_incidents.csv").read_bytes().replace(b"critical", b"CRITICAL", 1))
    assert (d / "fact_incidents.csv").read_bytes() != FILES["fact_incidents.csv"]


def test_mutations_are_caught_by_the_reconciliation():
    def problems(mutate):
        t = copy.deepcopy(TABLES)
        mutate(t)
        return " | ".join(eb.reconcile(t))
    first_critical = next(r for r in FACT if r["severity"] == "critical" and r["is_closed"])
    first_open = next(r for r in FACT if r["is_open"])
    first_repeat = next(r for r in FACT if r["is_repeat_90d"] and r["root_cause"] == "timeout")     # a timeout incident: the H2 winner
    cases = {
        "a repeat flag removed": (lambda t: [r.__setitem__("is_repeat_90d", 0) for r in t["fact_incidents"] if r["incident_id"] == first_repeat["incident_id"]], "H2"),
        "resolution time in days instead of hours": (lambda t: [r.__setitem__("resolution_hours", r["resolution_hours"] / 24) for r in t["fact_incidents"] if r["is_closed"]], "H1"),
        "a root cause leaked onto an open incident": (lambda t: [r.__setitem__("root_cause", "timeout") for r in t["fact_incidents"] if r["incident_id"] == first_open["incident_id"]], "evidence boundary"),
        "an off-by-one quarter": (lambda t: [r.__setitem__("quarter", "2026-Q2") for r in t["fact_incidents"] if r["category"] == "map_service" and r["quarter"] == "2026-Q1"][:1], "H4"),
        "a dropped incident": (lambda t: t["fact_incidents"].pop(), "incident count"),
        "a wrong deployment key": (lambda t: [r.__setitem__("deployment_id", "D99") for r in t["fact_incidents"]][:1], "referential"),
        "an open incident gets a resolution time": (lambda t: [r.__setitem__("resolution_hours", 5.0) for r in t["fact_incidents"] if r["incident_id"] == first_open["incident_id"]], "resolution time"),
        "a closed critical incident re-labelled": (lambda t: [r.__setitem__("severity", "high") for r in t["fact_incidents"] if r["incident_id"] == first_critical["incident_id"]], "derived columns")}
    for label, (mutate, needle) in cases.items():
        out = problems(mutate)
        assert out and needle.lower() in out.lower(), (label, out[:200])
    # changing a business definition in the exporter itself is caught too: a 60-day window breaks the H2 reconciliation
    saved = eb.REPEAT_WINDOW_DAYS
    try:
        eb.REPEAT_WINDOW_DAYS = 60
        try:
            eb.render()
            raise AssertionError("a different repeat window must make the export refuse to write")
        except SystemExit as e:
            assert "H2-s1/s2" in str(e) and "nothing written" in str(e)
    finally:
        eb.REPEAT_WINDOW_DAYS = saved


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
