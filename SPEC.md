# PR Review-Cost Model: Spec and Handoff

As of 2026-10-31. A copy of the spec doc, so this folder stands on its own. If the two differ, the newer one wins.

## Summary

prcost estimates what a pull request will cost a specific reviewer, and how to reshape it to cost less. It tracks two targets separately: active review effort and queue wait. It is also meant to change behavior: authors who see the score split work into smaller, independent, better-described PRs. That Goodhart effect is intended, and a counterweight metric keeps it honest.

What exists now:

- This spec: seven signals borrowed from complexity theory, how they combine, and how each gets tested.
- A Python CLI, `prcost`, that scores a diff with hand-set weights, suggests splits, picks the cheapest reviewer and drafts a description tailored to that reviewer. It parses Python and TypeScript/JavaScript.
- A handoff (`HANDOFF.md`) for tuning the model on real PR history once repo access exists.

**Status: every weight is a prior. No number in this doc has been validated against observed review effort.**
The seven dimensions must remain first-class concepts: each has a theoretical
motivation, a concrete fallible proxy, and an independent empirical validation
question. **`COMPLEXITY.md` is the canonical dimension ledger.**
Read `REVIEW.md` for corrections to the original interpretation. In particular, the model measures
compressed novelty, not Bennett logical depth; splits are exploratory, and a changed test is not
proof of coverage. Historical calibration now defaults to per-PR graph reconstruction and uses
chronological validation.

## Targets and assumptions

Two targets, never added together: active effort (minutes a reviewer spends) and queue wait (hours a PR sits before first review). Different levers move them, so one blended "review time" would hide which lever worked.

| Target | Moved by | Observable from GitHub |
| --- | --- | --- |
| Active effort | content of the change, reviewer familiarity, description quality | No. Proxies only (see Calibration) |
| Queue wait | perceived size, reviewer load, routing, time of day | Yes: ready-for-review to first review |
| Counterweight: escaped defects | how deep the review was | Partly: reverts and follow-up fixes within 14 days |
| Cost pushed to authors | splitting and writing descriptions | Partly: PRs per change, author time to merge |

Assumptions the design rests on, each testable:

1. **Effort is driven by the diff and the reviewer, not by who wrote it.** If author identity predicts effort better than content does, the model is measuring trust, not complexity.
2. **Effort is superlinear in size.** Splitting saves effort only if the size exponent (1 + ε) is above 1. If ε is near 0, splitting helps queue wait only. Calibration measures ε.
3. **Imports plus co-change history capture most coupling.** Runtime coupling (shared tables, feature flags, message schemas) is invisible to imports. Files that historically change together are the partial fix.
4. **Active effort cannot be read off GitHub.** Review rounds, comment counts and first-review-to-approval time are proxies. The best fix is cheap: a bot asks the approving reviewer "minutes spent: 5 / 15 / 30 / 60+".
5. **Faster review is only better if defects don't escape.** Any change made to cut review time is judged against revert and follow-up-fix rates over the same period.

Boundary note: optimizing reviewer time moves some cost onto authors. That cost sits outside the reviewer-time boundary, so it is tracked explicitly (last row above).

## Signals

There are **seven first-class dimensions**. Six feed the prior effort estimate,
while the seventh (Cynefin) picks the review process. A measured proxy is not a
measurement of the corresponding theoretical construct. All are computed from git alone, with no network access.

| Signal | Borrowed idea | Proxy in prcost | Enters the model as |
| --- | --- | --- | --- |
| Depth D | Bennett's logical depth, Kolmogorov complexity | Compressed novel bytes of logic lines given the old file, times (1 + branch density) | Base size, in review units (RU) |
| Familiarity F | Conditional complexity K(change given reviewer) | Reviewer's decayed commit history on the touched files | Multiplier 1 + α(1 − F) |
| Interactions P | Parameterized complexity: cost scales with k, not lines | Coupling edges among touched files, per cluster | Adds β × the smaller endpoint's depth, per edge |
| Entropy H | Shannon entropy of changes (Hassan 2009) | How evenly depth is spread over files, in bits | Multiplier 1 + θH |
| Blast radius B | Graph reachability | Non-test files that transitively depend on touched files, decayed by distance | Multiplier 1 + κ log2(1 + B) |
| Test co-change T | Communication complexity, interactive proofs | Share of changed source files whose tests also changed | Multiplier 1 − τT |
| Regime | Cynefin | Rules over the signals above plus change history | Picks the review process |

**Novelty proxy.** Each changed line is sorted into low-effort (blank, non-directive comment, unchanged move, selected systematic identifier renames) or semantic (including changed imports and tool directives). These rules are heuristic, not proof of equivalence. Shallow lines cost a flat s RU each. For logic lines:

