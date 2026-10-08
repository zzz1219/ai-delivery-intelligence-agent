"""Visualization specification: allowed vocabulary, period detection and the validator.

A spec is a small dict that names a task and the columns to use. It never carries data. Allowed kinds: kpi, bar, line, table, empty.
"""
import re

KINDS = ("kpi", "bar", "line", "table", "empty")
ALLOWED_KEYS = {"task_id", "kind", "title", "unit", "reason", "x", "y", "series", "orientation", "sort", "kpis"}
MAX_TABLE_ROWS, MAX_BARS, MAX_SERIES, MAX_POINTS, MIN_LINE_PERIODS, MIN_BAR_PERIODS, MAX_KPIS, MAX_TITLE = 30, 12, 8, 96, 5, 2, 4, 120
LONG_TEXT = 40

_FAMILIES = [
    ("day", re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"), lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    ("month", re.compile(r"^(\d{4})-(\d{2})$"), lambda m: (int(m[1]), int(m[2]))),
    ("quarter", re.compile(r"^(\d{4})[ -]?Q([1-4])$"), lambda m: (int(m[1]), int(m[2]))),
    ("year", re.compile(r"^(\d{4})$"), lambda m: (int(m[1]),))]


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def period_key(value):
    """(family, sortable key) when value is a period such as 2026-03, 2025 Q4, 2026-Q1, 2025 or 2026-03-15; otherwise None."""
    if not isinstance(value, str):
        return None
    for name, rx, fn in _FAMILIES:
        m = rx.match(value.strip())
        if m:
            return name, fn(m)
    return None


def period_column(values):
    """True when EVERY value is a period of the SAME family (mixed formats are not a time axis)."""
    keys = [period_key(v) for v in values]
    return bool(keys) and all(keys) and len({k[0] for k in keys}) == 1


def column_values(task, name):
    j = task["columns"].index(name)
    return [r[j] for r in task["rows"]]


def numeric_column(task, name):
    vals = column_values(task, name)
    return bool(vals) and all(is_number(v) for v in vals)


def text_column(task, name):
    vals = column_values(task, name)
    return bool(vals) and all(isinstance(v, str) for v in vals)


def validate_spec(spec, tasks):
    """Return a list of problems (empty = valid). `tasks` maps task_id -> {columns, rows, status, ...}."""
    P = []
    extra = set(spec) - ALLOWED_KEYS
    if extra:
        P.append(f"unknown keys {sorted(extra)} (a spec may reference columns, never carry values)")
    kind = spec.get("kind")
    if kind not in KINDS:
        return P + [f"kind {kind!r} is not allowed"]
    if not isinstance(spec.get("title", ""), str) or len(spec.get("title", "")) > MAX_TITLE:
        P.append("title must be a string of at most %d characters" % MAX_TITLE)
    task = tasks.get(spec.get("task_id"))
    if task is None:
        return P + ["the spec references a task that does not exist"]
    if kind == "empty":
        return P
    if task.get("status") != "ok":
        return P + ["the referenced task did not succeed"]
    cols = task["columns"]

    def need(col, label):
        if col not in cols:
            P.append(f"{label} column {col!r} does not exist in the task result")
            return False
        return True
    if kind == "table":
        return P
    if kind == "kpi":
        if len(task["rows"]) != 1:
            P.append("a KPI needs exactly one row")
        ks = spec.get("kpis")
        if not isinstance(ks, list) or not 1 <= len(ks) <= MAX_KPIS:
            return P + [f"kpis must list 1 to {MAX_KPIS} entries"]
        for k in ks:
            if need(k.get("column"), "KPI") and not numeric_column(task, k["column"]):
                P.append(f"KPI column {k['column']!r} is not numeric")
            for lc in k.get("label_columns", []):
                need(lc, "KPI label")
        return P
    # bar / line
    x, y = spec.get("x"), spec.get("y")
    ok = need(x, "x") & need(y, "y")
    if not ok:
        return P
    if not numeric_column(task, y):
        P.append(f"y column {y!r} must be numeric in every row")
    temporal = period_column(column_values(task, x))
    n_rows = len(task["rows"])
    if kind == "bar":
        if not text_column(task, x):
            P.append("a bar chart needs a text x column")
        if n_rows > MAX_BARS:
            P.append(f"a bar chart is limited to {MAX_BARS} categories")
        if len(set(column_values(task, x))) != n_rows:
            P.append("x values must be unique in a bar chart")
        if temporal:
            if spec.get("sort") != "x_asc":
                P.append("a temporal bar chart must be sorted chronologically (x_asc)")
            if not MIN_BAR_PERIODS <= n_rows < MIN_LINE_PERIODS:
                P.append(f"a temporal bar chart needs {MIN_BAR_PERIODS} to {MIN_LINE_PERIODS - 1} periods")
        elif spec.get("sort") != "y_desc":
            P.append("a categorical bar chart must be sorted by value, descending (y_desc)")
        if spec.get("orientation") not in ("v", "h"):
            P.append("orientation must be 'v' or 'h'")
        return P
    # line
    if not temporal:
        P.append("a line chart needs a period x column of one consistent format")
    if spec.get("sort") != "x_asc":
        P.append("a line chart must be sorted chronologically (x_asc)")
    series = spec.get("series")
    if series is not None:
        if need(series, "series") and not text_column(task, series):
            P.append("the series column must be text")
        if series in cols and len(set(column_values(task, series))) > MAX_SERIES:
            P.append(f"a line chart is limited to {MAX_SERIES} series")
    if n_rows > MAX_POINTS:
        P.append(f"a line chart is limited to {MAX_POINTS} points")
    if temporal:
        keys = [(v, task_row_series(task, series, i)) for i, v in enumerate(column_values(task, x))]
        if len(set(keys)) != len(keys):
            P.append("duplicate (period, series) pairs: the result is not one value per point")
        if len({v for v, _ in keys}) < MIN_LINE_PERIODS:
            P.append(f"a line chart needs at least {MIN_LINE_PERIODS} distinct periods")
    return P


def task_row_series(task, series, i):
    return task["rows"][i][task["columns"].index(series)] if series in task["columns"] else None
