"""Signals, the effort model, the Cynefin regime and split planning.

RU are heuristic relative units constructed from zlib-compressed novelty, branch tokens,
and hand-set multipliers. They are not logical depth or reviewer time. A fitted model
may separately predict observed labels; it does not validate split what-ifs.
"""
from __future__ import annotations

import math
import posixpath
import re
import zlib
from dataclasses import dataclass, field

from . import gitio
from .graph import Graph, blast_radius, build_graph, components, coupling_edges, test_subject
from .lines import classify_diff, is_ignored, is_test, lang_of
from .people import Familiarity

FIX_RE = re.compile(r"\b(fix|fixes|fixed|bug|bugfix|revert|hotfix|regression)\b", re.I)
HOTFIX_RE = re.compile(r"\b(hotfix|incident|sev[0-2])\b", re.I)
BARREL_RE = re.compile(r"(^|/)(__init__\.py|index\.[cm]?[jt]sx?)$")


@dataclass
class Params:
    """Hand-set priors. Calibration replaces the size/interaction/etc. weights with fitted ones."""
    shallow_ru: float = 2.0        # s: RU per shallow line (moved, renamed, comment...)
    ref_size: float = 1500.0       # R: size at which the superlinear term is neutral
    eps: float = 0.25              # size exponent is 1 + eps
    beta: float = 0.5              # each coupling edge adds beta x the smaller endpoint's depth
    theta: float = 0.1             # per bit of change entropy
    kappa: float = 0.08            # per doubling of blast radius
    alpha: float = 1.0             # unfamiliar reviewer costs (1 + alpha) times a fully familiar one
    tau: float = 0.2               # discount when every changed source file has changed tests
    test_weight: float = 0.5       # test code depth counts at this weight
    removed_weight: float = 0.5    # removed logic lines, relative to added
    deleted_weight: float = 0.2    # lines of wholly deleted files
    split_overhead: float = 150.0  # fixed RU cost of each extra PR
    clear_max_ru: float = 400.0    # Clear regime: at most this effort, no interactions
    risky_fix_ratio: float = 0.2   # Complex: share of recent commits on these files that were fixes...
    risky_fix_lift: float = 1.5    # ...and at least this multiple of the repo-wide fix share
    big_blast: float = 40.0        # Complex if blast radius above this and no test changes


@dataclass
class FileSignal:
    path: str
    old_path: str | None
    status: str
    lang: str
    test: bool
    shallow: int
    shallow_kinds: dict
    logic_added: int
    logic_removed: int
    branches: int
    state: int
    novel_bytes: float
    depth: float

    @property
    def logic(self) -> int:
        return self.logic_added + self.logic_removed


@dataclass
class Cluster:
    files: list[str]
    depth: float
    interactions: int          # coupling edges (tests excluded)
    interaction_ru: float      # sum over edges of min(depth a, depth b)
    entropy: float
    cost: float
    edges: list[tuple[str, str, str]]


@dataclass
class Estimate:
    logic_files: list[str]
    shallow_lines: int
    clusters: list[Cluster]
    size: float              # S
    size_term: float         # S * (S/R)^eps
    entropy_across: float
    blast: float
    blast_files: dict[str, int]
    certificates: float      # T: test co-change proxy, not verified coverage
    uncovered: list[str]
    base_effort: float       # everything except the reviewer factor
    weights: dict[str, float]

    def for_reviewer(self, familiarity: float, p: Params) -> float:
        return self.base_effort * (1 + p.alpha * (1 - familiarity))


def zlen(s: str) -> int:
    return len(zlib.compress(s.encode("utf-8", "replace"), 9))


def novel_bytes(context: str, extra: list[str]) -> float:
    """Approximate compression novelty, NOT Kolmogorov complexity or logical depth."""
    if not extra:
        return 0.0
    ctx = context or ""
    return float(max(0, zlen(ctx + "\n" + "\n".join(extra)) - zlen(ctx)))


