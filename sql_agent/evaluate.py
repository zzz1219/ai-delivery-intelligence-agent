"""S1-S5 evaluator for the SQL agent.

Metrics per question: executed, number of attempts, result correct (strict / loose), answer mentions the key fact.
Scoring follows each question's CONTRACT in evaluation_cases.json (not the question id):
  top1  (S1)        strict = right entity AND right count; loose = right entity
  table (S2,S3,S5)  strict = every expected key present with matching values; loose = strict
  table (S4)        strict = all rows, values right, highest row right; loose = the highest returned row is right
  Only a question whose contract declares a proportion metric (S5) may be answered as a fraction or a percentage.
  The measure column is the only numeric column, else the one whose NAME matches the contract hints; if still
  ambiguous the result is NOT guessed (score_note says why).
Note: with --llm oracle the agent receives the reference SQL, so 100% only proves the plumbing works.

Usage (from the delivery_agent directory):
  python -m sql_agent.evaluate --llm oracle

  Windows PowerShell:
    $env:GEMINI_API_KEY="your-key"
    python -m sql_agent.evaluate --llm gemini --model gemini-3.8-flash
  Linux / macOS:
    GEMINI_API_KEY=your-key python -m sql_agent.evaluate --llm gemini --model gemini-3.8-flash

  (Anthropic: set ANTHROPIC_API_KEY the same way and use --llm anthropic.)

Run lifecycle and result files (eval_results/, a NEW file per run, nothing is ever overwritten):
  * while running, progress is saved after every case to  sql_<model>_<timestamp>_INCOMPLETE.json
  * a fully completed run is renamed to                   sql_<model>_<timestamp>.json
  * an interrupted or incomplete run keeps the _INCOMPLETE name

Error handling (one model per run, never an automatic fallback to another model):
  * provider/transient errors (429, 503, timeouts): the case is recorded as status "error"; two CONSECUTIVE ones
    open a circuit breaker, the remaining cases are recorded as "skipped" (reason circuit_breaker)
  * any other exception (TypeError, SDK change, our own bug) is NOT treated as provider trouble: the run aborts
    at once, the remaining cases are "skipped" (reason aborted_on_runtime_error)
  * "error" and "skipped" cases are never counted in any accuracy denominator; the run is INCOMPLETE
Exit codes: 0 complete, 2 incomplete (provider errors / circuit breaker), 3 aborted on a runtime error, 130 interrupted.
"""
import argparse
import json
import os
import traceback
from datetime import datetime
from pathlib import Path

from .agent import run_agent
from .llm import is_provider_transient
from .tools import DB_PATH

ROOT = Path(__file__).resolve().parent.parent
CASES_PATH = ROOT / "ground_truth" / "evaluation_cases.json"
CIRCUIT_BREAKER_THRESHOLD = 2   # consecutive provider errors


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _find_key_col(columns, rows, gold_keys, subset_ok=False):
    """The key column is the one whose values are the gold keys (all of them, or a subset if subset_ok)."""
    for j in range(len(columns)):
        vals = [r[j] for r in rows]
        found = set(vals)
        if rows and len(found) == len(vals) and (found == gold_keys or (subset_ok and found <= gold_keys)):
            return j
    return None


def _pick_value_col(columns, rows, exclude, hints):
    """The measure column: the only numeric column left, otherwise the single one whose NAME matches a hint.
    Never 'any column that happens to match'; an ambiguous result is reported as ambiguous, not guessed."""
    cands = [j for j in range(len(columns))
             if j not in exclude and rows and all(_is_number(r[j]) for r in rows)]
    if len(cands) == 1:
        return cands[0], None
    if not cands:
        return None, "no numeric value column"
    hinted = [j for j in cands if any(h in columns[j].lower() for h in hints)]
    if len(hinted) == 1:
        return hinted[0], None
    return None, "ambiguous value column"


def _scales(contract):
    """Factors that convert a model value into the gold unit. Only questions whose contract declares a
    proportion metric may be answered as a fraction or as a percentage; no other question gets this leniency."""
    metric = contract.get("metric") or {}
    if metric.get("type") == "proportion" and contract.get("gold_scale") == "percent":
        factors = {"fraction": 100.0, "percent": 1.0}
        return [(name, factors[name]) for name in metric["accepted_scales"]]
    return [("as_is", 1.0)]


