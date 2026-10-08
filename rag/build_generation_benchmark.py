"""Specification of the answer-generation benchmark (rag-generation-1.0), built and FROZEN before any LLM call.

15 questions: 5 knowledge questions (R1-R5 of rag-1.0), 5 answerable symptom questions (from rag-retrieval-2.0),
5 no-matching-case questions (Q41-Q45 of rag-retrieval-2.0).

Order of work (no LLM is involved until the spec is frozen):
  1. classify every R1-R5 fact by the evidence it needs (guide / case / aggregate)
  2. CONTEXT-SUFFICIENCY AUDIT: for a grid of (cases, guides) lane sizes, check whether the evidence each question
     needs is present in the retrieved context. The final lane sizes are the smallest sufficient configuration, chosen
     by this audit and NOT by answer quality.
  3. freeze questions, facts, the selected configuration, prompts, checks and the rubric with a SHA-256.

  python -m rag.build_generation_benchmark            # audit, build, freeze
  python -m rag.build_generation_benchmark --audit    # print the audit table only
  python -m rag.build_generation_benchmark --check    # a fresh build must equal the frozen file
"""
import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import generate_data as g  # noqa: E402
from . import build_benchmark as b1  # noqa: E402
from . import build_retrieval_benchmark as b2  # noqa: E402
from .build_benchmark import load_frozen, sha256  # noqa: E402
from .corpus import load_corpus  # noqa: E402
from .lanes import TwoLaneRetriever  # noqa: E402

BENCH_PATH = ROOT / "benchmarks" / "rag_generation_benchmark_v1.json"
HASH_PATH = ROOT / "benchmarks" / "rag_generation_benchmark_v1.sha256"
SEED = 20261006
GRID_CASES, GRID_GUIDES = (1, 2, 3, 5), (1, 2, 3)

GUIDE_OF_ROOT = {v[1]: f"troubleshooting_guides/{name[:-3]}" for name, v in g.TROUBLESHOOTING.items()}

GENERATOR_SYSTEM = (
    "You are a troubleshooting assistant for enterprise project delivery. Answer ONLY from the documents provided in "
    "the user message.\n"
    "Rules:\n"
    "1. After every factual statement cite the document it comes from with its id in square brackets, exactly as "
    "written in the document header, for example [troubleshooting_guides/crs_mismatch].\n"
    "2. Documents whose id starts with historical_cases/ describe PAST incidents. Call a past case similar only if its "
    "symptoms and root cause genuinely match the new situation.\n"
    "3. If none of the provided cases genuinely matches, say explicitly that no documented matching case was found. "
    "You may still give the general checks recommended by the troubleshooting or deployment guides in the context.\n"
    "4. Never state a root cause as established unless a cited document supports it for this exact situation.\n"
    "5. Do not use knowledge from outside the documents. Keep the answer under 200 words.")
GENERATOR_USER = "Documents:\n\n{context}\n\nQuestion: {question}"
DOC_BLOCK = "### Document id: {doc_id}\n{text}"

JUDGE_SYSTEM = (
    "You are a strict evaluator of answers written by a retrieval-augmented assistant. You receive the question, the "
    "documents that were available to the assistant, the answer, and a checklist. Judge ONLY against the documents "
    "provided. Reply with one JSON object and nothing else. Use 1 for yes and 0 for no. 'items' maps every checklist "
    "item id to 0 or 1. 'unsupported_claims' is the number of statements in the answer that the provided documents do "
    "not support (0 if every statement is supported).")
JUDGE_USER = ("Question:\n{question}\n\nDocuments available to the assistant:\n\n{context}\n\nAnswer to evaluate:\n"
              "{answer}\n\nChecklist (id: meaning):\n{checklist}\n\nReturn JSON: "
              "{{\"items\": {{<id>: 0 or 1, ...}}, \"unsupported_claims\": <integer>}}")

