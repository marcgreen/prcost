"""Calibration: fit the model on a repository's merged-PR history.

fetch: GitHub REST API through the `gh` CLI -> prs.jsonl (timestamps, reviews, commit identities)
fit:   local clone + prs.jsonl -> signals per PR -> ridge regression on log labels -> calibration.json
"""
from __future__ import annotations

import csv
import json
import math
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime

from . import gitio
from .graph import build_graph
from .model import FIX_RE, Analysis, Estimate, Params, entropy

EFFORT_FEATURES = ["log_size", "log_novelty_depth", "log_interactions", "entropy", "log_blast",
                   "unfamiliarity", "certificates", "shallow_share"]
# Each ablation refits a model using the SAME chronological train/test folds.
# The D probe is incremental beyond composite log_size; it does not remove all
# depth information, because size itself includes novelty and interactions.
EFFORT_ABLATIONS = {
    "D": ["log_novelty_depth"],
    "F": ["unfamiliarity"],
    "P": ["log_interactions"],
    "H": ["entropy"],
    "B": ["log_blast"],
    "T": ["certificates"],
}
WAIT_FEATURES = ["log_lines", "log_files", "log_size", "unfamiliarity"]
BASELINE = ["log_lines"]

# ---------------------------------------------------------------- features


def feature_row(an: Analysis, est: Estimate, familiarity: float) -> dict[str, float]:
    lines = sum(s.logic + s.shallow for s in an.sig.values())
    return {
        "log_size": math.log1p(est.size),
        "log_novelty_depth": math.log1p(sum(c.depth for c in est.clusters)),
        "log_interactions": math.log1p(sum(c.interactions for c in est.clusters)),
        "entropy": entropy([an.sig[f].depth for f in est.logic_files]),
        "log_blast": math.log2(1 + est.blast),
        "unfamiliarity": 1 - familiarity,
        "certificates": est.certificates,
        "shallow_share": an.shallow_total / max(1, lines),
        "log_lines": math.log1p(lines),
        "log_files": math.log1p(len(an.sig)),
    }


# ---------------------------------------------------------------- fetch


def gh_rest(path: str):
    """One GitHub REST call through the gh CLI. (GraphQL is deliberately not used: some
    environments, including Claude sessions, only proxy the REST API.)"""
    proc = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github+json", path],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
    return json.loads(proc.stdout)


def _pages(path: str, api, max_pages: int = 10) -> list:
    out = []
    sep = "&" if "?" in path else "?"
    for page in range(1, max_pages + 1):
        items = api(f"{path}{sep}per_page=100&page={page}")
        out.extend(items)
        if len(items) < 100:
            break
    return out


def _login(obj) -> str | None:
    return (obj or {}).get("login")


def pr_record(gh_repo: str, pr: dict, api=gh_rest) -> dict:
    """Normalize one merged PR from the REST API into the record `labels()` and `fit()` read."""
    num = pr["number"]
    reviews = _pages(f"repos/{gh_repo}/pulls/{num}/reviews", api)
    comments = _pages(f"repos/{gh_repo}/pulls/{num}/comments", api)
    per_review = Counter(c.get("pull_request_review_id") for c in comments)
    commits = _pages(f"repos/{gh_repo}/pulls/{num}/commits", api, max_pages=3)
    events = _pages(f"repos/{gh_repo}/issues/{num}/events", api, max_pages=3)
    return {
        "number": num, "title": pr.get("title", ""), "createdAt": pr["created_at"], "mergedAt": pr["merged_at"],
        "isDraft": bool(pr.get("draft")), "baseRefOid": pr["base"]["sha"], "headRefOid": pr["head"]["sha"],
        "mergeCommit": {"oid": pr.get("merge_commit_sha")} if pr.get("merge_commit_sha") else None,
        "author": {"login": _login(pr.get("user"))},
        "commits": {"nodes": [{"commit": {"oid": c["sha"], "author": {
            "email": ((c.get("commit") or {}).get("author") or {}).get("email"),
            "name": ((c.get("commit") or {}).get("author") or {}).get("name"),
            "user": {"login": _login(c.get("author"))}}}} for c in commits]},
        "reviews": {"nodes": [{"author": {"login": _login(r.get("user"))}, "state": r.get("state"),
                               "submittedAt": r.get("submitted_at"),
                               "comments": {"totalCount": per_review.get(r.get("id"), 0)}} for r in reviews]},
        "timelineItems": {"nodes": [{"createdAt": e["created_at"]} for e in events
                                    if e.get("event") == "ready_for_review"]},
    }


