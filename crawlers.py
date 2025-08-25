import os
import time
import logging
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = os.getenv("CRAWLER_USER_AGENT", "Mozilla/5.0 (compatible; AutoFBPoster/1.0)")
DEFAULT_TIMEOUT = int(os.getenv("CRAWLER_TIMEOUT", "15"))
REQUEST_DELAY = float(os.getenv("CRAWLER_DELAY", "1.5"))  # giãn cách request

log = logging.getLogger("crawlers")

# ================= HTTP helpers =================

def http_get(url, timeout=DEFAULT_TIMEOUT):
    headers = {"User-Agent": USER_AGENT}
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r

# ================= Generic extractors =================

def clean_text(txt: str) -> str:
    return " ".join((txt or "").split())


def extract_listing_generic(html: str, base_url: str):
    """Thử tìm danh sách bài viết theo cấu trúc chung (article/h2/h3)."""
    soup = BeautifulSoup(html, "lxml")
    items = []

    # Ưu tiên <article>
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
            items.append({"title": title, "url": link, "summary": summary})

    # Fallback: các thẻ tiêu đề phổ biến
    if not items:
        for h in soup.select("h3 a[href], h2 a[href]"):
            title = clean_text(h.get_text())
            href = h.get("href", "")
            link = href if href.startswith("http") else urljoin(base_url, href)
            if title and link:
                items.append({"title": title, "url": link, "summary": ""})

    return items


def extract_full_article(html: str) -> str:
    """Cố gắng trích nội dung chính để tóm tắt. Không đảm bảo 100%."""
    soup = BeautifulSoup(html, "lxml")

    # Thử các vùng nội dung hay gặp
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

    # Fallback: lấy tất cả <p>
    paragraphs = [clean_text(p.get_text()) for p in soup.select("p")]
    text = " ".join([p for p in paragraphs if p])
    return text

# ================= Site-specific lightweight crawlers =================

def crawl_dantri_category(url: str):
    """Crawl danh mục Dan Tri (ví dụ: AI-Internet)."""
    resp = http_get(url)
    time.sleep(REQUEST_DELAY)
    items = extract_listing_generic(resp.text, base_url="https://dantri.com.vn")
    return items


def crawl_tuoitre_category(url: str):
    """Crawl danh mục Tuoi Tre cong nghe."""
    resp = http_get(url)
    time.sleep(REQUEST_DELAY)
    # Tuổi Trẻ thường có cấu trúc article/h3
    items = extract_listing_generic(resp.text, base_url="https://tuoitre.vn")
    return items


def find_og_image(html: str):
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


def pick_image_for_article(article_url: str, title: str = None, unsplash_key: str = None):
    # 1) Thử og:image từ bài gốc
    try:
        r = http_get(article_url, timeout=12)
        img = find_og_image(r.text)
        if img and img.startswith("http"):
            return img
    except Exception as e:
        log.warning(f"OG image fetch failed: {e}")

    # 2) Unsplash (nếu có key)
    if unsplash_key:
        try:
            q = title or "technology"
            res = requests.get(
                "https://api.unsplash.com/search/photos",
                params={"query": q, "per_page": 1},
                headers={"Authorization": f"Client-ID {unsplash_key}", "Accept-Version": "v1"},
                timeout=12,
            )
            res.raise_for_status()
            data = res.json()
            results = data.get("results", [])
            if results:
                return results[0]["urls"]["regular"]
        except Exception as e:
            log.warning(f"Unsplash error: {e}")

    return None
