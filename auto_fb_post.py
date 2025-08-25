#!/usr/bin/env python3
# auto_fb_post.py
# Auto Facebook Poster — RSS + HTML crawl, dedupe, per-source limit, 24h filter,
# image priority (og:image / <img>), Unsplash fallback with rate-limit check,
# scheduled posting and detailed logging.

import os
import sys
import time
import json
import logging
import argparse
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urljoin

import requests
import feedparser
import schedule
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from dotenv import load_dotenv

# ---------------------------
# Load environment & config
# ---------------------------
load_dotenv()

PAGE_ID = os.getenv("FACEBOOK_PAGE_ID", "").strip()
PAGE_TOKEN = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
UNSPLASH_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()

# Behavior flags
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "3"))
MAX_POSTS_PER_SOURCE = int(os.getenv("MAX_POSTS_PER_SOURCE", "2"))
POST_TIMES = os.getenv("POST_TIMES", "08:00,12:00,18:00")  # CSV of HH:MM
USE_FULLTEXT_FOR_SUMMARY = os.getenv("USE_FULLTEXT_FOR_SUMMARY", "true").lower() == "true"

# Limits & delays
UNSPLASH_MIN_REMAINING = int(os.getenv("UNSPLASH_MIN_REMAINING", "5"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "1.0"))
SUMMARY_MAX_LEN = int(os.getenv("SUMMARY_MAX_LEN", "450"))

# Files
SOURCES_FILE = os.getenv("SOURCES_FILE", "sources.yml")
POSTED_FILE = os.getenv("POSTED_FILE", "posted_links.json")
LOG_FILE = os.getenv("LOG_FILE", "log.txt")

# HTTP
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (compatible; AutoFBPoster/1.0)")
HEADERS = {"User-Agent": USER_AGENT}
GRAPH_BASE = "https://graph.facebook.com/v19.0"

# ---------------------------
# Setup logging
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("auto-fb-poster")

# ---------------------------
# Helpers
# ---------------------------
def http_get(url: str, timeout: int = 15) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r

def clean_text(txt: str) -> str:
    return " ".join((txt or "").split())

# ---------------------------
# Load sources.yml
# ---------------------------
def load_sources(path: str = SOURCES_FILE):
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} not found.")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    feeds = cfg.get("feeds", []) or []
    html_sites = cfg.get("html_sites", []) or []  # optional
    keywords = [k.lower() for k in (cfg.get("keywords") or [])]
    return feeds, html_sites, keywords

# ---------------------------
# Posted dedupe
# ---------------------------
def load_posted(path: str = POSTED_FILE) -> set:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data if isinstance(data, list) else [])
    except FileNotFoundError:
        return set()
    except Exception as e:
        log.warning(f"Could not read posted file {path}: {e}")
        return set()

def save_posted(posted: set, path: str = POSTED_FILE):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(list(posted)), f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Could not write posted file {path}: {e}")

# ---------------------------
# Unsplash client with rate-limit awareness
# ---------------------------
class UnsplashClient:
    def __init__(self, key: str):
        self.key = (key or "").strip()
        self.remaining = None
        self.enabled = bool(self.key)

    def search_first(self, query: str):
        if not self.enabled:
            return None
        if self.remaining is not None and self.remaining < UNSPLASH_MIN_REMAINING:
            log.info("Unsplash remaining below threshold — skipping Unsplash call.")
            return None
        try:
            r = requests.get(
                "https://api.unsplash.com/search/photos",
                params={"query": query, "per_page": 1},
                headers={"Authorization": f"Client-ID {self.key}", "Accept-Version": "v1"},
                timeout=12,
            )
            # update remaining if present
            rem = r.headers.get("X-Ratelimit-Remaining")
            if rem is not None:
                try:
                    self.remaining = int(rem)
                except:
                    pass
                log.info(f"Unsplash remaining: {self.remaining}")
            r.raise_for_status()
            data = r.json()
            results = data.get("results", [])
            if results:
                return results[0]["urls"]["regular"]
        except Exception as e:
            log.warning(f"Unsplash API error: {e}")
        return None

unsplash = UnsplashClient(UNSPLASH_KEY)

# ---------------------------
# HTML extraction helpers
# ---------------------------
def extract_listing_generic(html: str, base_url: str):
    soup = BeautifulSoup(html, "lxml")
    items = []
    # common pattern: <article> blocks
    for art in soup.select("article"):
        a = art.select_one("a[href]")
        if not a:
            continue
        title_el = art.select_one("h3, h2, h1") or a
        title = clean_text(title_el.get_text())
        href = a.get("href", "")
        link = href if href.startswith("http") else urljoin(base_url, href)
        summary_el = art.select_one("p")
        summary = clean_text(summary_el.get_text()) if summary_el else ""
        items.append({"title": title, "link": link, "summary": summary})
    # fallback: find h2/h3 links
    if not items:
        for el in soup.select("h3 a[href], h2 a[href]"):
            title = clean_text(el.get_text())
            href = el.get("href", "")
            link = href if href.startswith("http") else urljoin(base_url, href)
            items.append({"title": title, "link": link, "summary": ""})
    return items

