"""Plain-assert tests (pytest-compatible). Run from the delivery_agent directory:  python -m rag.test_rag"""
import json
import shutil
import tempfile
from pathlib import Path

from . import build_benchmark as bb
from . import metrics as m
from .bm25 import BM25Index, tokenize
from .corpus import load_corpus
from .evaluate_retrieval import evaluate
from .retriever import BM25Retriever


def test_tokenizer_keeps_identifiers_and_their_parts():
    t = tokenize("Connection_pool_exhausted after the 10 million records")
    assert "connection_pool_exhausted" in t and {"connection", "pool", "exhausted", "10", "million"} <= set(t)
    assert "the" not in t and "after" in t


def test_bm25_ranks_matching_text_first_and_is_deterministic():
    items = [("a", "tile cache misconfiguration"), ("b", "connection pool exhausted under load"),
             ("c", "connection pool sizing"), ("d", "unrelated text about frontend")]
    idx = BM25Index(items)
    r1 = idx.search("connection pool exhausted", k=4)
    assert r1[0][0] == "b" and [u for u, _ in r1][:2] == ["b", "c"] and all(s > 0 for _, s in r1)
    assert r1 == idx.search("connection pool exhausted", k=4)
    assert idx.search("zzz nothing matches", k=4) == []
    tie = BM25Index([("y", "same words"), ("x", "same words")]).search("same words")
    assert [u for u, _ in tie] == ["x", "y"]                      # ties broken by id


def test_corpus_loader():
    docs = load_corpus()
    by_type = {}
    for d in docs:
        by_type.setdefault(d.doc_type, []).append(d)
    assert len(docs) == 71 and {k: len(v) for k, v in by_type.items()} == {
        "historical_cases": 60, "troubleshooting_guides": 8, "deployment_guides": 3}
    case = by_type["historical_cases"][0]
    assert case.doc_id.startswith("historical_cases/INC_") and case.metadata["root_cause"]
    assert [h for h, _ in case.sections][:3] == ["overview", "symptoms", "investigation"]
    assert len({d.doc_id for d in docs}) == 71


def test_retriever_returns_unique_documents_for_both_granularities():
    docs = load_corpus()
    for gran in ("document", "section"):
        r = BM25Retriever(docs, gran)
        hits = r.search("coordinate system mismatch reproject", k=10)
        ids = [h["doc_id"] for h in hits]
        assert len(ids) == len(set(ids)) == 10 and [h["rank"] for h in hits] == list(range(1, 11))
        assert hits == r.search("coordinate system mismatch reproject", k=10)       # deterministic


def test_metrics_on_a_toy_ranking():
    ranked, rel = ["x", "r1", "y", "r2", "z"], {"r1", "r2", "r3"}
    assert m.hit_at_k(ranked, rel, 1) == 0 and m.hit_at_k(ranked, rel, 2) == 1
    assert m.recall_at_k_capped(ranked, rel, 3) == 1 / 3          # 1 found / min(3 relevant, k=3)
    assert m.recall_at_k_capped(ranked, rel, 5) == 2 / 3
    assert m.recall_at_k_capped(ranked, {"r1"}, 3) == 1.0         # cap: one relevant document is enough
    assert m.precision_at_k(ranked, rel, 5) == 0.4 and m.mrr(ranked, rel) == 0.5 and m.mrr(["x"], rel) == 0.0
    assert m.first_rank(ranked, {"r2"}) == 4 and m.first_rank(ranked, {"q"}) is None


class _Oracle:
    name = "oracle"

    def __init__(self, bench):
        self.rel = {c["question"]: c["relevant"] for c in bench["cases"]}

    def search(self, query, k=10):
        return [dict(rank=i + 1, doc_id=d, score=1.0) for i, d in enumerate(self.rel[query][:k])]


class _Empty:
    name = "empty"

    def search(self, query, k=10):
        return []


def test_benchmark_is_frozen_and_complete():
    bench, h = bb.load_frozen()
    assert h == bb.HASH_PATH.read_text().split()[0] and bench["benchmark_version"] == "rag-1.0"
    ids = {d.doc_id for d in load_corpus()}
    assert [c["id"] for c in bench["cases"]] == ["R1", "R2", "R3", "R4", "R5"]
    for c in bench["cases"]:
        assert set(c["relevant"]) <= ids and set(c["primary"]) <= set(c["relevant"]) and c["primary"]
        assert c["facts"], c["id"]                                   # semantic gold exists for every question
    assert bb.build() == bb.BENCH_PATH.read_bytes()                  # a fresh build is byte-identical


def test_tampering_with_the_frozen_benchmark_is_refused():
    d = Path(tempfile.mkdtemp())
    shutil.copy(bb.BENCH_PATH, d / "b.json")
    shutil.copy(bb.HASH_PATH, d / "b.sha256")
    bb.load_frozen(d / "b.json", d / "b.sha256")                     # untouched: fine
    doc = json.loads((d / "b.json").read_text())
    doc["cases"][0]["relevant"].append("historical_cases/INC_0001")
    (d / "b.json").write_text(json.dumps(doc, indent=2))
    try:
        bb.load_frozen(d / "b.json", d / "b.sha256")
        raise AssertionError("modified benchmark must be refused")
    except SystemExit as e:
        assert "frozen" in str(e)


def test_metric_pipeline_with_oracle_and_empty_retrievers():
    bench, _ = bb.load_frozen()
    _, perfect = evaluate(_Oracle(bench), bench)
    assert perfect["hit@1"] == perfect["mrr"] == 1.0
    _, none = evaluate(_Empty(), bench)
    assert none["hit@5"] == none["mrr"] == none["recall@5"] == 0.0


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
