"""Plain-assert tests for retrieval benchmark v2.0 (no model download, no real-retriever run on the benchmark).
Run from the delivery_agent directory:  python -m rag.test_rag_v2"""
import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path

import numpy as np

from . import build_retrieval_benchmark as b2
from . import metrics as m
from .bm25 import tokenize
from .build_benchmark import load_frozen
from .compare_runs import compare, paired_bootstrap
from .corpus import corpus_fingerprint, load_corpus, verify_corpus
from .dense import DenseRetriever
from .diagnose_misses import diagnose
from .evaluate_retrieval_v2 import evaluate_run, run_all


def _bench():
    return load_frozen(b2.BENCH_PATH, b2.HASH_PATH)


def test_v2_structure_and_no_leakage():
    bench, _ = _bench()
    qs = bench["queries"]
    ids = {d.doc_id for d in load_corpus()}
    texts = [d.text for d in load_corpus()]
    ans = [q for q in qs if q["kind"] == "answerable"]
    noa = [q for q in qs if q["kind"] == "no_answer"]
    assert len(ans) == 40 and len(noa) == 5 and len({q["id"] for q in qs}) == 45
    assert Counter(q["true_root_cause"] for q in ans) == {c: 5 for c in set(q["true_root_cause"] for q in ans)}
    assert len({q["true_root_cause"] for q in ans}) == 8 and all(q["true_root_cause"] == b2.NO_ANSWER_ROOT for q in noa)
    for q in ans:
        assert q["relevant"] and q["strongly_relevant"] and set(q["strongly_relevant"]) <= set(q["relevant"]) <= ids
    for q in noa:
        assert q["relevant"] == [] and q["strongly_relevant"] == []
    for q in qs:                                                         # the query incident never has its own document
        assert f"historical_cases/{q['incident_id']}" not in ids
        assert q["query_symptom_only"] not in "\n".join(texts)           # and its wording is not copied from a document
        assert q["query_symptom_only"] in q["query_symptom_component_environment"]


def test_v2_is_frozen_reproducible_and_tamper_proof():
    bench, h = _bench()
    assert bench["benchmark_version"] == "rag-retrieval-2.0" and "before either BM25 or dense" in bench["disclosure"]
    assert b2.build() == b2.BENCH_PATH.read_bytes()
    d = Path(tempfile.mkdtemp())
    shutil.copy(b2.BENCH_PATH, d / "b.json")
    shutil.copy(b2.HASH_PATH, d / "b.sha256")
    doc = json.loads((d / "b.json").read_text())
    doc["queries"][0]["relevant"].append("historical_cases/INC_0001")
    (d / "b.json").write_text(json.dumps(doc, indent=2))
    try:
        load_frozen(d / "b.json", d / "b.sha256")
        raise AssertionError("modified benchmark must be refused")
    except SystemExit as e:
        assert "frozen" in str(e)


def test_new_metrics():
    ranked, rel = ["x", "r1", "y", "r2", "z"], {"r1", "r2", "r3"}
    assert m.recall_at_k_standard(ranked, rel, 5) == 2 / 3 and m.recall_at_k_standard(ranked, rel, 1) == 0
    twelve = {f"d{i}" for i in range(12)}
    five = [f"d{i}" for i in range(5)]
    assert m.recall_at_k_capped(five, twelve, 5) == 1.0 and abs(m.recall_at_k_standard(five, twelve, 5) - 5 / 12) < 1e-9
    assert m.auroc([3, 4], [1, 2]) == 1.0 and m.auroc([1, 2], [3, 4]) == 0.0 and m.auroc([1], [1]) == 0.5
    assert m.auroc([], [1]) is None


class _HashEmbedder:
    """Deterministic bag-of-words hashing embedder: lets the dense code path run without a model."""
    model_name = "hash-embedder"
    library_version = "test"

    def __init__(self):
        self.calls = []                                    # one entry per encode() call, in order

    def encode(self, texts):
        self.calls.append(list(texts))
        out = np.zeros((len(texts), 64))
        for i, t in enumerate(texts):
            for tok in tokenize(t):
                out[i, sum(ord(c) * (j + 1) for j, c in enumerate(tok)) % 64] += 1.0
        return out


