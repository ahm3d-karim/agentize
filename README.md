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

Run `agentize` with no arguments to open the interactive menu — it shows your
current folder's detected stack and offers local generation, GitHub mode, and
help:

```bash
agentize                       # interactive menu
```

Non-interactive usage:

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
agentize --version   # → agentize 0.2.1
```

## GitHub mode: AGENTS.md as pull requests

Connect your GitHub, pick repos (yours **or** anyone's), and agentize opens a
PR adding `AGENTS.md` to each — the cheat sheet, delivered. Repos you don't
own get an automatic fork; runs are idempotent (re-running finds the open PR
instead of duplicating it).

```bash
agentize --github                       # interactive: pick from your repos
agentize --github --repos ahm3d-karim/agentize,octocat/Hello-World
agentize --github --repos my-tool       # bare names match your account
agentize --github --dry-run             # generate only, push nothing
agentize --github --notify discord      # DM the summary to your Discord bot
```

Credentials, in order of preference: your existing `gh` CLI login (zero setup),
`GITHUB_TOKEN` env var, or a saved token (`agentize --github` prompts on first
use — scope `repo`). Nothing is stored unless you paste a token, and tokens
never appear in URLs or git config.

## Why

AI coding agents read `AGENTS.md` to learn a project's commands and conventions — but most generated ones hallucinate. agentize extracts commands from `package.json` scripts, `pyproject.toml`, `Makefile` targets, and CI workflow `run:` steps, then annotates each with its source so you can audit every line before committing.

## AI polish (bring your own key)

agentize itself never calls a model — every command is extracted from real
files. But you can optionally polish the prose with your own LLM key
(OpenAI-compatible endpoints, key stored locally in `~/.agentize.json`):

```bash
agentize --llm                        # interactive provider picker
agentize --llm --provider openai      # skip the picker
agentize --llm --provider openrouter --model anthropic/claude-sonnet-4
agentize --llm --provider custom --base-url http://localhost:11434/v1 --model llama3.2
```

Providers: anthropic, openai, openrouter, gemini, xai, deepseek, groq,
mistral, ollama (local, no key), custom. Keys come from the provider's env
var (e.g. `ANTHROPIC_API_KEY`) or a one-time prompt — they never leave your
machine except to the provider's own API.

The model writes only an Overview paragraph from the extracted evidence —
**commands and structure stay 100% evidence-based; the model is forbidden
from inventing anything.** The interactive menu has it as option 4.

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

- [x] GitHub mode: connect, pick repos, PR AGENTS.md each (v0.2)
- [ ] LLM polish mode: optional synthesis of prose sections from the evidence JSON (never touches commands)
- [ ] Nested `AGENTS.md` for monorepo workspaces
- [ ] `--check` mode for CI (fail if AGENTS.md is stale vs config)
- [ ] `--watch` regeneration on config change
