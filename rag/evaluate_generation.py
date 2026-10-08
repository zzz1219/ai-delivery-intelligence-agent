"""Scoring of the answer-generation benchmark. PRIMARY = machine checks + the human rubric on all 15 answers.

  1. python -m rag.evaluate_generation sheet --run eval_results\\gen_<...>.json
        -> writes rubric_<run>.csv (fill the human_score column) and rubric_<run>.md (answers + context, reading aid)
  2. python -m rag.evaluate_generation score --run eval_results\\gen_<...>.json --human rubric_<run>.csv
        -> prints and saves the summary (no composite score; every number comes with its n)
  3. optional secondary: python -m rag.judge --run ...  then  score ... --judge judge_<run>.csv  (agreement with the human)

Human scores: 1 = yes, 0 = no for every item; the item 'unsupported_claims' is a count (0, 1, 2, ...).
"""
import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from .build_generation_benchmark import _available  # noqa: E402
from .corpus import load_corpus, verify_corpus  # noqa: E402
from .build_benchmark import load_frozen  # noqa: E402
from .build_generation_benchmark import BENCH_PATH, HASH_PATH  # noqa: E402

CITE = re.compile(r"\[((?:historical_cases|troubleshooting_guides|deployment_guides)/[A-Za-z0-9_]+)\]")
CASE_ID = re.compile(r"INC_\d{4}")
COUNT_ITEM = "unsupported_claims"
BEHAVIOUR_ITEMS = {
    "symptom_answerable": [("similar_case_stated", "The answer points to at least one documented similar historical case"),
                           ("advice_consistent", "The recommended actions are consistent with the cited documents")],
    "no_matching_case": [("precedent_claimed", "The answer claims that a documented similar historical case exists "
                                               "(the correct value is 0)"),
                         ("root_cause_asserted", "The answer states a root cause as established (the correct value is 0)"),
                         ("guide_checks_given", "The answer gives general checks taken from a troubleshooting guide in the context")]}


def load_run(path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not doc["meta"].get("complete"):
        raise SystemExit("this run is incomplete (provider errors, circuit breaker or abort); incomplete runs are never scored")
    return doc


def checklist(question):
    """Items the human (and the judge) score for one question: [(item_id, text, kind)]."""
    items = []
    if question["group"] == "knowledge":
        for f in question["facts"]:
            if f["kind"] in ("guide_fact", "case_fact"):
                extra = " (naming at least one of the listed approaches is enough)" if f.get("evidence", {}).get("any_of") else ""
                items.append((f["id"], f"The answer covers: {f['statement']}{extra}", "fact"))
    for item_id, text in BEHAVIOUR_ITEMS.get(question["group"], []):
        items.append((item_id, text, "behaviour"))
    items.append((COUNT_ITEM, "Number of statements in the answer that the provided documents do not support", "count"))
    return items


def machine_checks(spec, run, text_by_id=None):
    text_by_id = text_by_id or {d.doc_id: d.text for d in load_corpus()}
    out = {}
    answers = {a["id"]: a for a in run["answers"]}
    for q in spec["questions"]:
        a = answers[q["id"]]
        ctx = a["context_ids"]
        cited = list(dict.fromkeys(CITE.findall(a["answer"])))
        cited_cases = [c for c in cited if c.startswith("historical_cases/")]
        r = dict(cited=cited, invalid_citations=[c for c in cited if c not in ctx], has_citation=bool(cited),
                 n_citation_marks=len(CITE.findall(a["answer"])))
        avail = _available(q, ctx, text_by_id)
        r["evidence_in_context"] = avail["facts"]
        if q["group"] == "symptom_answerable":
            r["cited_relevant_case"] = any(c in q["relevant_cases"] for c in cited_cases)
            r["wrong_precedents"] = [c for c in cited_cases if c not in q["relevant_cases"]]
            r["relevant_case_in_context"] = avail["relevant_case"]
        if q["group"] == "no_matching_case":
            r["cited_any_case"] = bool(cited_cases)
            r["mentions_case_id"] = bool(CASE_ID.search(a["answer"]))
            r["precedent_flag"] = r["cited_any_case"] or r["mentions_case_id"]
            r["matching_guide_in_context"] = bool(avail["matching_guide"])
        out[q["id"]] = r
    return out


def write_sheet(spec, run, checks, out_dir, stem, text_by_id=None, force=False):
    out_dir = Path(out_dir)
    csv_path, md_path = out_dir / f"rubric_{stem}.csv", out_dir / f"rubric_{stem}.md"
    if csv_path.exists() and not force:
        scored = [r for r in csv.DictReader(open(csv_path, encoding="utf-8-sig")) if (r.get("human_score") or "").strip()]
        if scored:
            raise SystemExit(f"{csv_path.name} already contains {len(scored)} scores; refusing to overwrite them "
                             "(use --force only if you really want a blank sheet)")
    text_by_id = text_by_id or {d.doc_id: d.text for d in load_corpus()}
    answers = {a["id"]: a for a in run["answers"]}
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["question_id", "item_id", "item_text", "machine_note", "human_score", "notes"])
        for q in spec["questions"]:
            for item_id, text, kind in checklist(q):
                note = ""
                if kind == "fact":
                    note = "evidence_in_context=" + str(checks[q["id"]]["evidence_in_context"].get(item_id))
                if item_id == "precedent_claimed":
                    note = "machine flag (cited or mentioned a case id)=" + str(checks[q["id"]]["precedent_flag"])
                w.writerow([q["id"], item_id, text, note, "", ""])
    lines = ["# Human rubric reading aid", "",
             "Fill the `human_score` column of the CSV: 1 = yes, 0 = no for every item; `unsupported_claims` is a count.",
             "Judge ONLY against the documents that were in the context. Do not use your own knowledge.", ""]
    for q in spec["questions"]:
        a = answers[q["id"]]
        lines += [f"## {q['id']} ({q['group']})", f"**Question:** {q['question']}", "",
                  f"**Retrieved context:** {', '.join(a['context_ids'])}", "", "**Answer:**", "", a["answer"], "",
                  "**Documents the assistant saw (judge ONLY against these):**", ""]
        for d in a["context_ids"]:
            lines += [f"<details><summary>{d}</summary>", "", "```", text_by_id[d].strip(), "```", "", "</details>", ""]
        lines.append("**Items:**")
        lines += [f"- `{i}`: {t}" for i, t, _ in checklist(q)]
        agg = [f for f in q["facts"] if f["kind"] == "aggregate_fact"]
        if agg:
            lines += [f"- (not scored, aggregate fact for the Hybrid benchmark: {f['statement']})" for f in agg]
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return csv_path, md_path


