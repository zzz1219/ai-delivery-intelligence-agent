# Power BI operations report: build spec

A three-page report over the shared export in `bi_data/`. It is the **business view** of the same synthetic delivery data the agent works on; the agent is the **question-answering view**. The `.pbix`, the PDF export and the screenshots are built locally in Power BI Desktop.

> Footer text for **every** page (a text box, bottom of the page, small grey):
> `Synthetic portfolio dataset with intentionally planted operational patterns; findings are demonstrations of analytical behavior, not real business observations.`

## 0. Rules that keep the report honest
- Data come only from `bi_data/*.csv`. The hidden ground truth and any model output never enter the report.
- Measures are plain aggregations. No time intelligence, no `CALCULATE` gymnastics: filtering is done with slicers and visual-level filters, so each number can be reproduced by a pivot.
- Open incidents have **no root cause** (not yet determined). Every root-cause visual carries the visual filter `is_root_cause_known = 1` and the subtitle "closed incidents only".
- Average resolution time covers **closed incidents only** (`resolution_hours` is blank for open ones). Say so in the card subtitle.
- Describe the planted patterns as patterns of the synthetic data, never as discoveries about a real business.

## 1. Import and model (10 minutes)
1. Get data > Text/CSV: `fact_incidents`, `dim_project`, `dim_component`, `dim_deployment`, `dim_date` (UTF-8).
2. Column types (CSV inference can guess wrongly where there are blanks):
   - `fact_incidents`: `date_key`, `severity_sort`, `is_open`, `is_closed`, `is_critical`, `is_root_cause_known`, `is_repeat_90d` = Whole number; `reported_at`, `resolved_at` = Date/Time; `resolution_hours` = Decimal number.
   - `dim_date`: `date_key`, `year`, `quarter_number`, `month_number` = Whole number; `date` = Date. `dim_project[start_date]`, `dim_deployment[deployed_at]` = Date.
3. Relationships (all many-to-one, single direction, fact on the many side): `fact_incidents[project_id]` > `dim_project`, `[component_id]` > `dim_component`, `[deployment_id]` > `dim_deployment`, `[date_key]` > `dim_date[date_key]`. There is deliberately no `dim_deployment` to `dim_project` path.
4. Mark `dim_date` as the date table (`date`). Sort `dim_date[month_name]` by `month_number`, `fact_incidents[severity]` by `severity_sort`.
5. Use `dim_date[month]` (YYYY-MM) on every timeline and `dim_date[quarter]` (YYYY-Qn) for quarters. `month_name` drops the year; use it only for month-of-year views.

## 2. Measures (create in a `_Measures` table; format in brackets)
```DAX
Incident Count          = COUNTROWS ( fact_incidents )                        -- [#,0]
Open Incident Count     = SUM ( fact_incidents[is_open] )                     -- [#,0]
Closed Incident Count   = SUM ( fact_incidents[is_closed] )                   -- [#,0]
Critical Incident Count = SUM ( fact_incidents[is_critical] )                 -- [#,0]
Avg Resolution Hours    = AVERAGE ( fact_incidents[resolution_hours] )        -- [0.0]  closed incidents only
Repeat Incident Count   = SUM ( fact_incidents[is_repeat_90d] )               -- [#,0]
Repeat Share            = DIVIDE ( [Repeat Incident Count], [Closed Incident Count] ) -- [0.0%]  of closed incidents
```
Seven measures in total, each a single aggregation (or one division). If a visual needs a filter, put it on the visual, not in a measure.

Definitions you may be asked about: a **repeat incident** is a closed incident with at least one other closed incident of the same project, component and root cause within +-90 days (distinct incidents, not pairs). `Repeat Share` is therefore a share of closed incidents.

## 3. Pages

### Page 1: Operations overview
```
+----------------------------------------------------------------------------------+
| Delivery operations overview                       [Quarter v] [Severity v] [Region v] |
+----------------------------------------------------------------------------------+
| [Incident Count] [Open Incident Count] [Critical Incident Count] [Avg Resolution Hours] [Repeat Incident Count] |
|        200              15                    29                    21.3 h              75        |
+--------------------------------------------------+-------------------------------+
| Incidents by month (line, legend = category)      | Open incidents by project      |
| x: dim_date[month]  y: [Incident Count]            | (bar, sorted descending)       |
| 12 months, 6 lines                                 | y: dim_project[project_name]   |
|                                                    | x: [Open Incident Count]       |
+--------------------------------------------------+-------------------------------+
| footer (synthetic data)                                                           |
+----------------------------------------------------------------------------------+
```
- Slicers: `dim_date[quarter]`, `fact_incidents[severity]`, `dim_project[region]`.
- Card subtitle for `Avg Resolution Hours`: "closed incidents only".
- Line chart: use a colour-blind-safe palette and the **same line weight for every category**; add no highlight to any one category.
- Bar chart of open incidents: visual filter `[Open Incident Count] is greater than 0` so zero-open projects do not clutter the chart.

