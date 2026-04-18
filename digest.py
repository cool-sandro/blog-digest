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
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
import feedparser
import requests
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader
import urllib3

# Suppress InsecureRequestWarning for feeds with ssl_verify=false
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single JSON line, merging structured payloads."""

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        try:
            payload = json.loads(msg)
        except (json.JSONDecodeError, TypeError):
            payload = {"message": msg}
        payload["timestamp"] = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d %H:%M:%S")
        payload["level"] = record.levelname
        # Move timestamp and level to front for readability
        ordered = {"timestamp": payload.pop("timestamp"), "level": payload.pop("level")}
        ordered.update(payload)
        return json.dumps(ordered)


_handler = logging.StreamHandler()
_handler.setFormatter(JsonFormatter())
logging.root.setLevel(logging.INFO)
logging.root.handlers = [_handler]

log = logging.getLogger("blog-digest")
log.setLevel(logging.INFO)

# Suppress noisy DEBUG output from third-party libraries at module load time
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)

BASE_DIR = Path(__file__).parent
CACHE_FILE = BASE_DIR / ".article_cache.json"
REQUEST_HEADERS = {"User-Agent": "BlogDigest/1.0 (RSS Aggregator)"}

TITLE_MARKERS = ("TITLE:", "Title:", "title:")
LABEL_MARKERS = ("LABELS:", "Labels:", "labels:")
FOR_YOU_MARKERS = (
    "FOR_YOU:",
    "For_you:",
    "for_you:",
    "For you:",
    "For You:",
    "for you:",
)

TRACER = None
METRICS: dict[str, object] = {}


def telemetry_span(name: str):
    """Return a context manager for an OpenTelemetry span if enabled."""
    if TRACER:
        return TRACER.start_as_current_span(name)
    return nullcontext()


def telemetry_counter_add(metric_name: str, value: int, attributes: dict | None = None):
    """Safely increment an OTel counter if telemetry is enabled."""
    metric = METRICS.get(metric_name)
    if metric:
        try:
            metric.add(value, attributes=attributes or {})
        except Exception:
            pass


def telemetry_histogram_record(metric_name: str, value: float, attributes: dict | None = None):
    """Safely record a value to an OTel histogram if telemetry is enabled."""
    metric = METRICS.get(metric_name)
    if metric:
        try:
            metric.record(value, attributes=attributes or {})
        except Exception:
            pass


def setup_observability(config: dict):
    """Initialize OpenTelemetry trace/metric export to an OTLP endpoint (e.g. Grafana Alloy)."""
    global TRACER, METRICS

    obs_cfg = config.get("observability", {})
    if not obs_cfg.get("enabled", False):
        return

    try:
        from opentelemetry import metrics, trace
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.requests import RequestsInstrumentor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        service_name = obs_cfg.get("service_name", "blog-digest")
        service_version = obs_cfg.get("service_version", "0.1.0")
        endpoint = obs_cfg.get("otlp_endpoint", "http://localhost:4318").rstrip("/")
        timeout = obs_cfg.get("timeout", 15)
        metric_interval_ms = obs_cfg.get("metrics_export_interval_ms", 30000)
        headers = obs_cfg.get("headers", {})

        resource_attrs = {
            SERVICE_NAME: service_name,
            SERVICE_VERSION: service_version,
        }
        resource_attrs.update(obs_cfg.get("resource_attributes", {}))
        resource = Resource.create(resource_attrs)

        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(
                    endpoint=f"{endpoint}/v1/traces",
                    timeout=timeout,
                    headers=headers,
                )
            )
        )
        trace.set_tracer_provider(tracer_provider)
        TRACER = trace.get_tracer(service_name)

        metric_reader = PeriodicExportingMetricReader(
            exporter=OTLPMetricExporter(
                endpoint=f"{endpoint}/v1/metrics",
                timeout=timeout,
                headers=headers,
            ),
            export_interval_millis=metric_interval_ms,
        )
        metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[metric_reader]))
        meter = metrics.get_meter(service_name)

        METRICS = {
            "runs_total": meter.create_counter(
                "blog_digest_runs_total", description="Number of digest runs"
            ),
            "articles_processed_total": meter.create_counter(
                "blog_digest_articles_processed_total", description="Number of processed articles"
            ),
            "articles_failed_total": meter.create_counter(
                "blog_digest_articles_failed_total", description="Number of failed articles"
            ),
            "cache_hits_total": meter.create_counter(
                "blog_digest_cache_hits_total", description="Number of cache hits"
            ),
            "run_duration_seconds": meter.create_histogram(
                "blog_digest_run_duration_seconds", unit="s", description="End-to-end run duration"
            ),
            "summarize_duration_seconds": meter.create_histogram(
                "blog_digest_summarize_duration_seconds", unit="s", description="Summarization latency"
            ),
            "score_duration_seconds": meter.create_histogram(
                "blog_digest_score_duration_seconds", unit="s", description="Scoring latency"
            ),
            "fetch_duration_seconds": meter.create_histogram(
                "blog_digest_fetch_duration_seconds", unit="s", description="Feed fetch latency"
            ),
        }

        if obs_cfg.get("instrument_requests", True):
            RequestsInstrumentor().instrument()

        # Re-silence urllib3 after OTel instrumentation may have reset its level
        logging.getLogger("urllib3").setLevel(logging.WARNING)

        log.info(json.dumps({
            "step": "OTEL",
            "status": "enabled",
            "otlp_endpoint": endpoint,
            "service_name": service_name,
        }))
    except Exception as e:
        log.warning(json.dumps({"step": "OTEL", "status": "failed", "error": str(e)}))


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


def split_on_markers(text: str, markers: tuple[str, ...]) -> tuple[str, str] | None:
    """Split text at the first configured marker, preserving marker priority order."""
    for marker in markers:
        if marker in text:
            return text.split(marker, 1)
    return None


def extract_title_from_raw(raw: str, fallback_title: str) -> str:
    """Extract translated title from model output, falling back to original title."""
    split = split_on_markers(raw, TITLE_MARKERS)
    if not split:
        return fallback_title

    _, title_part = split
    for line in title_part.split("\n"):
        cleaned = line.strip().strip('"')
        if cleaned:
            return cleaned
    return fallback_title


def strip_title_preamble(text: str) -> str:
    """Remove TITLE preamble from text while handling title-on-next-line responses."""
    split = split_on_markers(text, TITLE_MARKERS)
    if not split:
        return text

    _, after_marker = split
    if "\n" not in after_marker:
        return ""

    first_line, rest = after_marker.split("\n", 1)
    if not first_line.strip():
        return rest.split("\n", 1)[1] if "\n" in rest else ""
    return rest


def split_for_you(text: str) -> tuple[str, bool]:
    """Split FOR_YOU flag from trailing text and return cleaned content + boolean."""
    split = split_on_markers(text, FOR_YOU_MARKERS)
    if not split:
        return text, False

    content, for_you_part = split
    token = for_you_part.strip().split()[0].upper() if for_you_part.strip() else ""
    return content.strip(), token.startswith("Y")


def parse_labels(label_str: str, max_labels: int, user_interests: list[str]) -> list[str]:
    """Normalize, filter, and deduplicate model-produced labels."""
    labels = [l.strip().lower().strip('"\' ') for l in label_str.split(",")]
    labels = [l.split("\n")[0].strip() for l in labels]
    labels = [l for l in labels if l and len(l) < 40 and len(l.split()) <= 4]
    labels = deduplicate_labels(labels)[:max_labels]

    interests_lower = {i.lower() for i in user_interests}
    if labels and interests_lower and all(l in interests_lower for l in labels):
        return []
    return labels


def parse_entry_published(entry) -> datetime | None:
    """Best-effort parse for published timestamps from feed entries."""
    for date_field in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, date_field, None)
        if parsed:
            return datetime.fromtimestamp(time.mktime(parsed), tz=timezone.utc)
    return None


def extract_entry_content(entry) -> str:
    """Return best available raw article content from a feed entry."""
    if hasattr(entry, "content") and entry.content:
        return entry.content[0].get("value", "")
    if hasattr(entry, "summary"):
        return entry.summary
    return ""


def group_articles_by_feed(articles: list[dict]) -> dict[str, list[dict]]:
    """Group article dictionaries by feed name."""
    grouped: dict[str, list[dict]] = {}
    for article in articles:
        grouped.setdefault(article["feed"], []).append(article)
    return grouped


def build_reports(out_dir: Path, today: str) -> list[dict]:
    """Build report metadata list from digest files, newest first."""
    reports = []
    for report_file in sorted(out_dir.glob("digest-????-??-??-??-??.html"), reverse=True):
        date_str = report_file.stem.replace("digest-", "")  # YYYY-MM-DD-HH-MM
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d-%H-%M")
            label = dt.strftime("%A, %B %d, %Y – %H:%M")
        except ValueError:
            label = date_str

        reports.append({
            "filename": report_file.name,
            "label": label,
            "date_short": date_str[:10],
            "is_today": date_str[:10] == today,
        })
    return reports


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
                headers=REQUEST_HEADERS,
                verify=verify_ssl,
            )
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            if attempt < max_retries - 1:
                log.warning(json.dumps({"step": "FETCH", "attempt": attempt + 1, "max_retries": max_retries, "url": url, "error": str(e), "retry_delay": delay}))
                time.sleep(delay)
                delay *= 2
            else:
                raise


def fetch_feeds(config: dict) -> list[dict]:
    """Fetch all RSS feeds and return list of articles."""
    with telemetry_span("fetch_feeds"):
        fn_start = time.time()
        articles = []
        max_age = timedelta(hours=config["summary"]["max_age_hours"])
        max_per_feed = config["summary"]["max_articles_per_feed"]
        cutoff = datetime.now(timezone.utc) - max_age

        for feed_cfg in config["feeds"]:
            name = feed_cfg["name"]
            url = feed_cfg["url"]
            verify_ssl = feed_cfg.get("ssl_verify", True)
            log.info(json.dumps({"step": "FETCH", "feed": name}))

            try:
                feed_start = time.time()
                resp = fetch_with_backoff(url, verify_ssl=verify_ssl)
                feed = feedparser.parse(resp.content)
                count = 0
                for entry in feed.entries:
                    if count >= max_per_feed:
                        break

                    published = parse_entry_published(entry)

                    if published and published < cutoff:
                        continue

                    link = getattr(entry, "link", "")
                    title = getattr(entry, "title", "No Title")
                    content = extract_entry_content(entry)

                    articles.append({
                        "id": article_id(link),
                        "feed": name,
                        "title": title,
                        "url": link,
                        "published": published.isoformat() if published else "",
                        "content_raw": content,
                    })
                    count += 1

                telemetry_histogram_record(
                    "fetch_duration_seconds",
                    time.time() - feed_start,
                    {"feed": name, "status": "ok"},
                )

            except Exception as e:
                telemetry_histogram_record(
                    "fetch_duration_seconds",
                    time.time() - feed_start,
                    {"feed": name, "status": "error"},
                )
                log.warning(json.dumps({"step": "FETCH", "feed": name, "error": str(e)}))

        log.info(json.dumps({"step": "FETCH", "status": "completed", "articles": len(articles), "feeds": len(config['feeds'])}))
        telemetry_histogram_record(
            "fetch_duration_seconds",
            time.time() - fn_start,
            {"scope": "all_feeds"},
        )
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
        resp = requests.get(url, timeout=15, headers=REQUEST_HEADERS)
        resp.raise_for_status()
        return extract_article_text(resp.text, max_chars)
    except Exception as e:
        log.warning(json.dumps({"step": "FETCH", "operation": "full_article", "url": url, "error": str(e)}))
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
    fn_start = time.time()
    ollama_cfg = config["ai"]["ollama"]
    base_url = ollama_cfg["base_url"]
    model = ollama_cfg["model"]
    max_words = config["summary"]["max_summary_length"]
    max_labels = config["summary"].get("max_labels", 5)

    # Get user interests if available
    user_interests = config.get("user_profile", {}).get("interests", [])
    interests_text = ""
    for_you_format = ""
    for_you_example = ""
    if user_interests:
        interests_text = f"\n\nReader interests (for FOR_YOU only, not labels): {', '.join(user_interests)}"
        for_you_format = "\nAfter LABELS, on a new line write FOR_YOU: YES or NO (based on reader interests listed below)."
        for_you_example = "\nFOR_YOU: YES"

    prompt = f"""Translate the title to English if needed, then summarize this blog post in {max_words} words or less.
