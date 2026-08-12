#!/usr/bin/env python3
"""
agentize — generate AGENTS.md from a codebase's actual config.

Evidence-based, zero-dependency, works offline. Every command in the output
is pulled from a real file (package.json scripts, pyproject.toml, Makefile
targets, CI workflow run: steps) and annotated with its source. It never
guesses.

Usage:
    agentize [PATH]                 # write AGENTS.md into PATH's repo root
    agentize [PATH] --stdout        # print instead of writing
    agentize [PATH] --claude        # also write CLAUDE.md
    agentize [PATH] --cursor        # also write .cursorrules
    agentize [PATH] --force         # overwrite an existing AGENTS.md
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

VERSION = "0.6.0"

# --------------------------------------------------------------------------
# terminal styling — zero-dep ANSI; inert when piped or NO_COLOR
# --------------------------------------------------------------------------


def color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("AGENTIZE_COLOR") == "1":
        return True
    return sys.stdout.isatty()


def _enable_windows_vt() -> None:
    """Windows: enable ANSI processing on the console — the colorama.init()
    equivalent, zero deps. CPython only does this automatically on 3.12+;
    on 3.11 raw \\x1b codes leak into the terminal without it."""
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        for stream, std in ((sys.stdout, -11), (sys.stderr, -12)):
            handle = k32.GetStdHandle(std)
            mode = wintypes.DWORD()
            if handle not in (0, None) and handle != wintypes.HANDLE(-1).value \
                    and k32.GetConsoleMode(handle, ctypes.byref(mode)):
                # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
                k32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass  # piped/redirected streams: color_enabled() already falls back


_enable_windows_vt()


def _s(code: str, s: str) -> str:
    return f"\x1b[{code}m{s}\x1b[0m" if color_enabled() else s


def ok(s: str) -> str:
    return _s("32", "✓ " + s)


def warn(s: str) -> str:
    return _s("33", "⚠ " + s)


def err(s: str) -> str:
    return _s("31", "✗ " + s)


def green(s: str) -> str:
    return _s("32", s)


def cyan(s: str) -> str:
    return _s("36", s)


def dim(s: str) -> str:
    return _s("2", s)


def bold(s: str) -> str:
    return _s("1", s)


@contextlib.contextmanager
def spinner(msg: str):
    """Rotating stderr spinner; degrades to a plain line when not a TTY."""
    if not color_enabled():
        print(dim(msg), file=sys.stderr)
        yield
        return
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    stop = threading.Event()

    def _spin():
        i = 0
        cr = chr(13)
        while not stop.is_set():
            sys.stderr.write(cr + "[2m" + frames[i % len(frames)] + " " + msg + "[0m ")
            sys.stderr.flush()
            i += 1
            time.sleep(0.08)
        sys.stderr.write(cr + " " * (len(msg) + 4) + cr)
    t = threading.Thread(target=_spin, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(0.5)

# --------------------------------------------------------------------------
# repo walk
# --------------------------------------------------------------------------

PRUNE_DIRS = {
    ".git", "node_modules", ".next", ".nuxt", "dist", "build", "out",
    ".venv", "venv", "env", "__pycache__", ".cache", ".turbo", ".parcel-cache",
    "target", "coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".eggs", ".idea", ".vscode", ".yarn", ".pnpm-store", ".serverless",
    ".vercel", ".expo", ".terraform", ".gitlab", "site-packages", ".docusaurus",
    ".storybook-static", "cdk.out", "AppData", ".hermes", ".local",
}

PRUNE_SUFFIXES = {".egg-info", ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                  ".ico", ".woff", ".woff2", ".ttf", ".eot", ".map", ".lock"}

MAX_FILES_FOR_DOCS = 400
MAX_WALK_FILES = 30_000  # safety cap — pathological trees must not hang the tool


def walk_repo(root: Path) -> list[Path]:
    """All source-ish files under root, pruned. Deterministic order."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in PRUNE_DIRS)
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            if p.suffix.lower() in PRUNE_SUFFIXES:
                continue
            files.append(p)
            if len(files) >= MAX_WALK_FILES:
                return files
    return files


def quick_stack(root: Path) -> str:
    """Cheap stack line for the menu — root-level manifests only, NO walk.
    detect_stack needs nothing deeper than the root; a recursive walk here
    made the bare menu take ~11s in a home directory (192k files)."""
    names: set[str] = set()
    try:
        with os.scandir(root) as it:
            for e in it:
                if e.is_file():
                    names.add(e.name)
    except OSError:
        return "no config detected"
    langs = [lang for fn, lang in LANG_BY_FILE.items() if fn in names]
    pm = next((name for lock, name in LOCKFILE_TO_PM.items() if lock in names), None)
    if pm is None and "package.json" in names:
        pm = "npm"
    blob = ""
    for fn in ("package.json", "pyproject.toml", "Cargo.toml",
               "requirements.txt", "go.mod"):
        if fn in names:
            blob += "\n" + (read_small(root / fn) or "")
    frameworks: list[str] = []
    for rx, name in FRAMEWORK_MARKERS:
        if rx.search(blob) and name not in frameworks:
            frameworks.append(name)
    bits = [s for s in [", ".join(langs), ", ".join(frameworks), pm] if s]
    return " · ".join(bits) if bits else "no config detected"


def rel(root: Path, p: Path) -> str:
    """POSIX-style relative path for output."""
    return p.relative_to(root).as_posix()


def read_small(p: Path, limit: int = 200_000) -> str | None:
    try:
        if p.stat().st_size > limit:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


# --------------------------------------------------------------------------
# stack detection
# --------------------------------------------------------------------------

LOCKFILE_TO_PM = {
    "package-lock.json": "npm", "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn", "bun.lockb": "bun", "bun.lock": "bun",
    "uv.lock": "uv", "poetry.lock": "poetry", "Pipfile.lock": "pipenv",
    "Cargo.lock": "cargo", "go.sum": "go",
}

LANG_BY_FILE = {
    "package.json": "JavaScript/TypeScript", "tsconfig.json": "TypeScript",
    "pyproject.toml": "Python", "requirements.txt": "Python",
    "Cargo.toml": "Rust", "go.mod": "Go", "Gemfile": "Ruby",
    "pom.xml": "Java", "build.gradle": "Java", "build.gradle.kts": "Java",
    "composer.json": "PHP", "Dockerfile": "Docker", "Makefile": "Make",
}

FRAMEWORK_MARKERS = [
    (re.compile(r'"next"\s*:'), "Next.js"),
    (re.compile(r'"react"\s*:'), "React"),
    (re.compile(r'"vue"\s*:'), "Vue"),
    (re.compile(r'"svelte"\s*:'), "Svelte"),
    (re.compile(r'"express"\s*:'), "Express"),
    (re.compile(r'"fastify"\s*:'), "Fastify"),
    (re.compile(r'"@nestjs/core"\s*:'), "NestJS"),
    (re.compile(r'"astro"\s*:'), "Astro"),
    (re.compile(r'"django"\s*:'), "Django"),
    (re.compile(r'"flask"\s*:'), "Flask"),
    (re.compile(r'"fastapi"\s*:'), "FastAPI"),
]

