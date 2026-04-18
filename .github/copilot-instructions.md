# Blog Digest — Project Guidelines

## Overview

Single-file Python tool (`digest.py`) that fetches RSS feeds, summarizes and scores articles via a local Ollama LLM, and renders dated static HTML pages using Jinja2 templates.

## Architecture

- **`digest.py`** — entry point and all logic (fetching, summarizing, scoring, rendering, git push)
- **`config.yaml`** — feeds, AI backend settings, scoring config, summary tunables, user profile, schedule, and output path
- **`templates/`** — Jinja2 HTML templates (`daily.html`, `index.html`)
- **`docs/`** — output directory; `digest-YYYY-MM-DD-HH-MM.html` per run + `index.html`
- **`.article_cache.json`** — SHA-256-keyed cache to avoid re-summarizing articles

## Stack

- Python 3.11+, single virtualenv (`.venv/`)
- Dependencies: `feedparser`, `requests`, `beautifulsoup4`, `pyyaml`, `python-dotenv`, `jinja2`, `lxml`
- AI: Ollama (local); configured in `config.yaml`

## Key Features

- **Scoring pipeline** — `score_article()` rates each article 1–10 using Ollama; articles below `scoring.threshold` are separated into a "Low Scores" section. Configurable via `scoring:` in `config.yaml`.
- **Thinking mode** — both summarization (`ai.ollama.thinking`) and scoring (`scoring.thinking`) support Ollama extended-thinking for better reasoning.
- **"For You" tagging** — articles matching interests listed under `user_profile.interests` in `config.yaml` are tagged.
- **Label deduplication** — `deduplicate_labels()` removes near-duplicate keyword labels using substring/similarity heuristics.
- **Per-feed SSL override** — set `ssl_verify: false` on a feed entry to skip TLS verification for that feed.
- **Structured JSON logging** — all log output is emitted as JSON lines via `JsonFormatter`; use the module-level `log` logger, never bare `print()`.
- **Auto git push** — after rendering, the script commits and pushes `docs/` automatically.
- **Deduplication across runs** — cached articles first seen on a previous day are skipped to avoid re-publishing stale content.

## CLI

```
python digest.py [--debug] [--model MODEL]
```

- `--debug` — clears the article cache and sets log level to DEBUG for a fully fresh run
- `--model MODEL` — overrides the Ollama model for this run (requires `--debug`)

## Conventions

- Keep all logic in `digest.py`; avoid splitting into multiple modules unless the file grows significantly
- Use the module-level `log` logger (`logging.getLogger("blog-digest")`) — no bare `print()` for diagnostics
- New AI providers should follow the existing `summarize_ollama` pattern
- Template changes go in `templates/`; never hardcode HTML in Python
- Output files always target `docs/` for GitHub Pages compatibility

## config.yaml Sections

| Section | Purpose |
|---|---|
| `feeds` | List of RSS feed URLs; supports `ssl_verify: false` per entry |
| `ai.ollama` | `base_url`, `model`, `timeout`, `thinking` |
| `scoring` | `enabled`, `threshold`, `min_threshold`, `thinking` |
| `summary` | `max_articles_per_feed`, `max_age_hours`, `max_summary_length`, `max_labels` |
| `user_profile.interests` | Keywords for "For You" article tagging |
| `output.directory` | Output path (default `./docs`) |
| `schedule.cron` / `schedule.timezone` | Cron expression for scheduled runs |

## Build & Test

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python digest.py
```

No automated test suite yet — manual verification via generated HTML in `docs/`.
