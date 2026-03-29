"""Tests for functions that call subprocess.run (mocked)."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import serve


def _mock_run(returncode=0, stdout="", stderr=""):
    """Create a mock subprocess.run that returns a fixed result."""
    def mock(*args, **kwargs):
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    return mock


# ---------------------------------------------------------------------------
# _find_repo_root
# ---------------------------------------------------------------------------

class TestFindRepoRoot:
    def test_success(self, monkeypatch):
        monkeypatch.setattr("serve.subprocess.run",
                            _mock_run(returncode=0, stdout="/repo/root\n"))
        result = serve._find_repo_root("/some/dir")
        assert result == "/repo/root"

    def test_non_zero_return(self, monkeypatch):
        monkeypatch.setattr("serve.subprocess.run",
                            _mock_run(returncode=128, stdout=""))
        result = serve._find_repo_root("/not/a/repo")
        assert result is None

    def test_exception(self, monkeypatch):
        def raise_err(*args, **kwargs):
            raise OSError("git not found")
        monkeypatch.setattr("serve.subprocess.run", raise_err)
        result = serve._find_repo_root("/some/dir")
        assert result is None

    def test_cache_hit_repo_root(self, monkeypatch):
        serve._repo_root_cache["/cached/dir"] = "/cached/root"
        result = serve._find_repo_root("/cached/dir")
        assert result == "/cached/root"

    def test_cache_hit_from_dirty_cache(self, monkeypatch):
        serve._git_dirty_cache["/repo"] = set()
        result = serve._find_repo_root("/repo/sub/dir")
        assert result == "/repo"


# ---------------------------------------------------------------------------
# _is_git_dirty
# ---------------------------------------------------------------------------

class TestIsGitDirty:
    def test_no_repo_root(self, monkeypatch):
        monkeypatch.setattr(serve, "_find_repo_root", lambda d: None)
        assert serve._is_git_dirty(Path("/some/file.md")) is False

    def test_file_in_porcelain(self, monkeypatch, tmp_path):
        repo = str(tmp_path / "repo")
        file_path = tmp_path / "repo" / "SKILL.md"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        monkeypatch.setattr(serve, "_find_repo_root", lambda d: repo)
        monkeypatch.setattr("serve.subprocess.run",
                            _mock_run(returncode=0, stdout=" M SKILL.md\n"))
        result = serve._is_git_dirty(file_path)
        assert result is True

    def test_file_not_dirty(self, monkeypatch, tmp_path):
        repo = str(tmp_path / "repo")
        file_path = tmp_path / "repo" / "subdir" / "clean.md"
        file_path.parent.mkdir(parents=True)
        file_path.touch()
        # Dirty file is in a completely different directory
        other_dir = tmp_path / "repo" / "other"
        other_dir.mkdir(parents=True)

        monkeypatch.setattr(serve, "_find_repo_root", lambda d: repo)
        # _is_git_dirty joins repo_root + relative path from porcelain
        monkeypatch.setattr("serve.subprocess.run",
                            _mock_run(returncode=0, stdout=" M other/something.md\n"))
        result = serve._is_git_dirty(file_path)
        assert result is False

    def test_sibling_file_dirty(self, monkeypatch, tmp_path):
        """A file in the same directory as a dirty file should be detected via dir check."""
        repo = str((tmp_path / "repo").resolve())
        skill_dir = (tmp_path / "repo" / "skills" / "my-skill").resolve()
        file_path = skill_dir / "SKILL.md"
        skill_dir.mkdir(parents=True)
        file_path.touch()

        monkeypatch.setattr(serve, "_find_repo_root", lambda d: repo)
        # Pre-populate dirty cache directly with the absolute dirty path
        # to avoid subprocess mock path-joining issues
        import os
        dirty_sibling = os.path.join(repo, "skills", "my-skill", "helper.py")
        serve._git_dirty_cache[repo] = {dirty_sibling}
        result = serve._is_git_dirty(file_path)
        assert result is True

    def test_exception_returns_false(self, monkeypatch, tmp_path):
        repo = str(tmp_path / "repo")
        file_path = tmp_path / "repo" / "SKILL.md"
        file_path.parent.mkdir(parents=True)
        file_path.touch()

        monkeypatch.setattr(serve, "_find_repo_root", lambda d: repo)
        def raise_err(*args, **kwargs):
            raise OSError("fail")
        monkeypatch.setattr("serve.subprocess.run", raise_err)
        assert serve._is_git_dirty(file_path) is False


# ---------------------------------------------------------------------------
# _marketplace_repo_url
# ---------------------------------------------------------------------------

class TestMarketplaceRepoUrl:
    def test_https_url(self, tmp_claude_dir, monkeypatch):
        mp = serve.MARKETPLACES_DIR / "my-mp"
        mp.mkdir()
        monkeypatch.setattr("serve.subprocess.run",
                            _mock_run(returncode=0, stdout="https://github.com/user/repo.git\n"))
        result = serve._marketplace_repo_url("my-mp")
        assert result == "https://github.com/user/repo"

    def test_ssh_url_conversion(self, tmp_claude_dir, monkeypatch):
        mp = serve.MARKETPLACES_DIR / "ssh-mp"
        mp.mkdir()
        monkeypatch.setattr("serve.subprocess.run",
                            _mock_run(returncode=0, stdout="git@github.com:user/repo.git\n"))
        result = serve._marketplace_repo_url("ssh-mp")
        assert result == "https://github.com/user/repo"

    def test_non_zero_return(self, tmp_claude_dir, monkeypatch):
        mp = serve.MARKETPLACES_DIR / "bad"
        mp.mkdir()
        monkeypatch.setattr("serve.subprocess.run", _mock_run(returncode=1))
        assert serve._marketplace_repo_url("bad") is None

    def test_missing_dir(self, tmp_claude_dir):
        assert serve._marketplace_repo_url("nonexistent") is None


# ---------------------------------------------------------------------------
# _run_plugin_install
# ---------------------------------------------------------------------------

class TestRunPluginInstall:
    def test_success(self, tmp_claude_dir, monkeypatch):
        monkeypatch.setattr("serve.subprocess.run",
                            _mock_run(returncode=0, stdout="Installed!"))
        monkeypatch.setattr(serve, "apply_prefs", lambda: 2)
        result = serve._run_plugin_install("plugin@mp")
        assert result["ok"] is True
        assert result["prefs_applied"] == 2

    def test_failure(self, tmp_claude_dir, monkeypatch):
        monkeypatch.setattr("serve.subprocess.run",
                            _mock_run(returncode=1, stderr="Not found"))
        result = serve._run_plugin_install("bad@mp")
        assert result["ok"] is False
        assert "Not found" in result["stderr"]


# ---------------------------------------------------------------------------
# refresh_and_check_updates
# ---------------------------------------------------------------------------

class TestRefreshAndCheckUpdates:
    def test_no_marketplaces_dir(self, tmp_claude_dir, monkeypatch):
        import shutil
        shutil.rmtree(serve.MARKETPLACES_DIR)
        result = serve.refresh_and_check_updates()
        assert result == {"updates": [], "errors": [], "pulled_changes": False}

    def test_successful_pull(self, tmp_claude_dir, monkeypatch):
        mp = serve.MARKETPLACES_DIR / "test-mp"
        mp.mkdir()
        (mp / ".git").mkdir()
        calls = []
        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            if "pull" in cmd:
                return SimpleNamespace(returncode=0, stdout="Updating abc..def\n", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        monkeypatch.setattr("serve.subprocess.run", mock_run)
        result = serve.refresh_and_check_updates()
        assert result["pulled_changes"] is True
        assert result["errors"] == []
