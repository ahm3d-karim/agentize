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
import json
import os
import re
import sys
import tomllib
from datetime import datetime
from pathlib import Path

VERSION = "0.1.0"

# --------------------------------------------------------------------------
# repo walk
# --------------------------------------------------------------------------

PRUNE_DIRS = {
    ".git", "node_modules", ".next", ".nuxt", "dist", "build", "out",
    ".venv", "venv", "env", "__pycache__", ".cache", ".turbo", ".parcel-cache",
    "target", "coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".eggs", ".idea", ".vscode", ".yarn", ".pnpm-store", ".serverless",
    ".vercel", ".expo", ".terraform", ".gitlab", "site-packages", ".docusaurus",
    ".storybook-static", "cdk.out",
}

PRUNE_SUFFIXES = {".egg-info", ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".svg",
                  ".ico", ".woff", ".woff2", ".ttf", ".eot", ".map", ".lock"}

MAX_FILES_FOR_DOCS = 400


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
    return files


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

    # ---- description
    if ev.get("description"):
        L.append(ev["description"].strip())
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


def main() -> int:
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
    ap.add_argument("--version", action="version", version=f"agentize {VERSION}")
    args = ap.parse_args()

    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"agentize: not a directory: {root}", file=sys.stderr)
        return 1

    ev = analyze(root)
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

    for label, path in targets:
        if path.exists() and not args.force:
            print(f"agentize: {label} exists — use --force to overwrite, or --stdout to preview.", file=sys.stderr)
            continue
        path.write_text(md + "\n", encoding="utf-8")
        print(f"agentize: wrote {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
