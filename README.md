# 📡 Blog Digest

A self-hosted, AI-powered daily blog summarizer. Fetches RSS feeds, summarizes articles with a local LLM (Ollama) or OpenRouter as fallback, and generates a dated HTML file per day.

## Features

- Fetches any number of RSS/Atom feeds
- Summarizes articles with a **local Ollama model** (no API costs)
- Falls back to **OpenRouter** if Ollama is unavailable
- Generates a dark-mode static HTML file per day (e.g. `output/digest-2026-04-03.html`)
- Caches processed articles to avoid re-summarizing on re-runs

## Requirements

- Python 3.11+
- [Ollama](https://ollama.com) (for local inference)
- Optional: [OpenRouter API key](https://openrouter.ai/keys) (fallback)

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
4. Create a `.env` file from `.env.example`
5. Register a daily cron job at 07:00

### Manual setup (Mac or any Linux)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Install Ollama: https://ollama.com
ollama pull llama3.2:3b

cp .env.example .env   # add OpenRouter key if needed
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
    enabled: true
    model: "llama3.2:3b"   # RPi 5 (4 GB RAM)
    # model: "llama3.1:8b" # RPi 5 (8 GB) or Mac M4

  openrouter:
    enabled: true           # fallback
    model: "google/gemini-2.0-flash-001"

summary:
  max_articles_per_feed: 5
  max_age_hours: 48
  max_summary_length: 200   # words per article
```

**API keys** go in `.env` (never committed):

```
OPENROUTER_API_KEY=sk-or-...
```

## Output

Each run creates one file: `output/digest-YYYY-MM-DD.html`. Old files are never overwritten, so you can catch up on missed days.

The script automatically commits and pushes the `output/` folder to git after each run. If `output/` is a GitHub Pages branch, each digest will be accessible at `https://<user>.github.io/<repo>/digest-YYYY-MM-DD.html`.

> To set this up: initialize a git repo inside `output/`, point it at your GitHub Pages branch, and ensure the machine running the cron job has push access (SSH key).

## Model recommendations

| Hardware | Model | RAM usage | Quality |
|---|---|---|---|
| RPi 5 (4 GB) | `llama3.2:3b` | ~2 GB | Good |
| RPi 5 (8 GB) | `llama3.1:8b` | ~5 GB | Better |
| Mac M4 | `llama3.1:8b` | ~5 GB | Best |

## Cron schedule

The setup script adds a cron entry at 07:00. To change it:

```bash
crontab -e
# 0 7 * * *  /path/to/.venv/bin/python /path/to/digest.py >> /path/to/digest.log 2>&1
```

## Project structure

```
blog-digest/
├── digest.py          # main script
├── config.yaml        # feeds and AI settings
├── requirements.txt   # Python dependencies
├── setup.sh           # RPi 5 setup & cron installer
├── .env.example       # API key template
├── templates/
│   └── daily.html     # Jinja2 HTML template
└── output/            # generated digests (gitignored)
```

## License

MIT – see [LICENSE](LICENSE).


## License

MIT – see [LICENSE](LICENSE).