```
D_f = ( [Z(old_f ‖ A_f) − Z(old_f)] + ½ [Z(new_f ‖ R_f) − Z(new_f)] ) × (1 + δ_f)
```

Z is zlib-compressed length, A and R are the added and removed logic lines, and δ is branch tokens per logic line. The bracket is the information the change adds given the file the reviewer can already read. Compression alone scores noise, such as a table of magic numbers, as maximal. This is **not** Bennett logical depth and does not measure the computation required to verify correctness.
It is a cheap, fallible description-length proxy modified by a branch-token heuristic. Test files count at half weight. Planned upgrade: the reasoning tokens a fixed model needs to verify the diff against its stated intent.

**Familiarity.** For reviewer r and file f, with n = r's commits touching f, each weighted by a 180-day half-life:

```
fam_r(f) = 1 − exp(−n_r(f) / 2)        F_r = Σ_f D_f · fam_r(f) / Σ_f D_f
```

Commits to other files in the same directory count at roughly half weight. Authorship history misses what someone learned by reviewing; GitHub review history fills that in after calibration.

**Coupling graph.** Interactions and blast radius share one graph. Edges come from imports (Python AST; TS/JS `import`, `export from`, `require`, relative paths, tsconfig path aliases, workspace package names) plus co-change: files committed together at least 3 times with confidence of at least 0.5. Clusters are the connected components of that graph restricted to the files a PR touches. Changed tests join the cluster of the code they cover, directly or through a re-export file such as `__init__.py` or `index.ts`. Clusters that don't interact add up. Inside a cluster, each coupling edge adds β times the smaller endpoint's depth, so a fully coupled cluster grows with the square of its file count: the k² from parameterized complexity, not a multiplier on everything.

## Cost model

Effort for reviewer r is a superlinear size term times four multipliers. Each cluster c of coupled files costs its depth plus an interaction charge per coupling edge:

```
cost_c = (D_c + β · Σ_{(a,b) in E_c} min(D_a, D_b)) · (1 + θ·H_c)
S      = Σ_c cost_c + s · N_shallow
E_r    = S · (S/R)^ε · (1 + θ·H_x) · (1 + κ·log2(1 + B)) · (1 + α·(1 − F_r)) · (1 − τ·T)
```

H_x is the entropy of cost across clusters (switching between unrelated parts). Every multiplier is printed by `prcost score`, so a surprising number can be traced to its cause.

| Symbol | Prior | Meaning |
| --- | --- | --- |
| s | 2 RU | cost of one shallow line |
| R | 1,500 RU | size where the superlinear term is neutral (about 120 to 200 logic lines) |
| ε | 0.25 | size exponent is 1 + ε; the parameter that decides whether splitting saves effort |
| β | 0.5 | interaction charge per coupling edge |
| θ | 0.1 | per bit of change entropy |
| κ | 0.08 | per doubling of blast radius |
| α | 1.0 | a fully unfamiliar reviewer takes twice as long |
| τ | 0.2 | discount when every changed source file has changed tests |
| Test, removed, deleted lines | 0.5, 0.5, 0.2 | weights relative to added logic |
| Split overhead | 150 RU | fixed cost of each extra PR |

All priors live in `Params` in `prcost/model.py`. Scale check: in two public repos (pallets/click, Python; sindresorhus/ky, TypeScript), a changed logic line averaged 7 to 13 RU.

**Reviewer familiarity ranking (not assignment).** Candidates are everyone with decayed commit history on the touched files or their directories, minus the PR's authors. Identities are merged by display name, so one person with two emails counts once. The top three by E_r are printed.

**Queue wait.** No structural prior: until calibrated, prcost reports only its drivers (lines, files, parts). After calibration it predicts wait hours from log lines, log files, log S and unfamiliarity. Reviewer load and time of day are known drivers that need live data and are not modeled yet.

**After calibration** the structural form is replaced by a log-linear fit of the same signals, log(1 + y) on log(1 + S), log(1 + edges), entropy, log2(1 + B), 1 − F, T and shallow share. Chronological feature ablations for D/F/P/H/B/T are reported independently; they
measure incremental prediction conditional on remaining features, which still
share information through the composite size S. R is checked against expert
risk labels and subsequent defects, not used as a cost multiplier.
The weight on log(1 + S) estimates an observational association, not the causal savings from splitting a PR. It should not be equated with a structural 1 + ε without an identification strategy.

## Cynefin routing

The regime is not a cost factor. It decides which review process a PR gets, because a cost estimate only means something for changes whose effects can be analyzed. Rules are checked top to bottom; the first match wins.

