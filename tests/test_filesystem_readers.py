"""Tests for filesystem reader functions."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import serve


# ---------------------------------------------------------------------------
# _find_skill_md
# ---------------------------------------------------------------------------

class TestFindSkillMd:
    def test_skill_md_found(self, tmp_path):
        d = tmp_path / "skill"
        d.mkdir()
        (d / "SKILL.md").write_text("content")
        assert serve._find_skill_md(d).name == "SKILL.md"

    def test_disabled_found(self, tmp_path):
        d = tmp_path / "skill"
        d.mkdir()
        (d / "SKILL.md.disabled").write_text("content")
        assert serve._find_skill_md(d).name == "SKILL.md.disabled"

    def test_neither(self, tmp_path):
        d = tmp_path / "skill"
        d.mkdir()
        assert serve._find_skill_md(d) is None

    def test_both_prefers_enabled(self, tmp_path):
        d = tmp_path / "skill"
        d.mkdir()
        (d / "SKILL.md").write_text("enabled")
        (d / "SKILL.md.disabled").write_text("disabled")
        assert serve._find_skill_md(d).name == "SKILL.md"


# ---------------------------------------------------------------------------
# _scan_skill_dirs
# ---------------------------------------------------------------------------

class TestScanSkillDirs:
    def test_empty_dir(self, tmp_claude_dir):
        parent = tmp_claude_dir / "empty"
        parent.mkdir()
        assert serve._scan_skill_dirs(parent, skip_git=True) == []

    def test_valid_skills(self, tmp_claude_dir, skill_factory):
        parent = tmp_claude_dir / "skills"
        skill_factory(parent / "alpha", name="alpha")
        skill_factory(parent / "beta", name="beta")
        results = serve._scan_skill_dirs(parent, skip_git=True)
        names = [r["name"] for r in results]
        assert "alpha" in names
        assert "beta" in names

    def test_dotfiles_skipped(self, tmp_claude_dir, skill_factory):
        parent = tmp_claude_dir / "skills"
        skill_factory(parent / ".hidden", name="hidden")
        skill_factory(parent / "visible", name="visible")
        results = serve._scan_skill_dirs(parent, skip_git=True)
        names = [r["name"] for r in results]
        assert "hidden" not in names
        assert "visible" in names

    def test_non_dirs_skipped(self, tmp_claude_dir, skill_factory):
        parent = tmp_claude_dir / "skills"
        skill_factory(parent / "valid", name="valid")
        (parent / "file.txt").write_text("not a dir")
        results = serve._scan_skill_dirs(parent, skip_git=True)
        assert len(results) == 1

    def test_nonexistent_parent(self):
        assert serve._scan_skill_dirs(Path("/nonexistent/path"), skip_git=True) == []


# ---------------------------------------------------------------------------
# get_char_budget
# ---------------------------------------------------------------------------

class TestGetCharBudget:
    def test_default_when_no_config(self, tmp_claude_dir, monkeypatch):
        monkeypatch.delenv("SLASH_COMMAND_TOOL_CHAR_BUDGET", raising=False)
        budget, source = serve.get_char_budget()
        assert budget == 15000
        assert source == "default"

    def test_env_var_set(self, tmp_claude_dir, monkeypatch):
        monkeypatch.setenv("SLASH_COMMAND_TOOL_CHAR_BUDGET", "20000")
        budget, source = serve.get_char_budget()
        assert budget == 20000
        assert source == "env"

    def test_settings_json(self, tmp_claude_dir, monkeypatch):
        monkeypatch.delenv("SLASH_COMMAND_TOOL_CHAR_BUDGET", raising=False)
        settings = {"env": {"SLASH_COMMAND_TOOL_CHAR_BUDGET": "25000"}}
        serve.SETTINGS_FILE.write_text(json.dumps(settings))
        budget, source = serve.get_char_budget()
        assert budget == 25000
        assert source == "settings.json"

    def test_env_var_takes_priority(self, tmp_claude_dir, monkeypatch):
        monkeypatch.setenv("SLASH_COMMAND_TOOL_CHAR_BUDGET", "30000")
        settings = {"env": {"SLASH_COMMAND_TOOL_CHAR_BUDGET": "25000"}}
        serve.SETTINGS_FILE.write_text(json.dumps(settings))
        budget, source = serve.get_char_budget()
        assert budget == 30000
        assert source == "env"

    def test_missing_settings_file(self, tmp_claude_dir, monkeypatch):
        monkeypatch.delenv("SLASH_COMMAND_TOOL_CHAR_BUDGET", raising=False)
        # settings file doesn't exist
        budget, source = serve.get_char_budget()
        assert budget == 15000
        assert source == "default"

    def test_zero_value_falls_through(self, tmp_claude_dir, monkeypatch):
        monkeypatch.setenv("SLASH_COMMAND_TOOL_CHAR_BUDGET", "0")
        budget, source = serve.get_char_budget()
        assert budget == 15000
        assert source == "default"


# ---------------------------------------------------------------------------
# get_installed_plugins
# ---------------------------------------------------------------------------

class TestGetInstalledPlugins:
    def test_missing_file(self, tmp_claude_dir):
        assert serve.get_installed_plugins() == {}

    def test_valid_data(self, tmp_claude_dir):
        data = {
            "plugins": {
                "my-plugin@mp": [
                    {"installPath": "/some/path", "version": "1.0.0"}
                ]
            }
        }
        serve.INSTALLED_PLUGINS_FILE.write_text(json.dumps(data))
        result = serve.get_installed_plugins()
        assert len(result) == 1

    def test_multiple_entries(self, tmp_claude_dir):
        data = {
            "plugins": {
                "p1@mp": [{"installPath": "/path1", "version": "1.0"}],
                "p2@mp": [{"installPath": "/path2", "version": "2.0"}],
            }
        }
        serve.INSTALLED_PLUGINS_FILE.write_text(json.dumps(data))
        result = serve.get_installed_plugins()
        assert len(result) == 2

    def test_malformed_json(self, tmp_claude_dir):
        serve.INSTALLED_PLUGINS_FILE.write_text("{invalid json")
        try:
            serve.get_installed_plugins()
        except json.JSONDecodeError:
            pass  # expected

    def test_empty_plugins(self, tmp_claude_dir):
        data = {"plugins": {}}
        serve.INSTALLED_PLUGINS_FILE.write_text(json.dumps(data))
        assert serve.get_installed_plugins() == {}


# ---------------------------------------------------------------------------
# load_prefs / save_prefs
# ---------------------------------------------------------------------------

class TestPrefs:
    def test_missing_file(self, tmp_claude_dir):
        assert serve.load_prefs() == {}

    def test_round_trip(self, tmp_claude_dir):
        prefs = {"skills/foo": True, "skills/bar": False}
        serve.save_prefs(prefs)
        loaded = serve.load_prefs()
        assert loaded == prefs

    def test_save_creates_file(self, tmp_claude_dir):
        serve.save_prefs({"test": True})
        assert serve.PREFS_FILE.exists()

    def test_overwrite(self, tmp_claude_dir):
        serve.save_prefs({"a": True})
        serve.save_prefs({"b": True})
        loaded = serve.load_prefs()
        assert "a" not in loaded
        assert loaded["b"] is True


# ---------------------------------------------------------------------------
# read_plugin_manifest
# ---------------------------------------------------------------------------

class TestReadPluginManifest:
    def test_present(self, tmp_path):
        pd = tmp_path / "plugin"
        cp = pd / ".claude-plugin"
        cp.mkdir(parents=True)
        manifest = {"name": "test", "version": "1.0"}
        (cp / "plugin.json").write_text(json.dumps(manifest))
        assert serve.read_plugin_manifest(pd) == manifest

    def test_missing(self, tmp_path):
        assert serve.read_plugin_manifest(tmp_path) is None

    def test_invalid_json(self, tmp_path):
        pd = tmp_path / "plugin"
        cp = pd / ".claude-plugin"
        cp.mkdir(parents=True)
        (cp / "plugin.json").write_text("{bad}")
        assert serve.read_plugin_manifest(pd) is None

    def test_nested_fields(self, tmp_path):
        pd = tmp_path / "plugin"
        cp = pd / ".claude-plugin"
        cp.mkdir(parents=True)
        manifest = {"name": "test", "skills": ["extra/"], "agents": ["agents/"]}
        (cp / "plugin.json").write_text(json.dumps(manifest))
        result = serve.read_plugin_manifest(pd)
        assert result["skills"] == ["extra/"]


# ---------------------------------------------------------------------------
# scan_commands
# ---------------------------------------------------------------------------

class TestScanCommands:
    def test_no_dir(self, tmp_claude_dir):
        plugin = tmp_claude_dir / "plugins" / "test"
        plugin.mkdir(parents=True)
        assert serve.scan_commands(plugin, skip_git=True) == []

    def test_flat_command(self, tmp_claude_dir):
        plugin = tmp_claude_dir / "plugins" / "test"
        cmd_dir = plugin / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "hello.md").write_text("---\nname: hello\ndescription: Say hello\n---\n")
        results = serve.scan_commands(plugin, skip_git=True)
        assert len(results) == 1
        assert results[0]["name"] == "hello"

    def test_nested_group_command(self, tmp_claude_dir):
        plugin = tmp_claude_dir / "plugins" / "test"
        cmd_dir = plugin / "commands" / "grp"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "sub.md").write_text("---\nname: sub\ndescription: Grouped\n---\n")
        results = serve.scan_commands(plugin, skip_git=True)
        assert len(results) == 1
        assert results[0]["name"] == "grp:sub"

    def test_readme_skipped(self, tmp_claude_dir):
        plugin = tmp_claude_dir / "plugins" / "test"
        cmd_dir = plugin / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "README.md").write_text("---\nname: readme\n---\n")
        (cmd_dir / "real.md").write_text("---\nname: real\ndescription: Real\n---\n")
        results = serve.scan_commands(plugin, skip_git=True)
        assert len(results) == 1
        assert results[0]["name"] == "real"


# ---------------------------------------------------------------------------
# scan_agents
# ---------------------------------------------------------------------------

class TestScanAgents:
    def test_no_dir(self, tmp_claude_dir):
        plugin = tmp_claude_dir / "plugins" / "test"
        plugin.mkdir(parents=True)
        assert serve.scan_agents(plugin) == []

    def test_tools_as_list(self, tmp_claude_dir):
        plugin = tmp_claude_dir / "plugins" / "test"
        agents_dir = plugin / "agents"
        agents_dir.mkdir(parents=True)
        content = '---\nname: my-agent\ndescription: An agent\ntools: ["Read", "Write"]\n---\n'
        (agents_dir / "my-agent.md").write_text(content)
        results = serve.scan_agents(plugin)
        assert len(results) == 1
        assert results[0]["tools"] == ["Read", "Write"]

    def test_tools_as_comma_string(self, tmp_claude_dir):
        plugin = tmp_claude_dir / "plugins" / "test"
        agents_dir = plugin / "agents"
        agents_dir.mkdir(parents=True)
        content = "---\nname: ag\ndescription: Agent\ntools: Read, Write, Bash\n---\n"
        (agents_dir / "ag.md").write_text(content)
        results = serve.scan_agents(plugin)
        assert results[0]["tools"] == ["Read", "Write", "Bash"]

    def test_disabled_agent(self, tmp_claude_dir):
        plugin = tmp_claude_dir / "plugins" / "test"
        agents_dir = plugin / "agents"
        agents_dir.mkdir(parents=True)
        content = "---\nname: off\ndescription: Disabled\n---\n"
        (agents_dir / "off.md.disabled").write_text(content)
        results = serve.scan_agents(plugin)
        assert len(results) == 1
        assert results[0]["enabled"] is False

    def test_manifest_paths(self, tmp_claude_dir):
        plugin = tmp_claude_dir / "plugins" / "test"
        custom_dir = plugin / "custom-agents"
        custom_dir.mkdir(parents=True)
        (custom_dir / "special.md").write_text("---\nname: special\ndescription: Custom\n---\n")
        manifest = {"agents": ["custom-agents/"]}
        results = serve.scan_agents(plugin, manifest=manifest)
        assert any(a["name"] == "special" for a in results)


# ---------------------------------------------------------------------------
# scan_hooks
# ---------------------------------------------------------------------------

class TestScanHooks:
    def test_no_file(self, tmp_claude_dir):
        plugin = tmp_claude_dir / "plugins" / "test"
        plugin.mkdir(parents=True)
        assert serve.scan_hooks(plugin) == []

    def test_valid_hooks_json(self, tmp_claude_dir):
        plugin = tmp_claude_dir / "plugins" / "test"
        hooks_dir = plugin / "hooks"
        hooks_dir.mkdir(parents=True)
        data = {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "echo hi"}]
                    }
                ]
            }
        }
        (hooks_dir / "hooks.json").write_text(json.dumps(data))
        results = serve.scan_hooks(plugin)
        assert len(results) == 1
        assert results[0]["event_type"] == "PreToolUse"
        assert results[0]["matcher"] == "Bash"

    def test_manifest_inline_dict(self, tmp_claude_dir):
        plugin = tmp_claude_dir / "plugins" / "test"
        plugin.mkdir(parents=True)
        manifest = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "*",
                        "hooks": [{"type": "command", "command": "echo done"}]
                    }
                ]
            }
        }
        results = serve.scan_hooks(plugin, manifest=manifest)
        assert len(results) == 1
        assert results[0]["event_type"] == "PostToolUse"

    def test_manifest_string_path(self, tmp_claude_dir):
        plugin = tmp_claude_dir / "plugins" / "test"
        plugin.mkdir(parents=True)
        hooks_data = {
            "SessionStart": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "echo start"}]}
            ]
        }
        (plugin / "my-hooks.json").write_text(json.dumps(hooks_data))
        manifest = {"hooks": "my-hooks.json"}
        results = serve.scan_hooks(plugin, manifest=manifest)
        assert len(results) == 1


# ---------------------------------------------------------------------------
# scan_mcp_servers
# ---------------------------------------------------------------------------

class TestScanMcpServers:
    def test_manifest_inline(self, tmp_claude_dir):
        plugin = tmp_claude_dir / "plugins" / "test"
        plugin.mkdir(parents=True)
        manifest = {
            "mcpServers": {
                "my-server": {
                    "type": "stdio",
                    "command": "node",
                    "args": ["server.js"],
                    "env": {"API_KEY": "xxx"}
                }
            }
        }
        results = serve.scan_mcp_servers(plugin, manifest=manifest)
        assert len(results) == 1
        assert results[0]["name"] == "my-server"
        assert results[0]["env_keys"] == ["API_KEY"]

    def test_manifest_string_path(self, tmp_claude_dir):
        plugin = tmp_claude_dir / "plugins" / "test"
        plugin.mkdir(parents=True)
        mcp_data = {
            "mcpServers": {
                "ext-server": {"command": "python", "args": ["srv.py"]}
            }
        }
        (plugin / "mcp.json").write_text(json.dumps(mcp_data))
        manifest = {"mcpServers": "mcp.json"}
        results = serve.scan_mcp_servers(plugin, manifest=manifest)
        assert len(results) == 1
        assert results[0]["name"] == "ext-server"

    def test_mcp_json_fallback(self, tmp_claude_dir):
        plugin = tmp_claude_dir / "plugins" / "test"
        plugin.mkdir(parents=True)
        mcp_data = {
            "mcpServers": {
                "fallback": {"command": "echo"}
            }
        }
        (plugin / ".mcp.json").write_text(json.dumps(mcp_data))
        results = serve.scan_mcp_servers(plugin)
        assert len(results) == 1
        assert results[0]["name"] == "fallback"
        assert results[0]["source"] == ".mcp.json"

    def test_env_keys_extracted(self, tmp_claude_dir):
        plugin = tmp_claude_dir / "plugins" / "test"
        plugin.mkdir(parents=True)
        manifest = {
            "mcpServers": {
                "srv": {
                    "command": "node",
                    "env": {"KEY1": "a", "KEY2": "b"}
                }
            }
        }
        results = serve.scan_mcp_servers(plugin, manifest=manifest)
        assert sorted(results[0]["env_keys"]) == ["KEY1", "KEY2"]
