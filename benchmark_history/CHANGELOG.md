# Benchmark history (SQL questions S1-S5)

## v1.0 - first real-model run
Model: gemini-3.5-flash-lite, one run, complete (5/5 scored, 0 errors).
Result: executed 5/5, strict 3/5, loose 4/5.
Files: `evaluation_cases_v1.0.json`, `results_v1.0/sql_gemini_gemini-3_5-flash-lite_20261005_232005.json`.

Manual error analysis of S4 and S5 (the agent's SQL was re-executed on the frozen database):
- S5: the agent answered correctly, but as fractions (0.034 ...) while the gold values are percentages (3.4 ...).
  The scorer compared the raw numbers. This was an evaluator defect.
- S4: the agent returned only the top row (LIMIT 1), which is a valid answer to "which environment type has the
  most ...", but strict scoring wanted the full ranking. In addition, its SQL filtered `root_cause='timeout'`
  before counting deployments, so the denominator was "deployments with a timeout" instead of "all deployments":
  single_node would have been 2.4 instead of 2.0. The top-1 answer (distributed, 9.75) was right only because all
  four distributed deployments had timeouts. The question left the denominator ambiguous.

## v1.1 - benchmark specification fixed, agent unchanged
- S4 wording: "For each environment type, report the average number of timeout incidents per deployment, using all
  deployments of that environment type as the denominator, and identify which environment type has the highest rate."
- S5 wording: "For each severity level, what percentage of that severity's incidents are still open?"
- Every SQL case now has a scoring contract (kind, key column, value-column hints, tolerance, accepted scales):
  S1 top1 (strict = entity + count), S2/S3/S5 full table, S4 full table + highest row (loose = highest returned row).
  Only S5 (a proportion metric) accepts fraction or percent; the measure column is chosen by uniqueness or by name,
  never guessed.
- Result records now store the returned columns and the first 20 rows.
- Unchanged and verified by checksum: data, documents, reference SQL, gold values, agent code, prompts.

How to report: "Initial evaluation: 3/5 strict. Error analysis found two benchmark/evaluator issues (S5 scale
normalisation; S4 top-1 vs full ranking and an ambiguous denominator). After correcting the benchmark
specification, the UNCHANGED agent was re-evaluated on v1.1." Do not describe v1.1 as an agent improvement.

## rag-retrieval-2.0 - symptom-to-similar-case retrieval benchmark (frozen before any retriever ran)
Why: rag-retrieval-1.0 (R1-R5) saturated under BM25 (all metrics 1.0) because its questions repeat the guide titles.
After seeing that, v2.0 was designed as an audited benchmark: 40 answerable queries (5 per root cause, seed 20261005)
from the public `symptom_summary` of incidents WITHOUT their own case document, 5 no-answer queries
(service_publish_failure, no documented case exists), two relevance levels (same root cause; same root cause + same
component), pre-registered BM25 and dense (BAAI/bge-small-en-v1.5) configurations, standard AND capped recall.
Freeze history (both before any retriever was run on v2.0):
  1. sha256 4ab77c25...: first freeze.
  2. sha256 30673a85...: the unit tests showed that the 40 queries contain only 20 distinct symptom texts (the frozen
     data layer generates symptom_summary from 2-4 templates per root cause). Added `known_limitations` (distinct text
     counts, conflicting strong sets, upper bounds for symptom-only strong-hit) and changed the uncertainty method to a
     cluster bootstrap. Queries, relevance judgments and pre-registered configurations are byte-identical.
First BM25 run (primary: document + symptom_only): Hit@1 0.55, Hit@3 1.00, MRR 0.775, Strong-Hit@1 0.375 (upper
bound for symptom-only 0.775), abstention AUROC 0.57. Post-hoc diagnostic (not part of the scoring): most Hit@1
misses are troubleshooting GUIDES ranked above the case documents.

### rag-retrieval-2.0 - evaluator fix and errata (benchmark file NOT changed, hash 30673a85... unchanged)
Evaluator fix: result rows used the symptom-only text as the bootstrap cluster for every query variant. Now each row
stores `query_text` and clusters on the text of the variant that was run (20 clusters for symptom_only, 35 for
symptom_component_environment). The primary comparison was never affected. BM25 was rerun after the fix (it is
deterministic): every metric and every retrieved list is identical to the first run; use
`rag2_bm25_20261005_160314.json`. The first file (`..._155400.json`) is kept for traceability; compare_runs refuses it
for the ablation variant.
Test fix: the dense-prefix test could never fail; it now records the encode() batches and checks that documents are
embedded without the prefix and queries with it (verified by a mutation check).

