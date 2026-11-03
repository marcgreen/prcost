# Handoff: calibrating prcost on your repository

This folder is self-contained. Whoever picks it up (you, a teammate, or a new Claude session) needs
only this folder and access to the repository. Nothing here depends on the conversation that built it.

## What's in the folder

| Path | What it is |
| --- | --- |
| `SPEC.md` | The model: targets, signals, formulas, priors, Cynefin rules, split logic, risks. Start here for the why. |
| `HANDOFF.md` | This file: how to calibrate and roll out. |
| `README.md` | Command reference and code layout. |
| `prcost/` | The CLI (Python standard library only). |
| `tests/` | 20 tests; they build their own synthetic git repo. |
| `tools/dry_run_synthetic.py` | Runs the calibration pipeline on any public repo without API access. |

The spec also exists as a live doc for the person who commissioned this
(https://claude.ai/artifact/3i3rtgRXRdQemQQ9oTbc7P). It is private to them; `SPEC.md` is the same content.

## Requirements

- git, Python 3.9 or newer (tested on 3.9 and 3.13)
- The GitHub CLI `gh`, only for `calibrate fetch`
- Network access to GitHub

## Where things stand (2026-10-31)

- Every weight is a hand-set prior. Nothing has been fitted to real review times yet.
- Verified at the original handoff: the tests passed; `score` and `describe` ran on real PRs from pallets/click (Python) and
  sindresorhus/ky (TypeScript); the full calibration pipeline ran on 116 click PRs with invented review
  times (about 5 seconds).
- Not verified: the live GitHub fetch. It is tested against recorded API responses only, because the
  session that built it had no GitHub API access. Expect to fix small things on first contact.
- First finding to check on real data: in the click dry run, PRs classed Complex had fewer follow-up
  fixes than Complicated ones (6% vs 15%). The Complex rules are the first thing to tune.

## 0. Check the folder works (5 minutes, no access needed)

```bash
cd prcost && python3 -m unittest discover -s tests            # expect: Ran 20 tests ... OK
git clone https://github.com/pallets/click.git /tmp/click
python3 tools/dry_run_synthetic.py /tmp/click /tmp/prs-synthetic.jsonl 120
cd /tmp/click && PYTHONPATH=/path/to/prcost python3 -m prcost calibrate fit --prs /tmp/prs-synthetic.jsonl
```

## 1. Get access

prcost reads git history locally and PR review history through the GitHub REST API (`gh api`).

- **In a Claude session:** attach the repository with API access, not just clone access. Found the hard
  way: GitHub GraphQL is blocked in Claude sessions (prcost uses REST for this reason), and REST calls
  fail with "GitHub access to this repository is not enabled" unless the repo is attached with
  credentials. That attachment is labelled as push access even though prcost only reads. Tell the
  session not to push anything.
- **Anywhere else:** `gh auth login` with a token that can read the repo. Fine-grained token
  permissions: Metadata read, Contents read, Pull requests read.

## 2. Clone with full history

```bash
git clone https://github.com/OWNER/REPO.git && cd REPO     # no --depth
```

## 3. Smoke test

```bash
PYTHONPATH=/path/to/prcost python3 -m prcost score --base origin/main --head <a recent PR head or branch>
```

It rebuilds the import graph on every run: the original prototype reported about 0.3 seconds per PR on small repos.
Large monorepos are untested and will be slower (caching is a known gap).

## 4. Fetch merged PR history

```bash
python3 -m prcost calibrate fetch --gh-repo OWNER/REPO --limit 300 --out prs.jsonl
```

About 5 REST calls per PR, so 300 PRs is about 1,500 calls, inside GitHub's 5,000 per hour limit.

## 5. Fit

```bash
python3 -m prcost calibrate fit --repo . --prs prs.jsonl --out calibration.json --report calibration-report.md
```

It fetches PR head commits it can't find (`refs/pull/N/head`), so the clone needs a working remote.
**Default is now historical per-PR graphs and ancestry.** Faster `--fast-shared-graph` is approximate
and leaks later code relationships into earlier PR analyses.
Outputs: `calibration.json` (weights), `calibration-report.md`, `calibration.records.jsonl` (per-PR
features and labels, for your own analysis).

## 6. Read the report with these rules

| Question | Where | Decision |
| --- | --- | --- |
| Does the model beat lines changed? | Chronological holdout Spearman, effort and wait | Lift of +0.10 or more: use calibrated scores. Less: keep prcost for routing, splitting and descriptions only, and say so. |
| Which signals matter? | Standardized weights | Keep signals at 0.05 or more in absolute value. Drop or rethink the rest. A wrong sign means the proxy is broken, not that the idea is wrong. |
| Does splitting save effort? | Size elasticity | Requires actual minutes labels and a prospective comparison. Observational size elasticity alone does not prove a splitting intervention saves time. |
| Is the Cynefin classifier right? | Counterweight table by regime | Follow-up-fix rate should rise Clear < Complicated < Complex. If not, tune `risky_fix_ratio`, `risky_fix_lift`, `big_blast` in `prcost/model.py` (`Params`). |
| Are big PRs being skimmed? | Comments per 1,000 RU by size | A steep fall with size supports splitting on quality grounds even without time savings. |

Review touches (comments + 2 per review) are a weak stand-in for effort. Treat effort results as
directional until step 8.

## 7. Use the calibration

```bash
python3 -m prcost score --calibration calibration.json
```

Adds predicted review touches (or minutes) and queue-wait hours to the output.

## 8. Get a real effort label (recommended)

Active review time is not in GitHub. Cheapest fix: on approval, a bot or a PR template checkbox asks the
approver "minutes spent: 5 / 15 / 30 / 60+". After 4 to 6 weeks, export a CSV with columns `pr,minutes`
and refit:

```bash
python3 -m prcost calibrate fit --repo . --prs prs.jsonl --minutes minutes.csv
```

## 9. Roll out the intended side effect

The point is that authors change behavior: smaller PRs, mechanical changes split out, better
descriptions. A CI job that comments on each PR can influence behavior. **Do not deploy gating or automatic reviewer
assignment based on RU.** The following is an untested sketch, assuming this folder is
committed as `tools/prcost`:

```yaml
name: prcost
on: pull_request
permissions: { contents: read, pull-requests: write }
jobs:
  score:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install ./tools/prcost
      - env: { GH_TOKEN: "${{ github.token }}" }
        run: |
          HEAD_SHA=${{ github.event.pull_request.head.sha }}
          BASE=origin/${{ github.base_ref }}
          { echo '```'; prcost score --base "$BASE" --head "$HEAD_SHA" --calibration tools/prcost/calibration.json; echo '```';
            prcost describe --base "$BASE" --head "$HEAD_SHA"; } > comment.md
          gh pr comment ${{ github.event.pull_request.number }} --body-file comment.md
```

Track for 4 weeks before and after: median PR size, queue wait, review touches or minutes, revert and
follow-up-fix rate (the counterweight), and PRs per change (the cost pushed onto authors). The rollout
works only if wait or effort falls while the counterweight holds. Watch who gets routed: if reviews
concentrate on a few experts, cap per-person load (see Risks in `SPEC.md`).

## Done when

The calibration step is finished when these exist:

1. `calibration.json`, `calibration-report.md` and `calibration.records.jsonl` from at least 150 PRs.
2. `CALIBRATION_RESULTS.md`: one line per rule in step 6 with the number and the decision taken.
3. A proposed change to `Params` in `prcost/model.py`, each value with the evidence behind it, and the
   tests still passing.
4. `SPEC.md` updated where the results contradict it (for example the Cynefin thresholds).

## Paste-ready prompt for the next Claude session

Attach `prcost.zip` and the repository (with API access, see step 1), then send:

> Unzip prcost.zip and read HANDOFF.md and SPEC.md. Calibrate prcost on OWNER/REPO by following
> HANDOFF.md steps 0 to 6, and finish when everything under "Done when" exists. Report the step 6
> decisions to me in a short summary and send back the updated folder as a zip. If you can open the
> spec doc linked in HANDOFF.md, update its Calibration section too; otherwise SPEC.md is enough.
> Read-only on the repository: do not push, comment or open PRs.

## Known limits

- During `fit`, the import graph and commit ancestry are rebuilt for each PR by default (slower,
  historically faithful). `--fast-shared-graph` is explicitly approximation mode and leaks future
  code relationships. Even in exact mode, the historical *final* diff may include post-first-review edits.
- Familiarity comes from authorship only. Review history is a planned addition.
- One person with two display names counts as two people. Add a `.mailmap` to fix it.
- GitHub approval minutes should be labeled per reviewer and per review round where possible; the
  present tool stores one PR-level minutes label and does not support real-time reviewer availability.
- Imports are parsed for Python and TS/JS only. Other languages get co-change edges only.
- Not handled: `tsconfig` `extends`, imports built from variables, Python namespace packages without
  `__init__.py` at the top level.