PRE_REGISTERED = dict(
    retrieval=dict(retriever="bm25 (pure Python, k1=1.5, b=0.75)", granularity="document",
                   lanes="cases = historical_cases only; guides = troubleshooting_guides + deployment_guides",
                   context_order="cases first (by rank), then guides (by rank); full document text",
                   knowledge_query="the question text",
                   symptom_query="the frozen rag-retrieval-2.0 symptom + component + environment text",
                   note="Separate lanes were motivated by the post-hoc diagnostic of rag-retrieval-2.0 (top-1 misses "
                        "were guides ranked above cases). The symptom query follows the pre-declared enriched variant "
                        "of v2.0 because an engineer reporting an incident supplies the component and environment; "
                        "this choice is informed by v2.0 results and is disclosed as such."),
    generator=dict(model="gemini-3.5-flash-lite", system=GENERATOR_SYSTEM, user_template=GENERATOR_USER,
                   doc_block=DOC_BLOCK, citation_format="[doc_id]", sampling="provider defaults",
                   rule="One model per run; a different model is allowed only for availability reasons and is recorded "
                        "as a deviation, never because of scores."),
    judge=dict(role="SECONDARY (scalability experiment, never the source of truth)", model="gemini-3.8-flash",
               system=JUDGE_SYSTEM, user_template=JUDGE_USER, calls="one per answer"),
    scoring=dict(
        primary="machine checks + human rubric on ALL 15 answers",
        secondary="frozen LLM judge on the same 15 answers; agreement with the human rubric is reported "
                  "(raw agreement; Cohen's kappa only when both raters use both labels)",
        no_composite_score=True,
        reported_per_group=["fact coverage with the evidence split", "citation validity", "claims about precedent",
                            "unsupported claims", "sample size n"]),
    evidence_split=dict(
        covered="the fact is in the answer and its evidence was in the context",
        missed_context_available="the fact is missing although its evidence was in the context (generation failure)",
        missed_evidence_not_retrieved="the fact is missing and its evidence was NOT in the context (retrieval failure)",
        covered_without_context_evidence="the fact is in the answer although its evidence was not in the context "
                                         "(flag: possible use of outside knowledge)"),
    machine_checks=[
        "every cited id exists in the retrieved context (invalid citations are counted)",
        "answers contain at least one citation",
        "symptom questions: at least one cited case belongs to the relevant set (same true root cause)",
        "symptom questions: cited cases outside the relevant set are counted as wrong precedents",
        "no-matching-case questions: any cited historical case or any INC_ id mentioned is flagged for the human rubric"],
    human_rubric=dict(
        knowledge="per testable fact: covered (1/0); unsupported_claims (count)",
        symptom="similar_case_stated (1/0); advice_consistent (1/0); unsupported_claims (count)",
        no_matching_case="precedent_claimed (1/0, correct = 0); root_cause_asserted (1/0, correct = 0); "
                         "guide_checks_given (1/0); unsupported_claims (count)"),
    disclosures=[
        "Lane design was decided after the rag-retrieval-2.0 post-hoc diagnostic.",
        "Lane sizes come from a context-sufficiency audit that uses no LLM output.",
        "The 5 symptom questions cover only 5 of the 8 documented root causes; this is not a full-category test.",
        "Aggregate facts (corpus-level counts or shares, e.g. '9 documented cases', 'cases resolved with X (5)') cannot "
        "be supported by a top-k context; they are listed but excluded from the coverage denominators and belong to the "
        "Hybrid (SQL + RAG) benchmark.",
        "Case facts were narrowed before the first model call to claims a SINGLE retrieved relevant case can support "
        "(the build verifies that every relevant case supports every case fact); for resolution-pattern facts, naming "
        "at least one listed approach is enough.",
        "The system prompt states the desired behaviour explicitly (including how to treat non-matching cases); the "
        "benchmark measures compliance with that prompt, not behaviour under a neutral prompt."])


