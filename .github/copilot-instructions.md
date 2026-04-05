# Blog Digest — Project Guidelines

## Overview

Single-file Python tool (`digest.py`) that fetches RSS feeds, summarizes articles via a local Ollama LLM, and renders dated static HTML pages using Jinja2 templates.

## Architecture

- **`digest.py`** — entry point and all logic (fetching, summarizing, rendering)
- **`config.yaml`** — feeds, AI backend settings, and summary tunables
- **`templates/`** — Jinja2 HTML templates (`daily.html`, `index.html`)
- **`docs/`** — output directory; `digest-YYYY-MM-DD-HH-MM.html` per run + `index.html`
- **`.article_cache.json`** — SHA-256-keyed cache to avoid re-summarizing articles

## Stack

- Python 3.11+, single virtualenv (`.venv/`)
- Dependencies: `feedparser`, `requests`, `beautifulsoup4`, `pyyaml`, `python-dotenv`, `jinja2`, `lxml`
- AI: Ollama (local); configured in `config.yaml`

## Conventions

- Keep all logic in `digest.py`; avoid splitting into multiple modules unless the file grows significantly
- Use the module-level `log` logger (`logging.getLogger("blog-digest")`) — no bare `print()` for diagnostics
- New AI providers should follow the existing `summarize_ollama` pattern
- Template changes go in `templates/`; never hardcode HTML in Python
- Output files always target `docs/` for GitHub Pages compatibility
- Run with: `python digest.py` (optionally `--debug` to clear cache)

## Build & Test

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python digest.py
```

No automated test suite yet — manual verification via generated HTML in `docs/`.
