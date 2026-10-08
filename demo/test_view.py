"""Plain-assert tests for the view model, the live-mode helpers and a static check of the Streamlit file (which cannot be executed in the build sandbox).
Run from the delivery_agent folder:  python -m demo.test_view"""
import ast
import json
import os
import re
from pathlib import Path

from . import live, view_model as vm
from .test_demo import ENGINE, FakeFreeLLM, Q_MAP, Q_ROOT, Q_TREND, _answer

ROOT = Path(__file__).resolve().parent.parent
TRACES = {i: json.loads((ROOT / "traces" / f"{i}.json").read_text(encoding="utf-8")) for i in ("H1", "H2", "H3", "H4", "H5")}
ATRACES = {i: json.loads((ROOT / "traces" / f"{i}.json").read_text(encoding="utf-8")) for i in ("A1", "A2", "A3", "A4")}
LABELS = ["Plan parsed and valid", "SQL tasks completed", "Citations resolve to displayed evidence", "No unsupported numeric values detected",
          "Visualization is derived from recorded SQL rows", "Retrieval queries accepted"]


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _strings(v)


def test_every_committed_trace_has_a_complete_view():
    for i, t in TRACES.items():
        v = vm.view(t)
        assert v["header"]["kind_label"] == "Sealed evaluation replay" and v["header"]["question"] == t["question"] and v["footer"].startswith("Synthetic portfolio dataset")
        assert [a["task_id"] for a in v["analysis"]] == [x["task_id"] for x in t["tasks"]] and all(a["viz"]["fidelity_ok"] for a in v["analysis"])
        assert v["plan"]["flags"] == [("Needs SQL", True), ("Needs documents", True), ("Documents depend on SQL results", True)]
        assert v["evaluation"] is not None and v["evaluation"]["diagnostic"]["label"] == vm.ORACLE_LABEL
        task_ids = {a["task_id"] for a in v["analysis"]}
        docs = {d["doc_id"] for d in v["evidence"]["documents"]}
        for f in v["answer"]["findings"]:                                    # every finding links to evidence that exists on the page
            assert set(f["task_links"]) <= task_ids and set(f["doc_links"]) <= docs
    h5 = vm.view(TRACES["H5"])
    assert {d["doc_id"]: d["cited"] for d in h5["evidence"]["documents"] if d["lane"] == "cases"} == {"historical_cases/INC_0028": False, "historical_cases/INC_0016": False,
                                                                                                        "historical_cases/INC_0049": True}
    assert h5["evaluation"]["diagnostic"]["documents_not_shown_to_the_model"][0] == "historical_cases/INC_0026"      # the real INC_0185 precedent, never seen by the model
    assert [d["posthoc_attribution"] for d in h5["evaluation"]["doc_facts"]] == ["query_formulation_failure", "covered"]
    assert [d["frozen_attribution"] for d in h5["evaluation"]["doc_facts"]] == ["propagated_from_sql", "propagated_from_sql"]


def test_evidence_checks_are_worded_as_consistency_checks_and_cannot_see_a_wrong_sql():
    for i, t in TRACES.items():
        c = vm.view(t)["checks"]
        assert c["title"] == "Automated evidence checks" and [x["label"] for x in c["items"]] == LABELS
        assert c["disclaimer"] == "These checks verify evidence consistency, not semantic correctness of the underlying SQL or reasoning."
        text = " ".join(_strings(c)).lower()
        assert not re.search(r"verified|guarantee|correct answer|validated|\bsafe\b", text.replace("not semantic correctness", "")), i
    h2 = vm.view(TRACES["H2"])
    assert [x["state"] for x in h2["checks"]["items"]] == ["pass", "pass", "pass", "pass", "pass", "pass"]      # the sealed H2 answer is WRONG, yet every automated check passes
    assert h2["evaluation"]["doc_facts"][0]["frozen_attribution"] == "propagated_from_sql" and "pairs" in h2["evaluation"]["case_note"]