def _classify(qid, fact, primary):
    s = fact["statement"]
    if s.startswith(("Guide", "Checklist item")):
        # every guide fact is a step / fix / checklist item of the question's first primary guide; the claim is
        # verified against that guide's own text in _verify_guide_facts (the evidence mapping can never drift again)
        clean = s.replace(" (slow-query guide)", "")
        return dict(fact, kind="guide_fact", evidence_docs=[primary[0]],
                    **({"source_statement": s} if clean != s else {}), statement=clean)
    if s.startswith(("Finding:", "Documented cases", "Fixes applied", "Yes:")):
        return dict(fact, kind="case_fact", evidence_rule="at least one relevant historical case in the context")
    if "of the cases first appeared" in s:
        return dict(fact, kind="aggregate_fact", evidence_rule="needs corpus-level aggregation (Hybrid)")
    raise ValueError(f"unclassified fact: {fact['id']} {s}")


_PREFIX_WORDS = {"guide", "fix", "check", "step", "checklist", "item"}


def _tokens(text):
    import re
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _verify_guide_facts(questions, corpus):
    """Each guide fact must be contained in the text of its evidence guide (token containment >= 0.85)."""
    text = {d.doc_id: d.text for d in corpus}
    for q in questions:
        for f in q["facts"]:
            if f["kind"] != "guide_fact":
                continue
            need = _tokens(f["statement"].split(":", 1)[1]) - _PREFIX_WORDS
            have = _tokens(text[f["evidence_docs"][0]])
            share = len(need & have) / len(need)
            assert share >= 0.85, f"{f['id']} is not supported by {f['evidence_docs'][0]} ({share:.2f}): {f['statement']}"


def _supports(fact, text):
    """Does this case document support the fact? all_of markers must all occur; if any_of is given, at least one."""
    ev = fact["evidence"]
    return all(m in text for m in ev.get("all_of", [])) and (not ev.get("any_of") or any(m in text for m in ev["any_of"]))


def _rewrite_case_facts(q, text):
    """Case facts become claims that a SINGLE retrieved relevant case can support. Corpus-level counts are kept as
    separate aggregate facts (not scored here; they belong to the Hybrid benchmark). Nothing here looks at model output."""
    import re
    patterns = sorted({m for d in q["relevant_cases"] for m in re.findall(r"Solution pattern: (\w+)", text[d])})
    out = []
    for f in q["facts"]:
        if f["kind"] != "case_fact":
            out.append(f)
            continue
        src = f["statement"]
        base = dict(id=f["id"], kind="case_fact", source_statement=src)
        agg = dict(id=f"{f['id']}-agg", kind="aggregate_fact", statement=src,
                   evidence_rule="needs corpus-level aggregation (Hybrid)")
        if f["id"] in ("R1-f5", "R2-f6", "R3-f3"):
            what = "Documented comparable cases were resolved with approaches such as: " + "; ".join(
                g.FIX_TEXT[p].rstrip(".") for p in patterns) + "."
            out += [dict(base, statement=what, evidence=dict(any_of=[f"Solution pattern: {p}" for p in patterns]),
                         evidence_rule="a retrieved relevant case shows at least one of the listed approaches; "
                                       "naming at least one is enough, counts are not required"), agg]
        elif f["id"] == "R3-f1":
            out += [dict(base, statement="At least one documented timeout case occurred in a distributed deployment.",
                         evidence=dict(all_of=["Root cause: timeout", "Environment: distributed"]),
                         evidence_rule="a retrieved relevant case is a timeout case in a distributed environment"), agg]
        elif f["id"] == "R3-f2":
            out.append(dict(base, statement="Documented case investigations found that requests exceeded the configured "
                                            "timeout while upstream services were still processing.",
                            evidence=dict(all_of=["exceeding the configured timeout", "still processing"]),
                            evidence_rule="a retrieved relevant case contains this investigation finding"))
        else:
            raise ValueError(f"no rewrite rule for case fact {f['id']}")
    return out


def _verify_case_facts(questions, text):
    """Every relevant case must support every case fact of its question, so ANY single retrieved relevant case does."""
    for q in questions:
        for f in q["facts"]:
            if f["kind"] == "case_fact":
                for d in q["relevant_cases"]:
                    assert _supports(f, text[d]), f"{f['id']} is not supported by relevant case {d}"