Write plain flowing prose only — no bullet points, no numbered lists, no markdown, no headers, no bold or italic text.
Be concise and focus on the key takeaways. Write in English.

Start your response with: TITLE: [translated title]
Then the summary.
After the summary, on a new line, write LABELS: followed by up to {max_labels} short comma-separated keyword/topic labels in lowercase.
Labels should be DIVERSE and SPECIFIC (not variations or synonyms of each other).
Avoid redundant labels like both "kubernetes" and "k8s", or "containers" and "containerization".
Pick the most important, distinct topics the article covers.{for_you_format}
Example format:
TITLE: Kubernetes Best Practices
Your summary here...
LABELS: kubernetes, security, performance-optimization{for_you_example}{interests_text}

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
        
        start_time = time.time()
        log.debug(json.dumps({"step": "SUMMARIZE", "status": "starting", "title": title[:50]}))
        resp = requests.post(
            f"{base_url}/api/generate",
            json=payload,
            timeout=timeout,
        )
        elapsed = time.time() - start_time
        log.debug(json.dumps({"step": "SUMMARIZE", "status": "response_received", "elapsed_sec": round(elapsed, 2), "title": title[:50]}))
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("response", "").strip()

        # Extract TITLE, summary, LABELS, and FOR_YOU
        title_en = extract_title_from_raw(raw, title)
        summary = raw
        labels = []
        for_you = False

        # Extract summary and labels
        split = split_on_markers(raw, LABEL_MARKERS)
        if split:
            summary_part, rest = split
            summary = strip_title_preamble(summary_part).strip()
            label_str, for_you = split_for_you(rest.strip())
            labels = parse_labels(label_str, max_labels, user_interests)
        else:
            # No LABELS marker found — strip TITLE and FOR_YOU lines from raw summary
            summary = strip_title_preamble(summary)
            summary, for_you = split_for_you(summary)
            summary = summary.strip()

        tps = None
        eval_count = data.get("eval_count")
        eval_duration = data.get("eval_duration")  # nanoseconds
        if eval_count and eval_duration and eval_duration > 0:
            tps = round(eval_count / (eval_duration / 1e9), 1)
        
        total_time = time.time() - fn_start
        telemetry_histogram_record(
            "summarize_duration_seconds",
            total_time,
            {"status": "ok", "model": model},
        )
        log.debug(json.dumps({"step": "SUMMARIZE", "status": "completed", "elapsed_sec": round(total_time, 2), "tps": tps, "title": title[:50]}))
        return summary, labels, for_you, tps, title_en
    except Exception as e:
        telemetry_histogram_record(
            "summarize_duration_seconds",
            time.time() - fn_start,
            {"status": "error", "model": model},
        )
        log.warning(json.dumps({"step": "SUMMARIZE", "status": "failed", "error": str(e)}))
        return None


