"""Build and FREEZE retrieval benchmark rag-retrieval-2.0 (symptom -> similar historical case).

Disclosure (also stored in the file): v1.0 saturated under BM25 because its questions repeat the guides' own words.
After observing that, v2.0 was designed as an audited retrieval benchmark. The v2.0 specification is frozen BEFORE
either BM25 or dense retrieval is run on it.

Design
  * query    : the public `symptom_summary` of an incident that has NO case document of its own (no leakage);
               its wording differs from the case documents' Symptoms text on purpose
  * relevant : case documents with the same TRUE root cause (hidden truth file; the agent never sees it)
  * strongly relevant : same root cause AND same component
  * 40 answerable queries (5 per root cause, fixed seed) + 5 no-answer queries (service_publish_failure has no
    documented case at all; the correct behaviour downstream is to abstain, not to invent a case)

  python -m rag.build_retrieval_benchmark            # build + freeze (writes benchmarks/ and the SHA-256)
  python -m rag.build_retrieval_benchmark --check    # a fresh build must equal the frozen file
"""
import argparse
import csv
import json
import random
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from .build_benchmark import sha256  # noqa: E402
from .corpus import load_corpus  # noqa: E402

BENCH_DIR = ROOT / "benchmarks"
BENCH_PATH = BENCH_DIR / "rag_retrieval_benchmark_v2.json"
HASH_PATH = BENCH_DIR / "rag_retrieval_benchmark_v2.sha256"
SEED = 20261005
N_PER_CAUSE = 5
NO_ANSWER_ROOT = "service_publish_failure"
DENSE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

PRE_REGISTERED = dict(
    query_variants=dict(primary="symptom_only", declared_ablation=["symptom_component_environment"],
                        note="the declared variant appends 'Component: <name>. Environment: <type>.' to the symptom"),
    retrievers=dict(
        bm25=dict(primary=dict(granularity="document", k1=1.5, b=0.75),
                  ablation=dict(granularity="section", k1=1.5, b=0.75, aggregation="max chunk score per document")),
        dense=dict(model="BAAI/bge-small-en-v1.5", library="sentence-transformers", similarity="cosine",
                   normalize_embeddings=True, query_prefix=DENSE_QUERY_PREFIX, document_prefix="",
                   primary=dict(granularity="document"),
                   ablation=dict(granularity="section", aggregation="max similarity per document"))),
    primary_comparison="bm25 / document / symptom_only  vs  dense / document / symptom_only",
    rule="Specification, queries, relevance judgments and configurations are frozen before the first run. "
         "No parameter, prefix, prompt or query is tuned afterwards, whichever retriever wins. Ablations are "
         "reported for discussion and never replace the primary configuration.")

METRICS = dict(
    unit="document", retrieval_depth=10, k_values=[1, 3, 5],
    levels=dict(relevant="same true root cause", strongly_relevant="same true root cause AND same component"),
    root_cause_level=dict(
        hit_at_k="1 if any relevant document is in the top k",
        recall_at_k="STANDARD recall: |relevant in top k| / |relevant|",
        capped_recall_at_k="|relevant in top k| / min(|relevant|, k): 1.0 means the top k are filled with relevant documents",
        precision_at_k="|relevant in top k| / k", mrr="1 / rank of the first relevant document (0 if none in the top 10)"),
    case_similarity_level=dict(
        strong_hit_at_k="1 if any strongly relevant document is in the top k",
        strong_mrr="1 / rank of the first strongly relevant document"),
    abstention_retrieval_diagnostic=dict(
        definition="AUROC of the top-1 retrieval score for separating answerable (positive) from no-answer queries",
        note="retrievers always return documents; this only measures whether their top score carries a signal. "
             "Generator-level abstention accuracy is a separate, later benchmark"),
    aggregation="macro average over the 40 answerable queries; the 5 no-answer queries are reported separately",
    uncertainty="queries sharing the same symptom text are perfectly correlated, so compare_runs.py reports a paired "
                "CLUSTER bootstrap (clusters = distinct symptom texts) with 95% intervals; the effective sample is the "
                "number of distinct texts, not 40")

DISCLOSURE = ("rag-retrieval-v1.0 (R1-R5) saturated under BM25 because its questions share the wording of the guide "
              "titles and the template-generated corpus. After observing that limitation, v2.0 was designed as an "
              "audited symptom-to-case retrieval benchmark. The v2.0 specification was frozen before either BM25 or "
              "dense retrieval was run on it. R1-R5 remain in use for the answer-generation benchmark.")