def test_dense_retriever_mechanics():
    docs = load_corpus()
    emb = _HashEmbedder()
    for gran in ("document", "section"):
        r = DenseRetriever(docs, emb, gran, query_prefix="PFX: ")
        hits = r.search("coordinate system mismatch reproject layers", k=10)
        ids = [h["doc_id"] for h in hits]
        assert len(ids) == len(set(ids)) == 10 and [h["rank"] for h in hits] == list(range(1, 11))
        assert hits == r.search("coordinate system mismatch reproject layers", k=10)
        assert all(-1.0001 <= h["score"] <= 1.0001 for h in hits)
        assert hits[0]["doc_id"].endswith("crs_mismatch") or "INC_" in hits[0]["doc_id"]
    # the first call of each retriever embeds the documents/sections; every later call embeds one query
    doc_batches = [c for c in emb.calls if len(c) > 1]
    query_calls = [c for c in emb.calls if len(c) == 1]
    assert len(doc_batches) == 2 and sorted(len(c) for c in doc_batches) == [71, 395]
    assert all(not t.startswith("PFX: ") for batch in doc_batches for t in batch)          # documents: NO prefix
    assert query_calls and all(c[0] == "PFX: coordinate system mismatch reproject layers" for c in query_calls)
    cfg = DenseRetriever(docs, emb, "document", "PFX: ").config()
    assert cfg["query_prefix"] == "PFX: " and cfg["normalized"] and cfg["similarity"] == "cosine"


class _OracleRetriever:
    """Returns exactly the relevant documents (strongly relevant first) so every metric must be perfect."""
    name = "oracle"

    def __init__(self, bench):
        self.by_text = {}
        for q in bench["queries"]:
            for v in ("query_symptom_only", "query_symptom_component_environment"):
                self.by_text[q[v]] = q

    def config(self):
        return dict(retriever="oracle")

    def search(self, query, k=10):
        q = self.by_text[query]
        order = q["strongly_relevant"] + [d for d in q["relevant"] if d not in q["strongly_relevant"]]
        filler = ["troubleshooting_guides/crs_mismatch"]
        return [dict(rank=i + 1, doc_id=d, score=1.0 / (i + 1)) for i, d in enumerate((order or filler)[:k])]


def test_v2_evaluator_with_oracle():
    bench, h = _bench()
    res = evaluate_run(_OracleRetriever(bench), bench, "symptom_component_environment")
    mc = res["macro"]
    assert mc["hit@1"] == mc["mrr"] == mc["strong_hit@1"] == mc["strong_mrr"] == 1.0
    assert 0 < mc["recall@5"] <= 1.0 and mc["capped_recall@5"] == 1.0 and mc["precision@5"] > 0
    assert len(res["by_root_cause"]) == 8 and res["abstention_diagnostic"]["auroc_top1_score"] is not None
    assert res["n_answerable"] == 40 and res["n_distinct_query_texts"] == 35         # enriched variant: 35 texts
    assert evaluate_run(_OracleRetriever(bench), bench, "symptom_only")["n_distinct_query_texts"] == 20
    d = tempfile.mkdtemp()
    path, runs = run_all(lambda g: _OracleRetriever(bench), bench, h, "oracle", out_dir=d, corpus_sha256="c0")
    assert len(runs) == 4 and [r["role"] for r in runs].count("PRIMARY") == 1
    saved = json.loads(path.read_text())
    assert saved["meta"]["benchmark_sha256"] == h and len(saved["runs"]) == 4


