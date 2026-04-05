# 📡 Blog Digest

A self-hosted, AI-powered daily blog summarizer. Fetches RSS feeds, summarizes articles with a local Ollama model, and generates a dated static HTML page for GitHub Pages.

## Features

- Fetches any number of RSS/Atom feeds
- Summarizes articles with a **local Ollama model** (no API costs)
- Generates a dark-mode static HTML file per run (e.g. `docs/digest-2026-04-04-14-30.html`)
- Caches processed articles to avoid re-summarizing on re-runs (`--debug` clears the cache for a fresh run)

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
ollama pull qwen2.5:1.5b

python digest.py
```

## Configuration

Edit `config.yaml`:

```yaml
feeds:
  - name: "My Blog"
    url: "https://example.com/feed.xml"

ai:
  ollama:
    base_url: "http://localhost:11434"
    model: "qwen2.5:1.5b"  # RPi 5 (4 GB RAM)
    # model: "llama3.2:3b" # RPi 5 (8 GB) or Mac
    thinking: false          # disable extended thinking for reasoning models

summary:
  max_articles_per_feed: 5
  max_age_hours: 24
  max_summary_length: 200   # words per article
```

## Output

Each run writes `docs/digest-YYYY-MM-DD-HH-MM.html` and regenerates `docs/index.html`. Multiple runs per day each produce their own file.

The script automatically commits and pushes `docs/` after each run, making each digest available at `https://<user>.github.io/<repo>/digest-YYYY-MM-DD-HH-MM.html` when GitHub Pages is pointed at the `docs/` folder.

Use `--debug` to clear the article cache so every article is re-summarized from scratch.

Use `--debug --model <name>` to override the Ollama model for a single run, useful for comparing models side by side.

## Project structure

```
blog-digest/
├── digest.py          # main script
├── config.yaml        # feeds and AI settings
├── requirements.txt   # Python dependencies
├── setup.sh           # RPi 5 setup & cron installer
├── templates/
│   ├── daily.html     # per-day digest template
│   └── index.html     # archive index template
└── docs/              # generated output (GitHub Pages)
```

## License

MIT – see [LICENSE](LICENSE).