def build_questions():
    v1, _ = load_frozen(b1.BENCH_PATH, b1.HASH_PATH)
    v2, _ = load_frozen(b2.BENCH_PATH, b2.HASH_PATH)
    qs = []
    for c in v1["cases"]:
        qs.append(dict(
            id=f"G{len(qs) + 1:02d}", group="knowledge", source=f"rag-1.0:{c['id']}", question=c["question"],
            retrieval_query=c["question"], primary_guides=c["primary"],
            relevant_cases=[d for d in c["relevant"] if d.startswith("historical_cases/")],
            facts=[_classify(c["id"], f, c["primary"]) for f in c["facts"]]))
        qs[-1]["facts"] = _rewrite_case_facts(qs[-1], {d.doc_id: d.text for d in load_corpus()})
    answerable = [q for q in v2["queries"] if q["kind"] == "answerable"]
    roots = sorted({q["true_root_cause"] for q in answerable})
    chosen_roots = sorted(random.Random(f"{SEED}-roots").sample(roots, 5))
    for root in chosen_roots:
        pool = sorted((q for q in answerable if q["true_root_cause"] == root), key=lambda q: q["id"])
        q = random.Random(f"{SEED}-{root}").choice(pool)
        qs.append(_symptom_question(qs, q, "symptom_answerable", GUIDE_OF_ROOT.get(root)))
    for q in [q for q in v2["queries"] if q["kind"] == "no_answer"]:
        qs.append(_symptom_question(qs, q, "no_matching_case", GUIDE_OF_ROOT.get(q["true_root_cause"])))
    corpus = load_corpus()
    _verify_guide_facts(qs, corpus)
    _verify_case_facts(qs, {d.doc_id: d.text for d in corpus})
    return qs, dict(root_causes_covered=chosen_roots, root_causes_total=len(roots))


def _symptom_question(qs, q, group, matching_guide):
    text = (f"A new incident was reported on the {q['component']} in a {q['env_type'].replace('_', ' ')} environment: "
            f"\"{q['query_symptom_only']}\" Have we seen this before, and what should we check or do?")
    return dict(id=f"G{len(qs) + 1:02d}", group=group, source=f"rag-retrieval-2.0:{q['id']}", question=text,
                retrieval_query=q["query_symptom_component_environment"], incident_id=q["incident_id"],
                true_root_cause=q["true_root_cause"], component_id=q["component_id"],
                relevant_cases=q["relevant"], strongly_relevant_cases=q["strongly_relevant"],
                matching_guide=matching_guide, facts=[])


def _available(question, ctx, text_by_id):
    """Which pieces of required evidence are in the retrieved context (no LLM involved)."""
    cases = [d for d in ctx if d.startswith("historical_cases/")]
    out = dict(facts={}, relevant_case=any(d in question["relevant_cases"] for d in cases),
               strong_case=any(d in question.get("strongly_relevant_cases", []) for d in cases),
               matching_guide=question.get("matching_guide") in ctx if question.get("matching_guide") else None)
    for f in question["facts"]:
        if f["kind"] == "guide_fact":
            out["facts"][f["id"]] = all(d in ctx for d in f["evidence_docs"])
        elif f["kind"] == "case_fact":
            out["facts"][f["id"]] = any(d in question["relevant_cases"] and _supports(f, text_by_id[d]) for d in cases)
    return out


