#!/usr/bin/env python3
"""
Claude Code Skill Manager — Local HTTP server.
Serves the UI and provides an API to read skill data from the filesystem.

Discovery logic matches how Claude Code actually loads skills:
  1. plugin.json has non-empty skills[] → only those paths loaded
  2. plugin.json exists but skills field absent → auto-discover skills/ subdir
  3. plugin.json has skills: [] (empty) → nothing loaded
  4. No plugin.json → flat layout (top-level subdirs with SKILL.md)
  Commands (commands/*.md) are also discovered separately.
"""

import json
import os
import re
import subprocess
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

CLAUDE_DIR = Path.home() / ".claude"
PLUGINS_DIR = CLAUDE_DIR / "plugins"
MARKETPLACES_DIR = PLUGINS_DIR / "marketplaces"
CACHE_DIR = PLUGINS_DIR / "cache"
SKILLS_DIR = CLAUDE_DIR / "skills"
AGENTS_DIR = CLAUDE_DIR / "agents"
COMMANDS_DIR = CLAUDE_DIR / "commands"
INSTALLED_PLUGINS_FILE = PLUGINS_DIR / "installed_plugins.json"
KNOWN_MARKETPLACES_FILE = PLUGINS_DIR / "known_marketplaces.json"
PREFS_FILE = Path(__file__).parent / "preferences.json"
SETTINGS_FILE = CLAUDE_DIR / "settings.json"
PORT = 8421


def get_char_budget() -> tuple[int, str]:
    """Resolve the skill description character budget.

    Priority:
    1. OS environment variable SLASH_COMMAND_TOOL_CHAR_BUDGET
    2. settings.json env section (where Claude Code reads it)
    3. Default: 15000

    Returns (budget, source_label).
    """
    # 1. OS env
    env_val = os.environ.get("SLASH_COMMAND_TOOL_CHAR_BUDGET")
    if env_val and env_val.isdigit() and int(env_val) > 0:
        return int(env_val), "env"

    # 2. settings.json env section
    try:
        with open(SETTINGS_FILE) as f:
            settings = json.load(f)
        val = settings.get("env", {}).get("SLASH_COMMAND_TOOL_CHAR_BUDGET")
        if val and str(val).isdigit() and int(val) > 0:
            return int(val), "settings.json"
    except Exception:
        pass

    # 3. Default
    return 15000, "default"


def parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from SKILL.md or command .md, handling multi-line values."""
    m = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not m:
        return {}
    fm = {}
    lines = m.group(1).splitlines()
    i = 0
    while i < len(lines):
        kv = re.match(r"^(\w[\w-]*):\s*(.*)", lines[i])
        if kv:
            key = kv.group(1).strip()
            val = kv.group(2).strip().strip("\"'")
            # Handle YAML block scalars: >- , > , |- , |
            if val in (">-", ">", "|-", "|", ""):
                # Collect indented continuation lines
                parts = []
                i += 1
                while i < len(lines) and (lines[i].startswith("  ") or lines[i].startswith("\t")):
                    parts.append(lines[i].strip())
                    i += 1
                val = " ".join(parts)
                fm[key] = val
                continue
            fm[key] = val
        i += 1
    return fm


def get_installed_plugins() -> dict:
    """Read installed_plugins.json and return {realpath: {id, version}}."""
    if not INSTALLED_PLUGINS_FILE.exists():
        return {}
    with open(INSTALLED_PLUGINS_FILE) as f:
        data = json.load(f)
    paths = {}
    for plugin_id, entries in data.get("plugins", {}).items():
        if isinstance(entries, list):
            for entry in entries:
                p = entry.get("installPath", "")
                if p:
                    paths[os.path.realpath(p)] = {
                        "id": plugin_id,
                        "version": entry.get("version", "?"),
                        "installPath": p,
                    }
    return paths


def load_prefs() -> dict:
    """Load skill disable preferences. Returns {skill_path: bool} where True = disabled."""
    if not PREFS_FILE.exists():
        return {}
    with open(PREFS_FILE) as f:
        return json.load(f)


def save_prefs(prefs: dict):
    """Save skill disable preferences."""
    with open(PREFS_FILE, "w") as f:
        json.dump(prefs, f, indent=2)


def set_skill_enabled(rel_path: str, enabled: bool) -> dict:
    """Enable or disable a skill by renaming SKILL.md <-> SKILL.md.disabled."""
    skill_file = CLAUDE_DIR / rel_path
    disabled_file = skill_file.parent / "SKILL.md.disabled"

    prefs = load_prefs()

    if enabled:
        # Re-enable: rename .disabled back to SKILL.md
        if disabled_file.exists() and not skill_file.exists():
            disabled_file.rename(skill_file)
        prefs.pop(rel_path, None)
        # Also remove by dir key
        dir_key = str(skill_file.parent.relative_to(CLAUDE_DIR))
        prefs.pop(dir_key, None)
    else:
        # Disable: rename SKILL.md to .disabled
        if skill_file.exists():
            skill_file.rename(disabled_file)
        # Store by dir so cron can re-apply after updates
        dir_key = str(skill_file.parent.relative_to(CLAUDE_DIR))
        prefs[dir_key] = True

    save_prefs(prefs)
    return {"ok": True, "path": rel_path, "enabled": enabled}


def apply_prefs():
    """Re-apply disable preferences (called by cron). Disables any SKILL.md
    in directories listed in preferences.json."""
    prefs = load_prefs()
    applied = 0
    for dir_key, disabled in prefs.items():
        if not disabled:
            continue
        skill_file = CLAUDE_DIR / dir_key / "SKILL.md"
        disabled_file = CLAUDE_DIR / dir_key / "SKILL.md.disabled"
        if skill_file.exists() and not disabled_file.exists():
            skill_file.rename(disabled_file)
            applied += 1
    return applied


# ---------------------------------------------------------------------------
# Git dirty detection (per-file)
# ---------------------------------------------------------------------------

# Cache: git repo root -> set of dirty paths (relative to repo root)
_git_dirty_cache: dict[str, set[str]] = {}
# Cache: directory path -> repo root (avoids repeated rev-parse subprocess calls)
_repo_root_cache: dict[str, str | None] = {}


def _find_repo_root(dir_path: str) -> str | None:
    """Find the git repo root for a directory, with caching.

    If dir_path is inside a known repo root, returns that root immediately
    without spawning a subprocess.
    """
    if dir_path in _repo_root_cache:
        return _repo_root_cache[dir_path]
    # Fast path: check if dir_path is inside any already-known repo root
    for cached_root in _git_dirty_cache:
        if dir_path.startswith(cached_root + os.sep) or dir_path == cached_root:
            _repo_root_cache[dir_path] = cached_root
            return cached_root
    try:
        result = subprocess.run(
            ["git", "-C", dir_path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        root = result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        root = None
    _repo_root_cache[dir_path] = root
    return root


def _is_git_dirty(file_path: Path) -> bool:
    """Check if a file has uncommitted changes (modified, staged, or untracked) in its git repo."""
    abs_str = str(file_path.resolve())
    # Find the git repo root for this file (cached per directory)
    repo_root = _find_repo_root(str(file_path.parent))
    if repo_root is None:
        return False

    # Populate cache for this repo if not already done
    if repo_root not in _git_dirty_cache:
        try:
            result = subprocess.run(
                ["git", "-C", repo_root, "status", "--porcelain"],
                capture_output=True, text=True, timeout=10,
            )
            dirty_paths: set[str] = set()
            if result.returncode == 0:
                for line in result.stdout.strip().splitlines():
                    if len(line) > 3:
                        # porcelain format: XY filename (or XY orig -> renamed)
                        path_part = line[3:].split(" -> ")[-1].strip()
                        dirty_paths.add(os.path.join(repo_root, path_part))
            _git_dirty_cache[repo_root] = dirty_paths
        except Exception:
            _git_dirty_cache[repo_root] = set()

    # Check if this file or its parent directory has dirty files
    dirty = _git_dirty_cache[repo_root]
    if abs_str in dirty:
        return True
    # Also check if any file in the skill's directory is dirty
    skill_dir = str(file_path.parent.resolve())
    return any(d.startswith(skill_dir + os.sep) or d == skill_dir for d in dirty)


# ---------------------------------------------------------------------------
# Skill metadata extraction
# ---------------------------------------------------------------------------

def _build_skill_entry(skill_md: Path, source_type: str = "skill", skip_git: bool = False) -> dict | None:
    """Read a SKILL.md (or command .md) and return a metadata dict."""
    try:
        content = skill_md.read_text(encoding="utf-8")
    except Exception:
        return None

    fm = parse_frontmatter(content)
    name = fm.get("name", skill_md.parent.name if source_type == "skill" else skill_md.stem)
    description = fm.get("description", "")
    is_disabled = skill_md.name == "SKILL.md.disabled" or skill_md.name.endswith(".md.disabled")

    # For disabled skills, report the path as if it were SKILL.md (for toggle API)
    if source_type == "skill":
        active_path = str((skill_md.parent / "SKILL.md").relative_to(CLAUDE_DIR))
    else:
        active_path = str(skill_md.relative_to(CLAUDE_DIR))

    rel = str(skill_md.relative_to(CLAUDE_DIR))

    # File modification time (for staleness detection)
    try:
        mtime = skill_md.stat().st_mtime
        age_days = int((time.time() - mtime) / 86400)
    except Exception:
        mtime = 0
        age_days = -1

    # Check if this skill also has a copy in ~/.claude/skills/ (subagent-accessible)
    has_personal_copy = False
    if source_type == "skill" and not str(skill_md).startswith(str(SKILLS_DIR)):
        personal_path = SKILLS_DIR / name / "SKILL.md"
        has_personal_copy = personal_path.exists()

    # --- Validation issues ---
    issues = []
    fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not fm_match:
        issues.append("no_frontmatter")
    else:
        if "name:" not in fm_match.group(1):
            issues.append("no_name")
        if "description:" not in fm_match.group(1):
            issues.append("no_description")

    # --- Description char count (what Claude Code injects into context) ---
    # If description frontmatter exists, that's what's injected.
    # Otherwise Claude Code uses the first paragraph of the body.
    if description:
        injected_text = description
    elif fm_match:
        body = content[fm_match.end():].strip()
        injected_text = body.split("\n\n")[0].strip() if body else ""
    else:
        injected_text = content.split("\n\n")[0].strip() if content else ""

    desc_chars = len(injected_text)

    return {
        "name": name,
        "description": description,
        "path": active_path,
        "actual_path": rel,
        "abs_path": str(skill_md),
        "dir": str(skill_md.parent.relative_to(CLAUDE_DIR)),
        "user_invocable": fm.get("user-invocable", "true").lower() != "false",
        "disable_model_invocation": fm.get("disable-model-invocation", "false").lower() == "true",
        "enabled": not is_disabled,
        "source_type": source_type,
        "age_days": age_days,
        "has_personal_copy": has_personal_copy,
        "validation_issues": issues,
        "desc_chars": desc_chars,
        "frontmatter": fm,
        "git_dirty": False if skip_git else _is_git_dirty(skill_md),
    }


# ---------------------------------------------------------------------------
# Plugin manifest reading
# ---------------------------------------------------------------------------

def read_plugin_manifest(plugin_dir: Path) -> dict | None:
    """Read .claude-plugin/plugin.json if it exists, return parsed dict or None."""
    manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
    if not manifest_path.exists():
        return None
    try:
        with open(manifest_path) as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Discovery helpers — match Claude Code's actual behavior
# ---------------------------------------------------------------------------

def scan_supplemental_skills(plugin_dir: Path, skills_field: list | str, skip_git: bool = False) -> list[dict]:
    """Scan additional skill paths from the skills field (supplements default skills/ dir).
    The skills field can be a string (single path) or array of paths."""
    if isinstance(skills_field, str):
        skills_field = [skills_field]
    results = []
    seen = set()
    for skill_path in skills_field:
        resolved = (plugin_dir / skill_path).resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_dir():
            # Could be a skill dir (contains SKILL.md) or a scan dir (contains skill subdirs)
            for pattern in ("SKILL.md", "SKILL.md.disabled"):
                candidate = resolved / pattern
                if candidate.exists():
                    entry = _build_skill_entry(candidate, "skill", skip_git=skip_git)
                    if entry:
                        results.append(entry)
                    break
            else:
                # It's a directory to scan for skill subdirs
                for subdir in sorted(resolved.iterdir()):
                    if not subdir.is_dir() or subdir.name.startswith("."):
                        continue
                    for pattern in ("SKILL.md", "SKILL.md.disabled"):
                        candidate = subdir / pattern
                        if candidate.exists():
                            entry = _build_skill_entry(candidate, "skill", skip_git=skip_git)
                            if entry:
                                results.append(entry)
                            break
    return results


def scan_skills_subdir(plugin_dir: Path, skip_git: bool = False) -> list[dict]:
    """Rule 2: no skills field in manifest — auto-discover skills/*/SKILL.md (one level deep)."""
    results = []
    skills_dir = plugin_dir / "skills"
    if not skills_dir.is_dir():
        return results
    for subdir in sorted(skills_dir.iterdir()):
        if not subdir.is_dir() or subdir.name.startswith("."):
            continue
        for pattern in ("SKILL.md", "SKILL.md.disabled"):
            candidate = subdir / pattern
            if candidate.exists():
                entry = _build_skill_entry(candidate, "skill", skip_git=skip_git)
                if entry:
                    results.append(entry)
                break
    return results


def scan_flat_layout(plugin_dir: Path, skip_git: bool = False) -> list[dict]:
    """Rule 4: no plugin.json — scan default locations.

    Claude Code scans `skills/` first. If that doesn't exist, it looks for a
    subdirectory matching the plugin name (e.g., document-skills/document-skills/)
    and treats that as the skills container. This avoids false positives from
    sibling directories like algorithmic-art/ that aren't actually loaded.
    """
    results = []
    if not plugin_dir.is_dir():
        return results

    # Try skills/ first (standard default)
    skills_dir = plugin_dir / "skills"
    if skills_dir.is_dir():
        for subdir in sorted(skills_dir.iterdir()):
            if not subdir.is_dir() or subdir.name.startswith("."):
                continue
            for pattern in ("SKILL.md", "SKILL.md.disabled"):
                candidate = subdir / pattern
                if candidate.exists():
                    entry = _build_skill_entry(candidate, "skill", skip_git=skip_git)
                    if entry:
                        results.append(entry)
                    break
        return results

    # Try a subdirectory matching the plugin name
    plugin_name_dir = None
    for subdir in plugin_dir.iterdir():
        if subdir.is_dir() and not subdir.name.startswith("."):
            has_skill_subdirs = any(
                (child / "SKILL.md").exists() or (child / "SKILL.md.disabled").exists()
                for child in subdir.iterdir()
                if child.is_dir() and not child.name.startswith(".")
            )
            if has_skill_subdirs:
                plugin_name_dir = subdir
                break

    if plugin_name_dir:
        for subdir in sorted(plugin_name_dir.iterdir()):
            if not subdir.is_dir() or subdir.name.startswith("."):
                continue
            for pattern in ("SKILL.md", "SKILL.md.disabled"):
                candidate = subdir / pattern
                if candidate.exists():
                    entry = _build_skill_entry(candidate, "skill", skip_git=skip_git)
                    if entry:
                        results.append(entry)
                    break
        return results

    return results


def scan_commands(plugin_dir: Path, skip_git: bool = False) -> list[dict]:
    """Scan commands/*.md and commands/**/*.md for command files."""
    results = []
    commands_dir = plugin_dir / "commands"
    if not commands_dir.is_dir():
        return results
    for md_file in sorted(list(commands_dir.rglob("*.md")) + list(commands_dir.rglob("*.md.disabled"))):
        if md_file.name.startswith(".") or md_file.stem.upper() == "README":
            continue
        entry = _build_skill_entry(md_file, "command", skip_git=skip_git)
        if entry:
            # For commands in subdirs, prefix the name with the subdir
            rel = md_file.relative_to(commands_dir)
            if len(rel.parts) > 1:
                entry["name"] = f"{rel.parts[0]}:{entry['name']}"
            results.append(entry)
    return results


def scan_agents(plugin_dir: Path, manifest: dict | None = None) -> list[dict]:
    """Scan agents/*.md files and custom paths from plugin.json."""
    results = []
    seen = set()

    def _scan_agent_file(md_file: Path):
        if md_file in seen or not md_file.is_file():
            return
        if md_file.stem.upper() == "README":
            return
        seen.add(md_file)
        try:
            content = md_file.read_text(encoding="utf-8")
        except Exception:
            return
        fm = parse_frontmatter(content)
        is_disabled = md_file.name.endswith(".md.disabled")
        name = fm.get("name", md_file.stem.replace(".md", "") if is_disabled else md_file.stem)
        # Parse tools field (can be comma-separated string or JSON array-like)
        tools_raw = fm.get("tools", "")
        if tools_raw.startswith("["):
            try:
                tools = json.loads(tools_raw.replace("'", '"'))
            except Exception:
                tools = [t.strip().strip('"') for t in tools_raw.strip("[]").split(",") if t.strip()]
        elif tools_raw:
            tools = [t.strip() for t in tools_raw.split(",") if t.strip()]
        else:
            tools = []
        results.append({
            "name": name,
            "description": fm.get("description", ""),
            "model": fm.get("model", ""),
            "color": fm.get("color", ""),
            "tools": tools,
            "tools_count": len(tools),
            "skills": fm.get("skills", ""),
            "abs_path": str(md_file),
            "enabled": not is_disabled,
            "frontmatter": fm,
        })

    def _scan_agent_dir(d: Path):
        if not d.is_dir():
            return
        for md in sorted(list(d.glob("*.md")) + list(d.glob("*.md.disabled"))):
            if not md.name.startswith("."):
                _scan_agent_file(md)
        # Also scan subdirectories (e.g., agents/review/, agents/research/)
        for sub in sorted(d.iterdir()):
            if sub.is_dir() and not sub.name.startswith("."):
                for md in sorted(list(sub.glob("*.md")) + list(sub.glob("*.md.disabled"))):
                    if not md.name.startswith("."):
                        _scan_agent_file(md)

    # Default agents/ dir
    _scan_agent_dir(plugin_dir / "agents")

    # Custom paths from manifest
    if manifest:
        agents_field = manifest.get("agents")
        if agents_field:
            if isinstance(agents_field, str):
                agents_field = [agents_field]
            for p in agents_field:
                resolved = (plugin_dir / p).resolve()
                if resolved.is_file() and resolved.suffix == ".md":
                    _scan_agent_file(resolved)
                elif resolved.is_dir():
                    _scan_agent_dir(resolved)

    return results


def scan_hooks(plugin_dir: Path, manifest: dict | None = None) -> list[dict]:
    """Scan hooks/hooks.json and custom paths from plugin.json."""
    results = []

    def _parse_hooks_data(data: dict):
        hooks_obj = data.get("hooks", data)
        for event_type, matchers in hooks_obj.items():
            if not isinstance(matchers, list):
                continue
            for matcher_block in matchers:
                matcher = matcher_block.get("matcher", "*")
                for hook_entry in matcher_block.get("hooks", []):
                    entry_type = hook_entry.get("type", "unknown")
                    results.append({
                        "event_type": event_type,
                        "matcher": matcher if matcher else "*",
                        "entry_type": entry_type,
                        "command": hook_entry.get("command", ""),
                        "prompt": hook_entry.get("prompt", ""),
                        "agent": hook_entry.get("agent", ""),
                        "timeout": hook_entry.get("timeout"),
                    })

    def _read_hooks_file(path: Path):
        if not path.is_file():
            return
        try:
            with open(path) as f:
                data = json.load(f)
            _parse_hooks_data(data)
        except Exception:
            pass

    # Default hooks/hooks.json
    _read_hooks_file(plugin_dir / "hooks" / "hooks.json")

    # Custom paths from manifest
    if manifest:
        hooks_field = manifest.get("hooks")
        if hooks_field:
            if isinstance(hooks_field, dict):
                _parse_hooks_data(hooks_field)
            elif isinstance(hooks_field, str):
                _read_hooks_file((plugin_dir / hooks_field).resolve())
            elif isinstance(hooks_field, list):
                for p in hooks_field:
                    _read_hooks_file((plugin_dir / p).resolve())

    return results


def scan_mcp_servers(plugin_dir: Path, manifest: dict | None = None) -> list[dict]:
    """Read mcpServers from plugin.json or fallback .mcp.json."""
    results = []

    def _parse_servers(data: dict, source_label: str):
        for name, config in data.items():
            if not isinstance(config, dict):
                continue
            env_keys = list(config.get("env", {}).keys()) if isinstance(config.get("env"), dict) else []
            results.append({
                "name": name,
                "type": config.get("type", "stdio"),
                "command": config.get("command", ""),
                "args": config.get("args", []),
                "url": config.get("url", ""),
                "env_keys": env_keys,
                "source": source_label,
            })

    if manifest:
        mcp_field = manifest.get("mcpServers")
        if isinstance(mcp_field, dict):
            _parse_servers(mcp_field, "manifest")
            return results
        elif isinstance(mcp_field, str):
            mcp_path = (plugin_dir / mcp_field).resolve()
            if mcp_path.is_file():
                try:
                    with open(mcp_path) as f:
                        data = json.load(f)
                    _parse_servers(data.get("mcpServers", data), mcp_path.name)
                except Exception:
                    pass
                return results

    # Fallback: .mcp.json
    mcp_fallback = plugin_dir / ".mcp.json"
    if mcp_fallback.is_file():
        try:
            with open(mcp_fallback) as f:
                data = json.load(f)
            _parse_servers(data.get("mcpServers", data), ".mcp.json")
        except Exception:
            pass

    return results


def _scan_all_skills_recursive(plugin_dir: Path, skip_git: bool = False) -> list[dict]:
    """Find ALL SKILL.md files anywhere under a plugin dir (for showing unloaded skills)."""
    results = []
    if not plugin_dir.is_dir():
        return results
    for pattern in ("SKILL.md", "SKILL.md.disabled"):
        for skill_md in sorted(plugin_dir.rglob(pattern)):
            entry = _build_skill_entry(skill_md, "skill", skip_git=skip_git)
            if entry:
                results.append(entry)
    return results


def discover_plugin_components(plugin_dir: Path, skip_git: bool = False) -> dict:
    """Discover all plugin components the way Claude Code does — respecting plugin.json.

    Returns a dict with keys: skills, commands, agents, hooks, mcp_servers.
    Skills discovery follows Claude Code's official behavior:
    - The default skills/ directory is ALWAYS scanned, regardless of plugin.json.
    - The `skills` field in plugin.json adds SUPPLEMENTAL paths on top of that.
    - commands/ is always scanned separately.
    - If no plugin.json exists, flat layout discovery applies.
    """
    manifest = read_plugin_manifest(plugin_dir)

    if manifest is not None:
        # Always scan the default skills/ subdir
        skills = scan_skills_subdir(plugin_dir, skip_git=skip_git)
        seen_paths = {s["abs_path"] for s in skills}

        # Supplement with any additional paths from the skills field
        skills_field = manifest.get("skills")
        if skills_field and (isinstance(skills_field, str) or len(skills_field) > 0):
            for extra in scan_supplemental_skills(plugin_dir, skills_field, skip_git=skip_git):
                if extra["abs_path"] not in seen_paths:
                    skills.append(extra)
                    seen_paths.add(extra["abs_path"])
    else:
        # No manifest → flat layout (document-skills style)
        skills = scan_flat_layout(plugin_dir, skip_git=skip_git)

    # Mark loaded skills
    loaded_paths = {s["abs_path"] for s in skills}
    for s in skills:
        s["loaded"] = True

    # Find unloaded skills (exist on disk but not discovered)
    all_skills = _scan_all_skills_recursive(plugin_dir, skip_git=skip_git)
    for s in all_skills:
        if s["abs_path"] not in loaded_paths:
            s["loaded"] = False
            skills.append(s)

    # Always also discover commands
    commands = scan_commands(plugin_dir, skip_git=skip_git)
    for c in commands:
        c["loaded"] = True

    # Discover agents, hooks, MCP servers
    agents = scan_agents(plugin_dir, manifest)
    hooks = scan_hooks(plugin_dir, manifest)
    mcp_servers = scan_mcp_servers(plugin_dir, manifest)

    # Combined skills list (for backward compat — skills + commands)
    all_skill_entries = skills + commands

    return {
        "skills": all_skill_entries,
        "agents": agents,
        "hooks": hooks,
        "mcp_servers": mcp_servers,
        "component_counts": {
            "skills": len(skills),
            "commands": len(commands),
            "agents": len(agents),
            "hooks": len(hooks),
            "mcp_servers": len(mcp_servers),
        },
    }


def scan_local_skills(base_dir: Path) -> list[dict]:
    """Scan ~/.claude/skills/ — recursive, matching current behavior."""
    skills = []
    if not base_dir.is_dir():
        return skills
    for pattern in ("SKILL.md", "SKILL.md.disabled"):
        for skill_md in sorted(base_dir.rglob(pattern)):
            entry = _build_skill_entry(skill_md, "skill")
            if entry:
                skills.append(entry)
    return skills


def scan_local_agents() -> list[dict]:
    """Scan ~/.claude/agents/ for user-level agent definitions."""
    return scan_agents(CLAUDE_DIR)


def scan_local_commands() -> list[dict]:
    """Scan ~/.claude/commands/ for user-level command files."""
    return scan_commands(CLAUDE_DIR)


def scan_settings_hooks() -> list[dict]:
    """Read hooks from ~/.claude/settings.json (user-level hooks, not plugin hooks)."""
    results = []
    if not SETTINGS_FILE.exists():
        return results
    try:
        with open(SETTINGS_FILE) as f:
            settings = json.load(f)
    except Exception:
        return results

    hooks_obj = settings.get("hooks", {})
    for event_type, matchers in hooks_obj.items():
        if not isinstance(matchers, list):
            continue
        for matcher_block in matchers:
            matcher = matcher_block.get("matcher", "*")
            for hook_entry in matcher_block.get("hooks", []):
                entry_type = hook_entry.get("type", "unknown")
                command = hook_entry.get("command", "")
                # Derive a short name from the command
                if command:
                    # Extract script filename from the command
                    parts = command.strip().split()
                    # Find the last part that looks like a file path
                    script_name = ""
                    for p in reversed(parts):
                        if "/" in p and not p.startswith("-"):
                            script_name = Path(p).name
                            break
                    if not script_name:
                        script_name = parts[-1] if parts else command[:40]
                else:
                    script_name = ""
                results.append({
                    "event_type": event_type,
                    "matcher": matcher if matcher else "*",
                    "entry_type": entry_type,
                    "command": command,
                    "prompt": hook_entry.get("prompt", ""),
                    "agent": hook_entry.get("agent", ""),
                    "timeout": hook_entry.get("timeout"),
                    "script_name": script_name,
                })
    return results


# ---------------------------------------------------------------------------
def _marketplace_repo_url(mp_name: str) -> str | None:
    """Get the GitHub repo URL for a marketplace by checking its git remote."""
    mp_path = MARKETPLACES_DIR / mp_name
    if not mp_path.is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(mp_path), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            # Normalize git@github.com:org/repo.git to https://github.com/org/repo
            if url.startswith("git@"):
                url = url.replace(":", "/", 1).replace("git@", "https://", 1)
            if url.endswith(".git"):
                url = url[:-4]
            return url
    except Exception:
        pass
    return None


# Cache marketplace repo URLs (computed once per request)
_mp_repo_cache: dict[str, str | None] = {}


def _get_mp_repo_url(mp_name: str) -> str | None:
    if mp_name not in _mp_repo_cache:
        _mp_repo_cache[mp_name] = _marketplace_repo_url(mp_name)
    return _mp_repo_cache.get(mp_name)


# Main data assembly
# ---------------------------------------------------------------------------

def get_all_data() -> dict:
    """Build the complete data structure for the UI.

    Two top-level groups:
      installed — skills loaded at runtime (cache + local + direct plugins)
      available — marketplace plugins not yet installed (browse/install only)
    """
    _mp_repo_cache.clear()
    _git_dirty_cache.clear()
    _repo_root_cache.clear()
    installed_map = get_installed_plugins()

    # --- Scan marketplace for available/installed plugin metadata ---
    mp_plugins = {}  # plugin_id -> {name, marketplace, installed, skills, plugin_id}
    if MARKETPLACES_DIR.is_dir():
        for mp_dir in sorted(MARKETPLACES_DIR.iterdir()):
            if not mp_dir.is_dir() or mp_dir.name.startswith("."):
                continue
            plugins_subdir = mp_dir / "plugins"
            search_dir = plugins_subdir if plugins_subdir.is_dir() else mp_dir
            for plugin_dir in sorted(search_dir.iterdir()):
                if not plugin_dir.is_dir() or plugin_dir.name.startswith("."):
                    continue
                components = discover_plugin_components(plugin_dir, skip_git=True)
                skills = components["skills"]
                if not skills:
                    continue
                plugin_id = f"{plugin_dir.name}@{mp_dir.name}"
                is_installed = any(
                    info["id"] == plugin_id or info["id"].startswith(plugin_dir.name + "@")
                    for info in installed_map.values()
                )
                mp_plugins[plugin_id] = {
                    "name": plugin_dir.name,
                    "marketplace": mp_dir.name,
                    "plugin_id": plugin_id,
                    "installed": is_installed,
                    "skills": skills,
                    "skill_count": len(skills),
                    "source": "marketplace",
                    "repo_url": _get_mp_repo_url(mp_dir.name),
                    "agents": components["agents"],
                    "hooks": components["hooks"],
                    "mcp_servers": components["mcp_servers"],
                    "component_counts": components["component_counts"],
                }

    # --- Installed: cache (referenced only) ---
    installed_groups = []
    missing_plugins = []

    if CACHE_DIR.is_dir():
        for mp_dir in sorted(CACHE_DIR.iterdir()):
            if not mp_dir.is_dir() or mp_dir.name.startswith("."):
                continue
            for plugin_dir in sorted(mp_dir.iterdir()):
                if not plugin_dir.is_dir():
                    continue
                for version_dir in sorted(plugin_dir.iterdir()):
                    if not version_dir.is_dir():
                        continue
                    real = os.path.realpath(str(version_dir))
                    info = installed_map.get(real)
                    if info is None:
                        continue  # stale — skip
                    components = discover_plugin_components(version_dir)
                    skills = components["skills"]
                    if skills:
                        installed_groups.append({
                            "name": plugin_dir.name,
                            "plugin_id": info["id"],
                            "version": version_dir.name,
                            "marketplace": mp_dir.name,
                            "skills": skills,
                            "skill_count": len(skills),
                            "source": "plugin",
                            "install_path": str(version_dir),
                            "repo_url": _get_mp_repo_url(mp_dir.name),
                            "agents": components["agents"],
                            "hooks": components["hooks"],
                            "mcp_servers": components["mcp_servers"],
                            "component_counts": components["component_counts"],
                        })
                    elif not version_dir.is_dir():
                        # Plugin referenced but dir missing
                        missing_plugins.append({
                            "name": plugin_dir.name,
                            "plugin_id": info["id"],
                            "installPath": info.get("installPath", ""),
                        })

    # Check for plugins whose install paths don't exist at all
    for real_path, info in installed_map.items():
        if not os.path.isdir(real_path):
            # Check if we already reported this plugin
            already = any(m["plugin_id"] == info["id"] for m in missing_plugins)
            if not already:
                name = info["id"].split("@")[0] if "@" in info["id"] else info["id"]
                missing_plugins.append({
                    "name": name,
                    "plugin_id": info["id"],
                    "installPath": info.get("installPath", ""),
                })

    # --- Installed: direct plugins ---
    if PLUGINS_DIR.is_dir():
        skip = {"cache", "marketplaces", "_archived"}
        for item in sorted(PLUGINS_DIR.iterdir()):
            if not item.is_dir() or item.name in skip or item.name.startswith("."):
                continue
            components = discover_plugin_components(item)
            skills = components["skills"]
            if skills:
                installed_groups.append({
                    "name": item.name,
                    "plugin_id": None,
                    "version": None,
                    "marketplace": None,
                    "skills": skills,
                    "skill_count": len(skills),
                    "source": "local-plugin",
                    "install_path": str(item),
                    "repo_url": None,
                    "agents": components["agents"],
                    "hooks": components["hooks"],
                    "mcp_servers": components["mcp_servers"],
                    "component_counts": components["component_counts"],
                })

    # --- Installed: local skills + agents + commands + hooks (as a pseudo-group) ---
    local_skills = scan_local_skills(SKILLS_DIR)
    local_agents = scan_local_agents()
    local_commands = scan_local_commands()
    local_hooks = scan_settings_hooks()

    # Local commands are skills with source_type="command"
    for c in local_commands:
        c["loaded"] = True

    if local_skills or local_agents or local_commands or local_hooks:
        installed_groups.insert(0, {
            "name": "Local",
            "plugin_id": None,
            "version": None,
            "marketplace": None,
            "skills": local_skills + local_commands,
            "skill_count": len(local_skills),
            "source": "local",
            "install_path": str(CLAUDE_DIR),
            "repo_url": None,
            "agents": local_agents,
            "hooks": local_hooks,
            "mcp_servers": [],
            "component_counts": {
                "skills": len(local_skills),
                "commands": len(local_commands),
                "agents": len(local_agents),
                "hooks": len(local_hooks),
                "mcp_servers": 0,
            },
        })

    # --- Available: marketplace plugins NOT installed ---
    available = [p for p in mp_plugins.values() if not p["installed"]]

    # --- Version freshness: compare installed vs marketplace ---
    # Build marketplace version map from manifest files
    mp_latest_versions: dict[str, str] = {}  # plugin_id -> latest version
    if MARKETPLACES_DIR.is_dir():
        for mp_dir in sorted(MARKETPLACES_DIR.iterdir()):
            if not mp_dir.is_dir() or mp_dir.name.startswith("."):
                continue
            plugins_subdir = mp_dir / "plugins"
            search_dir = plugins_subdir if plugins_subdir.is_dir() else mp_dir
            for plugin_dir in sorted(search_dir.iterdir()):
                if not plugin_dir.is_dir() or plugin_dir.name.startswith("."):
                    continue
                manifest = read_plugin_manifest(plugin_dir)
                if manifest and manifest.get("version"):
                    pid = f"{plugin_dir.name}@{mp_dir.name}"
                    mp_latest_versions[pid] = str(manifest["version"])

    for g in installed_groups:
        pid = g.get("plugin_id")
        installed_ver = g.get("version")
        latest_ver = mp_latest_versions.get(pid) if pid else None
        if latest_ver and installed_ver:
            has_update = str(latest_ver) != str(installed_ver)
            g["marketplace_version"] = latest_ver
            g["has_update"] = has_update
        else:
            g["marketplace_version"] = None
            g["has_update"] = None  # unknown (local plugin, no marketplace)
        # Propagate to each skill in the group
        for s in g["skills"]:
            s["has_update"] = g["has_update"]
            s["marketplace_version"] = g["marketplace_version"]

    # --- Priority resolution for duplicates ---
    # Priority: local (personal ~/.claude/skills/) > local-plugin > plugin (cache)
    # Within same priority, first found wins.
    PRIORITY = {"local": 0, "local-plugin": 1, "plugin": 2}
    name_entries: dict[str, list[tuple[int, int, dict]]] = {}  # name -> [(priority, group_idx, skill)]
    for gi, g in enumerate(installed_groups):
        pri = PRIORITY.get(g["source"], 3)
        for s in g["skills"]:
            key = s["name"]
            name_entries.setdefault(key, []).append((pri, gi, s))

    for key, entries in name_entries.items():
        if len(entries) <= 1:
            entries[0][2]["is_winner"] = True
            entries[0][2]["is_duplicate"] = False
            continue
        # Sort by priority (lowest number wins)
        entries.sort(key=lambda x: x[0])
        winner_pri = entries[0][0]
        for i, (pri, gi, s) in enumerate(entries):
            s["is_duplicate"] = True
            if i == 0:
                s["is_winner"] = True
            else:
                s["is_winner"] = False
                # Explain why it's shadowed
                winner = entries[0][2]
                s["shadowed_by"] = winner["abs_path"]

    # --- Stats (only count loaded skills) ---
    loaded_names = set()
    loaded_files = 0
    total_desc_chars = 0
    total_issues = 0
    total_agents = 0
    total_commands = 0
    total_hooks = 0
    total_mcp_servers = 0
    for g in installed_groups:
        for s in g["skills"]:
            if not s.get("loaded", True):
                continue
            loaded_names.add(s["name"])
            loaded_files += 1
            if s.get("enabled") and not s.get("disable_model_invocation"):
                total_desc_chars += s.get("desc_chars", 0)
            total_issues += len(s.get("validation_issues", []))
        total_agents += len(g.get("agents", []))
        cc = g.get("component_counts", {})
        total_commands += cc.get("commands", 0)
        total_hooks += len(g.get("hooks", []))
        total_mcp_servers += len(g.get("mcp_servers", []))

    budget, budget_source = get_char_budget()

    return {
        "installed": installed_groups,
        "available": available,
        "missing_plugins": missing_plugins,
        "stats": {
            "loaded_files": loaded_files,
            "unique_loaded": len(loaded_names),
            "duplicate_copies": loaded_files - len(loaded_names),
            "available_plugins": len(available),
            "available_skills": sum(p["skill_count"] for p in available),
            "missing_plugins": len(missing_plugins),
            "total_desc_chars": total_desc_chars,
            "budget": budget,
            "budget_pct": round(total_desc_chars / budget * 100) if budget else 0,
            "budget_source": budget_source,
            "total_issues": total_issues,
            "total_agents": total_agents,
            "total_commands": total_commands,
            "total_hooks": total_hooks,
            "total_mcp_servers": total_mcp_servers,
        },
    }


def get_marketplace_data() -> list[dict]:
    """Read known_marketplaces.json and enrich each entry with git/plugin metadata."""
    if not KNOWN_MARKETPLACES_FILE.exists():
        return []
    try:
        with open(KNOWN_MARKETPLACES_FILE) as f:
            raw = json.load(f)
    except Exception:
        return []

    skip_dirs = {"cache", "marketplaces", "_archived"}
    results = []
    for name, entry in raw.items():
        source = entry.get("source", {})
        source_type = source.get("source", "unknown")
        url = source.get("url", "") or source.get("repo", "") or source.get("path", "")
        install_loc = entry.get("installLocation", "")
        last_updated = entry.get("lastUpdated", "")

        loc_path = Path(install_loc) if install_loc else None
        exists = loc_path.is_dir() if loc_path else False

        # Count plugins in the marketplace directory
        plugin_count = 0
        if exists:
            plugins_subdir = loc_path / "plugins"
            search_dir = plugins_subdir if plugins_subdir.is_dir() else loc_path
            for item in search_dir.iterdir():
                if not item.is_dir() or item.name.startswith(".") or item.name in skip_dirs:
                    continue
                # Check it has at least one SKILL.md somewhere
                if any(item.rglob("SKILL.md")) or any(item.rglob("SKILL.md.disabled")):
                    plugin_count += 1

        # Git info
        branch = None
        short_sha = None
        has_git = False
        if exists and (loc_path / ".git").is_dir():
            has_git = True
            try:
                br = subprocess.run(
                    ["git", "-C", str(loc_path), "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True, timeout=5
                )
                if br.returncode == 0:
                    branch = br.stdout.strip()
                sha = subprocess.run(
                    ["git", "-C", str(loc_path), "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, timeout=5
                )
                if sha.returncode == 0:
                    short_sha = sha.stdout.strip()
            except Exception:
                pass

        # Format last_updated date
        date_short = ""
        if last_updated:
            try:
                from datetime import datetime
                dt = datetime.fromisoformat(last_updated.replace("Z", "+00:00"))
                date_short = dt.strftime("%m/%d")
            except Exception:
                date_short = last_updated[:10]

        results.append({
            "name": name,
            "source_type": source_type,
            "url": url,
            "install_location": install_loc,
            "last_updated": last_updated,
            "date_short": date_short,
            "plugin_count": plugin_count,
            "branch": branch,
            "short_sha": short_sha,
            "has_git": has_git,
            "exists": exists,
        })

    return results


def refresh_and_check_updates() -> dict:
    """Pull latest marketplace repos and compare versions with installed plugins."""
    if not MARKETPLACES_DIR.is_dir():
        return {"updates": [], "errors": []}

    errors = []

    # Git pull each marketplace
    for mp_dir in sorted(MARKETPLACES_DIR.iterdir()):
        if not mp_dir.is_dir() or mp_dir.name.startswith("."):
            continue
        git_dir = mp_dir / ".git"
        if git_dir.is_dir():
            try:
                result = subprocess.run(
                    ["git", "-C", str(mp_dir), "pull", "--ff-only"],
                    capture_output=True, text=True, timeout=30
                )
                if result.returncode != 0:
                    errors.append(f"{mp_dir.name}: git pull failed: {result.stderr.strip()}")
            except Exception as e:
                errors.append(f"{mp_dir.name}: {str(e)}")

    # Compare installed versions with marketplace versions
    installed = get_installed_plugins()
    installed_versions = {}
    for info in installed.values():
        installed_versions[info["id"]] = info["version"]

    updates = []
    for mp_dir in sorted(MARKETPLACES_DIR.iterdir()):
        if not mp_dir.is_dir() or mp_dir.name.startswith("."):
            continue
        plugins_subdir = mp_dir / "plugins"
        search_dir = plugins_subdir if plugins_subdir.is_dir() else mp_dir
        for plugin_dir in sorted(search_dir.iterdir()):
            if not plugin_dir.is_dir() or plugin_dir.name.startswith("."):
                continue
            plugin_id = f"{plugin_dir.name}@{mp_dir.name}"
            installed_ver = installed_versions.get(plugin_id)
            if installed_ver is None:
                continue

            manifest = read_plugin_manifest(plugin_dir)
            mp_version = None
            if manifest:
                mp_version = manifest.get("version")

            if mp_version and str(mp_version) != str(installed_ver):
                updates.append({
                    "plugin_id": plugin_id,
                    "name": plugin_dir.name,
                    "installed_version": str(installed_ver),
                    "available_version": str(mp_version),
                })

    return {"updates": updates, "errors": errors}


def toggle_component_enabled(abs_path: str, enabled: bool) -> dict:
    """Enable/disable a command or agent by renaming .md <-> .md.disabled."""
    p = Path(abs_path)
    if enabled:
        # Re-enable: remove .disabled suffix
        if p.name.endswith(".md.disabled"):
            new_path = p.parent / p.name.replace(".md.disabled", ".md")
            if new_path.exists():
                return {"ok": False, "error": f"Target already exists: {new_path}"}
            p.rename(new_path)
            return {"ok": True, "path": str(new_path), "enabled": True}
        return {"ok": True, "path": abs_path, "enabled": True}
    else:
        # Disable: add .disabled suffix
        if p.name.endswith(".md") and not p.name.endswith(".md.disabled"):
            new_path = p.parent / (p.name + ".disabled")
            if new_path.exists():
                return {"ok": False, "error": f"Target already exists: {new_path}"}
            p.rename(new_path)
            return {"ok": True, "path": str(new_path), "enabled": False}
        return {"ok": True, "path": abs_path, "enabled": False}


def move_skill_to_scanned(abs_path: str, name: str) -> dict:
    """Copy a skill's parent directory to ~/.claude/skills/<name>/."""
    import shutil

    src = Path(abs_path)
    if not src.is_file():
        return {"ok": False, "error": "Source file not found"}

    src_dir = src.parent
    dest_dir = SKILLS_DIR / name

    if dest_dir.exists():
        return {"ok": False, "error": f"Destination already exists: {dest_dir}"}

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(src_dir), str(dest_dir))
    return {"ok": True, "source": str(src_dir), "destination": str(dest_dir)}


def toggle_model_invocation(abs_path: str, disable: bool) -> dict:
    """Toggle the disable-model-invocation frontmatter field in a .md file."""
    p = Path(abs_path)
    if not p.is_file():
        return {"ok": False, "error": "File not found"}

    content = p.read_text(encoding="utf-8")
    fm_match = re.match(r"^(---\s*\n)(.*?)(\n---)", content, re.DOTALL)
    if not fm_match:
        if disable:
            # No frontmatter exists — add one with the field
            content = "---\ndisable-model-invocation: true\n---\n" + content
            p.write_text(content, encoding="utf-8")
            return {"ok": True, "path": abs_path, "disable_model_invocation": True}
        return {"ok": True, "path": abs_path, "disable_model_invocation": False}

    fm_block = fm_match.group(2)

    if disable:
        # Add or set the field to true
        if re.search(r"^disable-model-invocation:", fm_block, re.MULTILINE):
            fm_block = re.sub(
                r"^disable-model-invocation:.*$",
                "disable-model-invocation: true",
                fm_block,
                flags=re.MULTILINE,
            )
        else:
            fm_block = fm_block.rstrip("\n") + "\ndisable-model-invocation: true"
    else:
        # Remove the field entirely
        fm_block = re.sub(r"^disable-model-invocation:.*\n?", "", fm_block, flags=re.MULTILINE)

    new_content = fm_match.group(1) + fm_block + fm_match.group(3) + content[fm_match.end():]
    p.write_text(new_content, encoding="utf-8")
    return {"ok": True, "path": abs_path, "disable_model_invocation": disable}


# ---------------------------------------------------------------------------
# Response cache — avoids re-scanning the filesystem on every request
# ---------------------------------------------------------------------------

_skills_cache: bytes | None = None
_skills_cache_key: tuple | None = None  # (installed_plugins mtime, preferences mtime, settings mtime)


def _cache_key() -> tuple:
    """Build a cheap cache key from file mtimes that signal data changes."""
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except Exception:
            return 0.0
    return (
        _mtime(INSTALLED_PLUGINS_FILE),
        _mtime(PREFS_FILE),
        _mtime(SETTINGS_FILE),
        _mtime(KNOWN_MARKETPLACES_FILE),
    )


def _invalidate_skills_cache():
    global _skills_cache, _skills_cache_key
    _skills_cache = None
    _skills_cache_key = None


def _get_skills_response() -> bytes:
    global _skills_cache, _skills_cache_key
    key = _cache_key()
    if _skills_cache is not None and _skills_cache_key == key:
        return _skills_cache
    data = get_all_data()
    _skills_cache = json.dumps(data, indent=2).encode()
    _skills_cache_key = key
    return _skills_cache


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/skills":
            payload = _get_skills_response()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/api/marketplaces":
            data = get_marketplace_data()
            self._json_response(data)
        elif self.path == "/" or self.path == "/index.html":
            self.path = "/index.html"
            return super().do_GET()
        else:
            return super().do_GET()

    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length))

    def do_POST(self):
        _invalidate_skills_cache()
        if self.path == "/api/toggle":
            body = self._read_body()
            result = set_skill_enabled(body.get("path", ""), body.get("enabled", True))
            self._json_response(result)

        elif self.path == "/api/install":
            body = self._read_body()
            plugin_id = body.get("plugin_id", "")
            if not plugin_id or "@" not in plugin_id:
                self._json_response({"ok": False, "error": "Invalid plugin_id"}, 400)
                return
            result = subprocess.run(
                ["claude", "plugin", "install", plugin_id],
                capture_output=True, text=True, timeout=60
            )
            self._json_response({
                "ok": result.returncode == 0,
                "plugin_id": plugin_id,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            })

        elif self.path == "/api/uninstall":
            body = self._read_body()
            plugin_id = body.get("plugin_id", "")
            if not plugin_id or "@" not in plugin_id:
                self._json_response({"ok": False, "error": "Invalid plugin_id"}, 400)
                return
            result = subprocess.run(
                ["claude", "plugin", "uninstall", plugin_id],
                input="y\n", capture_output=True, text=True, timeout=60
            )
            self._json_response({
                "ok": result.returncode == 0,
                "plugin_id": plugin_id,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            })

        elif self.path == "/api/remove-missing":
            # Remove a stale plugin entry from installed_plugins.json
            body = self._read_body()
            plugin_id = body.get("plugin_id", "")
            if not plugin_id:
                self._json_response({"ok": False, "error": "Missing plugin_id"}, 400)
                return
            try:
                with open(INSTALLED_PLUGINS_FILE) as f:
                    data = json.load(f)
                if plugin_id in data.get("plugins", {}):
                    del data["plugins"][plugin_id]
                    with open(INSTALLED_PLUGINS_FILE, "w") as f:
                        json.dump(data, f, indent=2)
                    # Also remove from settings.json enabledPlugins
                    try:
                        with open(SETTINGS_FILE) as f:
                            settings = json.load(f)
                        if plugin_id in settings.get("enabledPlugins", {}):
                            del settings["enabledPlugins"][plugin_id]
                            with open(SETTINGS_FILE, "w") as f:
                                json.dump(settings, f, indent=2)
                    except Exception:
                        pass
                    self._json_response({"ok": True, "plugin_id": plugin_id})
                else:
                    self._json_response({"ok": False, "error": "Plugin not found"}, 404)
            except Exception as e:
                self._json_response({"ok": False, "error": str(e)}, 500)

        elif self.path == "/api/remove-local-plugin":
            # Remove a local plugin directory
            body = self._read_body()
            plugin_name = body.get("name", "")
            if not plugin_name or "/" in plugin_name or ".." in plugin_name:
                self._json_response({"ok": False, "error": "Invalid name"}, 400)
                return
            plugin_dir = PLUGINS_DIR / plugin_name
            if not plugin_dir.is_dir():
                self._json_response({"ok": False, "error": "Directory not found"}, 404)
                return
            import shutil
            archive_dir = PLUGINS_DIR / "_archived"
            archive_dir.mkdir(exist_ok=True)
            dest = archive_dir / plugin_name
            if dest.exists():
                shutil.rmtree(dest)
            plugin_dir.rename(dest)
            self._json_response({"ok": True, "name": plugin_name, "archived_to": str(dest)})

        elif self.path == "/api/toggle-component":
            body = self._read_body()
            abs_path = body.get("abs_path", "")
            enabled = body.get("enabled", True)
            try:
                resolved = Path(abs_path).resolve()
                if not str(resolved).startswith(str(CLAUDE_DIR.resolve())):
                    self._json_response({"ok": False, "error": "Path not under ~/.claude"}, 403)
                    return
                result = toggle_component_enabled(str(resolved), enabled)
                self._json_response(result)
            except Exception as e:
                self._json_response({"ok": False, "error": str(e)}, 500)

        elif self.path == "/api/move-skill-to-scanned":
            body = self._read_body()
            abs_path = body.get("abs_path", "")
            name = body.get("name", "")
            if not abs_path or not name:
                self._json_response({"ok": False, "error": "Missing abs_path or name"}, 400)
                return
            # Security: validate path is under CLAUDE_DIR
            try:
                resolved = Path(abs_path).resolve()
                if not str(resolved).startswith(str(CLAUDE_DIR.resolve())):
                    self._json_response({"ok": False, "error": "Path not under ~/.claude"}, 403)
                    return
                result = move_skill_to_scanned(str(resolved), name)
                self._json_response(result)
            except Exception as e:
                self._json_response({"ok": False, "error": str(e)}, 500)

        elif self.path == "/api/toggle-model-invocation":
            body = self._read_body()
            abs_path = body.get("abs_path", "")
            disable = body.get("disable", True)
            # Security: validate path is under CLAUDE_DIR
            try:
                resolved = Path(abs_path).resolve()
                if not str(resolved).startswith(str(CLAUDE_DIR.resolve())):
                    self._json_response({"ok": False, "error": "Path not under ~/.claude"}, 403)
                    return
                result = toggle_model_invocation(str(resolved), disable)
                self._json_response(result)
            except Exception as e:
                self._json_response({"ok": False, "error": str(e)}, 500)

        elif self.path == "/api/check-updates":
            try:
                result = refresh_and_check_updates()
                self._json_response(result)
            except Exception as e:
                self._json_response({"updates": [], "errors": [str(e)]}, 500)

        elif self.path == "/api/update-plugin":
            body = self._read_body()
            plugin_id = body.get("plugin_id", "")
            if not plugin_id or "@" not in plugin_id:
                self._json_response({"ok": False, "error": "Invalid plugin_id"}, 400)
                return
            result = subprocess.run(
                ["claude", "plugin", "install", plugin_id],
                capture_output=True, text=True, timeout=60
            )
            self._json_response({
                "ok": result.returncode == 0,
                "plugin_id": plugin_id,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            })

        elif self.path == "/api/marketplace-add":
            body = self._read_body()
            url = body.get("url", "").strip()
            if not url:
                self._json_response({"ok": False, "error": "Missing url"}, 400)
                return
            result = subprocess.run(
                ["claude", "plugin", "marketplace", "add", url, "--scope", "user"],
                capture_output=True, text=True, timeout=120
            )
            self._json_response({
                "ok": result.returncode == 0,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            })

        elif self.path == "/api/marketplace-remove":
            body = self._read_body()
            name = body.get("name", "").strip()
            if not name:
                self._json_response({"ok": False, "error": "Missing name"}, 400)
                return
            result = subprocess.run(
                ["claude", "plugin", "marketplace", "remove", name],
                input="y\n", capture_output=True, text=True, timeout=30
            )
            self._json_response({
                "ok": result.returncode == 0,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            })

        elif self.path == "/api/marketplace-update":
            body = self._read_body()
            name = body.get("name", "").strip()
            cmd = ["claude", "plugin", "marketplace", "update"]
            if name:
                cmd.append(name)
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120
            )
            self._json_response({
                "ok": result.returncode == 0,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            })

        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        pass  # Quiet


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Skill Manager running at http://127.0.0.1:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "apply-prefs":
        n = apply_prefs()
        print(f"Re-applied preferences: {n} skills re-disabled")
        sys.exit(0)
    main()
