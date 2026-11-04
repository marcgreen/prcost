"""prcost command line."""
from __future__ import annotations

import argparse
import json
import math
import sys

from . import calibrate
from .describe import REGIME_MODE, review_guide
from .dimensions import complexity_dimensions
from .model import Analysis, Params


def _analysis(args) -> Analysis:
    return Analysis(args.repo, args.base, args.head, Params(), hotfix=args.hotfix)


def _name(an: Analysis, email: str | None) -> str:
    if not email:
        return "(none)"
    n = an.fam.names.get(email)
    return f"{n} <{email}>" if n else email


def _summary(an: Analysis, reviewer_query: str | None, calibration: dict | None) -> dict:
    p = an.p
    est = an.whole()
    fixed = an.resolve_reviewer(reviewer_query)
    best = an.reviewers(est)
    regime, reasons = an.regime(est, best[0] if best else None)
    # An explicit unknown reviewer must not quietly fall back to the cheapest candidate.
    fixed_for_split = (fixed or "") if reviewer_query else None
    parts, whole_eff, split_eff = an.split_plan(fixed_for_split)
    F_used = (an.familiarity(fixed, est) if fixed else 0.0) if reviewer_query else (best[0][1] if best else 0.0)
    P_total = sum(c.interactions for c in est.clusters)
    out = {
        "base": an.base, "head": an.head,
        "files": len(an.sig), "ignored": an.ignored,
        "lines": {"logic": sum(s.logic for s in an.sig.values()), "shallow": an.shallow_total},
        "regime": regime, "regime_reasons": reasons, "review_mode": REGIME_MODE[regime],
        "effort_ru": {
            "reviewer": {"who": fixed or reviewer_query, "known_history": bool(fixed), "familiarity": F_used, "effort": est.for_reviewer(F_used, p)} if reviewer_query else None,
            "candidates": [{"who": r, "familiarity": f, "effort": e} for r, f, e in best],
            "unfamiliar": est.for_reviewer(0.0, p),
        },
        "factors": {
            "size_S": est.size,
            "logic_depth": sum(c.depth for c in est.clusters),
            "shallow_lines": est.shallow_lines,
            "size_multiplier": (est.size / p.ref_size) ** p.eps if est.size else 1.0,
            "clusters": [{"files": c.files, "depth": c.depth, "interactions": c.interactions,
                          "interaction_ru": c.interaction_ru,
                          "entropy_bits": c.entropy, "cost": c.cost} for c in est.clusters],
            "interactions_total": P_total,
            "interaction_ru": sum(c.interaction_ru for c in est.clusters),
            "entropy_across_bits": est.entropy_across,
            "entropy_multiplier": 1 + p.theta * est.entropy_across,
            "blast_radius": est.blast, "blast_files": len(est.blast_files),
            "blast_multiplier": 1 + p.kappa * math.log2(1 + est.blast),
            "certificates": est.certificates, "uncovered_sources": est.uncovered,
            "certificate_multiplier": 1 - p.tau * est.certificates,
            "familiarity_multiplier": 1 + p.alpha * (1 - F_used),
        },
        "complexity_dimensions": complexity_dimensions(an, est, F_used, regime, reasons),
        "files_detail": [
            {"path": s.path, "status": s.status, "logic_added": s.logic_added, "logic_removed": s.logic_removed,
             "shallow": s.shallow, "shallow_kinds": s.shallow_kinds, "branches": s.branches,
             "novel_bytes": s.novel_bytes, "depth": s.depth}
            for s in sorted(an.sig.values(), key=lambda s: -s.depth)
        ],
        "routing_warning": ("No non-author reviewer with matching git history. "
                            "Find a qualified independent reviewer and provide extra context."
                            if not best else None),
        "limitations": [
            "RU are unvalidated relative heuristics, not observed reviewer minutes.",
            "Familiarity measures past authorship, not reviewer skill, availability or independence.",
            "Split comparisons simulate assumptions in Params and are not calibrated predictions.",
            "Changed tests are a co-change proxy, not proof that behavior is exercised.",
        ],
        "split": {
            "status": "exploratory_only",
            "parts": [{"name": pt.name, "logic_files": pt.logic_files, "shallow_lines": pt.shallow_lines,
                       "stacked_on": pt.stacked_on, "reviewer": pt.reviewer, "familiarity": pt.familiarity,
                       "effort": pt.effort} for pt in parts],
            "whole_effort": whole_eff, "split_effort": split_eff,
        },
        "cochange_gaps": [{"changed": f, "usually_with": g, "times": n, "share": c}
                          for f, g, n, c in an.cochange_gaps()],
    }
    if calibration:
        row = calibrate.feature_row(an, est, F_used)
        out["calibrated"] = calibrate.apply(calibration, row)
    return out