Errata for statements inside the frozen file (not edited, because editing would change the hash):
- "root-cause-level metrics are unaffected by duplicates" should read: identical symptom texts never carry conflicting
  root-cause labels. Duplicates DO change the weighting of the macro averages (they are averages over 40 incidents,
  not over 20 distinct texts). That is intended (the population is incidents); uncertainty intervals use the cluster
  bootstrap over distinct texts.
- The 5 "no_answer" queries (service_publish_failure) have no documented similar historical CASE, but a troubleshooting
  GUIDE for that failure exists (`service_publication_failures`; BM25 ranks it first for all five). In human-readable
  text call them "no_matching_case" queries (the code field stays `no_answer`). The later generation benchmark must NOT
  require "I don't know"; it must require: (1) no claim that a documented similar case exists, (2) no unrelated case
  presented as evidence, (3) no root cause asserted as established, and it MAY (and should) point to the guide's
  general checks. Abstention is defined as refusing to claim a documented matching case, not refusing the question.

### rag-retrieval-2.0 - corpus provenance (benchmark file, queries, gold and configs unchanged; hash 30673a85...)
The benchmark hash protected the questions and gold, but nothing protected the 71 documents the retrievers actually
search. Added:
- `benchmarks/corpus.sha256` (corpus fingerprint 7bc608d5...): SHA-256 over every document id + text, registered once
  BEFORE the first dense run. The corpus was verified to be byte-identical to a fresh deterministic regeneration of the
  frozen data layer (seed 58). The fingerprint ignores CRLF/LF differences, but any content edit, or a different number
  of documents, changes it.
- `rag.evaluate_retrieval_v2` refuses to run if the corpus differs from the registered one and writes `corpus_sha256`
  (and the real document count) into every result file.
- `rag.compare_runs` refuses to compare files with different or missing corpus fingerprints, different benchmark
  hashes, or a different query text / cluster for the same query id; its wording now says "clusters = distinct query
  texts of the selected variant".
BM25 was rerun once more so that its file carries the fingerprint: all metrics and all retrieved lists are identical to
the previous BM25 run. Use `rag2_bm25_20261005_<latest>.json` (the earlier BM25 files are kept for traceability and are
refused by compare_runs).

### rag-retrieval-2.0 - first results (BM25 vs dense, primary configuration: document + symptom_only)
Files: `rag2_bm25_20261005_161007.json`, `rag2_dense_20261006_195238.json` (local BAAI/bge-small-en-v1.5).
| metric | BM25 | dense | B - A (paired cluster bootstrap 95%, 20 clusters) |
|---|---|---|---|
| hit@1 | 0.550 | 0.350 | -0.200 [-0.442, +0.000] |
| hit@3 | 1.000 | 0.950 | -0.050 [-0.143, +0.000] |
| mrr | 0.775 | 0.646 | -0.129 [-0.270, -0.013] |
| recall@5 | 0.644 | 0.587 | -0.057 [-0.119, -0.010] |
| strong_hit@1 | 0.375 | 0.150 | -0.225 [-0.412, -0.051] |
| strong_hit@3 | 0.775 | 0.700 | -0.075 [-0.216, +0.043] |
| strong_mrr | 0.610 | 0.454 | -0.156 [-0.282, -0.041] |
Reading (the rule fixed in advance: an interval containing 0 means "no reliable difference"): every point estimate
favours BM25; the intervals exclude 0 for MRR, Recall@5, Strong-Hit@1 and Strong-MRR; Hit@1 is borderline (upper bound
exactly 0); Hit@3 and Strong-Hit@3 show no reliable difference. No metric favours dense. Caveats: 7 metrics, intervals
not adjusted for multiple comparisons, no single headline metric was pre-declared, 20 independent units only.
Abstention diagnostic (AUROC of the top-1 score): BM25 0.57, dense 0.13 (inverted: a guide for the failure exists and
nearly repeats the query, so no-matching-case queries score HIGH). Top-1 score does not indicate a documented case.
Post-hoc diagnostic (`rag.diagnose_misses`, not part of the frozen scoring): for BM25 all 18 top-1 misses are guides
(16 of the same root cause, 2 other), none is a case with a wrong root cause; with guides removed from the saved top-10
(hypothetical, never run) BM25 would score Hit@1 1.00 / MRR 1.00 / Strong-Hit@1 0.675. The root-cause-level Hit@1 gap is
therefore mostly "guide ranked above case", not case-matching quality.
Decision rule (declared before reading the intervals): use dense downstream only if it is reliably better; otherwise
use the simpler deterministic BM25. Result: BM25 (document granularity) is the retriever for the answer-generation
baseline. v2.0 is not modified; no dense parameter is tuned.

