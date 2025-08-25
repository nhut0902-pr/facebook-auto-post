import os
import time
import json
import hashlib
import sqlite3
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from dotenv import load_dotenv
import yaml

from crawlers import (
    crawl_dantri_category,
    crawl_tuoitre_category,
    extract_full_article,
    pick_image_for_article,
)

# ================= Logging =================
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("auto-fb-poster")

# ================= Env =================
load_dotenv()
PAGE_ID = os.getenv("FACEBOOK_PAGE_ID", "").strip()
PAGE_TOKEN = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
UNSPLASH_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "3"))
LANG = os.getenv("LANG", "vi")
USE_FULLTEXT_FOR_SUMMARY = os.getenv("USE_FULLTEXT_FOR_SUMMARY", "false").lower() == "true"

if not PAGE_ID or not PAGE_TOKEN:
    raise SystemExit("Thiếu FACEBOOK_PAGE_ID hoặc FACEBOOK_PAGE_ACCESS_TOKEN trong .env/secrets")

GRAPH_BASE = "https://graph.facebook.com/v19.0"
DB_PATH = "posted.db"
SOURCES_FILE = "sources.yaml"
USER_AGENT = "Mozilla/5.0 (compatible; AutoFBPoster/1.0)"

# ================= DB =================

def ensure_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS posted(
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            title TEXT,
            published_at TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def was_posted(conn, url):
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM posted WHERE id=?", (h,))
    return cur.fetchone() is not None


def mark_posted(conn, url, title, published_at):
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO posted(id,url,title,published_at,created_at) VALUES(?,?,?,?,?)",
        (h, url, title, published_at or "", now),
    )
    conn.commit()

# ================= Sources =================

def load_sources():
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    feeds = data.get("feeds", [])
    html_sites = data.get("html_sites", [])
    keywords = [k.lower() for k in data.get("keywords", [])]
    return feeds, html_sites, keywords

# ================= HTTP =================

def http_get(url, timeout=15):
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r

# ================= Facebook Posting =================

def post_link(message, link):
    url = f"{GRAPH_BASE}/{PAGE_ID}/feed"
    payload = {"message": message, "link": link, "access_token": PAGE_TOKEN}
    r = requests.post(url, data=payload, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"FB feed error: {r.status_code} {r.text}")
    return r.json()


def post_photo(caption, image_url):
    url = f"{GRAPH_BASE}/{PAGE_ID}/photos"
    payload = {"caption": caption, "url": image_url, "published": "true", "access_token": PAGE_TOKEN}
    r = requests.post(url, data=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"FB photos error: {r.status_code} {r.text}")
    return r.json()

# ================= Caption & Summary =================

def build_caption(title, summary, source_url):
    host = urlparse(source_url).netloc.replace("www.", "")
    parts = [title.strip()]
    if summary:
        parts.append(f"\n\nTóm tắt: {summary.strip()}")
    parts.append(f"\nNguồn: {host}\n{source_url}")
    parts.append("\n#AI #congnghe")
    return "\n".join(parts)


def summarize(html_or_text, max_len=350):
    try:
        soup = BeautifulSoup(html_or_text, "lxml")
        text = " ".join(soup.get_text(" ").split())
    except Exception:
        text = html_or_text
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

# ================= RSS =================

def iter_feed_entries(feed_url):
    fp = feedparser.parse(feed_url)
    for e in fp.entries:
        title = e.get("title", "").strip()
        link = e.get("link", "").strip()
        summary = e.get("summary", "")
        published = e.get("published", "") or e.get("updated", "")
        yield {
            "title": title,
            "link": link,
            "summary": BeautifulSoup(summary, "lxml").get_text(" ").strip() if summary else "",
            "published": published,
            "source": "rss",
        }


def choose_candidates_from_feeds(feeds, keywords):
    candidates = []
    for f in feeds:
        try:
            for e in iter_feed_entries(f):
                t = (e["title"] or "").lower()
                s = (e["summary"] or "").lower()
                if keywords and not any(k in (t + " " + s) for k in keywords):
                    continue
                candidates.append(e)
        except Exception as ex:
            log.warning(f"RSS error {f}: {ex}")

    def parse_dt(x):
        try:
            return dateparser.parse(x.get("published") or "")
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    candidates.sort(key=parse_dt, reverse=True)
    return candidates

# ================= HTML =================

def choose_candidates_from_html(html_sites, keywords):
    candidates = []
    for site in html_sites:
        name = site.get("name")
        url = site.get("url")
        if not url:
            continue
        try:
            if "dantri" in url:
                items = crawl_dantri_category(url)
            elif "tuoitre" in url:
                items = crawl_tuoitre_category(url)
            else:
                # Fallback: tải và parse danh sách chung
                resp = http_get(url)
                from crawlers import extract_listing_generic
                items = extract_listing_generic(resp.text, base_url=url)
            for it in items[:15]:  # giới hạn mỗi danh mục 15 bài mới nhất
                t = (it.get("title") or "").lower()
                s = (it.get("summary") or "").lower()
                if keywords and not any(k in (t + " " + s) for k in keywords):
                    continue
                candidates.append({
                    "title": it.get("title"),
                    "link": it.get("url"),
                    "summary": it.get("summary", ""),
                    "published": "",
                    "source": name or "html",
                })
            time.sleep(1.0)
        except Exception as ex:
            log.warning(f"HTML crawl error {url}: {ex}")
    return candidates

# ================= Main =================

def main():
    conn = ensure_db()
    feeds, html_sites, keywords = load_sources()

    rss_items = choose_candidates_from_feeds(feeds, keywords)
    html_items = choose_candidates_from_html(html_sites, keywords)

    # Gộp & khử trùng lặp theo link
    by_link = {}
    for it in rss_items + html_items:
        link = it.get("link") or it.get("url")
        if not link:
            continue
        by_link.setdefault(link, it)

    items = list(by_link.values())

    posted_count = 0
    for it in items:
        if posted_count >= MAX_POSTS_PER_RUN:
            break
        url = it.get("link") or it.get("url")
        title = it.get("title") or "Bài viết"
        if not url or was_posted(conn, url):
            continue

        page_html = ""
        try:
            r = http_get(url, timeout=15)
            page_html = r.text
        except Exception as e:
            log.warning(f"Không tải được bài {url}: {e}")

        if USE_FULLTEXT_FOR_SUMMARY:
            fulltext = extract_full_article(page_html)
            summ = summarize(fulltext, max_len=450) if fulltext else (it.get("summary") or "")
        else:
            summ = it.get("summary") or summarize(page_html, max_len=350)

        caption = build_caption(title, summ, url)
        img = pick_image_for_article(url, title=title, unsplash_key=UNSPLASH_KEY)

        try:
            if img:
                res = post_photo(caption, img)
                log.info(f"Đăng ảnh thành công: {res}")
            else:
                res = post_link(caption, url)
                log.info(f"Đăng link thành công: {res}")
            mark_posted(conn, url, title, it.get("published"))
            posted_count += 1
            time.sleep(3)
        except Exception as e:
            log.error(f"Đăng thất bại: {e}")

    log.info(f"Hoàn tất. Đã đăng: {posted_count}")


if __name__ == "__main__":
    main()
