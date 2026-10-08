"""Shared, tested data export for the Power BI report and the Streamlit demo (bi-export-1.0).

Principle: the PYTHON layer defines every business rule; Power BI only filters, aggregates and displays.
Derived columns (resolution_hours, is_repeat_90d, month, quarter, ...) are computed here, once, and reconciled against the sealed
Hybrid gold values before anything is written. If a reconciliation fails the export refuses to write.

Source: ONLY the public database data/delivery.db (open incidents have no root cause). The evaluation's hidden-truth folder is never read by the exporter.

  python -m analytics.export_bi              # reconcile, then write bi_data/
  python -m analytics.export_bi --check      # the files on disk must equal a fresh export
  python -m analytics.export_bi --out DIR    # write somewhere else

Star schema (fact_incidents has one row per incident; no aggregate tables):
  fact_incidents -> dim_project (project_id), dim_component (component_id), dim_deployment (deployment_id), dim_date (date_key = reported date)
dim_deployment deliberately has no project_id: a second path fact -> deployment -> project would make the model ambiguous in Power BI.
"""
import argparse
import csv
import hashlib
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "delivery.db"
OUT_DIR = ROOT / "bi_data"
EXPORT_VERSION = "bi-export-1.0"
REPEAT_WINDOW_DAYS = 90
TS = "%Y-%m-%d %H:%M:%S"
FOOTER = ("Synthetic portfolio dataset with intentionally planted operational patterns; findings are demonstrations of "
          "analytical behavior, not real business observations.")
SEVERITY_SORT = {"critical": 1, "high": 2, "medium": 3, "low": 4}

COLUMNS = {
    "fact_incidents": [
        ("incident_id", "text", "Primary key, one row per incident"),
        ("project_id", "text", "FK to dim_project"), ("component_id", "text", "FK to dim_component"),
        ("deployment_id", "text", "FK to dim_deployment"), ("date_key", "int", "FK to dim_date: the REPORTED date as YYYYMMDD"),
        ("severity", "text", "critical / high / medium / low"), ("severity_sort", "int", "1 = critical ... 4 = low (use as the sort column of severity)"),
        ("category", "text", "layer of the affected component (database, map_service, ...)"),
        ("status", "text", "open / closed"), ("is_open", "int", "1 if status is open"), ("is_closed", "int", "1 if status is closed"),
        ("is_critical", "int", "1 if severity is critical"),
        ("reported_at", "datetime", "when the incident was reported"), ("resolved_at", "datetime", "blank for open incidents"),
        ("resolution_hours", "float", "(resolved_at - reported_at) in hours; BLANK for open incidents, so AVERAGE() covers closed incidents only"),
        ("month", "text", "YYYY-MM of the reported date"), ("quarter", "text", "YYYY-Qn of the reported date (calendar quarters)"),
        ("root_cause", "text", "BLANK for open incidents: the root cause is not yet determined"),
        ("is_root_cause_known", "int", "1 for closed incidents; root-cause visuals must be restricted to these"),
        ("solution_pattern", "text", "blank for open incidents"),
        ("is_repeat_90d", "int", "1 if the incident is a CLOSED incident with at least one other closed incident of the same project, component and "
                                 "root cause within +-90 days; 0 otherwise (always 0 for open incidents)"),
        ("symptom_summary", "text", "short symptom description (public)"), ("resolution_summary", "text", "blank for open incidents")],
    "dim_project": [("project_id", "text", "Primary key"), ("project_name", "text", ""), ("start_date", "date", ""), ("project_status", "text", ""),
                    ("sla_tier", "text", "gold / silver / bronze"), ("customer_id", "text", ""), ("customer_name", "text", "fictional"),
                    ("industry", "text", "government / energy / transport / utilities"), ("region", "text", "")],
    "dim_component": [("component_id", "text", "Primary key"), ("component_name", "text", ""), ("layer", "text", "same values as fact_incidents.category")],
    "dim_deployment": [("deployment_id", "text", "Primary key"), ("env_type", "text", "single_node / distributed / cloud"),
                       ("version", "text", "software version, e.g. v3.2"), ("deployed_at", "date", "")],
    "dim_date": [("date_key", "int", "Primary key, YYYYMMDD"), ("date", "date", ""), ("year", "int", ""), ("quarter", "text", "YYYY-Qn"),
                 ("quarter_number", "int", ""), ("month", "text", "YYYY-MM (sorts chronologically)"), ("month_number", "int", ""),
                 ("month_name", "text", "sort by month_number"), ("day_of_week", "text", "")]}
