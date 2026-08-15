"""Permanent test suite for agentize — zero-dependency (stdlib unittest).

Run from the repo root:  python -m unittest discover -s tests -v
Or via pytest, if you have it:  pytest tests/ -q
"""
import builtins
import contextlib
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import agentize  # noqa: E402
from tests.fixtures import materialize  # noqa: E402

AGENTIZE = REPO / "agentize.py"


def run_cli(*args):
    return subprocess.run([sys.executable, str(AGENTIZE), *args],
                          capture_output=True, text=True)


class TestCiExtraction(unittest.TestCase):
    """extract_ci style-agnosticism: bare run:, pipe blocks, templates."""

    def _wf(self, body):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = pathlib.Path(tmp.name)
        d = root / ".github" / "workflows"
        d.mkdir(parents=True)
        (d / "ci.yml").write_text(body, encoding="utf-8")
        return root

    def test_bare_run_and_dash_run_both_mined(self):
        root = self._wf("""
on: [push]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: npm test
      - name: Lint
        run: npm run lint
""")
        cmds = [c["cmd"] for c in agentize.extract_ci(root)]
        self.assertIn("npm test", cmds)
        self.assertIn("npm run lint", cmds)

    def test_pipe_block_joins_lines(self):
        root = self._wf("""
jobs:
  smoke:
    steps:
      - name: Install + smoke
        run: |
          python -m venv venv
          venv/bin/python -m pip install -q .
          venv/bin/agentize --version
""")
        cmds = [c["cmd"] for c in agentize.extract_ci(root)]
        self.assertEqual(len(cmds), 1)
        self.assertIn("python -m venv venv && venv/bin/python -m pip install -q .", cmds[0])

    def test_literal_pipe_marker_never_mined(self):
        root = self._wf("""
jobs:
  x:
    steps:
      - run: |
          echo hi
""")
        cmds = [c["cmd"] for c in agentize.extract_ci(root)]
        self.assertTrue(all(c != "|" for c in cmds))

    def test_template_placeholders_skipped(self):
        root = self._wf("""
jobs:
  x:
    strategy:
      matrix:
        python: ["3.11", "3.12"]
    steps:
      - run: python -m pip wheel . -w dist
      - run: python -m pip install ${{ matrix.python }}.0
""")
        cmds = [c["cmd"] for c in agentize.extract_ci(root)]
        self.assertIn("python -m pip wheel . -w dist", cmds)
        self.assertTrue(all("${{" not in c for c in cmds))


class TestExtractionWeb(unittest.TestCase):
    """The JS/TS fixture: every command must be real and sourced."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.web = materialize("fixture_web", pathlib.Path(cls.tmp.name))
        cls.ev = agentize.analyze(cls.web)
        cls.md = agentize.render(cls.ev)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_install_command_sourced(self):
        self.assertIn("npm install", self.md)
        self.assertIn("detected package manager: npm", self.md)

    def test_dev_command_sourced(self):
        self.assertIn("`next dev`", self.md)
        self.assertIn("package.json:scripts.dev", self.md)

    def test_ci_steps_mined(self):
        self.assertIn("npm run typecheck", self.md)
        self.assertIn(".github/workflows/ci.yml", self.md)

    def test_contributing_gotchas(self):
        self.assertIn("Asia/Karachi", self.md)
        self.assertIn("Never commit directly to `main`", self.md)

    def test_env_keys(self):
        self.assertIn("DATABASE_URL", self.md)
        self.assertIn("NEXT_PUBLIC_SITE_URL", self.md)

    def test_ts_strict(self):
        self.assertTrue(self.ev["stack"]["ts_strict"])
        self.assertIn("TypeScript strict mode enabled", self.md)

    def test_stack_detection(self):
        self.assertIn("Next.js", self.md)
        self.assertIn("vitest", self.md)


class TestNoFabrication(unittest.TestCase):
    """The Python fixture: agentize must never cite files that don't exist."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ml = materialize("fixture_ml", pathlib.Path(cls.tmp.name))
        cls.ev = agentize.analyze(cls.ml)
        cls.md = agentize.render(cls.ev)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_editable_install(self):
        self.assertIn("`pip install -e .`", self.md)

    def test_no_requirements_fabrication(self):
        # fixture_ml has no requirements.txt — no command may claim it does
        self.assertNotIn("requirements.txt present", self.md)
        self.assertNotIn("requirements.txt", self.md)

    def test_pytest_and_ruff_detected(self):
        self.assertIn("pytest", self.md)
        self.assertIn("ruff", self.md)