def entropy(weights) -> float:
    total = sum(w for w in weights if w > 0)
    if total <= 0:
        return 0.0
    return max(0.0, -sum((w / total) * math.log2(w / total) for w in weights if w > 0))


def common_dir(paths: list[str]) -> str:
    if not paths:
        return ""
    if len(paths) == 1:
        return paths[0]
    d = posixpath.commonpath(paths)
    return (d + "/") if d else "(repo root)"


def part_name(paths: list[str], sig: dict) -> str:
    src = [p for p in paths if not sig[p].test] or paths
    top = max(src, key=lambda p: sig[p].depth)
    more = len(paths) - 1
    return f"{top}" + (f" and {more} related file{'s' if more != 1 else ''}" if more else "")


@dataclass
class Part:
    name: str
    logic_files: list[str]
    shallow_lines: int
    shallow_files: list[str]
    extra_ru: float = 0.0
    stacked_on: str | None = None
    reviewer: str | None = None
    familiarity: float = 0.0
    effort: float = 0.0


class Analysis:
    def __init__(self, repo: str, base_ref: str | None, head_ref: str = "HEAD", params: Params | None = None,
                 history_days: int = 730, max_commits: int = 5000, hotfix: bool = False,
                 graph: Graph | None = None, commits: list | None = None, base_is_exact: bool = False):
        """graph/commits: precomputed, shared across many PRs during calibration. Commits are then
        filtered to those before this PR's base, so familiarity never sees the future."""
        self.repo = repo
        self.p = params or Params()
        self.head = gitio.rev_parse(repo, head_ref)
        base_tip = gitio.rev_parse(repo, base_ref or gitio.default_base(repo))
        self.base = base_tip if base_is_exact else gitio.merge_base(repo, base_tip, self.head)
        self.head_ts = gitio.commit_time(repo, self.head)
        author_ids = gitio.range_authors(repo, self.base, self.head)
        self.subjects = gitio.run(repo, "log", "--format=%s", f"{self.base}..{self.head}").splitlines()
        self.hotfix = hotfix or any(HOTFIX_RE.search(s) for s in self.subjects)

        self.changes = gitio.diff(repo, self.base, self.head)
        since = self.head_ts - history_days * 86400
        if commits is None:
            self.commits = gitio.log_commits(repo, self.base, since_ts=since, max_count=max_commits)
        else:
            base_ts = gitio.commit_time(repo, self.base)
            self.commits = [c for c in commits if since <= c.ts <= base_ts]
        self.fam = Familiarity.from_commits(self.commits, self.head_ts)
        self.authors = {self.fam.canonical(e, n) for e, n in author_ids}
        reader = gitio.BlobReader(repo)
        try:
            if graph is None:
                files_at_head = gitio.ls_tree(repo, self.head)
                graph = build_graph(lambda p: reader.read(self.head, p), files_at_head, self.commits)
            self.graph: Graph = graph
            self._signals(reader)
        finally:
            reader.close()
        self.aliases = {s.path: s.old_path for s in self.sig.values() if s.old_path and s.old_path != s.path}
        self.logic_paths = sorted(p for p, s in self.sig.items() if s.logic > 0)
        self.shallow_paths = sorted(p for p, s in self.sig.items() if s.shallow > 0)
        self.shallow_total = sum(s.shallow for s in self.sig.values())
        self.tests_changed = [p for p, s in self.sig.items() if s.test and s.status != "D"]

    # ---------------------------------------------------------------- signals

    def _signals(self, reader: gitio.BlobReader) -> None:
        p = self.p
        lines = classify_diff(self.changes)
        self.sig: dict[str, FileSignal] = {}
        self.ignored: list[str] = []
        for fc in self.changes:
            fl = lines.get(fc.path)
            if fl is None:
                self.ignored.append(fc.path)
                continue
            old_text = (reader.read(self.base, fc.old_path) or "") if fc.old_path else ""
            new_text = (reader.read(self.head, fc.new_path) or "") if fc.new_path else ""
            if fc.status == "R" and not fc.added and not fc.removed:
                fl.shallow += 1  # a path change needs validation even with identical content
                fl.kinds["path_move"] += 1
            rem_w = p.deleted_weight if fc.status == "D" else p.removed_weight
            nb = novel_bytes(old_text, fl.logic_added) + rem_w * novel_bytes(new_text, fl.logic_removed)
            delta = fl.branches / max(1, fl.logic_lines)
            test = is_test(fc.path)
            depth = nb * (1 + delta) * (p.test_weight if test else 1.0)
            self.sig[fc.path] = FileSignal(
                path=fc.path, old_path=fc.old_path, status=fc.status, lang=fl.lang, test=test,
                shallow=fl.shallow, shallow_kinds=dict(fl.kinds), logic_added=len(fl.logic_added),
                logic_removed=len(fl.logic_removed), branches=fl.branches, state=fl.state,
                novel_bytes=nb, depth=depth,
            )

    # ---------------------------------------------------------------- the model

    def interaction_ru(self, edges) -> float:
        return sum(min(self.sig[a].depth, self.sig[b].depth) for a, b, k in edges if k != "test")

    def estimate(self, logic_files: list[str], shallow_lines: int, shallow_files: list[str],
                 extra_ru: float = 0.0) -> Estimate:
        p = self.p
        edges = self.edges(logic_files)
        clusters: list[Cluster] = []
        for comp in components(logic_files, edges):
            cset = set(comp)
            c_edges = [e for e in edges if e[0] in cset and e[1] in cset]
            P = sum(1 for e in c_edges if e[2] != "test")
            I = self.interaction_ru(c_edges)
            depths = [self.sig[f].depth for f in comp]
            H = entropy(depths)
            D = sum(depths)
            clusters.append(Cluster(comp, D, P, I, H, (D + p.beta * I) * (1 + p.theta * H), c_edges))
        if extra_ru and clusters:
            big = max(clusters, key=lambda c: c.depth)
            big.interaction_ru += extra_ru
            big.cost = (big.depth + p.beta * big.interaction_ru) * (1 + p.theta * big.entropy)
        clusters.sort(key=lambda c: -c.cost)

        S = sum(c.cost for c in clusters) + p.shallow_ru * shallow_lines
        size_term = S * (S / p.ref_size) ** p.eps if S > 0 else 0.0
        h_across = entropy([c.cost for c in clusters])
        live = {f for f in logic_files if self.sig[f].status != "D"}
        B, dist = blast_radius(self.graph, live, skip=is_test)

        sources = [f for f in logic_files
                   if not self.sig[f].test and self.sig[f].status != "D" and self.sig[f].lang in ("py", "js")]
        uncovered = [f for f in sources if not self._covered(f)]
        T = (len(sources) - len(uncovered)) / len(sources) if sources else 0.0

        base = (size_term * (1 + p.theta * h_across) * (1 + p.kappa * math.log2(1 + B)) * (1 - p.tau * T))
        weights = {f: self.sig[f].depth for f in logic_files}
        for f in shallow_files:
            weights[f] = weights.get(f, 0.0) + p.shallow_ru * self.sig[f].shallow
        return Estimate(logic_files, shallow_lines, clusters, S, size_term, h_across, B, dist, T,
                        uncovered, base, weights)

    def _covers(self, t: str, src: str) -> bool:
        """Test t covers src if it imports src directly, through one re-export hub
        (__init__.py, index.ts), or matches it by name."""
        deps = self.graph.imports.get(t, set())
        if src in deps or test_subject(t, {src}) == src:
            return True
        return any(BARREL_RE.search(hub) and src in self.graph.imports.get(hub, ()) for hub in deps)

    def _covered(self, src: str) -> bool:
        return any(self._covers(t, src) for t in self.tests_changed)

    def edges(self, logic_files: list[str]) -> list[tuple[str, str, str]]:
        """Coupling edges plus test edges, so each changed test joins the cluster it certifies."""
        edges = coupling_edges(self.graph, logic_files)
        have = {(a, b) for a, b, _ in edges}
        srcs = [f for f in logic_files if not self.sig[f].test]
        for t in logic_files:
            if not self.sig[t].test:
                continue
            for src in srcs:
                key = (t, src) if t < src else (src, t)
                if key not in have and self._covers(t, src):
                    edges.append((*key, "test"))
                    have.add(key)
        return edges

    def whole(self) -> Estimate:
        return self.estimate(self.logic_paths, self.shallow_total, self.shallow_paths)

    def familiarity(self, person: str | None, est: Estimate) -> float:
        if not person:
            return 0.0
        return self.fam.weighted(person, est.weights, self.aliases)

    def reviewers(self, est: Estimate, top: int = 3) -> list[tuple[str, float, float]]:
        """(email, familiarity, effort) for the cheapest candidate reviewers, PR authors excluded."""
        cands = self.fam.candidates(list(est.weights), self.aliases, exclude=self.authors)
        scored = []
        for person in cands:
            F = self.familiarity(person, est)
            if F > 0.02:
                scored.append((person, F, est.for_reviewer(F, self.p)))
        scored.sort(key=lambda r: r[2])
        return scored[:top]

    def resolve_reviewer(self, query: str | None) -> str | None:
        return self.fam.match(query) if query else None

    # ---------------------------------------------------------------- Cynefin

    def history_risk(self, days: int = 180) -> tuple[int, int, float]:
        """(fix commits, commits) touching these files, and the repo-wide fix share, last `days`."""
        existing = {f for f in self.logic_paths if self.sig[f].status != "A"}
        existing |= {self.sig[f].old_path for f in existing if self.sig[f].old_path}
        cutoff = self.head_ts - days * 86400
        recent = [c for c in self.commits if c.ts >= cutoff]
        touching = [c for c in recent if existing.intersection(c.files)]
        base_rate = sum(1 for c in recent if FIX_RE.search(c.subject)) / max(1, len(recent))
        return sum(1 for c in touching if FIX_RE.search(c.subject)), len(touching), base_rate

    def regime(self, est: Estimate, best: tuple[str, float, float] | None) -> tuple[str, list[str]]:
        p = self.p
        if self.hotfix:
            return "Chaotic", ["marked as a hotfix or incident fix"]
        if not est.logic_files:
            return "Clear", ["only mechanical changes (moves, renames, formatting, imports, comments)"]
        reasons = []
        # Reviewer familiarity is a routing/learning issue, not evidence that the
        # code exhibits emergent, unpredictable behavior. Do not turn a solo-project
        # PR into "Complex" merely because its author is excluded as reviewer.
        fixes, total, base_rate = self.history_risk()
        if total >= 5 and fixes / total >= max(p.risky_fix_ratio, p.risky_fix_lift * base_rate):
            reasons.append(f"{fixes} of the last {total} commits on these files were fixes or reverts "
                           f"(repo-wide: {base_rate:.0%})")
        P_total = sum(c.interactions for c in est.clusters)
        state = sum(self.sig[f].state for f in est.logic_files if not self.sig[f].test)
        if state and P_total >= 1:
            reasons.append("async, concurrency or shared-state code across interacting files")
        if est.certificates == 0 and est.blast >= p.big_blast and any(not self.sig[f].test for f in est.logic_files):
            reasons.append(f"wide blast radius ({est.blast:.0f}) with no test changes")
        if reasons:
            return "Complex", reasons
        effort = best[2] if best else est.for_reviewer(0.0, p)
        if effort <= p.clear_max_ru and P_total == 0 and est.blast < p.big_blast / 4:
            return "Clear", ["small, isolated change"]
        return "Complicated", ["analyzable by someone who knows the code"]

    # ---------------------------------------------------------------- splitting

    def split_plan(self, fixed_reviewer: str | None = None) -> tuple[list[Part], float, float]:
        """Returns (parts, effort of the whole PR, effort of all parts incl. per-PR overhead)."""
        p = self.p
        whole = self.whole()
        if fixed_reviewer is not None:
            whole_effort = whole.for_reviewer(self.familiarity(fixed_reviewer, whole), p)
        else:
            revs = self.reviewers(whole, top=1)
            whole_effort = revs[0][2] if revs else whole.for_reviewer(0.0, p)

        parts: list[Part] = []
        if self.logic_paths and self.shallow_total:
            parts.append(Part("Lower-effort edits (verify dependencies and behavior)",
                              [], self.shallow_total, self.shallow_paths))
        for comp in components(self.logic_paths, self.edges(self.logic_paths)):
            parts.extend(self._split_cluster(comp))
        if len(parts) <= 1:
            return [], whole_effort, whole_effort

        total = 0.0
        for part in parts:
            est = self.estimate(part.logic_files, part.shallow_lines, part.shallow_files, part.extra_ru)
            if fixed_reviewer is not None:
                part.reviewer = fixed_reviewer
                part.familiarity = self.familiarity(fixed_reviewer, est)
                part.effort = est.for_reviewer(part.familiarity, p)
            else:
                revs = self.reviewers(est, top=1)
                if revs:
                    part.reviewer, part.familiarity, part.effort = revs[0]
                else:
                    part.effort = est.for_reviewer(0.0, p)
            total += part.effort
        total += p.split_overhead * (len(parts) - 1)
        return parts, whole_effort, total

    def _split_cluster(self, comp: list[str]) -> list[Part]:
        name = part_name(comp, self.sig)
        est = self.estimate(comp, 0, [])
        if est.size <= self.p.ref_size or len(comp) < 3:
            return [Part(name, comp, 0, [])]
        cset = set(comp)
        imports_in = {f: self.graph.imports.get(f, set()) & cset for f in comp}
        imported = {g for f in comp for g in imports_in[f]}
        foundation = [f for f in comp if not imports_in[f] and f in imported]
        found_set = set(foundation)
        for f in comp:
            if self.sig[f].test and (imports_in[f] & found_set or test_subject(f, found_set)):
                found_set.add(f)
        rest = [f for f in comp if f not in found_set]
        if not found_set or not rest:
            return [Part(name, comp, 0, [])]
        cross = self.interaction_ru([e for e in self.edges(comp)
                                     if (e[0] in found_set) != (e[1] in found_set)])
        first = Part(f"Foundation: {part_name(sorted(found_set), self.sig)}", sorted(found_set), 0, [])
        second = Part(f"Built on it: {part_name(rest, self.sig)}", rest, 0, [], extra_ru=0.5 * cross,
                      stacked_on=first.name)
        return [first, second]

    # ---------------------------------------------------------------- extras

    def cochange_gaps(self, top: int = 5, min_conf: float = 0.6) -> list[tuple[str, str, int, float]]:
        """Files that usually change with a touched file but were not touched."""
        changed = set(self.sig) | set(self.ignored) | {s.old_path for s in self.sig.values() if s.old_path}
        gaps = []
        for f in self.logic_paths:
            key = self.sig[f].old_path or f
            n_f = self.graph.change_count.get(key, 0)
            for g, n in self.graph.cochange.get(key, {}).items():
                if g in changed or g not in self.graph.files or n_f == 0:
                    continue
                conf = n / n_f
                if conf >= min_conf:
                    gaps.append((f, g, n, conf))
        gaps.sort(key=lambda x: (-x[3], -x[2]))
        return gaps[:top]
