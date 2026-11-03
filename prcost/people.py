"""Reviewer familiarity: a stand-in for conditional complexity K(change | what the reviewer knows)."""
from __future__ import annotations

import math
import posixpath
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class Familiarity:
    file_w: dict[str, dict[str, float]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(float)))
    dir_w: dict[str, dict[str, float]] = field(default_factory=lambda: defaultdict(lambda: defaultdict(float)))
    names: dict[str, str] = field(default_factory=dict)
    total_w: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    canon: dict[str, str] = field(default_factory=dict)      # any email -> canonical email
    by_name: dict[str, str] = field(default_factory=dict)    # lowercased name -> canonical email

    @classmethod
    def from_commits(cls, commits, head_ts: int, half_life_days: float = 180.0) -> "Familiarity":
        fam = cls()
        for c in commits:  # newest first, so the most recent email becomes canonical
            age_days = max(0, head_ts - c.ts) / 86400
            w = 0.5 ** (age_days / half_life_days)
            who = fam.canonical(c.email, c.name, register=True)
            fam.names.setdefault(who, c.name)
            fam.total_w[who] += w
            for f in set(c.files):
                fam.file_w[f][who] += w
            for d in {posixpath.dirname(f) for f in c.files}:
                fam.dir_w[d][who] += w
        return fam

    def canonical(self, email: str, name: str = "", register: bool = False) -> str:
        """One identity per person: emails sharing a display name are merged (respects .mailmap)."""
        email = email.lower()
        if email in self.canon:
            return self.canon[email]
        key = name.strip().lower()
        who = self.by_name.get(key, email) if key else email
        if register:
            self.canon[email] = who
            if key:
                self.by_name.setdefault(key, who)
        return who

    def of(self, person: str, path: str, alias: str | None = None) -> float:
        best = 0.0
        for p in (path, alias):
            if not p:
                continue
            fw = self.file_w.get(p, {}).get(person, 0.0)
            dw = self.dir_w.get(posixpath.dirname(p), {}).get(person, 0.0)
            best = max(best, 1 - math.exp(-fw / 2), 0.5 * (1 - math.exp(-dw / 4)))
        return best

    def weighted(self, person: str, weights: dict[str, float], aliases: dict[str, str]) -> float:
        total = sum(weights.values())
        if total <= 0:
            return 1.0
        return sum(w * self.of(person, p, aliases.get(p)) for p, w in weights.items()) / total

    def candidates(self, paths, aliases: dict[str, str], exclude: set[str]) -> set[str]:
        out = set()
        for p in paths:
            for q in (p, aliases.get(p)):
                if not q:
                    continue
                out.update(self.file_w.get(q, {}).keys())
                out.update(self.dir_w.get(posixpath.dirname(q), {}).keys())
        return out - exclude

    def match(self, query: str) -> str | None:
        q = query.strip().lower()
        if q in self.canon:
            return self.canon[q]
        hits = [e for e, n in self.names.items() if q in n.lower() or q in e]
        if not hits:
            return None
        return max(hits, key=lambda e: self.total_w.get(e, 0.0))
