# prcost 0.2: constructive review, repaired prototype, decision gates

## Executive verdict

The original 0.1 prototype is an interesting, testable *reviewability assistant*. It is not yet a defensible estimator of reviewer minutes, a correctness detector, a safe PR splitter, or a reviewer assignment engine. The most defensible outputs are **dependency reading order**, **authorship familiarity**, and **co-change gaps worth investigating**. Keep them; avoid pretending that a sum of compressed bytes proves complexity theory or that model-implied savings are realized savings.

## What works and why

1. **Per-reviewer context:** tailoring reading order to changed files and git familiarity is useful even with no fitted minute model.
2. **Graph-aware explanations:** import edges and repeated co-change reveal context that changed-line counts miss; graph gaps are prompts, never automatic fix suggestions.
3. **Reproducibility:** standard-library Python, a local git-data path for ordinary scoring, tests that construct a synthetic repository, CLI JSON output and an explicit calibration pipeline.
4. **Two separate targets:** distinguishing active effort from queue wait is necessary. Keeping escaped defects and author costs as guardrails is also good design.

## Problems in the original model

| Severity | Issue | Why it matters | Decision |
| --- | --- | --- | --- |
| P0 | Unchanged file moves could produce 0 RU | A rename can break imports, build paths, configuration and runtime discovery | Charge at least a file-level review event; never represent as zero |
| P0 | Changed imports counted as mechanical | A one-line import change can change executed code or security behavior | Treat added/removed imports as semantic until shown equivalent |
| P0 | Explicit unknown `--reviewer` silently fell back to expert estimate | Misleading reviewer cost and what-if split comparison | Treat unknown explicitly as unfamiliar and expose `known_history` |
| P0 | Splitting saves an unvalidated RU quantity | Positive estimates can encourage harmful fragmentation and context loss | What-if only; require independence and author-time checks |
| P1 | Random PR cross-validation and HEAD-built historical graphs | Leaks the future and overstates out-of-time accuracy | Default historical graphs + chronological holdouts |
| P1 | No causal identification for cost of splitting | Size elasticity may reflect confounding (type, urgency, author, review quality) | Validate with prospective randomized/natural experiments |
| P1 | `T` was framed as a test certificate | Changed tests can be vacuous, unrelated, flaky or skipped | Call it a test co-change proxy and verify coverage separately |
| P1 | Compression novelty called “logical depth” | Compression length is not computational effort required to verify intent | Treat RU as an interpretable but unvalidated heuristic |
| P1 | Cheapest reviewer optimized without queue load or skill | Overuses specialists, hurts knowledge spread and independence | Show candidates; never assign automatically |
| P1 | No reviewer familiarity could automatically force Complex, conflating team knowledge with code behavior | A solo project looked inherently emergent-risky | Show a routing warning separately; reserve Complex for code/history risk |
| P1 | Complex/Clear/Cynefin used as authoritative | Git signals can't establish certainty of systemic behavior | Keep workflow suggestions advisory, validate with outcomes |
| P2 | Right-censored newest PRs counted as having no follow-up fixes | Makes risky new changes appear safer | Exclude unobservable 14-day follow-up windows |
| P2 | Generated review guide said “safe to skim” | Low token novelty is not proof of low risk | Rename to lower-effort changes requiring verification |

## What changed in 0.2

- Treat changed Python/JS/TS import statements as semantic. Preserve unchanged moved lines as lower-effort; important source directives are not automatically comments to skip.
- Count a path-only rename as a nonzero review event.
- Correct explicit unfamiliar-reviewer requests and annotate the JSON `known_history` value.
- Make output labels and guides distinguish model assumptions from observed measurements; replace “safe to skim” and automatic split recommendations.
- Historical `calibrate fit` uses per-PR graphs and base-ancestor commit history by default. `--fast-shared-graph` is intentionally leaky and opt-in.
- Use chronological forward-chaining holdouts for both the feature model and the lines baseline.
- Right-censor follow-up-fix signals for PRs whose 14-day observation window is incomplete.
- Extend tests (20, up from 13) for import edits, tooling directives, unfamiliar reviewer, pure rename and temporal leakage; maintain original regression tests.

