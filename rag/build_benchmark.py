"""Build and FREEZE the RAG benchmark (R1-R5) before any retriever is ever run on it.

Everything is derived from the frozen data layer (seed 58): relevance judgments from the document manifest and the
planted structure, semantic facts from the guides' own text and from the documented cases. Nothing here looks at
retrieval output. The file is written to benchmarks/ (NOT ground_truth/, which generate_data.py wipes), together
with a SHA-256; the evaluator refuses to run if the file no longer matches its hash.

  python -m rag.build_benchmark            # (re)build into benchmarks/ and write the hash
  python -m rag.build_benchmark --check    # verify the committed file still matches a fresh build
"""
import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import generate_data as g  # noqa: E402  (frozen data layer: guide text and manifests)
from .corpus import load_corpus  # noqa: E402

BENCH_DIR = ROOT / "benchmarks"
BENCH_PATH = BENCH_DIR / "rag_benchmark_v1.json"
HASH_PATH = BENCH_DIR / "rag_benchmark_v1.sha256"

PRE_REGISTERED = dict(
    primary=dict(retriever="bm25", granularity="document", k1=1.5, b=0.75,
                 tokenizer="lowercase [a-z0-9_]+, identifiers also split into parts, small stopword list, no stemming"),
    ablations=[dict(retriever="bm25", granularity="section", k1=1.5, b=0.75, aggregation="max chunk score per document")],
    rule="No parameter is tuned on this benchmark. Ablations are reported, never used to replace the primary config. "
         "Dense retrieval will be added later as a separate, equally pre-declared comparison.")

METRICS = dict(
    unit="document", retrieval_depth=10, k_values=[1, 3, 5],
    hit_at_k="1 if any relevant document is in the top k",
    recall_at_k_capped="|relevant in top k| / min(|relevant|, k)",
    precision_at_k="|relevant in top k| / k",
    mrr="1 / rank of the first relevant document (0 if none in the top 10)",
    primary_hit_at_k="1 if any PRIMARY document (the guide that directly answers the question) is in the top k",
    primary_mrr="1 / rank of the first primary document")

GENERATION_CONTRACT = dict(
    status="defined now, used later (answer-generation step)",
    judge="rubric: every fact must be covered; no claim unsupported by the retrieved documents",
    citations="answer must cite document ids; every cited id must be in the retrieved set",
    separate_from_retrieval=True)


def _rows(path):
    return list(csv.DictReader(open(path, encoding="utf-8")))


def _guide_id(filename):
    stem = filename[:-3] if filename.endswith(".md") else filename
    return f"troubleshooting_guides/{stem}" if filename in g.TROUBLESHOOTING else f"deployment_guides/{stem}"


def _guide_facts(filename):
    if filename in g.TROUBLESHOOTING:
        title, root, symptom, steps, fixes = g.TROUBLESHOOTING[filename]
        return steps, fixes
    return g.DEPLOYMENT[filename][1], []