TEST_MARKERS = [
    (re.compile(r'"vitest"'), "vitest"),
    (re.compile(r'"jest"'), "jest"),
    (re.compile(r'"@playwright/test"'), "playwright"),
    (re.compile(r'"cypress"'), "cypress"),
    (re.compile(r'"pytest"'), "pytest"),
    (re.compile(r'"unittest"'), "unittest"),
]

LINT_MARKERS = [
    (re.compile(r'"eslint"'), "ESLint"),
    (re.compile(r'"prettier"'), "Prettier"),
    (re.compile(r'"biomejs"|"@biomejs/biome"'), "Biome"),
    (re.compile(r'"ruff"'), "Ruff"),
    (re.compile(r'"black"'), "Black"),
    (re.compile(r'"mypy"'), "mypy"),
]

SCRIPT_ROLES = {
    "dev": "Dev server", "start": "Start", "build": "Build", "test": "Test",
    "lint": "Lint", "typecheck": "Typecheck", "type-check": "Typecheck",
    "tsc": "Typecheck", "format": "Format", "preview": "Preview",
    "deploy": "Deploy", "check": "Check", "prepare": "Prepare",
}


def detect_stack(root: Path, files: list[Path]) -> dict:
    """Returns {'languages': [...], 'pm': str|None, 'frameworks': [...],
    'test': str|None, 'linters': [...]} — all from file presence + contents."""
    names = {rel(root, f) for f in files}
    langs: list[str] = []
    for fn, lang in LANG_BY_FILE.items():
        if fn in names and lang not in langs:
            langs.append(lang)

    pm = None
    for lock, name in LOCKFILE_TO_PM.items():
        if lock in names:
            pm = name
            break
    if pm is None and "package.json" in names:
        pm = "npm"

    frameworks: list[str] = []
    tests: list[str] = []
    linters: list[str] = []
    blob = ""
    # Only root-level manifests inform stack detection — nested packages
    # (monorepo workspaces, test fixtures) are a roadmap item, not signal here.
    for f in files:
        if f.parent != root:
            continue
        if f.name in ("package.json", "pyproject.toml", "Cargo.toml",
                      "requirements.txt", "go.mod"):
            blob += "\n" + (read_small(f) or "")
    for rx, name in FRAMEWORK_MARKERS:
        if rx.search(blob) and name not in frameworks:
            frameworks.append(name)
    for rx, name in TEST_MARKERS:
        if rx.search(blob) and name not in tests:
            tests.append(name)
    for rx, name in LINT_MARKERS:
        if rx.search(blob) and name not in linters:
            linters.append(name)

    # TS strict mode?
    ts_strict = False
    if "tsconfig.json" in names:
        try:
            ts = json.loads(read_small(root / "tsconfig.json") or "{}")
            ts_strict = bool(ts.get("compilerOptions", {}).get("strict"))
        except json.JSONDecodeError:
            pass

    return {
        "languages": langs, "pm": pm, "frameworks": frameworks,
        "test": tests, "linters": linters, "ts_strict": ts_strict,
    }


# --------------------------------------------------------------------------
# command extraction (the evidence layer)
# --------------------------------------------------------------------------

def extract_package_json(root: Path) -> list[dict]:
    """Returns [{'cmd': str, 'role': str|None, 'source': str, 'desc': str|None}]"""
    p = root / "package.json"
    if not p.exists():
        return []
    data = json.loads(read_small(p) or "{}")
    scripts = data.get("scripts") or {}
    out = []
    for name, cmd in scripts.items():
        if not isinstance(cmd, str) or not cmd.strip():
            continue
        out.append({
            "cmd": cmd.strip(),
            "role": SCRIPT_ROLES.get(name, None),
            "source": f"package.json:scripts.{name}",
            "desc": f"`{name}` script",
        })
    return out


def extract_pyproject(root: Path) -> list[dict]:
    p = root / "pyproject.toml"
    if not p.exists():
        return []
    try:
        data = tomllib.loads(read_small(p) or "")
    except tomllib.TOMLDecodeError:
        return []
    out: list[dict] = []
    for name, cmd in (data.get("project", {}).get("scripts") or {}).items():
        out.append({
            "cmd": str(cmd), "role": SCRIPT_ROLES.get(name, None),
            "source": f"pyproject.toml:project.scripts.{name}",
            "desc": f"`{name}` script",
        })
    if "tool" in data:
        tool = data["tool"]
        for section, role in (("pytest", "Test"), ("ruff", "Lint"),
                              ("black", "Format")):
            if section in tool and role not in [c["role"] for c in out]:
                out.append({
                    "cmd": f"pytest" if section == "pytest" else section,
                    "role": role,
                    "source": f"pyproject.toml:[tool.{section}] present",
                    "desc": f"configured via [tool.{section}]",
                })
    return out


def extract_makefile(root: Path) -> list[dict]:
    p = root / "Makefile"
    if not p.exists():
        return []
    text = read_small(p) or ""
    out = []
    seen = set()
    for m in re.finditer(r"^([a-zA-Z0-9_.-]+)\s*:", text, re.MULTILINE):
        target = m.group(1)
        if target in seen or target in ("PHONY", "all", "help"):
            continue
        seen.add(target)
        out.append({
            "cmd": f"make {target}",
            "role": SCRIPT_ROLES.get(target, None),
            "source": "Makefile",
            "desc": f"`make {target}` target",
        })
    return out


def extract_ci(root: Path) -> list[dict]:
    """Commands CI actually runs, from workflow files. YAML-lite: pulls
    `run:` lines and step names; good enough for command discovery."""
    out = []
    for wf in sorted((root / ".github" / "workflows").glob("*.yml")) + \
             sorted((root / ".github" / "workflows").glob("*.yaml")):
        text = read_small(wf) or ""
        for m in re.finditer(r"^\s*-\s*run:\s*(.+?)\s*$", text, re.MULTILINE):
            cmd = m.group(1).strip()
            if not cmd or cmd.startswith("#"):
                continue
            out.append({
                "cmd": cmd,
                "role": None,
                "source": rel(root, wf),
                "desc": "CI step",
            })
    return out


def classify_role(cmd: str) -> str | None:
    """Guess role of an arbitrary CI command by keyword, for grouping."""
    low = cmd.lower()
    if "test" in low or "vitest" in low or "pytest" in low or "jest" in low:
        return "Test"
    if "lint" in low or "eslint" in low or "ruff" in low:
        return "Lint"
    if "typecheck" in low or "tsc" in low:
        return "Typecheck"
    if "build" in low:
        return "Build"
    if "format" in low:
        return "Format"
    return None


# --------------------------------------------------------------------------
# conventions & docs mining
# --------------------------------------------------------------------------

def extract_env_example(root: Path) -> list[str]:
    p = root / ".env.example"
    if not p.exists():
        return []
    keys = []
    for line in (read_small(p) or "").splitlines():
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m:
            keys.append(m.group(1))
    return keys


def readme_description(root: Path, files: list[Path]) -> str | None:
    for name in ("README.md", "readme.md"):
        p = root / name
        if not p.exists():
            continue
        text = read_small(p) or ""
        # drop the title line, find first paragraph
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        body = [l for l in lines[1:] if not l.startswith("#") and not l.startswith("```")]
        for l in body:
            if len(l) > 25:
                return l[:240]
        return None
    return None