#### Post-hoc diagnostic, both retrievers (`rag.diagnose_misses`; hypothetical, not part of the frozen scoring)
| | BM25 | dense |
|---|---|---|
| top-1 misses (of 40) | 18 (16 guide same cause, 2 other guide, 0 wrong-cause case) | 26 (23 guide same cause, 3 other guide, 0 wrong-cause case) |
| case-only Hit@1 / MRR (guides removed from the saved top-10) | 1.00 / 1.00 | 0.95 / 0.95 |
| case-only Strong-Hit@1 / Strong-MRR | 0.675 / 0.773 | 0.525 / 0.678 |
| no-matching-case top-1 | guide, same cause (5 of 5) | guide, same cause (5 of 5) |
Reading: for both retrievers the top-1 misses are guides, never cases with the wrong root cause. Most of the Hit@1/MRR gap
(0.55 vs 0.35 frozen) shrinks to 1.00 vs 0.95 once guides are ignored, so it mostly reflects how often a retriever ranks a
guide above the case documents. At the component level (strong relevance) BM25 keeps a lead (0.675 vs 0.525), no interval
computed. BM25 case-only top-1 score separates answerable from no-matching-case queries almost perfectly (AUROC 0.99) but
there are only 5 negatives, scores are unnormalised and any threshold would be calibrated on the same queries: not usable
as a gate yet.
Consequence for the pipeline (a design decision for the NEW answer-generation benchmark, not a change to v2.0): retrieve
case documents and guides separately. This choice was motivated by the post-hoc diagnostic and is disclosed as such.

## rag-generation-1.0 - answer-generation benchmark (frozen before any LLM call; sha256 a10cf963...)
15 questions: 5 knowledge (R1-R5), 5 answerable symptom questions (5 of the 8 root causes only), 5 no-matching-case
questions (Q41-Q45 of rag-retrieval-2.0). Counting note: an earlier message said "10 questions"; the correct number is 15.
Context-sufficiency audit (no LLM involved), grid of (cases, guides) lane sizes 1/2/3/5 x 1/2/3:
- First audit run: no configuration was sufficient. Cause: an EVIDENCE-MAPPING BUG of mine. The R1 fact "after the load,
  verify indexes and statistics" was assigned to the slow-query guide, but it is step 4 of the bulk-import guide itself
  (the slow-query guide ranked 5th in the guide lane). Fix: every guide fact is mapped to the question's first primary
  guide AND verified against that guide's own text (token containment >= 0.85) at build time, so the mapping cannot drift.
- After the fix: 29/29 testable facts are available in every configuration; the no-matching-case questions need the
  troubleshooting guide for their failure, which is rank 1 for 2 of 5 queries and rank 2 for the rest => 2 guides.
  Smallest sufficient configuration: 1 case + 2 guides (3 documents, about 275 words). Selected by the audit rule, not by
  answer quality. Declared optional ablation: 3 cases + 2 guides (reported next to the primary, never substituted).
Disclosures stored in the spec: lane design decided after the rag-retrieval-2.0 post-hoc diagnostic; the symptom query uses
the pre-declared enriched variant (informed by v2.0 results); the 5 symptom questions are not a full-category test; 1 fact is
an aggregate (corpus-level share) and is excluded from the coverage denominators (it belongs to the Hybrid benchmark); exact
counts inside case facts are not required; the system prompt states the desired behaviour explicitly.
Scoring: PRIMARY = machine checks + a human rubric on ALL 15 answers; SECONDARY = frozen gemini-3.8-flash judge on the same
answers, reported as agreement with the human (kappa per item kind, only when both raters use both labels). Evidence split
for every fact: covered / missed_context_available (generation failure) / missed_evidence_not_retrieved (retrieval failure) /
covered_without_context_evidence (possible outside knowledge). No composite score.
Tooling lesson recorded: an early version pooled Cohen's kappa over different item kinds; the unit tests showed that this
inflates agreement through base-rate differences, so kappa is now per kind with aligned polarity.