def audit(questions, corpus):
    words = {d.doc_id: len(d.text.split()) for d in corpus}
    text = {d.doc_id: d.text for d in corpus}
    rows = []
    for kc in GRID_CASES:
        for kg in GRID_GUIDES:
            r = TwoLaneRetriever(corpus, kc, kg)
            per = {q["id"]: _available(q, r.retrieve(q["retrieval_query"]), text) for q in questions}
            ctx_words = [sum(words[d] for d in r.retrieve(q["retrieval_query"])) for q in questions]
            testable = [(q["id"], fid) for q in questions for fid in per[q["id"]]["facts"]]
            ok_facts = sum(per[qid]["facts"][fid] for qid, fid in testable)
            know = [q for q in questions if q["group"] == "knowledge"]
            sym = [q for q in questions if q["group"] == "symptom_answerable"]
            nom = [q for q in questions if q["group"] == "no_matching_case"]
            crit = dict(
                knowledge_facts=f"{ok_facts}/{len(testable)}",
                symptom_relevant_case=f"{sum(per[q['id']]['relevant_case'] for q in sym)}/{len(sym)}",
                no_match_guide=f"{sum(bool(per[q['id']]['matching_guide']) for q in nom)}/{len(nom)}",
                symptom_strong_case_info=f"{sum(per[q['id']]['strong_case'] for q in sym)}/{len(sym)}")
            sufficient = (ok_facts == len(testable) and all(per[q["id"]]["relevant_case"] for q in sym)
                          and all(per[q["id"]]["matching_guide"] for q in nom))
            rows.append(dict(cases=kc, guides=kg, total=kc + kg, mean_context_words=round(sum(ctx_words) / len(ctx_words)),
                             sufficient=sufficient, **crit))
    ok = [r for r in rows if r["sufficient"]]
    pick = min(ok, key=lambda r: (r["total"], r["mean_context_words"], r["cases"])) if ok else None
    return rows, pick


def build():
    corpus = load_corpus()
    questions, coverage = build_questions()
    rows, pick = audit(questions, corpus)
    if pick is None:
        raise SystemExit("no configuration in the grid is sufficient; review the questions or the grid before freezing")
    doc = dict(
        benchmark_version="rag-generation-1.0", frozen=True, selection_seed=SEED,
        depends_on=dict(knowledge="rag-1.0 (R1-R5)", symptom="rag-retrieval-2.0",
                        corpus_sha256=(ROOT / "benchmarks/corpus.sha256").read_text().split()[0]),
        context_sufficiency_audit=dict(
            rule="smallest total number of documents (ties: fewer words, then fewer cases) such that (a) every guide "
                 "fact has its guide in the context, (b) every case fact is supported by a retrieved relevant case (marker check on its text), (c) every "
                 "answerable symptom question has at least one relevant case, (d) every no-matching-case question has "
                 "its troubleshooting guide. No answer quality is used.",
            grid=rows, selected=dict(cases=pick["cases"], guides=pick["guides"]),
            declared_ablation=dict(cases=3, guides=2, status="optional; reported next to the primary configuration, "
                                   "never substituted for it")),
        symptom_coverage=coverage, pre_registered=PRE_REGISTERED, questions=questions)
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def freeze():
    data = build()
    BENCH_PATH.write_bytes(data)
    HASH_PATH.write_text(f"{sha256(data)}  {BENCH_PATH.name}\n", encoding="utf-8")
    return sha256(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    if args.audit:
        corpus = load_corpus()
        questions, _ = build_questions()
        rows, pick = audit(questions, corpus)
        print(f"{'cases':>5}{'guides':>7}{'total':>6}{'words':>7}  {'facts':>8} {'sym case':>9} {'nomatch guide':>14} {'sym strong*':>12}  sufficient")
        for r in rows:
            print(f"{r['cases']:>5}{r['guides']:>7}{r['total']:>6}{r['mean_context_words']:>7}  {r['knowledge_facts']:>8} "
                  f"{r['symptom_relevant_case']:>9} {r['no_match_guide']:>14} {r['symptom_strong_case_info']:>12}  {r['sufficient']}")
        print("* informational only, not part of the sufficiency rule")
        print("selected:", pick and (pick["cases"], pick["guides"]))
        return
    if args.check:
        same = build() == BENCH_PATH.read_bytes() and sha256(BENCH_PATH.read_bytes()) == HASH_PATH.read_text().split()[0]
        print("frozen generation benchmark matches a fresh build" if same else "MISMATCH")
        raise SystemExit(0 if same else 1)
    print("frozen sha256:", freeze())


if __name__ == "__main__":
    main()