def compare(case, columns, rows):
    """Score one result against the question's own contract. Returns dict(strict, loose, reason, scale)."""
    gold, ct = case["gold"], case["contract"]
    out = dict(strict=False, loose=False, reason=None, scale=None)
    if not rows:
        out["reason"] = "empty result"
        return out
    tol = ct.get("tolerance", 0.0)
    if ct["kind"] == "top1":
        # the question asks for ONE entity: strict = right entity and right count, loose = right entity
        ej = next((j for j, v in enumerate(rows[0]) if v == gold["answer"]), None)
        if ej is None:
            out["reason"] = "top entity not found in the first row"
            return out
        out["loose"] = True
        vj, why = _pick_value_col(columns, rows[:1], {ej}, ct["value_hints"])
        if vj is None:
            out["reason"] = why
            return out
        out["strict"] = abs(rows[0][vj] - gold["top_value"]) <= tol
        out["reason"] = None if out["strict"] else "top entity right, value differs"
        return out
    gold_map = {r[0]: r[1] for r in gold["rows"]}
    top_gold = max(gold_map, key=gold_map.get)
    if ct.get("loose_rank"):
        # loose: the highest returned row is the right entity, even if the table is incomplete
        kj = _find_key_col(columns, rows, set(gold_map), subset_ok=True)
        vj = None if kj is None else _pick_value_col(columns, rows, {kj}, ct["value_hints"])[0]
        if kj is not None and vj is not None:
            out["loose"] = max(rows, key=lambda r: r[vj])[kj] == top_gold
    kj = _find_key_col(columns, rows, set(gold_map))
    if kj is None:
        out["reason"] = "incomplete table: not every expected key is present"
        return out
    vj, why = _pick_value_col(columns, rows, {kj}, ct["value_hints"])
    if vj is None:
        out["reason"] = why
        return out
    got = {r[kj]: r[vj] for r in rows}
    for name, factor in _scales(ct):
        if all(abs(got[k] * factor - gold_map[k]) <= tol for k in gold_map):
            out["strict"], out["scale"] = True, name
            break
    if out["strict"] and ct.get("loose_rank"):
        out["strict"] = max(got, key=got.get) == top_gold     # the highest row must also be the right one
    if not out["strict"] and out["reason"] is None:
        out["reason"] = "values differ from the gold answer"
    if not ct.get("loose_rank"):
        out["loose"] = out["strict"]
    return out


def key_fact(case):
    if case["id"] == "S1":
        return case["gold"]["answer"]
    if case["id"] == "S4":
        return "distributed"
    return None


def run_case(case, llm, db_path=DB_PATH):
    try:
        state = run_agent(case["question"], llm, db_path)
    except Exception as e:  # noqa: BLE001 - classified below; KeyboardInterrupt is not an Exception
        provider = is_provider_transient(e)
        return dict(id=case["id"], question=case["question"], status="error",
                    error_class="provider" if provider else "runtime",
                    attempts=None, executed=False, strict=False, loose=False, answer_has_key=None, sql=None,
                    answer=None, error_type=type(e).__name__, error_message=str(e)[:500],
                    error_traceback=None if provider else traceback.format_exc()[-2000:],
                    note="the evaluator did not receive a completed agent state")
    res = state.get("result") or {}
    ok = bool(res.get("ok"))
    columns, rows = (res.get("columns", []), res.get("rows", [])) if ok else ([], [])
    score = compare(case, columns, rows)
    fact = key_fact(case)
    return dict(id=case["id"], question=case["question"], status=state["status"], error_class=None,
                attempts=state["attempts"], executed=ok, strict=score["strict"], loose=score["loose"],
                score_note=score["reason"], scale_used=score["scale"],
                answer_has_key=(None if fact is None else fact.lower() in state["answer"].lower()),
                sql=state.get("sql"), answer=state["answer"],
                result_columns=columns, result_row_count=len(rows), result_rows=rows[:20])


def skipped_record(case, reason):
    return dict(id=case["id"], question=case["question"], status="skipped", reason=reason)


def summarize(results):
    """Only cases the agent actually answered or failed ("ok" / "failed") enter the denominators.
    "error" and "skipped" cases make the run incomplete and are never counted as wrong answers."""
    scored = [r for r in results if r["status"] in ("ok", "failed")]
    bad = [r for r in results if r["status"] == "error"]
    skipped = [r for r in results if r["status"] == "skipped"]
    first = f"{bad[0].get('error_type', 'Error')}: {bad[0].get('error_message', '')}" if bad else None
    return dict(n_total=len(results), n_scored=len(scored),
                executed=sum(r["executed"] for r in scored), strict=sum(r["strict"] for r in scored),
                loose=sum(r["loose"] for r in scored), errors=[r["id"] for r in bad],
                skipped=[r["id"] for r in skipped], first_error=first, complete=not bad and not skipped)


def result_path(llm_name, complete, now=None, out_dir=None):
    """A new file per run, so a later failed run can never overwrite an earlier good baseline."""
    now = now or datetime.now()
    safe = llm_name.replace(":", "_").replace("/", "_")
    suffix = "" if complete else "_INCOMPLETE"
    return Path(out_dir or ROOT / "eval_results") / f"sql_{safe}_{now:%Y%m%d_%H%M%S}{suffix}.json"


def _write_atomic(path, payload):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)   # a Ctrl+C can never leave a half-written result file


