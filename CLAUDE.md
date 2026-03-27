# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A local web UI for browsing, managing, and debugging Claude Code skills. Two-file architecture: `serve.py` (Python HTTP server + API) and `index.html` (single-page frontend with vanilla JS). No external dependencies — Python stdlib only.

## Running

```bash
python3 serve.py
# Open http://127.0.0.1:8421
```

CLI mode for cron use:
```bash
python3 serve.py apply-prefs
```

## Architecture

### serve.py (~1500 lines)

Single-file HTTP server built on `http.server.SimpleHTTPRequestHandler`. Three responsibilities:

1. **Discovery engine** — Scans `~/.claude/plugins/`, `~/.claude/skills/`, and marketplace directories to find all skills, commands, agents, hooks, and MCP servers. Mirrors Claude Code's actual loading rules:
   - `plugin.json` with `skills[]` field → those paths loaded (supplemental to default `skills/` dir)
   - `plugin.json` without `skills` field → auto-discover `skills/*/SKILL.md`
   - `plugin.json` with `skills: []` (empty) → nothing loaded
   - No `plugin.json` → flat layout discovery (top-level subdirs)

2. **JSON API** — GET endpoints (`/api/skills`, `/api/marketplaces`) return discovery data. POST endpoints handle mutations (toggle, install, uninstall, update, marketplace management).

3. **Static file server** — Serves `index.html` and assets.

Key data flow: `get_all_data()` assembles everything → groups into `installed` (cache + local + direct plugins) and `available` (marketplace plugins not yet installed) → computes duplicate resolution, budget usage, validation issues, version freshness.

### index.html

Single-page app. Fetches `/api/skills` on load, renders everything client-side. No framework, no build step. Uses CSS custom properties for theming. Fonts: JetBrains Mono (monospace elements), Effra/Source Sans 3 (body text).

### Key paths read by the server

- `~/.claude/plugins/cache/` — installed plugin snapshots
- `~/.claude/plugins/marketplaces/` — git clones of marketplace repos
- `~/.claude/plugins/installed_plugins.json` — maps plugin IDs to install paths
- `~/.claude/plugins/known_marketplaces.json` — registered marketplace repos
- `~/.claude/skills/` — local user skills
- `~/.claude/settings.json` — disabled skills, MCP servers, env vars

### Preferences

`preferences.json` (in repo root, gitignored) tracks which skills the user has disabled. Keyed by directory path relative to `~/.claude/`. The `apply-prefs` CLI mode re-applies these after marketplace syncs re-enable disabled skills.

## Constraints

- **No external dependencies.** No pip install, no node_modules. Python stdlib only.
- **Two files.** All backend logic in `serve.py`, all frontend in `index.html`.
- **Python 3.10+.** Modern features like `X | Y` type unions and match/case are fine.
- **Mutations shell out to `claude plugin ...` CLI** for install/uninstall/update. Toggle works via filesystem rename (`SKILL.md` <-> `SKILL.md.disabled`).

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/skills` | Full discovery data (installed + available + stats) |
| GET | `/api/marketplaces` | Marketplace metadata with git info |
| POST | `/api/toggle` | Enable/disable a skill (body: `{path, enabled}`) |
| POST | `/api/install` | Install a plugin (body: `{plugin_id}`) |
| POST | `/api/uninstall` | Uninstall a plugin (body: `{plugin_id}`) |
| POST | `/api/check-updates` | Git-pull marketplaces and compare versions |
| POST | `/api/update-plugin` | Re-install a plugin to update it |
| POST | `/api/toggle-component` | Enable/disable a command or agent |
| POST | `/api/toggle-model-invocation` | Toggle `disable-model-invocation` frontmatter |
| POST | `/api/move-skill-to-scanned` | Copy skill to `~/.claude/skills/` |
| POST | `/api/remove-missing` | Clean stale entry from `installed_plugins.json` |
| POST | `/api/remove-local-plugin` | Archive a local plugin to `_archived/` |
| POST | `/api/marketplace-add` | Add a marketplace repo |
| POST | `/api/marketplace-remove` | Remove a marketplace repo |
| POST | `/api/marketplace-update` | Pull latest for a marketplace |

## Security Notes

- All POST path operations validate that resolved paths are under `~/.claude/` before acting.
- `remove-local-plugin` rejects names containing `/` or `..`.
- Server binds to `127.0.0.1` only (not `0.0.0.0`).