def extract_full_article(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    candidates = [
        "article",
        "div#main-detail",
        "div.article__body",
        "div.dt-news__content",
        "div.entry-content",
        "div.content-detail",
        "section.article",
    ]
    for sel in candidates:
        node = soup.select_one(sel)
        if node:
            paragraphs = [clean_text(p.get_text()) for p in node.select("p")]
            text = " ".join([p for p in paragraphs if p])
            if len(text) > 200:
                return text
    # fallback: all <p>
    paragraphs = [clean_text(p.get_text()) for p in soup.select("p")]
    return " ".join([p for p in paragraphs if p])

def find_og_image(html: str) -> str or None:
    soup = BeautifulSoup(html, "lxml")
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return og["content"].strip()
    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content"):
        return tw["content"].strip()
    # fallback: first img
    img = soup.find("img")
    if img and img.get("src"):
        src = img["src"].strip()
        return src
    return None

def extract_published_from_html(html: str) -> datetime or None:
    soup = BeautifulSoup(html, "lxml")
    # common meta tags
    meta_props = [
        ("meta", {"property": "article:published_time"}),
        ("meta", {"name": "article:published_time"}),
        ("meta", {"property": "og:published_time"}),
        ("meta", {"name": "pubdate"}),
        ("time", {}),
    ]
    for tag, attrs in meta_props:
        if tag == "time":
            t = soup.find("time")
            if t:
                txt = t.get("datetime") or t.get_text()
                try:
                    return dateparser.parse(txt)
                except:
                    continue
        else:
            m = soup.find(tag, attrs=attrs)
            if m and m.get("content"):
                try:
                    return dateparser.parse(m.get("content"))
                except:
                    continue
    return None

# ---------------------------
# RSS iter + choose
# ---------------------------
def iter_feed_entries(feed_url: str):
    fp = feedparser.parse(feed_url)
    for e in fp.entries:
        title = e.get("title", "").strip()
        link = e.get("link", "").strip()
        summary = e.get("summary", "") or e.get("description", "")
        published = e.get("published", "") or e.get("updated", "")
        yield {"title": title, "link": link, "summary": clean_text(BeautifulSoup(summary, "lxml").get_text()), "published": published, "source": feed_url}

def choose_candidates_from_feeds(feeds, keywords):
    items = []
    for f in feeds:
        try:
            for e in iter_feed_entries(f):
                t = (e["title"] or "").lower()
                s = (e["summary"] or "").lower()
                if keywords and not any(k in (t + " " + s) for k in keywords):
                    continue
                items.append(e)
        except Exception as ex:
            log.warning(f"Failed to parse feed {f}: {ex}")
        time.sleep(REQUEST_DELAY)
    # sort by published
    def parse_dt(x):
        try:
            return dateparser.parse(x.get("published") or "")
        except:
            return datetime.min.replace(tzinfo=timezone.utc)
    items.sort(key=parse_dt, reverse=True)
    return items

# ---------------------------
# choose from HTML listing pages (optional)
# ---------------------------
def choose_candidates_from_html(sites, keywords):
    items = []
    for site in sites:
        url = site.get("url")
        base = site.get("base") or url
        try:
            r = http_get(url)
            lst = extract_listing_generic(r.text, base_url=base)
            for it in lst[:15]:
                t = (it.get("title") or "").lower()
                s = (it.get("summary") or "").lower()
                if keywords and not any(k in (t + " " + s) for k in keywords):
                    continue
                items.append({"title": it.get("title"), "link": it.get("link"), "summary": it.get("summary", ""), "published": "", "source": url})
        except Exception as ex:
            log.warning(f"HTML listing error {url}: {ex}")
        time.sleep(REQUEST_DELAY)
    return items

# ---------------------------
# FB posting wrappers
# ---------------------------
def fb_post_link(message: str, link: str):
    url = f"{GRAPH_BASE}/{PAGE_ID}/feed"
    payload = {"message": message, "link": link, "access_token": PAGE_TOKEN}
    r = requests.post(url, data=payload, timeout=25)
    if r.status_code >= 400:
        raise RuntimeError(f"FB feed error: {r.status_code} {r.text}")
    return r.json()

def fb_post_photo(caption: str, image_url: str):
    url = f"{GRAPH_BASE}/{PAGE_ID}/photos"
    payload = {"caption": caption, "url": image_url, "published": "true", "access_token": PAGE_TOKEN}
    r = requests.post(url, data=payload, timeout=40)
    if r.status_code >= 400:
        raise RuntimeError(f"FB photos error: {r.status_code} {r.text}")
    return r.json()

# ---------------------------
# caption & summarize
# ---------------------------
def summarize(text_or_html: str, max_len: int = SUMMARY_MAX_LEN) -> str:
    try:
        soup = BeautifulSoup(text_or_html or "", "lxml")
        text = " ".join(soup.get_text(" ").split())
    except Exception:
        text = text_or_html or ""
    if len(text) <= max_len:
        return text
    sentences = text.split(". ")
    out = []
    for s in sentences:
        if len(" ".join(out)) + len(s) + 2 <= max_len:
            out.append(s.strip())
        else:
            break
    return (". ".join(out)).strip()

def build_caption(title: str, summary: str, source_url: str) -> str:
    host = urlparse(source_url).netloc.replace("www.", "")
    parts = [title.strip()]
    if summary:
        parts.append(f"\n\nTóm tắt: {summary.strip()}")
    parts.append(f"\nNguồn: {host}\n{source_url}")
    parts.append("\n#AI #congnghe")
    return "\n".join(parts)

# ---------------------------
# Posting job
# ---------------------------
def within_24h(published_str: str, fallback_html: str = None) -> bool:
    # If feed provides published date -> check; else try to extract from HTML meta
    if published_str:
        try:
            dt = dateparser.parse(published_str)
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt) <= timedelta(days=1)
        except:
            return False
    # fallback: attempt parse from HTML
    if fallback_html:
        dt = extract_published_from_html(fallback_html)
        if dt:
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt) <= timedelta(days=1)
    return False

