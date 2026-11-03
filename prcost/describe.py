"""Reviewer-tailored PR description: send the reviewer only what they don't already know."""
from __future__ import annotations

from .model import Analysis, Estimate

REGIME_MODE = {
    "Clear": "One qualified reviewer; verify mechanical changes and behavior at affected boundaries.",
    "Complicated": "Route to the reviewer who knows this code best. Work through the interactions below.",
    "Complex": "Split into small safe-to-fail steps (feature flag if possible). Review together, live or "
               "in pairs, and watch production signals after merge.",
    "Chaotic": "Prioritize mitigation and a qualified expedited review; keep mandatory safety checks, then follow up within 48 hours.",
}


def reading_order(an: Analysis, files: list[str]) -> list[str]:
    """Dependencies before dependents (Kahn's algorithm on import edges among the given files)."""
    fset = set(files)
    deps = {f: an.graph.imports.get(f, set()) & fset for f in files}
    order, done = [], set()
    remaining = set(files)
    while remaining:
        ready = sorted((f for f in remaining if deps[f] <= done), key=lambda f: -an.sig[f].depth)
        if not ready:  # cycle: take the deepest file and continue
            ready = [max(remaining, key=lambda f: an.sig[f].depth)]
        for f in ready:
            order.append(f)
            done.add(f)
            remaining.discard(f)
    return order


def review_guide(an: Analysis, est: Estimate, person: str | None, regime: str, reasons: list[str],
                 best_effort: float | None) -> str:
    p = an.p
    name = an.fam.names.get(person, person) if person else "an unfamiliar reviewer"
    F = an.familiarity(person, est)
    effort = est.for_reviewer(F, p)
    fam = {f: an.fam.of(person, f, an.aliases.get(f)) if person else 0.0 for f in est.weights}
    out = [f"## Review guide for {name}", ""]
    line = f"**Estimated effort for you:** {effort:,.0f} RU"
    if best_effort is not None and best_effort < effort * 0.95:
        line += f" (cheapest available reviewer: {best_effort:,.0f} RU)"
    out += [line + ". RU are experimental relative units, not measured minutes or reviewer performance.", ""]
    out += [f"**Regime: {regime}.** {'; '.join(reasons).capitalize()}. {REGIME_MODE[regime]}", ""]
    out += ["**Intent (author fills in):** _What should be true after this merges, and how did you check it?_", ""]

    if est.logic_files:
        out += ["### Read in this order", ""]
        srcs_first = [f for f in est.logic_files if not an.sig[f].test]
        tests = [f for f in est.logic_files if an.sig[f].test]
        order = reading_order(an, srcs_first) + sorted(tests, key=lambda f: -an.sig[f].depth)
        shown = order[:15]
        for i, f in enumerate(shown, 1):
            s = an.sig[f]
            known = fam[f] >= 0.6
            tag = "you know this file" if known else ("partly familiar" if fam[f] >= 0.25 else "new to you")
            users = sorted(g for g in an.graph.importers.get(f, ()) if g in est.logic_files)
            why = f", used by {len(users)} other changed file{'s' if len(users) != 1 else ''}" if users else ""
            out.append(f"{i}. `{f}` ({s.status}, +{s.logic_added}/-{s.logic_removed} logic lines, {tag}{why})")
            if not known and not s.test:
                ctx = sorted(an.graph.imports.get(f, set()) - set(an.sig), key=lambda g: -len(an.graph.importers.get(g, ())))
                if ctx:
                    out.append(f"    - Context worth opening first: {', '.join(f'`{c}`' for c in ctx[:3])}")
        if len(order) > len(shown):
            out.append(f"\n...and {len(order) - len(shown)} more, mostly tests.")
        out.append("")

    edges = [e for c in est.clusters for e in c.edges if e[2] != "test"]
    if edges:
        out += ["### Interactions to check", ""]
        for a, b, kind in edges[:10]:
            how = "imports" if kind == "import" else "historically change together"
            out.append(f"- `{a}` and `{b}` ({how}): does the change on one side hold for the other?")
        out.append("")

    if est.shallow_lines:
        files = [f for f in an.shallow_paths if an.sig[f].shallow]
        kinds: dict[str, int] = {}
        for f in files:
            for k, v in an.sig[f].shallow_kinds.items():
                kinds[k] = kinds.get(k, 0) + v
        summary = ", ".join(f"{v} {k}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1]))
        out += ["### Lower-effort changes to verify", "", f"{est.shallow_lines} lines across {len(files)} files are classified as lower-effort: {summary}. Validate imports, renames, call sites and behavior; do not assume they are safe.", ""]

    if est.blast_files:
        top = sorted(est.blast_files, key=lambda f: (est.blast_files[f], f))[:5]
        out += ["### Blast radius", "",
                f"{len(est.blast_files)} files depend on the changed code (weighted {est.blast:.1f}). Closest: "
                + ", ".join(f"`{f}`" for f in top) + ".", ""]

    gaps = an.cochange_gaps()
    if gaps:
        out += ["### Possibly missing changes", ""]
        for f, g, n, conf in gaps:
            out.append(f"- `{g}` changed in {conf:.0%} of past commits touching `{f}` ({n} times), but not here.")
        out.append("")

    srcs = [f for f in est.logic_files if not an.sig[f].test and an.sig[f].status != "D" and an.sig[f].lang in ("py", "js")]
    if srcs:
        covered = len(srcs) - len(est.uncovered)
        out += ["### Tests as evidence", "", f"Changed source files with changed tests: {covered} of {len(srcs)}."]
        if est.uncovered:
            out.append("Without test changes: " + ", ".join(f"`{f}`" for f in est.uncovered[:6]) + ".")
        out.append("")
    out.append("_Generated heuristically by prcost. Test co-changes are not verified test coverage. The author must provide intent and evidence._")
    return "\n".join(out)
