# Blog Digest

A self-hosted, AI-powered daily blog digest. It fetches RSS feeds, summarizes and scores articles with a local Ollama model, and generates dated static HTML pages for GitHub Pages.

## Features

- Fetches any number of RSS/Atom feeds
- Summarizes articles with a **local Ollama model** (no API costs)
- Scores articles and splits low-scoring ones into a separate section
- Tags matching articles as **For you** based on `user_profile.interests`
- Deduplicates similar keyword labels
- Supports per-feed TLS overrides with `ssl_verify: false`
- Optional OpenTelemetry traces and metrics export via OTLP (Grafana Alloy compatible)
- Writes a dated static HTML file per run (e.g. `docs/digest-2026-04-04-14-30.html`)
- Caches processed articles to avoid re-summarizing on re-runs; `--debug` clears the cache for a fresh run
- Automatically commits and pushes the generated `docs/` output

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com)

## Quick Start

### Raspberry Pi 5

```bash
git clone <your-repo-url> blog-digest && cd blog-digest
bash setup.sh
```

`setup.sh` will:
1. Install system packages (`python3`, `curl`, `git`)
2. Create a Python virtualenv and install dependencies
3. Install Ollama and pull the configured model
4. Register a daily cron job at the time of your choise

### Manual setup (Mac or any Linux)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Install Ollama: https://ollama.com
ollama pull llama3.2:3b

python digest.py
```

Use `python digest.py --debug` for a fully fresh run, and `python digest.py --debug --model <name>` to compare a different Ollama model for that run.

## Config Guide

The runtime defaults below come from `digest.py`. If a field says “required”, the script reads it directly from `config.yaml` and does not provide a Python fallback.

### Feeds

| Parameter | Purpose | Default in `digest.py` |
|---|---|---|
| `feeds` | List of RSS or Atom feed definitions to fetch | Required; no Python fallback |
| `feeds[].name` | Display name for the feed section and article grouping | Required; no Python fallback |
| `feeds[].url` | Feed URL to request | Required; no Python fallback |
| `feeds[].ssl_verify` | Enables or disables TLS certificate verification for that feed | `true` |

### AI.Ollama

| Parameter | Purpose | Default in `digest.py` |
|---|---|---|
| `ai.ollama.base_url` | Base URL of the local Ollama server | Required; no Python fallback |
| `ai.ollama.model` | Ollama model used for summarization and scoring | Required; no Python fallback |
| `ai.ollama.timeout` | Request timeout in seconds for Ollama calls | `300` |
| `ai.ollama.thinking` | Enables extended-thinking mode for summarization | `false` |

### Scoring

| Parameter | Purpose | Default in `digest.py` |
|---|---|---|
| `scoring.enabled` | Turns the scoring pass on or off | `false` |
| `scoring.threshold` | Score cutoff for the Low Scores section | `7` |
| `scoring.min_threshold` | Safety floor for allowed scores | No runtime use in `digest.py` yet |
| `scoring.thinking` | Enables extended-thinking mode for scoring | `true` |

### Summary

| Parameter | Purpose | Default in `digest.py` |
|---|---|---|
| `summary.max_articles_per_feed` | Maximum number of articles kept from each feed | Required; no Python fallback |
| `summary.max_age_hours` | Maximum article age before it is skipped | Required; no Python fallback |
| `summary.max_summary_length` | Maximum summary length in words | Required; no Python fallback |
| `summary.max_labels` | Maximum number of labels kept per article | `5` |

### User Profile

| Parameter | Purpose | Default in `digest.py` |
|---|---|---|
| `user_profile.interests` | Keywords used to mark articles as For you | `[]` |

### Output

| Parameter | Purpose | Default in `digest.py` |
|---|---|---|
| `output.directory` | Directory where generated HTML is written | Required; no Python fallback |

### Schedule

| Parameter | Purpose | Default in `digest.py` |
|---|---|---|
| `schedule.cron` | Cron expression used by `setup.sh` for automation | Not used by `digest.py` |
| `schedule.timezone` | Timezone used by `setup.sh` for automation | Not used by `digest.py` |

### Observability

| Parameter | Purpose | Default in `digest.py` |
|---|---|---|
| `observability.enabled` | Enables OpenTelemetry instrumentation and export | `false` |
| `observability.otlp_endpoint` | OTLP HTTP base endpoint (e.g. Alloy receiver) | `http://localhost:4318` |
| `observability.service_name` | Service name attached to traces/metrics | `blog-digest` |
| `observability.service_version` | Service version resource attribute | `0.1.0` |
| `observability.timeout` | OTLP exporter timeout in seconds | `15` |
| `observability.metrics_export_interval_ms` | Metric export interval in milliseconds | `30000` |
| `observability.instrument_requests` | Auto-instruments outgoing HTTP requests | `true` |
| `observability.headers` | Optional OTLP headers (for auth/tenant routing) | `{}` |
| `observability.resource_attributes` | Extra resource attributes added to telemetry | `{}` |

#### Exported metrics

When `observability.enabled` is true, the app exports these custom metrics:

| Metric | Type |
|---|---|
| `blog_digest_runs_total` | Counter |
| `blog_digest_articles_processed_total` | Counter |
| `blog_digest_articles_failed_total` | Counter |
| `blog_digest_cache_hits_total` | Counter |
| `blog_digest_run_duration_seconds` | Histogram |
| `blog_digest_summarize_duration_seconds` | Histogram |
| `blog_digest_score_duration_seconds` | Histogram |
| `blog_digest_fetch_duration_seconds` | Histogram |

If `observability.instrument_requests` is enabled, OpenTelemetry HTTP client metrics and spans may also be emitted for outbound `requests` calls.

## Output

Each run writes `docs/digest-YYYY-MM-DD-HH-MM.html` and regenerates `docs/index.html`. Multiple runs per day each produce their own file.

The script automatically commits and pushes `docs/` after each run, making each digest available at `https://<user>.github.io/<repo>/digest-YYYY-MM-DD-HH-MM.html` when GitHub Pages is pointed at the `docs/` folder.

Use `--debug` to clear the article cache so every article is re-summarized from scratch.

Use `--debug --model <name>` to override the Ollama model for a single run, useful for comparing models side by side.

## CLI

```bash
python digest.py [--debug] [--model MODEL]
```

- `--debug` clears the article cache and sets the logger to DEBUG
- `--model MODEL` overrides the Ollama model for that run, and requires `--debug`

## Project structure

```
blog-digest/
├── digest.py          # main script
├── config.yaml        # feeds, AI settings, scoring, schedule
├── requirements.txt   # Python dependencies
├── setup.sh           # RPi 5 setup & cron installer
├── templates/
│   ├── daily.html     # per-day digest template
│   └── index.html     # archive index template
└── docs/              # generated output (GitHub Pages)
```

## License

MIT – see [LICENSE](LICENSE).
