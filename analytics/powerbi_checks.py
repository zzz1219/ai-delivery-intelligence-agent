"""Expected values for the Power BI report, computed from bi_data/*.csv with the standard library only.

  python -m analytics.powerbi_checks

Print these next to the report to reconcile it (every number below is a plain count or mean over the exported CSVs, no model involved).
"""
import csv
from collections import Counter, defaultdict
from pathlib import Path

BI = Path(__file__).resolve().parent.parent / "bi_data"


def _read(name):
    with open(BI / f"{name}.csv", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _mean(xs):
    xs = [float(x) for x in xs if x not in ("", None)]
    return round(sum(xs) / len(xs), 1) if xs else None


def expected():
    f = _read("fact_incidents")
    comp = {c["component_id"]: c for c in _read("dim_component")}
    proj = {p["project_id"]: p for p in _read("dim_project")}
    dep = {d["deployment_id"]: d for d in _read("dim_deployment")}
    for r in f:
        r["layer"], r["component_name"] = comp[r["component_id"]]["layer"], comp[r["component_id"]]["component_name"]
        r["project_name"], r["env_type"], r["version"] = proj[r["project_id"]]["project_name"], dep[r["deployment_id"]]["env_type"], dep[r["deployment_id"]]["version"]
    by = lambda key, rows=f: dict(Counter(r[key] for r in rows).most_common())
    avg_by = lambda key: dict(sorted(((k, _mean(r["resolution_hours"] for r in f if r[key] == k)) for k in {r[key] for r in f}), key=lambda kv: -kv[1]))
    closed_known = [r for r in f if r["is_root_cause_known"] == "1"]
    crit_dist = [r for r in f if r["is_critical"] == "1" and r["env_type"] == "distributed" and r["is_closed"] == "1"]
    map_q1 = [r for r in f if r["category"] == "map_service" and r["quarter"] == "2026-Q1"]
    return {
        "cards": dict(incident_count=len(f), open_incident_count=sum(int(r["is_open"]) for r in f), closed_incident_count=sum(int(r["is_closed"]) for r in f),
                      critical_incident_count=sum(int(r["is_critical"]) for r in f), avg_resolution_hours=_mean(r["resolution_hours"] for r in f),
                      repeat_incident_count=sum(int(r["is_repeat_90d"]) for r in f)),
        "incidents_by_month": dict(sorted(by("month").items())),
        "map_service_by_month": dict(sorted(by("month", [r for r in f if r["category"] == "map_service"]).items())),
        "incidents_by_quarter": dict(sorted(by("quarter").items())),
        "open_incidents_by_project": by("project_name", [r for r in f if r["is_open"] == "1"]),
        "avg_resolution_hours_by_component": avg_by("component_name"),
        "avg_resolution_hours_by_layer": avg_by("layer"),
        "closed_incidents_by_root_cause (is_root_cause_known = 1)": by("root_cause", closed_known),
        "repeat_incidents_by_root_cause": by("root_cause", [r for r in f if r["is_repeat_90d"] == "1"]),
        "critical_closed_in_distributed": dict(incident_count=len(crit_dist), avg_resolution_hours=_mean(r["resolution_hours"] for r in crit_dist)),
        "map_service_2026_Q1_by_version": by("version", map_q1),
    }


if __name__ == "__main__":
    for k, v in expected().items():
        print(f"{k}: {v}")
