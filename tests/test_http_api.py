"""Tests for HTTP API endpoints (end-to-end with real server)."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import serve


# ---------------------------------------------------------------------------
# GET routes
# ---------------------------------------------------------------------------

class TestGetRoutes:
    def test_api_skills(self, http_server, get_json):
        base_url, _ = http_server
        status, data = get_json(f"{base_url}/api/skills")
        assert status == 200
        assert "installed" in data
        assert "available" in data
        assert "stats" in data

    def test_api_marketplaces(self, http_server, get_json):
        base_url, _ = http_server
        status, data = get_json(f"{base_url}/api/marketplaces")
        assert status == 200
        assert isinstance(data, list)

    def test_root_serves_something(self, http_server):
        from urllib.request import urlopen
        base_url, _ = http_server
        resp = urlopen(f"{base_url}/", timeout=5)
        assert resp.status == 200

    def test_nonexistent_api_route(self, http_server):
        from urllib.request import urlopen
        from urllib.error import HTTPError
        base_url, _ = http_server
        try:
            urlopen(f"{base_url}/api/nonexistent", timeout=5)
            assert False, "Should have raised HTTPError"
        except HTTPError as e:
            assert e.code == 404


# ---------------------------------------------------------------------------
# POST /api/toggle
# ---------------------------------------------------------------------------

class TestToggle:
    def test_valid_toggle(self, http_server, post_json, tmp_claude_dir, skill_factory):
        base_url, _ = http_server
        skill_factory(tmp_claude_dir / "skills" / "tog", name="tog")
        status, data = post_json(f"{base_url}/api/toggle", {"path": "skills/tog/SKILL.md", "enabled": False})
        assert status == 200
        assert data["ok"] is True

    def test_empty_path(self, http_server, post_json):
        from urllib.error import URLError
        from http.client import RemoteDisconnected
        base_url, _ = http_server
        # Empty path causes ValueError in set_skill_enabled (relative_to fails)
        # Server doesn't catch it, so connection drops
        with pytest.raises((URLError, RemoteDisconnected, ConnectionError)):
            post_json(f"{base_url}/api/toggle", {"path": "", "enabled": True})

    def test_filesystem_state(self, http_server, post_json, tmp_claude_dir, skill_factory):
        base_url, _ = http_server
        skill_factory(tmp_claude_dir / "skills" / "fs", name="fs")
        post_json(f"{base_url}/api/toggle", {"path": "skills/fs/SKILL.md", "enabled": False})
        assert (tmp_claude_dir / "skills" / "fs" / "SKILL.md.disabled").exists()


# ---------------------------------------------------------------------------
# POST /api/install + /api/update-plugin
# ---------------------------------------------------------------------------

class TestInstallUpdate:
    def test_invalid_plugin_id(self, http_server, post_json):
        base_url, _ = http_server
        status, data = post_json(f"{base_url}/api/install", {"plugin_id": "nope"})
        assert status == 400

    def test_valid_install(self, http_server, post_json, monkeypatch):
        base_url, _ = http_server
        monkeypatch.setattr("serve.subprocess.run",
                            lambda *a, **kw: SimpleNamespace(returncode=0, stdout="OK", stderr=""))
        monkeypatch.setattr(serve, "apply_prefs", lambda: 0)
        status, data = post_json(f"{base_url}/api/install", {"plugin_id": "test@mp"})
        assert status == 200
        assert data["ok"] is True


# ---------------------------------------------------------------------------
# POST /api/uninstall
# ---------------------------------------------------------------------------

class TestUninstall:
    def test_invalid_plugin_id(self, http_server, post_json):
        base_url, _ = http_server
        status, data = post_json(f"{base_url}/api/uninstall", {"plugin_id": "bad"})
        assert status == 400

    def test_valid_uninstall(self, http_server, post_json, monkeypatch):
        base_url, _ = http_server
        monkeypatch.setattr("serve.subprocess.run",
                            lambda *a, **kw: SimpleNamespace(returncode=0, stdout="OK", stderr=""))
        status, data = post_json(f"{base_url}/api/uninstall", {"plugin_id": "test@mp"})
        assert status == 200
        assert data["ok"] is True


# ---------------------------------------------------------------------------
# POST /api/toggle-component
# ---------------------------------------------------------------------------

class TestToggleComponent:
    def test_path_outside_claude(self, http_server, post_json):
        base_url, _ = http_server
        status, data = post_json(f"{base_url}/api/toggle-component",
                                 {"abs_path": "/etc/passwd", "enabled": True})
        assert status == 403

    def test_valid_path(self, http_server, post_json, tmp_claude_dir):
        base_url, _ = http_server
        f = tmp_claude_dir / "commands" / "cmd.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("---\nname: cmd\n---\n")
        status, data = post_json(f"{base_url}/api/toggle-component",
                                 {"abs_path": str(f), "enabled": False})
        assert status == 200
        assert data["ok"] is True

    def test_component_renamed(self, http_server, post_json, tmp_claude_dir):
        base_url, _ = http_server
        f = tmp_claude_dir / "agents" / "ag.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("---\nname: ag\n---\n")
        post_json(f"{base_url}/api/toggle-component", {"abs_path": str(f), "enabled": False})
        assert (tmp_claude_dir / "agents" / "ag.md.disabled").exists()


# ---------------------------------------------------------------------------
# POST /api/move-skill-to-scanned
# ---------------------------------------------------------------------------

class TestMoveSkill:
    def test_missing_name(self, http_server, post_json, tmp_claude_dir):
        base_url, _ = http_server
        status, data = post_json(f"{base_url}/api/move-skill-to-scanned",
                                 {"abs_path": str(tmp_claude_dir / "x"), "name": ""})
        assert status == 400

    def test_path_outside_claude(self, http_server, post_json):
        base_url, _ = http_server
        status, data = post_json(f"{base_url}/api/move-skill-to-scanned",
                                 {"abs_path": "/etc/passwd", "name": "test"})
        assert status == 403


# ---------------------------------------------------------------------------
# POST /api/toggle-model-invocation
# ---------------------------------------------------------------------------

class TestToggleModelInvocation:
    def test_valid_disable(self, http_server, post_json, tmp_claude_dir):
        base_url, _ = http_server
        f = tmp_claude_dir / "skills" / "mi" / "SKILL.md"
        f.parent.mkdir(parents=True)
        f.write_text("---\nname: mi\n---\nbody")
        status, data = post_json(f"{base_url}/api/toggle-model-invocation",
                                 {"abs_path": str(f), "disable": True})
        assert status == 200
        assert data["ok"] is True

    def test_path_outside_claude(self, http_server, post_json):
        base_url, _ = http_server
        status, _ = post_json(f"{base_url}/api/toggle-model-invocation",
                              {"abs_path": "/tmp/evil.md", "disable": True})
        assert status == 403


# ---------------------------------------------------------------------------
# POST /api/remove-missing
# ---------------------------------------------------------------------------

class TestRemoveMissing:
    def test_missing_plugin_id(self, http_server, post_json):
        base_url, _ = http_server
        status, data = post_json(f"{base_url}/api/remove-missing", {})
        assert status == 400

    def test_not_found(self, http_server, post_json, tmp_claude_dir):
        base_url, _ = http_server
        serve.INSTALLED_PLUGINS_FILE.write_text(json.dumps({"plugins": {}}))
        status, data = post_json(f"{base_url}/api/remove-missing", {"plugin_id": "nope@mp"})
        assert status == 404

    def test_found_and_removed(self, http_server, post_json, tmp_claude_dir):
        base_url, _ = http_server
        data = {"plugins": {"test@mp": [{"installPath": "/old", "version": "1.0"}]}}
        serve.INSTALLED_PLUGINS_FILE.write_text(json.dumps(data))
        status, resp = post_json(f"{base_url}/api/remove-missing", {"plugin_id": "test@mp"})
        assert status == 200
        assert resp["ok"] is True
        reloaded = json.loads(serve.INSTALLED_PLUGINS_FILE.read_text())
        assert "test@mp" not in reloaded["plugins"]


# ---------------------------------------------------------------------------
# POST /api/remove-local-plugin
# ---------------------------------------------------------------------------

class TestRemoveLocalPlugin:
    def test_name_with_slash(self, http_server, post_json):
        base_url, _ = http_server
        status, data = post_json(f"{base_url}/api/remove-local-plugin", {"name": "a/b"})
        assert status == 400

    def test_name_with_dotdot(self, http_server, post_json):
        base_url, _ = http_server
        status, data = post_json(f"{base_url}/api/remove-local-plugin", {"name": ".."})
        assert status == 400

    def test_valid_archive(self, http_server, post_json, tmp_claude_dir, skill_factory):
        base_url, _ = http_server
        plugin_dir = tmp_claude_dir / "plugins" / "local-test"
        skill_factory(plugin_dir / "skills" / "s", name="s")
        status, data = post_json(f"{base_url}/api/remove-local-plugin", {"name": "local-test"})
        assert status == 200
        assert data["ok"] is True
        assert (tmp_claude_dir / "plugins" / "_archived" / "local-test").is_dir()


# ---------------------------------------------------------------------------
# POST /api/check-updates
# ---------------------------------------------------------------------------

class TestCheckUpdates:
    def test_empty_marketplaces(self, http_server, post_json):
        base_url, _ = http_server
        status, data = post_json(f"{base_url}/api/check-updates", {})
        assert status == 200


# ---------------------------------------------------------------------------
# Marketplace routes
# ---------------------------------------------------------------------------

class TestMarketplaceRoutes:
    def test_add_missing_url(self, http_server, post_json):
        base_url, _ = http_server
        status, data = post_json(f"{base_url}/api/marketplace-add", {"url": ""})
        assert status == 400

    def test_remove_missing_name(self, http_server, post_json):
        base_url, _ = http_server
        status, data = post_json(f"{base_url}/api/marketplace-remove", {"name": ""})
        assert status == 400

    def test_update_no_name(self, http_server, post_json, monkeypatch):
        base_url, _ = http_server
        monkeypatch.setattr("serve.subprocess.run",
                            lambda *a, **kw: SimpleNamespace(returncode=0, stdout="OK", stderr=""))
        status, data = post_json(f"{base_url}/api/marketplace-update", {"name": ""})
        assert status == 200


# ---------------------------------------------------------------------------
# Unknown POST route
# ---------------------------------------------------------------------------

class TestUnknownPost:
    def test_returns_404(self, http_server):
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        base_url, _ = http_server
        req = Request(f"{base_url}/api/doesnotexist",
                      data=b'{}', headers={"Content-Type": "application/json"}, method="POST")
        try:
            urlopen(req, timeout=5)
            assert False, "Should have raised HTTPError"
        except HTTPError as e:
            assert e.code == 404