def contributing_bullets(root: Path) -> list[tuple[str, str]]:
    """Pull concrete instructions from CONTRIBUTING.md — PR rules, pitfalls.
    Returns [(section, bullet)]."""
    p = root / "CONTRIBUTING.md"
    if not p.exists():
        return []
    text = read_small(p) or ""
    out: list[tuple[str, str]] = []
    section = "General"
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^#{2,3}\s+(.+)", line)
        if m:
            section = m.group(1).strip()
            continue
        if re.match(r"^[-*]\s+", line) and len(line) > 15:
            out.append((section, line.lstrip("-* ").strip()))
    return out


def structure_map(root: Path, files: list[Path]) -> list[tuple[str, str]]:
    """Top-level entries with one-line role heuristics."""
    DIR_ROLES = {
        "src": "source code", "app": "application/route code",
        "components": "UI components", "lib": "shared libraries/utilities",
        "utils": "utilities", "helpers": "helpers",
        "api": "API layer", "pages": "page routes",
        "public": "static assets", "assets": "static assets",
        "tests": "tests", "test": "tests", "__tests__": "tests",
        "spec": "tests", "docs": "documentation",
        "scripts": "dev/ops scripts", "config": "configuration",
        "migrations": "DB migrations", "db": "database layer",
        "models": "data models", "services": "service layer",
        "hooks": "React hooks", "store": "state management",
        "styles": "styles/CSS", "types": "type definitions",
        "constants": "constants", "data": "data files",
        "examples": "examples", "benchmarks": "benchmarks",
        "docker": "docker config", ".github": "CI + GitHub config",
        "infra": "infrastructure", "deploy": "deployment",
        "bin": "executables", "cmd": "CLI entry points",
    }
    FILE_ROLES = {
        "README.md": "readme", "AGENTS.md": "agent instructions",
        "CLAUDE.md": "agent instructions", ".cursorrules": "agent instructions",
        "package.json": "npm manifest", "pyproject.toml": "Python project config",
        "tsconfig.json": "TS config", "Makefile": "build targets",
        "docker-compose.yml": "local services", "docker-compose.yaml": "local services",
        "Dockerfile": "container build", ".env.example": "env template",
    }
    rows: list[tuple[str, str]] = []
    seen_dirs = set()
    for f in files:
        r = rel(root, f)
        parts = r.split("/")
        if len(parts) == 1:
            role = FILE_ROLES.get(f.name)
            if role:
                rows.append((f.name, role))
        elif len(parts) == 2 and parts[0] not in seen_dirs:
            seen_dirs.add(parts[0])
            role = DIR_ROLES.get(parts[0], "—")
            n = sum(1 for g in files if rel(root, g).startswith(parts[0] + "/"))
            rows.append((parts[0] + "/", f"{role} ({n} files)"))
    # drop agent-instruction files from the map — they're the output, not input
    rows = [(n, r) for n, r in rows if n not in ("AGENTS.md", "CLAUDE.md", ".cursorrules")]
    return rows[:22]


def detect_commit_conventions(root: Path) -> list[str]:
    out = []
    if (root / ".gitmessage").exists() or (root / ".commitlintrc.json").exists() \
       or (root / ".commitlintrc.js").exists():
        out.append("Conventional commits configured")
    for f in (root / ".github" / "workflows").glob("*.yml"):
        txt = read_small(f) or ""
        if "pull_request" in txt and "review" in txt.lower():
            out.append("PR review required in CI")
            break
    return out


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def render(ev: dict) -> str:
    """ev: everything the extractors found. Deterministic markdown."""
    stack = ev["stack"]
    name = ev["name"]

    L = []
    L.append(f"# {name}")
    L.append("")
    L.append("> Auto-generated by agentize. Every command below is sourced from ")
    L.append("> real config files — review, then keep this file updated as the repo evolves.")
    L.append("")

    # ---- stack line
    bits = [s for s in [", ".join(stack["languages"]), ", ".join(stack["frameworks"]),
                        stack["pm"], ", ".join(stack["test"])] if s]
    if bits:
        L.append(f"**Stack:** {' · '.join(bits)}")
        L.append("")

    # ---- description (AI polish wins; it's synthesized from the same evidence)
    desc = ev.get("ai_overview") or ev.get("description")
    if desc:
        L.append(desc.strip())
        L.append("")

    # ---- setup commands
    roles = ev["roles"]  # {role: [cmd,...]}
    setup = []
    if roles.get("Install"):
        setup.append(("Install dependencies", roles["Install"][0]))
    if roles.get("Dev server"):
        setup.append(("Start dev server", roles["Dev server"][0]))
    if roles.get("Build"):
        setup.append(("Build", roles["Build"][0]))
    if setup:
        L.append("## Setup commands")
        L.append("")
        for label, cmd in setup:
            L.append(f"- {label}: `{cmd}`")
        L.append("")

    # ---- testing
    t = roles.get("Test", [])
    if t:
        L.append("## Testing")
        L.append("")
        L.append(f"- Run all tests: `{t[0]}`")
        if stack["test"]:
            L.append(f"- Framework: {', '.join(stack['test'])}")
        if stack["linters"]:
            L.append(f"- CI also runs: {', '.join('`'+c+'`' for c in roles.get('Lint', [])[:2]) or 'lint'}")
        L.append("")

    # ---- code style
    style = []
    if stack["ts_strict"]:
        style.append("TypeScript strict mode enabled")
    for lint in stack["linters"]:
        style.append(f"Lint/format: {lint}")
    if style:
        L.append("## Code style")
        L.append("")
        for s in style:
            L.append(f"- {s}")
        L.append("")

    # ---- project structure
    if ev.get("structure"):
        L.append("## Project structure")
        L.append("")
        for n, r in ev["structure"]:
            L.append(f"- `{n}` — {r}")
        L.append("")

    # ---- env vars
    if ev.get("env"):
        L.append("## Environment variables")
        L.append("")
        L.append("- Required keys (see `.env.example`): "
                 + ", ".join(f"`{k}`" for k in ev["env"]))
        L.append("")

    # ---- pitfalls / contribution rules
    if ev.get("pitfalls"):
        L.append("## Gotchas")
        L.append("")
        for section, bullet in ev["pitfalls"][:6]:
            L.append(f"- ({section}) {bullet[:200]}")
        L.append("")

    if ev.get("conventions"):
        L.append("## Contribution rules")
        L.append("")
        for c in ev["conventions"]:
            L.append(f"- {c}")
        L.append("")

    # ---- recent activity (commit history context, optional)
    if ev.get("recent") is not None:
        since, commits = ev["recent"]
        L.append(f"## Recent activity (since {since})")
        L.append("")
        if commits:
            for d, a, s in commits:
                L.append(f"- {d} · {a} — {s[:90]}")
        else:
            L.append(f"- No commits since {since}.")
        L.append("")

    # ---- evidence table
    if ev.get("commands"):
        L.append("## Command reference (sources)")
        L.append("")
        L.append("| Command | Purpose | Source |")
        L.append("|---|---|---|")
        for c in ev["commands"][:25]:
            safe_cmd = c["cmd"].replace("|", "\\|")
            L.append(f"| `{safe_cmd}` | {c['desc']} | {c['source']} |")
        L.append("")

    L.append("---")
    L.append(f"_Generated by agentize {VERSION} — check the sources above before trusting any command._")
    return "\n".join(L)


# --------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------