### rag-generation-1.0 - re-frozen before the first model call (sha256 bbfa1cf1..., previous a10cf963...)
Pre-run audit found a mismatch between several case-fact statements and the evidence actually available under the selected
one-case context; scoring statements were narrowed to claims supportable by a retrieved case, while corpus-level counts
remain assigned to Hybrid evaluation. No model output had been observed (no LLM call had been made).
Changes (questions, lane sizes, prompts, retrieval query and scoring architecture are unchanged; the audit still selects
1 case + 2 guides with 29/29 testable facts available):
- R3-f1 "9 documented timeout cases ..." -> "At least one documented timeout case occurred in a distributed deployment";
  R3-f2 and the resolution-pattern facts R1-f5, R2-f6, R3-f3 now state only what a single case can show; their counts
  ("(5)/(3)/(2)", "9 cases") moved to aggregate facts R1-f5-agg, R2-f6-agg, R3-f1-agg, R3-f3-agg (together with R3-f4:
  5 aggregate facts, listed but not scored, for the Hybrid benchmark). Testable facts stay 29 (24 guide + 5 case).
- Every case fact now carries evidence markers checked against the case text; the build asserts that EVERY relevant case
  of the question supports the fact, so any single retrieved relevant case does (before, "a relevant case exists" was
  assumed to imply the specific evidence).
- R1-f4: the stale "(slow-query guide)" was removed (the step belongs to the bulk-import guide); the original text is kept
  in `source_statement`.
- evaluate_generation verifies the corpus fingerprint stored in the run before scoring.
Tooling: rag.judge now checkpoints after every answer, stops cleanly on a provider/quota error and continues with --resume
(same model), because the free tier allows 20 calls per day and a judge run can need up to 30 (15 answers + retries).
Reporting note: retrieval, context and scoring specifications are reproducible; model generation uses provider default
sampling and is therefore stochastic. Do not describe the generation scores as fully reproducible.

## rag-generation-1.0 - first run (gemini-3.5-flash-lite, lanes 1 case + 2 guides, PRIMARY; 15/15 answers, complete)
Files: `results_generation_v1/` (run, human rubric as filled, official score summary). Human rubric validated by the project
reader against spec bbfa1cf1...; scores are final as filled; no judge run yet.
Results (n per line; no composite score):
- knowledge (5 questions, 29 testable facts): coverage 21/29 (G01 4/5, G02 3/6, G03 2/6, G04 6/6, G05 6/6). Evidence split:
  21 covered, 8 missed_context_available, 0 missed_evidence_not_retrieved, 0 covered_without_context_evidence;
  5 aggregate facts listed, not scored.
- answerable symptom (5 questions, 5 of 8 root causes): relevant case cited 5/5 (machine), wrong precedents 0, human:
  similar case stated 5/5, advice consistent 5/5.
- no-matching-case (5 questions, only 4 distinct texts: G11 and G15 are identical): precedent claimed 0/5, root cause asserted
  0/5, guide checks given 5/5, abstention correct 5/5. The machine flag fired on 4/5 (the answer cites or names a case id).
- all answers: 93 citation marks, 0 invalid, 0 answers without a citation; unsupported claims: 1 (G01); no answer over 200 words.
Cross-check against the answer texts (post-hoc reading, not part of the scoring):
- The machine precedent flag was a FALSE POSITIVE on all 4 flagged answers: each cites the retrieved case only to say it is
  not a match (e.g. "INC_0100 involved a crs_mismatch rather than a publication failure"). The flag is deliberately
  conservative and exists for human review; the human score 0 is correct.
