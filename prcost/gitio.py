"""Thin git layer: run commands, read blobs in bulk, parse diffs and logs."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field


class GitError(RuntimeError):
    pass


def run(repo: str, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", "-C", repo, "-c", "core.quotePath=false", *args],
        capture_output=True,
    )
    if check and proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise GitError(f"git {' '.join(args)} failed: {err}")
    return proc.stdout.decode("utf-8", "replace")


def rev_parse(repo: str, ref: str) -> str:
    return run(repo, "rev-parse", "--verify", ref + "^{commit}").strip()


def has_commit(repo: str, ref: str) -> bool:
    return bool(run(repo, "rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False).strip())


def merge_base(repo: str, a: str, b: str) -> str:
    return run(repo, "merge-base", a, b).strip()


def commit_time(repo: str, rev: str) -> int:
    return int(run(repo, "show", "-s", "--format=%ct", rev).strip())


def default_base(repo: str) -> str:
    for ref in ("origin/HEAD", "origin/main", "origin/master", "main", "master"):
        if has_commit(repo, ref):
            return ref
    raise GitError("could not find a base branch; pass --base")


def ls_tree(repo: str, rev: str) -> list[str]:
    out = run(repo, "ls-tree", "-r", "-z", "--name-only", rev)
    return [p for p in out.split("\0") if p]


def range_authors(repo: str, base: str, head: str) -> set[tuple[str, str]]:
    """(email, name) of everyone who authored a commit in base..head."""
    out = run(repo, "log", "--format=%aE%x09%aN", f"{base}..{head}")
    pairs = set()
    for line in out.splitlines():
        email, _, name = line.partition("\t")
        if email.strip():
            pairs.add((email.strip().lower(), name.strip()))
    return pairs


class BlobReader:
    """Reads many files at a revision through one `git cat-file --batch` process."""

    def __init__(self, repo: str):
        self.proc = subprocess.Popen(
            ["git", "-C", repo, "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

    def read(self, rev: str, path: str) -> str | None:
        if "\n" in path:
            return None
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(f"{rev}:{path}\n".encode("utf-8"))
        self.proc.stdin.flush()
        header = self.proc.stdout.readline().decode("utf-8", "replace").split()
        if len(header) < 3 or header[-1] in ("missing", "ambiguous"):
            return None
        kind, size = header[1], int(header[2])
        data = self.proc.stdout.read(size)
        self.proc.stdout.read(1)  # trailing newline
        if kind != "blob" or b"\0" in data[:8000]:
            return None
        return data.decode("utf-8", "replace")

    def close(self) -> None:
        if self.proc.stdin:
            self.proc.stdin.close()
        if self.proc.stdout:
            self.proc.stdout.close()
        self.proc.wait()


@dataclass
class FileChange:
    old_path: str | None
    new_path: str | None
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    binary: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def status(self) -> str:
        if self.old_path is None:
            return "A"
        if self.new_path is None:
            return "D"
        if self.old_path != self.new_path:
            return "R"
        return "M"


_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_HEADER_RE = re.compile(r"^diff --git a/(.*) b/(.*)$")


def diff(repo: str, base: str, head: str) -> list[FileChange]:
    text = run(repo, "diff", "--no-color", "--no-ext-diff", "-M", "--unified=0", base, head)
    return parse_diff(text)


def _strip_prefix(p: str, prefix: str) -> str | None:
    p = p.rstrip("\t")
    if p == "/dev/null":
        return None
    return p[len(prefix):] if p.startswith(prefix) else p


def parse_diff(text: str) -> list[FileChange]:
    files: list[FileChange] = []
    cur: FileChange | None = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            cur = FileChange(None, None)
            m = _HEADER_RE.match(line)
            if m:
                cur.old_path, cur.new_path = m.group(1), m.group(2)
            files.append(cur)
            i += 1
            continue
        if cur is None:
            i += 1
            continue
        if line.startswith("new file mode"):
            cur.old_path = None
        elif line.startswith("deleted file mode"):
            cur.new_path = None
        elif line.startswith("rename from "):
            cur.old_path = line[len("rename from "):]
        elif line.startswith("rename to "):
            cur.new_path = line[len("rename to "):]
        elif line.startswith("--- "):
            cur.old_path = _strip_prefix(line[4:], "a/")
        elif line.startswith("+++ "):
            cur.new_path = _strip_prefix(line[4:], "b/")
        elif line.startswith("Binary files "):
            cur.binary = True
        else:
            m = _HUNK_RE.match(line)
            if m:
                rem_old = int(m.group(2)) if m.group(2) is not None else 1
                rem_new = int(m.group(4)) if m.group(4) is not None else 1
                j = i + 1
                while j < len(lines) and (rem_old > 0 or rem_new > 0):
                    body = lines[j]
                    if body.startswith("-") and rem_old > 0:
                        cur.removed.append(body[1:])
                        rem_old -= 1
                    elif body.startswith("+") and rem_new > 0:
                        cur.added.append(body[1:])
                        rem_new -= 1
                    elif body.startswith("\\"):
                        pass
                    elif body.startswith(" "):
                        rem_old -= 1
                        rem_new -= 1
                    else:
                        break
                    j += 1
                while j < len(lines) and lines[j].startswith("\\"):
                    j += 1
                i = j
                continue
        i += 1
    return files


@dataclass
class Commit:
    sha: str
    ts: int
    email: str
    name: str
    subject: str
    files: list[str]


def log_commits(repo: str, rev: str, since_ts: int | None = None, max_count: int = 5000) -> list[Commit]:
    args = [
        "log", "--no-merges", f"--max-count={max_count}",
        "--format=%x00%H%x09%ct%x09%aE%x09%aN%x09%s", "--name-only", rev,
    ]
    if since_ts is not None:
        args.insert(3, f"--since={since_ts}")
    out = run(repo, *args)
    commits = []
    for chunk in out.split("\0"):
        if not chunk.strip():
            continue
        head, _, rest = chunk.partition("\n")
        parts = head.split("\t", 4)
        if len(parts) < 5:
            continue
        sha, ts, email, name, subject = parts
        files = [f for f in rest.split("\n") if f.strip()]
        commits.append(Commit(sha, int(ts), email.strip().lower(), name.strip(), subject, files))
    return commits