def test_known_limitations_are_disclosed_and_consistent():
    bench, _ = _bench()
    lim = bench["known_limitations"]
    ans = [q for q in bench["queries"] if q["kind"] == "answerable"]
    assert lim["distinct_texts_symptom_only"] == len({q["query_symptom_only"] for q in ans}) < len(ans)
    assert lim["symptom_texts_with_conflicting_strong_sets"] > 0
    ub = lim["symptom_only_upper_bound"]
    assert 0 < ub["strong_hit_at_1"] < 1.0 and ub["strong_hit_at_3"] >= ub["strong_hit_at_1"]
    # identical symptom text always implies identical root-cause relevance (so root-cause metrics are unaffected)
    for t in {q["query_symptom_only"] for q in ans}:
        assert len({tuple(q["relevant"]) for q in ans if q["query_symptom_only"] == t}) == 1
    # a retriever that sees only the symptom text cannot beat the stated upper bound
    class SymptomOracle(_OracleRetriever):
        def __init__(self, b):
            self.groups = {}
            for q in b["queries"]:
                self.groups.setdefault(q["query_symptom_only"], []).append(q)

        def search(self, query, k=10):
            g = self.groups[query]
            from collections import Counter
            best = Counter(d for q in g for d in q["strongly_relevant"]).most_common()
            order = [d for d, _ in best] + [d for q in g for d in q["relevant"] if d not in dict(best)]
            return [dict(rank=i + 1, doc_id=d, score=1.0 / (i + 1)) for i, d in enumerate(order[:k])]
    res = evaluate_run(SymptomOracle(bench), bench, "symptom_only")
    assert res["macro"]["strong_hit@1"] <= ub["strong_hit_at_1"] + 1e-9


def test_cluster_is_the_text_of_the_variant_that_was_run():
    bench, h = _bench()
    for variant, expected in (("symptom_only", 20), ("symptom_component_environment", 35)):
        res = evaluate_run(_OracleRetriever(bench), bench, variant)
        ans = [r for r in res["queries"] if r["kind"] == "answerable"]
        assert len({r["cluster"] for r in ans}) == expected and all(r["cluster"] == r["query_text"] for r in ans)
    d = tempfile.mkdtemp()
    path, _ = run_all(lambda g: _OracleRetriever(bench), bench, h, "o", out_dir=d, corpus_sha256="c0")
    doc = json.loads(path.read_text())
    compare(doc, doc, "document", "symptom_component_environment")             # new files: fine
    for run in doc["runs"]:                                                      # a file written before the fix
        for q in run["queries"]:
            q.pop("query_text", None)
    for variant in ("symptom_component_environment", "symptom_only"):
        try:
            compare(doc, doc, "document", variant)
            raise AssertionError("legacy file must be refused")
        except SystemExit as e:
            assert "predates" in str(e)


def test_cluster_bootstrap_treats_identical_texts_as_one_unit():
    a, b = [0, 0, 0, 0], [1, 1, 0, 0]
    plain = paired_bootstrap(a, b)
    clustered = paired_bootstrap(a, b, clusters=["x", "x", "y", "y"])
    assert plain[0] == clustered[0] == 0.5
    assert (clustered[2] - clustered[1]) >= (plain[2] - plain[1])      # fewer independent units: never narrower


def test_compare_runs_and_bootstrap():
    mean, lo, hi = paired_bootstrap([0, 0, 0, 0], [1, 1, 1, 1])
    assert mean == lo == hi == 1.0
    mean, lo, hi = paired_bootstrap([1, 0, 1, 0], [1, 0, 1, 0])
    assert mean == lo == hi == 0.0
    assert paired_bootstrap([0, 1, 0, 1, 1, 0], [1, 1, 0, 1, 0, 0]) == paired_bootstrap([0, 1, 0, 1, 1, 0], [1, 1, 0, 1, 0, 0])
    bench, h = _bench()
    d = tempfile.mkdtemp()
    p1, _ = run_all(lambda g: _OracleRetriever(bench), bench, h, "o1", out_dir=d, corpus_sha256="c0")
    doc = json.loads(p1.read_text())
    res = compare(doc, doc)
    assert all(v["diff"] == 0.0 and v["ci_low"] == v["ci_high"] == 0.0 for v in res.values())
    other = json.loads(p1.read_text())
    other["meta"]["benchmark_sha256"] = "different"
    try:
        compare(doc, other)
        raise AssertionError("runs on different benchmark versions must not be compared")
    except AssertionError as e:
        assert "different benchmark" in str(e)


