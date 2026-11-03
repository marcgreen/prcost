"""Coupling graph: import edges (Python, TS/JS) plus co-change edges from git history."""
from __future__ import annotations

import ast
import json
import posixpath
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .lines import is_ignored, is_test, lang_of

JS_TRY_EXT = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
JS_SPEC_RE = re.compile(
    r"""(?:\bimport\s+(?:type\s+)?(?:[\w*${}\s,]+?\s+from\s+)?"""
    r"""|\bexport\s+(?:type\s+)?(?:\*(?:\s+as\s+[\w$]+)?|\{[^}]*\})\s+from\s+"""
    r"""|\brequire\s*\(\s*|\bimport\s*\(\s*)(['"])([^'"\n]+)\1"""
)
PY_FALLBACK_RE = re.compile(
    r"^\s*(?:from\s+(\.*[\w.]*)\s+import\s+\(?([\w*, ]+)|import\s+([\w., ]+))", re.M
)


@dataclass
class Graph:
    files: set[str]
    imports: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    importers: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    cochange: dict[str, dict[str, int]] = field(default_factory=lambda: defaultdict(dict))
    change_count: Counter = field(default_factory=Counter)

    def add_import(self, src: str, dst: str) -> None:
        if src != dst:
            self.imports[src].add(dst)
            self.importers[dst].add(src)


# ---------------------------------------------------------------- Python


def _py_roots(files: set[str]) -> set[str]:
    """Directories that act as sys.path roots: the repo root and parents of top-level packages."""
    roots = {""}
    for p in files:
        if p.endswith("/__init__.py"):
            parent = posixpath.dirname(posixpath.dirname(p))
            parent_init = (parent + "/__init__.py") if parent else "__init__.py"
            if parent_init not in files:
                roots.add(parent)
    return roots


def _py_lookup(base: str, files: set[str]) -> str | None:
    for cand in (base + ".py", base + ".pyi", base + "/__init__.py"):
        if cand in files:
            return cand
    return None


def _py_join(root: str, dotted: str) -> str:
    rel = dotted.replace(".", "/")
    return f"{root}/{rel}" if root else rel


def _py_absolute(dotted: str, roots: list[str], files: set[str]) -> str | None:
    parts = dotted.split(".")
    for n in range(len(parts), 0, -1):
        name = ".".join(parts[:n])
        for root in roots:
            hit = _py_lookup(_py_join(root, name), files)
            if hit:
                return hit
    return None


def _py_imports(path: str, text: str) -> list[tuple[int, str, list[str]]]:
    """(level, module, names) per import statement."""
    out = []
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        for m in PY_FALLBACK_RE.finditer(text):
            if m.group(3):
                for mod in m.group(3).split(","):
                    out.append((0, mod.strip().split(" ")[0], []))
            else:
                raw = m.group(1)
                level = len(raw) - len(raw.lstrip("."))
                names = [n.strip().split(" ")[0] for n in m.group(2).split(",") if n.strip()]
                out.append((level, raw.lstrip("."), names))
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.append((0, alias.name, []))
        elif isinstance(node, ast.ImportFrom):
            out.append((node.level or 0, node.module or "", [a.name for a in node.names]))
    return out


def _resolve_py(path: str, text: str, files: set[str], roots: set[str]) -> set[str]:
    deps = set()
    here = posixpath.dirname(path)
    search = sorted(roots, key=len) + [here]
    for level, module, names in _py_imports(path, text):
        if level == 0:
            if names:
                for n in names:
                    hit = None
                    for root in search:
                        hit = _py_lookup(_py_join(root, f"{module}.{n}"), files)
                        if hit:
                            break
                    if hit:
                        deps.add(hit)
                    else:
                        hit = _py_absolute(module, search, files)
                        if hit:
                            deps.add(hit)
            else:
                hit = _py_absolute(module, search, files)
                if hit:
                    deps.add(hit)
            # `import a.b.c` also executes a/__init__.py and a/b/__init__.py
            for d in list(deps):
                parent = posixpath.dirname(d if not d.endswith("/__init__.py") else posixpath.dirname(d))
                while parent and f"{parent}/__init__.py" in files:
                    deps.add(f"{parent}/__init__.py")
                    parent = posixpath.dirname(parent)
        else:
            pkg = here
            for _ in range(level - 1):
                pkg = posixpath.dirname(pkg)
            base = pkg
            if module:
                base = f"{pkg}/{module.replace('.', '/')}" if pkg else module.replace(".", "/")
            found_name = False
            for n in names:
                if n == "*":
                    continue
                hit = _py_lookup(f"{base}/{n}" if base else n, files)
                if hit:
                    deps.add(hit)
                    found_name = True
            if not found_name:
                hit = _py_lookup(base, files) if base else None
                if hit:
                    deps.add(hit)
    deps.discard(path)
    return deps


