"""Trace -> view model: every decision about WHAT the page shows lives here (pure Python, tested). The Streamlit file only draws it.

Wording rules enforced by tests: the checks are called "Automated evidence checks", never "verified" or "correct", and always carry the
disclaimer that they verify evidence consistency, not the semantic correctness of the SQL or the reasoning.
"""
import re

from analytics.export_bi import FOOTER
from rag import evaluate_hybrid as ev
from viz import materialize as vmat
from viz import select as vsel

TRUST_TITLE = "Automated evidence checks"
TRUST_DISCLAIMER = "These checks verify evidence consistency, not semantic correctness of the underlying SQL or reasoning."
VIZ_LABEL = "Visualization selected by deterministic rules (no model involved)"
ORACLE_LABEL = "Diagnostic only: documents retrieved by the benchmark's standard queries. They were never shown to the model."
ATTRIBUTION_NOTE = ("The frozen attribution is the sealed contract. The post-hoc, result-based view separates a flawed SQL method from a wrong SQL result "
                    "and does not replace it.")
SCREENING_NOTE = "Questions are screened for basic API-key patterns before they are sent or stored. This is not a complete secret-detection system."
KIND_LABEL = {"sealed_evaluation": "Sealed evaluation replay", "recorded_live": "Recorded run (frozen pipeline)"}
ROWS_SHOWN = 50
PALETTE = ["#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9", "#999999", "#000000"]      # colour-blind-safe (Okabe-Ito), adjacent hues far apart
GAP_NOTE = "Gaps indicate period-and-series combinations with no row in the SQL result; they are not imputed as zero."


def fmt_value(v, unit=""):
    if isinstance(v, bool):
        s = str(v)
    elif isinstance(v, float):
        s = f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 1e6 else f"{v:,.0f}"
    elif isinstance(v, int):
        s = f"{v:,}"
    else:
        s = str(v)
    return f"{s} {unit}".strip()


def axis_title(name, unit):
    """The measure's name, plus its unit only when the name does not already say it (avoids 'total_incidents (incidents)')."""
    return f"{name} ({unit})" if unit and unit.lower() not in name.lower().replace("_", " ") else name


def humanize(name):
    """'closed_incident_count' -> 'Closed incident count'; display only, never used to look anything up."""
    t = " ".join("SQL" if w.lower() == "sql" else w for w in str(name).replace("_", " ").split())
    return t[:1].upper() + t[1:]


_ABBREV = {"avg": "average"}


def _measure_words(col, unit):
    w = [_ABBREV.get(x, x) for x in col.lower().split("_")]
    if unit and len(w) > 2 and w[-1] == unit.lower():
        w.pop()                                              # the unit is already on the axis (kept when it would leave one word, e.g. 'total incidents')
    if len(w) > 1 and w[-1] == "count" and w[-2].endswith("s") and not w[-2].endswith("ss"):
        w[-2] = w[-2][:-1]                                   # 'open_incidents_count' -> 'open incident count'
    return " ".join(w)


def _dimension_words(col):
    w = col.lower().split("_")
    if len(w) > 1 and w[-1] in ("id", "name"):
        w.pop()                                              # 'component_name' -> 'component'
    return " ".join(w)


def chart_title(spec):
    """A short title built only from the column names the spec references (never from model text or from values)."""
    k = spec["kind"]
    if k in ("bar", "line"):
        t = f"{_measure_words(spec['y'], spec.get('unit', ''))} by {_dimension_words(spec['x'])}"
        t += f" and {_dimension_words(spec['series'])}" if spec.get("series") else ""
        return t[:1].upper() + t[1:]
    return "Key figures" if k == "kpi" else "Result"


def split_first_sentence(text, min_chars=60):
    """The lead of a case note: whole sentences until it is at least min_chars long (so a three-word opener such as 'SQL semantic failure.'
    is never shown alone), and the rest. lead + ' ' + rest always equals the note."""
    sents = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text.strip())
    k = 1
    while k < len(sents) and len(" ".join(sents[:k])) < min_chars:
        k += 1
    return " ".join(sents[:k]), " ".join(sents[k:])