- Misses: 7 of the 8 are facts the question text does not ask for (R2-f3..f5 guide checks for a question about fixes; R3-f3 fixes
  and R3-f5..f7 guide checks for "have we seen it, what was found"). G04/G05, whose questions request the guide content,
  scored 12/12. The 8th, R1-f5, is different: the model rejected the closest case (INC_0006, 15M records vs 10M in the
  question) as "not matching" because rule 2 of the system prompt says a case is similar only if symptoms and root cause
  "genuinely" match; this also produced the one unsupported claim (G01: "no documented matching case"). An earlier note that
  "all 8 misses were not requested" was too clean; the correct statement is 7 + 1.
- The frozen R1-R5 required facts list everything a guide contains, not what each question asks for, so "coverage" measures
  completeness relative to the guide. Lesson for the Hybrid benchmark: required facts must follow what the question asks.
- Borderline item for the human to reconsider (original score kept until the human decides): G14 contains the clause "as
  historical cases only describe past incidents with genuinely matching symptoms and root causes", which no document says and
  which does not compare INC_0015 with the new incident; scored unsupported_claims = 0, a strict reading gives 1.
  Sensitivity: unsupported claims total 1 -> 2 (answers with any: 1 -> 2).
Limitations: single run, provider default sampling, one model, 15 questions, symptom questions cover 5 of 8 root causes, the
system prompt states the desired behaviour explicitly, the human rater is the project author. These are measurements of this
configuration on this benchmark, not a general accuracy claim.

