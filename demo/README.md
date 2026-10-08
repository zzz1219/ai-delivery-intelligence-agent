# Demo layer

Streamlit only loads a trace and draws its view model. Every rule lives in tested modules (`trace.py`, `engine.py`, `view_model.py`, `live.py`; charts come from `viz/`).

## Run (Windows PowerShell, from the `delivery_agent` folder)
```
pip install streamlit plotly pandas
streamlit run demo/streamlit_app.py
```
Replay mode needs no API key. The five sealed Hybrid runs (H1 to H5) are already in `traces/`.

## Record the four analytics questions (needs a key; keep the first recording)
```
$env:GEMINI_API_KEY="your-key"
python -m demo.record --model gemini-3.5-flash-lite
```
Existing traces are never overwritten. `--force` moves the old file to `traces/_superseded/`. Do not re-record to get a nicer result: a weak run is a real limit of the pipeline.

## What the page claims, and what it does not
- Charts are chosen by deterministic rules and drawn from the recorded SQL rows. No model chooses or edits a chart.
- "Automated evidence checks" verify evidence consistency (citations resolve, numbers appear in a result or document, the chart matches the rows). They do **not** verify
  the semantic correctness of the SQL or the reasoning: the sealed H2 answer passes every check and is still wrong.
- Key findings are sentences of the answer that carry a citation, shown unchanged.
- Questions are screened for basic API-key patterns before they are sent or stored. This is not a complete secret-detection system.
- Live mode uses the visitor's own key, passed to the client object for the session only (never the environment, a file or a trace), limited to 5 runs per session.
- The data are synthetic with intentionally planted patterns; findings demonstrate analytical behaviour, not real business observations.

## Recorded analytics runs (A1-A4, recorded 2026-10-07, first and only recording)
- A1-A4 were recorded once with `gemini:gemini-3.5-flash-lite` and the frozen pipeline; their questions, SQL, rows and answers are exactly what the model produced.
- Known limits shown by the real runs, kept as they are: the answer step only receives the first 30 rows of a result (A1 has 64 rows, so its answer stops at 2026-03 and says
  so, while the chart uses all 64 rows); a line chart shows a gap where the result has no row for that period and series.
- After the recording, the numeric check was corrected for two formatting artefacts (list ordinals such as "3." and the month part of a date such as 2025-10 that is in a result).
  `python -m demo.retrust` recomputes only the `trust` field of stored traces with the current checks (no model call); `--check` only reports. It changed A1 and A3 and no sealed trace.
