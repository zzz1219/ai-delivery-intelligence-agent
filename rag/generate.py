"""Run the answer-generation benchmark (rag-generation-1.0) with ONE model.

  Windows PowerShell:
    $env:GEMINI_API_KEY="your-key"
    python -m rag.generate --model gemini-3.5-flash-lite
    python -m rag.generate --model gemini-3.5-flash-lite --ablation      # the declared (3 cases, 2 guides) ablation

The spec (questions, lanes, prompt) is frozen and hash-checked, the corpus fingerprint is verified, and the run writes a
NEW file eval_results/gen_<model>_c<k>g<k>_<timestamp>.json (checkpointed after every question; renamed when complete,
_INCOMPLETE otherwise). Two consecutive provider errors stop the run; any other exception aborts it. A model that differs
from the pre-registered one needs --allow-other-model and is recorded as a deviation (availability reasons only).
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from sql_agent.evaluate import _write_atomic  # noqa: E402
from sql_agent.llm import GeminiLLM, is_provider_transient  # noqa: E402
from .build_benchmark import load_frozen  # noqa: E402
from .build_generation_benchmark import BENCH_PATH, HASH_PATH  # noqa: E402
from .corpus import load_corpus, verify_corpus  # noqa: E402
from .lanes import TwoLaneRetriever  # noqa: E402


def load_spec():
    return load_frozen(BENCH_PATH, HASH_PATH)


def build_context(doc_ids, text_by_id, doc_block):
    return "\n\n".join(doc_block.format(doc_id=d, text=text_by_id[d].strip()) for d in doc_ids)


def lanes_for(spec, ablation=False):
    a = spec["context_sufficiency_audit"]
    pick = a["declared_ablation"] if ablation else a["selected"]
    return pick["cases"], pick["guides"]


def run_generation(spec, spec_hash, corpus, corpus_sha256, llm, ablation=False, out_dir=None, now=None,
                   threshold=2, deviation=False):
    """Returns (path, run_document, exit_code)."""
    reg = spec["pre_registered"]["generator"]
    k_cases, k_guides = lanes_for(spec, ablation)
    retriever = TwoLaneRetriever(corpus, k_cases, k_guides)
    text = {d.doc_id: d.text for d in corpus}
    out_dir = Path(out_dir or ROOT / "eval_results")
    out_dir.mkdir(parents=True, exist_ok=True)
    started = now or datetime.now()
    name = getattr(llm, "name", "llm").replace(":", "_").replace("/", "_")
    stem = f"gen_{name}_c{k_cases}g{k_guides}_{started:%Y%m%d_%H%M%S}"
    progress, final = out_dir / f"{stem}_INCOMPLETE.json", out_dir / f"{stem}.json"
    role = "declared ablation" if ablation else "PRIMARY"
    answers, consecutive, stop = [], 0, None

    def save(state):
        complete = state == "complete"
        _write_atomic(final if complete else progress, dict(
            meta=dict(model=getattr(llm, "name", "llm"), role=role, lanes=retriever.config(), state=state,
                      complete=complete, spec_version=spec["benchmark_version"], spec_sha256=spec_hash,
                      corpus_sha256=corpus_sha256, started_at=started.isoformat(timespec="seconds"),
                      deviation_from_registered_model=deviation, registered_model=reg["model"]),
            answers=answers))

    try:
        for idx, q in enumerate(spec["questions"]):
            cases, guides = retriever.retrieve_scored(q["retrieval_query"])
            hits = [dict(lane="cases", **h) for h in cases] + [dict(lane="guides", **h) for h in guides]
            ctx_ids = [h["doc_id"] for h in hits]
            record = dict(id=q["id"], group=q["group"], question=q["question"], retrieved=hits, context_ids=ctx_ids)
            user = reg["user_template"].format(context=build_context(ctx_ids, text, reg["doc_block"]),
                                               question=q["question"])
            try:
                record.update(status="ok", answer=llm.complete("answer", reg["system"], user).strip())
            except Exception as e:  # noqa: BLE001 - classified: provider-transient vs everything else
                provider = is_provider_transient(e)
                record.update(status="error", error_class="provider" if provider else "runtime", answer=None,
                              error_type=type(e).__name__, error_message=str(e)[:500])
                consecutive = consecutive + 1 if provider else consecutive
                if not provider:
                    stop = "aborted_on_runtime_error"
                elif consecutive >= threshold:
                    stop = "circuit_breaker"
            else:
                consecutive = 0
            answers.append(record)
            if stop:
                answers.extend(dict(id=r["id"], group=r["group"], status="skipped", reason=stop)
                               for r in spec["questions"][idx + 1:])
            save("running")
            if stop:
                break
    except KeyboardInterrupt:
        save("interrupted")
        raise
    ok = all(a["status"] == "ok" for a in answers) and len(answers) == len(spec["questions"])
    if ok:
        save("complete")
        progress.unlink(missing_ok=True)
        doc = dict(answers=answers)
        return final, doc, 0
    save("aborted" if stop == "aborted_on_runtime_error" else "incomplete")
    return progress, dict(answers=answers), 3 if stop == "aborted_on_runtime_error" else 2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=None)
    ap.add_argument("--ablation", action="store_true")
    ap.add_argument("--allow-other-model", action="store_true")
    args = ap.parse_args()
    spec, h = load_spec()
    registered = spec["pre_registered"]["generator"]["model"]
    model = args.model or registered
    if model != registered and not args.allow_other_model:
        raise SystemExit(f"the pre-registered generator is {registered}; use --allow-other-model only for availability "
                         "reasons (it is recorded as a deviation)")
    corpus = load_corpus()
    fp = verify_corpus(corpus)
    path, doc, code = run_generation(spec, h, corpus, fp, GeminiLLM(model=model), ablation=args.ablation,
                                     deviation=model != registered)
    ok = sum(a["status"] == "ok" for a in doc["answers"])
    print(f"model {model}: {ok}/{len(spec['questions'])} answers written\nresult file: {path}")
    if code:
        bad = [a for a in doc["answers"] if a["status"] == "error"]
        print("INCOMPLETE run. First error:", bad[0]["error_type"], bad[0]["error_message"] if bad else "")
        print("Do not score an incomplete run. Rerun, or switch model for the WHOLE run (availability reasons only).")
    raise SystemExit(code)


if __name__ == "__main__":
    main()