- Provide `score --brief` for practical reviewer-oriented output while keeping full details and JSON available.
- Decouple lack of reviewer familiarity from intrinsic code complexity; provide a separate routing warning.

## Recommended product model

The user-facing product should have three clearly separated layers:

**Layer A — evidence (can ship today):** changed-file inventory, dependency graph, test changes, co-change history, author familiarity, coverage gaps as *questions*, reviewer reading order, author-provided intent and test evidence. Provide clickable links to the relevant files/commits when integrated into a PR host.

**Layer B — heuristics (experimental):** relative score RU with feature breakdown, risk-regime suggestion, reviewer familiarity ranking, plausible split partitions. Label assumptions and never fail CI or auto-assign reviewers from these estimates.

**Layer C — predictions (only after prospective validation):** active review minutes and queue wait hours with out-of-time error ranges, uncertainty, coverage and drift tracking. A high Spearman rank alone does not give accurate minutes or wait-hour forecasts.

## Next milestones, with acceptance tests

### M1: make advice useful without fitting (first)

Build a concise default report with: summary, 3–5 review questions linked to changed files, reading order, test evidence and coverage unknowns, candidate reviewers as *context*, and split questions only when components look independent. Require actual author intent: **what behavior changes, why, and how tested**. Measure author/reviewer usefulness on 20 real PRs. Do not gate merges.

**Acceptance:** reviewers mark at least 70% of the top 3 questions per PR as relevant; no critical changed import, renaming, config or migration change is classified safe without an explicit check. Use blinded review against a baseline guide listing files by lines changed.

### M2: validate the predictive signals

Obtain 200–300 historical PRs, then 50+ future PRs with actual, low-friction reviewer-minute labels (prefer minutes per reviewer and per round, including abandoned reviews). Use repo/time holdout, remove confounded post-first-review features from first-review wait predictions, compare to changed-lines and simple size baselines, report median absolute error and 80% prediction-interval coverage alongside Spearman. Do **not** treat review touches as minutes.

**Acceptance:** improvement persists on a prospective cohort and there are no meaningful prediction failures concentrated in sensitive code categories. Otherwise keep the useful guides and drop effort forecasts.

### M3: test the split intervention causally

Compare comparable changes where teams randomly receive split suggestions vs no split suggestions, with independent tests/integration checks. Track reviewer minutes, author minutes, median queue wait, PRs per feature, defects and cross-PR fixups. A positive simulated RU saving is **not** success evidence.

**Acceptance:** net team time falls without an increase in escaped defects or time-to-complete a feature. Until then, keep splits as exploratory plans requiring human approval.

### M4: incorporate real reviewer routing

Add GitHub/GitLab review history, CODEOWNERS/approval rules, current queue, on-call load, security independence and a **learning** objective so all reviews do not concentrate on one senior engineer.

**Acceptance:** review latency improves without degrading load distribution or violating mandatory review policy.

## Remaining limitations (unfixed)

- Runtime/database/configuration coupling and behavior-preserving transformations are not statically verified.
- Pure file moves are still heavily underweighted relative to their possible integration risk; the nonzero adjustment is a guard, not calibration.
- Inline directives in existing code, generated files, binary assets, manifests, package lockfiles and language-specific semantics need stronger coverage.
- Reviewer familiarity reflects author commits, not expertise gained from reviewing or availability.
- A final merged PR diff is not a point-in-time snapshot of what was present when the first review was requested. **Do not deploy queue-wait predictions until this is addressed**.
- Revert/fix labels are imperfect, sample-dependent proxies. Recent follow-up windows are censored, but matching commits to fixes and detecting defects accurately needs richer evidence.
- Split what-if results are still calculated from structural priors, even when a calibration file is provided; a fitted minutes model is not used to simulate splitting.
- No live GitHub API fetch or large-scale monorepo performance validation was possible in this review.

## Validation performed

All 20 unit tests were run in the supplied package in this session. The CLI was exercised through tests that construct a temporary synthetic git repository. No claim is made of prospective empirical validity, real GitHub API access, or production readiness.