def run_once(feeds, html_sites, keywords, posted_set):
    rss_items = choose_candidates_from_feeds(feeds, keywords)
    html_items = choose_candidates_from_html(html_sites, keywords) if html_sites else []
    combined = {}
    for it in rss_items + html_items:
        link = it.get("link")
        if not link:
            continue
        combined.setdefault(link, it)
    items = list(combined.values())

    # Track per-source counters
    per_source_count = {}

    posted_count = 0
    for it in items:
        if posted_count >= MAX_POSTS_PER_RUN:
            break
        url = it.get("link")
        title = it.get("title") or "Bài viết"
        source = it.get("source") or "unknown"

        if url in posted_set:
            continue

        # per-source limit
        cnt = per_source_count.get(source, 0)
        if cnt >= MAX_POSTS_PER_SOURCE:
            continue

        # Fetch page HTML (to get fulltext + image + maybe published)
        page_html = ""
        try:
            r = http_get(url, timeout=15)
            page_html = r.text
        except Exception as e:
            log.warning(f"Could not fetch article HTML {url}: {e}")

        # 24-hour filter
        if not within_24h(it.get("published", ""), fallback_html=page_html):
            log.info(f"Skipping not-recent article: {title} ({url})")
            continue

        # Summary
        if USE_FULLTEXT_FOR_SUMMARY and page_html:
            fulltext = extract_full_article(page_html)
            summ = summarize(fulltext, SUMMARY_MAX_LEN) if fulltext else (it.get("summary") or "")
        else:
            summ = it.get("summary") or summarize(page_html, max_len=SUMMARY_MAX_LEN)

        caption = build_caption(title, summ, url)

        # Image priority: og:image -> first img -> Unsplash fallback
        image_url = None
        if page_html:
            try:
                img = find_og_image(page_html)
                if img and img.startswith("http"):
                    image_url = img
            except Exception:
                image_url = None
        if not image_url:
            image_url = unsplash.search_first(title or "technology")

        # Post to Facebook
        try:
            if image_url:
                resp = fb_post_photo(caption, image_url)
                log.info(f"Posted photo: {url} -> {resp.get('id') if isinstance(resp, dict) else resp}")
            else:
                resp = fb_post_link(caption, url)
                log.info(f"Posted link: {url} -> {resp.get('id') if isinstance(resp, dict) else resp}")
            # Mark as posted
            posted_set.add(url)
            save_posted(posted_set)
            per_source_count[source] = per_source_count.get(source, 0) + 1
            posted_count += 1
            time.sleep(2)
        except Exception as e:
            log.error(f"Failed to post {url}: {e}")

    log.info(f"Run finished — posted {posted_count} new items.")
    return posted_count

# ---------------------------
# Scheduler wrapper
# ---------------------------
def schedule_jobs(feeds, html_sites, keywords):
    posted_set = load_posted(POSTED_FILE)
    # schedule times
    times = [t.strip() for t in POST_TIMES.split(",") if t.strip()]
    if not times:
        times = ["08:00", "12:00", "18:00"]
    for t in times:
        schedule.every().day.at(t).do(lambda f=feeds, h=html_sites, k=keywords, p=posted_set: run_once(f, h, k, p))
        log.info(f"Scheduled job at {t} (local time)")

    # also allow manual immediate run with --once
    return posted_set

def main(argv=None):
    parser = argparse.ArgumentParser(description="Auto Facebook Poster")
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    args = parser.parse_args(argv)

    # sanity
    if not PAGE_ID or not PAGE_TOKEN:
        log.error("Missing FACEBOOK_PAGE_ID or FACEBOOK_PAGE_ACCESS_TOKEN in env.")
        sys.exit(1)

    feeds, html_sites, keywords = load_sources()
    posted_set = load_posted(POSTED_FILE)

    if args.once:
        run_once(feeds, html_sites, keywords, posted_set)
        return

    schedule.clear()
    schedule_jobs(feeds, html_sites, keywords)
    log.info("Scheduler started. Press Ctrl+C to exit.")
    try:
        while True:
            schedule.run_pending()
            time.sleep(10)
    except KeyboardInterrupt:
        log.info("Exiting scheduler loop.")

if __name__ == "__main__":
    main()