class TestSelfDogfood(unittest.TestCase):
    """agentize on itself: nested fixture repos must not leak into stack."""

    @classmethod
    def setUpClass(cls):
        cls.ev = agentize.analyze(REPO)
        cls.md = agentize.render(cls.ev)

    def test_stack_is_python_only(self):
        self.assertEqual(self.ev["stack"]["languages"], ["Python"])
        self.assertNotIn("React", self.md)
        self.assertNotIn("vitest", self.md)

    def test_own_entry_point_found(self):
        self.assertIn("agentize:main", self.md)
        self.assertIn("pyproject.toml:project.scripts.agentize", self.md)


class TestJsonMode(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.web = materialize("fixture_web", pathlib.Path(cls.tmp.name))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_evidence_is_json(self):
        r = run_cli(str(self.web), "--json")
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertIsInstance(data, dict)
        self.assertIn("commands", data)
        self.assertIn("roles", data)
        self.assertTrue(data["commands"])  # evidence is non-empty


class TestLifecycle(unittest.TestCase):
    """Write / refuse-overwrite / force / --claude / --cursor, on a temp copy.
    Each test gets its own fresh fixture — methods run alphabetically, so
    shared state (a leftover AGENTS.md) would make assertions order-dependent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.web = materialize("fixture_web", pathlib.Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def test_write_then_refuse_then_force(self):
        r1 = run_cli(str(self.web))
        self.assertEqual(r1.returncode, 0)
        self.assertIn("wrote", r1.stdout)
        self.assertIn("Done", r1.stdout)  # completion message with timing

        r2 = run_cli(str(self.web))
        self.assertIn("exists", r2.stderr)
        self.assertIn("--force", r2.stderr)

        r3 = run_cli(str(self.web), "--force")
        self.assertIn("wrote", r3.stdout)

    def test_claude_and_cursor(self):
        run_cli(str(self.web), "--claude", "--cursor", "--force")
        self.assertTrue((self.web / "CLAUDE.md").exists())
        # --cursor writes Cursor's current rule format (.mdc), not the
        # deprecated .cursorrules
        mdc = self.web / ".cursor" / "rules" / "agentize.mdc"
        self.assertTrue(mdc.exists())
        self.assertIn("description:", mdc.read_text(encoding="utf-8"))
        self.assertIn("alwaysApply: true", mdc.read_text(encoding="utf-8"))
        self.assertFalse((self.web / ".cursorrules").exists())

    def test_gemini_and_all(self):
        run_cli(str(self.web), "--gemini", "--force")
        self.assertTrue((self.web / "GEMINI.md").exists())
        run_cli(str(self.web), "--all", "--force")
        self.assertTrue((self.web / "CLAUDE.md").exists())
        self.assertTrue((self.web / "GEMINI.md").exists())
        self.assertTrue((self.web / ".cursor" / "rules" / "agentize.mdc").exists())


class TestCheckMode(unittest.TestCase):
    """--check: verify AGENTS.md matches the render; never writes, exit codes."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.web = materialize("fixture_web", pathlib.Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def test_fresh_generated_file_passes(self):
        r = run_cli(str(self.web))
        self.assertEqual(r.returncode, 0)
        r = run_cli(str(self.web), "--check")
        self.assertEqual(r.returncode, 0)
        self.assertIn("AGENTS.md is up to date", r.stdout)

    def test_out_of_date_fails(self):
        run_cli(str(self.web))
        # tamper INSIDE the managed block — the region agentize owns
        p = self.web / "AGENTS.md"
        text = p.read_text(encoding="utf-8")
        p.write_text(text.replace("**Stack:**", "**Stack:** (tampered)"), encoding="utf-8")
        r = run_cli(str(self.web), "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("AGENTS.md", r.stderr)
        self.assertIn("out of date", r.stderr)
        self.assertIn("lines differ", r.stderr)

    def test_legacy_file_without_markers_fails(self):
        # a hand-written AGENTS.md (no agentize markers) is not verifiable
        (self.web / "AGENTS.md").write_text(
            "# fixture-web\n\nhand-written, no markers\n", encoding="utf-8")
        r = run_cli(str(self.web), "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("markers", r.stderr)

    def test_human_edit_outside_block_passes(self):
        run_cli(str(self.web))
        p = self.web / "AGENTS.md"
        text = p.read_text(encoding="utf-8")
        p.write_text("## My team's notes\n\nkeep these!\n\n" + text, encoding="utf-8")
        r = run_cli(str(self.web), "--check")
        self.assertEqual(r.returncode, 0)  # human notes above the block = fine

    def test_update_regenerates_in_place(self):
        run_cli(str(self.web))
        p = self.web / "AGENTS.md"
        text = p.read_text(encoding="utf-8")
        p.write_text("## Human header\n\n" + text.replace("**Stack:**", "**Stack:** (stale)"),
                     encoding="utf-8")
        r = run_cli(str(self.web), "--check", "--update")
        self.assertEqual(r.returncode, 1)  # changed something
        new = p.read_text(encoding="utf-8")
        self.assertIn("## Human header", new)      # human content preserved
        self.assertNotIn("(stale)", new)           # block regenerated
        r2 = run_cli(str(self.web), "--check", "--update")
        self.assertEqual(r2.returncode, 0)         # second pass: clean

    def test_diff_shows_changes(self):
        run_cli(str(self.web))
        p = self.web / "AGENTS.md"
        text = p.read_text(encoding="utf-8")
        p.write_text(text.replace("**Stack:**", "**Stack:** (changed)"), encoding="utf-8")
        r = run_cli(str(self.web), "--diff")
        self.assertEqual(r.returncode, 1)
        self.assertIn("+", r.stdout)
        # after --update the diff is empty
        run_cli(str(self.web), "--check", "--update")
        r3 = run_cli(str(self.web), "--diff")
        self.assertEqual(r3.returncode, 0)

    def test_verify_reports_stale_claim(self):
        run_cli(str(self.web))
        p = self.web / "AGENTS.md"
        text = p.read_text(encoding="utf-8")
        # inject a claim that is NOT derivable from the repo
        p.write_text(text.replace(
            "## Command reference (sources)",
            "| `totally-made-up-cmd` | not real | nowhere |\n\n## Command reference (sources)"),
            encoding="utf-8")
        r = run_cli(str(self.web), "--verify")
        self.assertEqual(r.returncode, 1)
        self.assertIn("stale", r.stderr)

    def test_verify_passes_on_clean_file(self):
        run_cli(str(self.web))
        r = run_cli(str(self.web), "--verify")
        self.assertEqual(r.returncode, 0)
        self.assertIn("verified", r.stdout)

    def test_explain_finds_command(self):
        run_cli(str(self.web))
        r = run_cli(str(self.web), "--explain", "npm run build")
        self.assertEqual(r.returncode, 0)
        self.assertIn("source", r.stdout)

    def test_explain_unknown_command_fails(self):
        run_cli(str(self.web))
        r = run_cli(str(self.web), "--explain", "no-such-command-xyz")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no evidence", r.stderr)

    def test_missing_fails_without_writing(self):
        r = run_cli(str(self.web), "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("missing", r.stderr)
        # --check must never create the file
        self.assertFalse((self.web / "AGENTS.md").exists())

    def test_claude_and_cursor_checked_too(self):
        run_cli(str(self.web), "--claude", "--cursor", "--force")
        r = run_cli(str(self.web), "--check", "--claude", "--cursor")
        self.assertEqual(r.returncode, 0)
        self.assertIn("AGENTS.md is up to date", r.stdout)
        self.assertIn("CLAUDE.md is up to date", r.stdout)
        self.assertIn("agentize.mdc is up to date", r.stdout)
        (self.web / "CLAUDE.md").write_text("stale\n", encoding="utf-8")
        r = run_cli(str(self.web), "--check", "--claude", "--cursor")
        self.assertEqual(r.returncode, 1)
        self.assertIn("CLAUDE.md", r.stderr)
        self.assertIn("out of date", r.stderr)


class TestHookAndMcp(unittest.TestCase):
    """install-hook + MCP server hints."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.web = materialize("fixture_web", pathlib.Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def test_install_hook_writes_pre_commit(self):
        (self.web / ".git").mkdir(exist_ok=True)
        (self.web / ".git" / "hooks").mkdir(exist_ok=True)
        r = run_cli(str(self.web), "--install-hook")
        self.assertEqual(r.returncode, 0)
        hook = self.web / ".git" / "hooks" / "pre-commit"
        self.assertTrue(hook.exists())
        text = hook.read_text(encoding="utf-8")
        self.assertIn("agentize . --check", text)
        # idempotent
        r2 = run_cli(str(self.web), "--install-hook")
        self.assertEqual(r2.returncode, 0)
        self.assertIn("already installed", r2.stdout)
        # refuses to overwrite a foreign hook
        hook.write_text("#!/bin/sh\necho custom\n", encoding="utf-8")
        r3 = run_cli(str(self.web), "--install-hook")
        self.assertEqual(r3.returncode, 1)
        self.assertIn("refusing", r3.stderr)

    def test_install_hook_requires_git_repo(self):
        r = run_cli(str(self.web), "--install-hook")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a git repo", r.stderr)

    def test_mcp_servers_extracted(self):
        (self.web / ".mcp.json").write_text(json.dumps({
            "mcpServers": {
                "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
                "github": {"command": "gh", "args": ["mcp"]},
            }}), encoding="utf-8")
        ev = agentize.analyze(self.web)
        names = [s["name"] for s in ev.get("mcp", [])]
        self.assertIn("playwright", names)
        self.assertIn("github", names)
        self.assertIn("npx", ev["mcp"][0]["cmd"])
        md = agentize.render(ev)
        self.assertIn("## MCP servers", md)
        self.assertIn("playwright", md)


class TestMonorepoWorkspaces(unittest.TestCase):
    """pnpm workspaces: root listing + a nested AGENTS.md per package."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mono = materialize("fixture_monorepo", pathlib.Path(self.tmp.name))
        self.addCleanup(self.tmp.cleanup)

    def test_analyze_detects_workspaces(self):
        ws = agentize.analyze(self.mono)["workspaces"]
        self.assertEqual([w["path"].as_posix() for w in ws],
                         ["packages/a", "packages/b"])
        self.assertEqual([w["name"] for w in ws], ["pkg-a", "pkg-b"])
        # packages/c has no package.json — glob hit without a manifest
        self.assertNotIn("c", [w["name"] for w in ws])

    def test_render_lists_workspaces(self):
        md = agentize.render(agentize.analyze(self.mono))
        self.assertIn("## Workspaces", md)
        self.assertIn("- `packages/a/` — pkg-a", md)
        self.assertIn("- `packages/b/` — pkg-b", md)
        self.assertNotIn("packages/c", md)
        # the section sits right after Project structure
        self.assertLess(md.index("## Project structure"), md.index("## Workspaces"))

    def test_cli_writes_nested_agents_md(self):
        r = run_cli(str(self.mono))
        self.assertEqual(r.returncode, 0)
        self.assertTrue((self.mono / "AGENTS.md").exists())
        a_md = (self.mono / "packages" / "a" / "AGENTS.md").read_text(encoding="utf-8")
        b_md = (self.mono / "packages" / "b" / "AGENTS.md").read_text(encoding="utf-8")
        # each package's file carries its own scripts
        self.assertIn("next dev", a_md)
        self.assertIn("tsc -b", b_md)
        # c is not a package — no nested file for it
        self.assertFalse((self.mono / "packages" / "c" / "AGENTS.md").exists())

    def test_check_passes_after_generation(self):
        run_cli(str(self.mono))
        r = run_cli(str(self.mono), "--check")
        self.assertEqual(r.returncode, 0)
        self.assertIn("AGENTS.md is up to date", r.stdout)

    def test_check_fails_after_tampering(self):
        run_cli(str(self.mono))
        (self.mono / "packages" / "a" / "AGENTS.md").write_text(
            "# tampered\n\nno longer matches the render\n", encoding="utf-8")
        r = run_cli(str(self.mono), "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("out of date", r.stderr)


class TestGithubHelpers(unittest.TestCase):
    """Pure logic of --github mode: selection parsing, filters, config."""

    def test_parse_selection(self):
        self.assertEqual(agentize.parse_selection("1 3,5", 10), [1, 3, 5])
        self.assertEqual(agentize.parse_selection("2-4", 10), [2, 3, 4])
        self.assertEqual(agentize.parse_selection("all", 7), [1, 2, 3, 4, 5, 6, 7])
        self.assertEqual(agentize.parse_selection("*", 7), list(range(1, 8)))
        self.assertEqual(agentize.parse_selection("", 7), list(range(1, 8)))
        self.assertEqual(agentize.parse_selection("11", 10), [])  # out of range → empty

    def test_filter_repos(self):
        repos = [{"name": "agentize"}, {"name": "lrs-platform"},
                 {"name": "pid-dashboard"}]
        self.assertEqual(agentize.filter_repos(repos, "agent"), [0])
        self.assertEqual(agentize.filter_repos(repos, "platform"), [1])
        self.assertEqual(agentize.filter_repos(repos, "zzz"), [])
        self.assertEqual(agentize.filter_repos(repos, "AGENT"), [0])  # case-insensitive

    def test_config_roundtrip(self, tmp_path=None):
        import tempfile as _tf
        orig = agentize.CONFIG_PATH
        with _tf.TemporaryDirectory() as d:
            agentize.CONFIG_PATH = pathlib.Path(d) / "cfg.json"
            try:
                self.assertEqual(agentize.load_config(), {})
                agentize.save_config({"github_token": "abc123"})
                self.assertEqual(agentize.load_config()["github_token"], "abc123")
            finally:
                agentize.CONFIG_PATH = orig


class TestGithubAuth(unittest.TestCase):
    """Token hygiene and error semantics of --github mode."""

    def test_auth_header_encodes_token(self):
        h = agentize.auth_header("sekrit-token-123")
        self.assertIn("http.extraheader=", h)
        self.assertIn("Basic", h)
        # the literal token must never appear in the git config value
        self.assertNotIn("sekrit-token-123", h)

    def test_github_error_is_exception(self):
        # per-repo failures must be catchable, not SystemExit (batch survival)
        self.assertTrue(issubclass(agentize.GitHubError, Exception))
        self.assertFalse(issubclass(agentize.GitHubError, SystemExit))
        e = agentize.GitHubError(404, "not found")
        self.assertEqual(e.status, 404)
        self.assertEqual(str(e), "not found")


class TestMenu(unittest.TestCase):
    """Bare invocation: interactive when a TTY, help when stdin is piped."""

    def test_bare_invocation_piped_prints_help_without_hanging(self):
        r = subprocess.run([sys.executable, str(AGENTIZE)],
                           capture_output=True, text=True, input="", timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("Usage", r.stdout)
        self.assertIn("interactive menu", r.stdout)

    def test_bare_invocation_with_path_still_generates(self):
        # a path arg must keep working (menu only for zero args)
        with tempfile.TemporaryDirectory() as d:
            web = materialize("fixture_web", pathlib.Path(d))
            r = run_cli(str(web))
            self.assertEqual(r.returncode, 0)
            self.assertIn("wrote", r.stdout)


class TestStyle(unittest.TestCase):
    """Zero-dep styling must be inert when piped/NO_COLOR, active on demand."""

    def setUp(self):
        self._nc = os.environ.pop("NO_COLOR", None)
        self._ac = os.environ.pop("AGENTIZE_COLOR", None)

    def tearDown(self):
        if self._nc:
            os.environ["NO_COLOR"] = self._nc
        if self._ac:
            os.environ["AGENTIZE_COLOR"] = self._ac

    def test_color_off_when_piped(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(agentize.color_enabled())  # StringIO: not a tty
            # ✓ prefix is part of the message; only the ANSI codes must vanish
            self.assertEqual(agentize.ok("wrote x"), "✓ wrote x")
            self.assertNotIn("\x1b", agentize.ok("wrote x"))
            self.assertNotIn("\x1b", agentize.err("boom"))

    def test_force_color_env(self):
        os.environ["AGENTIZE_COLOR"] = "1"
        self.assertTrue(agentize.color_enabled())
        self.assertIn("\x1b[32m", agentize.ok("wrote x"))
        self.assertIn("\x1b[33m", agentize.warn("careful"))

    def test_no_color_env_wins(self):
        os.environ["NO_COLOR"] = "1"
        os.environ["AGENTIZE_COLOR"] = "1"
        self.assertFalse(agentize.color_enabled())


class TestAgentsMdMarkers(unittest.TestCase):
    """'already has AGENTS.md' detection for the repo picker."""

    def test_check_agents_md(self):
        real = agentize.api_call

        def fake(token, path, method="GET", body=None):
            if path.endswith("/contents/AGENTS.md"):
                if "hasit" in path:
                    return {"name": "AGENTS.md"}
                raise agentize.GitHubError(404, "not found")
            raise AssertionError(f"unexpected call: {path}")

        agentize.api_call = fake
        try:
            repos = [{"full_name": "me/hasit"}, {"full_name": "me/lacks"}]
            self.assertEqual(agentize.check_agents_md("t", repos),
                             {"me/hasit": True, "me/lacks": False})
        finally:
            agentize.api_call = real

    def test_pick_repos_marks_existing(self):
        # color off → the marker text appears plainly, next to the repo
        repos = [{"full_name": "me/hasit", "name": "hasit", "private": False,
                  "fork": False, "language": "Python"},
                 {"full_name": "me/lacks", "name": "lacks", "private": False,
                  "fork": False, "language": "Go"}]
        has = {"me/hasit": True}
        real_input = builtins.input
        builtins.input = lambda *a, **k: "q"  # three misses → SystemExit
        try:
            with contextlib.redirect_stdout(io.StringIO()) as buf:
                with self.assertRaises(SystemExit):
                    agentize.pick_repos(repos, None, has)
        finally:
            builtins.input = real_input
        out = buf.getvalue()
        self.assertIn("me/hasit", out)
        self.assertIn("has AGENTS.md", out)
        # the marker must sit on the hasit line, not the lacks line
        hasit_line = next(l for l in out.splitlines() if "me/hasit" in l)
        self.assertIn("has AGENTS.md", hasit_line)
        self.assertNotIn("has AGENTS.md",
                         next(l for l in out.splitlines() if "me/lacks" in l))


class TestLlmPolish(unittest.TestCase):
    """BYOK LLM mode: provider table, prompt rules, offline default."""

    def setUp(self):
        self._orig_cfg = agentize.CONFIG_PATH
        import tempfile as _tf
        self._cfg_dir = _tf.TemporaryDirectory()
        agentize.CONFIG_PATH = pathlib.Path(self._cfg_dir.name) / "cfg.json"

    def tearDown(self):
        agentize.CONFIG_PATH = self._orig_cfg
        self._cfg_dir.cleanup()

    def test_providers_table_complete(self):
        for key, p in agentize.PROVIDERS.items():
            self.assertIn("name", p)
            if key != "custom":
                self.assertTrue(p["base_url"], key)
                self.assertTrue(p["model"], key)
        self.assertIsNone(agentize.PROVIDERS["ollama"]["key_env"])
        self.assertIsNone(agentize.PROVIDERS["custom"]["base_url"])

    def test_polish_prompt_forbids_invention(self):
        prompt = agentize.build_polish_prompt({"name": "x", "commands": []})
        self.assertIn("Never invent", prompt)
        self.assertIn("UNTRUSTED DATA", prompt)   # injection hardening
        self.assertIn("<evidence>", prompt)        # delimited, not raw
        self.assertIn("</evidence>", prompt)
        self.assertIn("REAL config files", prompt)

    def test_sanitize_llm_output_strips_markdown(self):
        # injection-resistant: headings, fences, links never reach AGENTS.md
        self.assertEqual(
            agentize.sanitize_llm_output("# Big heading\nplain prose\n```sh\nrm -rf /\n```\n[click](http://evil)"),
            "plain prose")
        self.assertEqual(agentize.sanitize_llm_output(""), "")
        self.assertEqual(agentize.sanitize_llm_output("   "), "")
        self.assertLessEqual(len(agentize.sanitize_llm_output("w " * 800)), 701)

    def test_polish_unknown_provider_raises_clean(self):
        with self.assertRaises(SystemExit):
            agentize.polish({}, "bogus-provider", None, None)

    def test_render_prefers_ai_overview(self):
        ev = {"name": "x", "stack": {"languages": ["Python"], "frameworks": [],
              "pm": None, "test": [], "linters": [], "ts_strict": False},
              "roles": {}, "description": "readme says this",
              "ai_overview": "the model says this", "commands": []}
        md = agentize.render(ev)
        # AI overview is volatile prose — sits ABOVE the managed block
        self.assertLess(md.index("the model says this"),
                        md.index(agentize.AGENTIZE_START))
        # evidence description stays inside the deterministic block
        self.assertIn("readme says this", md)
        self.assertIn(agentize.AGENTIZE_START, md)
        self.assertIn(agentize.AGENTIZE_END, md)
        self.assertIn("fingerprint", md)

    def test_polish_offline_default(self):
        ev = {"name": "x"}
        self.assertIs(agentize.polish(ev, "none", None, None), ev)
        self.assertIs(agentize.polish(ev, None, None, None), ev)

    def test_choose_provider_by_number(self):
        real_input = builtins.input
        builtins.input = lambda *a, **k: "2"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(agentize.choose_provider(), "openai")
        finally:
            builtins.input = real_input

    def test_choose_provider_skip(self):
        real_input = builtins.input
        builtins.input = lambda *a, **k: "q"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(agentize.choose_provider(), "none")
        finally:
            builtins.input = real_input

    def test_llm_setup_uses_env_key_without_prompt(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-test-123"
        try:
            cfg = agentize.llm_setup("anthropic")
        finally:
            del os.environ["ANTHROPIC_API_KEY"]
        self.assertEqual(cfg["llm_api_key"], "sk-test-123")
        self.assertEqual(cfg["llm_model"], agentize.PROVIDERS["anthropic"]["model"])
        self.assertEqual(agentize.load_config()["llm_provider"], "anthropic")

    def test_call_llm_success(self):
        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"choices":[{"message":{"content":"  nice prose  "}}]}'

        with mock.patch("urllib.request.urlopen", return_value=FakeResp()):
            out = agentize.call_llm("openai", "hi", {"llm_api_key": "k"})
        self.assertEqual(out, "nice prose")

    def test_call_llm_http_error(self):
        err = urllib.error.HTTPError("https://x", 401, "unauthorized", {},
                                     io.BytesIO(b'{"error":"bad key"}'))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as cm:
                agentize.call_llm("openai", "hi", {"llm_api_key": "k"})
        self.assertIn("401", str(cm.exception))


class TestLocalRepoPicker(unittest.TestCase):
    """Menu option 5: pick a git repo in the tree."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = pathlib.Path(self.tmp.name)
        (base / "a" / ".git").mkdir(parents=True)
        (base / "b" / ".git").mkdir(parents=True)

    def test_picks_by_number(self):
        base = pathlib.Path(self.tmp.name)
        real_input = builtins.input
        builtins.input = lambda *a, **k: "2"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                got = agentize.pick_local_repo(base)
        finally:
            builtins.input = real_input
        self.assertEqual(got, base.resolve() / "a")  # order: base, a, b

    def test_eof_falls_back_to_base(self):
        base = pathlib.Path(self.tmp.name)
        real_input = builtins.input
        builtins.input = lambda *a, **k: (_ for _ in ()).throw(EOFError())
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                got = agentize.pick_local_repo(base)
        finally:
            builtins.input = real_input
        self.assertEqual(got, base.resolve())

    def test_single_repo_skips_prompt(self):
        base = pathlib.Path(self.tmp.name)
        shutil.rmtree(base / "b")
        real_input = builtins.input
        builtins.input = lambda *a, **k: "99"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                got = agentize.pick_local_repo(base)
        finally:
            builtins.input = real_input
        self.assertEqual(got, base.resolve())

    def test_windows_vt_enable_is_safe(self):
        # piped streams: must be a silent no-op, never raise
        agentize._enable_windows_vt()

    def test_piped_stdout_is_utf8_safe(self):
        # Windows pipes default to cp1252; ✓ (U+2713) must not crash a
        # piped child (this was a real Windows-CI regression).
        code = ("import sys; sys.path.insert(0, r'%s'); "
                "import agentize; print(agentize.ok('wrote x'))" % REPO)
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("\u2713 wrote x", r.stdout.decode("utf-8", "replace"))


class TestQuickStack(unittest.TestCase):
    """Menu startup: root-level manifests only, no recursive walk."""

    def test_detects_stack_without_walk(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "package.json").write_text(
                '{"scripts": {"dev": "next dev"}, "dependencies": {"next": "^15"}}')
            (root / "sub").mkdir()
            (root / "sub" / "deep.py").write_text("x")
            out = agentize.quick_stack(root)
            self.assertIn("npm", out)
            self.assertIn("Next.js", out)

    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(agentize.quick_stack(pathlib.Path(d)),
                             "no config detected")

    def test_walk_repo_capped(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            for i in range(50):
                (root / f"f{i}.txt").write_text("x")
            self.assertLessEqual(len(agentize.walk_repo(root)), agentize.MAX_WALK_FILES)


class TestHistoryAndBootstrap(unittest.TestCase):
    """Commit-history context (git_recent) + first-run bootstrap consent."""

    def _make_git_repo(self, commits):
        """commits: [(iso_date, author, subject)] → temp repo path."""
        d = pathlib.Path(tempfile.mkdtemp())
        subprocess.run(["git", "init", "-q", str(d)], check=True)
        subprocess.run(["git", "-C", str(d), "config", "user.name", "T"], check=True)
        subprocess.run(["git", "-C", str(d), "config", "user.email", "t@e.x"], check=True)
        for date, author, msg in commits:
            (d / "f.txt").write_text(msg, encoding="utf-8")
            env = {**os.environ,
                   "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date,
                   "GIT_AUTHOR_NAME": author, "GIT_COMMITTER_NAME": author}
            subprocess.run(["git", "-C", str(d), "add", "."], check=True)
            subprocess.run(["git", "-C", str(d), "commit", "-q", "-m", msg],
                           check=True, env=env)
        return d

    def test_git_recent_window_and_authors(self):
        from datetime import datetime, timedelta
        day = datetime.now().strftime("%Y-%m-%dT12:00:00")
        old = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%dT12:00:00")
        d = self._make_git_repo([
            (old, "Alice", "old work"),
            (day, "Alice", "fresh fix"),
            (day, "Bob", "another one"),
        ])
        recent = agentize.git_recent(d, "yesterday")
        self.assertEqual([c[2] for c in recent], ["another one", "fresh fix"])
        alice = agentize.git_recent(d, "yesterday", ["Alice"])
        self.assertEqual([c[2] for c in alice], ["fresh fix"])
        self.assertEqual(len(agentize.git_recent(d, "yesterday", ["Nobody"])), 0)
        import shutil
        shutil.rmtree(d, ignore_errors=True)

    def test_git_recent_not_a_repo(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(agentize.git_recent(pathlib.Path(d)), [])

    def test_render_recent_section(self):
        with tempfile.TemporaryDirectory() as d:
            ev = agentize.analyze(materialize("fixture_ml", pathlib.Path(d)))
            ev["recent"] = ("yesterday", [("2026-08-11", "Alice", "fix the thing")])
            md = agentize.render(ev)
        self.assertIn("## Recent activity (since yesterday)", md)
        self.assertIn("2026-08-11 · Alice — fix the thing", md)

    def test_bootstrap_consent_declined_runs_nothing(self):
        import shutil as _sh
        orig_which, orig_run = agentize.shutil.which, agentize.subprocess.run
        orig_cfg = agentize.CONFIG_PATH
        calls = []
        agentize.shutil.which = lambda _c: None  # pretend everything missing
        agentize.subprocess.run = lambda *a, **k: calls.append(a) or None
        agentize.CONFIG_PATH = pathlib.Path(tempfile.mkdtemp()) / "cfg.json"
        old_input = builtins.input
        try:
            builtins.input = lambda prompt="": "n"  # decline all installs
            agentize.bootstrap(interactive=True)
            self.assertEqual(calls, [])  # no install command may run
            self.assertTrue(agentize.load_config().get("first_run_done"))
            agentize.bootstrap(interactive=True)  # second call: no-op
            self.assertEqual(calls, [])
        finally:
            agentize.shutil.which, agentize.subprocess.run = orig_which, orig_run
            agentize.CONFIG_PATH = orig_cfg
            builtins.input = old_input

    def test_pick_local_repo_no_duplicate_entry(self):
        """Running from inside a repo must list it once (regression:
        base + .git-discovery used to duplicate the current repo)."""
        import builtins
        old_cwd, old_input = os.getcwd(), builtins.input
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
                repo = pathlib.Path(d) / "r"
                repo.mkdir()
                subprocess.run(["git", "init", "-q", str(repo)], check=True)
                os.chdir(repo)
                def _no_prompt(_prompt=""):
                    raise AssertionError("single-repo case must not prompt")
                builtins.input = _no_prompt
                picked = agentize.pick_local_repo()
                # compare canonical forms — Windows 8.3 short vs long path
                self.assertEqual(picked.resolve(), repo.resolve())
        finally:
            os.chdir(old_cwd)
            builtins.input = old_input

    def test_ask_history_defaults(self):
        old_input = builtins.input
        try:
            builtins.input = lambda prompt="": ""
            self.assertEqual(agentize.ask_history_defaults(), ("yesterday", None))
            answers = iter(["3d", "Alice, Bob"])
            builtins.input = lambda prompt="": next(answers)
            self.assertEqual(agentize.ask_history_defaults(),
                             ("3d", ["Alice", "Bob"]))
        finally:
            builtins.input = old_input


class TestMakefileExtraction(unittest.TestCase):
    """Makefile parsing: `:=` variable assignments are NOT targets."""

    def _makefile(self, text):
        d = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        (d / "Makefile").write_text(text, encoding="utf-8")
        return d

    def test_assignments_not_targets(self):
        # `CC := gcc` / `CFLAGS := -O2` are variable assignments, not targets
        d = self._makefile("CC := gcc\nCFLAGS := -O2\n")
        self.assertEqual(agentize.extract_makefile(d), [])

    def test_real_targets_still_found(self):
        d = self._makefile("CC := gcc\nbuild: main.c\n\ttest -f main.c\n")
        cmds = agentize.extract_makefile(d)
        self.assertEqual([c["cmd"] for c in cmds], ["make build"])


class TestStructureMap(unittest.TestCase):
    """structure_map buckets files by top-level segment — one pass, no O(n²)."""

    def test_counts_per_directory(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            for sub, n in (("src", 4), ("tests", 2)):
                (root / sub).mkdir()
                for i in range(n):
                    (root / sub / f"f{i}.py").write_text("x")
            rows = dict(agentize.structure_map(root, agentize.walk_repo(root)))
            self.assertEqual(rows.get("src/"), "source code (4 files)")
            self.assertEqual(rows.get("tests/"), "tests (2 files)")

    def test_many_files_counted_correctly(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            (root / "lib").mkdir()
            for i in range(300):
                (root / "lib" / f"m{i}.py").write_text("x")
            rows = dict(agentize.structure_map(root, agentize.walk_repo(root)))
            self.assertEqual(rows.get("lib/"), "shared libraries/utilities (300 files)")


if __name__ == "__main__":
    unittest.main()