| Regime | Triggered when | Review process |
| --- | --- | --- |
| Chaotic | `--hotfix`, or a commit in the PR mentions hotfix, incident or sev0 to sev2 | Prioritize mitigation, retain safety checks, conduct expedited qualified review and follow-up |
| Clear | No lines classified semantic; may contain moves, renames, formatting and comments | One qualified reviewer, still verify correctness |
| Complex | Any of: at least 5 recent commits on these files with a fix share of at least 20% and 1.5× the repo-wide share; threads, locks, atomics, globals, transactions or workers in non-test code with at least one coupling edge; blast radius of 40 or more with no tests covering the changed source | Split into safe-to-fail steps, behind a flag if possible; review live or in pairs; watch production signals after merge |
| Clear | Best-reviewer effort of at most 400 heuristic RU, no coupling edges, blast radius below 10 | One qualified reviewer; verify impact |
| Complicated | Everything else | Route to the reviewer who knows the code best; work through the interaction checklist |

Two corrections came from running on real repos. Plain async/await was dropped as a Complex trigger because it flagged nearly every TypeScript PR. The fix-share rule is relative to the repo-wide rate, because commit-message habits vary more between repos than risk does.

Open question: in a 116-PR dry run on pallets/click, PRs classed Complex had fewer follow-up fixes than Complicated ones (6% vs 15%). These thresholds are the first thing calibration should tune.

## Splitting

Splitting is the intended Goodhart effect: prcost shows the author a cheaper shape for the change, and the score rewards taking it. It lists an **exploratory candidate** when an unvalidated simulation indicates lower heuristic RU. This is neither measured saving nor safe executable split: author time, validation overhead, shared-file edits and runtime coupling can reverse it.

1. **Lower-effort edits first (only when feasible).** Low-effort hunks (moves, renames, formatting, comments) are modeled as one preliminary PR. This can require nontrivial hunk staging, and the intermediate commit must build and pass tests. Changed imports are now semantic. It is the cheapest to verify, and taking it out makes the logic diff readable.
2. **One PR per independent cluster.** Clusters have no detected import, co-change or test edge. They are candidate separations, but may still share runtime state, configuration, schema or rollout constraints; parallel review is subject to architectural validation.
3. **Foundation, then dependents.** A cluster above R with an internal import hierarchy splits into the files the others import (plus their tests) and a PR stacked on them. Edges across the cut are charged at half rate to the stacked PR, because by then the foundation is known context, not new change.
4. **Compare.** The sum of the parts plus 150 RU per extra PR, against the whole. Positive: suggested. Otherwise the parts are listed as "not worth splitting at this size".

The saving has three sources: superlinear size (ε), less switching between unrelated parts (H_x), and routing each part to its best reviewer (α). If calibration finds ε near zero and familiarity weak, splitting helps only queue wait and review depth.

| Real change | Whole | Split | Verdict |
| --- | --- | --- | --- |
| ky: large "Refactor" commit (33 files, 3,161 logic lines) | 158,765 RU | 135,221 RU | Saves 15%: mechanical lines, then 5 foundation utilities, then `Ky.ts` and 26 files stacked on them |
| click: add `Parameter.spec` (7 files) | 4,736 RU | 4,502 RU | Saves 5%: mechanical lines out; the rest is one coupled unit |
| ky: `beforeRetry` per-attempt options (9 files) | 12,415 RU | 11,799 RU | Saves 5%, same pattern |
| click: move `help` to `Parameter` (3 files) | 629 RU | 718 RU | Costs 14%: too small to split |

## Reviewer-tailored description

The description applies conditional complexity: it sends each reviewer only what they don't already know. A file they know gets one line. A file new to them gets its context. Familiarity is inferred from past authorship, not past review behavior. The same PR produces different guides for different reviewers (`prcost describe --reviewer <name>`).

What the guide contains:

- **Effort and regime**, with the review process the regime calls for.
- **Intent**, a slot only the author can fill: what should be true after merge, and how they checked it.
- **Reading order:** dependencies before dependents, then tests. Each file is tagged "you know this file", "partly familiar" or "new to you". Unfamiliar source files list the context worth opening first.
- **Interactions to check:** each coupling edge, phrased as a question.
- **Lower-effort changes to verify:** lines classified as shallow, by kind (never automatically safe).
- **Blast radius:** the closest files that depend on the change.
- **Possibly missing changes:** files that changed in at least 60% of past commits with a touched file but are untouched here. This catches coupling that imports can't see, such as config next to code.
- **Tests as evidence:** which changed source files have changed tests.

## Prototype

