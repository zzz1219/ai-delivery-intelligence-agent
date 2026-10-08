# bi_data - shared data export (generated; do not edit)

Synthetic portfolio dataset with intentionally planted operational patterns; findings are demonstrations of analytical behavior, not real business observations.

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
- `is_repeat_90d`: closed incident with at least one other closed incident of the same project, component and root cause within +-90 days.
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