def test_charts_kpis_and_tables_are_drawn_from_the_recorded_rows():
    h1 = vm.view(TRACES["H1"])
    t1, t2 = h1["analysis"]
    assert t1["viz"]["kind"] == "bar" and t1["viz"]["chart"]["data"][0]["type"] == "bar" and t1["viz"]["chart"]["data"][0]["x"] == ["database", "non-database"]
    assert [k["text"] for k in t2["viz"]["kpis"]] == ["41.07 hours"] and t2["viz"]["kpis"][0]["label"] == "connection_pool_exhausted" and t2["viz"]["chart"] is None
    h3 = vm.view(TRACES["H3"])["analysis"][0]["viz"]["kpis"]
    assert [k["text"] for k in h3] == ["35.44 hours", "8 incidents"]
    h4 = vm.view(TRACES["H4"])["analysis"][0]["viz"]["chart"]
    assert h4["data"][0]["x"] == ["2025 Q4", "2026 Q1", "2026 Q2"] and h4["data"][0]["y"] == [11, 40, 11] and h4["layout"]["xaxis"]["categoryarray"] == ["2025 Q4", "2026 Q1", "2026 Q2"]
    h5 = vm.view(TRACES["H5"])["analysis"]
    assert h5[1]["viz"]["kind"] == "table" and h5[1]["viz"]["chart"] is None and h5[1]["row_count"] == 4 and not h5[1]["truncated"]
    assert all(a["viz"]["label"] == "Visualization selected by deterministic rules (no model involved)" for a in h5)
    assert vm.fmt_value(41.07333) == "41.07" and vm.fmt_value(8, "incidents") == "8 incidents" and vm.fmt_value(2.0) == "2" and vm.fmt_value(1234567.0) == "1,234,567"


def test_live_style_traces_have_horizontal_bars_multi_series_lines_and_no_evaluation():
    _, t = _answer(Q_ROOT)
    v = vm.view(t)
    ch = v["analysis"][0]["viz"]["chart"]
    assert v["evaluation"] is None and v["header"]["kind_label"] == "Recorded run (frozen pipeline)"
    assert ch["data"][0]["orientation"] == "h" and ch["layout"]["yaxis"]["autorange"] == "reversed" and ch["data"][0]["x"] == sorted(ch["data"][0]["x"], reverse=True)
    assert v["evidence"]["empty_note"] and v["checks"]["items"][5]["state"] == "info"                    # retrieval was not requested: informative, not a warning
    _, trend = _answer(Q_TREND)
    line = vm.view(trend)["analysis"][0]["viz"]["chart"]
    assert len(line["data"]) == 6 and all(d["type"] == "scatter" and d["connectgaps"] is False for d in line["data"]) and line["layout"]["showlegend"] is True
    assert line["layout"]["xaxis"]["categoryarray"] == sorted(line["layout"]["xaxis"]["categoryarray"]) and len(line["layout"]["xaxis"]["categoryarray"]) == 12
    _, hyb = _answer(Q_MAP)
    hv = vm.view(hyb)
    assert hv["evidence"]["documents"] and any(d["cited"] for d in hv["evidence"]["documents"]) and hv["checks"]["items"][5]["state"] == "pass"


def test_wording_axis_titles_and_table_column_order():
    assert vm.axis_title("total_incidents", "incidents") == "total_incidents" and vm.axis_title("avg_resolution_hours", "hours") == "avg_resolution_hours"
    assert vm.axis_title("mean_value", "hours") == "mean_value (hours)" and vm.axis_title("n", "") == "n"
    assert [vm.count_text(n, "query", "queries") for n in (0, 1, 2)] == ["0 queries", "1 query", "2 queries"] and vm.count_text(8, "citation mark") == "8 citation marks"
    h4 = vm.view(TRACES["H4"])["analysis"][0]["viz"]["chart"]
    assert h4["layout"]["yaxis"]["title"] == "total_incidents"                                       # the screenshot showed 'total_incidents (incidents)'
    for t in TRACES.values():
        e = vm.view(t)["evaluation"]
        assert list(e["doc_facts"][0])[:4] == ["id", "covered", "frozen_attribution", "posthoc_attribution"] and list(e["doc_facts"][0])[-1] == "statement"
        assert list(e["sql_facts"][0])[0] == "id" and list(e["sql_facts"][0])[-1] == "statement" and list(e["requirements"][0])[-1] == "statement" if e["requirements"] else True
        text = " ".join(_strings(vm.view(t)["checks"]))
        assert "(s)" not in text and "(ies)" not in text
    for t in TRACES.values():
        assert not any(w in t["title"] for w in ("Hybrid:", "Hybrid failure", "Hybrid mixed")) and len(t["title"]) <= 50          # short enough for the dropdown


