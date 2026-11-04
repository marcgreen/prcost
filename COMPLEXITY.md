# Complexity dimensions: theory, measurements, falsification

**Status: research prototype, not a validated predictor of review time or review
quality.** The seven dimensions are permanent concepts in the model, not seven
claims that their current proxies are correct. Each dimension has (1) a theoretical
motivation, (2) an operational measurement, and (3) an empirical validation
question. These are deliberately kept separate.

`prcost score` shows all seven dimensions. `prcost score --json` emits the stable
`complexity_dimensions` array with `id`, `theory_inspiration`, `operational_proxy`,
`measured`, `measurement_confidence`, `empirical_validity`, `prior_delta_ru`, and
`limitation`. **`measurement_confidence: unassessed` is not a low-confidence estimate:
we have not yet measured reliability.** `empirical_validity: unvalidated` means
there is no held-out evidence here to support the mapping from proxy to outcome.

| Dimension | Theoretical motivation (not equivalent to measured value) | Operational proxy today | What to validate independently |
| --- | --- | --- | --- |
| **D · Novelty / depth** | Kolmogorov complexity; Bennett logical depth (resources needed to construct/explain) | zlib delta from added/removed semantic lines conditional on old file; branch-token adjustment; test weights | Predicts minutes and missed defects beyond changed semantic lines; separately evaluate risk of tiny but high-impact edits |
| **F · Familiarity** | Conditional complexity of a change given reviewer knowledge | Reviewer’s time-decayed prior authorship of file and nearby files | Predicts reviewer-specific minutes *after controlling for skill, authorship, load and independence*; test generalization to review-only experts |
| **P · Interactions** | Parameterized and combinatorial complexity of interacting components | Number and depth-weighted charge of import and historical co-change edges within changed-file clusters | Added prediction beyond number of files; manually audit hidden API, data, rollout and runtime dependencies |
| **H · Entropy** | Shannon entropy (uncertainty/distribution) | Entropy of estimated change depth within clusters and costs across clusters | Whether distribution adds predictive power after controlling size, file count and graph structure |
| **B · Blast radius** | Graph reachability / propagation | Distance-weighted non-test transitive importers | Correlates with actual behavioral impact and regression scope; compare to runtime and dataflow dependencies |
| **T · Test evidence** | Proof certificates and communication complexity (how evidence reduces verification work) | Share of changed source files related to changed tests | Does the test actually exercise changed behavior? Does coverage, mutation testing or reviewer evidence reduce time without worse escapes? |
| **R · Review regime** | Cynefin sense-making (workflow under uncertainty) | Rule-based Clear / Complicated / Complex / Chaotic routing | Predicts different verification needs or defect risk; compare to expert labels and defect outcomes, not RU |

## How the score uses the dimensions

D and P create the weighted size of each interacting cluster. H penalizes
within-cluster and across-cluster scattering. B, F and T respectively apply
blast-radius, familiarity and test co-change multipliers. A size exponent adds
an **unvalidated** superlinear prior. R chooses an advisory review workflow but
has no cost multiplier. The exact equation and weight priors are in `SPEC.md`.

The output includes **local prior deltas** for F, P, H, B and T: the full
score minus the same heuristic score with exactly one coefficient set to zero.
This includes recalculating cluster costs, size and across-cluster entropy as
necessary. A negative delta is possible, most obviously for T's discount.
These deltas **do not sum to the total**, are **not causal importance** or
predicted minutes saved, and only describe sensitivity to current *hand-set*
parameters. D does not have a meaningful independent “off” setting because
it defines the base quantity used throughout the model; R has no cost factor.
Both therefore have `prior_delta_ru: null`.

The old `factors.logic_depth` field is retained for compatibility but is a
misleading name: it means *compression-based novelty RU*, **not** Bennett
logical depth. Do not relabel it as a theoretical quantity in dashboards.

## Validation protocol: preserve a separate ledger for each dimension

1. **Measurement reliability.** Create human-reviewed examples per language,
   including moved imports, tiny permission changes, undocumented runtime
   coupling, renamed symbols, and tests that were changed but never run the
   changed code. Record proxy false positives/negatives. Version the corpus.
2. **Outcome measurements.** Prefer reviewer-reported active minutes. Use review
   touches only as a weak fallback. Separately measure queue wait, escapes,
   author overhead and review concentration. Avoid inferring elapsed duration
   from GitHub timestamps as active effort.
3. **Chronological evaluation.** Train on earlier PRs, evaluate only later
   PRs, using historical dependency graphs and commit ancestry. If predicting
   at creation time, use the *PR state at creation*, not its later final diff.
4. **Ablations.** `prcost calibrate fit` now reports held-out Spearman with
   one feature removed at a time for D/F/P/H/B/T. Compare the full model and
   the lines-changed baseline **on the same folds**. A negative full-minus-
   ablated score means the simpler model was better on that holdout.
5. **Causal claims need experiments.** Even a predictive feature does not show
   that reducing it via PR splitting lowers effort. Use controlled trials or
   credible quasi-experiments, and track review quality plus author overhead.

**Interpretation limits of the current ablations:** the D ablation removes an
additional `log_novelty_depth` feature *while retaining* `log_size`, which itself
contains D/P/H information. Thus it asks whether novel depth adds predictive
value beyond the composite size feature. P and H have the same partial-leakage
problem through `log_size`. Correlated predictors, tiny datasets and
hand-set model weights can yield unstable deltas. Confidence intervals,
repository-level replication, and conditional effect analysis are future work.
R is not one of these regression predictors: validate regimes against expert
review classifications and defect outcomes in the calibration report.

### Research ledger (do not replace missing evidence with a guessed score)

| Field | Required contents | Current value |
| --- | --- | --- |
| Operational proxy | Versioned measurement and known blind spots | Described above and in `SPEC.md` |
| Measurement reliability | Labeled examples, error rate and confidence interval | **Unassessed** |
| Incremental predictive validity | Chronological out-of-sample lift over simpler models, including ablation | **Unvalidated on real review minutes** |
| Structural or causal validity | Intervention evidence that changing the signal changes the outcome | **Unvalidated** |
| Deployment decision | Keep, refine, or retire *proxy* (not theoretical dimension), with justification | **Research only** |

The purpose is to make every disagreement testable: a proxy may fail even when
the theoretical dimension remains useful, and a good predictor may have no
causal meaning. Neither should be obscured by a single RU headline.
