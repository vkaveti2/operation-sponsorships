"""The pre-commit hook guards a PUBLIC repository, so it gets real tests. Each case builds a throwaway git
repository, stages some files and checks whether `git commit` is stopped.

Fake keys are assembled at run time, so this file itself never contains a key-shaped string."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "scripts" / "hooks"

pytestmark = pytest.mark.skipif(not (shutil.which("git") and shutil.which("bash")), reason="needs git and bash")


def fake(prefix: str, length: int = 45) -> str:
    return prefix + "Q" * length


def git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT="0")
    return env


class Repo:
    def __init__(self, path: Path):
        self.path = path
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.git("-c", "core.hooksPath=" + os.devnull, "commit", "-q", "--allow-empty", "-m", "init")
        self.git("config", "core.hooksPath", str(HOOKS))
        self.base = self.git("rev-parse", "HEAD").stdout.strip()

    def git(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=self.path, capture_output=True, text=True, env=git_env())

    def write(self, files: dict) -> None:
        for name, text in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def try_commit(self, files: dict) -> tuple:
        """Stage the files, try to commit, then put the repository back. Returns (blocked, hook message)."""
        self.write(files)
        self.git("add", "-f", *files)
        result = self.git("commit", "-q", "-m", "t")
        self.git("reset", "-q", "--hard", self.base)
        self.git("clean", "-fdq")
        blocked = result.returncode != 0
        assert not blocked or "pre-commit:" in result.stderr, result.stderr  # a refusal must come from the hook itself
        return blocked, result.stderr


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


BLOCKED = [
    ("env file", {".env": "SOMETHING=1\n"}),
    ("env variant", {".env.local": "SOMETHING=1\n"}),
    ("oauth client file", {"config/credentials.json": "{}"}),
    ("saved google login", {"config/token.json": "{}"}),
    ("downloaded client secret", {"client_secret_1234.json": "{}"}),
    ("pem file", {"key.pem": "x"}),
    ("logs", {"logs/outreach.log": "x"}),
    ("lead list", {"data/targets.csv": "a@b.com\n"}),
    ("lookup memory", {"data/lookups.json": "{}"}),
    ("claude key in code", {"a.py": 'k = "%s"\n' % fake("sk-ant-api03-")}),
    ("old google key in code", {"a.py": 'k = "%s"\n' % fake("AIza", 35)}),
    ("new google key in code", {"a.py": 'k = "%s"\n' % fake("AQ.", 50)}),
    ("oauth client secret", {"a.py": 'k = "%s"\n' % fake("GOCSPX-", 28)}),
    ("access token", {"a.py": 'k = "%s"\n' % fake("ya29.", 40)}),
    ("refresh token", {"a.py": 'k = "%s"\n' % fake("1//", 40)}),
    ("github token", {"a.py": 'k = "%s"\n' % fake("ghp_", 36)}),
    ("private key", {"a.txt": "-----BEGIN " + "PRIVATE KEY-----\n"}),
    ("name = long value", {"a.py": 'HUNTER_API_KEY = "%s"\n' % ("d" * 40)}),
    ("key-shaped value in docs", {"notes.md": "token: %s\n" % fake("AQ.", 50)}),
]

ALLOWED = [
    ("plain code", {"a.py": "print('hello')\n"}),
    ("template with blank keys", {".env.template": "ANTHROPIC_API_KEY=\nGEMINI_API_KEY=\n"}),
    ("example brand list", {"data/targets.example.csv": "Company Name,Domain\nRed Bull,redbull.com\n"}),
    ("code that reads a key from the environment", {"a.py": 'key = os.environ["GEMINI_API_KEY"]\n'}),
    ("placeholder in docs", {"README.md": "GEMINI_API_KEY=your-key-here\n"}),
    ("short look-alike", {"a.py": 'x = "AQ.short"\n'}),
]


@pytest.mark.parametrize("files", [case[1] for case in BLOCKED], ids=[case[0] for case in BLOCKED])
def test_the_hook_stops_secrets_and_lead_data(repo, files):
    blocked, message = repo.try_commit(files)
    assert blocked, "this commit should have been refused"
    assert "BLOCKED" in message and "commit aborted" in message


@pytest.mark.parametrize("files", [case[1] for case in ALLOWED], ids=[case[0] for case in ALLOWED])
def test_the_hook_lets_ordinary_files_through(repo, files):
    blocked, message = repo.try_commit(files)
    assert not blocked, message


def test_deleting_a_secret_that_was_committed_by_mistake_is_allowed(repo):
    repo.write({".env": "SOMETHING=1\n"})
    repo.git("add", "-f", ".env")
    assert repo.git("commit", "-q", "--no-verify", "-m", "oops").returncode == 0
    repo.git("rm", "-q", ".env")
    result = repo.git("commit", "-q", "-m", "remove it")
    assert result.returncode == 0, result.stderr
