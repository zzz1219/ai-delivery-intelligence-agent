"""Evaluate retrieval benchmark rag-retrieval-2.0 (symptom -> similar historical case) on the FROZEN specification.

  python -m rag.evaluate_retrieval_v2 --retriever bm25
  python -m rag.evaluate_retrieval_v2 --retriever dense      # needs: pip install sentence-transformers
  python -m rag.evaluate_retrieval_v2 --retriever both

Per retriever one NEW file eval_results/rag2_<retriever>_<timestamp>.json holds all runs: 2 granularities
(document = PRIMARY, section = ablation) x 2 query variants (symptom_only = PRIMARY, symptom_component_environment
= declared ablation). Only document + symptom_only is the primary configuration. The evaluator refuses to run if the
benchmark file no longer matches its frozen SHA-256.

Metrics (definitions are in the benchmark file): root-cause level Hit@k, standard Recall@k, capped Recall@k,
Precision@k, MRR; case-similarity level Strong-Hit@k, Strong-MRR; plus the AUROC abstention diagnostic.
Note: in the v1 files, "recall@k" means the CAPPED recall; here both are reported under explicit names.
"""
import argparse
import json
from datetime import datetime
from pathlib import Path

from . import metrics as m
from .build_benchmark import load_frozen
from .build_retrieval_benchmark import BENCH_PATH, HASH_PATH
from .corpus import load_corpus, verify_corpus
from .dense import DenseRetriever, SentenceTransformerEmbedder
from .retriever import BM25Retriever

ROOT = Path(__file__).resolve().parent.parent
VARIANTS = ("symptom_only", "symptom_component_environment")
GRANULARITIES = ("document", "section")


def _query_text(q, variant):
    return q["query_symptom_only" if variant == "symptom_only" else "query_symptom_component_environment"]


def score_query(retriever, q, variant, depth, ks):
    text = _query_text(q, variant)
    hits = retriever.search(text, k=depth)
    ranked = [h["doc_id"] for h in hits]
    rel, strong = set(q["relevant"]), set(q["strongly_relevant"])
    row = dict(id=q["id"], kind=q["kind"], incident_id=q["incident_id"], true_root_cause=q["true_root_cause"],
               query_text=text, cluster=text,          # identical query text = one bootstrap unit, per variant
               top1_score=hits[0]["score"] if hits else 0.0,
               retrieved=[dict(rank=h["rank"], doc_id=h["doc_id"], score=h["score"], relevant=h["doc_id"] in rel,
                               strongly_relevant=h["doc_id"] in strong) for h in hits])
    if q["kind"] == "answerable":
        row.update(mrr=m.mrr(ranked, rel), strong_mrr=m.mrr(ranked, strong))
        for k in ks:
            row[f"hit@{k}"] = m.hit_at_k(ranked, rel, k)
            row[f"recall@{k}"] = m.recall_at_k_standard(ranked, rel, k)
            row[f"capped_recall@{k}"] = m.recall_at_k_capped(ranked, rel, k)
            row[f"precision@{k}"] = m.precision_at_k(ranked, rel, k)
            row[f"strong_hit@{k}"] = m.hit_at_k(ranked, strong, k)
    return row


def evaluate_run(retriever, bench, variant):
    depth, ks = bench["metrics"]["retrieval_depth"], bench["metrics"]["k_values"]
    rows = [score_query(retriever, q, variant, depth, ks) for q in bench["queries"]]
    ans = [r for r in rows if r["kind"] == "answerable"]
    noans = [r for r in rows if r["kind"] == "no_answer"]
    keys = [k for k in ans[0] if k.startswith(("hit@", "recall@", "capped_recall@", "precision@", "strong_hit@"))] + ["mrr", "strong_mrr"]
    macro = {k: round(sum(r[k] for r in ans) / len(ans), 4) for k in keys}
    by_cause = {}
    for cause in sorted({r["true_root_cause"] for r in ans}):
        sub = [r for r in ans if r["true_root_cause"] == cause]
        by_cause[cause] = {k: round(sum(r[k] for r in sub) / len(sub), 3) for k in ("hit@1", "mrr", "strong_hit@1")}
    au = m.auroc([r["top1_score"] for r in ans], [r["top1_score"] for r in noans])
    return dict(variant=variant, n_answerable=len(ans), n_distinct_query_texts=len({r["cluster"] for r in ans}),
                macro=macro, by_root_cause=by_cause,
                abstention_diagnostic=dict(auroc_top1_score=None if au is None else round(au, 4),
                                           mean_top1_answerable=round(sum(r["top1_score"] for r in ans) / len(ans), 4),
                                           mean_top1_no_answer=round(sum(r["top1_score"] for r in noans) / len(noans), 4)),
                queries=rows)