def clean_summary(text: str) -> str:
    """Convert any leftover markdown in LLM output to plain HTML."""
    import re

    def strip_quotes(value: str) -> str:
        return re.sub(r'^[\s"\u201c\u201d\u2018\u2019]+', '', value)

    text = strip_quotes(text)

    for pattern in (
        r'(?i)^the title translates to\s+[^\n.!?]*[.!?]\s*',
        r'(?i)^the title\s+[^\n]*translates to[^\n]*[.!?]\s*',
        r'(?i)^title translation[:\s][^\n]*\n?',
        r'(?i)^the translated title is[:\s][^\n]*\n?',
        r'(?i)^here is a translation of the title[^\n]*\n?',
        r'(?i)^translation:\s*[^\n]*\n?',
        r'(?i)^\s*\*{0,2}summary:\*{0,2}\s*',
        r'(?i)^the summary is as follows[:\s]*\n?',
        r'(?i)^here is a summary[:\s]*\n?',
    ):
        text = re.sub(pattern, '', text)

    for pattern in (
        r'(?i)you can access this information[^.!?]*[.!?]\s*',
        r'(?i)this (move|article|post) is likely to be met with interest from[^.!?]*[.!?]\s*',
        r'(?i)(particularly those|especially those) (familiar with|interested in)[^.!?]*[.!?]\s*',
        r'(?im)^(for[_ ]you|for_you|for you)[:\s]+\w+\s*$',
    ):
        text = re.sub(pattern, '', text)

    for pattern, replacement in (
        (r'\*\*(.+?)\*\*', r'<strong>\1</strong>'),
        (r'__(.+?)__', r'<strong>\1</strong>'),
        (r'\*(.+?)\*', r'<em>\1</em>'),
        (r'_(.+?)_', r'<em>\1</em>'),
        (r'^[\s]*[-*]\s+', ''),
        (r'^[\s]*\d+\.\s+', ''),
    ):
        flags = re.MULTILINE if replacement == '' else 0
        text = re.sub(pattern, replacement, text, flags=flags)

    text = strip_quotes(text).strip()

    # Collapse multiple blank lines into paragraph breaks; flatten single newlines to spaces
    paragraphs = [' '.join(p.split('\n')).strip() for p in re.split(r'\n{2,}', text) if p.strip()]
    # Drop short leading paragraphs that look like title echoes or fragments
    # (fewer than 7 words and not ending with sentence-ending punctuation)
    while len(paragraphs) > 1 and len(paragraphs[0].split()) < 7 and not re.search(r'[.!?]$', paragraphs[0]):
        paragraphs.pop(0)
    if len(paragraphs) > 1:
        return '</p><p class="summary">'.join(paragraphs)
    return paragraphs[0] if paragraphs else text