def test_checks_report_problems_instead_of_hiding_them():
    _, bad = _answer(Q_ROOT, FakeFreeLLM(garbage_plan=True))
    v = vm.view(bad)
    states = {x["label"]: x["state"] for x in v["checks"]["items"]}
    assert states["Plan parsed and valid"] == "fail" and v["answer"]["text"] is None and v["plan"] is None and v["analysis"] == []
    t = json.loads(json.dumps(TRACES["H1"]))
    t["trust"]["invalid_doc_citations"] = ["historical_cases/INC_0001"]
    t["trust"]["numbers_not_in_any_result"] = ["99.9"]
    states = {x["label"]: (x["state"], x["detail"]) for x in vm.view(t)["checks"]["items"]}
    assert states["Citations resolve to displayed evidence"][0] == "fail" and "INC_0001" in states["Citations resolve to displayed evidence"][1]
    assert states["No unsupported numeric values detected"][0] == "warn" and "99.9" in states["No unsupported numeric values detected"][1]
    t = json.loads(json.dumps(TRACES["H1"]))
    t["tasks"][0]["rows"][0][1] = 12345.6                                               # the table no longer matches what the chart spec was derived from
    t["viz"][0]["kind"] = "bar"
    t["tasks"][0]["status"] = "failed"
    assert vm.view(t)["analysis"][0]["status"] == "failed"
    long = dict(TRACES["H5"]["tasks"][1], rows=[[f"INC_{i:04d}", "P03", "x" * 50, None] for i in range(80)], row_count=80)
    t = json.loads(json.dumps(TRACES["H5"]))
    t["tasks"][1] = long
    t["viz"][1] = dict(t["viz"][1])
    a = vm.view(t)["analysis"][1]
    assert len(a["rows"]) == vm.ROWS_SHOWN and a["truncated"] is True and a["row_count"] == 80


def test_live_mode_helpers_never_touch_the_environment_and_limit_usage():
    seen = {}
    before = dict(os.environ)

    class Dummy:
        pass

    def factory(key):
        seen["key"] = key
        return Dummy()
    llm = live.make_llm("gemini-3.5-flash-lite", "  " + "k" * 30 + "  ", client_factory=factory)
    assert seen["key"] == "k" * 30 and llm.name == "gemini:gemini-3.5-flash-lite" and dict(os.environ) == before
    assert "GEMINI_API_KEY" not in os.environ or os.environ["GEMINI_API_KEY"] == before.get("GEMINI_API_KEY")
    for bad in ("", "short", "has space inside the key 12345678", None, "k" * 201):
        try:
            live.make_llm("m", bad, client_factory=factory)
            raise AssertionError("must refuse: %r" % (bad,))
        except ValueError as e:
            assert "API key" in str(e)
    lim = live.SessionLimiter(max_runs=2, cooldown_s=20)
    assert lim.allow(100.0) == (True, "")
    ok, msg = lim.allow(110.0)
    assert not ok and "10 more second" in msg and lim.runs == 1                                    # a refused attempt does not count
    assert lim.allow(121.0) == (True, "")
    ok, msg = lim.allow(1000.0)
    assert not ok and "2 live runs" in msg and lim.runs == 2


