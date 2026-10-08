"""Spec + recorded task -> render model (plain data for Plotly / Streamlit) and a fidelity check.

The render model only re-orders and copies cells of the recorded result; it computes nothing. check_fidelity proves it.
"""
from .spec import column_values, period_key


def _sort_key_x(v):
    pk = period_key(v)
    return pk[1] if pk else (0,)


def materialize(spec, task):
    kind, cols, rows = spec["kind"], task.get("columns", []), task.get("rows", [])
    model = dict(kind=kind, title=spec.get("title", ""), unit=spec.get("unit", ""), reason=spec.get("reason", ""), columns=cols, rows=[list(r) for r in rows],
                 x=[], series=[], kpis=[], orientation=spec.get("orientation", "v"))
    if kind == "kpi":
        row = rows[0]
        for k in spec["kpis"]:
            label_parts = [str(row[cols.index(c)]) for c in k.get("label_columns", [])]
            model["kpis"].append(dict(column=k["column"], value=row[cols.index(k["column"])],
                                      label=" / ".join(label_parts) if label_parts else k["column"].replace("_", " ")))
    elif kind == "bar":
        xi, yi = cols.index(spec["x"]), cols.index(spec["y"])
        pairs = [(r[xi], r[yi]) for r in rows]
        pairs.sort(key=(lambda p: _sort_key_x(p[0])) if spec["sort"] == "x_asc" else (lambda p: (-p[1], str(p[0]))))
        model["x"] = [p[0] for p in pairs]
        model["series"] = [dict(name=spec["y"], y=[p[1] for p in pairs])]
    elif kind == "line":
        xi, yi = cols.index(spec["x"]), cols.index(spec["y"])
        si = cols.index(spec["series"]) if spec.get("series") else None
        xs = sorted({r[xi] for r in rows}, key=_sort_key_x)
        names = sorted({r[si] for r in rows}) if si is not None else [spec["y"]]
        lookup = {(r[xi], r[si] if si is not None else spec["y"]): r[yi] for r in rows}
        model["x"] = xs
        model["series"] = [dict(name=n, y=[lookup.get((x, n)) for x in xs]) for n in names]       # None = no value for that period (a gap)
    return model


def check_fidelity(model, task, spec):
    """Every plotted value must be a cell of the recorded result, in the right row; nothing may be added or dropped."""
    P, cols, rows = [], task["columns"], task["rows"]
    if model["kind"] == "table" or model["kind"] == "empty":
        return [] if model["rows"] == [list(r) for r in rows] else ["the table rows differ from the recorded result"]
    if model["kind"] == "kpi":
        row = rows[0]
        for k in model["kpis"]:
            if k["value"] != row[cols.index(k["column"])]:
                P.append(f"KPI {k['column']} differs from the recorded value")
        return P
    xi, yi = cols.index(spec["x"]), cols.index(spec["y"])
    si = cols.index(spec["series"]) if spec.get("series") else None
    recorded = {(r[xi], r[si] if si is not None else spec["y"]): r[yi] for r in rows}
    plotted = {}
    for s in model["series"]:
        for x, v in zip(model["x"], s["y"]):
            if v is not None:
                plotted[(x, s["name"])] = v
    if plotted != recorded:
        P.append("the plotted points differ from the recorded rows (%d plotted vs %d recorded)" % (len(plotted), len(recorded)))
    return P
