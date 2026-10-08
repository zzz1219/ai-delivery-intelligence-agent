# Answer-generation benchmark v1.0 - final results (SEALED)

Configuration: spec `rag-generation-1.0` (sha256 bbfa1cf1...), BM25 two-lane retrieval (1 case + 2 guides), generator
`gemini-3.5-flash-lite`, provider default sampling, one run, 15/15 answers. Human rubric on all 15 answers (revised file is final;
original kept). No LLM judge run yet.

## Formal results (frozen scoring; every line with its n)
| Item | Result |
|---|---|
| Run completion | 15/15 answers |
| Citation validity (machine) | 93 citation marks, all pointing to documents that were in the retrieved context; 0 invalid; 0 answers without a citation. Whether a cited document actually supports the sentence was NOT machine-checked; only the human unsupported-claim count covers that |
| Knowledge fact coverage (5 questions) | 21/29 (72.4%): G01 4/5, G02 3/6, G03 2/6, G04 6/6, G05 6/6 |
| Evidence availability | 29/29 scorable facts had their evidence in the context; therefore all 8 misses are `missed_context_available` (a benchmark taxonomy label), none is a retrieval-side miss |
| Answerable symptom questions (5, covering 5 of 8 root causes) | 5/5 cited a relevant historical case (machine); 0 wrong precedents; human: similar case stated 5/5, advice consistent 5/5 |
| No-matching-case (5 responses, 4 distinct question texts: G11 = G15) | precedent claimed 0/5; root cause asserted as established 0/5; guide checks given 5/5 |
| Unsupported claims (human) | 3 in 15 answers: G01 (over-narrow similarity judgment), G06 (states that the new incident's root cause matches the case's) and G14 (meta-claim derived from the system rule) |

## Post-hoc interpretation (not part of the frozen scoring; does not change any number above)
- Seven of the eight misses concerned diagnostic or remedial details that were present in the frozen rubric but were not explicitly
  requested by the surface wording of G02/G03 (and R1-f5's neighbours). The 72% coverage should therefore be read as completeness
  against the benchmark checklist, not simply as task-answer correctness. G04/G05, whose questions request the guide content, scored 12/12.
- The eighth miss (R1-f5) and the G01 unsupported claim have the same cause: the model made an overly narrow similarity judgment
  under the prompt's "genuinely match" instruction (it rejected a 15M-record case for a 10M-record question). The prompt did not
  define that threshold; this is prompt ambiguity combined with the model's interpretation, not a prompt bug alone.
- The deterministic precedent flag fired on 4 of the 5 no-matching answers; all four were semantic false positives (the answer names
  the retrieved case only to exclude it). Deterministic rules were intentionally high-recall and human review resolved them.
- "8 generation-side misses" is a benchmark taxonomy, not "8 wrong answers": 7 of them are incomplete restatements of the checklist.

## Limitations (to be stated wherever these numbers are used)
One run, one model, provider default sampling (generation is stochastic; retrieval, context and scoring specifications are
reproducible); 15 questions; symptom questions cover 5 of 8 root causes; the no-matching group has only 4 distinct texts; the system
prompt states the desired behaviour explicitly, so this measures compliance with that prompt; the human rater is the project author;
the benchmark was frozen before the run but its lane design was informed by an earlier post-hoc diagnostic (disclosed).

## Rubric revisions (final)
G14 and G06 were revised from 0 to 1 unsupported claims by the rater, with the same standard (a statement not established by the retrieved documents), after a sweep of all 15 answers for root-cause / similarity-criterion statements; only these two contained one. Original, intermediate and final rubric files are kept; the revisions were made after the cross-check, by the project author, who is also the rater.

## Lessons carried into the Hybrid benchmark
1. Required facts map one-to-one to what the question explicitly asks; useful extra content is listed as optional/bonus evidence and
   reported separately, never in the primary coverage denominator.
2. Define operational thresholds (what counts as a "similar" case) in the prompt before freezing, e.g. same failure mode and component
   class; a different data volume does not make a case dissimilar.
3. No duplicate question texts; audit the benchmark's distinct-text count before freezing.
4. Keep machine checks high-recall and let human review resolve semantic false positives.
5. Separate "citation exists in context" (machine) from "citation supports the sentence" (human or checked rule).
