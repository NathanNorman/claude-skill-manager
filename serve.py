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
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Iterator

CLAUDE_DIR = Path.home() / ".claude"
PLUGINS_DIR = CLAUDE_DIR / "plugins"
MARKETPLACES_DIR = PLUGINS_DIR / "marketplaces"
CACHE_DIR = PLUGINS_DIR / "cache"
SKILLS_DIR = CLAUDE_DIR / "skills"
AGENTS_DIR = CLAUDE_DIR / "agents"
COMMANDS_DIR = CLAUDE_DIR / "commands"
INSTALLED_PLUGINS_FILE = PLUGINS_DIR / "installed_plugins.json"
KNOWN_MARKETPLACES_FILE = PLUGINS_DIR / "known_marketplaces.json"
DATA_DIR = CLAUDE_DIR / "skill-manager-data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
PREFS_FILE = DATA_DIR / "preferences.json"
SETTINGS_FILE = CLAUDE_DIR / "settings.json"
PORT = 8421


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _find_skill_md(directory: Path) -> Path | None:
    """Find SKILL.md or SKILL.md.disabled in a directory."""
    for name in ("SKILL.md", "SKILL.md.disabled"):
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None


def _scan_skill_dirs(parent: Path, skip_git: bool = False) -> list[dict]:
    """Scan immediate subdirectories of parent for SKILL.md files.

    This is the shared pattern used by scan_skills_subdir, scan_flat_layout,
    and scan_supplemental_skills.
    """
    results = []
    if not parent.is_dir():
        return results
    for subdir in sorted(parent.iterdir()):
        if not subdir.is_dir() or subdir.name.startswith("."):
            continue
        skill_md = _find_skill_md(subdir)
        if skill_md:
            entry = _build_skill_entry(skill_md, "skill", skip_git=skip_git)
            if entry:
                results.append(entry)
    return results


def _iter_marketplace_plugins() -> Iterator[tuple[Path, Path, str]]:
    """Yield (mp_dir, plugin_dir, plugin_id) for each plugin in all marketplaces."""
    if not MARKETPLACES_DIR.is_dir():
        return
    for mp_dir in sorted(MARKETPLACES_DIR.iterdir()):
        if not mp_dir.is_dir() or mp_dir.name.startswith("."):
            continue
        plugins_subdir = mp_dir / "plugins"
        search_dir = plugins_subdir if plugins_subdir.is_dir() else mp_dir
        for plugin_dir in sorted(search_dir.iterdir()):
            if not plugin_dir.is_dir() or plugin_dir.name.startswith("."):
                continue
            yield mp_dir, plugin_dir, f"{plugin_dir.name}@{mp_dir.name}"


def _require_claude_path(abs_path: str) -> Path | None:
    """Validate that abs_path resolves under ~/.claude/. Returns resolved Path or None."""
    resolved = Path(abs_path).resolve()
    if str(resolved).startswith(str(CLAUDE_DIR.resolve())):
        return resolved
    return None


def _run_plugin_install(plugin_id: str) -> dict:
    """Run `claude plugin install`, apply prefs, invalidate cache. Shared by install and update."""
    result = subprocess.run(
        ["claude", "plugin", "install", plugin_id],
        capture_output=True, text=True, timeout=60
    )
    ok = result.returncode == 0
    prefs_applied = 0
    if ok:
        prefs_applied = apply_prefs()
        _invalidate_skills_cache()
    return {
        "ok": ok,
        "plugin_id": plugin_id,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "prefs_applied": prefs_applied,
    }


# ---------------------------------------------------------------------------
# Config and preferences
# ---------------------------------------------------------------------------

def get_char_budget() -> tuple[int, str]:
    """Resolve the skill description character budget.

    Priority:
    1. OS environment variable SLASH_COMMAND_TOOL_CHAR_BUDGET
    2. settings.json env section (where Claude Code reads it)
    3. Default: 15000

    Returns (budget, source_label).
    """
    env_val = os.environ.get("SLASH_COMMAND_TOOL_CHAR_BUDGET")
    if env_val and env_val.isdigit() and int(env_val) > 0:
        return int(env_val), "env"

    try:
        with open(SETTINGS_FILE) as f:
            settings = json.load(f)
        val = settings.get("env", {}).get("SLASH_COMMAND_TOOL_CHAR_BUDGET")
        if val and str(val).isdigit() and int(val) > 0:
            return int(val), "settings.json"
    except Exception:
        pass

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
            if val in (">-", ">", "|-", "|", ""):
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
        if disabled_file.exists() and not skill_file.exists():
            disabled_file.rename(skill_file)
        prefs.pop(rel_path, None)
        dir_key = str(skill_file.parent.relative_to(CLAUDE_DIR))
        prefs.pop(dir_key, None)
    else:
        if skill_file.exists():
            skill_file.rename(disabled_file)
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