def run_all(make_retriever, bench, bench_hash, name, out_dir=None, now=None, corpus_sha256=None, n_docs=None):
    """make_retriever(granularity) -> retriever. Runs the 2x2 grid and writes ONE new result file."""
    runs = []
    for gran in GRANULARITIES:
        retriever = make_retriever(gran)
        for variant in VARIANTS:
            res = evaluate_run(retriever, bench, variant)
            res.update(granularity=gran, retriever_config=retriever.config(),
                       role="PRIMARY" if (gran == "document" and variant == "symptom_only") else "ablation")
            runs.append(res)
    now = now or datetime.now()
    out = Path(out_dir or ROOT / "eval_results")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"rag2_{name}_{now:%Y%m%d_%H%M%S}.json"
    path.write_text(json.dumps(dict(
        meta=dict(retriever=name, benchmark_version=bench["benchmark_version"], benchmark_sha256=bench_hash,
                  corpus_sha256=corpus_sha256, n_docs=n_docs or bench["corpus"]["n_docs"],
                  run_at=now.isoformat(timespec="seconds")),
        runs=runs), ensure_ascii=False, indent=2), encoding="utf-8")
    return path, runs


COLS = ["hit@1", "hit@3", "mrr", "recall@5", "capped_recall@5", "precision@5", "strong_hit@1", "strong_hit@3", "strong_mrr"]


def print_runs(name, runs, path):
    print(f"\n=== {name}   result file: {path}")
    print(f"{'granularity':<10} {'query variant':<30} {'role':<9}" + "".join(f"{c:>16}" for c in COLS) + f"{'abstain AUROC':>15}")
    for r in runs:
        print(f"{r['granularity']:<10} {r['variant']:<30} {r['role']:<9}" + "".join(f"{r['macro'][c]:>16.3f}" for c in COLS)
              + f"{str(r['abstention_diagnostic']['auroc_top1_score']):>15}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--retriever", choices=["bm25", "dense", "both"], default="both")
    args = ap.parse_args()
    bench, h = load_frozen(BENCH_PATH, HASH_PATH)
    corpus = load_corpus()
    fp = verify_corpus(corpus)                      # refuse to run on documents that differ from the registered corpus
    reg = bench["pre_registered"]["retrievers"]
    if args.retriever in ("bm25", "both"):
        b = reg["bm25"]["primary"]
        path, runs = run_all(lambda g: BM25Retriever(corpus, g, b["k1"], b["b"]), bench, h, "bm25", corpus_sha256=fp, n_docs=len(corpus))
        print_runs("bm25", runs, path)
    if args.retriever in ("dense", "both"):
        d = reg["dense"]
        embedder = SentenceTransformerEmbedder(d["model"])
        path, runs = run_all(lambda g: DenseRetriever(corpus, embedder, g, d["query_prefix"], d["model"]), bench, h, "dense", corpus_sha256=fp, n_docs=len(corpus))
        print_runs("dense", runs, path)
    print("\nPRIMARY = document granularity + symptom_only. Everything else is a pre-declared ablation, reported for "
          "discussion; nothing is tuned or substituted after seeing results. Compare with: python -m rag.compare_runs")


if __name__ == "__main__":
    main()