def test_the_streamlit_file_is_a_thin_safe_renderer():
    src = (ROOT / "demo" / "streamlit_app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert {"demo.engine", "demo.live", "demo.view_model", "demo.trace", "streamlit", "plotly.graph_objects", "pandas"} <= imports
    assert not any(m and (m.startswith(("rag.", "viz.", "sql_agent", "analytics")) or m in ("rag", "viz")) for m in imports)      # no business logic imported directly
    assert "unsafe_allow_html" not in src                                                                 # model text is untrusted
    assert "os.environ" not in src and "putenv" not in src and 'type="password"' in src                    # the key never reaches process-wide state
    assert "api_key" not in src.replace("Gemini API key", "") and "session_state[\"key\"]" not in src
    assert sum(isinstance(n, ast.keyword) and n.arg == "use_container_width" for n in ast.walk(tree)) == 1 and 'width="stretch"' in src   # only in the helper's fallback
    assert "set_page_config" in src and "st.cache_resource" in src and "validate_trace" in src and "SessionLimiter" in src
    assert src.count("st.session_state") <= 3                                                             # only the limiter and the live trace live in the session
    assert "Replay (recorded traces)" in src and "Live (your own API key)" in src and "basic API-key patterns" in src


def test_view_text_never_overclaims():
    banned = re.compile(r"\b(verified answer|guaranteed|proven correct|100% accurate)\b", re.I)
    for t in TRACES.values():
        v = vm.view(t)
        assert not banned.search(" ".join(_strings({k: v[k] for k in ("header", "plan", "analysis", "checks")})))
    assert "not a complete secret-detection system" in vm.SCREENING_NOTE


def test_recorded_analytics_traces_have_complete_views_and_clean_checks():
    for i, t in ATRACES.items():
        v = vm.view(t)
        assert [x["label"] for x in v["checks"]["items"]] == LABELS and all(x["state"] in ("pass", "info") for x in v["checks"]["items"]), i
        assert v["checks"]["summary"] == "5 passed · 0 warnings · 0 failed · 1 not applicable", i
        assert v["analysis"][0]["viz"]["fidelity_ok"] and v["evaluation"] is None


def test_chart_titles_come_from_columns_and_stay_short():
    titles = {i: vm.view(t)["analysis"][0]["viz"]["title"] for i, t in ATRACES.items()}
    assert titles == {"A1": "Incident count by month and service category", "A2": "Closed incident count by root cause",
                      "A3": "Average resolution time by component", "A4": "Open incident count by project"}
    assert all(len(x) < 70 and "identify" not in x for x in titles.values())
    assert vm.view(TRACES["H4"])["analysis"][0]["viz"]["title"] == "Total incidents by quarter"
    for t in TRACES.values():
        assert all(len(a["viz"]["title"]) < 70 for a in vm.view(t)["analysis"])


def test_line_chart_uses_a_distinguishable_palette_gives_every_series_equal_weight_and_a_precise_gap_note():
    v = vm.view(ATRACES["A1"])["analysis"][0]["viz"]
    ch = v["chart"]
    assert len(set(ch["layout"]["colorway"])) >= 6 and v["gap_note"] == vm.GAP_NOTE and "not imputed as zero" in v["gap_note"]
    assert {d["line"]["width"] for d in ch["data"]} == {2} and len(ch["data"]) == 6          # the display layer adds no emphasis to any series
    assert vm.view(ATRACES["A2"])["analysis"][0]["viz"]["gap_note"] == ""


def test_evaluation_view_shows_not_applicable_instead_of_zero_of_zero_and_readable_labels():
    h3, h5, h2 = (vm.view(TRACES[i])["evaluation"] for i in ("H3", "H5", "H2"))
    assert h3["counts"]["requirements_met"] == "n/a" and h5["counts"]["requirements_met"] == "2 of 2" and h5["counts"]["sql_confirmed"] == "2 of 3"
    assert [d["frozen_label"] for d in h5["doc_facts"]] == ["Propagated from SQL"] * 2
    assert [d["posthoc_label"] for d in h5["doc_facts"]] == ["Query formulation failure", "Covered"]
    assert h2["case_note_short"].startswith("SQL semantic failure. The planner rewrote") and len(h2["case_note_short"]) >= 60 and " ".join((h5["case_note_short"] + " " + h5["case_note_rest"]).split()) == " ".join(h5["case_note"].split())
    assert h5["doc_facts"][0]["frozen_attribution"] == "propagated_from_sql"           # the raw trace value is untouched; only the label is humanised


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
