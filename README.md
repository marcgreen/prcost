# prcost

Analyzes pull-request reviewability: reviewer familiarity, dependency-aware reading order,
change clusters, potential coupling gaps, and exploratory split candidates. It also produces
an **unvalidated** relative effort heuristic and an individual reviewer guide. Python and TypeScript/JavaScript.
Standard library only (Python 3.9+, tested on 3.9 and 3.13); `score` and `describe` read nothing but
your local git history.

The model and its assumptions are in `SPEC.md`. Calibration on real PR history is in `HANDOFF.md`.

## Install

```bash
pip install ./prcost          # or, without installing:
PYTHONPATH=/path/to/prcost python3 -m prcost --help
```

The repository needs full history (no `--depth`): familiarity and co-change come from `git log`.

## Use

```bash
prcost score                                   # current branch vs origin/HEAD (or main/master)
prcost score --base main --head my-branch
prcost score --reviewer alice@example.com      # cost for a specific reviewer
prcost score --brief                          # concise reviewability summary
prcost score --json                           # everything, machine-readable
prcost score --calibration calibration.json    # adds predicted review touches/minutes and wait hours
prcost describe --reviewer bob > guide.md      # reviewer-tailored description (markdown)
prcost score --hotfix                          # force the Chaotic regime
```

`score` prints an advisory review regime, relative effort RU for candidate reviewers, explanatory
factors, clusters, files by novelty, a **what-if** split comparison (not a verified saving), and
files that historically change with touched files. Reviewer lists are not assignments: availability,
independence, expertise and workload must be assessed separately.

RU (review units) combine a compressed-novelty heuristic, branch counts and unvalidated weights.
RU are **not** logical depth, reviewer minutes, accuracy guarantees or a defensible cross-repo unit.
Fitting on *actual* labeled review minutes can provide a separate prediction; split cost comparisons
continue using the experimental structural priors. The project has not been calibrated on real review effort.

## Decision-support boundaries

- `score --reviewer unknown@example.com` now scores an unfamiliar reviewer instead of silently
  substituting a known expert. The JSON `known_history` field makes this visible.
- Changed import statements and important source directives are treated as semantic, not automatically
  safe. Identical moved imports may still be low-effort.
- Test co-changes do **not** prove the change is covered. Safe-to-skim and same-day-approval claims
  have been removed from generated guidance.
- Treat split plans as architectural questions. Shared files, runtime coupling, operational
  dependencies and cherry-pick feasibility need human checks.

Read `REVIEW.md` for the constructive critique, implementation changes and next-stage priorities.

## Calibrate

```bash
prcost calibrate fetch --gh-repo OWNER/REPO --limit 300 --out prs.jsonl   # GitHub REST via `gh`
prcost calibrate fit --repo . --prs prs.jsonl --out calibration.json --report calibration-report.md
# Historical graphs and commit ancestry are rebuilt per PR by default.
# Optional faster, leakage-prone approximation: --fast-shared-graph
prcost calibrate fit ... --minutes minutes.csv   # optional self-reported minutes (columns: pr,minutes)
```

## Test

```bash
python3 -m unittest discover -s tests -v
```

## Layout

| File | What it does |
| --- | --- |
| `prcost/gitio.py` | git commands, bulk blob reads, diff and log parsing |
| `prcost/lines.py` | shallow vs logic line classification, move and systematic-rename detection |
| `prcost/graph.py` | import graph (Python AST, TS/JS incl. tsconfig paths and workspace packages), co-change, blast radius, clusters |
| `prcost/people.py` | reviewer familiarity from decayed commit history, identity merging |
| `prcost/model.py` | novelty heuristic, the effort formula, advisory workflow, split planning |
| `prcost/describe.py` | reviewer-tailored description |
| `prcost/calibrate.py` | REST fetch, labels, ridge regression, chronological evaluation, report |
| `prcost/cli.py` | command line |
| `tools/dry_run_synthetic.py` | calibration dry run on any merge-commit repo, no API needed |
| `REVIEW.md` | critical review, implemented corrections, validation gates and remaining limitations |
