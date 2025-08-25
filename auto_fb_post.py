#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Auto Facebook Poster — Smart Sources (RSS or HTML auto-detect).
- Crawl danh sách từ từng nguồn (RSS hoặc HTML).
- Lấy full article (newspaper3k > BeautifulSoup + domain selectors).
- Ảnh: og:image / <img>, fallback Unsplash (có kiểm tra quota).
- Chống trùng (posted_links.json), giới hạn per-run & per-source.
- Lọc theo từ khóa, lọc bài mới (<=24h).
- Đăng lên Facebook Page (link/ảnh).
- One-shot: chạy xong thoát (phù hợp CI/CD).

Yêu cầu:
- .env: FACEBOOK_PAGE_ID, FACEBOOK_PAGE_ACCESS_TOKEN, (tùy chọn) UNSPLASH_ACCESS_KEY
- sources.yml: danh sách sources + keywords
- requirements: requests, feedparser, beautifulsoup4, lxml, PyYAML, python-dateutil, newspaper3k, python-dotenv

Chạy:
    python auto_fb_post.py --max 3
"""

import os
import sys
import json
import time
import logging
import argparse
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urljoin

import requests
import feedparser
import yaml
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from dotenv import load_dotenv

# newspaper3k
from newspaper import Article
from newspaper.article import ArticleException

# ---------------------------
# Cấu hình & hằng số
# ---------------------------
load_dotenv()

# Env
PAGE_ID = os.getenv("FACEBOOK_PAGE_ID", "").strip()
PAGE_TOKEN = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
UNSPLASH_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()

LANG = os.getenv("LANG", "vi")
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "3"))
MAX_POSTS_PER_SOURCE = int(os.getenv("MAX_POSTS_PER_SOURCE", "2"))
USE_FULLTEXT_FOR_SUMMARY = os.getenv("USE_FULLTEXT_FOR_SUMMARY", "true").lower() == "true"
SUMMARY_MAX_LEN = int(os.getenv("SUMMARY_MAX_LEN", "700"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.8"))
UNSPLASH_MIN_REMAINING = int(os.getenv("UNSPLASH_MIN_REMAINING", "5"))

# File paths
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_FILE = os.getenv("SOURCES_FILE", os.path.join(THIS_DIR, "sources.yml"))
POSTED_FILE = os.getenv("POSTED_FILE", os.path.join(THIS_DIR, "posted_links.json"))
LOG_FILE = os.getenv("LOG_FILE", os.path.join(THIS_DIR, "log.txt"))

# HTTP
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (compatible; AutoFBPoster/3.0)")
HEADERS = {"User-Agent": USER_AGENT}
GRAPH_BASE = "https://graph.facebook.com/v19.0"

# ---------------------------
# Logging
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
# Tiện ích
# ---------------------------
def http_get(url: str, timeout: int = 15) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r

def clean_text(txt: str) -> str:
    return " ".join((txt or "").split())

def host_of(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().replace("www.", "")
    except Exception:
        return ""

# ---------------------------
# Nguồn (sources.yml)
# ---------------------------
def load_sources(path: str = SOURCES_FILE):
    if not os.path.exists(path):
        raise FileNotFoundError(f"{path} not found.")
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    sources = cfg.get("sources", []) or []
    keywords = [k.lower() for k in (cfg.get("keywords") or [])]
    return sources, keywords

# ---------------------------
# Dedupe
# ---------------------------
def load_posted(path: str = POSTED_FILE) -> set:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data if isinstance(data, list) else [])
    except FileNotFoundError:
        return set()
    except Exception as e:
        log.warning(f"Không đọc được posted file {path}: {e}")
        return set()

def save_posted(posted: set, path: str = POSTED_FILE):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(sorted(list(posted)), f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Không ghi được posted file {path}: {e}")

# ---------------------------
# Unsplash (fallback)
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
            log.info("Unsplash quota thấp — bỏ qua gọi API.")
            return None
        try:
            r = requests.get(
                "https://api.unsplash.com/search/photos",
                params={"query": query, "per_page": 1},
                headers={"Authorization": f"Client-ID {self.key}", "Accept-Version": "v1"},
                timeout=12,
            )
            rem = r.headers.get("X-Ratelimit-Remaining")
            if rem is not None:
                try:
                    self.remaining = int(rem)
                except Exception:
                    pass
                log.info(f"Unsplash remaining: {self.remaining}")
            r.raise_for_status()
            data = r.json()
            results = data.get("results", [])
            if results:
                return results[0]["urls"]["regular"]
        except Exception as e:
            log.warning(f"Unsplash lỗi: {e}")
        return None

unsplash = UnsplashClient(UNSPLASH_KEY)

# ---------------------------
# Trích danh sách (HTML listing)
# ---------------------------
def extract_listing_generic(html: str, base_url: str):
    soup = BeautifulSoup(html, "lxml")
    items = []
    # Thử các khối article phổ biến
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
        if title and link:
            items.append({"title": title, "link": link, "summary": summary})
    # Fallback: tiêu đề nằm trong h2/h3
    if not items:
        for h in soup.select("h2 a[href], h3 a[href]"):
            title = clean_text(h.get_text())
            href = h.get("href", "")
            link = href if href.startswith("http") else urljoin(base_url, href)
            if title and link:
                items.append({"title": title, "link": link, "summary": ""})
    # VnExpress mục AI có thể dùng thẻ a trong list, thêm fallback:
    if not items:
        for a in soup.select("a[href]"):
            txt = clean_text(a.get_text())
            href = a.get("href", "")
            if txt and href and "/ai" in href:
                link = href if href.startswith("http") else urljoin(base_url, href)
                items.append({"title": txt, "link": link, "summary": ""})
    return items

# ---------------------------
# Trích ảnh từ bài
# ---------------------------
def find_og_image(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return og["content"].strip()
    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content"):
        return tw["content"].strip()
    img = soup.find("img")
    if img and img.get("src"):
        return img["src"].strip()
    return None

# ---------------------------
# Trích nội dung đầy đủ (siêu mạnh)
# ---------------------------
def extract_full_with_newspaper(url: str, language: str = "vi"):
    art = Article(url, language=language)
    art.download()
    art.parse()
    title = clean_text(art.title or "")
    text = clean_text(art.text or "")
    return title, text

DOMAIN_SELECTORS = {
    # VnExpress
    "vnexpress.net": [
        "article.fck_detail",
        "div.sidebar_1",
        "article",
    ],
    # Tuổi Trẻ
    "tuoitre.vn": [
        "div.detail-content.afcbc-body",
        "div#main-detail",
        "article",
    ],
    # Thanh Niên
    "thanhnien.vn": [
        "div.detail__content",
        "div#abody",
        "article",
    ],
    # Dân Trí
    "dantri.com.vn": [
        "div.singular-content",
        "div.article__body",
        "article",
    ],
    # ZingNews
    "zingnews.vn": [
        "div.the-article-body",
        "article",
    ],
    # Vietnamnet ICT
    "vietnamnet.vn": [
        "div#ArticleContent",
        "article",
    ],
    # GenK
    "genk.vn": [
        "div.knc-content",
        "div#contentDetail",
        "article",
    ],
}

def extract_full_with_bs(url: str, html: str) -> tuple[str, str]:
    soup = BeautifulSoup(html, "lxml")
    domain = host_of(url)
    selectors = DOMAIN_SELECTORS.get(domain, [])

    # Tiêu đề
    title = ""
    t = soup.find("h1")
    if t:
        title = clean_text(t.get_text())

    # Theo selector đặc thù
    for sel in selectors:
        node = soup.select_one(sel)
        if node:
            paragraphs = [clean_text(p.get_text()) for p in node.select("p")]
            text = " ".join([p for p in paragraphs if p])
            if len(text) > 120:
                return title, text

    # Fallback <article>
    art = soup.find("article")
    if art:
        paragraphs = [clean_text(p.get_text()) for p in art.select("p")]
        text = " ".join([p for p in paragraphs if p])
        if len(text) > 100:
            return title, text

    # Fallback gom toàn bộ <p>
    paragraphs = [clean_text(p.get_text()) for p in soup.find_all("p")]
    text = " ".join([p for p in paragraphs if p])
    return title, text

def extract_full_text(url: str) -> tuple[str, str, str]:
    """
    Trả về: (title, text, image_url_from_article)
    """
    # 1) Newspaper
    try:
        title, text = extract_full_with_newspaper(url, language=LANG)
        if len(text) >= 200:
            try:
                r = http_get(url, timeout=12)
                img = find_og_image(r.text)
            except Exception:
                img = None
            return title, text, img
    except (ArticleException, Exception) as e:
        log.info(f"newspaper3k không lấy được ({url}): {e}")

    # 2) Fallback BeautifulSoup
    try:
        r = http_get(url, timeout=15)
        title, text = extract_full_with_bs(url, r.text)
        img = find_og_image(r.text)
        return title, text, img
    except Exception as e:
        log.warning(f"Fallback BeautifulSoup lỗi ({url}): {e}")
        return "", "", None

# ---------------------------
# Phát hiện nguồn: RSS hay HTML
# ---------------------------
def detect_rss_entries(url: str):
    try:
        feed = feedparser.parse(url)
        # Nếu có entries hợp lệ, coi là RSS
        if getattr(feed, "entries", None) and len(feed.entries) > 0 and not feed.bozo:
            return feed.entries
        # Một số RSS có bozo nhưng vẫn có entries dùng được
        if getattr(feed, "entries", None) and len(feed.entries) > 0:
            return feed.entries
    except Exception as e:
        log.info(f"Không parse RSS được ({url}): {e}")
    return None

def fetch_listing_html(url: str):
    # Dùng HTML để trích danh sách liên kết
    r = http_get(url, timeout=15)
    soup = BeautifulSoup(r.text, "lxml")
    return soup, r.text

def gather_candidates_from_source(src_url: str, keywords: list[str]) -> list[dict]:
    """
    Trả về danh sách item: {title, link, summary, published, source}
    Tự nhận diện RSS/HTML.
    """
    items = []
    # 1) Thử RSS
    rss_entries = detect_rss_entries(src_url)
    if rss_entries is not None:
        for e in rss_entries:
            title = (e.get("title") or "").strip()
            link = (e.get("link") or "").strip()
            if not link:
                continue
            summary = e.get("summary", "") or e.get("description", "")
            published = e.get("published", "") or e.get("updated", "")
            t = title.lower()
            s = clean_text(BeautifulSoup(summary, "lxml").get_text()).lower()
            if keywords and not any(k in (t + " " + s) for k in keywords):
                continue
            items.append({
                "title": title,
                "link": link,
                "summary": clean_text(BeautifulSoup(summary, "lxml").get_text()),
                "published": published,
                "source": src_url
            })
        time.sleep(REQUEST_DELAY)
        return items

    # 2) HTML listing
    try:
        soup, raw = fetch_listing_html(src_url)
        base = src_url
        lst = extract_listing_generic(raw, base_url=base)
        for it in lst[:25]:
            title = it.get("title") or ""
            link = it.get("link") or ""
            if not link:
                continue
            summary = it.get("summary") or ""
            t = title.lower()
            s = summary.lower()
            if keywords and not any(k in (t + " " + s) for k in keywords):
                continue
            items.append({
                "title": title,
                "link": link,
                "summary": clean_text(summary),
                "published": "",
                "source": src_url
            })
        time.sleep(REQUEST_DELAY)
        return items
    except Exception as ex:
        log.warning(f"HTML listing lỗi {src_url}: {ex}")
        return items

# ---------------------------
# Tóm tắt & caption
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
    host = host_of(source_url)
    parts = [title.strip()]
    if summary:
        parts.append(f"\n\nTóm tắt: {summary.strip()}")
    parts.append(f"\nNguồn: {host}\n{source_url}")
    parts.append("\n#AI #congnghe")
    return "\n".join(parts)

# ---------------------------
# FB Posting
# ---------------------------
def fb_post_link(message: str, link: str):
    url = f"{GRAPH_BASE}/{PAGE_ID}/feed"
    payload = {"message": message, "link": link, "access_token": PAGE_TOKEN}
    r = requests.post(url, data=payload, timeout=30)
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
# Bộ lọc thời gian (<=24h)
# ---------------------------
def extract_published_from_html(html: str) -> datetime | None:
    soup = BeautifulSoup(html, "lxml")
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

def within_24h(published_str: str, url: str) -> bool:
    if published_str:
        try:
            dt = dateparser.parse(published_str)
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt) <= timedelta(days=1)
        except:
            pass
    # Fallback: lấy từ HTML
    try:
        r = http_get(url, timeout=10)
        dt = extract_published_from_html(r.text)
        if dt:
            if not dt.tzinfo:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt) <= timedelta(days=1)
    except Exception:
        pass
    return False

# ---------------------------
# Quy trình chính
# ---------------------------
def run_once(max_posts: int):
    if not PAGE_ID or not PAGE_TOKEN:
        raise SystemExit("Thiếu FACEBOOK_PAGE_ID hoặc FACEBOOK_PAGE_ACCESS_TOKEN trong .env")

    sources, keywords = load_sources(SOURCES_FILE)
    posted = load_posted(POSTED_FILE)

    # Gom tất cả candidates từ từng nguồn (tự nhận diện RSS/HTML)
    candidates = []
    for src in sources:
        items = gather_candidates_from_source(src, keywords)
        if not items:
            log.info(f"Không lấy được item nào từ nguồn: {src}")
        candidates.extend(items)

    # Sắp xếp theo published (mới -> cũ)
    def parse_dt(x):
        try:
            return dateparser.parse(x.get("published") or "") or datetime.min.replace(tzinfo=timezone.utc)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)

    candidates.sort(key=parse_dt, reverse=True)

    # Khử trùng lặp theo link
    unique = {}
    for it in candidates:
        link = it.get("link")
        if link:
            unique.setdefault(link, it)
    items = list(unique.values())

    per_source_count: dict[str, int] = {}
    posted_count = 0

    for it in items:
        if posted_count >= max_posts:
            break

        url = it.get("link")
        if not url:
            continue
        if url in posted:
            continue

        src = it.get("source") or "unknown"
        if per_source_count.get(src, 0) >= MAX_POSTS_PER_SOURCE:
            continue

        # Lọc bài trong 24h
        if not within_24h(it.get("published", ""), url):
            log.info(f"Bỏ qua bài cũ (>24h): {url}")
            continue

        # Lấy full bài
        title_from_feed = it.get("title") or ""
        title, fulltext, img_from_article = extract_full_text(url)
        title = title or title_from_feed or "Bài viết"

        if not fulltext:
            fulltext = it.get("summary", "")

        # Caption
        text_for_summary = fulltext if USE_FULLTEXT_FOR_SUMMARY else (it.get("summary") or fulltext)
        summary = summarize(text_for_summary, SUMMARY_MAX_LEN)
        caption = build_caption(title, summary, url)

        # Ảnh
        image_url = None
        if img_from_article and img_from_article.startswith("http"):
            image_url = img_from_article
        if not image_url:
            image_url = unsplash.search_first(title or "technology")

        # Đăng
        try:
            if image_url:
                resp = fb_post_photo(caption, image_url)
                log.info(f"Đăng ảnh OK: {url} -> {resp}")
            else:
                resp = fb_post_link(caption, url)
                log.info(f"Đăng link OK: {url} -> {resp}")

            posted.add(url)
            save_posted(posted, POSTED_FILE)
            per_source_count[src] = per_source_count.get(src, 0) + 1
            posted_count += 1
            time.sleep(1.2)
        except Exception as e:
            log.error(f"Đăng thất bại: {url} | {e}")

    log.info(f"Hoàn tất. Đã đăng mới: {posted_count}")

# ---------------------------
# CLI
# ---------------------------
def main():
    parser = argparse.ArgumentParser(description="Auto Facebook Poster — Smart")
    parser.add_argument("--max", type=int, default=MAX_POSTS_PER_RUN, help="Số bài tối đa mỗi lần chạy")
    args = parser.parse_args()

    log.info("Bắt đầu chạy auto_fb_post.py (one-shot, smart sources).")
    run_once(args.max)
    log.info("Kết thúc.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.error(f"Lỗi nghiêm trọng: {e}", exc_info=True)
        sys.exit(1)