def count_text(n, singular, plural=None):
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def chart_for(model):
    """A Plotly-ready dict {data, layout} for bar and line models; None for KPI, table and empty. Pure data, no plotting library needed here."""
    kind, unit = model["kind"], model.get("unit", "")
    if kind == "bar":
        s = model["series"][0]
        ytitle = axis_title(s["name"], unit)
        if model.get("orientation") == "h":
            return dict(data=[dict(type="bar", orientation="h", x=s["y"], y=model["x"], name=s["name"])],
                        layout=dict(xaxis=dict(title=ytitle), yaxis=dict(autorange="reversed", type="category"), showlegend=False,
                                    margin=dict(l=10, r=10, t=10, b=10)))
        return dict(data=[dict(type="bar", x=model["x"], y=s["y"], name=s["name"])],
                    layout=dict(xaxis=dict(type="category", categoryorder="array", categoryarray=model["x"]), yaxis=dict(title=ytitle), showlegend=False,
                                margin=dict(l=10, r=10, t=10, b=10)))
    if kind == "line":
        multi = len(model["series"]) > 1
        data = [dict(type="scatter", mode="lines+markers", x=model["x"], y=s_["y"], name=s_["name"], connectgaps=False,
                     line=dict(width=2), marker=dict(size=6)) for s_ in model["series"]]               # every series gets the same visual weight
        return dict(data=data,
                    layout=dict(colorway=PALETTE, xaxis=dict(type="category", categoryorder="array", categoryarray=model["x"]),
                                yaxis=dict(title=axis_title(model["series"][0]["name"], unit) if not multi else (unit or "")),
                                showlegend=multi, margin=dict(l=10, r=10, t=10, b=10)))
    return None


def _viz_view(spec, task):
    model = vmat.materialize(spec, task)
    out = dict(kind=spec["kind"], reason=spec.get("reason", ""), label=VIZ_LABEL, fidelity_ok=not vmat.check_fidelity(model, task, spec) if task["status"] == "ok" else True,
               kpis=[], chart=chart_for(model), unit=spec.get("unit", ""), title=chart_title(spec),
               gap_note=GAP_NOTE if spec["kind"] == "line" and any(v is None for s_ in model["series"] for v in s_["y"]) else "")
    for k in model["kpis"]:
        out["kpis"].append(dict(label=k["label"], value=k["value"], text=fmt_value(k["value"], vsel.unit_of(k["column"]))))
    return out


def _checks(trace, analysis):
    t = trace["trust"]
    items = []
    items.append(dict(label="Plan parsed and valid", state="pass" if t["plan_valid"] else "fail",
                      detail="" if t["plan_valid"] else "the planner reply was not a valid plan; no later stage ran"))
    total, ok = t["sql_tasks_total"], t["sql_tasks_ok"]
    items.append(dict(label="SQL tasks completed", state="info" if total == 0 else "pass" if ok == total else "warn", detail=f"{ok} of {total}"))
    bad = t["invalid_doc_citations"] + [f"sql:{s}" for s in t["invalid_sql_citations"]]
    if bad:
        items.append(dict(label="Citations resolve to displayed evidence", state="fail", detail="unresolved: " + ", ".join(bad)))
    elif trace["answer"] and t["citation_marks"] == 0:
        items.append(dict(label="Citations resolve to displayed evidence", state="warn", detail="the answer contains no citations"))
    else:
        items.append(dict(label="Citations resolve to displayed evidence", state="pass" if trace["answer"] else "info", detail=count_text(t["citation_marks"], "citation mark")))
    nums = t["numbers_not_in_any_result"]
    items.append(dict(label="No unsupported numeric values detected", state="warn" if nums else "pass" if trace["answer"] else "info",
                      detail=("numbers not found in any SQL result or document: " + ", ".join(nums)) if nums else ""))
    if analysis:
        items.append(dict(label="Visualization is derived from recorded SQL rows", state="pass" if all(a["viz"]["fidelity_ok"] for a in analysis) else "fail", detail=""))
    else:
        items.append(dict(label="Visualization is derived from recorded SQL rows", state="info", detail="no SQL task"))
    rs = trace["retrieval"]["status"]
    detail = f"{count_text(len(trace['retrieval']['queries']), 'query', 'queries')}, {count_text(t['documents_in_context'], 'document')}"
    items.append(dict(label="Retrieval queries accepted", state="warn" if rs == "failed_validation" else "info" if rs in ("not_requested", None) else "pass",
                      detail="no valid query could be produced" if rs == "failed_validation" else "retrieval was not requested" if rs in ("not_requested", None) else detail))
    n = {k: sum(i["state"] == k for i in items) for k in ("pass", "warn", "fail", "info")}
    summary = f"{n['pass']} passed · {count_text(n['warn'], 'warning')} · {n['fail']} failed · {n['info']} not applicable"
    return dict(title=TRUST_TITLE, items=items, summary=summary, disclaimer=TRUST_DISCLAIMER)


