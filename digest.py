#!/usr/bin/env python3
"""
Blog Digest - Daily AI-powered blog summarizer.
Fetches RSS feeds, summarizes articles with local Ollama,
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
from jinja2 import Environment, FileSystemLoader

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


def fetch_with_backoff(url: str, max_retries: int = 3, timeout: int = 20, verify_ssl: bool = True) -> requests.Response:
    """HTTP GET with exponential backoff on transient failures."""
    delay = 2
    for attempt in range(max_retries):
        try:
            resp = requests.get(
                url,
                timeout=timeout,
                headers={"User-Agent": "BlogDigest/1.0 (RSS Aggregator)"},
                verify=verify_ssl,
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
        verify_ssl = feed_cfg.get("ssl_verify", True)
        log.info(f"Fetching feed: {name}")

        try:
            resp = fetch_with_backoff(url, verify_ssl=verify_ssl)
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


def deduplicate_labels(labels: list[str]) -> list[str]:
    """Remove similar/redundant labels using string similarity (Levenshtein-ish heuristic).
    Keeps the first occurrence of each label family."""
    if len(labels) <= 1:
        return labels
    
    def similarity(s1: str, s2: str) -> float:
        """Simple similarity metric: 1 - (edit distance / max length)."""
        s1_lower, s2_lower = s1.lower(), s2.lower()
        if s1_lower == s2_lower:
            return 1.0
        # Check if one is a substring of the other
        if s1_lower in s2_lower or s2_lower in s1_lower:
            return 0.8
        # Levenshtein-style: count matching characters
        matches = sum(1 for a, b in zip(s1_lower, s2_lower) if a == b)
        return matches / max(len(s1_lower), len(s2_lower))
    
    result = []
    threshold = 0.70  # labels more similar than this are considered duplicates
    for label in labels:
        is_similar = any(similarity(label, existing) > threshold for existing in result)
        if not is_similar:
            result.append(label)
    return result


def summarize_ollama(text: str, title: str, config: dict) -> tuple[str, list[str], bool, float | None, str] | None:
    """Summarize and extract labels using local Ollama in a single call.
    Returns (summary, labels, for_you, tokens_per_sec, title_translated) or None."""
    ollama_cfg = config["ai"]["ollama"]
    base_url = ollama_cfg["base_url"]
    model = ollama_cfg["model"]
    max_words = config["summary"]["max_summary_length"]
    max_labels = config["summary"].get("max_labels", 5)

    # Get user interests if available
    user_interests = config.get("user_profile", {}).get("interests", [])
    interests_text = ""
    if user_interests:
        interests_text = f"\nUser interests: {', '.join(user_interests)}"
        interests_text += "\nAfter LABELS, add FOR_YOU: on a new line with YES or NO."

    prompt = f"""Translate the title to English if needed, then summarize this blog post in {max_words} words or less.
Write plain flowing prose only — no bullet points, no numbered lists, no markdown, no headers, no bold or italic text.
Be concise and focus on the key takeaways. Write in English.

Start your response with: TITLE: [translated title]
Then the summary.
After the summary, on a new line, write LABELS: followed by up to {max_labels} short comma-separated keyword/topic labels in lowercase.
Labels should be DIVERSE and SPECIFIC (not variations or synonyms of each other).
Avoid redundant labels like both "kubernetes" and "k8s", or "containers" and "containerization".
Pick the most important, distinct topics the article covers.
Example format:
TITLE: Kubernetes Best Practices
Your summary here...
LABELS: kubernetes, security, performance-optimization{interests_text}

Original Title: {title}

Content:
{text[:4000]}