def score_article(summary: str, title: str, config: dict) -> int:
    """Score an article (1-10) based on its summary and readability.
    Uses Ollama with thinking mode for better reasoning on quality/relevance.
    Returns score as int (1-10), defaults to 5 if extraction fails."""
    fn_start = time.time()
    ollama_cfg = config["ai"]["ollama"]
    base_url = ollama_cfg["base_url"]
    model = ollama_cfg["model"]
    use_thinking = config.get("scoring", {}).get("thinking", True)

    # Get user interests if available for context
    user_interests = config.get("user_profile", {}).get("interests", [])
    interests_text = ""
    if user_interests:
        interests_text = f"\nReader interests: {', '.join(user_interests)}"

    prompt = f"""Rate article relevance (1-10): clarity, depth, insights, relevance.{interests_text}
Score 1: spam/deals/promo/job-posts. 2-3: thin. 4-5: okay. 6-7: good. 8-10: excellent.
Title: {title}
Summary: {summary[:500]}
SCORE:"""

    try:
        timeout = ollama_cfg.get("timeout", 300)
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
        }
        # Add thinking directive if enabled
        if use_thinking:
            payload["think"] = True
        
        start_time = time.time()
        log.debug(json.dumps({"step": "SCORE", "status": "starting", "title": title[:50]}))
        resp = requests.post(
            f"{base_url}/api/generate",
            json=payload,
            timeout=timeout,
        )
        elapsed = time.time() - start_time
        log.debug(json.dumps({"step": "SCORE", "status": "response_received", "elapsed_sec": round(elapsed, 2), "title": title[:50]}))
        resp.raise_for_status()
        data = resp.json()
        raw = data.get("response", "").strip()

        # Extract score from response
        import re
        score = 5  # default
        for score_marker in ("SCORE:", "Score:", "score:"):
            if score_marker in raw:
                score_part = raw.split(score_marker, 1)[1].strip()
                # Extract first number
                match = re.search(r'\d+', score_part)
                if match:
                    score = int(match.group())
                    # Clamp to valid range
                    score = max(1, min(10, score))
                    break
        
        total_time = time.time() - fn_start
        telemetry_histogram_record(
            "score_duration_seconds",
            total_time,
            {"status": "ok", "model": model},
        )
        log.info(json.dumps({"step": "SCORE", "status": "completed", "elapsed_sec": round(total_time, 2), "score": score, "title": title[:50]}))
        return score
    except Exception as e:
        telemetry_histogram_record(
            "score_duration_seconds",
            time.time() - fn_start,
            {"status": "error", "model": model},
        )
        log.warning(json.dumps({"step": "SCORE", "status": "failed", "default_score": 5, "error": str(e)}))
        return 5


