# agentize

Generate `AGENTS.md` (and optionally `CLAUDE.md` / `.cursorrules`) from a codebase's **actual config** — every command sourced from a real file, nothing invented.

Works offline. Zero dependencies. Python 3.10+.

## Why

AI coding agents read `AGENTS.md` to learn a project's commands and conventions — but most generated ones hallucinate. agentize extracts commands from `package.json` scripts, `pyproject.toml`, `Makefile` targets, and CI workflow `run:` steps, then annotates each with its source so you can audit every line before committing.

## Install

```bash
uv tool install .        # from this repo
agentize --help
```

Or run without installing:

```bash
python agentize.py ~/path/to/repo
```

## Usage

```bash
agentize [PATH]              # write AGENTS.md into the repo root
agentize [PATH] --stdout     # preview without writing
agentize [PATH] --claude     # also write CLAUDE.md
agentize [PATH] --cursor     # also write .cursorrules
agentize [PATH] --force      # overwrite existing AGENTS.md
agentize [PATH] --json       # dump extracted evidence as JSON
```

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
