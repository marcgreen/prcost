"""First-class, explicitly unvalidated complexity dimensions for score/JSON.

The theory labels are hypotheses motivating operational proxies. The reported
counterfactuals are local changes to *hand-set* weights, not measured causal
contributions, and cannot be summed to reconstruct total effort.
"""
from __future__ import annotations

import math
from dataclasses import replace

from .model import Analysis, Estimate, Params, entropy


DEFINITIONS = (
    ("D", "Novelty/depth", "Algorithmic information / Bennett logical depth",
     "zlib-compressed changed logic conditioned on the old file, weighted by branch tokens"),
    ("F", "Reviewer familiarity", "Conditional description complexity",
     "time-decayed authorship of touched files and neighboring paths"),
    ("P", "Interactions", "Parameterized and combinatorial complexity",
     "import/co-change edges among changed files, charged by smaller endpoint depth"),
    ("H", "Change entropy", "Shannon entropy",
     "distribution of depth within connected clusters and cost across clusters"),
    ("B", "Blast radius", "Graph reachability",
     "distance-weighted transitively dependent non-test files"),
    ("T", "Test evidence", "Proof certificates / communication complexity",
     "changed source files with related changed tests, NOT verified test coverage"),
    ("R", "Review regime", "Cynefin sense-making framework",
     "advisory rule-based classification, not a complexity quantity"),
)


def prior_effort(est: Estimate, familiarity: float, p: Params) -> float:
    """Re-evaluate the existing prior formula from raw cluster signals.

    A parameter-off comparison does not delete evidence from the PR. It asks
    what the same *heuristic* model scores when one coefficient is set to zero.
    """
    costs = [(c.depth + p.beta * c.interaction_ru) * (1 + p.theta * c.entropy)
             for c in est.clusters]
    size = sum(costs) + p.shallow_ru * est.shallow_lines
    if size <= 0:
        return 0.0
    return (size * (size / p.ref_size) ** p.eps
            * (1 + p.theta * entropy(costs))
            * (1 + p.kappa * math.log2(1 + est.blast))
            * (1 - p.tau * est.certificates)
            * (1 + p.alpha * (1 - familiarity)))


def complexity_dimensions(an: Analysis, est: Estimate, familiarity: float,
                          regime: str, reasons: list[str]) -> list[dict]:
    """Return the 7 measured dimensions without pretending they are proven theory.

    ``prior_delta_ru`` can be negative (e.g. a test co-change discount). It is
    the full prior score minus the score with ONE coefficient set to zero,
    holding measured inputs fixed. Nonlinear interactions make deltas nonadditive.
    """
    p = an.p
    full = prior_effort(est, familiarity, p)
    deltas = {
        "F": full - prior_effort(est, familiarity, replace(p, alpha=0.0)),
        "P": full - prior_effort(est, familiarity, replace(p, beta=0.0)),
        "H": full - prior_effort(est, familiarity, replace(p, theta=0.0)),
        "B": full - prior_effort(est, familiarity, replace(p, kappa=0.0)),
        "T": full - prior_effort(est, familiarity, replace(p, tau=0.0)),
    }
    values = {
        "D": {"novelty_depth_ru": sum(c.depth for c in est.clusters),
              "novel_bytes": sum(s.novel_bytes for s in an.sig.values()),
              "branch_tokens": sum(s.branches for s in an.sig.values())},
        "F": {"familiarity": familiarity, "unfamiliarity": 1 - familiarity},
        "P": {"coupling_edges": sum(c.interactions for c in est.clusters),
              "edge_depth_ru": sum(c.interaction_ru for c in est.clusters)},
        "H": {"within_cluster_bits": [c.entropy for c in est.clusters],
              "across_cluster_bits": est.entropy_across},
        "B": {"weighted_reach": est.blast, "dependent_files": len(est.blast_files)},
        "T": {"changed_test_relationship_share": est.certificates,
              "unmatched_changed_sources": len(est.uncovered)},
        "R": {"regime": regime, "reasons": reasons},
    }
    notes = {
        "D": "Not Bennett logical depth; novelty/branching cannot isolate verification cost.",
        "F": "Authorship is not expertise, prior review experience, independence or availability.",
        "P": "Import and co-change edges miss runtime, data and operational coupling.",
        "H": "Entropy measures distribution of a heuristic weight, not semantic uncertainty.",
        "B": "Only dependencies visible to the graph; conditional and runtime calls may be missed.",
        "T": "Changed tests need not exercise changed behavior; no proof discount is warranted.",
        "R": "Workflow recommendation; no independent RU effect or verified risk ordering.",
    }
    out = []
    for key, name, theory, proxy in DEFINITIONS:
        out.append({
            "id": key, "name": name, "theory_inspiration": theory,
            "operational_proxy": proxy, "measured": values[key],
            "measurement_confidence": "unassessed", "empirical_validity": "unvalidated",
            "prior_delta_ru": deltas.get(key),
            "limitation": notes[key],
        })
    return out