# ---------------------------------------------------------------- TS / JS


def _strip_json_comments(text: str) -> str:
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


@dataclass
class TsConfig:
    base_url: str | None = None
    paths: dict[str, list[str]] = field(default_factory=dict)
    packages: dict[str, str] = field(default_factory=dict)  # package.json name -> its directory


def load_packages(files: set[str], read) -> dict[str, str]:
    """Workspace and self-referencing package names (`import x from 'my-pkg'` inside my-pkg)."""
    out = {}
    for p in files:
        if posixpath.basename(p) != "package.json" or is_ignored(p) or p.count("/") > 3:
            continue
        try:
            name = json.loads(read(p) or "{}").get("name")
        except (ValueError, AttributeError):
            continue
        if isinstance(name, str) and name:
            out[name] = posixpath.dirname(p)
    return out


def load_tsconfig(read) -> TsConfig:
    for name in ("tsconfig.json", "jsconfig.json"):
        text = read(name)
        if not text:
            continue
        try:
            data = json.loads(_strip_json_comments(text))
        except ValueError:
            continue
        opts = data.get("compilerOptions") or {}
        base = opts.get("baseUrl")
        base = posixpath.normpath(base) if base else None
        if base == ".":
            base = ""
        paths = {k: v for k, v in (opts.get("paths") or {}).items() if isinstance(v, list)}
        return TsConfig(base, paths)
    return TsConfig()


def _js_candidates(spec: str, importer: str, cfg: TsConfig) -> list[str]:
    if spec.startswith("."):
        return [posixpath.normpath(posixpath.join(posixpath.dirname(importer), spec))]
    out = []
    for name, pdir in cfg.packages.items():
        if spec == name or spec.startswith(name + "/"):
            sub = spec[len(name) + 1:] or "index"
            for src in ("src", "source", "lib", ""):
                out.append(posixpath.normpath(posixpath.join(pdir or ".", src, sub)))
    base = cfg.base_url if cfg.base_url is not None else ""
    for pattern, targets in cfg.paths.items():
        if pattern.endswith("*") and spec.startswith(pattern[:-1]):
            rest = spec[len(pattern) - 1:]
        elif pattern == spec:
            rest = ""
        else:
            continue
        for t in targets:
            out.append(posixpath.normpath(posixpath.join(base, t.replace("*", rest))))
    if cfg.base_url is not None:
        out.append(posixpath.normpath(posixpath.join(base, spec)))
    return out


def _js_lookup(cand: str, files: set[str]) -> str | None:
    if cand in files:
        return cand
    stem = cand
    for js, ts in ((".js", ".ts"), (".jsx", ".tsx"), (".mjs", ".mts"), (".cjs", ".cts")):
        if cand.endswith(js):
            alt = cand[: -len(js)] + ts
            if alt in files:
                return alt
            stem = cand[: -len(js)]
    for ext in JS_TRY_EXT:
        if stem + ext in files:
            return stem + ext
    for ext in JS_TRY_EXT:
        if f"{cand}/index{ext}" in files:
            return f"{cand}/index{ext}"
    return None


def _resolve_js(path: str, text: str, files: set[str], cfg: TsConfig) -> set[str]:
    deps = set()
    for m in JS_SPEC_RE.finditer(text):
        spec = m.group(2)
        for cand in _js_candidates(spec, path, cfg):
            hit = _js_lookup(cand, files)
            if hit:
                deps.add(hit)
                break
    deps.discard(path)
    return deps


# ---------------------------------------------------------------- build


