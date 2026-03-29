"""Shared fixtures for skill-manager tests."""

import json
import sys
import threading
from http.server import HTTPServer
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest

# Ensure serve.py is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import serve


# ---------------------------------------------------------------------------
# Autouse: clear module-level caches before/after each test
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_module_caches():
    """Clear all 5 module-level caches before and after each test."""
    caches = [
        serve._git_dirty_cache,
        serve._repo_root_cache,
        serve._mp_repo_cache,
    ]
    for c in caches:
        c.clear()
    serve._invalidate_skills_cache()
    yield
    for c in caches:
        c.clear()
    serve._invalidate_skills_cache()


# ---------------------------------------------------------------------------
# tmp_claude_dir: mock ~/.claude/ structure in tmp_path
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_claude_dir(tmp_path, monkeypatch):
    """Create a mock ~/.claude/ structure and monkeypatch all 12 serve.py globals."""
    claude = tmp_path / ".claude"
    plugins = claude / "plugins"
    marketplaces = plugins / "marketplaces"
    cache = plugins / "cache"
    skills = claude / "skills"
    agents = claude / "agents"
    commands = claude / "commands"
    data_dir = claude / "skill-manager-data"

    for d in (claude, plugins, marketplaces, cache, skills, agents, commands, data_dir):
        d.mkdir(parents=True, exist_ok=True)

    installed_plugins_file = plugins / "installed_plugins.json"
    known_marketplaces_file = plugins / "known_marketplaces.json"
    prefs_file = data_dir / "preferences.json"
    settings_file = claude / "settings.json"

    monkeypatch.setattr(serve, "CLAUDE_DIR", claude)
    monkeypatch.setattr(serve, "PLUGINS_DIR", plugins)
    monkeypatch.setattr(serve, "MARKETPLACES_DIR", marketplaces)
    monkeypatch.setattr(serve, "CACHE_DIR", cache)
    monkeypatch.setattr(serve, "SKILLS_DIR", skills)
    monkeypatch.setattr(serve, "AGENTS_DIR", agents)
    monkeypatch.setattr(serve, "COMMANDS_DIR", commands)
    monkeypatch.setattr(serve, "INSTALLED_PLUGINS_FILE", installed_plugins_file)
    monkeypatch.setattr(serve, "KNOWN_MARKETPLACES_FILE", known_marketplaces_file)
    monkeypatch.setattr(serve, "DATA_DIR", data_dir)
    monkeypatch.setattr(serve, "PREFS_FILE", prefs_file)
    monkeypatch.setattr(serve, "SETTINGS_FILE", settings_file)

    return claude


# ---------------------------------------------------------------------------
# Factories
# ---------------------------------------------------------------------------

@pytest.fixture
def skill_factory():
    """Callable that creates SKILL.md files with frontmatter."""
    def _create(directory: Path, name: str = "test-skill", description: str = "A test skill",
                extra_fm: dict | None = None, disabled: bool = False, content_body: str = ""):
        directory.mkdir(parents=True, exist_ok=True)
        filename = "SKILL.md.disabled" if disabled else "SKILL.md"
        fm_lines = [f"name: {name}", f"description: {description}"]
        if extra_fm:
            for k, v in extra_fm.items():
                fm_lines.append(f"{k}: {v}")
        text = "---\n" + "\n".join(fm_lines) + "\n---\n" + content_body
        (directory / filename).write_text(text, encoding="utf-8")
        return directory / filename
    return _create


@pytest.fixture
def plugin_factory(skill_factory):
    """Callable that creates plugin dirs with .claude-plugin/plugin.json + skills."""
    def _create(plugin_dir: Path, manifest: dict | None = None,
                skills: list[str] | None = None):
        plugin_dir.mkdir(parents=True, exist_ok=True)
        cp_dir = plugin_dir / ".claude-plugin"
        cp_dir.mkdir(exist_ok=True)
        if manifest is None:
            manifest = {"name": plugin_dir.name, "version": "1.0.0"}
        (cp_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")

        if skills is None:
            skills = ["my-skill"]
        skills_dir = plugin_dir / "skills"
        for s in skills:
            skill_factory(skills_dir / s, name=s, description=f"Desc for {s}")
        return plugin_dir
    return _create


# ---------------------------------------------------------------------------
# HTTP server fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def http_server(tmp_claude_dir, monkeypatch):
    """Start a real HTTPServer on port 0, yield (base_url, server)."""
    import os
    # Ensure Handler serves from the project directory
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)

    server = HTTPServer(("127.0.0.1", 0), serve.Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}", server
    server.shutdown()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def get_json():
    """GET helper that returns parsed JSON."""
    def _get(url: str) -> tuple[int, dict | list]:
        try:
            resp = urlopen(url, timeout=5)
            return resp.status, json.loads(resp.read())
        except HTTPError as e:
            return e.code, json.loads(e.read()) if e.read() else {}
    return _get


@pytest.fixture
def post_json():
    """POST helper that sends JSON and returns (status, parsed_body)."""
    def _post(url: str, data: dict) -> tuple[int, dict]:
        body = json.dumps(data).encode()
        req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            resp = urlopen(req, timeout=5)
            return resp.status, json.loads(resp.read())
        except HTTPError as e:
            raw = e.read()
            return e.code, json.loads(raw) if raw else {}
    return _post
