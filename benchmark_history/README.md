# benchmark_history: index

This folder is the evidence trail of the benchmarks. **Nothing here is renamed or moved**, because sealed traces, the demo converter and the tests reference some of these paths (for example `results_hybrid_v1/hyb_gemini_gemini-3_5-flash-lite_20261007_004003.json`). This file only says which file is which.

## Reading rules
- **Frozen results are the results.** A benchmark file, its first real run and its final human scoring are fixed once sealed. Later analysis never replaces them.
- **Post-hoc analyses are diagnostics only.** Anything computed after seeing a result (for example, attributing a miss to the retriever or to the query, or re-checking documents the model never saw) is reported next to the frozen result and labelled post-hoc. It does not change a frozen number.
- **Proposed / intermediate files are kept for traceability.** They show how a rubric or an evaluator changed; they are not the final score.
- All runs are single runs of one model (`gemini-3.5-flash-lite`) with provider default sampling, on synthetic data (seed 58).

## Top level
| File | Status | What it is |
|---|---|---|
| `CHANGELOG.md` | record | Chronological log of benchmark versions, defects found and fixes |
| `evaluation_cases_v1.0.json` | frozen (SQL benchmark v1.0 cases) | The five SQL evaluation cases of the first SQL run; the corrected specification is v1.1 (see the changelog) |
| `generation_v1_results_summary.md` | final summary | Answer-generation results, with post-hoc diagnostics labelled |
| `hybrid_v1_results_summary.md` | final summary | Hybrid-pipeline results, failure analysis, post-hoc diagnostics labelled |

## `results_v1.0/` : SQL agent, first real run
| File | Status |
|---|---|
| `sql_gemini_gemini-3_5-flash-lite_20261005_232005.json` | **First real SQL run** (executed 5/5, strict 3/5, loose 4/5). Error analysis found two benchmark/evaluator defects; the specification was then fixed as v1.1 and the unchanged agent re-evaluated. This is not an agent improvement. |

## `results_generation_v1/` : answer generation (generation-1.0)
| File | Status |
|---|---|
| `gen_..._c1g2_20261006_204010.json` | **First real generation run** (15 answers; sealed) |
| `rubric_gen_..._204010.csv` | intermediate: original rubric before human revision |
| `rubric_gen_..._204010_revised_step1_G14_only.csv` | intermediate: first revision step (G14 only) |
| `rubric_gen_..._204010_revised.csv` | **final human rubric** (all revisions applied; see the log) |
| `score_gen_..._204010.json` | intermediate: score of the original rubric |
| `score_gen_..._204010_revised_step1_G14_only.json` | intermediate: score after step 1 |
| `score_gen_..._204010_revised.json` | **final score** (reported in the README and the summary) |
| `rubric_revision_log.json` | why each rubric change was made (the rater changed the unsupported-claim count for G14 and G06 from 0 to 1) |

## `results_hybrid_v1/` : hybrid pipeline (hybrid-1.0)
| File | Status |
|---|---|
| `hyb_..._20261007_004003.json` | **First real hybrid run** (5 questions; sealed). The five sealed demo traces H1 to H5 point to this exact path. **Never rename.** |
| `rubric_hyb_..._004003.csv`, `.md` | blank rubric as generated, before scoring |
| `rubric_hyb_..._004003_PROPOSED_by_external_reviewer.csv` | proposed: scores suggested by an external reviewer, not final |
| `score_hyb_..._004003_PROPOSED.json` | proposed / intermediate score of the proposed rubric |
| `rubric_hyb_..._004003_FINAL.csv` | **final human rubric**: the 55 proposed scores adopted by the author with one change (H3 unsupported claims 1 → 0). Not an independent judgement of the author alone; stated as a limitation |
| `score_hyb_..._004003_FINAL.json` | **final score** (reported in the README and the summary) |
| `rubric_revision_log.json` | provenance, the one change and the reasons for kept decisions |
| `README.md` | **outdated**: written before the final scoring and still describes the proposed rubric as "not final". Where it disagrees with this index or with `hybrid_v1_results_summary.md`, the summary and the `_FINAL` files are right. It is kept unchanged as a historical note. |

## Where the other benchmark evidence is
- Frozen benchmark files and their hashes: `../benchmarks/` (each `.json` has a `.sha256`; the `rag.build_*` scripts rebuild and compare).
- Intermediate retrieval runs (BM25 variants, rag-retrieval runs): `../eval_results/`. Kept for traceability; the frozen retrieval result is the one in the README and the changelog.
- The recorded demo runs: `../traces/` (A1 to A4 were recorded live once with the frozen pipeline on 2026-10-07; H1 to H5 are converted from the sealed hybrid run above).
