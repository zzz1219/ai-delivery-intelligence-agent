"""POST-HOC diagnostic of retrieval result files. Not part of the frozen scoring; it never replaces a metric.

  python -m rag.diagnose_misses eval_results\\rag2_bm25_<ts>.json eval_results\\rag2_dense_<ts>.json

For the PRIMARY run of each file it answers three questions from the saved top-10 lists:
  1. When the top-1 document is not a relevant case, what is it? (guide of the same root cause / another guide /
     a case with the wrong root cause)
  2. Hypothetical "case-only" ranking: remove guides from the saved list and re-score. This is NOT what was run;
     it only shows how much of the Hit@1 / MRR gap is due to guides ranking above cases.
  3. For the no-matching-case queries: what do the retrievers put first?
  4. Hypothetical "case-only" abstention signal: AUROC of the top-1 CASE score (guides ignored) for separating
     answerable from no-matching-case queries. Tells whether a score gate over case documents could work at all.
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import generate_data as g  # noqa: E402
from . import metrics as m  # noqa: E402

GUIDE_ROOT = {f"troubleshooting_guides/{name[:-3]}": v[1] for name, v in g.TROUBLESHOOTING.items()}


def _kind(doc_id, true_root):
    if doc_id in GUIDE_ROOT:
        return "guide, same root cause" if GUIDE_ROOT[doc_id] == true_root else "guide, other root cause"
    if doc_id.startswith("deployment_guides/"):
        return "deployment guide"
    return "case"


def diagnose(doc):
    prim = next(r for r in doc["runs"] if r["role"] == "PRIMARY")
    ans = [q for q in prim["queries"] if q["kind"] == "answerable"]
    out = dict(retriever=doc["meta"]["retriever"], n=len(ans))
    miss = [q for q in ans if q["hit@1"] == 0]
    out["top1_misses"] = len(miss)
    out["miss_types"] = dict(Counter(_kind(q["retrieved"][0]["doc_id"], q["true_root_cause"]) if q["retrieved"] else "empty"
                                     for q in miss))
    h1 = mrr = s1 = smrr = 0.0
    for q in ans:
        cases = [r for r in q["retrieved"] if r["doc_id"].startswith("historical_cases/")]
        first = next((i for i, r in enumerate(cases, 1) if r["relevant"]), None)
        sfirst = next((i for i, r in enumerate(cases, 1) if r["strongly_relevant"]), None)
        h1 += first == 1
        mrr += 1 / first if first else 0
        s1 += sfirst == 1
        smrr += 1 / sfirst if sfirst else 0
    out["case_only"] = dict(hit_at_1=round(h1 / len(ans), 3), mrr=round(mrr / len(ans), 3),
                            strong_hit_at_1=round(s1 / len(ans), 3), strong_mrr=round(smrr / len(ans), 3))
    out["frozen_primary"] = {k: prim["macro"][k] for k in ("hit@1", "mrr", "strong_hit@1", "strong_mrr")}
    noans = [q for q in prim["queries"] if q["kind"] == "no_answer"]
    out["no_matching_case_top1"] = dict(Counter(_kind(q["retrieved"][0]["doc_id"], q["true_root_cause"]) for q in noans))

    def top_case_score(q):
        return next((r["score"] for r in q["retrieved"] if r["doc_id"].startswith("historical_cases/")), 0.0)
    au = m.auroc([top_case_score(q) for q in ans], [top_case_score(q) for q in noans])
    out["case_only_top1_auroc"] = None if au is None else round(au, 3)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    args = ap.parse_args()
    print("POST-HOC DIAGNOSTIC (not part of the frozen scoring)\n")
    for f in args.files:
        d = diagnose(json.load(open(f, encoding="utf-8")))
        print(f"== {d['retriever']}  ({Path(f).name})")
        print(f"  top-1 misses on {d['n']} answerable queries: {d['top1_misses']}  ->  {d['miss_types']}")
        print(f"  frozen primary          : {d['frozen_primary']}")
        print(f"  hypothetical case-only  : {d['case_only']}   (guides removed from the saved top-10)")
        print(f"  no-matching-case top-1  : {d['no_matching_case_top1']}")
        print(f"  case-only top-1 score AUROC (answerable vs no-matching-case): {d['case_only_top1_auroc']}\n")


if __name__ == "__main__":
    main()