def read_scores(path, spec, column="human_score"):
    """Returns {(question_id, item_id): int}. Refuses blanks, non-integers and non-binary values."""
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    expected = {(q["id"], i): kind for q in spec["questions"] for i, _, kind in checklist(q)}
    got = {}
    for r in rows:
        key = (r["question_id"], r["item_id"])
        raw = (r.get(column) or "").strip()
        if key not in expected:
            raise SystemExit(f"unknown item {key} in {path}")
        if raw == "":
            raise SystemExit(f"{column} is blank for {key}; all 15 answers must be fully scored")
        try:
            value = int(raw)
        except ValueError:
            raise SystemExit(f"{column} for {key} must be an integer, got {raw!r}")
        if value < 0 or (expected[key] != "count" and value not in (0, 1)):
            raise SystemExit(f"{column} for {key} must be 0 or 1 (count items: 0 or more), got {value}")
        got[key] = value
    missing = set(expected) - set(got)
    if missing:
        raise SystemExit(f"{len(missing)} items missing in {path}, e.g. {sorted(missing)[0]}")
    return got


def summarize(spec, run, checks, human):
    qs = {q["id"]: q for q in spec["questions"]}
    s = {}
    # knowledge: fact coverage with the evidence split
    split = Counter()
    per_q = {}
    for qid, q in qs.items():
        if q["group"] != "knowledge":
            continue
        n = cov = 0
        for f in q["facts"]:
            if f["kind"] == "aggregate_fact":
                split["aggregate_not_scored"] += 1
                continue
            has = checks[qid]["evidence_in_context"][f["id"]]
            covered = human[(qid, f["id"])] == 1
            n += 1
            cov += covered
            split["covered" if covered and has else "covered_without_context_evidence" if covered
                  else "missed_context_available" if has else "missed_evidence_not_retrieved"] += 1
        per_q[qid] = f"{cov}/{n}"
    testable = sum(v for k, v in split.items() if k != "aggregate_not_scored")
    s["knowledge"] = dict(n_questions=len(per_q), testable_facts=testable, per_question=per_q,
                          evidence_split=dict(split),
                          coverage=f"{split['covered'] + split['covered_without_context_evidence']}/{testable}")
    sym = [qid for qid, q in qs.items() if q["group"] == "symptom_answerable"]
    s["symptom_answerable"] = dict(
        n=len(sym), cited_relevant_case_machine=sum(checks[i]["cited_relevant_case"] for i in sym),
        answers_with_wrong_precedent_machine=sum(bool(checks[i]["wrong_precedents"]) for i in sym),
        similar_case_stated_human=sum(human[(i, "similar_case_stated")] for i in sym),
        advice_consistent_human=sum(human[(i, "advice_consistent")] for i in sym),
        relevant_case_in_context=sum(checks[i]["relevant_case_in_context"] for i in sym),
        note="covers 5 of the 8 root causes only")
    nom = [qid for qid, q in qs.items() if q["group"] == "no_matching_case"]
    s["no_matching_case"] = dict(
        n=len(nom), precedent_claimed_human=sum(human[(i, "precedent_claimed")] for i in nom),
        precedent_flag_machine=sum(checks[i]["precedent_flag"] for i in nom),
        root_cause_asserted_human=sum(human[(i, "root_cause_asserted")] for i in nom),
        guide_checks_given_human=sum(human[(i, "guide_checks_given")] for i in nom),
        abstention_correct=sum(human[(i, "precedent_claimed")] == 0 and human[(i, "root_cause_asserted")] == 0 for i in nom),
        matching_guide_in_context=sum(checks[i]["matching_guide_in_context"] for i in nom))
    marks = sum(c["n_citation_marks"] for c in checks.values())
    bad = sum(len(c["invalid_citations"]) for c in checks.values())
    uns = [human[(qid, COUNT_ITEM)] for qid in qs]
    s["all_answers"] = dict(n=len(qs), answers_without_citation=sum(not c["has_citation"] for c in checks.values()),
                            citation_marks=marks, answers_with_invalid_citation=sum(bool(c["invalid_citations"]) for c in checks.values()),
                            invalid_distinct_citations=bad, unsupported_claims_total=sum(uns),
                            answers_with_unsupported_claims=sum(u > 0 for u in uns))
    s["note"] = "no composite score; report each line with its n and the declared limitations"
    return s