def _print(an: Analysis, s: dict) -> None:
    f = s["factors"]
    w = print
    w(f"prcost  {s['base'][:8]}..{s['head'][:8]}  {s['files']} files, "
      f"{s['lines']['logic']} logic lines, {s['lines']['shallow']} shallow lines")
    w("")
    w(f"Advisory workflow: {s['regime']}. {'; '.join(s['regime_reasons']).capitalize()}.")
    w(f"  Review mode: {s['review_mode']}")
    w("")
    if s["routing_warning"]:
        w(f"  Routing note: {s['routing_warning']}")
    w("Heuristic effort (unvalidated RU; NOT minutes or proven reviewer cost)")
    e = s["effort_ru"]
    if e["reviewer"]:
        r = e["reviewer"]
        w(f"  requested  {_name(an, r['who']):<44} familiarity {r['familiarity']:.2f}  {r['effort']:>8,.0f}")
    for i, c in enumerate(e["candidates"], 1):
        w(f"  option {i}   {_name(an, c['who']):<44} familiarity {c['familiarity']:.2f}  {c['effort']:>8,.0f}")
    w(f"  {'unfamiliar reviewer':<55} familiarity 0.00  {e['unfamiliar']:>8,.0f}")
    if "calibrated" in s:
        for k, v in s["calibrated"].items():
            w(f"  calibrated prediction: {k} = {v:,.1f}")
    w("")
    w("Seven complexity dimensions (theory-inspired proxies; not validated measures)")
    w("  ID  Dimension                 Measured proxy                           Prior delta RU")
    for d in s["complexity_dimensions"]:
        m = d["measured"]
        key = d["id"]
        value = {
            "D": lambda: f"{m['novelty_depth_ru']:,.0f} depth RU",
            "F": lambda: f"{m['familiarity']:.2f} familiarity",
            "P": lambda: f"{m['coupling_edges']} coupled edges",
            "H": lambda: f"{m['across_cluster_bits']:.2f} cross-cluster bits",
            "B": lambda: f"{m['weighted_reach']:.1f} weighted reach",
            "T": lambda: f"{m['changed_test_relationship_share']:.0%} related changed tests",
            "R": lambda: m['regime'],
        }[key]()
        impact = f"{d['prior_delta_ru']:+,.0f}" if d['prior_delta_ru'] is not None else "n/a"
        w(f"  {key:<3} {d['name']:<25} {value:<40} {impact:>9}")
    w("  D and R have no separable prior delta. Other deltas switch one hand-set coefficient")
    w("  off at a time; they are nonlinear, nonadditive and NOT empirical effects.")
    w("  Each dimension has unassessed measurement confidence and unvalidated empirical validity.")
    w("  See COMPLEXITY.md and score --json for definitions and per-dimension limitations.")
    w("")
    w("Why")
    w(f"  size S              {f['size_S']:>8,.0f} RU  (logic depth {f['logic_depth']:,.0f} + interactions "
      f"{an.p.beta:g} x {f['interaction_ru']:,.0f} + {f['shallow_lines']} shallow lines x {an.p.shallow_ru:g}, "
      f"times within-cluster entropy)")
    w(f"  size multiplier     x{f['size_multiplier']:.2f}     (S / {an.p.ref_size:g})^{an.p.eps:g}")
    w(f"  interactions        {f['interactions_total']:>8}     coupling edges inside {len(f['clusters'])} cluster(s)")
    w(f"  entropy across      x{f['entropy_multiplier']:.2f}     {f['entropy_across_bits']:.2f} bits")
    w(f"  blast radius        x{f['blast_multiplier']:.2f}     {f['blast_radius']:.1f} weighted, {f['blast_files']} dependent files")
    w(f"  test co-change      x{f['certificate_multiplier']:.2f}     {f['certificates']:.0%} of changed source files have related changed tests (not verified coverage)")
    w("")
    w("Clusters (independent groups add up; each coupling edge inside one adds interaction cost)")
    for c in f["clusters"][:8]:
        names = ", ".join(c["files"][:4]) + (f" +{len(c['files']) - 4}" if len(c["files"]) > 4 else "")
        w(f"  {c['cost']:>8,.0f} RU  depth {c['depth']:,.0f}, edges {c['interactions']}, "
          f"entropy {c['entropy_bits']:.2f}  [{names}]")
    w("")
    w("Files by depth")
    w(f"  {'depth':>8}  {'logic +/-':>10}  {'shallow':>7}  path")
    for d in s["files_detail"][:12]:
        w(f"  {d['depth']:>8,.0f}  {'+' + str(d['logic_added']) + '/-' + str(d['logic_removed']):>10}  "
          f"{d['shallow']:>7}  {d['path']} ({d['status']})")
    if len(s["files_detail"]) > 12:
        w(f"  ... {len(s['files_detail']) - 12} more")
    sp = s["split"]
    w("")
    if sp["parts"]:
        saved = sp["whole_effort"] - sp["split_effort"]
        worth = saved > 0
        w("Exploratory split candidate" if worth else "Exploratory partition (model does not favor splitting)")
        for i, pt in enumerate(sp["parts"], 1):
            stack = f" (stack on part {[q['name'] for q in sp['parts']].index(pt['stacked_on']) + 1})" if pt["stacked_on"] else ""
            n = len(pt["logic_files"])
            what = f"{n} file{'s' if n != 1 else ''}" if n else f"{pt['shallow_lines']} lower-effort lines"
            w(f"  {i}. {pt['name']}{stack}")
            who = _name(an, pt["reviewer"]) if pt["reviewer"] else "unfamiliar reviewer"
            w(f"     {what}, {pt['effort']:,.0f} RU, reviewer: {who}")
        w(f"  Whole PR {sp['whole_effort']:,.0f} RU vs split {sp['split_effort']:,.0f} RU "
          f"(incl. {an.p.split_overhead:g} RU per extra PR): {'saves' if worth else 'costs'} "
          f"{abs(saved):,.0f} RU ({abs(saved) / max(1, sp['whole_effort']):.0%}).")
        w("  Experimental what-if only: verify functional independence, review overhead and author effort before splitting.")
        if worth:
            w("  Claimed model savings rely on superlinear effort and reviewer reassignment; neither is validated.")
    else:
        w("Exploratory split: none under current graph assumptions.")
    if s["cochange_gaps"]:
        w("")
        w("Possibly missing changes (usually change together, untouched here)")
        for g in s["cochange_gaps"]:
            w(f"  {g['usually_with']}  ({g['share']:.0%} of commits touching {g['changed']})")
    w("")
    w("Caution: review routing, risk labels and split comparisons are advisory. Validate intent, tests and ownership.")
    if s["ignored"]:
        w("")
        w(f"Ignored (generated, lock, binary or vendored): {len(s['ignored'])} files")


