"""Tests for pure functions: parse_frontmatter, _require_claude_path."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import serve


# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------

class TestParseFrontmatter:
    def test_empty_string(self):
        assert serve.parse_frontmatter("") == {}

    def test_no_frontmatter(self):
        assert serve.parse_frontmatter("Just some text\nno fences here") == {}

    def test_no_closing_fence(self):
        assert serve.parse_frontmatter("---\nname: foo\n") == {}

    def test_simple_key_value(self):
        content = "---\nname: my-skill\ndescription: A skill\n---\nbody"
        fm = serve.parse_frontmatter(content)
        assert fm["name"] == "my-skill"
        assert fm["description"] == "A skill"

    def test_double_quoted_value(self):
        content = '---\nname: "quoted-name"\n---\n'
        assert serve.parse_frontmatter(content)["name"] == "quoted-name"

    def test_single_quoted_value(self):
        content = "---\nname: 'single-quoted'\n---\n"
        assert serve.parse_frontmatter(content)["name"] == "single-quoted"

    def test_multiline_block_folded_strip(self):
        content = "---\ndescription: >-\n  line one\n  line two\n---\n"
        fm = serve.parse_frontmatter(content)
        assert fm["description"] == "line one line two"

    def test_multiline_block_folded(self):
        content = "---\ndescription: >\n  line one\n  line two\n---\n"
        fm = serve.parse_frontmatter(content)
        assert fm["description"] == "line one line two"

    def test_multiline_block_literal_strip(self):
        content = "---\ndescription: |-\n  line one\n  line two\n---\n"
        fm = serve.parse_frontmatter(content)
        assert fm["description"] == "line one line two"

    def test_multiline_block_literal(self):
        content = "---\ndescription: |\n  line one\n  line two\n---\n"
        fm = serve.parse_frontmatter(content)
        assert fm["description"] == "line one line two"

    def test_multiline_empty_indicator(self):
        content = "---\ndescription:\n  line one\n  line two\n---\n"
        fm = serve.parse_frontmatter(content)
        assert fm["description"] == "line one line two"

    def test_hyphenated_key(self):
        content = "---\ndisable-model-invocation: true\n---\n"
        fm = serve.parse_frontmatter(content)
        assert fm["disable-model-invocation"] == "true"

    def test_numeric_value_as_string(self):
        content = "---\nversion: 42\n---\n"
        fm = serve.parse_frontmatter(content)
        assert fm["version"] == "42"

    def test_key_without_value(self):
        content = "---\nempty-key:\n---\n"
        fm = serve.parse_frontmatter(content)
        assert fm["empty-key"] == ""

    def test_frontmatter_not_at_start(self):
        content = "some text\n---\nname: foo\n---\n"
        assert serve.parse_frontmatter(content) == {}

    def test_multiple_keys(self):
        content = "---\nname: a\ndescription: b\nuser-invocable: false\n---\n"
        fm = serve.parse_frontmatter(content)
        assert len(fm) == 3
        assert fm["user-invocable"] == "false"

    def test_value_with_colon(self):
        content = "---\nname: my:skill\n---\n"
        fm = serve.parse_frontmatter(content)
        assert fm["name"] == "my:skill"

    def test_tab_indented_multiline(self):
        content = "---\ndescription: >-\n\tline one\n\tline two\n---\n"
        fm = serve.parse_frontmatter(content)
        assert fm["description"] == "line one line two"


# ---------------------------------------------------------------------------
# _require_claude_path
# ---------------------------------------------------------------------------

class TestRequireClaudePath:
    def test_valid_path(self, tmp_claude_dir):
        skill = tmp_claude_dir / "skills" / "foo" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.touch()
        result = serve._require_claude_path(str(skill))
        assert result is not None
        assert result == skill.resolve()

    def test_path_outside_claude_dir(self, tmp_claude_dir):
        outside = tmp_claude_dir.parent / "outside" / "file.txt"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.touch()
        assert serve._require_claude_path(str(outside)) is None

    def test_path_traversal(self, tmp_claude_dir):
        malicious = str(tmp_claude_dir / "skills" / ".." / ".." / ".." / "etc" / "passwd")
        assert serve._require_claude_path(malicious) is None

    def test_empty_string(self, tmp_claude_dir):
        # Empty string resolves to cwd, which is not under tmp_claude_dir
        assert serve._require_claude_path("") is None

    def test_prefix_trick(self, tmp_claude_dir):
        # _require_claude_path uses str.startswith which doesn't catch this;
        # verify current behavior (accepts paths that share the prefix)
        fake = Path(str(tmp_claude_dir) + "-extension") / "evil.txt"
        fake.parent.mkdir(parents=True, exist_ok=True)
        fake.touch()
        # Known limitation: startswith match passes for prefix-extended dirs
        result = serve._require_claude_path(str(fake))
        assert result is not None  # documents actual behavior

    def test_symlink_escape(self, tmp_claude_dir):
        # Symlink inside claude dir pointing outside
        outside = tmp_claude_dir.parent / "secret.txt"
        outside.write_text("secret")
        link = tmp_claude_dir / "skills" / "link.txt"
        link.symlink_to(outside)
        result = serve._require_claude_path(str(link))
        # resolve() follows symlinks, so it should point outside
        assert result is None

    def test_deeply_nested_valid(self, tmp_claude_dir):
        deep = tmp_claude_dir / "plugins" / "cache" / "mp" / "plug" / "1.0" / "skills" / "s" / "SKILL.md"
        deep.parent.mkdir(parents=True, exist_ok=True)
        deep.touch()
        result = serve._require_claude_path(str(deep))
        assert result is not None

    def test_claude_dir_itself(self, tmp_claude_dir):
        result = serve._require_claude_path(str(tmp_claude_dir))
        assert result is not None

    def test_relative_path_not_under_claude(self, tmp_claude_dir):
        assert serve._require_claude_path("relative/path") is None

    def test_nonexistent_but_under_claude(self, tmp_claude_dir):
        p = tmp_claude_dir / "nonexistent" / "file.md"
        result = serve._require_claude_path(str(p))
        # Path doesn't need to exist, just needs to resolve under CLAUDE_DIR
        assert result is not None
