"""Rule-based visualization planner (no LLM). One task result in, one spec out. Anything it cannot draw safely becomes a table.

Rules, in this order:
  failed task -> empty            no rows -> empty            any long-text column (> 40 characters) -> table
  more than 30 rows (without a series) -> table              no numeric column -> table
  exactly 1 row -> kpi (one KPI per numeric column, at most 4)
  period column + numeric:  no series: 2-4 periods -> bar (chronological), >= 5 -> line; with a text series (2-8 values): >= 5 periods -> line
                            (multi-series), otherwise table
  text column + numeric:    2-12 distinct categories -> bar sorted by value descending (horizontal when labels are long or > 6 bars), else table
  several numeric columns:  the first is drawn, the others stay visible in the table that the UI always shows next to the chart
"""
from .spec import (LONG_TEXT, MAX_BARS, MAX_KPIS, MAX_POINTS, MAX_SERIES, MAX_TABLE_ROWS, MIN_BAR_PERIODS, MIN_LINE_PERIODS, column_values,
                   is_number, numeric_column, period_column, text_column)


def unit_of(column):
    c = column.lower()
    if "hour" in c:
        return "hours"
    if "pct" in c or "percent" in c or "share" in c:
        return "%"
    if "count" in c or "incident" in c:
        return "incidents"
    return ""


def _title(task):
    return " ".join(str(task.get("description", task["task_id"])).split())[:120]


def select(task):
    base = dict(task_id=task["task_id"], title=_title(task))
    if task.get("status") != "ok":
        return dict(base, kind="empty", reason="the SQL task did not succeed")
    cols, rows = task["columns"], task["rows"]
    if not rows:
        return dict(base, kind="empty", reason="the SQL task returned no rows")
    table = lambda why: dict(base, kind="table", reason=why)
    if any(isinstance(c, str) and len(c) > LONG_TEXT for r in rows for c in r):
        return table("a column holds long text")
    nums = [c for c in cols if numeric_column(task, c)]
    texts = [c for c in cols if text_column(task, c)]
    if not nums:
        return table("no numeric column")
    if len(rows) == 1:
        ks = [dict(column=c, label_columns=texts) for c in nums[:MAX_KPIS]]
        return dict(base, kind="kpi", kpis=ks, unit=unit_of(nums[0]), reason=f"one row with {len(nums)} numeric value(s)")
    y = nums[0]
    note = f"; {len(nums) - 1} other measure(s) stay in the table" if len(nums) > 1 else ""
    time_cols = [c for c in texts if period_column(column_values(task, c))]
    if time_cols:
        x = time_cols[0]
        others = [c for c in texts if c != x]
        n_periods = len(set(column_values(task, x)))
        series = next((c for c in others if 2 <= len(set(column_values(task, c))) <= MAX_SERIES), None)
        if series and n_periods < len(rows):                       # repeated periods: one line per series value
            if n_periods >= MIN_LINE_PERIODS and len(rows) <= MAX_POINTS and len(set(zip(column_values(task, x), column_values(task, series)))) == len(rows):
                return dict(base, kind="line", x=x, y=y, series=series, sort="x_asc", unit=unit_of(y),
                            reason=f"{n_periods} periods x {len(set(column_values(task, series)))} series{note}")
            return table("a time axis with repeated periods that cannot be drawn as a clean multi-series line")
        if n_periods != len(rows):
            return table("the time axis has repeated periods without a series column")
        if len(rows) >= MIN_LINE_PERIODS and len(rows) <= MAX_POINTS:
            return dict(base, kind="line", x=x, y=y, sort="x_asc", unit=unit_of(y), reason=f"{len(rows)} periods{note}")
        if MIN_BAR_PERIODS <= len(rows) < MIN_LINE_PERIODS:
            return dict(base, kind="bar", x=x, y=y, sort="x_asc", orientation="v", unit=unit_of(y), reason=f"{len(rows)} periods: too few for a trend line{note}")
        return table("too many periods")
    if len(rows) > MAX_TABLE_ROWS:
        return table(f"more than {MAX_TABLE_ROWS} rows")
    cat = next((c for c in texts if len(set(column_values(task, c))) == len(rows)), None)
    if cat is None:
        return table("no text column with one distinct value per row")
    if 2 <= len(rows) <= MAX_BARS:
        longest = max(len(str(v)) for v in column_values(task, cat))
        return dict(base, kind="bar", x=cat, y=y, sort="y_desc", orientation="h" if longest > 14 or len(rows) > 6 else "v", unit=unit_of(y),
                    reason=f"{len(rows)} categories{note}")
    return table(f"more than {MAX_BARS} categories")