def _print_brief(an: Analysis, s: dict) -> None:
    """Short, actionable view; the full report and JSON retain the complete signal detail."""
    f, e, split = s["factors"], s["effort_ru"], s["split"]
    print(f"prcost  {s['base'][:8]}..{s['head'][:8]}  {s['files']} files; "
          f"{s['lines']['logic']} semantic / {s['lines']['shallow']} lower-effort lines")
    print(f"Advisory workflow: {s['regime']} — {'; '.join(s['regime_reasons'])}")
    print("Review first: " + (", ".join(d["path"] for d in s["files_detail"][:4]) or "no source changes detected"))
    print(f"Interactions: {f['interactions_total']} coupled changed-file edges; "
          f"blast radius: {f['blast_files']} dependent files (graph approximation)")
    print(f"Related changed tests: {f['certificates']:.0%} of changed source files (NOT verified coverage)")
    if f["uncovered_sources"]:
        print("Test changes to examine: " + ", ".join(f["uncovered_sources"][:5]))
    if s["cochange_gaps"]:
        print("Potential coupling gap: " + ", ".join(g["usually_with"] for g in s["cochange_gaps"][:3]))
    if e["reviewer"]:
        q = e["reviewer"]
        print(f"Requested reviewer: {_name(an, q['who'])}, familiarity {q['familiarity']:.2f}" +
              (" (no matched git history)" if not q["known_history"] else ""))
    elif e["candidates"]:
        q = e["candidates"][0]
        print(f"Most familiar available-history candidate: {_name(an, q['who'])}; "
              f"familiarity {q['familiarity']:.2f} (not an assignment)")
    if split["parts"]:
        candidate = split["whole_effort"] > split["split_effort"]
        print(f"Splitting: {'model favors candidate partition' if candidate else 'model does not favor it'} "
              "(unvalidated what-if, never an automatic recommendation)")
    if s["routing_warning"]:
        print("Routing note: " + s["routing_warning"])
    print("RU and workflow labels are experimental; verify behavior, ownership, test evidence and review load.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="prcost", description="Analyze PR reviewability and experimental effort heuristics.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("--repo", default=".", help="path to the git repository (default: .)")
        sp.add_argument("--base", default=None, help="base branch or commit (default: origin/HEAD, main or master)")
        sp.add_argument("--head", default="HEAD", help="head commit (default: HEAD)")
        sp.add_argument("--reviewer", default=None, help="reviewer email or name")
        sp.add_argument("--hotfix", action="store_true", help="treat as a hotfix (Chaotic regime)")
        sp.add_argument("--calibration", default=None, help="calibration.json from `prcost calibrate fit`")

    sc = sub.add_parser("score", help="advisory analysis: relative effort, reviewer familiarity, coupling, what-if splits")
    common(sc)
    sc.add_argument("--json", action="store_true", help="machine-readable output")
    sc.add_argument("--brief", action="store_true", help="short actionable summary instead of detailed factors")

    de = sub.add_parser("describe", help="write a reviewer-tailored PR description (markdown)")
    common(de)

    ca = sub.add_parser("calibrate", help="fit weights on a repo's PR history")
    csub = ca.add_subparsers(dest="step", required=True)
    fe = csub.add_parser("fetch", help="download merged PR history with gh")
    fe.add_argument("--gh-repo", required=True, help="owner/name")
    fe.add_argument("--limit", type=int, default=300)
    fe.add_argument("--out", default="prs.jsonl")
    fi = csub.add_parser("fit", help="fit models from prs.jsonl and a local clone")
    fi.add_argument("--repo", default=".")
    fi.add_argument("--prs", default="prs.jsonl")
    fi.add_argument("--minutes", default=None, help="optional CSV with columns pr,minutes (self-reported)")
    fi.add_argument("--remote", default="origin")
    fi.add_argument("--per-pr-graph", dest="per_pr_graph", action="store_true", default=True,
                    help="historically correct graph and commit ancestry (default)")
    fi.add_argument("--fast-shared-graph", dest="per_pr_graph", action="store_false",
                    help="approximate fast mode; leaks future code relationships into older PRs")
    fi.add_argument("--out", default="calibration.json")
    fi.add_argument("--report", default="calibration-report.md")

    args = ap.parse_args(argv)
    log = lambda m: print(m, file=sys.stderr)
    if args.cmd == "calibrate":
        if args.step == "fetch":
            n = calibrate.fetch(args.gh_repo, args.limit, args.out, log)
            log(f"wrote {n} PRs to {args.out}")
        else:
            res = calibrate.fit(args.repo, args.prs, args.out, args.report, args.minutes, args.remote,
                                args.per_pr_graph, log)
            log(f"wrote {args.out} and {args.report} ({res['n_records']} PRs)")
        return 0

    if args.calibration:
        with open(args.calibration, encoding="utf-8") as fh:
            cal = json.load(fh)
    else:
        cal = None
    an = _analysis(args)
    if args.cmd == "score":
        s = _summary(an, args.reviewer, cal)
        if args.json:
            print(json.dumps(s, indent=2, default=str))
        elif args.brief:
            _print_brief(an, s)
        else:
            _print(an, s)
        return 0
    est = an.whole()
    best = an.reviewers(est)
    person = an.resolve_reviewer(args.reviewer) if args.reviewer else (best[0][0] if best else None)
    if args.reviewer and not person:
        log(f"warning: no git history for reviewer '{args.reviewer}'; treating them as unfamiliar")
    regime, reasons = an.regime(est, best[0] if best else None)
    print(review_guide(an, est, person, regime, reasons, best[0][2] if best else None))
    return 0


def run() -> int:
    try:
        return main()
    except BrokenPipeError:  # output piped into head, less, etc.
        sys.stderr.close()
        return 0


if __name__ == "__main__":
    sys.exit(run())