def kappa(a, b):
    """Cohen's kappa for two lists of 0/1. None when either rater uses a single label (kappa is undefined)."""
    n = len(a)
    if n == 0 or len(set(a)) < 2 or len(set(b)) < 2:
        return None
    po = sum(x == y for x, y in zip(a, b)) / n
    pe = sum((a.count(v) / n) * (b.count(v) / n) for v in (0, 1))
    return None if pe == 1 else round((po - pe) / (1 - pe), 3)


_DESIRED_ZERO = {"precedent_claimed", "root_cause_asserted"}


def agreement(spec, human, judge):
    """Item-level agreement between the human rubric and the judge, on binary decisions.
    Polarity is aligned so that 1 always means the desired outcome (precedent_claimed, root_cause_asserted and
    'any unsupported claim' are desired when 0). Raw agreement is pooled; Cohen's kappa is computed per item kind only,
    because kappa pooled over kinds with different base rates is misleading."""
    kinds = {"fact": ([], []), "behaviour": ([], []), "unsupported_any": ([], [])}
    for q in spec["questions"]:
        for item_id, _, kind in checklist(q):
            h, j = human[(q["id"], item_id)], judge[(q["id"], item_id)]
            if kind == "count":
                kinds["unsupported_any"][0].append(int(h == 0))
                kinds["unsupported_any"][1].append(int(j == 0))
            elif item_id in _DESIRED_ZERO:
                kinds[kind][0].append(1 - h)
                kinds[kind][1].append(1 - j)
            else:
                kinds[kind][0].append(h)
                kinds[kind][1].append(j)
    allh = sum((g[0] for g in kinds.values()), [])
    allj = sum((g[1] for g in kinds.values()), [])
    rep = {"overall": dict(n=len(allh), raw_agreement=round(sum(x == y for x, y in zip(allh, allj)) / len(allh), 3),
                           kappa="not reported (pooled kappa over different item kinds is misleading)")}
    for k, (h, j) in kinds.items():
        if h:
            rep[k] = dict(n=len(h), raw_agreement=round(sum(x == y for x, y in zip(h, j)) / len(h), 3), kappa=kappa(h, j))
    rep["note"] = ("kappa is reported per item kind and only when both raters use both labels; with n this small it is "
                   "indicative only. The human rubric stays the source of truth; the judge is a secondary experiment.")
    return rep


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("sheet", "score"):
        p = sub.add_parser(name)
        p.add_argument("--run", required=True)
        if name == "sheet":
            p.add_argument("--force", action="store_true", help="overwrite a sheet that already contains scores")
        if name == "score":
            p.add_argument("--human", required=True)
            p.add_argument("--judge")
    args = ap.parse_args()
    spec, _ = load_frozen(BENCH_PATH, HASH_PATH)
    run = load_run(args.run)
    if run["meta"]["spec_sha256"] != load_frozen(BENCH_PATH, HASH_PATH)[1]:
        raise SystemExit("the run was produced with a different generation spec")
    if run["meta"].get("corpus_sha256") != verify_corpus(load_corpus()):
        raise SystemExit("the run was produced on a different document corpus")
    checks = machine_checks(spec, run)
    stem = Path(args.run).stem
    if args.cmd == "sheet":
        c, m = write_sheet(spec, run, checks, Path(args.run).parent, stem, force=args.force)
        print(f"rubric sheet: {c}\nreading aid : {m}\nFill human_score for every row (1/0; unsupported_claims is a count).")
        return
    human = read_scores(args.human, spec)
    result = dict(run=Path(args.run).name, model=run["meta"]["model"], lanes=run["meta"]["lanes"],
                  summary=summarize(spec, run, checks, human))
    if args.judge:
        result["judge_agreement"] = agreement(spec, human, read_scores(args.judge, spec, "judge_score"))
    out = Path(args.run).parent / f"score_{stem}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"\nsaved: {out}")


if __name__ == "__main__":
    main()
