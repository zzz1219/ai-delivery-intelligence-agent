"""SECONDARY evaluation: a frozen LLM judge scores the same 15 answers and is compared with the human rubric.
The judge is never the source of truth. One call per answer, the prompt is frozen in the generation spec.

  python -m rag.judge --run eval_results\\gen_<...>.json                  # writes judge_<run>.csv (+ .json)
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sql_agent.llm import GeminiLLM, is_provider_transient  # noqa: E402
from .build_benchmark import load_frozen  # noqa: E402
from .build_generation_benchmark import BENCH_PATH, HASH_PATH  # noqa: E402
from .corpus import load_corpus  # noqa: E402
from .evaluate_generation import COUNT_ITEM, checklist, load_run  # noqa: E402
from .generate import build_context  # noqa: E402


def parse_judgement(raw, items):
    """Extract and validate the judge's JSON. Returns (scores_or_None, error_or_None)."""
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None, "no JSON object in the reply"
    try:
        obj = json.loads(m.group(0))
        given = obj["items"]
        out = {}
        for item_id, _, kind in items:
            if item_id == COUNT_ITEM:
                continue
            v = int(given[item_id])
            if v not in (0, 1):
                return None, f"item {item_id} must be 0 or 1"
            out[item_id] = v
        n = int(obj[COUNT_ITEM])
        if n < 0:
            return None, "unsupported_claims must be >= 0"
        out[COUNT_ITEM] = n
        return out, None
    except (KeyError, ValueError, TypeError) as e:
        return None, f"invalid judge JSON: {type(e).__name__}: {e}"


def judge_run(spec, run, corpus, llm, done=None, on_question=None):
    """Judge every answer (one call, one retry on invalid JSON). `done` = already judged questions (resume);
    `on_question(qid, rows, log_entry)` is called after every answer (checkpoint). A provider error (quota, overload)
    stops the loop cleanly. Returns (rows, log, interrupted_message_or_None)."""
    done = done or {}
    reg = spec["pre_registered"]["judge"]
    gen = spec["pre_registered"]["generator"]
    text = {d.doc_id: d.text for d in corpus}
    answers = {a["id"]: a for a in run["answers"]}
    rows, log, interrupted = [], [], None
    for q in spec["questions"]:
        if q["id"] in done:
            rows += done[q["id"]]["rows"]
            log.append(done[q["id"]]["log"])
            continue
        items = checklist(q)
        a = answers[q["id"]]
        user = reg["user_template"].format(
            question=q["question"], context=build_context(a["context_ids"], text, gen["doc_block"]), answer=a["answer"],
            checklist="\n".join(f"{i}: {t}" for i, t, k in items if i != COUNT_ITEM))
        scores, err = None, None
        try:
            for attempt in range(2):                                # one retry when the reply is not valid JSON
                raw = llm.complete("judge", reg["system"], user if attempt == 0 else user + "\n\nReturn ONLY the JSON object.")
                scores, err = parse_judgement(raw, items)
                if scores:
                    break
        except Exception as e:  # noqa: BLE001
            if not is_provider_transient(e):
                raise
            interrupted = f"{type(e).__name__}: {str(e)[:300]}"
            break
        entry = dict(question_id=q["id"], error=err, raw=raw if err else None)
        q_rows = [dict(question_id=q["id"], item_id=i, item_text=t, judge_score="" if scores is None else scores[i])
                  for i, t, _ in items]
        rows += q_rows
        log.append(entry)
        if on_question:
            on_question(q["id"], q_rows, entry)
    return rows, log, interrupted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True)
    ap.add_argument("--model", default=None)
    ap.add_argument("--resume", action="store_true", help="continue an interrupted judge run (same model)")
    args = ap.parse_args()
    spec, h = load_frozen(BENCH_PATH, HASH_PATH)
    run = load_run(args.run)
    model = args.model or spec["pre_registered"]["judge"]["model"]
    stem = Path(args.run).stem
    out = Path(args.run).parent / f"judge_{stem}.csv"
    partial = out.with_suffix(".partial.json")
    done = {}
    if partial.exists():
        if not args.resume:
            raise SystemExit(f"{partial.name} exists (an interrupted judge run). Use --resume to continue it, or delete it.")
        saved = json.loads(partial.read_text(encoding="utf-8"))
        if saved["spec_sha256"] != h or saved["model"] != model:
            raise SystemExit("the partial file belongs to a different spec or model; the judge model must stay the same")
        done = saved["done"]
    elif args.resume:
        raise SystemExit("nothing to resume")

    def checkpoint(qid, q_rows, entry):
        done[qid] = dict(rows=q_rows, log=entry)
        partial.write_text(json.dumps(dict(spec_sha256=h, model=model, run=Path(args.run).name, done=done),
                                      ensure_ascii=False), encoding="utf-8")

    rows, log, interrupted = judge_run(spec, run, load_corpus(), GeminiLLM(model=model), done=done, on_question=checkpoint)
    if interrupted:
        print(f"INTERRUPTED after {len(done)}/{len(spec['questions'])} answers ({interrupted}).\n"
              "Progress is saved; run the same command with --resume when the quota is back. Do not switch the judge model.")
        raise SystemExit(2)
    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["question_id", "item_id", "item_text", "judge_score"])
        w.writeheader()
        w.writerows(rows)
    (out.with_suffix(".log.json")).write_text(json.dumps(dict(model=model, spec_sha256=h, log=log), indent=2), encoding="utf-8")
    partial.unlink(missing_ok=True)
    bad = [l for l in log if l["error"]]
    print(f"judge {model}: {len(log) - len(bad)}/{len(log)} answers judged; csv: {out}")
    if bad:
        print("unparseable judgements (left blank, see the .log.json):", [l["question_id"] for l in bad])
        raise SystemExit(2)


if __name__ == "__main__":
    main()
