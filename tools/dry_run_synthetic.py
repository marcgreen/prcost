"""Dry run of the calibration pipeline without GitHub API access.

Builds a prs.jsonl from a local clone's merge commits (PR number from "#123" in the subject), with
INVENTED review timestamps and comment counts. Use it to check that `prcost calibrate fit` runs on a
repo before real data exists. The fitted weights are meaningless; the revert, follow-up-fix and regime
columns in the report come from real git history.

    python3 tools/dry_run_synthetic.py /path/to/clone prs-synthetic.jsonl 120
    cd /path/to/clone && python3 -m prcost calibrate fit --prs prs-synthetic.jsonl

Works for repos that merge PRs with merge commits (e.g. pallets/click). Squash-merge repos have no
second parent to use as the PR head, so the script finds nothing there.
"""
import json, math, random, re, subprocess, sys
from datetime import datetime, timezone

repo, out, limit = sys.argv[1], sys.argv[2], int(sys.argv[3])
def git(*a): return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=True).stdout
def iso(ts): return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
rng = random.Random(1)
rows = [l.split(" ", 4) for l in git("log", "--merges", "--first-parent", f"-n{limit}", "--format=%H %P %ct %s", "HEAD").splitlines()]
recs, logins = [], set()
for h, p1, p2, ct, subj in [r for r in rows if len(r) == 5 and len(r[2]) == 40]:
    m = re.search(r"#(\d+)", subj)
    if not m: continue
    commits = [l.split("\t") for l in git("log", "--format=%H\t%aE\t%aN\t%ct", f"{p1}..{p2}").splitlines()]
    if not commits: continue
    login = lambda e: re.sub(r"[^a-z0-9]", "", e.split("@")[0].lower())[:20] or "ghost"
    author = login(commits[-1][1]); logins.add(author)
    stat = git("diff", "--shortstat", f"{p1}...{p2}")
    lines = sum(int(x) for x in re.findall(r"(\d+) (?:insertion|deletion)", stat))
    created = min(int(c[3]) for c in commits)
    recs.append(dict(number=int(m.group(1)), title=subj, createdAt=iso(created), mergedAt=iso(int(ct)), isDraft=False,
        baseRefOid=p1, headRefOid=p2, mergeCommit={"oid": h}, author={"login": author},
        commits={"nodes": [{"commit": {"oid": c[0], "author": {"email": c[1], "name": c[2], "user": {"login": login(c[1])}}}} for c in commits]},
        _lines=lines))
pool = sorted(logins)
for r in recs:
    others = [l for l in pool if l != r["author"]["login"]] or ["reviewer"]
    who = rng.choice(others)
    created = datetime.fromisoformat(r["createdAt"].replace("Z", "+00:00")).timestamp()
    wait = math.exp(rng.gauss(1.0 + 0.3 * math.log1p(r["_lines"]), 0.8)) * 3600
    touches = max(0, int(round(math.exp(0.45 * math.log1p(r.pop("_lines")) + rng.gauss(0, 0.5)))))
    comments = max(0, touches - 4)
    r["reviews"] = {"nodes": [
        {"author": {"login": who}, "state": "COMMENTED", "submittedAt": iso(created + wait), "comments": {"totalCount": comments}},
        {"author": {"login": who}, "state": "APPROVED", "submittedAt": iso(created + wait + 3600), "comments": {"totalCount": 0}}]}
    r["timelineItems"] = {"nodes": []}
with open(out, "w") as fh:
    for r in recs: fh.write(json.dumps(r) + "\n")
print(len(recs), "records")