def _limitations(ans):
    """Deterministic facts about query diversity. The incidents' symptom_summary comes from a small set of templates,
    so many queries share the same text; this caps what the benchmark can distinguish."""
    from itertools import combinations
    groups = defaultdict(list)
    for q in ans:
        groups[q["query_symptom_only"]].append(q)
    conflicting = [t for t, g in groups.items() if len({tuple(x["strongly_relevant"]) for x in g}) > 1]

    def upper_bound(k):
        """Best possible mean strong-hit@k for ANY retriever that sees only the symptom text."""
        total = 0
        for g in groups.values():
            cands = sorted({d for x in g for d in x["strongly_relevant"]})
            best = max(sum(any(d in x["strongly_relevant"] for d in combo) for x in g)
                       for combo in combinations(cands, min(k, len(cands))))
            total += best
        return round(total / len(ans), 4)

    return dict(
        n_answerable_queries=len(ans),
        distinct_texts_symptom_only=len(groups),
        distinct_texts_symptom_component_environment=len({q["query_symptom_component_environment"] for q in ans}),
        symptom_texts_with_conflicting_strong_sets=len(conflicting),
        symptom_only_upper_bound=dict(strong_hit_at_1=upper_bound(1), strong_hit_at_3=upper_bound(3)),
        cause="symptom_summary is generated from 2-4 templates per root cause (frozen data layer), so several "
              "queries are textually identical",
        consequences=[
            "the effective sample size of the primary comparison is the number of distinct symptom texts, not 40",
            "root-cause-level metrics are unaffected by duplicates (identical text implies identical root cause)",
            "with symptom text only, strong (component-level) relevance cannot be fully resolved: the upper bounds "
            "above cap Strong-Hit@k for every retriever; the declared component+environment variant lifts that cap",
            "this was found by the unit tests after the first freeze and BEFORE any retriever was run; the disclosure "
            "was added and the benchmark re-frozen once; no query and no relevance judgment changed"])


def build():
    truth = {r["incident_id"]: r for r in csv.DictReader(open(ROOT / "ground_truth/incidents_truth.csv", encoding="utf-8"))}
    manifest = list(csv.DictReader(open(ROOT / "ground_truth/doc_manifest.csv", encoding="utf-8")))
    corpus_ids = {d.doc_id for d in load_corpus()}
    con = sqlite3.connect(f"file:{(ROOT / 'data/delivery.db').as_posix()}?mode=ro", uri=True)
    pub = {r[0]: r[1:] for r in con.execute(
        """SELECT i.incident_id, i.symptom_summary, i.component_id, c.name, d.env_type, i.status
           FROM incidents i JOIN components c ON c.component_id=i.component_id
           JOIN deployments d ON d.deployment_id=i.deployment_id""")}
    docs_by_root = defaultdict(list)
    for r in manifest:
        docs_by_root[r["root_cause"]].append(r)
    nodoc = sorted(i for i, t in truth.items() if t["has_doc"] == "0")

    def query(n, kind, inc):
        symptom, comp_id, comp_name, env, status = pub[inc]
        root = truth[inc]["true_root_cause"]
        rel = sorted(f"historical_cases/{r['incident_id']}" for r in docs_by_root[root])
        strong = sorted(f"historical_cases/{r['incident_id']}" for r in docs_by_root[root] if r["component_id"] == comp_id)
        assert set(rel) <= corpus_ids
        return dict(id=f"Q{n:02d}", kind=kind, incident_id=inc, incident_status=status, true_root_cause=root,
                    component_id=comp_id, component=comp_name, env_type=env,
                    query_symptom_only=symptom,
                    query_symptom_component_environment=f"{symptom} Component: {comp_name}. Environment: {env.replace('_', ' ')}.",
                    relevant=rel, strongly_relevant=strong)

    queries, n = [], 0
    for root in sorted(docs_by_root):
        comps = {r["component_id"] for r in docs_by_root[root]}
        pool = [i for i in nodoc if truth[i]["true_root_cause"] == root and pub[i][1] in comps]
        pick = sorted(random.Random(f"{SEED}-{root}").sample(pool, N_PER_CAUSE))
        for inc in pick:
            n += 1
            queries.append(query(n, "answerable", inc))
    for inc in [i for i in nodoc if truth[i]["true_root_cause"] == NO_ANSWER_ROOT]:
        n += 1
        queries.append(query(n, "no_answer", inc))

    doc = dict(benchmark_version="rag-retrieval-2.0", supersedes="rag-retrieval-1.0 (saturated)", frozen=True,
               selection_seed=SEED, disclosure=DISCLOSURE,
               corpus=dict(n_docs=len(corpus_ids)), pre_registered=PRE_REGISTERED, metrics=METRICS,
               known_limitations=_limitations([q for q in queries if q["kind"] == "answerable"]), queries=queries)
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def freeze(path=BENCH_PATH, hash_path=HASH_PATH):
    data = build()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    hash_path.write_text(f"{sha256(data)}  {path.name}\n", encoding="utf-8")
    return sha256(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.check:
        same = build() == BENCH_PATH.read_bytes() and sha256(BENCH_PATH.read_bytes()) == HASH_PATH.read_text().split()[0]
        print("frozen benchmark matches a fresh build" if same else "MISMATCH")
        raise SystemExit(0 if same else 1)
    print("frozen sha256:", freeze())


if __name__ == "__main__":
    main()
