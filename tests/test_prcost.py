"""End-to-end tests on a synthetic repository with known structure."""
from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest

from prcost import calibrate
from prcost.cli import _summary, main
from prcost.describe import reading_order, review_guide
from prcost.gitio import parse_diff
from prcost.lines import classify_diff
from prcost.model import Analysis

ALICE = ("Alice", "alice@example.com")
BOB = ("Bob", "bob@example.com")
CAROL = ("Carol", "carol@example.com")


def sh(repo, *args, env=None):
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, env=env)


def write(repo, path, text):
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as fh:
        fh.write(textwrap.dedent(text).lstrip("\n"))


def commit(repo, who, msg, day):
    env = dict(os.environ, GIT_AUTHOR_NAME=who[0], GIT_AUTHOR_EMAIL=who[1], GIT_COMMITTER_NAME=who[0],
               GIT_COMMITTER_EMAIL=who[1], GIT_AUTHOR_DATE=f"2026-0{1 + day // 28}-{1 + day % 28:02d}T12:00:00Z",
               GIT_COMMITTER_DATE=f"2026-0{1 + day // 28}-{1 + day % 28:02d}T12:00:00Z")
    sh(repo, "add", "-A")
    sh(repo, "commit", "-q", "-m", msg, env=env)


def build_repo() -> str:
    repo = tempfile.mkdtemp(prefix="prcost-test-")
    sh(repo, "init", "-q", "-b", "main")
    write(repo, "app/__init__.py", "")
    write(repo, "app/models.py", """
        class User:
            def __init__(self, name, age):
                self.name = name
                self.age = age
        """)
    write(repo, "app/service.py", """
        from .models import User

        DB = {}

        def get_user(name):
            return DB.get(name)

        def helper_one(x):
            total = 0
            for i in range(x):
                total += i * 2
            return total
        """)
    write(repo, "app/api.py", """
        from app.service import get_user

        def handle(req):
            user = get_user(req["name"])
            return {"user": user}
        """)
    write(repo, "tests/test_service.py", """
        from app.service import get_user

        def test_missing():
            assert get_user("nobody") is None
        """)
    write(repo, "config/settings.yaml", "timeout: 5\n")
    write(repo, "tsconfig.json", """
        {
          // comment with a url: https://example.com
          "$schema": "https://json.schemastore.org/tsconfig",
          "compilerOptions": { "baseUrl": ".", "paths": { "@/*": ["web/src/*"] }, },
        }
        """)
    write(repo, "web/src/client.ts", """
        export async function fetchUser(name: string) {
          const res = await fetch(`/api/users/${name}`);
          return res.json();
        }
        """)
    write(repo, "web/src/view.tsx", """
        import { fetchUser } from './client';

        export function render(name: string) {
          return fetchUser(name);
        }
        """)
    write(repo, "web/src/page.ts", """
        import { render } from '@/view';

        export const page = () => render('me');
        """)
    commit(repo, ALICE, "initial", 1)
    for i in range(4):  # alice owns app/, service.py and settings.yaml co-change
        with open(os.path.join(repo, "app/service.py"), "a") as fh:
            fh.write(f"\nCONST_{i} = {i}\n")
        write(repo, "config/settings.yaml", f"timeout: {6 + i}\n")
        commit(repo, ALICE, f"tune service {i}", 3 + i)
    for i in range(3):  # bob owns web/
        with open(os.path.join(repo, "web/src/client.ts"), "a") as fh:
            fh.write(f"\nexport const V{i} = {i};\n")
        commit(repo, BOB, f"web tweak {i}", 10 + i)

    sh(repo, "checkout", "-q", "-b", "feature")
    # 1. systematic rename get_user -> find_user (mechanical)
    for path in ("app/service.py", "app/api.py", "tests/test_service.py"):
        full = os.path.join(repo, path)
        with open(full) as fh:
            text = fh.read().replace("get_user", "find_user")
        with open(full, "w") as fh:
            fh.write(text)
    # 2. move helper_one to the end of the file (mechanical) and add new branchy logic (deep)
    full = os.path.join(repo, "app/service.py")
    with open(full) as fh:
        text = fh.read()
    block = "def helper_one(x):\n    total = 0\n    for i in range(x):\n        total += i * 2\n    return total\n"
    text = text.replace(block, "") + "\n" + block
    text += "\ndef timeout_for(user):\n    return 30 if user.age > 65 else 10\n"
    with open(full, "w") as fh:
        fh.write(text)
    with open(os.path.join(repo, "app/models.py"), "a") as fh:
        fh.write(textwrap.dedent("""
            def can_vote(self, country):
                if country == "AT" and self.age >= 16:
                    return True
                elif self.age >= 18 or country in ("BR",):
                    return True
                return False
            User.can_vote = can_vote
            """))
    # 3. TS logic change
    with open(os.path.join(repo, "web/src/client.ts")) as fh:
        client_text = fh.read()
    write(repo, "web/src/client.ts", client_text.replace(
        "return res.json();", "if (!res.ok) { throw new Error(`HTTP ${res.status}`); }\n  return res.json();"))
    # 4. a comment
    with open(os.path.join(repo, "web/src/view.tsx"), "a") as fh:
        fh.write("// render is called by page.ts\n")
    commit(repo, CAROL, "find_user, voting rules, client errors", 20)
    return repo


class PrcostTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = build_repo()
        cls.an = Analysis(cls.repo, "main", "feature")

    def test_diff_parser_handles_plus_plus_lines(self):
        text = "diff --git a/x.js b/x.js\n--- a/x.js\n+++ b/x.js\n@@ -1 +1,2 @@\n-i++;\n+++i;\n+--j;\n"
        fc = parse_diff(text)[0]
        self.assertEqual(fc.removed, ["i++;"])
        self.assertEqual(fc.added, ["++i;", "--j;"])

    def test_rename_and_move_are_shallow(self):
        kinds = {}
        for s in self.an.sig.values():
            for k, v in s.shallow_kinds.items():
                kinds[k] = kinds.get(k, 0) + v
        self.assertGreaterEqual(kinds.get("renamed", 0), 4, kinds)
        self.assertGreaterEqual(kinds.get("moved", 0), 4, kinds)
        self.assertGreater(self.an.sig["app/api.py"].logic, 0)  # changed import is semantic
        self.assertGreater(self.an.sig["app/models.py"].branches, 2)

    def test_one_off_identifier_swap_is_logic(self):
        changes = parse_diff("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x = foo(a)\n+x = bar(a)\n")
        fl = classify_diff(changes)["a.py"]
        self.assertEqual(fl.logic_lines, 2)

    def test_lack_of_reviewer_history_is_not_code_complexity(self):
        repo = tempfile.mkdtemp(prefix="prcost-solo-")
        sh(repo, "init", "-q", "-b", "main")
        write(repo, "a.py", "def calc(x):\n    return x + 1\n")
        commit(repo, ALICE, "initial", 1)
        sh(repo, "checkout", "-q", "-b", "feature")
        write(repo, "a.py", "def calc(x):\n    return x + 2\n")
        commit(repo, ALICE, "change", 2)
        an = Analysis(repo, "main", "feature")
        regime, reasons = an.regime(an.whole(), None)
        self.assertNotEqual(regime, "Complex", reasons)
        summary = _summary(an, None, None)
        self.assertIsNotNone(summary["routing_warning"])

    def test_changed_import_is_not_mechanical(self):
        changes = parse_diff("diff --git a/auth.py b/auth.py\n--- a/auth.py\n+++ b/auth.py\n"
                             "@@ -1 +1 @@\n-from auth import allow\n+from auth import deny\n")
        fl = classify_diff(changes)["auth.py"]
        self.assertEqual(fl.logic_lines, 2)
        self.assertEqual(fl.kinds.get("import", 0), 0)

    def test_directive_comment_is_not_mechanical(self):
        changes = parse_diff("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
                             "@@ -1 +1 @@\n-x = 5  # type: ignore\n+x = 5\n")
        fl = classify_diff(changes)["a.py"]
        self.assertEqual(fl.logic_lines, 2)

    def test_explicit_unknown_reviewer_is_unfamiliar(self):
        s = _summary(self.an, "unknown@example.com", None)
        self.assertEqual(s["effort_ru"]["reviewer"]["familiarity"], 0)
        self.assertFalse(s["effort_ru"]["reviewer"]["known_history"])
        self.assertAlmostEqual(s["split"]["whole_effort"], s["effort_ru"]["unfamiliar"])

    def test_pure_file_move_has_nonzero_cost(self):
        repo = tempfile.mkdtemp(prefix="prcost-rename-")
        sh(repo, "init", "-q", "-b", "main")
        write(repo, "a.py", "def greet():\n    return 'hello'\n")
        commit(repo, ALICE, "initial", 1)
        sh(repo, "checkout", "-q", "-b", "feature")
        sh(repo, "mv", "a.py", "b.py")
        commit(repo, BOB, "move file", 2)
        an = Analysis(repo, "main", "feature")
        self.assertEqual(an.sig["b.py"].status, "R")
        self.assertGreater(an.sig["b.py"].shallow, 0)
        self.assertGreater(an.whole().for_reviewer(0.0, an.p), 0)

    def test_forward_chaining_uses_past_only(self):
        # Inspect the actual training matrices to verify future rows never enter a fit.
        from unittest.mock import patch
        rows = [{"x": float(i)} for i in range(30)]
        y = [float(i if i < 15 else 30 - i) for i in range(30)]
        with patch.object(calibrate, "ridge", wraps=calibrate.ridge) as spy:
            val = calibrate.cv_spearman(rows, y, ["x"])
        self.assertTrue(-1 <= val <= 1)
        self.assertEqual(len(spy.call_args_list), 5)
        for c in spy.call_args_list:
            X = c.args[0]
            self.assertEqual(X, [[float(i)] for i in range(len(X))])
            self.assertGreaterEqual(len(X), 15)

    def test_import_graph(self):
        g = self.an.graph
        self.assertIn("app/models.py", g.imports["app/service.py"])
        self.assertIn("app/service.py", g.imports["app/api.py"])
        self.assertIn("app/service.py", g.imports["tests/test_service.py"])
        self.assertIn("web/src/client.ts", g.imports["web/src/view.tsx"])
        self.assertIn("web/src/view.tsx", g.imports["web/src/page.ts"])  # tsconfig paths alias

    def test_cochange_edge_and_gap(self):
        self.assertIn("config/settings.yaml", self.an.graph.cochange["app/service.py"])
        gaps = self.an.cochange_gaps()
        self.assertTrue(any(g == "config/settings.yaml" for _, g, _, _ in gaps), gaps)

    def test_reviewer_routing(self):
        est = self.an.whole()
        revs = dict((r, f) for r, f, _ in self.an.reviewers(est, top=5))
        self.assertNotIn(CAROL[1], revs)  # PR author excluded
        app = self.an.estimate(["app/models.py", "app/service.py"], 0, [])
        web = self.an.estimate(["web/src/client.ts"], 0, [])
        self.assertEqual(self.an.reviewers(app, 1)[0][0], ALICE[1])
        self.assertEqual(self.an.reviewers(web, 1)[0][0], BOB[1])

    def test_split_separates_mechanical_and_independent_clusters(self):
        parts, whole, split = self.an.split_plan()
        names = [p.name for p in parts]
        self.assertTrue(names[0].startswith("Lower-effort"), names)
        files = [set(p.logic_files) for p in parts[1:]]
        self.assertTrue(any("web/src/client.ts" in f for f in files))
        self.assertTrue(all(not ({"web/src/client.ts"} & f and {"app/models.py"} & f) for f in files))

    def test_blast_radius_counts_transitive_importers(self):
        _, dist = self.an.estimate(["web/src/client.ts"], 0, []).blast, \
            self.an.estimate(["web/src/client.ts"], 0, []).blast_files
        self.assertEqual(dist.get("web/src/view.tsx"), 1)
        self.assertEqual(dist.get("web/src/page.ts"), 2)

    def test_reading_order_puts_dependencies_first(self):
        order = reading_order(self.an, ["app/service.py", "app/models.py"])
        self.assertLess(order.index("app/models.py"), order.index("app/service.py"))

    def test_outputs_render(self):
        s = _summary(self.an, None, None)
        self.assertIn(s["regime"], ("Clear", "Complicated", "Complex", "Chaotic"))
        guide = review_guide(self.an, self.an.whole(), ALICE[1], s["regime"], s["regime_reasons"], None)
        self.assertIn("Read in this order", guide)
        self.assertIn("config/settings.yaml", guide)
        self.assertEqual(main(["score", "--repo", self.repo, "--base", "main", "--head", "feature"]), 0)
        self.assertEqual(main(["describe", "--repo", self.repo, "--base", "main", "--head", "feature",
                               "--reviewer", "bob"]), 0)

    def test_brief_score(self):
        from contextlib import redirect_stdout
        from io import StringIO
        output = StringIO()
        with redirect_stdout(output):
            rc = main(["score", "--brief", "--repo", self.repo, "--base", "main", "--head", "feature"])
        self.assertEqual(rc, 0)
        self.assertIn("Review first:", output.getvalue())
        self.assertIn("not an assignment", output.getvalue())

    def test_stats(self):
        self.assertAlmostEqual(calibrate.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        X = [[float(i), float(i % 3)] for i in range(40)]
        y = [2.0 * r[0] - 1.0 * r[1] + 0.5 for r in X]
        m = calibrate.ridge(X, y, ["a", "b"], lam=1e-6)
        self.assertAlmostEqual(m["coef"][0], 2.0, places=3)
        self.assertAlmostEqual(m["coef"][1], -1.0, places=3)

    def test_labels(self):
        pr = {"author": {"login": "carol"}, "createdAt": "2026-01-01T00:00:00Z",
              "reviews": {"nodes": [
                  {"author": {"login": "carol"}, "state": "COMMENTED", "submittedAt": "2026-01-01T01:00:00Z",
                   "comments": {"totalCount": 1}},
                  {"author": {"login": "alice"}, "state": "COMMENTED", "submittedAt": "2026-01-01T05:00:00Z",
                   "comments": {"totalCount": 3}},
                  {"author": {"login": "alice"}, "state": "APPROVED", "submittedAt": "2026-01-01T07:00:00Z",
                   "comments": {"totalCount": 0}}]},
              "timelineItems": {"nodes": [{"createdAt": "2026-01-01T02:00:00Z"}]}}
        lab = calibrate.labels(pr)
        self.assertAlmostEqual(lab["wait_hours"], 3.0)
        self.assertEqual(lab["touches"], 3 + 2 * 2)
        self.assertAlmostEqual(lab["span_hours"], 2.0)
        self.assertEqual(lab["reviewer"], "alice")


    def test_rest_fetch_normalizes_records(self):
        pr = {"number": 7, "title": "Fix x", "created_at": "2026-01-01T00:00:00Z", "merged_at": "2026-01-02T00:00:00Z",
              "draft": False, "base": {"sha": "b" * 40}, "head": {"sha": "h" * 40}, "merge_commit_sha": "m" * 40,
              "user": {"login": "carol"}}
        pages = {
            "repos/o/r/pulls?state=closed&sort=created&direction=desc&per_page=100&page=1": [pr, dict(pr, merged_at=None)],
            "repos/o/r/pulls?state=closed&sort=created&direction=desc&per_page=100&page=2": [],
            "repos/o/r/pulls/7/reviews?per_page=100&page=1": [
                {"id": 1, "user": {"login": "alice"}, "state": "COMMENTED", "submitted_at": "2026-01-01T05:00:00Z"},
                {"id": 2, "user": {"login": "alice"}, "state": "APPROVED", "submitted_at": "2026-01-01T07:00:00Z"}],
            "repos/o/r/pulls/7/comments?per_page=100&page=1": [{"pull_request_review_id": 1}] * 3,
            "repos/o/r/pulls/7/commits?per_page=100&page=1": [
                {"sha": "c" * 40, "commit": {"author": {"email": "Carol@x.com", "name": "Carol"}}, "author": {"login": "carol"}}],
            "repos/o/r/issues/7/events?per_page=100&page=1": [{"event": "ready_for_review", "created_at": "2026-01-01T02:00:00Z"}],
        }
        out = os.path.join(tempfile.mkdtemp(), "prs.jsonl")
        n = calibrate.fetch("o/r", 10, out, log=lambda m: None, api=lambda path: pages[path])
        self.assertEqual(n, 1)
        import json
        with open(out) as fh:
            rec = json.load(fh)
        lab = calibrate.labels(rec)
        self.assertAlmostEqual(lab["wait_hours"], 3.0)
        self.assertEqual(lab["touches"], 3 + 2 * 2)
        self.assertEqual(calibrate.login_emails([rec])["carol"]["carol@x.com"], 1)


if __name__ == "__main__":
    unittest.main()
