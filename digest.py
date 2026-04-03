#!/usr/bin/env python3
"""
Blog Digest - Daily AI-powered blog summarizer.
Fetches RSS feeds, summarizes articles with local AI (Ollama) or OpenRouter,
and generates a static HTML page for GitHub Pages.
"""

import os
import sys
import json
import time
import hashlib
import logging
import argparse
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
import feedparser
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from jinja2 import Environment, FileSystemLoader

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("blog-digest")

BASE_DIR = Path(__file__).parent
CACHE_FILE = BASE_DIR / ".article_cache.json"


def load_config() -> dict:
    with open(BASE_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def load_cache() -> dict:
    if CACHE_FILE.exists():
        with open(CACHE_FILE) as f:
            return json.load(f)
    return {}


def save_cache(cache: dict):
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def article_id(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def fetch_with_backoff(url: str, max_retries: int = 3, timeout: int = 20) -> requests.Response:
    """HTTP GET with exponential backoff on transient failures."""
    delay = 2
    for attempt in range(max_retries):
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": "BlogDigest/1.0 (RSS Aggregator)"},
            )
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                log.warning(f"Attempt {attempt + 1}/{max_retries} failed for {url}: {e}. Retrying in {delay}s…")
                time.sleep(delay)
                delay *= 2
            else:
                raise


def fetch_feeds(config: dict) -> list[dict]:
    """Fetch all RSS feeds and return list of articles."""
    articles = []
    max_age = timedelta(hours=config["summary"]["max_age_hours"])
    max_per_feed = config["summary"]["max_articles_per_feed"]
    cutoff = datetime.now(timezone.utc) - max_age

    for feed_cfg in config["feeds"]:
        name = feed_cfg["name"]
        url = feed_cfg["url"]
        log.info(f"Fetching feed: {name}")

        try:
            resp = fetch_with_backoff(url)
            feed = feedparser.parse(resp.content)
            count = 0
            for entry in feed.entries:
                if count >= max_per_feed:
                    break

                # Parse publication date
                published = None
                for date_field in ("published_parsed", "updated_parsed"):
                    parsed = getattr(entry, date_field, None)
                    if parsed:
                        published = datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
                        break

                if published and published < cutoff:
                    continue

                link = getattr(entry, "link", "")
                title = getattr(entry, "title", "No Title")

                # Get summary/content from feed
                content = ""
                if hasattr(entry, "content"):
                    content = entry.content[0].get("value", "")
                elif hasattr(entry, "summary"):
                    content = entry.summary

                articles.append({
                    "id": article_id(link),
                    "feed": name,
                    "title": title,
                    "url": link,
                    "published": published.isoformat() if published else "",
                    "content_raw": content,
                })
                count += 1

        except Exception as e:
            log.warning(f"Failed to fetch {name}: {e}")

    log.info(f"Fetched {len(articles)} articles from {len(config['feeds'])} feeds")
    return articles


def extract_article_text(html: str, max_chars: int = 5000) -> str:
    """Extract clean text from HTML content."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return text[:max_chars]


def fetch_full_article(url: str, max_chars: int = 5000) -> str:
    """Fetch and extract text from a full article URL."""
    try:
        resp = requests.get(url, timeout=15, headers={
            "User-Agent": "BlogDigest/1.0 (RSS Aggregator)"
        })
        resp.raise_for_status()
        return extract_article_text(resp.text, max_chars)
    except Exception as e:
        log.warning(f"Could not fetch full article {url}: {e}")
        return ""


def summarize_ollama(text: str, title: str, config: dict) -> tuple[str, float | None] | None:
    """Summarize using local Ollama. Returns (summary, tokens_per_sec) or None."""
    ollama_cfg = config["ai"]["ollama"]
    if not ollama_cfg.get("enabled"):
        return None

    base_url = ollama_cfg["base_url"]
    model = ollama_cfg["model"]
    max_words = config["summary"]["max_summary_length"]

    prompt = f"""Summarize this blog post in {max_words} words or less.
Write plain flowing prose only — no bullet points, no numbered lists, no markdown, no headers, no bold or italic text.
Be concise and focus on the key takeaways. Write in English.

Title: {title}

Content:
{text[:4000]}

Summary:"""

    try:
        timeout = ollama_cfg.get("timeout", 300)
        resp = requests.post(
            f"{base_url}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        summary = data.get("response", "").strip()
        tps = None
        eval_count = data.get("eval_count")
        eval_duration = data.get("eval_duration")  # nanoseconds
        if eval_count and eval_duration and eval_duration > 0:
            tps = round(eval_count / (eval_duration / 1e9), 1)
        return summary, tps
    except Exception as e:
        log.warning(f"Ollama failed: {e}")
        return None


def summarize_openrouter(text: str, title: str, config: dict) -> tuple[str, None] | None:
    """Summarize using OpenRouter API. Returns (summary, None) or None."""
    or_cfg = config["ai"]["openrouter"]
    if not or_cfg.get("enabled"):
        return None

    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        log.warning("OPENROUTER_API_KEY not set")
        return None

    model = or_cfg["model"]
    max_words = config["summary"]["max_summary_length"]

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": f"You summarize blog posts in {max_words} words or less. Write plain flowing prose only — no bullet points, no numbered lists, no markdown, no headers, no bold or italic text. Be concise, focus on key takeaways. Write in English.",
                    },
                    {
                        "role": "user",
                        "content": f"Title: {title}\n\nContent:\n{text[:4000]}",
                    },
                ],
            },
            timeout=60,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip(), None
    except Exception as e:
        log.warning(f"OpenRouter failed: {e}")
        return None


def clean_summary(text: str) -> str:
    """Convert any leftover markdown in LLM output to plain HTML."""
    import re
    # Remove leading label like "Summary:" or "**Summary:**"
    text = re.sub(r'^\s*\*{0,2}Summary:\*{0,2}\s*', '', text, flags=re.IGNORECASE)
    # Convert **bold** and __bold__
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'__(.+?)__', r'<strong>\1</strong>', text)
    # Convert *italic* and _italic_
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    text = re.sub(r'_(.+?)_', r'<em>\1</em>', text)
    # Convert markdown bullet/numbered list items into inline sentences
    text = re.sub(r'^[\s]*[-*]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[\s]*\d+\.\s+', '', text, flags=re.MULTILINE)
    # Collapse multiple blank lines into paragraph breaks; flatten single newlines to spaces
    paragraphs = [' '.join(p.split('\n')).strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    if len(paragraphs) > 1:
        return '</p><p class="summary">'.join(paragraphs)
    return paragraphs[0] if paragraphs else text


def summarize(text: str, title: str, config: dict) -> tuple[str, str, float | None]:
    """Try Ollama first, fall back to OpenRouter. Returns (summary, backend, tps)."""
    result = summarize_ollama(text, title, config)
    if result:
        summary, tps = result
        log.info(f"  Summarized with Ollama: {title[:50]}")
        return summary, "Ollama", tps

    result = summarize_openrouter(text, title, config)
    if result:
        summary, _ = result
        log.info(f"  Summarized with OpenRouter: {title[:50]}")
        return summary, "OpenRouter", None

    return "Summary unavailable – both Ollama and OpenRouter failed.", "none", None


def process_articles(articles: list[dict], config: dict) -> tuple[list[dict], dict]:
    """Summarize articles, using cache to skip already processed ones."""
    cache = load_cache()
    processed = []
    stats = {"ollama": 0, "openrouter": 0, "failed": 0, "cached": 0, "tps_samples": []}

    for article in articles:
        aid = article["id"]

        # Check cache
        if aid in cache:
            log.info(f"  Cached: {article['title'][:50]}")
            processed.append(cache[aid])
            stats["cached"] += 1
            continue

        # Get article text
        text = extract_article_text(article["content_raw"])
        if len(text) < 100:
            text = fetch_full_article(article["url"])

        if len(text) < 50:
            log.warning(f"  Skipping (no content): {article['title']}")
            stats["failed"] += 1
            continue

        # Summarize
        summary, backend, tps = summarize(text, article["title"], config)
        summary = clean_summary(summary)

        if backend == "Ollama":
            stats["ollama"] += 1
            if tps is not None:
                stats["tps_samples"].append(tps)
        elif backend == "OpenRouter":
            stats["openrouter"] += 1
        else:
            stats["failed"] += 1

        result = {
            "id": aid,
            "feed": article["feed"],
            "title": article["title"],
            "url": article["url"],
            "published": article["published"],
            "summary": summary,
            "backend": backend,
        }

        cache[aid] = result
        processed.append(result)

    # Prune old cache entries – keep at least 7 days, or longer if max_age_hours exceeds that
    prune_hours = max(config["summary"]["max_age_hours"], 24 * 7)
    prune_cutoff = (datetime.now(timezone.utc) - timedelta(hours=prune_hours)).isoformat()
    cache = {k: v for k, v in cache.items() if v.get("published", "") >= prune_cutoff or not v.get("published")}
    save_cache(cache)

    return processed, stats


def generate_html(articles: list[dict], config: dict, debug: bool = False, run_stats: dict | None = None):
    """Generate the static HTML digest page."""
    env = Environment(loader=FileSystemLoader(BASE_DIR / "templates"))
    template = env.get_template("daily.html")

    # Group articles by feed
    by_feed = {}
    for a in articles:
        by_feed.setdefault(a["feed"], []).append(a)

    today = datetime.now().strftime("%A, %B %d, %Y")

    html = template.render(
        articles_by_feed=by_feed,
        total_articles=len(articles),
        date=today,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        run_stats=run_stats or {},
    )

    out_dir = Path(config["output"]["directory"])
    if not out_dir.is_absolute():
        out_dir = BASE_DIR / out_dir

    out_dir.mkdir(parents=True, exist_ok=True)

    if debug:
        filename = "digest-debug.html"
        log.info("DEBUG mode – writing to digest-debug.html (not committed)")
    else:
        filename = f"digest-{datetime.now().strftime('%Y-%m-%d')}.html"
    (out_dir / filename).write_text(html)
    log.info(f"HTML written to {out_dir / filename}")

    generate_index(out_dir)
    return out_dir


def generate_index(out_dir: Path):
    """Regenerate index.html listing all digest reports, newest first."""
    env = Environment(loader=FileSystemLoader(BASE_DIR / "templates"))
    template = env.get_template("index.html")

    today = datetime.now().strftime("%Y-%m-%d")

    reports = []
    for f in sorted(out_dir.glob("digest-????-??-??.html"), reverse=True):
        date_str = f.stem.replace("digest-", "")  # YYYY-MM-DD
        try:
            label = datetime.strptime(date_str, "%Y-%m-%d").strftime("%A, %B %d, %Y")
        except ValueError:
            label = date_str
        reports.append({
            "filename": f.name,
            "label": label,
            "is_today": date_str == today,
        })

    html = template.render(
        reports=reports,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    (out_dir / "index.html").write_text(html)
    log.info(f"Index updated: {out_dir / 'index.html'} ({len(reports)} reports)")


def git_push(output_dir: Path):
    """Commit and push to GitHub Pages."""
    try:
        os.chdir(output_dir)
        today = datetime.now().strftime("%Y-%m-%d")

        subprocess.run(["git", "add", "."], check=True, capture_output=True)
        result = subprocess.run(
            ["git", "status", "--porcelain"], capture_output=True, text=True
        )

        if not result.stdout.strip():
            log.info("No changes to push")
            return

        subprocess.run(
            ["git", "commit", "-m", f"Daily digest {today}"],
            check=True, capture_output=True,
        )
        subprocess.run(["git", "push"], check=True, capture_output=True)
        log.info("Pushed to GitHub Pages")
    except subprocess.CalledProcessError as e:
        log.error(f"Git push failed: {e}")
    except Exception as e:
        log.error(f"Git error: {e}")


def main():
    parser = argparse.ArgumentParser(description="Blog Digest")
    parser.add_argument("--debug", action="store_true", help="Write to digest-debug.html and skip git push")
    args = parser.parse_args()

    log.info("=" * 50)
    log.info("Blog Digest - Starting daily run")
    log.info("=" * 50)

    start_time = datetime.now()
    config = load_config()

    # 1. Fetch feeds
    articles = fetch_feeds(config)
    if not articles:
        log.warning("No articles found. Exiting.")
        sys.exit(0)

    # 2. Summarize articles
    processed, proc_stats = process_articles(articles, config)
    log.info(f"Processed {len(processed)} articles")

    end_time = datetime.now()
    duration = end_time - start_time
    duration_str = f"{int(duration.total_seconds() // 60)}m {int(duration.total_seconds() % 60)}s"

    tps_samples = proc_stats.pop("tps_samples", [])
    avg_tps = round(sum(tps_samples) / len(tps_samples), 1) if tps_samples else None

    run_stats = {
        "started_at": start_time.strftime("%H:%M:%S"),
        "finished_at": end_time.strftime("%H:%M:%S"),
        "duration": duration_str,
        "ollama_articles": proc_stats["ollama"],
        "openrouter_articles": proc_stats["openrouter"],
        "failed_articles": proc_stats["failed"],
        "cached_articles": proc_stats["cached"],
        "avg_tps": avg_tps,
        "model_ollama": config["ai"]["ollama"]["model"] if config["ai"]["ollama"].get("enabled") else None,
        "model_openrouter": config["ai"]["openrouter"]["model"] if config["ai"]["openrouter"].get("enabled") else None,
    }

    # 3. Generate HTML
    output_dir = generate_html(processed, config, debug=args.debug, run_stats=run_stats)

    # 4. Push to GitHub Pages (skipped in debug mode)
    if not args.debug:
        git_push(output_dir)

    log.info("Done!")


if __name__ == "__main__":
    main()
