# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Packaging

- Renamed the PyPI distribution to `agentize-cli` (the bare `agentize` name is taken on PyPI); the console script stays `agentize`.
- Wheel now ships only `agentize.py` (+ dist-info) instead of the whole repository.
- Version bumped to 0.8.0; added classifiers, keywords, authors, and `[project.urls]`.

### Docs

- README: documented `--check`, added an example-output section, updated install commands for the renamed package, added CI/license badges.
- CHANGELOG backfilled from git history (0.1.0 → 0.7.1).

### CI

- Test matrix expanded to Python 3.11/3.12/3.13 × ubuntu-latest/windows-latest/macos-latest.
- New wheel build + install smoke job (fresh venv; `agentize --version`).

## [0.7.1] - 2026-08-13

### Fixed

- UTF-8 piped stdio: cp1252 pipes crashed on `✓`/`✗`/`—` glyphs; the Windows CI leg is now green.

## [0.7.0] - 2026-08-13

### Added

- `--check` mode: fail (exit 1) if AGENTS.md is stale vs config; never writes.
- Nested `AGENTS.md` generation for monorepo workspaces.
- Windows CI leg and e2e harness.
- Repo selector now asks about commit history + authors, like local generate.

### Fixed

- Repo picker listed the current repo twice when run from inside it.

## [0.6.0] - 2026-08-13

### Added

- First-run bootstrap: tool check + consent-based installs.
- Commit-history context: `--since`/`--authors` flags and a "Recent activity" section.
- Regenerated the repo's own AGENTS.md with the Recent activity section (dogfood).

## [0.5.1] - 2026-08-13

### Fixed

- Instant menu startup: `quick_stack` without a repo walk, walk cap + OS-dir pruning.

## [0.5.0] - 2026-08-13

### Added

- Windows ANSI support (VT enable, zero-dep), parallel GitHub checks, menu repo picker, done messages with timing.

## [0.4.0] - 2026-08-13

### Added

- BYOK LLM polish: Hermes-style provider picker, 10 providers, OpenAI-compatible client, evidence-only prompt.

## [0.3.0] - 2026-08-13

### Added

- Styled CLI: colors, spinner, guided GitHub connect, AGENTS.md-exists markers in the repo picker.

## [0.2.1] - 2026-08-12

### Added

- Bare `agentize` loads an interactive menu (local / GitHub / help); piped stdin shows help instead of hanging.

### Changed

- Test fixtures are generated at test time — zero JS/TS/Vercel-detectable files in the repo.

## [0.2.0] - 2026-08-12

### Added

- GitHub mode: connect, pick repos (any owner/repo), fork + PR AGENTS.md, Discord notify.
- MIT license, CI workflow, `requires-python >= 3.11` (tomllib).
- unittest suite (15 tests) + `.gitignore`.
- README install instructions: 4 verified copy-paste options + Windows PATH note.

## [0.1.0] - 2026-08-12

### Added

- Evidence-based AGENTS.md generator — zero deps, sources every command.