def _evaluation(trace):
    e = trace.get("evaluation")
    if not e:
        return None
    sqlf, docf, reqs = e["sql_facts"], e["doc_facts"], e["requirements"]
    seen = set(trace["retrieval"]["context_ids"])
    yn = lambda b: "yes" if b else "no"
    frac = lambda k, n: f"{k} of {n}" if n else "n/a"                 # a rubric item that does not exist for this question is not a poor score
    short, rest = split_first_sentence(e["case_note"])
    return dict(
        scoring=e["scoring"], provenance=e["provenance"], case_note=e["case_note"], case_note_short=short, case_note_rest=rest, attribution_note=ATTRIBUTION_NOTE,
        counts=dict(sql_confirmed=frac(sum(f["human_sql_ok"] for f in sqlf), len(sqlf)), answer_states=frac(sum(f["answer_states_it"] for f in sqlf), len(sqlf)),
                    documents_covered=frac(sum(d["covered"] for d in docf), len(docf)), requirements_met=frac(sum(r["met"] for r in reqs), len(reqs)),
                    unsupported_claims=e["unsupported_claims"]),
        sql_facts=[dict(id=f["id"], sql_ok=yn(f["human_sql_ok"]), answer_states_it=yn(f["answer_states_it"]), machine_matched_task=f["machine_matched_task"] or "-",
                        statement=f["statement"]) for f in sqlf],
        doc_facts=[dict(id=d["id"], covered=yn(d["covered"]), frozen_attribution=d["frozen_attribution"], posthoc_attribution=d["posthoc_attribution"],
                        in_model_context=yn(d["evidence_in_agent_context"]), in_standard_query_context=yn(d["evidence_in_oracle_context"]),
                        frozen_label=humanize(d["frozen_attribution"]), posthoc_label=humanize(d["posthoc_attribution"]),
                        statement=d["statement"]) for d in docf],
        requirements=[dict(id=r["id"], met=yn(r["met"]), statement=r["statement"]) for r in reqs],
        diagnostic=dict(label=ORACLE_LABEL, documents_not_shown_to_the_model=[d for d in e["oracle_context_ids"] if d not in seen]),
        route_compliant=yn(e["route_compliant"]))


def view(trace):
    cited = set(ev.DOC_CITE.findall(trace["answer"] or ""))
    analysis = []
    for t, spec in zip(trace["tasks"], trace["viz"]):
        analysis.append(dict(task_id=t["task_id"], description=t["description"], status=t["status"], error=t.get("error"), attempts=t.get("attempts"), sql=t.get("sql"),
                             row_count=t["row_count"], columns=t["columns"], rows=t["rows"][:ROWS_SHOWN], truncated=t["row_count"] > ROWS_SHOWN,
                             viz=_viz_view(spec, t)))
    plan = trace["plan"]
    return dict(
        header=dict(title=trace["title"], kind_label=KIND_LABEL[trace["kind"]], model=trace["model"], recorded_at=trace["recorded_at"], question=trace["question"],
                    status=trace["status"], role=trace.get("role"),
                    specs={k: v[:12] for k, v in trace["specs"].items()}),
        plan=None if plan is None else dict(
            flags=[("Needs SQL", plan["requires_sql"]), ("Needs documents", plan["requires_retrieval"]), ("Documents depend on SQL results", plan["retrieval_depends_on_sql"])],
            tasks=[dict(task_id=t["task_id"], description=t["description"]) for t in plan["sql_tasks"]], retrieval_purpose=plan.get("retrieval_purpose", "")),
        analysis=analysis,
        evidence=dict(status=trace["retrieval"]["status"], purpose=trace["retrieval"]["purpose"], queries=trace["retrieval"]["queries"],
                      documents=[dict(d, cited=d["doc_id"] in cited) for d in trace["retrieval"]["documents"]],
                      empty_note="No documents were retrieved for this question." if not trace["retrieval"]["documents"] else ""),
        answer=dict(text=trace["answer"], findings=[dict(text=f["text"], task_links=f["sql_tasks"], doc_links=f["docs"]) for f in trace["findings"]]),
        checks=_checks(trace, analysis), evaluation=_evaluation(trace), footer=FOOTER, screening_note=SCREENING_NOTE)