Response:"""

    try:
        timeout = ollama_cfg.get("timeout", 300)
        thinking = ollama_cfg.get("thinking", False)
        payload = {"model": model, "prompt": prompt, "stream": False}
        if not thinking:
            payload["think"] = False
        resp = requests.post(
            f"{base_url}/api/generate",
            json=payload,
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("response", "").strip()

        # Extract TITLE, summary, LABELS, and FOR_YOU
        title_en = title  # fallback to original
        summary = raw
        labels = []
        for_you = False

        # Extract title first
        for title_marker in ("TITLE:", "Title:", "title:"):
            if title_marker in raw:
                title_part = raw.split(title_marker, 1)[1]
                # Find where title ends (before the next line that looks like content)
                title_lines = title_part.split("\n")
                title_en = title_lines[0].strip().strip('"') if title_lines else title
                break

        # Extract summary and labels
        for marker in ("LABELS:", "Labels:", "labels:"):
            if marker in raw:
                parts = raw.split(marker, 1)
                summary = parts[0].strip()
                rest = parts[1].strip()
                # Remove TITLE line from summary if present
                for title_marker in ("TITLE:", "Title:", "title:"):
                    if title_marker in summary:
                        title_split = summary.split(title_marker, 1)
                        after_marker = title_split[1]
                        # Remove everything up to and including the first newline
                        if '\n' in after_marker:
                            summary = after_marker.split('\n', 1)[1]
                        else:
                            summary = ""
                        break
                summary = summary.strip()
                
                # Extract labels first
                for_you_marker = None
                for fy_marker in ("FOR_YOU:", "For_you:", "for_you:"):
                    if fy_marker in rest:
                        for_you_marker = fy_marker
                        break
                
                if for_you_marker:
                    label_part, for_you_part = rest.split(for_you_marker, 1)
                    label_str = label_part.strip()
                    for_you_str = for_you_part.strip().split()[0].upper()  # Get first word (YES/NO)
                    for_you = for_you_str.startswith("Y")
                else:
                    label_str = rest
                
                labels = [l.strip().lower().strip('"\' ') for l in label_str.split(",")]
                labels = [l for l in labels if l and len(l) < 40 and len(l.split()) <= 4]
                # Deduplicate similar labels before truncating
                labels = deduplicate_labels(labels)
                labels = labels[:max_labels]
                break
        else:
            # No LABELS marker found — strip TITLE and FOR_YOU lines from raw summary
            for title_marker in ("TITLE:", "Title:", "title:"):
                if title_marker in summary:
                    title_split = summary.split(title_marker, 1)
                    after_marker = title_split[1]
                    summary = after_marker.split('\n', 1)[1] if '\n' in after_marker else ""
                    break
            for fy_marker in ("FOR_YOU:", "For_you:", "for_you:"):
                if fy_marker in summary:
                    fy_split = summary.split(fy_marker, 1)
                    summary = fy_split[0].strip()
                    for_you_str = fy_split[1].strip().split()[0].upper() if fy_split[1].strip() else ""
                    for_you = for_you_str.startswith("Y")
                    break
            summary = summary.strip()

        tps = None
        eval_count = data.get("eval_count")
        eval_duration = data.get("eval_duration")  # nanoseconds
        if eval_count and eval_duration and eval_duration > 0:
            tps = round(eval_count / (eval_duration / 1e9), 1)
        return summary, labels, for_you, tps, title_en
    except Exception as e:
        log.warning(f"Ollama failed: {e}")
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


def summarize(text: str, title: str, config: dict) -> tuple[str, list[str], bool, float | None, str]:
    """Summarize with Ollama. Returns (summary, labels, for_you, tps, title_en)."""
    result = summarize_ollama(text, title, config)
    if result:
        summary, labels, for_you, tps, title_en = result
        log.info(f"  Summarized with Ollama: {title[:50]}")
        return summary, labels, for_you, tps, title_en

    return "Summary unavailable – Ollama failed.", [], False, None, title


def process_articles(articles: list[dict], config: dict) -> tuple[list[dict], dict]:
    """Summarize articles, using cache to skip already processed ones."""
    cache = load_cache()
    processed = []
    stats = {"ollama": 0, "failed": 0, "cached": 0, "tps_samples": []}

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

        # Summarize + extract labels (single LLM call)
        summary, labels, for_you, tps, title_en = summarize(text, article["title"], config)
        summary = clean_summary(summary)

        if tps is not None:
            stats["ollama"] += 1
            stats["tps_samples"].append(tps)
        else:
            stats["failed"] += 1

        result = {
            "id": aid,
            "feed": article["feed"],
            "title": title_en,
            "url": article["url"],
            "published": article["published"],
            "summary": summary,
            "labels": labels,
            "for_you": for_you,
        }

        cache[aid] = result
        processed.append(result)

    # Prune old cache entries – keep at least 7 days, or longer if max_age_hours exceeds that
    prune_hours = max(config["summary"]["max_age_hours"], 24 * 7)
    prune_cutoff = (datetime.now(timezone.utc) - timedelta(hours=prune_hours)).isoformat()
    cache = {k: v for k, v in cache.items() if v.get("published", "") >= prune_cutoff or not v.get("published")}
    save_cache(cache)

    return processed, stats


def generate_html(articles: list[dict], config: dict, run_stats: dict | None = None):
    """Generate the static HTML digest page."""
    env = Environment(loader=FileSystemLoader(BASE_DIR / "templates"))
    template = env.get_template("daily.html")

    # Group articles by feed
    by_feed = {}
    for a in articles:
        by_feed.setdefault(a["feed"], []).append(a)

    today = datetime.now().strftime("%A, %B %d, %Y")
    
    out_dir = Path(config["output"]["directory"])
    if not out_dir.is_absolute():
        out_dir = BASE_DIR / out_dir

    out_dir.mkdir(parents=True, exist_ok=True)
    
    # Build digest archive list for sidebar navigation
    today_str = datetime.now().strftime("%Y-%m-%d")
    reports = []
    for f in sorted(out_dir.glob("digest-????-??-??-??-??.html"), reverse=True):
        date_str = f.stem.replace("digest-", "")  # YYYY-MM-DD-HH-MM
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d-%H-%M")
            label = dt.strftime("%A, %B %d, %Y – %H:%M")
        except ValueError:
            label = date_str
        reports.append({
            "filename": f.name,
            "label": label,
            "date_short": date_str[:10],
            "is_today": date_str[:10] == today_str,
        })

    html = template.render(
        articles_by_feed=by_feed,
        total_articles=len(articles),
        date=today,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        run_stats=run_stats or {},
        reports=reports,
    )

    filename = f"digest-{datetime.now().strftime('%Y-%m-%d-%H-%M')}.html"
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
    for f in sorted(out_dir.glob("digest-????-??-??-??-??.html"), reverse=True):
        date_str = f.stem.replace("digest-", "")  # YYYY-MM-DD-HH-MM
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d-%H-%M")
            label = dt.strftime("%A, %B %d, %Y – %H:%M")
        except ValueError:
            label = date_str
        reports.append({
            "filename": f.name,
            "label": label,
            "is_today": date_str[:10] == today,
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
    parser.add_argument("--debug", action="store_true", help="Clear article cache before running (fresh summaries)")
    parser.add_argument("--model", type=str, help="Override Ollama model (requires --debug)")
    args = parser.parse_args()

    if args.model and not args.debug:
        parser.error("--model can only be used with --debug")

    if args.debug and CACHE_FILE.exists():
        CACHE_FILE.unlink()
        log.info("DEBUG mode – deleted article cache for fresh run")

    log.info("=" * 50)
    log.info("Blog Digest - Starting daily run")
    log.info("=" * 50)

    start_time = datetime.now()
    config = load_config()

    if args.model:
        config["ai"]["ollama"]["model"] = args.model
        log.info(f"Model override: {args.model}")

    # 1. Fetch feeds
    articles = fetch_feeds(config)
    if not articles:
        log.warning("No articles found. Exiting.")
        sys.exit(0)

    # 2. Summarize articles
    processed, proc_stats = process_articles(articles, config)
    log.info(f"Processed {len(processed)} articles")

    # Abort if too many summaries failed (don't publish broken digests)
    newly_summarized = proc_stats["ollama"] + proc_stats["failed"]
    if newly_summarized > 0:
        failure_ratio = proc_stats["failed"] / newly_summarized
        if failure_ratio > 0.5:
            log.error(
                f"Aborting: {proc_stats['failed']}/{newly_summarized} new summaries failed "
                f"({failure_ratio:.0%}). Not publishing a broken digest."
            )
            sys.exit(1)

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
        "failed_articles": proc_stats["failed"],
        "cached_articles": proc_stats["cached"],
        "avg_tps": avg_tps,
        "model": config["ai"]["ollama"]["model"],
    }

    # 3. Generate HTML
    output_dir = generate_html(processed, config, run_stats=run_stats)

    # 4. Push to GitHub Pages
    git_push(output_dir)

    log.info("Done!")


if __name__ == "__main__":
    main()
