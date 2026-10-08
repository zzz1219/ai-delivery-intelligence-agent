"""Retrieval-only evaluation of R1-R5 on the FROZEN benchmark (no LLM involved, fully reproducible).

  python -m rag.evaluate_retrieval            # primary (pre-registered) config, then the pre-declared ablation
  python -m rag.evaluate_retrieval --primary-only

Each run writes a NEW file eval_results/rag_retrieval_<config>_<timestamp>.json with the retrieved document ids
and scores per question, the metrics, the retriever config and the benchmark SHA-256.
The evaluator refuses to run if the benchmark file no longer matches its frozen hash.
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

from . import metrics as m
from .build_benchmark import load_frozen
from .corpus import load_corpus
from .retriever import BM25Retriever

ROOT = Path(__file__).resolve().parent.parent


def evaluate(retriever, bench):
    depth = bench["metrics"]["retrieval_depth"]
    ks = bench["metrics"]["k_values"]
    per_q = []
    for case in bench["cases"]:
        hits = retriever.search(case["question"], k=depth)
        ranked = [h["doc_id"] for h in hits]
        rel, prim = set(case["relevant"]), set(case["primary"])
        row = dict(id=case["id"], question=case["question"], n_relevant=len(rel),
                   retrieved=[dict(rank=h["rank"], doc_id=h["doc_id"], score=h["score"],
                                   relevant=h["doc_id"] in rel, primary=h["doc_id"] in prim) for h in hits],
                   first_relevant_rank=m.first_rank(ranked, rel), first_primary_rank=m.first_rank(ranked, prim),
                   mrr=m.mrr(ranked, rel), primary_mrr=m.mrr(ranked, prim))
        for k in ks:
            row[f"hit@{k}"] = m.hit_at_k(ranked, rel, k)
            row[f"recall@{k}"] = round(m.recall_at_k_capped(ranked, rel, k), 4)
            row[f"precision@{k}"] = round(m.precision_at_k(ranked, rel, k), 4)
            row[f"primary_hit@{k}"] = m.hit_at_k(ranked, prim, k)
        per_q.append(row)
    keys = [k for k in per_q[0] if k.startswith(("hit@", "recall@", "precision@", "primary_hit@")) or k in ("mrr", "primary_mrr")]
    macro = {k: round(sum(r[k] for r in per_q) / len(per_q), 4) for k in keys}
    return per_q, macro


def run(retriever, bench, bench_hash, role, out_dir=None, now=None):
    per_q, macro = evaluate(retriever, bench)
    now = now or datetime.now()
    out = Path(out_dir or ROOT / "eval_results")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"rag_retrieval_{retriever.name}_{now:%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(dict(
        meta=dict(role=role, retriever=retriever.config(), benchmark_version=bench["benchmark_version"],
                  benchmark_sha256=bench_hash, n_docs=bench["corpus"]["n_docs"], run_at=now.isoformat(timespec="seconds")),
        macro=macro, questions=per_q), ensure_ascii=False, indent=2), encoding="utf-8")
    return path, per_q, macro


def _print(retriever, role, per_q, macro, path):
    print(f"\n== {retriever.name}  [{role}]  {retriever.config()}")
    print(f"{'id':<4}{'rel':<5}{'hit@1':<7}{'hit@3':<7}{'hit@5':<7}{'rec@3':<7}{'rec@5':<7}{'mrr':<7}{'prim@1':<8}{'prim@3':<8}{'1st rel':<8}{'1st prim'}")
    for r in per_q:
        print(f"{r['id']:<4}{r['n_relevant']:<5}{r['hit@1']:<7.0f}{r['hit@3']:<7.0f}{r['hit@5']:<7.0f}{r['recall@3']:<7.2f}"
              f"{r['recall@5']:<7.2f}{r['mrr']:<7.2f}{r['primary_hit@1']:<8.0f}{r['primary_hit@3']:<8.0f}"
              f"{str(r['first_relevant_rank']):<8}{r['first_primary_rank']}")
    print("macro: " + "  ".join(f"{k}={v}" for k, v in macro.items() if k in ("hit@1", "hit@3", "hit@5", "recall@3", "recall@5", "mrr", "primary_hit@1", "primary_hit@3", "primary_mrr")))
    print(f"result file: {path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--primary-only", action="store_true")
    args = ap.parse_args()
    bench, h = load_frozen()
    corpus = load_corpus()
    reg = bench["pre_registered"]
    configs = [("PRIMARY (pre-registered)", reg["primary"])]
    if not args.primary_only:
        configs += [("ablation (pre-declared)", a) for a in reg["ablations"]]
    for role, cfg in configs:
        r = BM25Retriever(corpus, cfg["granularity"], cfg["k1"], cfg["b"])
        path, per_q, macro = run(r, bench, h, role)
        _print(r, role, per_q, macro, path)
    print("\nOnly the PRIMARY config is the baseline; the ablation is reported for discussion and never replaces it.")


if __name__ == "__main__":
    main()
