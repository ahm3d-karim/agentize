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
        self.assertTrue((self.web / ".cursorrules").exists())


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
        self.assertIn("Evidence JSON", prompt)
        self.assertIn("REAL config files", prompt)

    def test_render_prefers_ai_overview(self):
        ev = {"name": "x", "stack": {"languages": ["Python"], "frameworks": [],
              "pm": None, "test": [], "linters": [], "ts_strict": False},
              "roles": {}, "description": "readme says this",
              "ai_overview": "the model says this", "commands": []}
        md = agentize.render(ev)
        self.assertIn("the model says this", md)
        self.assertNotIn("readme says this", md)

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


if __name__ == "__main__":
    unittest.main()
