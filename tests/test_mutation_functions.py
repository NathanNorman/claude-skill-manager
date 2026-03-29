"""Tests for mutation functions that modify the filesystem."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import serve


# ---------------------------------------------------------------------------
# set_skill_enabled
# ---------------------------------------------------------------------------

class TestSetSkillEnabled:
    def test_disable_enabled(self, tmp_claude_dir, skill_factory):
        skill_factory(tmp_claude_dir / "skills" / "foo", name="foo")
        result = serve.set_skill_enabled("skills/foo/SKILL.md", False)
        assert result["ok"] is True
        assert (tmp_claude_dir / "skills" / "foo" / "SKILL.md.disabled").exists()
        assert not (tmp_claude_dir / "skills" / "foo" / "SKILL.md").exists()

    def test_enable_disabled(self, tmp_claude_dir, skill_factory):
        skill_factory(tmp_claude_dir / "skills" / "bar", name="bar", disabled=True)
        result = serve.set_skill_enabled("skills/bar/SKILL.md", True)
        assert result["ok"] is True
        assert (tmp_claude_dir / "skills" / "bar" / "SKILL.md").exists()
        assert not (tmp_claude_dir / "skills" / "bar" / "SKILL.md.disabled").exists()

    def test_already_enabled(self, tmp_claude_dir, skill_factory):
        skill_factory(tmp_claude_dir / "skills" / "ok", name="ok")
        result = serve.set_skill_enabled("skills/ok/SKILL.md", True)
        assert result["ok"] is True
        assert (tmp_claude_dir / "skills" / "ok" / "SKILL.md").exists()

    def test_already_disabled(self, tmp_claude_dir, skill_factory):
        skill_factory(tmp_claude_dir / "skills" / "off", name="off", disabled=True)
        result = serve.set_skill_enabled("skills/off/SKILL.md", False)
        assert result["ok"] is True

    def test_prefs_persisted_on_disable(self, tmp_claude_dir, skill_factory):
        skill_factory(tmp_claude_dir / "skills" / "x", name="x")
        serve.set_skill_enabled("skills/x/SKILL.md", False)
        prefs = serve.load_prefs()
        assert prefs.get("skills/x") is True

    def test_prefs_cleared_on_enable(self, tmp_claude_dir, skill_factory):
        skill_factory(tmp_claude_dir / "skills" / "y", name="y", disabled=True)
        serve.save_prefs({"skills/y": True})
        serve.set_skill_enabled("skills/y/SKILL.md", True)
        prefs = serve.load_prefs()
        assert "skills/y" not in prefs
        assert "skills/y/SKILL.md" not in prefs

    def test_returns_ok_dict(self, tmp_claude_dir, skill_factory):
        skill_factory(tmp_claude_dir / "skills" / "z", name="z")
        result = serve.set_skill_enabled("skills/z/SKILL.md", False)
        assert "ok" in result
        assert "path" in result
        assert "enabled" in result

    def test_both_key_types_cleaned(self, tmp_claude_dir, skill_factory):
        skill_factory(tmp_claude_dir / "skills" / "w", name="w", disabled=True)
        serve.save_prefs({"skills/w": True, "skills/w/SKILL.md": True})
        serve.set_skill_enabled("skills/w/SKILL.md", True)
        prefs = serve.load_prefs()
        assert "skills/w" not in prefs


# ---------------------------------------------------------------------------
# apply_prefs
# ---------------------------------------------------------------------------

class TestApplyPrefs:
    def test_empty_prefs(self, tmp_claude_dir):
        serve.save_prefs({})
        assert serve.apply_prefs() == 0

    def test_disabled_renames(self, tmp_claude_dir, skill_factory):
        skill_factory(tmp_claude_dir / "skills" / "a", name="a")
        serve.save_prefs({"skills/a": True})
        count = serve.apply_prefs()
        assert count == 1
        assert (tmp_claude_dir / "skills" / "a" / "SKILL.md.disabled").exists()

    def test_disabled_false_skipped(self, tmp_claude_dir, skill_factory):
        skill_factory(tmp_claude_dir / "skills" / "b", name="b")
        serve.save_prefs({"skills/b": False})
        count = serve.apply_prefs()
        assert count == 0
        assert (tmp_claude_dir / "skills" / "b" / "SKILL.md").exists()

    def test_already_disabled_no_double_rename(self, tmp_claude_dir, skill_factory):
        skill_factory(tmp_claude_dir / "skills" / "c", name="c", disabled=True)
        serve.save_prefs({"skills/c": True})
        count = serve.apply_prefs()
        assert count == 0  # already disabled


# ---------------------------------------------------------------------------
# toggle_component_enabled
# ---------------------------------------------------------------------------

class TestToggleComponentEnabled:
    def test_enable_disabled(self, tmp_claude_dir):
        f = tmp_claude_dir / "commands" / "test.md.disabled"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("---\nname: test\n---\n")
        result = serve.toggle_component_enabled(str(f), True)
        assert result["ok"] is True
        assert result["enabled"] is True
        assert (tmp_claude_dir / "commands" / "test.md").exists()

    def test_disable_enabled(self, tmp_claude_dir):
        f = tmp_claude_dir / "commands" / "test.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("---\nname: test\n---\n")
        result = serve.toggle_component_enabled(str(f), False)
        assert result["ok"] is True
        assert result["enabled"] is False
        assert (tmp_claude_dir / "commands" / "test.md.disabled").exists()

    def test_already_enabled(self, tmp_claude_dir):
        f = tmp_claude_dir / "commands" / "test.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("content")
        result = serve.toggle_component_enabled(str(f), True)
        assert result["ok"] is True

    def test_already_disabled(self, tmp_claude_dir):
        f = tmp_claude_dir / "commands" / "test.md.disabled"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("content")
        result = serve.toggle_component_enabled(str(f), False)
        assert result["ok"] is True

    def test_target_exists_conflict(self, tmp_claude_dir):
        f = tmp_claude_dir / "commands" / "test.md.disabled"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("disabled")
        (tmp_claude_dir / "commands" / "test.md").write_text("enabled")
        result = serve.toggle_component_enabled(str(f), True)
        assert result["ok"] is False
        assert "already exists" in result["error"]


# ---------------------------------------------------------------------------
# move_skill_to_scanned
# ---------------------------------------------------------------------------

class TestMoveSkillToScanned:
    def test_source_not_found(self, tmp_claude_dir):
        result = serve.move_skill_to_scanned("/nonexistent/SKILL.md", "test")
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_success_copies_tree(self, tmp_claude_dir, skill_factory):
        src = tmp_claude_dir / "plugins" / "cache" / "mp" / "plug" / "1.0" / "skills" / "s"
        skill_factory(src, name="s")
        (src / "resources").mkdir()
        (src / "resources" / "data.txt").write_text("data")
        result = serve.move_skill_to_scanned(str(src / "SKILL.md"), "s")
        assert result["ok"] is True
        dest = tmp_claude_dir / "skills" / "s"
        assert dest.is_dir()
        assert (dest / "SKILL.md").exists()
        assert (dest / "resources" / "data.txt").exists()

    def test_dest_exists_error(self, tmp_claude_dir, skill_factory):
        src = tmp_claude_dir / "plugins" / "test" / "skills" / "dup"
        skill_factory(src, name="dup")
        (tmp_claude_dir / "skills" / "dup").mkdir(parents=True)
        result = serve.move_skill_to_scanned(str(src / "SKILL.md"), "dup")
        assert result["ok"] is False
        assert "already exists" in result["error"]

    def test_nested_dir_copied(self, tmp_claude_dir, skill_factory):
        src = tmp_claude_dir / "plugins" / "test" / "skills" / "deep"
        skill_factory(src, name="deep")
        nested = src / "a" / "b" / "c"
        nested.mkdir(parents=True)
        (nested / "file.txt").write_text("nested")
        result = serve.move_skill_to_scanned(str(src / "SKILL.md"), "deep")
        assert result["ok"] is True
        assert (tmp_claude_dir / "skills" / "deep" / "a" / "b" / "c" / "file.txt").exists()


# ---------------------------------------------------------------------------
# toggle_model_invocation
# ---------------------------------------------------------------------------

class TestToggleModelInvocation:
    def test_file_not_found(self, tmp_claude_dir):
        result = serve.toggle_model_invocation("/nonexistent/SKILL.md", True)
        assert result["ok"] is False

    def test_add_to_existing_fm(self, tmp_claude_dir):
        f = tmp_claude_dir / "test.md"
        f.write_text("---\nname: foo\n---\nbody")
        result = serve.toggle_model_invocation(str(f), True)
        assert result["ok"] is True
        content = f.read_text()
        assert "disable-model-invocation: true" in content

    def test_update_existing_key(self, tmp_claude_dir):
        f = tmp_claude_dir / "test.md"
        f.write_text("---\nname: foo\ndisable-model-invocation: false\n---\nbody")
        result = serve.toggle_model_invocation(str(f), True)
        assert result["ok"] is True
        content = f.read_text()
        assert "disable-model-invocation: true" in content
        assert "disable-model-invocation: false" not in content

    def test_remove_key(self, tmp_claude_dir):
        f = tmp_claude_dir / "test.md"
        f.write_text("---\nname: foo\ndisable-model-invocation: true\n---\nbody")
        result = serve.toggle_model_invocation(str(f), False)
        assert result["ok"] is True
        content = f.read_text()
        assert "disable-model-invocation" not in content

    def test_no_fm_disable(self, tmp_claude_dir):
        f = tmp_claude_dir / "test.md"
        f.write_text("Just body text, no frontmatter")
        result = serve.toggle_model_invocation(str(f), True)
        assert result["ok"] is True
        content = f.read_text()
        assert content.startswith("---\ndisable-model-invocation: true\n---\n")

    def test_no_fm_enable(self, tmp_claude_dir):
        f = tmp_claude_dir / "test.md"
        f.write_text("Just body text")
        result = serve.toggle_model_invocation(str(f), False)
        assert result["ok"] is True
        # No change needed, no frontmatter to remove from
        assert result["disable_model_invocation"] is False