def test_corpus_fingerprint_registered_and_tamper_proof():
    docs = load_corpus()
    fp = corpus_fingerprint(docs)
    assert len(fp) == 64 and verify_corpus(docs) == fp                       # the registered corpus is the current one
    root = Path(tempfile.mkdtemp())
    shutil.copytree(Path(__file__).resolve().parent.parent / "docs", root / "docs")
    assert corpus_fingerprint(load_corpus(root / "docs")) == fp
    crlf = root / "docs" / "historical_cases" / "INC_0001.md"
    crlf.write_bytes(crlf.read_bytes().replace(b"\n", b"\r\n"))              # e.g. a Windows checkout: same content
    assert corpus_fingerprint(load_corpus(root / "docs")) == fp
    target = root / "docs" / "troubleshooting_guides" / "service_timeouts.md"
    target.write_text(target.read_text(encoding="utf-8").replace("timeout", "time-out", 1), encoding="utf-8")
    changed = load_corpus(root / "docs")
    assert len(changed) == 71 and corpus_fingerprint(changed) != fp          # same count, different content: caught
    try:
        verify_corpus(changed)
        raise AssertionError("a modified corpus must be refused")
    except SystemExit as e:
        assert "corpus changed" in str(e)
    (root / "docs" / "deployment_guides" / "cloud_deployment.md").unlink()
    assert corpus_fingerprint(load_corpus(root / "docs")) != fp


def test_compare_requires_same_corpus_and_same_queries():
    bench, h = _bench()
    d = tempfile.mkdtemp()
    p, _ = run_all(lambda g: _OracleRetriever(bench), bench, h, "o", out_dir=d, corpus_sha256="c0")
    a = json.loads(p.read_text())
    b = json.loads(p.read_text())
    compare(a, b)
    b["meta"]["corpus_sha256"] = "c1"
    try:
        compare(a, b)
        raise AssertionError("different corpora must not be compared")
    except AssertionError as e:
        assert "different document corpora" in str(e)
    b["meta"]["corpus_sha256"] = None
    try:
        compare(a, b)
        raise AssertionError("a file without a corpus fingerprint must be refused")
    except SystemExit as e:
        assert "corpus_sha256" in str(e)
    b = json.loads(p.read_text())
    next(r for r in b["runs"] if r["role"] == "PRIMARY")["queries"][0]["query_text"] = "something else"
    try:
        compare(a, b)
        raise AssertionError("different query text for the same id must be refused")
    except AssertionError as e:
        assert "different query text" in str(e)


def test_post_hoc_diagnostic_on_a_perfect_and_a_guide_first_retriever():
    bench, h = _bench()
    d = tempfile.mkdtemp()
    p, _ = run_all(lambda g: _OracleRetriever(bench), bench, h, "oracle", out_dir=d, corpus_sha256="c0")
    out = diagnose(json.loads(p.read_text()))
    assert out["top1_misses"] == 0 and out["case_only"]["hit_at_1"] == 1.0 and out["miss_types"] == {}
    assert out["case_only_top1_auroc"] is not None

    class GuideFirst(_OracleRetriever):                       # always puts the matching guide above the relevant cases
        guide = {"authentication_failure": "authentication_failures", "batch_size_too_large": "bulk_import_performance",
                 "connection_pool_exhausted": "connection_pool_exhausted", "crs_mismatch": "crs_mismatch",
                 "frontend_config_error": "service_timeouts", "missing_index": "slow_queries_missing_index",
                 "tile_cache_misconfig": "tile_cache_issues", "timeout": "service_timeouts",
                 "service_publish_failure": "service_publication_failures"}

        def search(self, query, k=10):
            q = self.by_text[query]
            first = "troubleshooting_guides/" + self.guide[q["true_root_cause"]]
            rest = super().search(query, k)
            return [dict(rank=1, doc_id=first, score=2.0)] + [dict(r, rank=i + 2) for i, r in enumerate(rest) if r["doc_id"] != first][: k - 1]
    p2, _ = run_all(lambda g: GuideFirst(bench), bench, h, "gf", out_dir=d, corpus_sha256="c0")
    out = diagnose(json.loads(p2.read_text()))
    assert out["case_only"]["hit_at_1"] == 1.0                              # guides removed: the cases were all first
    assert out["frozen_primary"]["hit@1"] < 0.2                              # but with guides first the frozen metric collapses
    assert out["top1_misses"] >= 30 and "case" not in out["miss_types"]


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
