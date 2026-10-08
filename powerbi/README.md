# Power BI report

Three-page operations report built on the synthetic star schema in `../bi_data/` (one fact table, `fact_incidents`, 200 rows, plus `dim_project`, `dim_component`, `dim_deployment`, `dim_date`).

| File | Content |
|---|---|
| `delivery_operations_portfolio.pbix` | the report (Power BI Desktop) |
| `delivery_operations_portfolio.pdf` | static three-page export for viewing without Power BI Desktop |
| `screenshots/` | one 1920 px PNG per page, used in the main README |

## Pages
1. **Operations Overview**: incident, open, critical and repeat counts; average resolution hours (closed incidents); incidents by month and category; open incidents by project.
2. **Resolution Time and Root Causes**: resolution hours by component and layer; closed and repeat incidents by root cause (closed incidents only).
3. **Deployments and Customers**: Map Service incidents by version; incidents by environment type; project / customer / SLA-tier matrix.

## Reconciliation
Headline numbers on the unfiltered report: 200 incidents, 15 open, 29 critical, 185 closed, 75 repeat (40.5% of closed), 21.3 average resolution hours. They can be checked against the agent's SQL results with `python -m analytics.powerbi_checks` from the repository root.

## Opening and refreshing
The `.pbix` uses **import mode**: the data are stored inside the file, so it opens and displays without any external source. To refresh, repoint the data source to the local `bi_data/` folder (Home > Transform data > Data source settings). The file stores the author's local source path, which will not exist on another machine.

## Notes
- All data are synthetic with intentionally planted operational patterns; the report shows analytical behaviour, not real business observations.
- Resolution-time and root-cause visuals are filtered to closed incidents; open incidents have no root cause.
- Nothing in this folder is read by the agent, the benchmarks or the demo.
