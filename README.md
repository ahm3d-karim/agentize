# agentize

Generate `AGENTS.md` (and optionally `CLAUDE.md` / `.cursorrules`) from a codebase's **actual config** — every command sourced from a real file, nothing invented.

Works offline. Zero dependencies. Requires Python 3.11+.

## Install

Pick whichever fits your setup — all of these are copy-paste ready.

**Option 1 — uv (recommended, one command):**

```bash
uv tool install --from git+https://github.com/ahm3d-karim/agentize.git agentize
agentize --help
```

**Option 2 — pip:**

```bash
pip install git+https://github.com/ahm3d-karim/agentize.git
agentize --help
```

**Option 3 — single file, no install at all:**

```bash
curl -fsSL -o agentize.py https://raw.githubusercontent.com/ahm3d-karim/agentize/main/agentize.py
python agentize.py --help
```

**Option 4 — from source:**

```bash
git clone https://github.com/ahm3d-karim/agentize.git
cd agentize
uv tool install .
agentize --help
```

> **Windows note:** if `agentize` isn't recognized after installing, run `uv tool update-shell`, then close and reopen your terminal. Still stuck? Add `C:\Users\<you>\.local\bin` to your PATH — that's where uv (and pip, in user mode) put tool binaries.

## Quick start

Generate a cheat sheet for a repo (current directory by default):

```bash
agentize .                 # write AGENTS.md in the current repo
agentize path/to/repo      # ...or any other repo
agentize . --stdout        # preview without writing anything
agentize . --claude        # also write CLAUDE.md
agentize . --cursor        # also write .cursorrules
agentize . --force         # overwrite an existing AGENTS.md
```

Then open the generated AGENTS.md, sanity-check the commands, commit it. Done.

Verify the install worked with:

```bash
agentize --version   # → agentize 0.1.0
```

## Why

AI coding agents read `AGENTS.md` to learn a project's commands and conventions — but most generated ones hallucinate. agentize extracts commands from `package.json` scripts, `pyproject.toml`, `Makefile` targets, and CI workflow `run:` steps, then annotates each with its source so you can audit every line before committing.

## What it extracts

| Signal | Source |
|---|---|
| Install / dev / build / test / lint commands | `package.json` scripts, `pyproject.toml` scripts, `Makefile` targets |
| What CI actually runs | `.github/workflows/*.yml` `run:` steps |
| Package manager | lockfiles (`pnpm-lock.yaml`, `uv.lock`, `Cargo.lock`, …) |
| Stack & frameworks | manifest contents (Next.js, React, Django, FastAPI, …) |
| Test / lint tooling | vitest, jest, pytest, eslint, ruff, biome, … |
| TypeScript strict mode | `tsconfig.json` |
| Required env vars | `.env.example` keys |
| Project structure map | top-level dirs with role heuristics |
| Gotchas & PR rules | `CONTRIBUTING.md` sections |

## Roadmap

- [ ] LLM polish mode: optional synthesis of prose sections from the evidence JSON (never touches commands)
- [ ] Nested `AGENTS.md` for monorepo workspaces
- [ ] `--check` mode for CI (fail if AGENTS.md is stale vs config)
- [ ] `--watch` regeneration on config change
