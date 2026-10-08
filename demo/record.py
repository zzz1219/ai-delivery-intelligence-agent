"""Record demo questions with the frozen pipeline and ONE model (needs GEMINI_API_KEY). Windows PowerShell:

    $env:GEMINI_API_KEY="your-key"
    python -m demo.record --model gemini-3.5-flash-lite

Writes traces/<id>.json (trace-1.0, kind recorded_live) and rebuilds traces/index.json. Existing traces are NEVER overwritten unless --force,
and then the old file is moved to traces/_superseded/ (so a re-recording is visible, not silent). Two consecutive provider errors stop the run.
Each trace is validated before it is written; an invalid trace is reported and not written.
"""
import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sql_agent.llm import GeminiLLM, is_provider_transient  # noqa: E402

from .engine import Engine  # noqa: E402
from .gallery import rebuild_index  # noqa: E402
from .trace import dump_json, validate_trace  # noqa: E402

TRACES = ROOT / "traces"
QUESTIONS = ROOT / "demo" / "questions.json"


def record_questions(engine, llm, questions, out_dir=TRACES, force=False, now=None, threshold=2):
    """Returns a list of (id, outcome, detail). Outcomes: written, exists, invalid, plan_failed_written, provider_error, skipped."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    results, consecutive, stop = [], 0, False
    for q in questions:
        if stop:
            results.append((q["id"], "skipped", "stopped after consecutive provider errors"))
            continue
        path = out / f"{q['id']}.json"
        if path.exists() and not force:
            results.append((q["id"], "exists", "kept the existing trace (use --force to re-record; the old one is moved to _superseded)"))
            continue
        try:
            trace = engine.answer(q["question"], llm, trace_id=q["id"], title=q.get("title"), role=q.get("role"), now=now)
            consecutive = 0
        except Exception as e:  # noqa: BLE001
            if not is_provider_transient(e):
                raise
            consecutive += 1
            stop = consecutive >= threshold
            results.append((q["id"], "provider_error", f"{type(e).__name__}: {str(e)[:160]}"))
            continue
        problems = validate_trace(trace, engine.docs)
        if problems:
            results.append((q["id"], "invalid", "; ".join(problems)[:300]))
            continue
        if path.exists():
            (out / "_superseded").mkdir(exist_ok=True)
            shutil.move(str(path), str(out / "_superseded" / f"{q['id']}_{(now or datetime.now()):%Y%m%d_%H%M%S}.json"))
        path.write_bytes(dump_json(trace))
        kinds = [v["kind"] for v in trace["viz"]]
        hint = f", expected {q['expect_viz']}" if q.get("expect_viz") else ""
        results.append((q["id"], "written" if trace["status"] == "ok" else "plan_failed_written", f"status {trace['status']}, viz {kinds}{hint}"))
    rebuild_index(out)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="gemini-3.5-flash-lite")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--only", nargs="*", help="record only these ids")
    args = ap.parse_args()
    engine = Engine()
    registered = engine.pspec["models"]["all_stages"]
    if args.model != registered:
        print(f"note: the evaluated pipeline used {registered}; recording with {args.model} is shown in each trace's model field")
    qs = json.loads(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    if args.only:
        qs = [q for q in qs if q["id"] in args.only]
    for qid, outcome, detail in record_questions(engine, GeminiLLM(model=args.model), qs, force=args.force):
        print(f"{qid:<4}{outcome:<20}{detail}")


if __name__ == "__main__":
    main()