### Page 2: Resolution time and root causes
```
+----------------------------------------------------------------------------------+
| Resolution time and root causes                              [Quarter v] [Layer v] |
+----------------------------------------------------------------------------------+
| [Avg Resolution Hours] [Closed Incident Count] [Repeat Incident Count] [Repeat Share] |
+--------------------------------------------------+-------------------------------+
| Average resolution hours by component (bar, desc)  | Average resolution hours by    |
| y: dim_component[component_name]                   | layer (bar, desc)              |
| x: [Avg Resolution Hours]                          | y: dim_component[layer]        |
+--------------------------------------------------+-------------------------------+
| Closed incidents by root cause (bar, desc)         | Repeat incidents by root cause |
| filter: is_root_cause_known = 1                    | (bar, desc)                    |
| subtitle: "closed incidents only"                  | x: [Repeat Incident Count]     |
+--------------------------------------------------+-------------------------------+
| footer (synthetic data)                                                           |
+----------------------------------------------------------------------------------+
```
- Slicers: `dim_date[quarter]`, `dim_component[layer]`.
- Root-cause visuals: visual filter `fact_incidents[is_root_cause_known] = 1`. The repeat visual needs no extra filter (repeat incidents are closed by definition) but keep the same subtitle.
- Bars always sorted by the measure, descending (categories), never alphabetically.

### Page 3: Deployments and customers
```
+----------------------------------------------------------------------------------+
| Deployments and customers                            [Quarter v] [Environment v]  |
+----------------------------------------------------------------------------------+
| Map-service incidents by deployment version        | Incidents by environment type  |
| (column) filter: category = map_service            | (column)                       |
| x: dim_deployment[version] y: [Incident Count]     | x: dim_deployment[env_type]    |
+--------------------------------------------------+-------------------------------+
| Project and customer table (matrix)                                                |
| rows: dim_project[project_name], dim_project[customer_name], dim_project[sla_tier]  |
| values: [Incident Count] [Open Incident Count] [Critical Incident Count]            |
|         [Avg Resolution Hours]                                                      |
+----------------------------------------------------------------------------------+
| footer (synthetic data)                                                           |
+----------------------------------------------------------------------------------+
```
- Slicers: `dim_date[quarter]`, `dim_deployment[env_type]`.
- The matrix replaces any customer chart: it is the one place customer and SLA tier appear. Sort by `[Incident Count]` descending. Conditional-format nothing.
- Version column chart: select quarter `2026-Q1` in the slicer to reproduce the table below.

## 4. Reconcile the report before you screenshot it
Run `python -m analytics.powerbi_checks` and compare. Each number is a count or a mean over the CSVs; the same figures appear in the sealed benchmark and in the Streamlit traces, so the report, the agent and the benchmark can be cross-checked.

| Where to look in the report | Expected |
|---|---|
| Page 1 cards (no slicer) | 200 incidents, 15 open, 29 critical, 21.3 h average resolution, 75 repeat incidents |
| Page 1 line chart | monthly totals 12, 17, 10, 9, 25, 35, 15, 15, 14, 13, 19, 16 (2025-10 to 2026-09); map_service line peaks at 21 in 2026-03 |
| Page 1 open by project | Grid Asset Mapping 9, Port Operations Map 2, four projects with 1 (matches A4 and H5) |
| Page 2 by component | Primary Database 40.7, Replica Database 34.8, Tile Cache 21.8, Map Service 20.6, Data Import Pipeline 16.1, API Gateway 13.2, Auth Service 11.2, Web Frontend 10.4 hours (matches A3) |
| Page 2 by layer | database 38.4 vs 21.0, 16.1, 13.2, 11.2, 10.4 for the other five layers (database vs rest 38.4 vs 16.7, matches H1) |
| Page 2 root cause (closed) | timeout 59, tile_cache_misconfig 25, crs_mismatch 20, authentication_failure 20, batch_size_too_large 18, missing_index 16, connection_pool_exhausted 15, frontend_config_error 10, service_publish_failure 2 (matches A2) |
| Page 2 repeat by root cause | timeout 25 (the value in the sealed H2 gold), tile_cache_misconfig 14, batch_size_too_large 11, crs_mismatch 11, missing_index 5, authentication_failure 4, frontend_config_error 3, connection_pool_exhausted 2 |
| Page 3 map_service in 2026-Q1 | v3.2 32, v3.1 8 (40 in total, matches H4) |
| Page 3 filter: distributed, critical, closed | 8 incidents, 35.4 h (matches H3) |

If a number differs, check in this order: a column type (blank-aware), a relationship direction, the `is_root_cause_known` filter, then the slicer state.

## 5. What to deliver from the build
1. `delivery_operations.pbix` (keep it out of the repo if it embeds data you do not want to publish; the CSVs are the source).
2. `delivery_operations.pdf`: File > Export > PDF, three pages, footer visible on each.
3. Three PNGs, one per page, 1920 wide, no browser chrome: `pbi_page1_overview.png`, `pbi_page2_resolution.png`, `pbi_page3_deployments.png`.
4. Optional: a one-line note of the Power BI Desktop version used.

## 6. Wording for the README (suggested)
> The same synthetic dataset is exported to a star schema (`bi_data/`) and reported in Power BI: one fact table (`fact_incidents`, one row per incident), four dimensions, and seven simple measures. Every headline number in the report is reconcilable with the agent's SQL results and the sealed benchmark.

Optional narrative link: on page 1 the map-service line rises in 2026-02 and 2026-03, and the agent's H4 question reports 40 map-service incidents in 2026-Q1. These are two granularities (month and quarter) of the same planted pattern in the synthetic data, not a new finding by the agent.