def fetch(gh_repo: str, limit: int, out_path: str, log=print, api=gh_rest) -> int:
    """Newest merged PRs first. About 5 API calls per PR."""
    n, page = 0, 1
    with open(out_path, "w") as fh:
        while n < limit:
            prs = api(f"repos/{gh_repo}/pulls?state=closed&sort=created&direction=desc&per_page=100&page={page}")
            if not prs:
                break
            for pr in prs:
                if not pr.get("merged_at"):
                    continue
                fh.write(json.dumps(pr_record(gh_repo, pr, api)) + "\n")
                n += 1
                if n >= limit:
                    break
            log(f"fetched {n} merged PRs")
            page += 1
    return n


# ---------------------------------------------------------------- labels


def _ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def labels(pr: dict) -> dict | None:
    author = (pr.get("author") or {}).get("login")
    reviews = [r for r in (pr.get("reviews") or {}).get("nodes", [])
               if r.get("submittedAt") and r.get("state") != "PENDING"
               and (r.get("author") or {}).get("login") not in (None, author)]
    if not reviews:
        return None
    reviews.sort(key=lambda r: r["submittedAt"])
    first = _ts(reviews[0]["submittedAt"])
    ready = _ts(pr["createdAt"])
    for ev in (pr.get("timelineItems") or {}).get("nodes", []):
        t = _ts(ev["createdAt"]) if ev and ev.get("createdAt") else None
        if t and ready < t <= first:
            ready = t
    approvals = [_ts(r["submittedAt"]) for r in reviews if r["state"] == "APPROVED"]
    comments = sum((r.get("comments") or {}).get("totalCount", 0) for r in reviews)
    return {
        "wait_hours": max(0.0, (first - ready) / 3600),
        "touches": comments + 2 * len(reviews),
        "span_hours": (min(approvals) - first) / 3600 if approvals else None,
        "reviewer": reviews[0]["author"]["login"],
    }


def login_emails(prs: list[dict]) -> dict[str, Counter]:
    out: dict[str, Counter] = defaultdict(Counter)
    for pr in prs:
        for node in (pr.get("commits") or {}).get("nodes", []):
            a = node["commit"].get("author") or {}
            login = (a.get("user") or {}).get("login")
            if login and a.get("email"):
                out[login][a["email"].lower()] += 1
    return out


# ---------------------------------------------------------------- statistics (stdlib only)


def _ranks(xs: list[float]) -> list[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            ranks[order[k]] = (i + j) / 2
        i = j + 1
    return ranks


def spearman(a: list[float], b: list[float]) -> float:
    if len(a) < 3:
        return float("nan")
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mb) ** 2 for y in rb))
    return cov / (va * vb) if va and vb else float("nan")


def _solve(A: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(M[r][c]))
        M[c], M[piv] = M[piv], M[c]
        if abs(M[c][c]) < 1e-12:
            continue
        for r in range(n):
            if r != c:
                f = M[r][c] / M[c][c]
                for k in range(c, n + 1):
                    M[r][k] -= f * M[c][k]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]


def ridge(X: list[list[float]], y: list[float], names: list[str], lam: float = 1.0) -> dict:
    n, d = len(X), len(names)
    mean = [sum(r[j] for r in X) / n for j in range(d)]
    std = [math.sqrt(sum((r[j] - mean[j]) ** 2 for r in X) / n) or 1.0 for j in range(d)]
    Z = [[(r[j] - mean[j]) / std[j] for j in range(d)] for r in X]
    ym = sum(y) / n
    A = [[sum(z[i] * z[j] for z in Z) + (lam if i == j else 0.0) for j in range(d)] for i in range(d)]
    b = [sum(z[i] * (yy - ym) for z, yy in zip(Z, y)) for i in range(d)]
    beta = _solve(A, b)
    raw = [beta[j] / std[j] for j in range(d)]
    intercept = ym - sum(raw[j] * mean[j] for j in range(d))
    return {"features": names, "coef_std": beta, "coef": raw, "intercept": intercept}


def predict(model: dict, row: dict[str, float]) -> float:
    return model["intercept"] + sum(c * row[f] for c, f in zip(model["coef"], model["features"]))