def build_graph(read_head, files: list[str], commits, min_count: int = 3,
                min_conf: float = 0.5, max_commit_files: int = 30) -> Graph:
    """read_head(path) -> text at head. commits: history used for co-change edges."""
    file_set = set(files)
    g = Graph(files=file_set)
    roots = _py_roots(file_set)
    cfg = load_tsconfig(read_head)
    cfg.packages = load_packages(file_set, read_head)
    for p in files:
        lang = lang_of(p)
        if lang not in ("py", "js") or is_ignored(p):
            continue
        text = read_head(p)
        if not text:
            continue
        deps = _resolve_py(p, text, file_set, roots) if lang == "py" else _resolve_js(p, text, file_set, cfg)
        for d in deps:
            g.add_import(p, d)

    pair_n: Counter = Counter()
    for c in commits:
        fs = sorted({f for f in c.files if not is_ignored(f)})
        g.change_count.update(fs)
        if 2 <= len(fs) <= max_commit_files:
            for i in range(len(fs)):
                for j in range(i + 1, len(fs)):
                    pair_n[(fs[i], fs[j])] += 1
    for (a, b), n in pair_n.items():
        if n >= min_count and n / min(g.change_count[a], g.change_count[b]) >= min_conf:
            g.cochange[a][b] = n
            g.cochange[b][a] = n
    return g


# ---------------------------------------------------------------- queries


def test_subject(test_path: str, candidates: set[str]) -> str | None:
    """Best-effort name match: tests/test_foo.py -> foo.py, foo.test.ts -> foo.ts."""
    base = posixpath.basename(test_path)
    stems = set()
    m = re.match(r"test_(.+)\.py$", base) or re.match(r"(.+)_test\.py$", base)
    if m:
        stems.add(m.group(1) + ".py")
    m = re.match(r"(.+)\.(?:test|spec)(\.[cm]?[jt]sx?)$", base)
    if m:
        for ext in JS_TRY_EXT:
            stems.add(m.group(1) + ext)
    for c in sorted(candidates):
        if c != test_path and not is_test(c) and posixpath.basename(c) in stems:
            return c
    return None


def blast_radius(g: Graph, touched: set[str], max_depth: int = 6, skip=None) -> tuple[float, dict[str, int]]:
    """Files that transitively import the touched files, weighted 0.5^(distance-1).
    `skip(path)` excludes files (tests) from the count; they are not production dependents."""
    dist: dict[str, int] = {}
    seen = set(touched)
    frontier = list(touched)
    depth = 0
    while frontier and depth < max_depth:
        depth += 1
        nxt = []
        for f in frontier:
            for imp in g.importers.get(f, ()):
                if imp not in seen:
                    seen.add(imp)
                    dist[imp] = depth
                    nxt.append(imp)
        frontier = nxt
    if skip:
        dist = {f: d for f, d in dist.items() if not skip(f)}
    return sum(0.5 ** (d - 1) for d in dist.values()), dist


def coupling_edges(g: Graph, paths: list[str]) -> list[tuple[str, str, str]]:
    """Undirected edges among the given files: import > test > co-change."""
    pset = set(paths)
    best: dict[tuple[str, str], str] = {}
    rank = {"import": 0, "test": 1, "co-change": 2}

    def put(a: str, b: str, kind: str) -> None:
        if a == b:
            return
        if is_test(a) or is_test(b):  # tests are not production interactions
            kind = "test"
        key = (a, b) if a < b else (b, a)
        if key not in best or rank[kind] < rank[best[key]]:
            best[key] = kind

    for a in paths:
        for b in g.imports.get(a, ()):
            if b in pset:
                put(a, b, "import")
        for b in g.cochange.get(a, {}):
            if b in pset:
                put(a, b, "co-change")
        if is_test(a):
            subj = test_subject(a, pset)
            if subj:
                put(a, subj, "test")
    return [(a, b, k) for (a, b), k in sorted(best.items())]


def components(paths: list[str], edges: list[tuple[str, str, str]]) -> list[list[str]]:
    parent = {p: p for p in paths}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b, _ in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb
    groups: dict[str, list[str]] = defaultdict(list)
    for p in paths:
        groups[find(p)].append(p)
    return [sorted(v) for v in groups.values()]