_git_dirty_cache: dict[str, set[str]] = {}
_repo_root_cache: dict[str, str | None] = {}


def _find_repo_root(dir_path: str) -> str | None:
    """Find the git repo root for a directory, with caching."""
    if dir_path in _repo_root_cache:
        return _repo_root_cache[dir_path]
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
    """Check if a file has uncommitted changes in its git repo."""
    abs_str = str(file_path.resolve())
    repo_root = _find_repo_root(str(file_path.parent))
    if repo_root is None:
        return False

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
                        path_part = line[3:].split(" -> ")[-1].strip()
                        dirty_paths.add(os.path.join(repo_root, path_part))
            _git_dirty_cache[repo_root] = dirty_paths
        except Exception:
            _git_dirty_cache[repo_root] = set()

    dirty = _git_dirty_cache[repo_root]
    if abs_str in dirty:
        return True
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
    is_disabled = skill_md.name.endswith(".md.disabled")

    if source_type == "skill":
        active_path = str((skill_md.parent / "SKILL.md").relative_to(CLAUDE_DIR))
    else:
        active_path = str(skill_md.relative_to(CLAUDE_DIR))

    rel = str(skill_md.relative_to(CLAUDE_DIR))

    try:
        mtime = skill_md.stat().st_mtime
        age_days = int((time.time() - mtime) / 86400)
    except Exception:
        mtime = 0
        age_days = -1

    has_personal_copy = False
    if source_type == "skill" and not str(skill_md).startswith(str(SKILLS_DIR)):
        has_personal_copy = (SKILLS_DIR / name / "SKILL.md").exists()

    # Validation issues
    issues = []
    fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not fm_match:
        issues.append("no_frontmatter")
    else:
        if "name:" not in fm_match.group(1):
            issues.append("no_name")
        if "description:" not in fm_match.group(1):
            issues.append("no_description")

    # Description char count (what Claude Code injects into context)
    if description:
        injected_text = description
    elif fm_match:
        body = content[fm_match.end():].strip()
        injected_text = body.split("\n\n")[0].strip() if body else ""
    else:
        injected_text = content.split("\n\n")[0].strip() if content else ""

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
        "desc_chars": len(injected_text),
        "frontmatter": fm,
        "git_dirty": False if skip_git else _is_git_dirty(skill_md),
    }


# ---------------------------------------------------------------------------
# Plugin manifest reading
# ---------------------------------------------------------------------------

def read_plugin_manifest(plugin_dir: Path) -> dict | None:
    """Read .claude-plugin/plugin.json if it exists."""
    manifest_path = plugin_dir / ".claude-plugin" / "plugin.json"
    if not manifest_path.exists():
        return None
    try:
        with open(manifest_path) as f:
            return json.load(f)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Discovery — match Claude Code's actual behavior
# ---------------------------------------------------------------------------

def scan_supplemental_skills(plugin_dir: Path, skills_field: list | str, skip_git: bool = False) -> list[dict]:
    """Scan additional skill paths from the skills field (supplements default skills/ dir)."""
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
            skill_md = _find_skill_md(resolved)
            if skill_md:
                entry = _build_skill_entry(skill_md, "skill", skip_git=skip_git)
                if entry:
                    results.append(entry)
            else:
                results.extend(_scan_skill_dirs(resolved, skip_git=skip_git))
    return results


def scan_skills_subdir(plugin_dir: Path, skip_git: bool = False) -> list[dict]:
    """Rule 2: no skills field in manifest — auto-discover skills/*/SKILL.md."""
    return _scan_skill_dirs(plugin_dir / "skills", skip_git=skip_git)


