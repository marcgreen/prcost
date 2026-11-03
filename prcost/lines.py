"""Sort changed lines into shallow (cheap to verify) and logic (must be reasoned about).

Lower-effort: blank, non-directive comment, pure structure (brackets, bare identifiers),
moved or re-indented lines, and selected systematic renames. Changed imports and
tooling directives are semantic. Logic: everything else.
"""
from __future__ import annotations

import posixpath
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

PY_EXT = {".py", ".pyi"}
JS_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}
DOC_EXT = {".md", ".rst", ".txt", ".adoc"}

IGNORE_RE = re.compile(
    r"(^|/)(node_modules|dist|build|vendor|\.next|coverage|__snapshots__)/"
    r"|(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Pipfile\.lock|uv\.lock|Cargo\.lock|go\.sum)$"
    r"|\.(min\.js|min\.css|map|snap|lock|svg|png|jpg|jpeg|gif|ico|woff2?|ttf|pdf)$"
)
TEST_RE = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs|test-d|e2e)/"
    r"|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$"
    r"|\.(test|spec)\.[cm]?[jt]sx?$"
)

BRANCH_RE = re.compile(
    r"\b(?:if|elif|else|for|while|case|catch|except|switch)\b|&&|\|\||\?\?|\band\b|\bor\b|\s\?\s"
)
# Shared mutable state and real concurrency primitives (async/await alone is too common to signal risk).
STATE_RE = re.compile(
    r"\b(?:threading|Thread|Lock|RLock|Mutex|mutex|Semaphore|Condition|Atomics?|atomic|global|nonlocal"
    r"|transaction|Worker|multiprocessing|SharedArrayBuffer|ThreadPoolExecutor|ProcessPoolExecutor)\b"
)
TOKEN_RE = re.compile(
    r"[A-Za-z_$][\w$]*|\d[\w.]*|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|\S"
)
IDENT_RE = re.compile(r"^[A-Za-z_$][\w$]*$")
KEYWORDS = frozenset(
    """and as assert async await break case catch class const continue def default del delete do
    elif else enum except export extends false False finally for from function global if import in
    instanceof interface is lambda let new None nonlocal not null or pass private protected public
    raise readonly return self static super switch this throw true True try type typeof undefined
    var void while with yield""".split()
)
PUNCT_ONLY_RE = re.compile(r"^[\s()\[\]{},;:]*$")
BARE_IDENT_RE = re.compile(r"^[A-Za-z_$][\w$]*(\s+as\s+[A-Za-z_$][\w$]*)?,?$")
PY_IMPORT_RE = re.compile(r"^(from\s+\S+\s+import\b|import\s+\S)")
JS_IMPORT_RE = re.compile(
    r"^(import\b|export\s+(type\s+)?(\*|\{[^}]*\})\s+from\b|(const|let|var)\s+[\w${}\s,]+=\s*require\()"
)


def lang_of(path: str) -> str:
    ext = posixpath.splitext(path)[1].lower()
    if ext in PY_EXT:
        return "py"
    if ext in JS_EXT:
        return "js"
    if ext in DOC_EXT:
        return "doc"
    return "other"


def is_ignored(path: str) -> bool:
    return bool(IGNORE_RE.search(path))


def is_test(path: str) -> bool:
    return bool(TEST_RE.search(path))


def tokens(line: str) -> tuple[str, ...]:
    out = []
    for t in TOKEN_RE.findall(line):
        if len(t) >= 2 and t[0] == "'" and t[-1] == "'":
            t = '"' + t[1:-1] + '"'
        out.append(t)
    while out and out[-1] in (",", ";"):
        out.pop()
    return tuple(out)


def shape(toks: tuple[str, ...]) -> tuple[str, ...]:
    return tuple("\x01" if IDENT_RE.match(t) and t not in KEYWORDS else t for t in toks)


def idents(toks: tuple[str, ...]) -> list[str]:
    return [t for t in toks if IDENT_RE.match(t) and t not in KEYWORDS]


def _is_comment(s: str, lang: str) -> bool:
    if lang == "py":
        return s.startswith("#")
    if lang == "js":
        return s.startswith(("//", "/*", "*", "*/"))
    return s.startswith(("#", "//", "<!--", "-- "))


def _is_import(s: str, lang: str) -> bool:
    if lang == "py":
        return bool(PY_IMPORT_RE.match(s))
    if lang == "js":
        return bool(JS_IMPORT_RE.match(s))
    return False