def cv_spearman(rows: list[dict], y: list[float], names: list[str], k: int = 5,
                seed: int = 7, warmup: int | None = None) -> float:
    """Forward-chaining evaluation. Rows MUST be sorted oldest to newest.

    Predict only later PRs using models fitted on strictly earlier PRs. The same
    temporal boundaries are used for the feature model and the baseline.
    """
    n = len(rows)
    warmup = max(len(names) + 3, n // 2) if warmup is None else max(len(names) + 3, warmup)
    if n - warmup < 3:
        return float("nan")
    actual, pred = [], []
    for fold in range(min(k, n - warmup)):
        lo = warmup + (n - warmup) * fold // min(k, n - warmup)
        hi = warmup + (n - warmup) * (fold + 1) // min(k, n - warmup)
        model = ridge([[r[f] for f in names] for r in rows[:lo]], y[:lo], names)
        for i in range(lo, hi):
            actual.append(y[i])
            pred.append(predict(model, rows[i]))
    return spearman(pred, actual)


# ---------------------------------------------------------------- fit


def _ensure_commits(repo: str, prs: list[dict], remote: str, log) -> None:
    missing = [pr for pr in prs if not gitio.has_commit(repo, pr["headRefOid"])]
    for i in range(0, len(missing), 50):
        specs = [f"+refs/pull/{pr['number']}/head:refs/prcost/pr/{pr['number']}" for pr in missing[i:i + 50]]
        gitio.run(repo, "fetch", "--no-tags", "--quiet", remote, *specs, check=False)
    if missing:
        log(f"fetched heads for {len(missing)} PRs")


def _reverted(repo: str) -> tuple[set[str], set[str]]:
    out = gitio.run(repo, "log", "-i", "--grep=revert", "--format=%x00%B", "HEAD")
    shas, titles = set(), set()
    for body in out.split("\0"):
        shas.update(m.lower() for m in re.findall(r"This reverts commit ([0-9a-fA-F]{7,40})", body))
        titles.update(re.findall(r'^Revert "(.+)"', body, re.M))
    return shas, titles


def fit(repo: str, prs_path: str, out_path: str, report_path: str, minutes_csv: str | None = None,
        remote: str = "origin", per_pr_graph: bool = True, log=print) -> dict:
    with open(prs_path, encoding="utf-8") as fh:
        prs = [json.loads(line) for line in fh if line.strip()]
    log(f"{len(prs)} PRs loaded")
    minutes = {}
    if minutes_csv:
        with open(minutes_csv) as fh:
            for row in csv.DictReader(fh):
                minutes[int(row["pr"])] = float(row["minutes"])

    _ensure_commits(repo, prs, remote, log)
    commits = gitio.log_commits(repo, "HEAD", max_count=20000) if not per_pr_graph else None
    graph = None
    if not per_pr_graph:
        reader = gitio.BlobReader(repo)
        head = gitio.rev_parse(repo, "HEAD")
        graph = build_graph(lambda p: reader.read(head, p), gitio.ls_tree(repo, head), commits)
        reader.close()
    ids = login_emails(prs)
    rev_shas, rev_titles = _reverted(repo)

    records, skipped, unmapped = [], Counter(), 0
    for n, pr in enumerate(prs, 1):
        lab = labels(pr)
        if lab is None:
            skipped["no non-author review"] += 1
            continue
        head = pr["headRefOid"]
        if not gitio.has_commit(repo, head):
            skipped["head commit unavailable"] += 1
            continue
        merge = (pr.get("mergeCommit") or {}).get("oid")
        try:
            if merge and gitio.has_commit(repo, merge + "^1"):
                base = gitio.merge_base(repo, head, merge + "^1")
            else:
                base = gitio.merge_base(repo, head, pr["baseRefOid"])
            # For trustworthy historical features, use git history reachable from THAT PR's base.
            # Supplying HEAD commits by date alone can include commits not yet on that branch.
            an = Analysis(repo, base, head, graph=graph, commits=None if per_pr_graph else commits,
                          base_is_exact=True)
        except gitio.GitError as e:
            skipped[f"git error: {str(e)[:60]}"] += 1
            continue
        est = an.whole()
        emails = list({an.fam.canonical(e) for e in ids.get(lab["reviewer"], {})})
        if not emails:
            hit = an.fam.match(lab["reviewer"])
            emails = [hit] if hit else []
        if not emails:
            unmapped += 1
        F = max((an.familiarity(e, est) for e in emails), default=0.0)
        best = an.reviewers(est, top=1)
        regime, _ = an.regime(est, best[0] if best else None)
        row = feature_row(an, est, F)
        oids = {c["commit"]["oid"].lower() for c in (pr.get("commits") or {}).get("nodes", [])}
        if merge:
            oids.add(merge.lower())
        reverted = pr["title"] in rev_titles or any(o.startswith(s) for o in oids for s in rev_shas)
        records.append({
            "number": pr["number"], "title": pr["title"], "merged": _ts(pr["mergedAt"]),
            "files": sorted(f for f in an.logic_paths if not an.sig[f].test), "row": row, "regime": regime, "effort_ru": est.for_reviewer(F, an.p),
            "comments": lab["touches"], "reverted": reverted, **lab,
            "minutes": minutes.get(pr["number"]),
        })
        if n % 25 == 0:
            log(f"analyzed {n}/{len(prs)}")

    # Counterweight: a later fix PR within 14 days that changes logic in the same source files
    # (changelogs, docs and tests excluded, since nearly every PR touches them).
    latest_merge = max((r["merged"] for r in records), default=0)
    for r in records:
        fs = set(r["files"])
        # Right-censor newest PRs: the retrieved cohort cannot observe their full 14-day window.
        r["followup_fix"] = (None if r["merged"] + 14 * 86400 > latest_merge else any(
            o is not r and FIX_RE.search(o["title"]) and 0 < o["merged"] - r["merged"] <= 14 * 86400
            and fs & set(o["files"]) for o in records))

    result = _fit_models(records, minutes_csv is not None)
    result.update({"evaluation": "forward_chaining_last_half", "n_records": len(records), "skipped": dict(skipped), "unmapped_reviewers": unmapped,
                   "graph": "per PR" if per_pr_graph else "shared, built at current HEAD"})
    with open(out_path, "w") as fh:
        json.dump(result, fh, indent=2)
    with open(report_path, "w") as fh:
        fh.write(_report(records, result))
    with open(out_path.rsplit(".", 1)[0] + ".records.jsonl", "w") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return result


def _fit_models(records: list[dict], have_minutes: bool) -> dict:
    out: dict = {"version": 1}
    label = "minutes" if have_minutes else "touches"
    eff = sorted((r for r in records if r.get(label) is not None), key=lambda r: r["merged"])
    if len(eff) >= 20:
        rows, y = [r["row"] for r in eff], [math.log1p(r[label]) for r in eff]
        out["effort"] = ridge([[x[f] for f in EFFORT_FEATURES] for x in rows], y, EFFORT_FEATURES)
        out["effort"]["label"] = label
        common_warmup = max(len(EFFORT_FEATURES) + 3, len(rows) // 2)
        out["effort"]["cv_spearman"] = cv_spearman(rows, y, EFFORT_FEATURES, warmup=common_warmup)
        out["effort"]["baseline_cv_spearman"] = cv_spearman(rows, y, BASELINE, warmup=common_warmup)
        out["effort"]["n"] = len(eff)
        full_cv = out["effort"]["cv_spearman"]
        out["effort"]["dimension_ablations"] = {
            key: {
                "removed_features": removed,
                "without_cv_spearman": cv_spearman(
                    rows, y, [f for f in EFFORT_FEATURES if f not in removed],
                    warmup=common_warmup),
            }
            for key, removed in EFFORT_ABLATIONS.items()
        }
        for entry in out["effort"]["dimension_ablations"].values():
            entry["delta_spearman"] = full_cv - entry["without_cv_spearman"]
    wait = sorted((r for r in records if r.get("wait_hours") is not None), key=lambda r: r["merged"])
    if len(wait) >= 20:
        rows, y = [r["row"] for r in wait], [math.log1p(r["wait_hours"]) for r in wait]
        out["wait"] = ridge([[x[f] for f in WAIT_FEATURES] for x in rows], y, WAIT_FEATURES)
        out["wait"]["label"] = "wait_hours"
        common_warmup = max(len(WAIT_FEATURES) + 3, len(rows) // 2)
        out["wait"]["cv_spearman"] = cv_spearman(rows, y, WAIT_FEATURES, warmup=common_warmup)
        out["wait"]["baseline_cv_spearman"] = cv_spearman(rows, y, BASELINE, warmup=common_warmup)
        out["wait"]["n"] = len(wait)
    return out


def _quintiles(records: list[dict], key) -> list[list[dict]]:
    srt = sorted(records, key=key)
    n = len(srt)
    return [srt[i * n // 5:(i + 1) * n // 5] for i in range(5)] if n >= 10 else [srt]


def _report(records: list[dict], result: dict) -> str:
    L = ["# prcost calibration report", ""]
    L.append(f"PRs analyzed: {result['n_records']}. Skipped: {result['skipped'] or 'none'}. "
             f"Reviewers with no git identity: {result['unmapped_reviewers']}. Graph: {result['graph']}.")
    L.append("")
    for key, title in (("effort", "Review activity / active-effort proxy"), ("wait", "Queue wait")):
        m = result.get(key)
        if not m:
            L += [f"## {title}", "", "Not enough labeled PRs (need 20).", ""]
            continue
        lift = m["cv_spearman"] - m["baseline_cv_spearman"]
        L += [f"## {title} (label: {m['label']}, n = {m['n']})", "",
              f"Chronological holdout Spearman: model {m['cv_spearman']:.2f}, lines-changed baseline "
              f"{m['baseline_cv_spearman']:.2f}, lift {lift:+.2f}. Success bar: lift of at least +0.10.", "",
              "| Feature | Standardized weight | Raw weight |", "| --- | --- | --- |"]
        for f, cs, c in sorted(zip(m["features"], m["coef_std"], m["coef"]), key=lambda t: -abs(t[1])):
            L.append(f"| {f} | {cs:+.3f} | {c:+.3f} |")
        if key == "effort":
            L += ["", "### Individual complexity probes (chronological feature ablations)", "",
                  "| Dimension | Removed feature(s) | Holdout Spearman without | Full minus without |",
                  "| --- | --- | ---: | ---: |"]
            for dim, a in m.get("dimension_ablations", {}).items():
                L.append(f"| {dim} | {', '.join(a['removed_features'])} | "
                         f"{a['without_cv_spearman']:.3f} | {a['delta_spearman']:+.3f} |")
            L += ["", "Negative deltas mean the ablated model did better on this holdout. "
                  "D is only the added value of novelty beyond log_size (which still embeds depth). "
                  "Because size is composite and signals correlate, these are *conditional predictive* "
                  "probes, not isolated causal contributions, theoretical validation, or robust "
                  "per-dimension confidence intervals. R (Cynefin) is evaluated separately against "
                  "follow-up defects below.", ""]
            b = m["coef"][m["features"].index("log_size")]
            L += ["", f"Observational weight on log_size: {b:.2f}. This is not a causal size elasticity; "
                      "a value above 1 does not establish that splitting saves reviewer time."]
        L.append("")
    L += ["## Counterweight: did faster review let defects through?", "",
          "Follow-up fix rates exclude PRs without a complete observable 14-day follow-up window. "
          "These signals are noisy and not causal evidence of review quality.", "",
          "| Group | PRs | Reverted | Follow-up fix within 14 days (observable n) |", "| --- | --- | --- | --- |"]
    groups = defaultdict(list)
    for r in records:
        groups[r["regime"]].append(r)
    for name in ("Clear", "Complicated", "Complex", "Chaotic"):
        g = groups.get(name, [])
        if g:
            obs = [r for r in g if r["followup_fix"] is not None]
            rate = f"{sum(r['followup_fix'] for r in obs) / len(obs):.0%} ({len(obs)})" if obs else "n/a (0)"
            L.append(f"| {name} | {len(g)} | {sum(r['reverted'] for r in g) / len(g):.0%} | "
                     f"{rate} |")
    for i, q in enumerate(_quintiles(records, lambda r: r["effort_ru"]), 1):
        if q:
            obs = [r for r in q if r["followup_fix"] is not None]
            rate = f"{sum(r['followup_fix'] for r in obs) / len(obs):.0%} ({len(obs)})" if obs else "n/a (0)"
            L.append(f"| Effort quintile {i} | {len(q)} | {sum(r['reverted'] for r in q) / len(q):.0%} | "
                     f"{rate} |")
    L += ["", "## Rubber-stamp check: review comments per 1,000 RU, by size", "",
          "| Size quintile | Median RU | Comments per 1,000 RU |", "| --- | --- | --- |"]
    for i, q in enumerate(_quintiles(records, lambda r: r["effort_ru"]), 1):
        if q:
            ru = sorted(r["effort_ru"] for r in q)[len(q) // 2]
            per = 1000 * sum(r["comments"] for r in q) / max(1.0, sum(r["effort_ru"] for r in q))
            L.append(f"| {i} | {ru:,.0f} | {per:.2f} |")
    L += ["", "If comments per RU fall steeply as size grows, big PRs are being skimmed: that supports splitting "
              "on quality grounds even if it doesn't save time.", ""]
    return "\n".join(L)


def apply(calibration: dict, row: dict[str, float]) -> dict[str, float]:
    out = {}
    if "effort" in calibration:
        out[calibration["effort"]["label"]] = math.expm1(predict(calibration["effort"], row))
    if "wait" in calibration:
        out["wait_hours"] = math.expm1(predict(calibration["wait"], row))
    return out