def scan_flat_layout(plugin_dir: Path, skip_git: bool = False) -> list[dict]:
    """Rule 4: no plugin.json — scan default locations.

    Tries skills/ first, then looks for a subdirectory containing skill subdirs.
    """
    if not plugin_dir.is_dir():
        return []

    skills_dir = plugin_dir / "skills"
    if skills_dir.is_dir():
        return _scan_skill_dirs(skills_dir, skip_git=skip_git)

    # Try a subdirectory that contains skill subdirs
    for subdir in plugin_dir.iterdir():
        if subdir.is_dir() and not subdir.name.startswith("."):
            has_skill_subdirs = any(
                _find_skill_md(child) is not None
                for child in subdir.iterdir()
                if child.is_dir() and not child.name.startswith(".")
            )
            if has_skill_subdirs:
                return _scan_skill_dirs(subdir, skip_git=skip_git)

    return []


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
        # Validation issues
        issues = []
        fm_match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
        if not fm_match:
            issues.append("no_frontmatter")
        else:
            if "name:" not in fm_match.group(1):
                issues.append("no_name")
            if "description:" not in fm_match.group(1):
                issues.append("no_description")
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
            "validation_issues": issues,
        })

    def _scan_agent_dir(d: Path):
        if not d.is_dir():
            return
        for md in sorted(list(d.glob("*.md")) + list(d.glob("*.md.disabled"))):
            if not md.name.startswith("."):
                _scan_agent_file(md)
        for sub in sorted(d.iterdir()):
            if sub.is_dir() and not sub.name.startswith("."):
                for md in sorted(list(sub.glob("*.md")) + list(sub.glob("*.md.disabled"))):
                    if not md.name.startswith("."):
                        _scan_agent_file(md)

    _scan_agent_dir(plugin_dir / "agents")

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
                    results.append({
                        "event_type": event_type,
                        "matcher": matcher if matcher else "*",
                        "entry_type": hook_entry.get("type", "unknown"),
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
                _parse_hooks_data(json.load(f))
        except Exception:
            pass

    _read_hooks_file(plugin_dir / "hooks" / "hooks.json")

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
                        _parse_servers(json.load(f).get("mcpServers", {}), mcp_path.name)
                except Exception:
                    pass
                return results

    mcp_fallback = plugin_dir / ".mcp.json"
    if mcp_fallback.is_file():
        try:
            with open(mcp_fallback) as f:
                data = json.load(f)
            _parse_servers(data.get("mcpServers", data), ".mcp.json")
        except Exception:
            pass

    return results


def discover_plugin_components(plugin_dir: Path, skip_git: bool = False) -> dict:
    """Discover all plugin components the way Claude Code does.

    Returns: {skills, agents, hooks, mcp_servers, component_counts}
    """
    manifest = read_plugin_manifest(plugin_dir)

    if manifest is not None:
        skills = scan_skills_subdir(plugin_dir, skip_git=skip_git)
        seen_paths = {s["abs_path"] for s in skills}

        skills_field = manifest.get("skills")
        if skills_field and (isinstance(skills_field, str) or len(skills_field) > 0):
            for extra in scan_supplemental_skills(plugin_dir, skills_field, skip_git=skip_git):
                if extra["abs_path"] not in seen_paths:
                    skills.append(extra)
                    seen_paths.add(extra["abs_path"])
    else:
        skills = scan_flat_layout(plugin_dir, skip_git=skip_git)

    # Mark loaded vs unloaded skills
    loaded_paths = {s["abs_path"] for s in skills}
    for s in skills:
        s["loaded"] = True

    # Find unloaded skills (exist on disk but not discovered by rules)
    if plugin_dir.is_dir():
        for pattern in ("SKILL.md", "SKILL.md.disabled"):
            for skill_md in sorted(plugin_dir.rglob(pattern)):
                if str(skill_md) not in loaded_paths:
                    entry = _build_skill_entry(skill_md, "skill", skip_git=skip_git)
                    if entry:
                        entry["loaded"] = False
                        skills.append(entry)

    commands = scan_commands(plugin_dir, skip_git=skip_git)
    for c in commands:
        c["loaded"] = True

    agents = scan_agents(plugin_dir, manifest)
    hooks = scan_hooks(plugin_dir, manifest)
    mcp_servers = scan_mcp_servers(plugin_dir, manifest)

    return {
        "skills": skills + commands,
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


def scan_settings_hooks() -> list[dict]:
    """Read hooks from ~/.claude/settings.json (user-level hooks)."""
    results = []
    if not SETTINGS_FILE.exists():
        return results
    try:
        with open(SETTINGS_FILE) as f:
            settings = json.load(f)
    except Exception:
        return results

    for event_type, matchers in settings.get("hooks", {}).items():
        if not isinstance(matchers, list):
            continue
        for matcher_block in matchers:
            matcher = matcher_block.get("matcher", "*")
            for hook_entry in matcher_block.get("hooks", []):
                command = hook_entry.get("command", "")
                script_name = ""
                if command:
                    parts = command.strip().split()
                    for p in reversed(parts):
                        if "/" in p and not p.startswith("-"):
                            script_name = Path(p).name
                            break
                    if not script_name:
                        script_name = parts[-1] if parts else command[:40]
                results.append({
                    "event_type": event_type,
                    "matcher": matcher if matcher else "*",
                    "entry_type": hook_entry.get("type", "unknown"),
                    "command": command,
                    "prompt": hook_entry.get("prompt", ""),
                    "agent": hook_entry.get("agent", ""),
                    "timeout": hook_entry.get("timeout"),
                    "script_name": script_name,
                })
    return results


# ---------------------------------------------------------------------------
# Marketplace helpers
# ---------------------------------------------------------------------------

# Cache marketplace repo URLs (computed once per request)
_mp_repo_cache: dict[str, str | None] = {}


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
            if url.startswith("git@"):
                url = url.replace(":", "/", 1).replace("git@", "https://", 1)
            if url.endswith(".git"):
                url = url[:-4]
            return url
    except Exception:
        pass
    return None


def _get_mp_repo_url(mp_name: str) -> str | None:
    if mp_name not in _mp_repo_cache:
        _mp_repo_cache[mp_name] = _marketplace_repo_url(mp_name)
    return _mp_repo_cache.get(mp_name)


def _get_marketplace_versions() -> dict[str, str]:
    """Build plugin_id -> latest version map from marketplace manifests."""
    versions: dict[str, str] = {}
    for mp_dir, plugin_dir, plugin_id in _iter_marketplace_plugins():
        manifest = read_plugin_manifest(plugin_dir)
        if manifest and manifest.get("version"):
            versions[plugin_id] = str(manifest["version"])
    return versions


# ---------------------------------------------------------------------------
# Main data assembly
# ---------------------------------------------------------------------------

def get_all_data() -> dict:
    """Build the complete data structure for the UI."""
    _mp_repo_cache.clear()
    _git_dirty_cache.clear()
    _repo_root_cache.clear()
    installed_map = get_installed_plugins()

    # Scan marketplace for available/installed plugin metadata
    mp_plugins = {}
    for mp_dir, plugin_dir, plugin_id in _iter_marketplace_plugins():
        components = discover_plugin_components(plugin_dir, skip_git=True)
        if not components["skills"]:
            continue
        is_installed = any(
            info["id"] == plugin_id or info["id"].startswith(plugin_dir.name + "@")
            for info in installed_map.values()
        )
        mp_plugins[plugin_id] = {
            "name": plugin_dir.name,
            "marketplace": mp_dir.name,
            "plugin_id": plugin_id,
            "installed": is_installed,
            "skills": components["skills"],
            "skill_count": len(components["skills"]),
            "source": "marketplace",
            "repo_url": _get_mp_repo_url(mp_dir.name),
            "agents": components["agents"],
            "hooks": components["hooks"],
            "mcp_servers": components["mcp_servers"],
            "component_counts": components["component_counts"],
        }

    # Installed: cache (referenced only)
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
                        continue
                    components = discover_plugin_components(version_dir)
                    if components["skills"]:
                        installed_groups.append({
                            "name": plugin_dir.name,
                            "plugin_id": info["id"],
                            "version": version_dir.name,
                            "marketplace": mp_dir.name,
                            "skills": components["skills"],
                            "skill_count": len(components["skills"]),
                            "source": "plugin",
                            "install_path": str(version_dir),
                            "repo_url": _get_mp_repo_url(mp_dir.name),
                            "agents": components["agents"],
                            "hooks": components["hooks"],
                            "mcp_servers": components["mcp_servers"],
                            "component_counts": components["component_counts"],
                        })
                    elif not version_dir.is_dir():
                        missing_plugins.append({
                            "name": plugin_dir.name,
                            "plugin_id": info["id"],
                            "installPath": info.get("installPath", ""),
                        })

    # Check for plugins whose install paths don't exist
    for real_path, info in installed_map.items():
        if not os.path.isdir(real_path):
            if not any(m["plugin_id"] == info["id"] for m in missing_plugins):
                name = info["id"].split("@")[0] if "@" in info["id"] else info["id"]
                missing_plugins.append({
                    "name": name,
                    "plugin_id": info["id"],
                    "installPath": info.get("installPath", ""),
                })

    # Installed: direct plugins
    if PLUGINS_DIR.is_dir():
        skip = {"cache", "marketplaces", "_archived"}
        for item in sorted(PLUGINS_DIR.iterdir()):
            if not item.is_dir() or item.name in skip or item.name.startswith("."):
                continue
            components = discover_plugin_components(item)
            if components["skills"]:
                installed_groups.append({
                    "name": item.name,
                    "plugin_id": None,
                    "version": None,
                    "marketplace": None,
                    "skills": components["skills"],
                    "skill_count": len(components["skills"]),
                    "source": "local-plugin",
                    "install_path": str(item),
                    "repo_url": None,
                    "agents": components["agents"],
                    "hooks": components["hooks"],
                    "mcp_servers": components["mcp_servers"],
                    "component_counts": components["component_counts"],
                })

    # Installed: local skills + agents + commands + hooks (pseudo-group)
    local_skills = []
    if SKILLS_DIR.is_dir():
        for pattern in ("SKILL.md", "SKILL.md.disabled"):
            for skill_md in sorted(SKILLS_DIR.rglob(pattern)):
                entry = _build_skill_entry(skill_md, "skill")
                if entry:
                    local_skills.append(entry)

    local_agents = scan_agents(CLAUDE_DIR)
    local_commands = scan_commands(CLAUDE_DIR)
    local_hooks = scan_settings_hooks()

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

    # Available: marketplace plugins NOT installed
    available = [p for p in mp_plugins.values() if not p["installed"]]

    # Version freshness
    mp_latest_versions = _get_marketplace_versions()
    for g in installed_groups:
        pid = g.get("plugin_id")
        installed_ver = g.get("version")
        latest_ver = mp_latest_versions.get(pid) if pid else None
        if latest_ver and installed_ver:
            g["has_update"] = str(latest_ver) != str(installed_ver)
            g["marketplace_version"] = latest_ver
        else:
            g["marketplace_version"] = None
            g["has_update"] = None
        for s in g["skills"]:
            s["has_update"] = g["has_update"]
            s["marketplace_version"] = g["marketplace_version"]

    # Priority resolution for duplicates
    PRIORITY = {"local": 0, "local-plugin": 1, "plugin": 2}
    name_entries: dict[str, list[tuple[int, int, dict]]] = {}
    for gi, g in enumerate(installed_groups):
        pri = PRIORITY.get(g["source"], 3)
        for s in g["skills"]:
            key = (s["name"], s.get("source_type", "skill"))
            name_entries.setdefault(key, []).append((pri, gi, s))

    for key, entries in name_entries.items():
        if len(entries) <= 1:
            entries[0][2]["is_winner"] = True
            entries[0][2]["is_duplicate"] = False
            continue
        entries.sort(key=lambda x: x[0])
        for i, (pri, gi, s) in enumerate(entries):
            s["is_duplicate"] = True
            if i == 0:
                s["is_winner"] = True
            else:
                s["is_winner"] = False
                s["shadowed_by"] = entries[0][2]["abs_path"]

    # Stats
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
        for a in g.get("agents", []):
            total_issues += len(a.get("validation_issues", []))
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
    """Read known_marketplaces.json and enrich with git/plugin metadata."""
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

        plugin_count = 0
        if exists:
            plugins_subdir = loc_path / "plugins"
            search_dir = plugins_subdir if plugins_subdir.is_dir() else loc_path
            for item in search_dir.iterdir():
                if not item.is_dir() or item.name.startswith(".") or item.name in skip_dirs:
                    continue
                if any(item.rglob("SKILL.md")) or any(item.rglob("SKILL.md.disabled")):
                    plugin_count += 1

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
        return {"updates": [], "errors": [], "pulled_changes": False}

    errors = []
    pulled_changes = False

    for mp_dir in sorted(MARKETPLACES_DIR.iterdir()):
        if not mp_dir.is_dir() or mp_dir.name.startswith("."):
            continue
        if not (mp_dir / ".git").is_dir():
            continue
        try:
            subprocess.run(
                ["git", "-C", str(mp_dir), "stash", "--quiet"],
                capture_output=True, text=True, timeout=10
            )
            result = subprocess.run(
                ["git", "-C", str(mp_dir), "pull", "--ff-only"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                errors.append(f"{mp_dir.name}: git pull failed: {result.stderr.strip()}")
            elif "Already up to date" not in result.stdout:
                pulled_changes = True
        except Exception as e:
            errors.append(f"{mp_dir.name}: {str(e)}")

    # Compare installed versions with marketplace versions
    installed_versions = {}
    for info in get_installed_plugins().values():
        installed_versions[info["id"]] = info["version"]

    updates = []
    for _mp_dir, plugin_dir, plugin_id in _iter_marketplace_plugins():
        installed_ver = installed_versions.get(plugin_id)
        if installed_ver is None:
            continue
        manifest = read_plugin_manifest(plugin_dir)
        mp_version = manifest.get("version") if manifest else None
        if mp_version and str(mp_version) != str(installed_ver):
            updates.append({
                "plugin_id": plugin_id,
                "name": plugin_dir.name,
                "installed_version": str(installed_ver),
                "available_version": str(mp_version),
            })

    if pulled_changes:
        _invalidate_skills_cache()

    return {"updates": updates, "errors": errors, "pulled_changes": pulled_changes}


# ---------------------------------------------------------------------------
# Mutation helpers
# ---------------------------------------------------------------------------

def toggle_component_enabled(abs_path: str, enabled: bool) -> dict:
    """Enable/disable a command or agent by renaming .md <-> .md.disabled."""
    p = Path(abs_path)
    if enabled:
        if p.name.endswith(".md.disabled"):
            new_path = p.parent / p.name.replace(".md.disabled", ".md")
            if new_path.exists():
                return {"ok": False, "error": f"Target already exists: {new_path}"}
            p.rename(new_path)
            return {"ok": True, "path": str(new_path), "enabled": True}
        return {"ok": True, "path": abs_path, "enabled": True}
    else:
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

    dest_dir = SKILLS_DIR / name
    if dest_dir.exists():
        return {"ok": False, "error": f"Destination already exists: {dest_dir}"}

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(src.parent), str(dest_dir))
    return {"ok": True, "source": str(src.parent), "destination": str(dest_dir)}


def toggle_model_invocation(abs_path: str, disable: bool) -> dict:
    """Toggle the disable-model-invocation frontmatter field in a .md file."""
    p = Path(abs_path)
    if not p.is_file():
        return {"ok": False, "error": "File not found"}

    content = p.read_text(encoding="utf-8")
    fm_match = re.match(r"^(---\s*\n)(.*?)(\n---)", content, re.DOTALL)
    if not fm_match:
        if disable:
            content = "---\ndisable-model-invocation: true\n---\n" + content
            p.write_text(content, encoding="utf-8")
            return {"ok": True, "path": abs_path, "disable_model_invocation": True}
        return {"ok": True, "path": abs_path, "disable_model_invocation": False}

    fm_block = fm_match.group(2)

    if disable:
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
        fm_block = re.sub(r"^disable-model-invocation:.*\n?", "", fm_block, flags=re.MULTILINE)

    new_content = fm_match.group(1) + fm_block + fm_match.group(3) + content[fm_match.end():]
    p.write_text(new_content, encoding="utf-8")
    return {"ok": True, "path": abs_path, "disable_model_invocation": disable}


# ---------------------------------------------------------------------------
# Response cache
# ---------------------------------------------------------------------------

_skills_cache: bytes | None = None
_skills_cache_key: tuple | None = None


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
        _mtime(SKILLS_DIR),
        _mtime(CACHE_DIR),
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


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

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
            self._json_response(get_marketplace_data())
        elif self.path in ("/", "/index.html"):
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

    def _validate_plugin_id(self, body: dict) -> str | None:
        """Extract and validate plugin_id from body. Sends error if invalid. Returns id or None."""
        plugin_id = body.get("plugin_id", "")
        if not plugin_id or "@" not in plugin_id:
            self._json_response({"ok": False, "error": "Invalid plugin_id"}, 400)
            return None
        return plugin_id

    def _safe_path_handler(self, body: dict, handler):
        """Validate abs_path is under ~/.claude/, then call handler(resolved_path, body)."""
        abs_path = body.get("abs_path", "")
        resolved = _require_claude_path(abs_path)
        if resolved is None:
            self._json_response({"ok": False, "error": "Path not under ~/.claude"}, 403)
            return
        try:
            result = handler(resolved, body)
            self._json_response(result)
        except Exception as e:
            self._json_response({"ok": False, "error": str(e)}, 500)

    # POST route dispatch
    _post_routes: dict = {}

    def do_POST(self):
        _invalidate_skills_cache()
        route = self._post_routes.get(self.path)
        if route:
            route(self)
        else:
            self.send_response(404)
            self.end_headers()

    def _handle_toggle(self):
        body = self._read_body()
        self._json_response(set_skill_enabled(body.get("path", ""), body.get("enabled", True)))

    def _handle_install(self):
        body = self._read_body()
        plugin_id = self._validate_plugin_id(body)
        if plugin_id:
            self._json_response(_run_plugin_install(plugin_id))

    def _handle_uninstall(self):
        body = self._read_body()
        plugin_id = self._validate_plugin_id(body)
        if not plugin_id:
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

    def _handle_remove_missing(self):
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

    def _handle_remove_local_plugin(self):
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

    def _handle_toggle_component(self):
        body = self._read_body()
        self._safe_path_handler(body, lambda p, b: toggle_component_enabled(str(p), b.get("enabled", True)))

    def _handle_move_skill(self):
        body = self._read_body()
        name = body.get("name", "")
        if not name:
            self._json_response({"ok": False, "error": "Missing name"}, 400)
            return
        self._safe_path_handler(body, lambda p, b: move_skill_to_scanned(str(p), b["name"]))

    def _handle_toggle_model_invocation(self):
        body = self._read_body()
        self._safe_path_handler(body, lambda p, b: toggle_model_invocation(str(p), b.get("disable", True)))

    def _handle_check_updates(self):
        try:
            self._json_response(refresh_and_check_updates())
        except Exception as e:
            self._json_response({"updates": [], "errors": [str(e)]}, 500)

    def _handle_update_plugin(self):
        body = self._read_body()
        plugin_id = self._validate_plugin_id(body)
        if plugin_id:
            self._json_response(_run_plugin_install(plugin_id))

    def _handle_marketplace_add(self):
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

    def _handle_marketplace_remove(self):
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

    def _handle_marketplace_update(self):
        body = self._read_body()
        name = body.get("name", "").strip()
        cmd = ["claude", "plugin", "marketplace", "update"]
        if name:
            cmd.append(name)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        self._json_response({
            "ok": result.returncode == 0,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        })

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, format, *args):
        pass  # Quiet


# Register POST routes
Handler._post_routes = {
    "/api/toggle": Handler._handle_toggle,
    "/api/install": Handler._handle_install,
    "/api/uninstall": Handler._handle_uninstall,
    "/api/remove-missing": Handler._handle_remove_missing,
    "/api/remove-local-plugin": Handler._handle_remove_local_plugin,
    "/api/toggle-component": Handler._handle_toggle_component,
    "/api/move-skill-to-scanned": Handler._handle_move_skill,
    "/api/toggle-model-invocation": Handler._handle_toggle_model_invocation,
    "/api/check-updates": Handler._handle_check_updates,
    "/api/update-plugin": Handler._handle_update_plugin,
    "/api/marketplace-add": Handler._handle_marketplace_add,
    "/api/marketplace-remove": Handler._handle_marketplace_remove,
    "/api/marketplace-update": Handler._handle_marketplace_update,
}


# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------

def _pull_marketplaces_background():
    """Pull marketplace repos in background so data is fresh on first page load."""
    try:
        result = refresh_and_check_updates()
        if result.get("pulled_changes"):
            print("Marketplace repos updated in background.")
    except Exception:
        pass


def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    threading.Thread(target=_pull_marketplaces_background, daemon=True).start()
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
