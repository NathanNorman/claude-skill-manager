# claude-skill-manager

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![No Dependencies](https://img.shields.io/badge/dependencies-none-orange.svg)](#)

A local web UI for browsing, managing, and debugging your Claude Code skills.

> You have 30 skills installed and can't remember what half of them do. You can't tell which are active, which are broken, which are shadowing each other. You're over budget but don't know which skill is the culprit. **This fixes that.**

<!-- TODO: Add demo GIF here -->
<!-- ![demo](docs/demo.gif) -->

## Quick Start

```bash
python3 serve.py
# Open http://127.0.0.1:8421
```

That's it. No dependencies beyond Python stdlib.

## Features

| Feature | Description |
|---------|-------------|
| **Browse skills** | All installed skills grouped by plugin, with inline descriptions |
| **Toggle on/off** | Enable/disable individual skills or entire plugins via checkbox |
| **Plugin detail panel** | Click any plugin name to see install path, version, repo link, component counts |
| **Skill detail panel** | Click any skill to see status, type, budget cost, frontmatter, and warnings |
| **Component browsers** | Expandable sections for commands, agents, hooks, and MCP servers |
| **Validation warnings** | Flags missing frontmatter, missing name/description fields |
| **Duplicate detection** | Shows which copy wins when multiple plugins provide the same skill |
| **Budget bar** | Visual progress bar showing total description chars vs budget limit |
| **Version tracking** | Installed vs latest version comparison, one-click upgrade |
| **Marketplace management** | Add, remove, and pull updates from plugin marketplaces |
| **Install/uninstall** | Install available plugins or uninstall existing ones directly from the UI |
| **Search** | Filter skills by name or description across all plugins |
| **Filter views** | All, Available, Duplicates, Issues, Updates, Marketplaces |

## How It Works

`serve.py` is a single-file Python HTTP server (~1500 lines) that reads Claude Code's plugin cache, installed plugins list, marketplace directories, and local skills. It discovers all skills, commands, agents, hooks, and MCP servers, computes validation issues, duplicate resolution, budget usage, and version freshness, then serves a single-page `index.html` that renders everything client-side.

All operations (toggle, install, uninstall) shell out to `claude plugin ...` CLI commands.

## Configuration

The server reads from standard Claude Code paths:

- `~/.claude/plugins/` — installed plugins and cache
- `~/.claude/skills/` — local skills directory
- `~/.claude/settings.json` — disabled skills list and MCP servers

## Roadmap

- [ ] Dark mode
- [ ] Keyboard shortcuts (j/k navigation, enter to expand)
- [ ] Per-plugin budget breakdown in detail panel
- [ ] Export skill inventory as JSON
- [ ] Syntax highlighting for SKILL.md frontmatter
- [ ] Skill diff viewer (compare versions across plugins)
- [ ] Drag-and-drop skill priority ordering
- [ ] CLI mode (`python3 serve.py --json` for scripting)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

[MIT](LICENSE)