RELATIONSHIPS = [
    dict(many="fact_incidents.project_id", one="dim_project.project_id"), dict(many="fact_incidents.component_id", one="dim_component.component_id"),
    dict(many="fact_incidents.deployment_id", one="dim_deployment.deployment_id"), dict(many="fact_incidents.date_key", one="dim_date.date_key")]
DEFINITIONS = {
    "resolution_hours": "(resolved_at - reported_at) in hours, closed incidents only",
    "repeat_incident": f"closed incident with at least one other closed incident of the same project, component and root cause within +-{REPEAT_WINDOW_DAYS} days "
                       "(distinct incidents, not pairs)",
    "root_cause": "known only for closed incidents; open incidents are not-yet-determined, never guessed",
    "month_quarter": "calendar month / quarter of the reported date",
    "date_key": "reported date; resolved dates are not a relationship"}


def _ts(s):
    return datetime.strptime(s, TS) if s else None


def build_tables(db_path=DB_PATH):
    """All tables as lists of dicts, using only the public database. Business definitions live here and nowhere else."""
    con = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    inc = [dict(r) for r in con.execute("SELECT * FROM incidents ORDER BY incident_id")]
    projects = {r["project_id"]: dict(r) for r in con.execute("SELECT * FROM projects")}
    customers = {r["customer_id"]: dict(r) for r in con.execute("SELECT * FROM customers")}
    comps = [dict(r) for r in con.execute("SELECT * FROM components ORDER BY component_id")]
    deps = [dict(r) for r in con.execute("SELECT * FROM deployments ORDER BY deployment_id")]
    con.close()

    for r in inc:
        r["_rep"], r["_res"] = _ts(r["reported_at"]), _ts(r["resolved_at"])
    # repeat flag: distinct closed incidents that have a closed neighbour of the same (project, component, root cause) within the window
    groups = {}
    for r in inc:
        if r["status"] == "closed" and r["root_cause"]:
            groups.setdefault((r["project_id"], r["component_id"], r["root_cause"]), []).append(r)
    repeat = set()
    window = timedelta(days=REPEAT_WINDOW_DAYS)
    for members in groups.values():
        for a in members:
            if any(b is not a and abs(a["_rep"] - b["_rep"]) <= window for b in members):
                repeat.add(a["incident_id"])

    fact = []
    for r in inc:
        closed = r["status"] == "closed"
        hours = round((r["_res"] - r["_rep"]).total_seconds() / 3600, 4) if closed and r["_res"] else ""
        fact.append(dict(
            incident_id=r["incident_id"], project_id=r["project_id"], component_id=r["component_id"], deployment_id=r["deployment_id"],
            date_key=int(r["_rep"].strftime("%Y%m%d")), severity=r["severity"], severity_sort=SEVERITY_SORT[r["severity"]], category=r["category"],
            status=r["status"], is_open=int(not closed), is_closed=int(closed), is_critical=int(r["severity"] == "critical"),
            reported_at=r["reported_at"], resolved_at=r["resolved_at"] or "", resolution_hours=hours,
            month=r["_rep"].strftime("%Y-%m"), quarter=f"{r['_rep'].year}-Q{(r['_rep'].month - 1) // 3 + 1}",
            root_cause=(r["root_cause"] or "") if closed else "", is_root_cause_known=int(closed and bool(r["root_cause"])),
            solution_pattern=(r["solution_pattern"] or "") if closed else "", is_repeat_90d=int(r["incident_id"] in repeat),
            symptom_summary=r["symptom_summary"] or "", resolution_summary=(r["resolution_summary"] or "") if closed else ""))
    dim_project = []
    for pid in sorted(projects):
        p, c = projects[pid], customers[projects[pid]["customer_id"]]
        dim_project.append(dict(project_id=pid, project_name=p["name"], start_date=p["start_date"], project_status=p["status"], sla_tier=p["sla_tier"],
                                customer_id=c["customer_id"], customer_name=c["name"], industry=c["industry"], region=c["region"]))
    dim_component = [dict(component_id=c["component_id"], component_name=c["name"], layer=c["layer"]) for c in comps]
    dim_deployment = [dict(deployment_id=d["deployment_id"], env_type=d["env_type"], version=d["version"], deployed_at=d["deployed_at"][:10]) for d in deps]
    first, last = min(r["_rep"] for r in inc).date(), max(r["_rep"] for r in inc).date()
    start = date(first.year, 3 * ((first.month - 1) // 3) + 1, 1)
    end_month = 3 * ((last.month - 1) // 3) + 3
    end = (date(last.year + (end_month == 12), end_month % 12 + 1, 1)) - timedelta(days=1)
    dim_date, d = [], start
    while d <= end:
        dim_date.append(dict(date_key=int(d.strftime("%Y%m%d")), date=d.isoformat(), year=d.year, quarter=f"{d.year}-Q{(d.month - 1) // 3 + 1}",
                             quarter_number=(d.month - 1) // 3 + 1, month=d.strftime("%Y-%m"), month_number=d.month, month_name=d.strftime("%B"),
                             day_of_week=d.strftime("%A")))
        d += timedelta(days=1)
    return dict(fact_incidents=fact, dim_project=dim_project, dim_component=dim_component, dim_deployment=dim_deployment, dim_date=dim_date)


# ------------------------------------------------------------------ reconciliation
def _gold():
    """Gold values of the SEALED Hybrid benchmark (hash-checked); the export must reproduce them."""
    sys.path.insert(0, str(ROOT))
    from rag.build_benchmark import load_frozen
    from rag.build_hybrid_benchmark import BENCH_PATH, HASH_PATH
    spec, _ = load_frozen(BENCH_PATH, HASH_PATH)
    return {f["id"]: f["gold"] for q in spec["questions"] for f in q["sql_facts"]}


def reconcile(tables, db_path=DB_PATH):
    """Return a list of problems (empty = the export reproduces the sealed gold values and respects the evidence boundary)."""
    P, gold = [], _gold()
    fact = tables["fact_incidents"]
    dep = {d["deployment_id"]: d for d in tables["dim_deployment"]}
    mean = lambda xs: sum(xs) / len(xs) if xs else float("nan")
    top_of = lambda d, key=None: max(d, key=key or d.get) if d else None
    env_of = lambda r: dep.get(r["deployment_id"], {}).get("env_type")
    close = lambda a, b, tol: abs(a - b) <= tol

    def check(label, ok, detail=""):
        if not ok:
            P.append(f"{label} {detail}".strip())

    closed = [r for r in fact if r["is_closed"]]
    check("incident count", len(fact) == 200, f"(got {len(fact)}, expected 200)")
    check("open incident count", sum(r["is_open"] for r in fact) == 15)
    leak = [r["incident_id"] for r in fact if r["is_open"] and (r["root_cause"] or r["solution_pattern"] or r["resolution_summary"] or r["is_root_cause_known"])]
    check("evidence boundary: open incidents expose a root cause / solution", not leak, f"{leak[:3]}")
    check("evidence boundary: open incidents have a resolution time", not [r for r in fact if r["is_open"] and r["resolution_hours"] != ""])
    # H1: database vs non-database mean resolution hours (closed)
    db = mean([r["resolution_hours"] for r in closed if r["category"] == "database"])
    other = mean([r["resolution_hours"] for r in closed if r["category"] != "database"])
    check("H1-s1 database mean hours", close(db, gold["H1-s1"], 0.06), f"({db:.2f} vs {gold['H1-s1']})")
    check("H1-s2 non-database mean hours", close(other, gold["H1-s2"], 0.06), f"({other:.2f} vs {gold['H1-s2']})")
    by_rc = {}
    for r in closed:
        by_rc.setdefault(r["root_cause"], []).append(r["resolution_hours"])
    top = top_of(by_rc, lambda k: mean(by_rc[k]))
    check("H1-s4/s5 root cause with the highest mean", top == gold["H1-s4"] and close(mean(by_rc.get(top, [float("nan")])), gold["H1-s5"], 0.06), f"({top})")
    # H2: repeat incidents by root cause (distinct incidents, definition frozen in the benchmark)
    reps = {}
    for r in fact:
        if r["is_repeat_90d"]:
            reps[r["root_cause"]] = reps.get(r["root_cause"], 0) + 1
    top2 = top_of(reps)
    check("H2-s1/s2 repeat incidents", top2 == gold["H2-s1"] and reps.get(top2) == gold["H2-s2"], f"({top2}={reps.get(top2)} vs {gold['H2-s1']}={gold['H2-s2']})")
    # H3: critical incidents in distributed deployments (closed)
    h3 = [r["resolution_hours"] for r in closed if r["severity"] == "critical" and env_of(r) == "distributed"]
    check("H3-s1/s2 critical distributed", len(h3) == gold["H3-s2"] and close(mean(h3), gold["H3-s1"], 0.06), f"(n={len(h3)}, mean={mean(h3):.2f})")
    h3rc = {}
    for r in closed:
        if r["severity"] == "critical" and env_of(r) == "distributed":
            h3rc[r["root_cause"]] = h3rc.get(r["root_cause"], 0) + 1
    t3 = top_of(h3rc)
    check("H3-s3/s4 most common root cause", t3 == gold["H3-s3"] and h3rc.get(t3) == gold["H3-s4"])
    # H4: map-service incidents per quarter and the version of the Q1 incidents
    q = {}
    for r in fact:
        if r["category"] == "map_service":
            q[r["quarter"]] = q.get(r["quarter"], 0) + 1
    got = [q.get("2025-Q4"), q.get("2026-Q1"), q.get("2026-Q2"), q.get("2026-Q3")]
    check("H4-s1..s4 map-service incidents per quarter", got == [gold["H4-s1"], gold["H4-s2"], gold["H4-s3"], gold["H4-s4"]], f"({got})")
    v = {}
    for r in fact:
        if r["category"] == "map_service" and r["quarter"] == "2026-Q1":
            ver = dep.get(r["deployment_id"], {}).get("version")
            v[ver] = v.get(ver, 0) + 1
    check("H4-s5/s6 Q1 version", top_of(v) == gold["H4-s5"] and v.get(gold["H4-s5"]) == gold["H4-s6"], f"({v})")
    # H5: open incidents per project and the open high-severity map-service incidents of the top project
    op = {}
    for r in fact:
        if r["is_open"]:
            op[r["project_id"]] = op.get(r["project_id"], 0) + 1
    t5 = top_of(op)
    check("H5-s1/s2 open incidents", t5 == gold["H5-s1"] and op.get(t5) == gold["H5-s2"])
    ids = sorted(r["incident_id"] for r in fact if r["is_open"] and r["severity"] == "high" and r["category"] == "map_service" and r["project_id"] == t5)
    check("H5-s3 open high-severity map-service incidents", ids == sorted(gold["H5-s3"]), f"({ids})")
    # independent check against the database itself (a second code path for the resolution time)
    con = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    sql_mean = con.execute("SELECT AVG((julianday(resolved_at)-julianday(reported_at))*24) FROM incidents WHERE status='closed'").fetchone()[0]
    con.close()
    check("overall mean resolution hours vs SQL", close(mean([r["resolution_hours"] for r in closed]), sql_mean, 0.01))
    # internal consistency of the derived columns
    for r in fact:
        rep = datetime.strptime(r["reported_at"], TS)
        ok = (r["is_open"] + r["is_closed"] == 1 and r["month"] == rep.strftime("%Y-%m") and r["date_key"] == int(rep.strftime("%Y%m%d"))
              and r["severity_sort"] == SEVERITY_SORT[r["severity"]] and (not r["is_repeat_90d"] or r["is_closed"]))
        if r["is_closed"]:
            ok = ok and abs(r["resolution_hours"] - (datetime.strptime(r["resolved_at"], TS) - rep).total_seconds() / 3600) < 1e-3
        if not ok:
            P.append(f"derived columns inconsistent for {r['incident_id']}")
            break
    layer = {c["component_id"]: c["layer"] for c in tables["dim_component"]}
    bad_layer = [r["incident_id"] for r in fact if r["category"] != layer.get(r["component_id"])]
    check("category differs from the component's layer", not bad_layer, f"{bad_layer[:3]}")
    con = sqlite3.connect(f"file:{Path(db_path).resolve().as_posix()}?mode=ro", uri=True)
    bad_proj = con.execute("SELECT i.incident_id FROM incidents i JOIN deployments d ON d.deployment_id=i.deployment_id "
                           "WHERE i.project_id <> d.project_id").fetchall()
    con.close()
    check("source data: incident project differs from its deployment's project (dim_deployment has no project_id, so the model would hide it)",
          not bad_proj, f"{bad_proj[:3]}")
    keys = {d["date_key"] for d in tables["dim_date"]}
    check("referential integrity", all(r["date_key"] in keys and r["project_id"] in {p["project_id"] for p in tables["dim_project"]}
                                       and r["component_id"] in {c["component_id"] for c in tables["dim_component"]} and r["deployment_id"] in dep for r in fact))
    return P


# ------------------------------------------------------------------ writing
def _csv_bytes(name, rows):
    cols = [c[0] for c in COLUMNS[name]]
    import io
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: r[c] for c in cols})
    return buf.getvalue().encode("utf-8")


def render(db_path=DB_PATH):
    """Everything that would be written: {filename: bytes}. Deterministic (no timestamps)."""
    tables = build_tables(db_path)
    problems = reconcile(tables, db_path)
    if problems:
        raise SystemExit("reconciliation failed, nothing written:\n- " + "\n- ".join(problems))
    files = {f"{name}.csv": _csv_bytes(name, rows) for name, rows in tables.items()}
    manifest = dict(
        export_version=EXPORT_VERSION, footer=FOOTER, source=dict(database="data/delivery.db", database_sha256=hashlib.sha256(Path(db_path).read_bytes()).hexdigest(),
                                                                  public_data_only=True),
        definitions=DEFINITIONS, relationships=RELATIONSHIPS, root_cause_rule="restrict root-cause visuals to is_root_cause_known = 1 (closed incidents)",
        reconciled_against="sealed hybrid-1.0 gold values (H1-H5 SQL facts) and the database",
        tables={name: dict(rows=len(rows), sha256=hashlib.sha256(files[f"{name}.csv"]).hexdigest(),
                           columns=[dict(name=c, type=t, description=d) for c, t, d in COLUMNS[name]]) for name, rows in tables.items()})
    files["MANIFEST.json"] = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    files["README.md"] = README.encode("utf-8")
    return files


README = f"""# bi_data - shared data export (generated; do not edit)

{FOOTER}

Import these CSVs into Power BI (Get data > Text/CSV, UTF-8). `MANIFEST.json` lists every column, the relationships and the SHA-256 of each file.

## Model (star schema)
`fact_incidents` (one row per incident) many-to-one to `dim_project`, `dim_component`, `dim_deployment` and `dim_date` (`date_key` = reported date).
Set every relationship to single direction, many-to-one. `dim_deployment` has no `project_id` on purpose (no second path to `dim_project`).
Sort `dim_date[month_name]` by `month_number` and `fact_incidents[severity]` by `severity_sort`. Mark `dim_date` as the date table.

## Column types to set in Power BI (CSV type inference can guess wrongly on columns with blanks)
```
fact_incidents : date_key, severity_sort, is_open, is_closed, is_critical, is_root_cause_known, is_repeat_90d  -> Whole number
                 reported_at, resolved_at -> Date/Time        resolution_hours -> Decimal number
dim_date       : date_key, year, quarter_number, month_number -> Whole number        date -> Date
dim_project    : start_date -> Date          dim_deployment : deployed_at -> Date
```

## Timelines
The data spans 2025-10 to 2026-09. Use `dim_date[month]` (YYYY-MM) for every multi-year timeline and `dim_date[quarter]` (YYYY-Qn) for quarters.
Use `month_name` only for month-of-year views: it drops the year and would merge October 2025 with a future October.

## Rules that live in the export, not in Power BI
- `resolution_hours` is blank for open incidents, so `AVERAGE` covers closed incidents only.
- `is_repeat_90d`: closed incident with at least one other closed incident of the same project, component and root cause within +-{REPEAT_WINDOW_DAYS} days.
- `root_cause` is blank for open incidents (not yet determined). Restrict root-cause visuals to `is_root_cause_known = 1` and say so on the page.

## Suggested measures (simple aggregations only; not executed in the build sandbox)
```
Incident Count          = COUNTROWS ( fact_incidents )
Open Incident Count     = SUM ( fact_incidents[is_open] )
Closed Incident Count   = SUM ( fact_incidents[is_closed] )
Critical Incident Count = SUM ( fact_incidents[is_critical] )
Avg Resolution Hours    = AVERAGE ( fact_incidents[resolution_hours] )
Repeat Incident Count   = SUM ( fact_incidents[is_repeat_90d] )
```
## Evidence boundary
The export reads only the public database. The hidden ground truth of the evaluation is used by the tests alone, to detect leakage; it is never consumed by the
export, the Streamlit demo or the Power BI report. Open incidents have no root cause: it is not yet determined, never guessed.

Expected values on the full data: 200 incidents, 15 open, mean resolution 38.4 h for database vs 16.7 h for the rest, 25 repeat incidents with root cause timeout.
"""


def write(out_dir=OUT_DIR, db_path=DB_PATH):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = render(db_path)
    for name, data in files.items():
        (out / name).write_bytes(data)
    return files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="the files on disk must equal a fresh export")
    ap.add_argument("--out", default=str(OUT_DIR))
    args = ap.parse_args()
    if args.check:
        files = render()
        bad = [n for n, d in files.items() if not (Path(args.out) / n).exists() or (Path(args.out) / n).read_bytes() != d]
        print("bi_data matches a fresh export" if not bad else f"MISMATCH: {bad}")
        raise SystemExit(1 if bad else 0)
    files = write(args.out)
    m = json.loads(files["MANIFEST.json"])
    print(f"wrote {len(files)} files to {args.out}, reconciled against the sealed gold values")
    for name, t in m["tables"].items():
        print(f"  {name:<16}{t['rows']:>5} rows")


if __name__ == "__main__":
    main()