`prcost` is a Python CLI (standard library only) that runs on a local clone with full history. Tested on Python 3.9 and 3.13. See `README.md` for commands.

Verified so far:

- The original 13 tests on a synthetic repo with known structure, plus 7 new regression tests: systematic renames and moves come out shallow, a one-off identifier swap stays logic, imports resolve (relative, absolute, tsconfig aliases, workspace packages), co-change gaps are found, reviewers route to the right owner, splits separate independent clusters, the REST fetch normalizes recorded API responses.
- Real runs on pallets/click and sindresorhus/ky: about 0.3 seconds per PR. Those runs exposed and fixed three bugs: interactions exploding on large PRs, tests counted as interactions, and test coverage missed through `__init__.py` re-exports.
- The calibration pipeline end to end on 116 click PRs with synthetic review labels: 5 seconds. Reproduce with `tools/dry_run_synthetic.py` (see HANDOFF.md). The live REST fetch is untested, because the session that built this had no GitHub API access.

Known limits:

- The import graph is rebuilt on every run. Tested only on repos of a few hundred files; large monorepos will need a cache.
- Imports are parsed for Python and TS/JS only. Other languages get co-change edges only. Not handled: tsconfig `extends`, imports built from variables.
- Familiarity comes from authorship, not review history. One person with two display names counts twice unless `.mailmap` merges them.
- Depth ignores what a line means, only how novel and branchy it is. A one-character change to a security check scores low.

## Calibration plan

Calibration replaces the priors with weights fitted on the repo's own merged PRs and shows which signals earn their place. Aim for 300 PRs. The code needs at least 20 per model, but cross-validation is unstable below about 150.

| Label | Definition | Source | Quality |
| --- | --- | --- | --- |
| Queue wait | Hours from ready-for-review (or creation) to the first non-author review | REST: reviews, issue events | Good |
| Effort, first pass | Review touches: review comments + 2 per review | REST: reviews, review comments | Weak proxy |
| Effort, later | Approver's self-reported minutes | Bot or PR template, 4 to 6 weeks | Best available |
| Reverted | Merge or PR commit named in a revert commit, or a `Revert "<title>"` commit | git | Good, but rare |
| Follow-up fix | A fix-titled PR within 14 days that changes logic in the same source files | REST and git | Noisy |

**Method.** Ridge regression on log(1 + label), evaluated by forward-chaining chronological holdout Spearman against the same lines-changed baseline. By default each PR uses its own code graph and the git ancestors of its base. `--fast-shared-graph` is a faster approximation that leaks later relationships. Historical final-PR diffs may still contain changes made after the first review: a deployment predicting queue wait at PR creation requires point-in-time feature snapshots.

**Success criteria.**

1. Held-out Spearman beats the lines-changed baseline by at least +0.10, for wait or effort. Otherwise the estimates add nothing over line counts, and prcost is kept for routing, splitting and descriptions only.
2. The follow-up-fix rate rises from Clear to Complicated to Complex. Otherwise the regime thresholds get retuned.
3. After rollout, comparing 4 weeks before and after: median queue wait or effort falls, revert and follow-up-fix rates don't rise, and PRs per change (the cost pushed onto authors) is reported next to both.

## Handoff

The steps, the access requirements and a paste-ready prompt are in `HANDOFF.md`.

## Risks and falsifiers

The biggest structural risk is a reinforcing loop in routing. Always sending a PR to its cheapest reviewer concentrates reviews on experts: their queue grows, wait rises, and nobody else gains familiarity, which makes them cheaper still next time. Mitigation: cap open reviews per person, and route Clear PRs to less familiar reviewers on purpose, as cheap learning.

What would show the model is wrong:

| Observation | What it means | Response |
| --- | --- | --- |
| Lines changed predicts as well (lift below +0.10) | The complexity signals add no predictive value here | Keep prcost for routing, splits and guides; stop quoting RU as estimates |
| Author identity predicts effort better than content | The model measures trust, not complexity | Add author as a control; report it separately |
| Size elasticity at or below 1 with a minutes label | Splitting doesn't save effort | Justify splits by queue wait and review depth only, or drop them |
| Comments per RU fall or follow-up fixes rise after rollout | Faster review became shallower review | Raise the Complex bar for review depth; slow the rollout |
| PRs per change rise with no fall in wait | Cost moved to authors with no benefit | Raise the split overhead so fewer splits are suggested |
| Reviews concentrate on a few people | The routing loop above | Load caps, learning routes |

Two gaming paths to watch. Coupled code split into stacked PRs can be reviewed out of context, so watch follow-up fixes that cross stacked parts. A systematic rename counts as mechanical, so a rename that changes which function gets called could pass as shallow.
