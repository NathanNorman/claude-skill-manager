# Contributing

Contributions are welcome! Here's how to get started.

## Development Setup

```bash
git clone https://github.com/NathanNorman/claude-skill-manager.git
cd claude-skill-manager
python3 serve.py
# Open http://127.0.0.1:8421
```

That's it. No build step, no package install, no virtual environment.

## Project Structure

```
serve.py      — Python HTTP server (API + static file serving)
index.html    — Single-page frontend (HTML + CSS + vanilla JS)
favicon.svg   — Browser tab icon
```

The entire app is two files. `serve.py` reads Claude Code's plugin directories and serves JSON APIs. `index.html` fetches that data and renders the UI client-side.

## How to Submit a Change

1. Fork the repo and clone your fork
2. Create a branch: `git checkout -b my-change`
3. Make your changes
4. Test: run `python3 serve.py`, open the UI, verify it works
5. Open a PR — describe what problem it solves

## What We're Looking For

- Bug fixes (always welcome)
- Issues labeled [`good first issue`](https://github.com/NathanNorman/claude-skill-manager/labels/good%20first%20issue)
- Features on the [roadmap](README.md#roadmap)
- UI/UX improvements

## Constraints

- **No external dependencies.** The stdlib-only design is intentional — it means zero install friction and no version conflicts. If your change requires `pip install`, it won't be accepted.
- **Single-page app.** Keep everything in `serve.py` + `index.html`. No build tools, no bundlers, no frameworks.
- **Python 3.10+.** Use modern Python features (match/case, type hints with `|` union) freely.

## Response Time

You can expect a review within 48 hours of opening a PR.