def run_evaluation(cases, llm, db_path=DB_PATH, out_dir=None, now=None, note="real model run",
                   threshold=CIRCUIT_BREAKER_THRESHOLD, benchmark_version=None):
    """Run all cases with ONE model. Returns (path, results, summary, exit_code)."""
    out_dir = Path(out_dir or ROOT / "eval_results")
    out_dir.mkdir(parents=True, exist_ok=True)
    started = now or datetime.now()
    name = getattr(llm, "name", "llm")
    progress = result_path(name, False, started, out_dir)
    final = result_path(name, True, started, out_dir)
    results, consecutive, stop_reason = [], 0, None

    def save(state, path):
        sm = summarize(results)
        meta = dict(llm=name, benchmark_version=benchmark_version, started_at=started.isoformat(timespec="seconds"),
                    state=state, note=note,
                    complete=sm["complete"] and state == "complete", errors=sm["errors"],
                    skipped=sm["skipped"], first_error=sm["first_error"])
        _write_atomic(path, dict(meta=meta, results=results))

    try:
        for idx, case in enumerate(cases):
            r = run_case(case, llm, db_path)
            results.append(r)
            if r["status"] == "error" and r["error_class"] == "provider":
                consecutive += 1
                if consecutive >= threshold:
                    stop_reason = "circuit_breaker"
            elif r["status"] == "error":
                stop_reason = "aborted_on_runtime_error"
            else:
                consecutive = 0
            if stop_reason:
                results.extend(skipped_record(c, stop_reason) for c in cases[idx + 1:])
            save("running", progress)
            if stop_reason:
                break
    except KeyboardInterrupt:
        save("interrupted", progress)
        raise
    sm = summarize(results)
    if sm["complete"]:
        save("complete", final)
        progress.unlink(missing_ok=True)
        return final, results, sm, 0
    aborted = stop_reason == "aborted_on_runtime_error"
    save("aborted" if aborted else "incomplete", progress)
    return progress, results, sm, 3 if aborted else 2


def load_benchmark_version(path=CASES_PATH):
    return json.loads(Path(path).read_text(encoding="utf-8")).get("benchmark_version")


def load_sql_cases(path=CASES_PATH):
    return [c for c in json.loads(Path(path).read_text(encoding="utf-8"))["cases"] if c["type"] == "sql"]


def make_llm(kind, cases, model=None):
    if kind == "oracle":
        import generate_data as g
        from .llm import OracleLLM
        return OracleLLM({c["question"]: g.REFERENCE_SQL[c["reference_sql"][0]] for c in cases})
    if kind == "gemini":
        from .llm import GeminiLLM
        return GeminiLLM(model=model)
    from .llm import AnthropicLLM
    return AnthropicLLM(model=model)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", choices=["oracle", "anthropic", "gemini"], default="oracle")
    ap.add_argument("--model", default=None, help="model id (anthropic: default $ANTHROPIC_MODEL; gemini: required or $GEMINI_MODEL)")
    args = ap.parse_args()
    cases = load_sql_cases()
    llm = make_llm(args.llm, cases, args.model)
    note = "oracle = plumbing check only" if args.llm == "oracle" else "real model run"
    try:
        path, results, sm, code = run_evaluation(cases, llm, note=note, benchmark_version=load_benchmark_version())
    except KeyboardInterrupt:
        print("Interrupted. Finished cases were saved to the *_INCOMPLETE.json file in eval_results/.")
        raise SystemExit(130)
    print(f"LLM: {getattr(llm, 'name', args.llm)}   benchmark v{load_benchmark_version()}")
    print(f"{'id':<4}{'status':<9}{'exec':<6}{'tries':<7}{'strict':<8}{'loose':<7}{'answer_has_key'}")
    for r in results:
        if r["status"] in ("ok", "failed"):
            print(f"{r['id']:<4}{r['status']:<9}{str(r['executed']):<6}{r['attempts']:<7}{str(r['strict']):<8}"
                  f"{str(r['loose']):<7}{r['answer_has_key']}")
        else:
            print(f"{r['id']:<4}{r['status']:<9}-     -      -       -      {r.get('error_class') or r.get('reason')}")
    print(f"scored {sm['n_scored']}/{sm['n_total']}: executed {sm['executed']}  strict {sm['strict']}  loose {sm['loose']}")
    if not sm["complete"]:
        print(f"WARNING: INCOMPLETE run. errors={sm['errors']} skipped={sm['skipped']}\n"
              f"First error: {sm['first_error']}")
        if code == 3:
            print("A runtime (non-provider) error aborted the run: this is a code/SDK problem, not model overload. "
                  "See error_traceback in the result file.")
        print("Do not report these numbers as agent accuracy. Rerun, or switch model for the WHOLE run "
              "(only for availability reasons, never because of the score).")
    print(f"Result file: {path}")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