def process_articles(articles: list[dict], config: dict) -> tuple[list[dict], dict]:
    """Summarize articles, using cache to skip already processed ones."""
    with telemetry_span("process_articles"):
        cache = load_cache()
        processed = []
        stats = {"ollama": 0, "failed": 0, "skipped": 0, "cached": 0, "tps_samples": []}
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        scoring_enabled = config.get("scoring", {}).get("enabled", False)

        for article in articles:
            aid = article["id"]

            # Check cache
            if aid in cache:
                cached_entry = cache[aid]
                first_seen = cached_entry.get("first_seen", "")
                if first_seen and first_seen[:10] < today_str:
                    log.info(json.dumps({"step": "PROCESS", "operation": "deduplicated", "reason": "seen_previous_day", "first_seen": first_seen[:10], "title": article['title'][:50]}))
                    stats["cached"] += 1
                    telemetry_counter_add("cache_hits_total", 1, {"type": "seen_previous_day"})
                    continue
                log.info(json.dumps({"step": "PROCESS", "operation": "cached", "title": article['title'][:50]}))
                processed.append(cached_entry)
                stats["cached"] += 1
                telemetry_counter_add("cache_hits_total", 1, {"type": "same_day"})
                continue

            # Get article text
            text = extract_article_text(article["content_raw"])
            if len(text) < 100:
                text = fetch_full_article(article["url"])

            if len(text) < 50:
                log.warning(json.dumps({"step": "PROCESS", "operation": "skipped", "reason": "no_content", "title": article['title']}))
                stats["skipped"] += 1
                telemetry_counter_add("articles_failed_total", 1, {"reason": "no_content"})
                continue

            # Summarize + extract labels (single LLM call)
            summarize_start = time.time()
            summary_result = summarize_ollama(text, article["title"], config)
            if summary_result is None:
                summary, labels, for_you, tps, title_en = (
                    "Summary unavailable – Ollama failed.",
                    [],
                    False,
                    None,
                    article["title"],
                )
            else:
                summary, labels, for_you, tps, title_en = summary_result
            summary = clean_summary(summary)
            log.info(json.dumps({"step": "PROCESS", "operation": "summarized", "elapsed_sec": round(time.time() - summarize_start, 2), "tps": tps, "title": article["title"][:50]}))

            if tps is not None:
                stats["ollama"] += 1
                stats["tps_samples"].append(tps)
                telemetry_counter_add("articles_processed_total", 1, {"status": "ok"})
            else:
                stats["failed"] += 1
                telemetry_counter_add("articles_failed_total", 1, {"reason": "summary_failed"})

            # Score the article if scoring is enabled
            score = None  # no score when scoring is disabled
            if scoring_enabled:
                score = score_article(summary, article["title"], config)

            result = {
                "id": aid,
                "feed": article["feed"],
                "title": title_en,
                "url": article["url"],
                "published": article["published"],
                "summary": summary,
                "labels": labels,
                "for_you": for_you,
                "score": score,
                "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
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
    with telemetry_span("generate_html"):
        env = Environment(loader=FileSystemLoader(BASE_DIR / "templates"))
        template = env.get_template("daily.html")

        # Split articles by score tier (if scoring enabled)
        score_threshold = config.get("scoring", {}).get("threshold", 7)
        scoring_enabled = config.get("scoring", {}).get("enabled", False)
        
        articles_high = []
        articles_low = []
        if scoring_enabled:
            articles_high = [a for a in articles if a.get("score", 5) >= score_threshold]
            articles_low = [a for a in articles if a.get("score", 5) < score_threshold]
        else:
            articles_high = articles
            articles_low = []

        by_feed_high = group_articles_by_feed(articles_high)
        by_feed_low = group_articles_by_feed(articles_low)

        today = datetime.now().strftime("%A, %B %d, %Y")
        
        out_dir = Path(config["output"]["directory"])
        if not out_dir.is_absolute():
            out_dir = BASE_DIR / out_dir

        out_dir.mkdir(parents=True, exist_ok=True)
        log.debug(json.dumps({"step": "RENDER", "output_dir": str(out_dir)}))
        
        # Build digest archive list for sidebar navigation
        reports = build_reports(out_dir, datetime.now().strftime("%Y-%m-%d"))

        html = template.render(
            articles_by_feed=by_feed_high,
            articles_by_feed_low=by_feed_low,
            total_articles=len(articles),
            total_articles_high=len(articles_high),
            total_articles_low=len(articles_low),
            score_threshold=score_threshold,
            scoring_enabled=scoring_enabled,
            date=today,
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
            run_stats=run_stats or {},
            reports=reports,
        )

        filename = f"digest-{datetime.now().strftime('%Y-%m-%d-%H-%M')}.html"
        (out_dir / filename).write_text(html)
        log.info(json.dumps({"step": "RENDER", "operation": "html_written", "path": str(out_dir / filename)}))

        generate_index(out_dir)
        return out_dir


def generate_index(out_dir: Path):
    """Regenerate index.html listing all digest reports, newest first."""
    env = Environment(loader=FileSystemLoader(BASE_DIR / "templates"))
    template = env.get_template("index.html")

    today = datetime.now().strftime("%Y-%m-%d")

    reports = build_reports(out_dir, today)

    html = template.render(
        reports=reports,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    (out_dir / "index.html").write_text(html)
    log.info(json.dumps({"step": "RENDER", "operation": "index_updated", "path": str(out_dir / 'index.html'), "reports": len(reports)}))


def git_push(output_dir: Path):
    """Commit and push to GitHub Pages."""
    with telemetry_span("git_push"):
        try:
            os.chdir(output_dir)
            today = datetime.now().strftime("%Y-%m-%d")

            subprocess.run(["git", "add", "."], check=True, capture_output=True)
            result = subprocess.run(
                ["git", "status", "--porcelain"], capture_output=True, text=True
            )

            if not result.stdout.strip():
                log.info(json.dumps({"step": "GIT", "status": "no_changes"}))
                return

            subprocess.run(
                ["git", "commit", "-m", f"Daily digest {today}"],
                check=True, capture_output=True,
            )
            subprocess.run(["git", "push"], check=True, capture_output=True)
            log.info(json.dumps({"step": "GIT", "status": "pushed"}))
        except subprocess.CalledProcessError as e:
            log.error(json.dumps({"step": "GIT", "status": "failed", "error": str(e)}))
        except Exception as e:
            log.error(json.dumps({"step": "GIT", "status": "error", "error": str(e)}))


def main():
    parser = argparse.ArgumentParser(description="Blog Digest")
    parser.add_argument("--debug", action="store_true", help="Clear article cache before running (fresh summaries)")
    parser.add_argument("--model", type=str, help="Override Ollama model (requires --debug)")
    args = parser.parse_args()

    if args.model and not args.debug:
        parser.error("--model can only be used with --debug")

    if args.debug:
        logging.root.setLevel(logging.DEBUG)
        logging.getLogger("blog-digest").setLevel(logging.DEBUG)
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()
            log.info(json.dumps({"step": "MAIN", "mode": "debug", "action": "cache_cleared"}))

    log.info(json.dumps({"step": "MAIN", "status": "starting"}))

    start_time = datetime.now()
    config = load_config()
    setup_observability(config)
    telemetry_counter_add("runs_total", 1, {"debug": str(args.debug).lower()})

    if args.model:
        config["ai"]["ollama"]["model"] = args.model
        log.info(json.dumps({"step": "MAIN", "action": "model_override", "model": args.model}))

    # 1. Fetch feeds
    articles = fetch_feeds(config)
    if not articles:
        log.warning(json.dumps({"step": "MAIN", "status": "abort", "reason": "no_articles_found"}))
        sys.exit(0)

    # 2. Summarize articles
    processed, proc_stats = process_articles(articles, config)
    log.info(json.dumps({"step": "MAIN", "action": "processed", "articles": len(processed)}))

    # Abort if too many summaries failed (don't publish broken digests)
    newly_summarized = proc_stats["ollama"] + proc_stats["failed"]  # skipped (no_content) excluded
    if newly_summarized == 0:
        log.warning(json.dumps({"step": "MAIN", "status": "abort", "reason": "all_from_cache"}))
        sys.exit(0)

    failure_ratio = proc_stats["failed"] / newly_summarized
    if failure_ratio > 0.1:
        log.error(
            json.dumps({
                "step": "MAIN",
                "status": "abort",
                "reason": "too_many_failures",
                "failed": proc_stats["failed"],
                "total": newly_summarized,
                "failure_ratio": f"{failure_ratio:.1%}"
            })
        )
        sys.exit(1)

    end_time = datetime.now()
    duration = end_time - start_time
    duration_str = f"{int(duration.total_seconds() // 60)}m {int(duration.total_seconds() % 60)}s"
    telemetry_histogram_record("run_duration_seconds", duration.total_seconds(), {"status": "done"})

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

    log.info(json.dumps({"step": "MAIN", "status": "done"}))


if __name__ == "__main__":
    main()
