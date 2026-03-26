# claude-skill-manager

A local web UI for browsing, managing, and debugging Claude Code skills, plugins, and components.

## What it does

- **Browse all installed skills** grouped by plugin, with search and filtering
- **Toggle skills on/off** with a checkbox (updates Claude Code's disabled list)
- **See plugin metadata** — install path, version, repo URL, component counts
- **Spot issues** — validation warnings, duplicate/shadowed skills, missing plugins
- **Track budget** — visual progress bar showing total description chars vs budget limit
- **Manage marketplaces** — add, remove, pull updates from plugin marketplaces
- **Install/uninstall plugins** directly from the UI

## Quick start

```bash
python3 ~/.claude/skill-manager/serve.py
# Open http://127.0.0.1:8421
```

Requires Python 3.10+ (no dependencies beyond stdlib).

## How it works

`serve.py` is a single-file HTTP server that:

1. Reads Claude Code's plugin cache, installed plugins list, marketplace directories, and local skills
2. Discovers all skills, commands, agents, hooks, and MCP servers in each plugin
3. Computes validation issues, duplicate resolution, budget usage, and version freshness
4. Serves a single-page `index.html` that renders everything client-side

All operations (toggle, install, uninstall, marketplace management) shell out to `claude plugin ...` CLI commands.

## Features

| Feature | Description |
|---------|-------------|
| **Skill toggle** | Enable/disable individual skills or entire plugins via checkbox |
| **Plugin detail panel** | Click plugin name to see install path, version, repo link, components |
| **Skill detail panel** | Click any skill to see status, type, budget cost, frontmatter, warnings |
| **Component browsers** | Expandable sections for commands, agents, hooks, MCP servers |
| **Validation** | Flags missing frontmatter, missing name/description fields |
| **Duplicate detection** | Shows which copy wins when multiple plugins provide the same skill |
| **Budget bar** | Total description chars vs configurable budget with color-coded bar |
| **Version tracking** | Shows installed vs latest version, one-click upgrade |
| **Marketplace management** | Add/remove/pull marketplace registries |
| **Search** | Filter skills by name or description across all plugins |
| **Filter views** | All, Available, Duplicates, Issues, Updates, Marketplaces |

## Configuration

The server reads from standard Claude Code paths:

- `~/.claude/plugins/` — installed plugins and cache
- `~/.claude/skills/` — local skills directory
- `~/.claude/settings.json` — disabled skills list and MCP servers

## License

MIT