def trivial_kind(line: str, lang: str) -> str | None:
    s = line.strip()
    if not s:
        return "blank"
    if lang == "doc":
        return "doc"
    if _is_comment(s, lang):
        # Tooling directives can affect type checking, lint, test coverage, and bundling.
        if re.search(r"\b(type:\s*ignore|noqa|eslint-|istanbul|c8 ignore|webpack\w*|@ts-(?:ignore|expect-error|nocheck))\b", s, re.I):
            return None
        return "comment"
    # Changed imports may reroute code or cause side effects. They are not inherently mechanical.
    # An identical import moved elsewhere is still recognized by the move detector below.
    if PUNCT_ONLY_RE.match(s) or BARE_IDENT_RE.match(s):
        return "structure"
    return None


@dataclass
class FileLines:
    path: str
    lang: str
    shallow: int = 0
    kinds: Counter = field(default_factory=Counter)
    logic_added: list[str] = field(default_factory=list)
    logic_removed: list[str] = field(default_factory=list)
    branches: int = 0
    state: int = 0

    @property
    def logic_lines(self) -> int:
        return len(self.logic_added) + len(self.logic_removed)


def classify_diff(changes) -> dict[str, FileLines]:
    """Classify every changed line of a diff. Move and rename detection is diff-wide."""
    result: dict[str, FileLines] = {}
    pend_add: list[tuple[str, str, tuple]] = []
    pend_rem: list[tuple[str, str, tuple]] = []
    for fc in changes:
        if fc.binary or is_ignored(fc.path):
            continue
        lang = lang_of(fc.path)
        fl = FileLines(fc.path, lang)
        result[fc.path] = fl
        for side, bucket in ((fc.added, pend_add), (fc.removed, pend_rem)):
            for line in side:
                kind = trivial_kind(line, lang)
                if kind:
                    fl.shallow += 1
                    fl.kinds[kind] += 1
                else:
                    bucket.append((fc.path, line, tokens(line)))

    # Moves and re-indents: identical token sequences on both sides, anywhere in the diff.
    moved = Counter(t for _, _, t in pend_add) & Counter(t for _, _, t in pend_rem)
    quota_add, quota_rem = Counter(moved), Counter(moved)
    rest_add, rest_rem = [], []
    for item, quota, rest in ((pend_add, quota_add, rest_add), (pend_rem, quota_rem, rest_rem)):
        for path, line, t in item:
            if quota[t] > 0:
                quota[t] -= 1
                result[path].shallow += 1
                result[path].kinds["moved"] += 1
            else:
                rest.append((path, line, t))

    # Renames: same shape, identifiers differ, and every substitution recurs in the diff
    # (a systematic rename is mechanical; a one-off identifier swap is logic).
    rem_by_shape: dict[str, dict[tuple, list[int]]] = defaultdict(lambda: defaultdict(list))
    for idx, (path, _, t) in enumerate(rest_rem):
        rem_by_shape[path][shape(t)].append(idx)
    candidates = []
    subs_count: Counter = Counter()
    for a_idx, (path, _, t) in enumerate(rest_add):
        bucket = rem_by_shape[path].get(shape(t))
        if not bucket:
            continue
        r_idx = bucket.pop(0)
        old_ids, new_ids = idents(rest_rem[r_idx][2]), idents(t)
        subs = frozenset((o, n) for o, n in zip(old_ids, new_ids) if o != n)
        candidates.append((a_idx, r_idx, subs))
        subs_count.update(subs)
    renamed_add, renamed_rem = set(), set()
    for a_idx, r_idx, subs in candidates:
        # Even systematic replacements in imports may change the dependency being executed.
        if (subs and all(subs_count[s] >= 2 for s in subs)
                and not _is_import(rest_add[a_idx][1].strip(), lang_of(rest_add[a_idx][0]))
                and not _is_import(rest_rem[r_idx][1].strip(), lang_of(rest_rem[r_idx][0]))):
            renamed_add.add(a_idx)
            renamed_rem.add(r_idx)

    for rest, renamed, side in ((rest_add, renamed_add, "added"), (rest_rem, renamed_rem, "removed")):
        for idx, (path, line, _) in enumerate(rest):
            fl = result[path]
            if idx in renamed:
                fl.shallow += 1
                fl.kinds["renamed"] += 1
                continue
            (fl.logic_added if side == "added" else fl.logic_removed).append(line)
            fl.branches += len(BRANCH_RE.findall(line))
            fl.state += len(STATE_RE.findall(line))
    return result