def build():
    cases = {c["id"]: c for c in json.loads((ROOT / "ground_truth/evaluation_cases.json").read_text())["cases"]}
    manifest = {r["incident_id"]: r for r in _rows(ROOT / "ground_truth/doc_manifest.csv")}
    corpus_ids = {d.doc_id for d in load_corpus()}

    def case_docs(ids):
        return [f"historical_cases/{i}" for i in sorted(ids)]

    def patterns(ids):
        return sorted(Counter(manifest[i]["solution_pattern"] for i in ids).items(), key=lambda kv: (-kv[1], kv[0]))

    out_cases = []

    def add(cid, guides, doc_ids, facts):
        rel = [_guide_id(x) for x in guides] + case_docs(doc_ids)
        assert set(rel) <= corpus_ids, sorted(set(rel) - corpus_ids)
        out_cases.append(dict(
            id=cid, question=cases[cid]["question"], relevant=rel, primary=[_guide_id(x) for x in guides],
            facts=[dict(id=f"{cid}-f{i}", statement=s) for i, s in enumerate(facts, 1)],
            generation_contract=GENERATION_CONTRACT))

    # R1: latency after a 10M-record import
    ids = cases["R1"]["expected_doc_ids"]
    steps, _ = _guide_facts("bulk_import_performance.md")
    pats = ", ".join(f"{p} ({n})" for p, n in patterns(ids))
    add("R1", cases["R1"]["expected_guides"], ids,
        [f"Guide: {s}" for s in steps[:3]] +
        ["Guide: after the load, verify indexes and statistics (slow-query guide)",
         f"Documented cases with imports of 10 million records or more were resolved with: {pats}"])

    # R2: usual fixes for coordinate-system mismatch
    ids = cases["R2"]["expected_doc_ids"]
    steps, fixes = _guide_facts("crs_mismatch.md")
    pats = ", ".join(f"{p} ({n})" for p, n in patterns(ids))
    add("R2", cases["R2"]["expected_guides"], ids,
        [f"Guide fix: {f}" for f in fixes] + [f"Guide check: {s}" for s in steps] +
        [f"Documented cases were resolved with: {pats}"])

    # R3: have timeouts been seen after distributed deployments, and what was found
    ids = cases["R3"]["expected_doc_ids"]
    pats = ", ".join(f"{p} ({n})" for p, n in patterns(ids))
    v32 = sum(manifest[i]["version"] == "v3.2" for i in ids)
    facts = [f"Yes: {len(ids)} documented timeout cases occurred in distributed deployments",
             "Finding: gateway and backend logs showed requests exceeding the configured timeout while upstream "
             "services were still processing",
             f"Fixes applied in those cases: {pats}"]
    if v32:
        facts.append(f"{v32} of the cases first appeared shortly after the upgrade to v3.2 (coincidence in time, "
                     "not a proven cause)")
    steps, fixes = _guide_facts("service_timeouts.md")
    facts += [f"Guide check: {s}" for s in steps[:3]]
    add("R3", cases["R3"]["expected_guides"], ids, facts)

    # R4: what the connection-pool guide says
    ids = cases["R4"]["expected_doc_ids"]
    steps, fixes = _guide_facts("connection_pool_exhausted.md")
    add("R4", cases["R4"]["expected_guides"], ids,
        [f"Guide step: {s}" for s in steps] + [f"Guide fix: {f}" for f in fixes])

    # R5: pre-go-live checks for a distributed deployment (answerable from one guide only)
    steps, _ = _guide_facts("distributed_deployment.md")
    add("R5", cases["R5"]["expected_guides"], [], [f"Checklist item: {s}" for s in steps])

    doc = dict(benchmark_version="rag-1.0", frozen=True, data_seed=g.FROZEN_SEED,
               corpus=dict(n_docs=len(corpus_ids), types=list(sorted({i.split('/')[0] for i in corpus_ids}))),
               pre_registered=PRE_REGISTERED, metrics=METRICS, cases=out_cases)
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def freeze(path=BENCH_PATH, hash_path=HASH_PATH):
    data = build()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    hash_path.write_text(f"{sha256(data)}  {path.name}\n", encoding="utf-8")
    return sha256(data)


def load_frozen(path=BENCH_PATH, hash_path=HASH_PATH):
    """Load the benchmark; refuse if it was modified after freezing."""
    data = Path(path).read_bytes()
    expected = Path(hash_path).read_text().split()[0]
    if sha256(data) != expected:
        raise SystemExit(f"{Path(path).name} does not match its frozen SHA-256: the benchmark was modified after "
                         "freezing. Restore it, or create a new benchmark version.")
    return json.loads(data), expected


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="verify that a fresh build equals the frozen file")
    args = ap.parse_args()
    if args.check:
        same = build() == BENCH_PATH.read_bytes() and sha256(BENCH_PATH.read_bytes()) == HASH_PATH.read_text().split()[0]
        print("frozen benchmark matches a fresh build" if same else "MISMATCH")
        raise SystemExit(0 if same else 1)
    print("frozen sha256:", freeze())


if __name__ == "__main__":
    main()
