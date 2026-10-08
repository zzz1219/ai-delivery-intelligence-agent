"""Paired bootstrap comparison of two rag2 result files (e.g. BM25 vs dense) on the same frozen benchmark.

  python -m rag.compare_runs eval_results/rag2_bm25_<ts>.json eval_results/rag2_dense_<ts>.json
  python -m rag.compare_runs A.json B.json --granularity document --variant symptom_only   (the PRIMARY run, default)

Reports mean(B) - mean(A) per metric with a 95% percentile interval from 2000 paired CLUSTER resamples of the answerable
queries (clusters = queries with identical text for the selected variant; seeded, so the output is reproducible). With 40 queries an interval that contains 0 means "no reliable
difference", whatever the point estimate says.
"""
import argparse
import json
import random

METRICS = ["hit@1", "hit@3", "mrr", "recall@5", "strong_hit@1", "strong_hit@3", "strong_mrr"]


def paired_bootstrap(a, b, clusters=None, n=2000, seed=2026):
    """a, b: per-query values (same order). clusters: optional cluster label per query; queries with the same label
    are resampled together (they share the same text, so they are not independent). Returns (mean diff, lo, hi)."""
    assert len(a) == len(b) and a
    diffs = [y - x for x, y in zip(a, b)]
    groups = {}
    for i, c in enumerate(clusters if clusters is not None else range(len(diffs))):
        groups.setdefault(c, []).append(diffs[i])
    units = [(sum(v), len(v)) for _, v in sorted(groups.items(), key=lambda kv: str(kv[0]))]
    rng = random.Random(seed)
    k = len(units)
    means = []
    for _ in range(n):
        pick = [units[rng.randrange(k)] for _ in range(k)]
        means.append(sum(s for s, _ in pick) / sum(c for _, c in pick))
    means.sort()
    return sum(diffs) / len(diffs), means[int(0.025 * n)], means[int(0.975 * n) - 1]


def pick_run(doc, granularity, variant):
    return next(r for r in doc["runs"] if r["granularity"] == granularity and r["variant"] == variant)


def compare(doc_a, doc_b, granularity="document", variant="symptom_only"):
    assert doc_a["meta"]["benchmark_sha256"] == doc_b["meta"]["benchmark_sha256"], "different benchmark versions"
    ca, cb = doc_a["meta"].get("corpus_sha256"), doc_b["meta"].get("corpus_sha256")
    if not ca or not cb:
        raise SystemExit("a result file has no corpus_sha256 (it predates the corpus fingerprint). Rerun that retriever "
                         "with the current code and compare the new file.")
    assert ca == cb, "different document corpora: the two retrievers did not search the same documents"
    ra, rb = pick_run(doc_a, granularity, variant), pick_run(doc_b, granularity, variant)
    for r in (ra, rb):
        if "query_text" not in r["queries"][0]:
            raise SystemExit("this result file predates the per-variant cluster fix. Rerun the (deterministic) "
                             "retriever and compare the new file.")
    qa = {q["id"]: q for q in ra["queries"] if q["kind"] == "answerable"}
    qb = {q["id"]: q for q in rb["queries"] if q["kind"] == "answerable"}
    ids = sorted(qa)
    assert ids == sorted(qb)
    for i in ids:                                      # the two runs must have answered exactly the same query
        assert qa[i]["query_text"] == qb[i]["query_text"], f"{i}: different query text in A and B"
        assert qa[i]["cluster"] == qb[i]["cluster"], f"{i}: different cluster in A and B"
    out = {}
    for met in METRICS:
        mean_d, lo, hi = paired_bootstrap([qa[i][met] for i in ids], [qb[i][met] for i in ids],
                                          clusters=[qa[i]["cluster"] for i in ids])
        out[met] = dict(a=round(sum(qa[i][met] for i in ids) / len(ids), 4),
                        b=round(sum(qb[i][met] for i in ids) / len(ids), 4),
                        diff=round(mean_d, 4), ci_low=round(lo, 4), ci_high=round(hi, 4))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--granularity", default="document")
    ap.add_argument("--variant", default="symptom_only")
    args = ap.parse_args()
    da, db = json.load(open(args.a, encoding="utf-8")), json.load(open(args.b, encoding="utf-8"))
    res = compare(da, db, args.granularity, args.variant)
    print(f"A = {da['meta']['retriever']}   B = {db['meta']['retriever']}   ({args.granularity}, {args.variant}; "
          "difference = B - A, paired cluster bootstrap 95% interval; clusters = distinct query texts of the selected variant)")
    print(f"{'metric':<14}{'A':>8}{'B':>8}{'B - A':>9}   95% interval")
    for met, r in res.items():
        verdict = "" if r["ci_low"] <= 0 <= r["ci_high"] else ("   B better" if r["diff"] > 0 else "   A better")
        print(f"{met:<14}{r['a']:>8.3f}{r['b']:>8.3f}{r['diff']:>+9.3f}   [{r['ci_low']:+.3f}, {r['ci_high']:+.3f}]{verdict}")


if __name__ == "__main__":
    main()
