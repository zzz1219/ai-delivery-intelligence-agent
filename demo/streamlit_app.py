"""Streamlit page for the AI Delivery Intelligence Agent. It only LOADS a trace and DRAWS its view model; no business rule lives here.

Run from the delivery_agent folder:
    pip install streamlit plotly pandas
    streamlit run demo/streamlit_app.py

Replay mode (default) needs no API key. Live mode runs the frozen pipeline with the visitor's OWN Gemini key (used for the session only).
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import streamlit as st  # noqa: E402

from demo.engine import Engine  # noqa: E402
from demo.live import SessionLimiter, make_llm  # noqa: E402
from demo.trace import MAX_QUESTION, check_question, validate_trace  # noqa: E402
from demo.view_model import view  # noqa: E402

TRACES = ROOT / "traces"
ICON = {"pass": "✅", "warn": "⚠️", "fail": "❌", "info": "ℹ️"}
ROLE_TEXT = {"analytics": "Analytics", "success_example": "Worked", "partial_example": "Partial",
             "failure_case": "Failure case", "mixed_case": "Mixed", None: "Other"}

st.set_page_config(page_title="AI Delivery Intelligence Agent", layout="wide")


@st.cache_resource
def get_engine():
    return Engine()


def stretch(call, *args, **kwargs):
    """Full-width rendering that works on both API generations: width="stretch" (current) and use_container_width=True (older, now deprecated)."""
    try:
        return call(*args, width="stretch", **kwargs)
    except TypeError:
        return call(*args, use_container_width=True, **kwargs)


def load_index():
    return json.loads((TRACES / "index.json").read_text(encoding="utf-8"))


def load_trace(name):
    return json.loads((TRACES / name).read_text(encoding="utf-8"))


def draw_viz(a):
    v = a["viz"]
    if v["kind"] == "kpi":
        cols = st.columns(len(v["kpis"]))
        for c, k in zip(cols, v["kpis"]):
            c.metric(k["label"], k["text"])
    elif v["chart"] is not None:
        stretch(st.plotly_chart, go.Figure(v["chart"]))
    elif v["kind"] == "empty":
        st.info("This SQL task returned no drawable result.")
    st.caption(f"{v['label']}. {v['reason'].rstrip('.')}." + (f" {v['gap_note']}" if v["gap_note"] else ""))


def draw_analysis(view_model):
    if not view_model["analysis"]:
        st.info("No SQL task was run for this question.")
    for a in view_model["analysis"]:
        st.subheader(f"{a['task_id']} · {a['viz']['title']}")
        st.caption(f"SQL task {a['task_id']} · {a['description']}")
        if a["status"] != "ok":
            st.error(f"The SQL task failed: {a['error']}")
            continue
        draw_viz(a)
        with st.expander("Generated SQL"):
            st.code(a["sql"], language="sql")
            st.caption(f"{a['attempts']} attempt(s), {a['row_count']} row(s)")
        with st.expander("Result table"):
            stretch(st.dataframe, pd.DataFrame(a["rows"], columns=a["columns"]), hide_index=True)
            if a["truncated"]:
                st.caption(f"Showing the first {len(a['rows'])} of {a['row_count']} rows.")


def draw_evidence(view_model):
    e = view_model["evidence"]
    if e["queries"]:
        st.markdown("**Retrieval queries written by the model**")
        for q in e["queries"]:
            st.code(q, language=None)
    if e["purpose"]:
        st.caption(f"Purpose: {e['purpose']}")
    if e["empty_note"]:
        st.info(e["empty_note"])
    for d in e["documents"]:
        tag = "cited in the answer" if d["cited"] else "retrieved, not cited"
        with st.expander(f"{d['title']}  ·  {d['lane']}  ·  {d['doc_id']}  ·  {tag}"):
            for k, val in d.get("fields", {}).items():
                st.markdown(f"**{k.replace('_', ' ')}:** {val}")
            for name, text in d["sections"].items():
                st.markdown(f"**{name}**")
                st.write(text)


def draw_answer(view_model):
    a = view_model["answer"]
    if a["text"] is None:
        st.error("No answer: the planner did not produce a valid plan for this question.")
        return
    st.markdown(a["text"])
    if a["findings"]:
        with st.expander(f"Key findings ({len(a['findings'])} cited sentences)"):
            st.caption("Sentences of the answer that carry a citation, shown unchanged.")
            for f in a["findings"]:
                links = [f"SQL task {t}" for t in f["task_links"]] + f["doc_links"]
                st.markdown(f"- {f['text']}")                       # model text is untrusted: plain markdown only, never raw HTML
                st.caption("evidence: " + ", ".join(links))


def draw_checks(view_model):
    c = view_model["checks"]
    st.markdown(f"**{c['title']}**")
    st.caption(c["summary"])
    for i in c["items"]:
        st.markdown(f"{ICON[i['state']]} {i['label']}" + (f" — {i['detail']}" if i["detail"] else ""))
    st.caption(c["disclaimer"])


def _card():
    try:
        return st.container(border=True)
    except TypeError:                                   # older Streamlit without bordered containers
        return st.container()


def draw_evaluation(view_model):
    e = view_model["evaluation"]
    st.markdown(f"**Scoring:** {e['scoring']}. {e['provenance']}.")
    st.info(e["case_note_short"])
    if e["case_note_rest"]:
        with st.expander("Full case note"):
            st.write(e["case_note_rest"])
    c = e["counts"]
    cols = st.columns(5)
    for col, (label, key) in zip(cols, [("SQL facts confirmed", "sql_confirmed"), ("Answer states them", "answer_states"), ("Documents covered", "documents_covered"),
                                        ("Requirements met", "requirements_met"), ("Unsupported claims", "unsupported_claims")]):
        col.metric(label, c[key])
    st.markdown("**SQL facts**")
    for f in e["sql_facts"]:
        with _card():
            st.markdown(f"**{f['id']}** — {f['statement']}")                   # cards, not a table: a long statement wraps instead of being cut off
            for col, (label, value) in zip(st.columns(3), [("SQL method valid", f["sql_ok"]), ("Stated in answer", f["answer_states_it"]),
                                                          ("Matched SQL task", f["machine_matched_task"])]):
                col.caption(label)
                col.markdown(f"**{value}**")
    st.markdown("**Document facts and failure attribution**")
    for d in e["doc_facts"]:
        with _card():
            st.markdown(f"**{d['id']}** — {d['statement']}")
            for col, (label, value) in zip(st.columns(5), [("Covered", d["covered"]), ("Frozen attribution", d["frozen_label"]), ("Post-hoc attribution", d["posthoc_label"]),
                                                          ("In model context", d["in_model_context"]),
                                                          ("In standard queries", d["in_standard_query_context"])]):
                col.caption(label)
                col.markdown(f"**{value}**")
    st.caption(e["attribution_note"])
    if e["requirements"]:
        st.markdown("**Synthesis requirements**")
        for r in e["requirements"]:
            with _card():
                st.markdown(f"**{r['id']}** — {r['statement']}")
                st.caption("Met")
                st.markdown(f"**{r['met']}**")
    d = e["diagnostic"]
    st.markdown("**Diagnostic**")
    st.caption(d["label"])
    if d["documents_not_shown_to_the_model"]:
        for doc_id in d["documents_not_shown_to_the_model"]:
            st.markdown(f"- `{doc_id}`")
    else:
        st.write("No additional documents.")


def draw_trace(trace):
    vm = view(trace)
    h = vm["header"]
    st.header(h["title"])
    st.caption(f"{h['kind_label']} · model {h['model']} · {h['recorded_at']}")
    st.markdown(f"**Question:** {h['question']}")
    names = ["Answer", "Analysis", "Evidence", "Agent plan", "Checks"] + (["Evaluation notes"] if vm["evaluation"] else [])
    tabs = st.tabs(names)
    with tabs[0]:
        draw_answer(vm)
    with tabs[1]:
        draw_analysis(vm)
    with tabs[2]:
        draw_evidence(vm)
    with tabs[3]:
        if vm["plan"] is None:
            st.error("The planner reply was not a valid plan.")
        else:
            for label, flag in vm["plan"]["flags"]:
                st.markdown(f"- {label}: **{'yes' if flag else 'no'}**")
            for t in vm["plan"]["tasks"]:
                st.markdown(f"- SQL task `{t['task_id']}`: {t['description']}")
            if vm["plan"]["retrieval_purpose"]:
                st.caption(f"Retrieval purpose: {vm['plan']['retrieval_purpose']}")
    with tabs[4]:
        draw_checks(vm)
    if vm["evaluation"]:
        with tabs[5]:
            draw_evaluation(vm)
    st.divider()
    st.caption(vm["footer"])


def sidebar_about(vm_hint):
    with st.sidebar.expander("About this demo"):
        st.markdown("Replay shows runs that were recorded with the **frozen** pipeline evaluated in the Hybrid benchmark. Charts are chosen by deterministic rules "
                    "and drawn from the recorded SQL rows; the checks verify evidence consistency, not the correctness of the reasoning.")
        st.caption(vm_hint)


def main():
    index = load_index()
    st.sidebar.title("AI Delivery Intelligence Agent")
    mode = st.sidebar.radio("Mode", ["Replay (recorded traces)", "Live (your own API key)"])
    st.sidebar.caption("Synthetic data. Replay needs no key.")
    if mode.startswith("Replay"):
        options = {f"{ROLE_TEXT.get(e['role'], 'Other')} · {e['title']}": e["file"] for e in index["traces"]}
        if not options:
            st.warning("No traces found.")
            return
        choice = st.sidebar.selectbox("Trace", list(options))
        if index["pending_recordings"]:
            st.sidebar.caption("Not recorded yet: " + ", ".join(p["trace_id"] for p in index["pending_recordings"]))
        trace = load_trace(options[choice])
        problems = validate_trace(trace, get_engine().docs)
        if problems:
            st.error("This trace failed validation and is not shown: " + "; ".join(problems[:3]))
            return
        sidebar_about("; ".join(f"{k}: {v[:10]}" for k, v in trace["specs"].items()))
        draw_trace(trace)
        return
    # live mode
    engine = get_engine()
    st.title("Live run with the frozen pipeline")
    st.caption("Your key is used for this session only: it is passed to the client directly, never written to the environment, a file or a trace.")
    limiter = st.session_state.setdefault("limiter", SessionLimiter(max_runs=5, cooldown_s=20))
    key = st.text_input("Gemini API key", type="password")
    model = st.text_input("Model", value=engine.pspec["models"]["all_stages"])
    question = st.text_area("Question", max_chars=MAX_QUESTION, placeholder="Which projects currently have the most open incidents?")
    st.caption("Questions are screened for basic API-key patterns before they are sent or stored. This is not a complete secret-detection system.")
    if st.button("Run"):
        ok, msg = limiter.allow(time.time())
        if not ok:
            st.warning(msg)
        else:
            try:
                q = check_question(question)
                llm = make_llm(model, key)
                with st.spinner("Running the pipeline (about 6 to 10 model calls)..."):
                    trace = engine.answer(q, llm, trace_id="live", title=q)
                problems = validate_trace(trace, engine.docs)
                if problems:
                    st.error("The run produced an invalid trace: " + "; ".join(problems[:3]))
                else:
                    st.session_state["live_trace"] = trace
            except ValueError as e:
                limiter.runs -= 1
                st.error(str(e))
            except Exception as e:  # noqa: BLE001
                st.error(f"{type(e).__name__}: {str(e)[:300]}")
    trace = st.session_state.get("live_trace")
    if trace:
        st.download_button("Download this trace (JSON)", json.dumps(trace, ensure_ascii=False, indent=1), file_name="live_trace.json", mime="application/json")
        draw_trace(trace)


main()
