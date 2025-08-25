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

# ============ Cấu hình logging ============
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("auto-fb-poster")

# ============ Load env ============
load_dotenv()
PAGE_ID = os.getenv("FACEBOOK_PAGE_ID", "").strip()
PAGE_TOKEN = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
UNSPLASH_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "2"))
LANG = os.getenv("LANG", "vi")

if not PAGE_ID or not PAGE_TOKEN:
    raise SystemExit("Thiếu FACEBOOK_PAGE_ID hoặc FACEBOOK_PAGE_ACCESS_TOKEN trong .env")

GRAPH_BASE = "https://graph.facebook.com/v19.0"  # v19.0 hiện còn hiệu lực dài hạn
DB_PATH = "posted.db"
SOURCES_FILE = "sources.yaml"
USER_AGENT = "Mozilla/5.0 (compatible; AutoFBPoster/1.0)"

# ============ DB: lưu URL đã đăng để tránh trùng ============
def ensure_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS posted(
            id TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            title TEXT,
            published_at TEXT,
            created_at TEXT NOT NULL
        )
    """)
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
        (h, url, title, published_at or "", now)
    )
    conn.commit()

# ============ Đọc cấu hình nguồn ============
def load_sources():
    with open(SOURCES_FILE, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    feeds = data.get("feeds", [])
    keywords = [k.lower() for k in data.get("keywords", [])]
    return feeds, keywords

# ============ Helpers web ============
def http_get(url, timeout=15):
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r

def extract_og_image(html):
    soup = BeautifulSoup(html, "html.parser")
    # Ưu tiên og:image
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return og["content"].strip()
    # Thử twitter:image
    tw = soup.find("meta", attrs={"name": "twitter:image"})
    if tw and tw.get("content"):
        return tw["content"].strip()
    # Thử <img> đầu tiên (ít ưu tiên)
    img = soup.find("img")
    if img and img.get("src"):
        return img["src"].strip()
    return None

def pick_image(article_url, title=None, fallback_query="technology"):
    # 1) Thử lấy og:image từ chính bài viết
    try:
        resp = http_get(article_url, timeout=12)
        img = extract_og_image(resp.text)
        if img and img.startswith("http"):
            return img
    except Exception as e:
        log.warning(f"Không lấy được og:image: {e}")

    # 2) Nếu có Unsplash key, tìm ảnh phù hợp
    if UNSPLASH_KEY:
        try:
            q = (title or fallback_query or "technology")
            res = requests.get(
                "https://api.unsplash.com/search/photos",
                params={"query": q, "per_page": 1},
                headers={"Authorization": f"Client-ID {UNSPLASH_KEY}", "Accept-Version": "v1"},
                timeout=12
            )
            res.raise_for_status()
            data = res.json()
            results = data.get("results", [])
            if results:
                return results[0]["urls"]["regular"]
        except Exception as e:
            log.warning(f"Unsplash lỗi: {e}")

    # 3) Không có ảnh
    return None

def is_language_ok(text):
    # Bộ lọc rất đơn giản theo từ khóa đã cấu hình
    return True if not text else True

# ============ Đăng lên Facebook ============
def post_link(message, link):
    url = f"{GRAPH_BASE}/{PAGE_ID}/feed"
    payload = {
        "message": message,
        "link": link,
        "access_token": PAGE_TOKEN,
    }
    r = requests.post(url, data=payload, timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"FB feed error: {r.status_code} {r.text}")
    return r.json()

def post_photo(caption, image_url):
    url = f"{GRAPH_BASE}/{PAGE_ID}/photos"
    payload = {
        "caption": caption,
        "url": image_url,
        "published": "true",
        "access_token": PAGE_TOKEN,
    }
    r = requests.post(url, data=payload, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"FB photos error: {r.status_code} {r.text}")
    return r.json()

# ============ Soạn caption ============
def build_caption(title, summary, source_url):
    host = urlparse(source_url).netloc.replace("www.", "")
    parts = []
    parts.append(title.strip())
    if summary:
        parts.append(f"\n\nTóm tắt: {summary.strip()}")
    parts.append(f"\nNguồn: {host}\n{source_url}")
    parts.append("\n#AI #congnghe")
    return "\n".join(parts)

def summarize(html_or_text, max_len=300):
    # Tóm tắt cực nhẹ (extract 1-2 câu đầu)
    try:
        soup = BeautifulSoup(html_or_text, "html.parser")
        text = " ".join(soup.get_text(" ").split())
    except Exception:
        text = html_or_text
    if len(text) <= max_len:
        return text
    # Cắt theo dấu chấm
    sentences = text.split(". ")
    out = []
    for s in sentences:
        if len(" ".join(out)) + len(s) + 2 <= max_len:
            out.append(s.strip())
        else:
            break
    return (". ".join(out)).strip()

# ============ Lấy bài mới từ RSS ============
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
            "summary": BeautifulSoup(summary, "html.parser").get_text(" ").strip() if summary else "",
            "published": published
        }

def choose_candidates(feeds, keywords):
    candidates = []
    for f in feeds:
        try:
            for e in iter_feed_entries(f):
                t = (e["title"] or "").lower()
                s = (e["summary"] or "").lower()
                if keywords:
                    if not any(k.lower() in (t + " " + s) for k in keywords):
                        continue
                candidates.append(e)
        except Exception as ex:
            log.warning(f"Lỗi feed {f}: {ex}")
    # Sắp xếp theo thời gian nếu có
    def parse_dt(x):
        try:
            return dateparser.parse(x["published"])
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)
    candidates.sort(key=parse_dt, reverse=True)
    return candidates

# ============ Chạy chính ============
def main():
    conn = ensure_db()
    feeds, keywords = load_sources()
    items = choose_candidates(feeds, keywords)

    posted_count = 0
    for it in items:
        if posted_count >= MAX_POSTS_PER_RUN:
            break
        url = it["link"]
        title = it["title"] or "Bài viết"
        if not url or was_posted(conn, url):
            continue

        # Lấy nội dung ngắn + ảnh
        html = ""
        try:
            r = http_get(url, timeout=15)
            html = r.text
        except Exception as e:
            log.warning(f"Không tải được bài {url}: {e}")

        summ = it["summary"] or summarize(html, max_len=350)
        caption = build_caption(title, summ, url)
        img = pick_image(url, title=title, fallback_query="AI")

        try:
            if img:
                res = post_photo(caption, img)
                log.info(f"Đăng ảnh thành công: {res}")
            else:
                res = post_link(caption, url)
                log.info(f"Đăng link thành công: {res}")
            mark_posted(conn, url, title, it.get("published"))
            posted_count += 1
            time.sleep(3)  # tránh spam
        except Exception as e:
            log.error(f"Đăng thất bại: {e}")
            # Không mark_posted để thử lại lần sau

    log.info(f"Hoàn tất. Đã đăng: {posted_count}")

if __name__ == "__main__":
    main()