#### rag-generation-1.0 - rubric revision, corrections and SEAL
- Rubric: G14 unsupported_claims 0 -> 1 (author's decision: the clause is derived from the system rule, not from retrieved documents).
  The original rubric file is kept; the revised file is final (`rubric_..._revised.csv`, `rubric_revision_log.json`). Unsupported claims
  total 1 -> 2; no other group changes. G06 ("matching symptoms and root cause") is recorded as an open consistency question, not applied.
- Corrections to earlier wording: (1) "93 citations" means 93 citation MARKS pointing to documents present in the context; citation
  support was only checked by the human unsupported-claim count. (2) The G01 miss is "an overly narrow similarity judgment under the
  prompt's 'genuinely match' instruction" (prompt ambiguity x model interpretation), not "the prompt applied too strictly".
  (3) The "all misses were not requested" claim stays corrected to 7 + 1. (4) "8 generation-side misses" is a taxonomy label.
- SEALED: the generation benchmark v1.0 spec (bbfa1cf1...), the run, the revised rubric and the score summary are final. No further
  edits to the benchmark, prompt or lane sizes; the declared (3 cases + 2 guides) ablation is not run for now because every missed
  fact already had its evidence in the context. Summary for reporting: `generation_v1_results_summary.md`.

#### rag-generation-1.0 - final rubric (supersedes the G14-only revision above)
G06 unsupported_claims 0 -> 1 as well (same standard as G14; consistency sweep over all 15 answers found only G06 and G14).
Unsupported claims total: 1 (original) -> 2 (G14) -> 3 (final: G01, G06, G14). Knowledge coverage 21/29, symptom 5/5, no-matching 5/5
unchanged. The final rubric is `rubric_..._revised.csv`; intermediate and original files are kept. No further revisions.

## hybrid-1.0 - Hybrid (SQL + RAG) benchmark specification (frozen before any model run; sha256 d6a8b50c...)
Five questions that need both the database and the documents; H1-H3 were rewritten because the earlier versions could be answered from
SQL alone and their anchors were fragile (16 vs 15, 11 vs 9). Required facts (sql_facts, doc_facts, synthesis_requirements) follow what each
question asks; bonus facts are reported separately and never enter the primary denominators.
- H1 closed incidents, database vs non-database mean hours (38.4 vs 16.7), root cause with the highest mean (connection_pool_exhausted 41.1,
  next 35.8), guide's first check. H2 repeat incident defined in the question text (closed, same project/component/root cause, +-90 days):
  top root cause timeout with 25 repeat incidents (next 14), recorded lesson. H3 closed critical incidents in distributed deployments: mean
  35.4 h, 8 incidents (the count is NOT in the question), most common root cause timeout (4 of 8), guide's first check.
  H4 spike in 2026-Q1 (11 / 40 / 11 / 14 across Q4 2025..Q3 2026), version associated with most Q1 incidents (v3.2, 32 of 40), required
  evidence boundary: association is stated, causation is not claimed as established; documented failure modes after the v3.2 upgrade.
  H5 project with the most open incidents (P03, 9) and its open HIGH-severity incidents in the MAP-SERVICE layer: INC_0185, INC_0188,
  INC_0192, INC_0200 = 2 incidents without any documented case (service_publish_failure) + 2 with documented cases of distinct root causes.
- H5 sampling: the planned "stratified fixed-seed sample" was replaced by a natural SQL filter that yields exactly the 2 + 2 structure;
  this was found after inspecting the open incidents of the top project (disclosed in the spec).
- Stages and information boundaries are frozen (route -> SQL -> retrieval -> synthesis; failure attribution rules; the agent never sees
  ground truth); the number of LLM calls per question is deliberately NOT frozen.
- No-LLM oracle-plan audit: gold SQL results are turned into retrieval queries by pre-registered templates; with the two-lane retriever the
  smallest sufficient configuration is 1 case + 2 guides per query (H3's guide ranks second under the template, so one guide is not enough).
- Build-time assertions: unique winners with margins (H1 >= 3 h, H2 >= 1.5x, H3 >= 2x, H5 open-incident margin >= 2), 4 distinct H5 symptom
  texts, evidence supported by every relevant case / contained in the guide, five distinct question texts; tests also check that
  question texts do not leak gold values. Not decided yet: planner/router design, prompts and the hybrid scoring code.

#### hybrid-1.0 - re-frozen before any model run (sha256 13968e27..., previous d6a8b50c...)
Review before the pipeline was written; four changes, questions, SQL gold values, lanes (1 case + 2 guides) and H1-H3/H5 untouched:
1. "Routing accuracy" renamed to ROUTE COMPLIANCE: all five questions expect the same plan, so an always-hybrid planner scores 5/5.
   The expected plan is now three booleans (requires_sql, requires_retrieval, retrieval_depends_on_sql); no SQL-only / RAG-only /
   direct-answer controls (out of scope).
2. SQL facts are bound to ONE SQL task result, never to the union of all results: entity + number must share a row (H1-s4/s5, H2-s1/s2,
   H3-s3/s4, H4-s5/s6, H5-s1/s2), a standalone number only counts in a result with at most 30 rows, the H5 id set must equal the gold set
   exactly, and every task is recorded (task id, description, SQL, columns, rows). Facts are bound by CONTENT rather than by planner-chosen
   task ids (the planner invents the ids, so name-binding would be unstable and would reject valid decompositions such as separate mean and
   count tasks). Machine matching is a screening step; the human rubric confirms each sql_fact against the recorded task.
3. Retrieval stage diagnostic: the same context rule is also run deterministically on the oracle queries (never shown to the model), so
   failures can be attributed: agent query misses + oracle query succeeds = query-formulation failure; both miss = retriever / context
   failure; evidence present but not used = synthesis failure. Taxonomy: planner / route -> SQL task -> query formulation ->
   retriever / context -> synthesis (reported in four stages, retrieval decomposed). No extra LLM call.
4. H4-d1 evidence contract: the first draft accepted the marker "shortly after the upgrade to v3.2" (a temporal remark, not a failure mode)
   as sufficient. Now the fact requires a concrete failure mode (the case's documented root cause: tile_cache_misconfig, timeout or
   crs_mismatch; all 14 Q1 map-service cases satisfy it); the temporal remark is a bonus fact. The audit still selects 1 case + 2 guides.
Wording: H5 is a deliberately selected diagnostic (challenge) slice, not a representative sample.

#### hybrid-1.0 - last pre-run fixes (sha256 08a19e42..., previous 13968e27...)
H4's question now asks for "what concrete failure mode is documented in at least one relevant historical case" (singular), matching the
at-least-one required fact; a test assertion with an operator-precedence slip (`a and b or c`) was split so it really checks that
`expected_route` is gone. Nothing else changed (the other four questions and all gold values are identical to the first freeze).

## hybrid-pipeline-1.0 - pipeline specification (frozen before any model call; sha256 778bc1ae...)
Files: `rag/build_hybrid_pipeline_spec.py` (prompts, schemas, limits, scoring contract), `rag/hybrid_pipeline.py` (runner),
`rag/evaluate_hybrid.py` (stage-wise scoring and the human rubric sheet), `rag/test_hybrid_pipeline.py` (10 tests, all with fake models).
- Flow per question: planner (sees only the question; describes structured SQL tasks, writes no SQL) -> the frozen SQL agent once per task
  (its answer step is skipped, so no extra call; the SQL agent code is pinned by a fingerprint and the pipeline refuses to run if it changed)
  -> retrieval-query generator (sees the question, the planner's purpose and the ACTUAL SQL results) -> two-lane BM25, 1 case + 2 guides per
  query -> synthesis (sees the question, the actual SQL results and the documents retrieved with the AGENT's queries; cites [doc_id] and
  [sql:<task_id>]). The oracle-query context is computed deterministically, stored for diagnostics and never shown to a model.
- Outcomes: ok / plan_failed (a planner reply that is not a valid plan after 2 attempts: a scored route failure, not an error) / error
  (provider) / skipped; two consecutive provider errors stop the run, any other exception aborts it; incomplete runs are never scored.
- Scoring per stage: route compliance; SQL screening under the frozen sql_binding rules plus human confirmation (sqlok:<fact>); evidence in the
  agent vs oracle context; failure attribution (planner_failure, propagated_from_sql, synthesis_failure, query_formulation_failure,
  retriever_context_failure, covered, covered_without_context_evidence); human items says / covered / req / unsupported_claims (55 items for 5
  questions); machine flags for invalid [doc_id] / [sql:task] citations and for numbers no SQL result supports. No composite score.
- Prompt choices recorded in the spec: synthesis rule 3 defines similarity operationally (failure mode, not data volume; lesson from the
  generation benchmark); rule 5 states association versus causation explicitly (H4 measures compliance with that prompt).
- Test evidence: information boundaries (the planner sees only the question; no gold value or oracle query in any prompt; the synthesis prompt
  contains exactly the agent's documents), content-bound SQL matching (single result, same row, <= 30 rows, exact H5 set, valid decompositions
  accepted), plan validation, lifecycle and error handling, attribution of every category. Mutation checks confirmed that union matching, an
  oracle leak into the agent context and an attribution that ignores the oracle diagnostic each make a test fail.
- Known limitation: SQL results must come at the granularity the question asks for; coarser results aggregated by the synthesis are not counted as
  SQL-stage successes.

#### hybrid-pipeline-1.0 - re-frozen before any model call (sha256 5448dd4e..., previous 778bc1ae...)
A pre-run review found a logical conflict between synthesis rule 3 and H5. Rule 3 let the model call a past case similar only if its
"symptoms and root cause" matched the incident, but the root cause of an open incident is hidden from the agent by design. For INC_0185 and
INC_0192 the model therefore had two bad options: refuse ("root cause unknown, so I cannot call it similar", failing the two required doc facts)
or infer the root cause (the unsupported claim already penalised in generation G06). New rule 3: a past case may be called similar when its
documented symptoms and failure mode are consistent with the OBSERVED symptoms; sharing only the component, project or environment does not make
a case similar (this limits false precedents for INC_0188 / INC_0200, which have no documented case) and a different data volume, project or
environment alone does not make a similar case dissimilar; when the root cause is not recorded in the SQL results (an open incident) the answer
must never state that it is established or identical to the past case's, and calls the case a symptomatically similar precedent. Rule 4 now says
"no documented similar case was found". The same weakness existed in the sealed generation prompt ("symptoms and root cause genuinely match") and
partly explains G01 and G06; the sealed benchmark is not changed.
Other changes: the record of every question now has an explicit `query_status` (ok / failed_validation / not_requested; a failed validation
still runs the synthesis with an empty context and is scored as a query-formulation failure); the fingerprint wording says "SQL agent core
(agent.py, prompts.py, tools.py)" because llm.py and evaluate.py are outside it. Hybrid benchmark 08a19e42..., questions, gold values, lanes and
the scoring contract are unchanged.

## hybrid-1.0 - first real run (gemini-3.5-flash-lite, 5/5 questions answered, complete) - verification of a PRE-FILLED rubric
Files: `results_hybrid_v1/`. The 55-item rubric was pre-filled by someone other than the rater; the assistant checked every score against the
run. The sheet is structurally identical to the blank one (same rows, texts and machine notes; only human_score and notes changed) and the
project's scorer accepts it. It stays a PROPOSAL until the project author decides the open items below.
Verified against the run:
- H1 and H3: SQL tasks correct (means 38.43 / 16.74 / 41.07 for connection_pool_exhausted; 35.44 h, 8 incidents, timeout 4), guide first checks stated, 0 invalid citations.
- H2: the SQL counted UNORDERED PAIRS of qualifying incidents (crs_mismatch 37; timeout 26) instead of the distinct repeat incidents of the question's
  own definition (timeout 25; crs_mismatch 11). The planner rewrote the definition as "find pairs ... count of repeat incidents". Consequently the
  answer (crs_mismatch, 37, the CRS lesson from INC_0015) is wrong but consistent with its SQL result and retrieved case: an upstream SQL error,
  not a generation hallucination.
- H4: the planner restricted the quarter task to 2025 Q4, 2026 Q1, 2026 Q2; 2026 Q3 (14) was never computed. 32 of 40 (v3.2), the association-not-causation
  statement and a concrete failure mode (INC_0070, tile cache; the case really mentions the v3.2 upgrade) are correct.
- H5: project P03 with 9 open incidents and the four incident ids are correct; INC_0192 found INC_0049 (CRS); INC_0185 failed: the agent's query contained the project name,
  BM25 returned INC_0028 (a P03 Tile Cache TIMEOUT case), the model rightly did not call it similar, so the answer says no similar case exists for INC_0185 while the
  oracle context (INC_0026, tile cache) contains one: a query-formulation failure. INC_0188 / INC_0200: no precedent claimed, no root cause asserted (the feared
  false-precedent effect of the relaxed similarity rule did not appear).
- Machine screening: 16 of 20 SQL facts matched; the unmatched ones are the derived boolean H1-s3, H2-s1/s2 and H4-s4; route compliance 5/5 (trivial: all five expect the same plan);
  no invalid citation; no unsupported-number flag.
Open items for the rater (recommendations only):
1. H3 unsupported_claims (proposed 1): the answer calls INC_0054 "a symptomatically similar precedent" although H3 is an aggregate question. H2 ("symptomatically similar
   historical cases") and H4 ("a symptomatically similar historical case") use the same label and were scored 0. Treat the three alike; the cited content is documented in all three.
2. H5-s3 sqlok (proposed 0): t2 has no project filter ("for the identified project" cannot be executed, the SQL agent never sees t1's result); the four rows are exactly right only
   because no other project has an open high-severity map-service incident. Decide on the merits (does the task compute the quantity), not on the attribution effect below.
3. H4-s4 (Q3 = 14) stays 0 under the frozen contract, but the question says "surrounding quarters" and Q3 2026 is not adjacent to Q1: a specification weakness of the benchmark
   (a required fact beyond the question's wording), to be disclosed and not corrected after the run.
Specification flaw found: the frozen attribution marks a doc fact `propagated_from_sql` whenever a dependency has sqlok = 0, even when the rejection concerns the METHOD of a task whose RESULT
was right. With the proposed H5-s3 = 0, the formal attribution therefore labels both H5 doc facts propagated_from_sql, hiding INC_0185's query-formulation failure and mislabelling the covered INC_0192.
A post-hoc, clearly labelled result-based attribution (`posthoc_result_based_attribution`) is reported next to the frozen one and does not replace it.

#### hybrid-1.0 - first run: FINAL rubric and SEAL
Rater's decisions: H3 unsupported_claims 1 -> 0 (the label "symptomatically similar" echoes the prompt; the cited content is documented; same treatment as H2 and H4; unlike G06 no root cause of an
unknown incident is asserted); H5-s3 sqlok kept 0; H4-s4 kept 0 / 0 and registered as a benchmark-specification defect. Final unsupported claims: 0 in 5 answers. Provenance is recorded in
`rubric_revision_log.json` (scores proposed externally, verified by the assistant, adopted by the author). The evaluator received one ADDITIVE change after the run (the post-hoc result-based
attribution); the formal fields are computed by the unchanged frozen logic and are identical. SEALED: hybrid-1.0 (08a19e42...), hybrid-pipeline-1.0 (5448dd4e...), the run, the final rubric and the
final score. Summary for reporting: `hybrid_v1_results_summary.md`. No further edits to these.
