"""Permanent test suite for agentize — zero-dependency (stdlib unittest).

Run from the repo root:  python -m unittest discover -s tests -v
Or via pytest, if you have it:  pytest tests/ -q
"""
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import agentize  # noqa: E402

WEB = REPO / "tests" / "fixture_web"
ML = REPO / "tests" / "fixture_ml"
AGENTIZE = REPO / "agentize.py"


def run_cli(*args):
    return subprocess.run([sys.executable, str(AGENTIZE), *args],
                          capture_output=True, text=True)


class TestExtractionWeb(unittest.TestCase):
    """The JS/TS fixture: every command must be real and sourced."""

    @classmethod
    def setUpClass(cls):
        cls.ev = agentize.analyze(WEB)
        cls.md = agentize.render(cls.ev)

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
        cls.ev = agentize.analyze(ML)
        cls.md = agentize.render(cls.ev)

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
    def test_evidence_is_json(self):
        r = run_cli(str(WEB), "--json")
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertIsInstance(data, dict)
        self.assertIn("commands", data)
        self.assertIn("roles", data)
        self.assertTrue(data["commands"])  # evidence is non-empty


class TestLifecycle(unittest.TestCase):
    """Write / refuse-overwrite / force / --claude / --cursor, on a temp copy.
    Each test gets its own fresh copy — methods run alphabetically, so
    shared state (a leftover AGENTS.md) would make assertions order-dependent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.web = pathlib.Path(self.tmp.name) / "web"
        shutil.copytree(WEB, self.web)
        self.addCleanup(self.tmp.cleanup)

    def test_write_then_refuse_then_force(self):
        r1 = run_cli(str(self.web))
        self.assertEqual(r1.returncode, 0)
        self.assertIn("wrote", r1.stdout)

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


if __name__ == "__main__":
    unittest.main()
