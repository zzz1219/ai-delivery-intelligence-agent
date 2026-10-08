# Hybrid benchmark hybrid-1.0 - final results (SEALED)

Configuration: benchmark `hybrid-1.0` (sha256 08a19e42...), pipeline `hybrid-pipeline-1.0` (sha256 5448dd4e...), every stage `gemini-3.5-flash-lite`,
provider default sampling, one run, 5 questions. Rubric: 55 items; the scores were first proposed by someone other than the rater, verified against the
run, and adopted by the rater (the project author) with one change (H3 unsupported_claims 1 -> 0). Final files: `results_hybrid_v1/*_FINAL*`.

## Formal results (frozen contract; every line with its n)
| Stage | Result |
|---|---|
| Run | complete, 5/5 questions answered, query status ok 5/5, 0 plan failures |
| Route compliance | 5/5 plans equal the expected plan. Weak evidence: all five questions expect the same plan, so an always-hybrid planner would also score 5/5 (no controls) |
| SQL stage (20 sql_facts) | machine screening matched 16; human confirmed 16 (H2 2, H4-s4, H5-s3 rejected). Machine and human disagree on 2 facts by design: H5-s3 (machine yes, human no: the task has no project filter, the right rows are a data coincidence) and H1-s3 (derived boolean, human only) |
| Retrieval stage (6 doc_facts) | evidence in the agent's context 4/6; in the oracle-query context 6/6 |
| Frozen failure attribution | covered 3, propagated_from_sql 3 (H2-d1, H5-d-INC_0185, H5-d-INC_0192) |
| Synthesis stage | SQL facts stated correctly 17/20; doc facts covered 4/6; synthesis requirements met 4/4 (H4 causation boundary; H5 no-precedent for INC_0188 and INC_0200) |
| Citations | 0 invalid [doc_id] or [sql:task] citations; 0 answers without a citation; 0 unsupported-number flags |
| Unsupported claims (human) | 0 in 5 answers |

## Post-hoc diagnostics (do not replace the frozen numbers)
Result-based attribution (a SQL dependency propagates only when the SQL RESULT was wrong): covered 4, propagated_from_sql 1 (H2-d1), query_formulation_failure 1 (H5-d-INC_0185).
| Item | Mechanism |
|---|---|
| H2 | SQL semantic error: the planner rewrote the question's definition of a repeat incident as "pairs", the SQL counted unordered pairs (crs_mismatch 37; gold: distinct incidents, timeout 25); the answer is consistent with its wrong SQL and the retrieved case, so it is not a generation hallucination |
| H4 | the planner restricted the quarter task to 2025 Q4, 2026 Q1, 2026 Q2; 2026 Q3 (14) was never computed. Not separable between planner interpretation and the benchmark wording (see defect 1). Everything else correct: 32 of 40 for v3.2, association not causation, a concrete failure mode (INC_0070) |
| H5 INC_0185 | query-formulation failure: the model's query contained the project name, BM25 returned a P03 Tile Cache TIMEOUT case (INC_0028), the model rightly did not call it similar; the oracle context contains the real tile-cache precedent (INC_0026). The statement "no documented similar case was found" is honest relative to the retrieved context but false relative to the corpus |
| H5 INC_0192 | successful retrieval (INC_0049) and synthesis |
| H5 INC_0188 / INC_0200 | no precedent claimed, no root cause asserted: the feared false-precedent effect of the relaxed similarity rule did not appear |

## Specification defects found (disclosed, nothing corrected after the run)
1. H4-s4 is a frozen benchmark-specification defect: 2026 Q3 was required by the rubric although it was not explicitly requested by the surface wording ("surrounding quarters"; Q3 is not adjacent to Q1). Scored sqlok 0 / says 0 under the frozen contract.
2. The frozen attribution rule marks a doc fact `propagated_from_sql` whenever a SQL dependency has sqlok = 0, even when the rejection concerns the METHOD of a task whose RESULT was right (H5-s3). It hid INC_0185's query-formulation failure and mislabelled INC_0192.
3. SQL tasks cannot see each other's results, so a task such as "for the identified project" cannot be executed; the planner prompt asks for self-contained tasks and the planner did not comply for H5 t2.
4. Route compliance without controls is not routing accuracy.

## Limitations (state wherever these numbers are used)
One run, one model, provider default sampling (stochastic generation; specifications, contexts and scoring are reproducible); 5 questions, a smoke-level benchmark; H5 is a deliberately selected
diagnostic slice, not a representative sample; the rubric scores were proposed by an external reviewer and adopted by the author after verification, so they are not an independent judgement; prompts state
the desired behaviour explicitly (association vs causation, no invented precedent); SQL results must come at the granularity the question asks for.

## Lessons for the next iteration
1. Required facts must follow the question's wording (H4-s4 repeats the generation lesson).
2. Do not let the planner paraphrase definitions: pass defined terms verbatim into SQL task descriptions (H2).
3. Make tasks self-contained or pass earlier results into dependent tasks (H5 t2).
4. Keep entity names that are not failure vocabulary (project names) out of BM25 retrieval queries (INC_0185); consider hybrid lexical + dense retrieval.
5. Score a SQL task's method and its result separately so that attribution can tell them apart.
6. Add SQL-only, RAG-only and direct-answer controls before reporting any routing metric.