def analyze(root: Path) -> dict:
    files = walk_repo(root)
    stack = detect_stack(root, files)

    cmds = []
    cmds += extract_package_json(root)
    cmds += extract_pyproject(root)
    cmds += extract_makefile(root)
    cmds += extract_ci(root)

    # dedupe by (cmd, source)
    seen, dedup = set(), []
    for c in cmds:
        key = (c["cmd"], c["source"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(c)
    cmds = dedup

    # install command from lockfile/pm
    if stack["pm"]:
        install_map = {
            "npm": "npm install", "pnpm": "pnpm install", "yarn": "yarn install",
            "bun": "bun install", "uv": "uv sync", "poetry": "poetry install",
            "pipenv": "pipenv install", "cargo": "cargo build",
        }
        inst = install_map.get(stack["pm"])
        if inst:
            cmds.insert(0, {"cmd": inst, "role": "Install",
                            "source": f"detected package manager: {stack['pm']}",
                            "desc": "install dependencies"})
    elif "Python" in stack["languages"]:
        if (root / "requirements.txt").exists():
            cmds.insert(0, {"cmd": "pip install -r requirements.txt", "role": "Install",
                            "source": "requirements.txt present", "desc": "install dependencies"})
        elif (root / "pyproject.toml").exists():
            cmds.insert(0, {"cmd": "pip install -e .", "role": "Install",
                            "source": "pyproject.toml present", "desc": "install dependencies (editable)"})

    # role buckets; CI commands get keyword-classified
    roles: dict[str, list[str]] = {}
    for c in cmds:
        role = c["role"] or classify_role(c["cmd"])
        if role and role not in ("Check", "Prepare"):
            roles.setdefault(role, [])
            if c["cmd"] not in roles[role]:
                roles[role].append(c["cmd"])

    # test single-file hint
    test_cmd = roles.get("Test", [""])[0] if roles.get("Test") else None

    ev = {
        "name": root.name,
        "stack": stack,
        "commands": cmds,
        "roles": roles,
        "description": readme_description(root, files),
        "env": extract_env_example(root),
        "structure": structure_map(root, files),
        "pitfalls": contributing_bullets(root),
        "conventions": detect_commit_conventions(root),
        "test_cmd": test_cmd,
    }
    return ev


# --------------------------------------------------------------------------
# GitHub mode: agentize --github
# --------------------------------------------------------------------------

CONFIG_PATH = Path.home() / ".agentize.json"
API = "https://api.github.com"
USER_AGENT = "agentize"


class GitHubError(Exception):
    """API failure with HTTP status — per-repo errors must not kill the run."""

    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def gh_token() -> str | None:
    """Reuse an authenticated gh CLI if present — zero setup for gh users."""
    try:
        r = subprocess.run(["gh", "auth", "token"], capture_output=True,
                           text=True, timeout=10)
        t = r.stdout.strip()
        return t if r.returncode == 0 and t else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------
# LLM polish mode — bring your own key, OpenAI-compatible endpoints
# --------------------------------------------------------------------------

PROVIDERS = {
    "anthropic": {"name": "Anthropic", "base_url": "https://api.anthropic.com/v1",
                  "model": "claude-sonnet-4-20250514", "key_env": "ANTHROPIC_API_KEY"},
    "openai": {"name": "OpenAI", "base_url": "https://api.openai.com/v1",
               "model": "gpt-4o-mini", "key_env": "OPENAI_API_KEY"},
    "openrouter": {"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
                   "model": "anthropic/claude-sonnet-4", "key_env": "OPENROUTER_API_KEY"},
    "gemini": {"name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "model": "gemini-2.0-flash", "key_env": "GEMINI_API_KEY"},
    "xai": {"name": "xAI (Grok)", "base_url": "https://api.x.ai/v1",
            "model": "grok-2", "key_env": "XAI_API_KEY"},
    "deepseek": {"name": "DeepSeek", "base_url": "https://api.deepseek.com/v1",
                 "model": "deepseek-chat", "key_env": "DEEPSEEK_API_KEY"},
    "groq": {"name": "Groq", "base_url": "https://api.groq.com/openai/v1",
             "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY"},
    "mistral": {"name": "Mistral", "base_url": "https://api.mistral.ai/v1",
                "model": "mistral-small-latest", "key_env": "MISTRAL_API_KEY"},
    "ollama": {"name": "Ollama (local)", "base_url": "http://localhost:11434/v1",
               "model": "llama3.2", "key_env": None},
    "custom": {"name": "Custom (OpenAI-compatible)", "base_url": None,
               "model": None, "key_env": None},
}


def choose_provider() -> str:
    """Numbered provider picker, Hermes-setup style. Returns a PROVIDERS key
    or 'none' (evidence-only, the default)."""
    cfg = load_config()
    if cfg.get("llm_provider"):
        return cfg["llm_provider"]
    print()
    print(bold(cyan("  Choose an AI provider (bring your own key):")))
    names = list(PROVIDERS)
    for i, key in enumerate(names, 1):
        p = PROVIDERS[key]
        note = "  (local, no key)" if p["key_env"] is None else ""
        print(f"  {green(str(i) + '.')}  {p['name']:<26}{note}")
    print(f"  {green('q.')}  Skip — evidence only (default)")
    for _ in range(3):
        try:
            ans = input("\n  Provider: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("agentize: no provider selected")
        if ans in ("q", "skip", "none"):
            return "none"
        try:
            i = int(ans)
            if 1 <= i <= len(names):
                return names[i - 1]
        except ValueError:
            pass
        for key in names:
            if ans == key or ans == PROVIDERS[key]["name"].lower():
                return key
        print("  ?")
    raise SystemExit("agentize: no provider selected")


def llm_setup(provider: str) -> dict:
    """Collect key/model/base-url for the provider (env > config > prompt),
    persist to ~/.agentize.json, return the config. Keys are BYOK and stay
    local — never sent anywhere but the provider's own API."""
    p = PROVIDERS[provider]
    cfg = load_config()
    key = cfg.get("llm_api_key") or (os.environ.get(p["key_env"]) if p["key_env"] else None)
    base = cfg.get("llm_base_url") or p["base_url"]
    model = cfg.get("llm_model") or p["model"]
    if provider == "custom":
        if not base:
            base = input("  Base URL (OpenAI-compatible, e.g. https://api.example.com/v1): ").strip()
        if not model:
            model = input("  Model name: ").strip()
        if not base or not model:
            raise SystemExit("agentize: custom provider needs a base URL and model")
    if p["key_env"] and not key:
        key = getpass.getpass(f"  {p['name']} API key (stored locally): ").strip()
        if not key:
            raise SystemExit("agentize: no API key — set {0} or paste one".format(p["key_env"]))
    cfg = {**cfg, "llm_provider": provider, "llm_base_url": base, "llm_model": model}
    if key:
        cfg["llm_api_key"] = key
    save_config(cfg)
    return cfg


def build_polish_prompt(ev: dict) -> str:
    """Evidence-only prompt: the model may polish prose, never invent facts."""
    evidence = {k: ev.get(k) for k in
                ("name", "stack", "description", "structure", "env",
                 "pitfalls", "conventions", "commands")}
    return (
        "You are agentize, a repo-documentation assistant. Below is JSON evidence "
        "extracted from a repository's REAL config files (commands, stack, "
        "structure, env vars, gotchas, README description).\n\n"
        "Write a short Overview for the repo's AGENTS.md: 2-4 sentences describing "
        "what this project appears to be and how a developer should approach it.\n"
        "Rules:\n"
        "- Use ONLY the evidence below. Never invent files, commands, features, or facts.\n"
        "- Plain prose, no headings, no bullet lists, under 70 words.\n\n"
        "Evidence JSON:\n" + json.dumps(evidence, indent=2, default=str)
    )


def call_llm(provider: str, prompt: str, cfg: dict) -> str:
    """One OpenAI-compatible chat completion via urllib — zero deps."""
    p = PROVIDERS[provider]
    base = cfg.get("llm_base_url") or p["base_url"]
    model = cfg.get("llm_model") or p["model"]
    key = cfg.get("llm_api_key") or (os.environ.get(p["key_env"]) if p["key_env"] else None)
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 300,
        "temperature": 0.3,
    }).encode()
    req = urllib.request.Request(base.rstrip("/") + "/chat/completions",
                                 data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", USER_AGENT)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LLM call failed ({e.code}): {e.read().decode()[:200]}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"LLM call failed: {e.reason}") from None
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError):
        raise RuntimeError(f"unexpected LLM response: {str(data)[:200]}") from None


def polish(ev: dict, provider: str | None, model: str | None,
           base_url: str | None) -> dict:
    """LLM polish: add ai_overview to ev. Offline when provider is None/'none'.
    Commands and structure stay evidence-based — only prose is generated."""
    if not provider or provider == "none":
        return ev
    cfg = llm_setup(provider)
    if model:
        cfg = {**cfg, "llm_model": model}
    if base_url:
        cfg = {**cfg, "llm_base_url": base_url}
    with spinner(f"Polishing with {PROVIDERS[provider]['name']} ({cfg['llm_model']})"):
        text = call_llm(provider, build_polish_prompt(ev), cfg)
    print(ok("  AI overview generated"))
    return {**ev, "ai_overview": text}


def api_call(token: str, path: str, method: str = "GET",
             body: dict | None = None) -> dict | list:
    """Minimal GitHub REST client. Errors raise GitHubError with status."""
    req = urllib.request.Request(f"{API}{path}", method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", USER_AGENT)
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data=data, timeout=30) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:200] if e.fp else str(e)
        raise GitHubError(e.code, f"GitHub API {e.code} on {path}: {detail}")


def auth_header(token: str) -> str:
    """git -c value: Basic-auth header, token base64'd — never in URLs/config."""
    cred = base64.b64encode(("x-access-token:" + token).encode()).decode()
    return "http.extraheader=" + "AUTHORIZATION: " + "Basic " + cred


def connect_github() -> str:
    """Return a working token: gh CLI > env > saved config > guided connect."""
    token = gh_token() or os.environ.get("GITHUB_TOKEN") \
        or load_config().get("github_token")
    if token:
        try:
            api_call(token, "/user")  # throws if invalid
            return token
        except GitHubError:
            token = None  # stale token — fall through to guided connect
    print(warn("GitHub not connected."))
    print()
    print("  1.  Run `gh auth login` (recommended)")
    print("  2.  Paste a personal access token")
    print("  3.  Skip")
    for _ in range(3):
        try:
            ans = input("\n  How do you want to connect? ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("agentize: no GitHub connection")
        if ans in ("1", "gh", "login"):
            if not shutil.which("gh"):
                print("  gh CLI not installed — use option 2 (token).")
                continue
            print(dim("  Running `gh auth login` — follow the prompts…"))
            rc = subprocess.run(["gh", "auth", "login"]).returncode
            token = gh_token() if rc == 0 else None
            if token:
                print(ok("Connected via gh CLI"))
                return token
            print(warn("  gh still not authenticated — paste a token instead:"))
        if ans in ("2", "token", "paste"):
            print("  Create one: https://github.com/settings/tokens  (scope: repo)")
            try:
                token = input("  Token: ").strip()
            except (EOFError, KeyboardInterrupt):
                raise SystemExit("agentize: no GitHub connection")
            if not token:
                continue
            save_config({**load_config(), "github_token": token})
            try:
                api_call(token, "/user")
                print(ok("Connected with token"))
                return token
            except GitHubError as e:
                print(err(f"  token rejected: {e}"))
                token = None
                continue
        if ans in ("3", "skip", "q", "quit"):
            raise SystemExit("agentize: GitHub not connected")
        print("  ?")
    raise SystemExit("agentize: no GitHub connection")


def list_repos(token: str) -> list[dict]:
    repos: list[dict] = []
    page = 1
    while True:
        batch = api_call(token, f"/user/repos?per_page=100&page={page}&sort=updated")
        if not isinstance(batch, list) or not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return repos


def check_agents_md(token: str, repos: list[dict]) -> dict[str, bool]:
    """full_name -> True if the repo already has AGENTS.md.
    Contents API: 200 = exists, 404 = absent; any other failure counts as
    absent (generate_in_clone re-checks locally and skips safely).
    Parallelized — sequential checks of 100+ repos were the slow path."""
    out: dict[str, bool] = {}

    def _one(r: dict) -> tuple[str, bool]:
        try:
            api_call(token, f"/repos/{r['full_name']}/contents/AGENTS.md")
            return r["full_name"], True
        except GitHubError:
            return r["full_name"], False

    with ThreadPoolExecutor(max_workers=8) as ex:
        for name, has in ex.map(_one, repos):
            out[name] = has
    return out


def parse_selection(text: str, n: int) -> list[int]:
    """'1 3,5' -> [1,3,5]; '2-4' -> [2,3,4]; 'all'/'*'/'' -> everything."""
    text = text.strip().lower()
    if text in ("all", "*", ""):
        return list(range(1, n + 1))
    out: set[int] = set()
    for part in re.split(r"[\s,]+", text):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return sorted(i for i in out if 1 <= i <= n)


def filter_repos(repos: list[dict], term: str) -> list[int]:
    """Substring match on repo name — returns matching indices."""
    term = term.strip().lower()
    return [i for i, r in enumerate(repos) if term in r["name"].lower()]


def pick_repos(repos: list[dict], names_arg: str | None,
               has_agents: dict[str, bool] | None = None) -> list[int]:
    """Interactive numbered multi-select; --repos skips the prompt.
    Accepts bare names ('lrs-platform') or owner-qualified ('owner/lrs-platform').
    Foreign owner/repo names are left for github_mode to fetch via API.
    `has_agents` marks repos that already carry AGENTS.md next to their name."""
    if names_arg:
        wanted = [s.strip() for s in names_arg.split(",") if s.strip()]
        idx = []
        for w in wanted:
            hits = [i for i, r in enumerate(repos)
                    if w == r["name"] or w == r["full_name"]]
            if not hits and "/" not in w:
                print(f"agentize: repo not found: {w}", file=sys.stderr)
            idx.extend(hits)
        return sorted(set(idx))
    print(f"\nYour GitHub repos ({len(repos)}):")
    for i, r in enumerate(repos, 1):
        flags = []
        if r["private"]:
            flags.append("private")
        if r.get("fork"):
            flags.append("fork")
        f = f"  ({', '.join(flags)})" if flags else ""
        mark = ""
        if has_agents and has_agents.get(r["full_name"]):
            mark = "  " + warn("has AGENTS.md")
        print(f"  {i:>3}.  {r['full_name']:<40}{f}  {r.get('language') or ''}{mark}")
    for _ in range(3):
        try:
            ans = input("\nPick repos — numbers (1 3, 2-5), 'all', or a search term: ")
        except EOFError:
            raise SystemExit("agentize: no input")
        try:
            idx = parse_selection(ans, len(repos))
            if idx:
                return idx
            print("  no match — try again")
        except ValueError:
            hit = filter_repos(repos, ans)
            if hit:
                return hit
            print(f"  no repos match '{ans.strip()}' — try again")
    raise SystemExit("agentize: no repos selected")


def ensure_fork(token: str, full_name: str, me: str) -> str:
    """Fork a repo we don't own; poll until GitHub finishes creating it."""
    _, repo = full_name.split("/")
    try:
        api_call(token, f"/repos/{me}/{repo}")
        return f"{me}/{repo}"
    except GitHubError as e:
        if e.status != 404:
            raise
    api_call(token, f"/repos/{full_name}/forks", method="POST")
    for _ in range(20):
        time.sleep(1.5)
        try:
            api_call(token, f"/repos/{me}/{repo}")
            return f"{me}/{repo}"
        except GitHubError:
            continue
    raise GitHubError(0, f"fork of {full_name} not ready after 30s")


def find_open_pr(token: str, full_name: str, head: str) -> dict | None:
    """Reuse an existing PR for this head instead of 422-ing."""
    q = urllib.parse.quote(head, safe="")
    try:
        pulls = api_call(token, f"/repos/{full_name}/pulls?state=open&head={q}&per_page=5")
        return pulls[0] if pulls else None
    except GitHubError:
        return None


def generate_in_clone(repo: dict, token: str, me: str, dry_run: bool,
                      since: str = "yesterday",
                      authors: list[str] | None = None) -> str:
    """Clone repo, generate AGENTS.md, optionally push a branch + open PR.
    Returns a status string for the summary. Never raises."""
    name = repo["full_name"]
    work = Path(tempfile.mkdtemp(prefix=f"agentize-{repo['name']}-"))
    try:
        cmd = ["git", "clone", "--depth", "1", "--quiet"]
        if repo.get("private"):
            cmd += ["-c", auth_header(token)]
        cmd += [repo["clone_url"], str(work)]
        clone = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if clone.returncode != 0:
            return f"{name}: clone failed ({clone.stderr.strip()[:80]})"
        md_path = work / "AGENTS.md"
        if md_path.exists():
            return f"{name}: already has AGENTS.md — skipped"
        ev = analyze(work)
        ev["recent"] = (since, git_recent(work, since, authors))
        md_path.write_text(render(ev) + "\n", encoding="utf-8")
        print("  " + ok(f"{name}: AGENTS.md generated ({md_path.stat().st_size} bytes)"))
        if dry_run:
            return f"{name}: generated (dry-run, nothing pushed)"
        return push_pr(repo, token, me, work)
    except GitHubError as e:
        return f"{name}: {e}"
    except Exception as e:  # noqa: BLE001
        return f"{name}: error — {e}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def push_pr(repo: dict, token: str, me: str, work: Path) -> str:
    """Branch -> commit -> push (fork if not ours) -> PR. Extraheader auth
    keeps the token out of remote URLs and git config."""
    name = repo["full_name"]
    branch = "agentize/agents-md"
    subprocess.run(["git", "-C", str(work), "checkout", "-q", "-b", branch], check=True)
    subprocess.run(["git", "-C", str(work), "add", "AGENTS.md"], check=True)
    subprocess.run(
        ["git", "-C", str(work), "-c",
         "user.name=agentize", "-c", "user.email=agentize@users.noreply.github.com",
         "commit", "-q", "-m", "Add AGENTS.md (generated by agentize)"],
        check=True)
    target = name
    if repo["owner"]["login"] != me:
        target = ensure_fork(token, name, me)
        print(dim(f"  {name}: using fork {target}"))
    head = me + ":" + branch
    # existing PR for this branch? reuse it — don't push or 422
    existing = find_open_pr(token, name, head)
    if existing:
        return f"{name}: PR already open #{existing['number']} → {existing['html_url']}"
    push = subprocess.run(
        ["git", "-C", str(work), "-c", auth_header(token),
         "push", "-q", f"https://github.com/{target}.git", branch],
        capture_output=True, text=True, timeout=120)
    if push.returncode != 0:
        return f"{name}: push failed ({push.stderr.strip()[:100]})"
    pr = api_call(token, f"/repos/{name}/pulls", method="POST", body={
        "title": f"Add AGENTS.md for {name}",
        "head": head,
        "base": repo["default_branch"],
        "body": ("Auto-generated by agentize — commands sourced from the "
                 "repo's actual config files. Review before merging."),
    })
    return f"{name}: PR #{pr['number']} → {pr['html_url']}"


def notify_discord(text: str) -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    channel = os.environ.get("DISCORD_HOME_CHANNEL")
    if not token or not channel:
        print("  (notify skipped: set DISCORD_BOT_TOKEN + DISCORD_HOME_CHANNEL)")
        return
    for i in range(0, len(text), 1900):
        chunk = text[i:i + 1900]
        payload = Path(tempfile.mkdtemp()) / "payload.json"
        payload.write_text(json.dumps({"content": chunk}), encoding="utf-8")
        subprocess.run(
            ["curl", "-s", "-X", "POST",
             f"https://discord.com/api/v10/channels/{channel}/messages",
             "-H", "Authorization: " + "Bot " + token,
             "-H", "Content-Type: application/json",
             "--data-binary", "@" + str(payload)],
            capture_output=True, text=True)
        payload.unlink(missing_ok=True)


def github_mode(args) -> int:
    t0 = time.monotonic()
    print(bold(cyan("agentize — GitHub mode")))
    token = connect_github()
    me = api_call(token, "/user")["login"]
    print(ok(f"Connected as {me}"))
    repos = list_repos(token)
    has = None
    if not args.repos:
        with spinner(f"Checking AGENTS.md status across {len(repos)} repos"):
            has = check_agents_md(token, repos)
    idx = pick_repos(repos, args.repos, has)
    targets = [repos[i] for i in idx]
    if args.repos:
        for w in (s.strip() for s in args.repos.split(",") if s.strip()):
            if "/" in w and w not in {r["name"] for r in targets} \
                    and w not in {r["full_name"] for r in targets}:
                try:
                    extra = api_call(token, f"/repos/{w}")
                    if extra.get("archived"):
                        print(f"agentize: skipping {w} — archived", file=sys.stderr)
                        continue
                    targets.append(extra)
                    print(f"agentize: added external repo {w}")
                except GitHubError as e:
                    print(f"agentize: skipping {w} — {e}", file=sys.stderr)
    if not targets:
        print("agentize: no repos selected", file=sys.stderr)
        return 1
    since = getattr(args, "since", None)
    authors = getattr(args, "authors", None)
    if sys.stdin.isatty() and since is None:
        since, authors = ask_history_defaults()
    since = since or "yesterday"
    print(f"\nProcessing {len(targets)} repo(s)...")
    results: list[str] = [""] * len(targets)
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(generate_in_clone, r, token, me, args.dry_run,
                          since, authors): i
                for i, r in enumerate(targets)}
        for fut in as_completed(futs):
            i = futs[fut]
            results[i] = fut.result()
            print(dim(f"  [{sum(1 for x in results if x)}/{len(targets)}] done"))
    print("\nSummary:")
    fails = 0
    for line in results:
        if "PR #" in line:
            print("  " + ok(line))
        elif "already has" in line or "skipped" in line:
            print("  " + warn(line))
        elif "dry-run" in line:
            print("  " + cyan(line))
        else:
            print("  " + err(line))
            fails += 1
    if args.notify == "discord":
        summary = "\n".join(f"• {r['full_name']}: {s}" for r, s in zip(targets, results))
        notify_discord(f"agentize done — {len(targets)} repo(s)\n{summary}")
    print(ok(f"Done — {len(targets)} repo(s) in {time.monotonic() - t0:.1f}s"))
    return 1 if fails else 0


HELP_TEXT = """agentize — generate AGENTS.md from a repo's actual config

Usage:
  agentize                     interactive menu
  agentize [PATH]              write AGENTS.md for a local folder (default: .)
  agentize [PATH] --stdout     preview without writing
  agentize [PATH] --claude     also write CLAUDE.md
  agentize [PATH] --cursor     also write .cursorrules
  agentize [PATH] --force      overwrite an existing AGENTS.md
  agentize [PATH] --since 3d   include commit history (git ref, default yesterday)
  agentize [PATH] --authors a,b
  agentize --json              dump extracted evidence as JSON
  agentize --llm               polish with an LLM (bring your own key)
  agentize --llm --provider openai --model gpt-4o-mini
  agentize --github            GitHub mode: connect, pick repos, open PRs
  agentize --github --repos a/b,c/d
  agentize --github --dry-run  generate only, push nothing
  agentize --github --notify discord
  agentize --version

Providers: anthropic, openai, openrouter, gemini, xai, deepseek, groq,
mistral, ollama (local), custom (OpenAI-compatible). Keys are yours —
stored in ~/.agentize.json, or set the provider's env var (ANTHROPIC_API_KEY,
OPENAI_API_KEY, ...). Without --llm, agentize is fully offline and never
calls a model.
"""

FIRST_RUN_FLAG = "first_run_done"


def git_recent(root: Path, since: str = "yesterday",
               authors: list[str] | None = None, cap: int = 15) -> list[tuple]:
    """Commit subjects since a git date ref, optionally filtered by authors.
    Returns [(date, author, subject)] — never raises."""
    try:
        cmd = ["git", "-C", str(root), "log", "--since=" + since,
               "--pretty=format:%ad|%an|%s", "--date=short", "-n", "200"]
        if authors:
            cmd += [f"--author={a}" for a in authors]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
        return [tuple(l.split("|", 2)) for l in out.splitlines() if "|" in l][:cap]
    except Exception:  # noqa: BLE001
        return []


def tool_status() -> list[tuple[str, str | None, str]]:
    """(name, found path or None, install hint) for python/uv/gh."""
    return [
        ("Python", shutil.which("python") or shutil.which("python3"),
         "winget install -e --id Python.Python.3.12"),
        ("uv", shutil.which("uv"),
         'powershell -c "irm https://astral.sh/uv/install.ps1 | iex"'),
        ("gh", shutil.which("gh"),
         "winget install -e --id GitHub.cli"),
    ]


def bootstrap(interactive: bool) -> None:
    """First-run setup: check Python/uv/gh, install missing ones with consent.
    Never runs anything without an explicit yes. Runs once (first_run flag)."""
    cfg = load_config()
    if cfg.get(FIRST_RUN_FLAG):
        return
    print("\n  First-run setup:")
    for name, path, hint in tool_status():
        if path:
            print(f"    ok {name}: {path}")
        elif interactive:
            try:
                ans = input(f"    {name} not found. Install it now? [y/N] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = "n"
            if ans == "y":
                print(f"    installing {name}...")
                subprocess.run(hint, shell=True, check=False)
            else:
                print(f"    skipped — later: {hint}")
        else:
            print(f"    missing {name} — install with: {hint}")
    if sys.version_info < (3, 11) and interactive:
        try:
            ans = input("    Python too old (need 3.11+). Install 3.12 via uv? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = "n"
        if ans == "y" and shutil.which("uv"):
            subprocess.run(["uv", "python", "install", "3.12"], check=False)
    save_config({**cfg, FIRST_RUN_FLAG: True})


def signin_github() -> str | None:
    """Interactive sign-in: gh device flow first, token paste as fallback."""
    if shutil.which("gh"):
        print("  Signing in with GitHub CLI (device flow) — a code will appear;")
        print("  enter it at https://github.com/login/device, then return here.")
        subprocess.run(["gh", "auth", "login"], check=False)
        token = gh_token()
        if token:
            save_config({**load_config(), "github_token": token})
            return token
    try:
        token = input("  Paste a GitHub token (scope: repo): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not token:
        return None
    try:
        api_call(token, "/user")
    except GitHubError:
        print("  invalid token")
        return None
    save_config({**load_config(), "github_token": token})
    return token


def ask_history_defaults() -> tuple[str, list[str] | None]:
    """Interactive: commit window (default yesterday) and authors (default all)."""
    try:
        since = input("  Commits since? [yesterday] ").strip() or "yesterday"
        raw = input("  Authors? [all, comma-separated] ").strip()
    except (EOFError, KeyboardInterrupt):
        return "yesterday", None
    authors = [a.strip() for a in raw.split(",") if a.strip()] or None
    return since, authors


def write_agents_md(root: Path, md: str) -> bool:
    """Overwrite-confirm + write AGENTS.md into root. True if written."""
    target = root / "AGENTS.md"
    if target.exists():
        try:
            ans = input(warn("  AGENTS.md already exists here. Overwrite? [y/N] ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("  cancelled")
            return False
        if ans != "y":
            print("  cancelled")
            return False
    target.write_text(md + "\n", encoding="utf-8")
    print(ok(f"  wrote {target}"))
    return True


def menu_local_generate() -> int:
    """Menu option 1: generate AGENTS.md for the current folder."""
    root = Path.cwd().resolve()
    print(dim(f"Scanning {root.name}…"), file=sys.stderr)
    t0 = time.monotonic()
    ev = analyze(root)
    if (root / ".git").exists():
        since, authors = ask_history_defaults()
        ev["recent"] = (since, git_recent(root, since, authors))
    md = render(ev)
    if write_agents_md(root, md):
        print(ok(f"Done — {time.monotonic() - t0:.1f}s"))
    return 0


def pick_local_repo(base: Path | None = None) -> Path:
    """Numbered pick of git repos in the tree (depth <= 3). Defaults to cwd."""
    base = (base or Path.cwd()).resolve()
    found = [base]
    for dirpath, dirnames, _ in os.walk(base):
        depth = dirpath[len(str(base)):].count(os.sep)
        if depth >= 3:
            dirnames[:] = []
            continue
        # .git must be detected BEFORE pruning (it's in PRUNE_DIRS)
        if ".git" in dirnames:
            found.append(Path(dirpath))
            dirnames.remove(".git")
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
    found.sort(key=lambda p: (p != base, str(p).lower()))
    if len(found) == 1:
        print(dim("  no other git repos in this tree — using current folder"))
        return base
    print()
    for i, p in enumerate(found, 1):
        label = " (current)" if p == base else ""
        shown = p.name if p == base else p.relative_to(base).as_posix()
        print(f"  {green(str(i) + '.')}  {shown}{dim(label)}")
    for _ in range(3):
        try:
            ans = input("\n  Select repo: ").strip()
        except (EOFError, KeyboardInterrupt):
            return base
        try:
            i = int(ans)
            if 1 <= i <= len(found):
                return found[i - 1]
        except ValueError:
            pass
        print("  ?")
    return base


def menu_selected_repo() -> int:
    """Menu option 5: pick a git repo in the tree, generate there."""
    root = pick_local_repo()
    if root != Path.cwd().resolve():
        print(dim(f"  Selected: {root}"))
    print(dim(f"Scanning {root.name}…"), file=sys.stderr)
    t0 = time.monotonic()
    md = render(analyze(root))
    if write_agents_md(root, md):
        print(ok(f"Done — {time.monotonic() - t0:.1f}s"))
    return 0


def menu_ai_polish() -> int:
    """Menu option 4: pick a provider, generate with LLM-polished prose."""
    provider = choose_provider()
    if provider == "none":
        print("  skipped — evidence only")
        return 0
    root = Path.cwd().resolve()
    print(dim(f"Scanning {root.name}…"), file=sys.stderr)
    t0 = time.monotonic()
    ev = polish(analyze(root), provider, None, None)
    md = render(ev)
    if write_agents_md(root, md):
        print(ok(f"Done — {time.monotonic() - t0:.1f}s"))
    return 0


def interactive_menu() -> int:
    """Bare `agentize`: a small TUI — local generate, GitHub mode, help."""
    bootstrap(interactive=True)
    gh = gh_token() or os.environ.get("GITHUB_TOKEN") or load_config().get("github_token")
    cwd = Path.cwd().resolve()
    ctx = quick_stack(cwd)  # computed ONCE — no per-keystroke repo walk
    print()
    print(bold(cyan("  ⚡ agentize — AGENTS.md generator for AI agents")))
    print(dim("  ───────────────────────────────────────────────────"))
    while True:
        print(dim(f"\n  Current folder: {cwd.name}  ({ctx})"))
        print(dim(f"  GitHub: {'connected' if gh else 'not connected'}"))
        print()
        print(f"  {green('1.')}  Generate AGENTS.md here (local)")
        connect_hint = "" if gh else dim("  (will ask to connect)")
        print(f"  {green('2.')}  GitHub — pick repos, open AGENTS.md PRs{connect_hint}")
        print(f"  {green('3.')}  Help — all commands")
        print(f"  {green('4.')}  AI polish (BYOK) — pick a provider, generate with LLM prose")
        print(f"  {green('5.')}  Select repo — pick a git repo in this tree")
        print(f"  {green('q.')}  Quit")
        try:
            choice = input("\n  Choice: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(ok("\n  bye"))
            return 0
        if choice == "1":
            return menu_local_generate()
        if choice == "2":
            return github_mode(argparse.Namespace(
                repos=None, dry_run=False, notify="none",
                since=None, authors=None))
        if choice == "3":
            print("\n" + HELP_TEXT)
            continue
        if choice == "4":
            return menu_ai_polish()
        if choice == "5":
            return menu_selected_repo()
        if choice in ("q", "quit", "exit"):
            print(ok("  bye"))
            return 0
        print("  ?")


def main() -> int:
    if not sys.argv[1:]:
        # bare invocation → interactive interface (or help when stdin is piped)
        if sys.stdin.isatty():
            return interactive_menu()
        print(HELP_TEXT)
        return 0
    ap = argparse.ArgumentParser(
        prog="agentize",
        description="Generate AGENTS.md from a repo's actual config — evidence-based, no guessing.",
    )
    ap.add_argument("path", nargs="?", default=".",
                    help="repo root (default: current dir)")
    ap.add_argument("--stdout", action="store_true", help="print instead of writing files")
    ap.add_argument("--claude", action="store_true", help="also write CLAUDE.md")
    ap.add_argument("--cursor", action="store_true", help="also write .cursorrules")
    ap.add_argument("--force", action="store_true", help="overwrite existing AGENTS.md")
    ap.add_argument("--json", action="store_true", help="dump the extracted evidence as JSON")
    ap.add_argument("--github", action="store_true",
                    help="GitHub mode: pick repos from your account, generate AGENTS.md, open PRs")
    ap.add_argument("--repos", metavar="NAMES",
                    help="comma-separated repo names (skips the interactive picker)")
    ap.add_argument("--dry-run", action="store_true",
                    help="generate locally but don't push or open PRs")
    ap.add_argument("--notify", choices=["none", "discord"], default="none",
                    help="notify when done (discord needs DISCORD_BOT_TOKEN + DISCORD_HOME_CHANNEL)")
    ap.add_argument("--llm", action="store_true",
                    help="polish the output with an LLM (bring your own key)")
    ap.add_argument("--provider", metavar="NAME",
                    help="LLM provider: anthropic, openai, openrouter, gemini, xai, "
                         "deepseek, groq, mistral, ollama, custom (skips the picker)")
    ap.add_argument("--model", metavar="NAME", help="LLM model override")
    ap.add_argument("--base-url", metavar="URL",
                    help="custom OpenAI-compatible base URL (with --provider custom)")
    ap.add_argument("--since", metavar="REF",
                    help="include commit history in AGENTS.md (git date ref, "
                         "e.g. yesterday, 3d, 2026-08-01)")
    ap.add_argument("--authors", metavar="NAMES",
                    help="commit authors to include with --since (comma-separated)")
    ap.add_argument("--version", action="version", version=f"agentize {VERSION}")
    args = ap.parse_args()

    if args.github:
        return github_mode(args)

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(err(f"agentize: not a directory: {root}"), file=sys.stderr)
        return 1

    print(dim(f"Scanning {root.name}…"), file=sys.stderr)
    t0 = time.monotonic()
    ev = analyze(root)

    if args.llm and not args.json:
        provider = args.provider or choose_provider()
        if provider != "none":
            ev = polish(ev, provider, args.model, args.base_url)

    if args.since:
        ev["recent"] = (args.since, git_recent(root, args.since, args.authors))

    md = render(ev)

    if args.json:
        # strip non-serializable bits
        print(json.dumps(ev, indent=2, default=str))
        return 0

    targets = []
    if args.stdout:
        print(md)
        return 0

    targets.append(("AGENTS.md", root / "AGENTS.md"))
    if args.claude:
        targets.append(("CLAUDE.md", root / "CLAUDE.md"))
    if args.cursor:
        targets.append((".cursorrules", root / ".cursorrules"))

    written = 0
    for label, path in targets:
        if path.exists() and not args.force:
            print(warn(f"agentize: {label} exists — run bare 'agentize' for the menu, "
                       f"--force to overwrite, or --stdout to preview."), file=sys.stderr)
            continue
        path.write_text(md + "\n", encoding="utf-8")
        print(ok(f"agentize: wrote {path}"))
        written += 1
    if written:
        print(ok(f"Done — {written} file(s) in {time.monotonic() - t0:.1f}s"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
