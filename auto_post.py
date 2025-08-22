# auto_post.py
import os
import json
import time
import random
import datetime
import requests
import feedparser
from bs4 import BeautifulSoup

# ========== Config (từ ENV) ==========
PAGE_ID = os.getenv("PAGE_ID", "").strip()
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "3"))
KEYWORDS = [k.strip() for k in os.getenv(
    "KEYWORDS",
    "AI,Artificial Intelligence,Machine Learning,Deep Learning,ChatGPT,Nvidia,OpenAI,LLM,Generative AI"
).split(",") if k.strip()]

ENABLE_DELAY = os.getenv("ENABLE_DELAY", "false").lower() in ("1","true","yes")
DELAY_MIN_SEC = int(os.getenv("DELAY_MIN_SEC", str(60*60)))    # 1 hour
DELAY_MAX_SEC = int(os.getenv("DELAY_MAX_SEC", str(2*60*60)))  # 2 hours

RSS_FEEDS = [
    "https://vnexpress.net/rss/so-hoa.rss",
    "https://vietnamnet.vn/rss/khoa-hoc.rss",
    "https://www.24h.com.vn/cong-nghe-ai-c1101.html",
    "https://znews.vn/cong-nghe.html",
]

LOG_FILE = "post_log.json"

# ========== Utilities ==========
def load_log():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_log(logs):
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)

def add_log(title, url, status, resp=None):
    logs = load_log()
    logs.append({
        "title": title,
        "url": url,
        "status": status,
        "time": datetime.datetime.utcnow().isoformat(),
        "response": resp
    })
    save_log(logs)

def is_already_posted(url):
    logs = load_log()
    for e in logs:
        if e.get("url") == url and e.get("status") == "success":
            return True
    return False

def safe_get(url, timeout=10):
    try:
        return requests.get(url, timeout=timeout, headers={"User-Agent":"Mozilla/5.0"})
    except Exception:
        return None

# ========== Fetch articles (RSS) ==========
def fetch_from_feeds():
    articles = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:10]:
                title = getattr(e, "title", "") or ""
                link  = getattr(e, "link", "") or ""
                summary = (getattr(e, "summary", "") or "").strip()
                if title and link:
                    articles.append({"title": title.strip(), "url": link.strip(), "summary": summary})
        except Exception as ex:
            print("Feed error", url, ex)
    # dedupe by url
    seen = set(); uniq = []
    for a in articles:
        if a["url"] not in seen:
            seen.add(a["url"]); uniq.append(a)
    return uniq

# ========== Content helpers ==========
def extract_og_image(article_url):
    resp = safe_get(article_url)
    if not resp or resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return og["content"].strip()
    # fallback: first meaningful <img>
    imgs = soup.find_all("img")
    for img in imgs:
        src = img.get("data-src") or img.get("src")
        if src and src.startswith("http"):
            return src
    return None

def extract_text_summary(article_url, max_chars=800):
    resp = safe_get(article_url)
    if not resp or resp.status_code != 200:
        return ""
    soup = BeautifulSoup(resp.text, "html.parser")
    meta = soup.find("meta", attrs={"name":"description"})
    if meta and meta.get("content"):
        return meta["content"].strip()[:max_chars]
    ogd = soup.find("meta", property="og:description")
    if ogd and ogd.get("content"):
        return ogd["content"].strip()[:max_chars]
    # fallback: gather first paragraphs
    paragraphs = soup.find_all("p")
    text = " ".join([p.get_text(" ", strip=True) for p in paragraphs[:6]])
    return text[:max_chars]

# ========== Gemini caption ==========
def generate_caption(title, url, context_text):
    if not GEMINI_API_KEY:
        # fallback simple caption
        hashtags = " ".join(["#AI","#Tech"])
        return f"{title}\n\nĐọc thêm: {url}\n\n{hashtags}"
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = (
            "Bạn là biên tập viên mạng xã hội. Viết caption Facebook 2-3 câu, "
            "tóm tắt nội dung, hấp dẫn, tiếng Việt. Thêm 2-4 hashtag phù hợp.\n\n"
            f"Tiêu đề: {title}\nMô tả: {context_text[:1200]}\nLink: {url}\n\nCaption:"
        )
        resp = model.generate_content(prompt)
        text = (resp.text or "").strip()
        if "http" not in text:
            text = f"{text}\n\nĐọc thêm: {url}"
        return text
    except Exception as e:
        print("Gemini error:", e)
        hashtags = " ".join(["#AI","#Tech"])
        return f"{title}\n\nĐọc thêm: {url}\n\n{hashtags}"

# ========== Facebook publishing ==========
def post_photo(image_url, caption):
    endpoint = f"https://graph.facebook.com/{PAGE_ID}/photos"
    data = {"url": image_url, "caption": caption, "access_token": PAGE_ACCESS_TOKEN}
    r = requests.post(endpoint, data=data)
    return r

def post_feed(caption):
    endpoint = f"https://graph.facebook.com/{PAGE_ID}/feed"
    data = {"message": caption, "access_token": PAGE_ACCESS_TOKEN}
    r = requests.post(endpoint, data=data)
    return r

def publish_article(article):
    title = article["title"]; url = article["url"]
    if is_already_posted(url):
        print("Skip already posted:", url); return False

    summary_text = article.get("summary") or extract_text_summary(url)
    image_url = extract_og_image(url)
    caption = generate_caption(title, url, summary_text)

    if image_url:
        r = post_photo(image_url, caption)
    else:
        r = post_feed(caption)

    try:
        j = r.json()
    except Exception:
        j = {"raw": r.text}
    if r.ok:
        print("Posted:", title)
        add_log(title, url, "success", j)
        return True
    else:
        print("Post error:", j)
        add_log(title, url, "error", j)
        return False

# ========== Main ==========
def main():
    if not PAGE_ID or not PAGE_ACCESS_TOKEN:
        print("Missing PAGE_ID or PAGE_ACCESS_TOKEN in env.")
        return

    print("Start fetching articles...")
    all_articles = fetch_from_feeds()
    # filter by keywords
    selected = []
    for a in all_articles:
        blob = (a["title"] + " " + (a.get("summary") or "")).lower()
        if any(k.lower() in blob for k in KEYWORDS):
            selected.append(a)

    print(f"Found {len(selected)} articles after keyword filter.")
    # remove already posted
    to_post = [a for a in selected if not is_already_posted(a["url"])]
    print(f"{len(to_post)} not posted yet. Will post up to {MAX_POSTS_PER_RUN}")

    to_post = to_post[:MAX_POSTS_PER_RUN]
    for idx, art in enumerate(to_post, start=1):
        print(f"[{idx}/{len(to_post)}] Publishing: {art['title']}")
        publish_article(art)
        if ENABLE_DELAY and idx < len(to_post):
            d = random.randint(DELAY_MIN_SEC, DELAY_MAX_SEC)
            print(f"Sleeping {d//60} minutes before next post...")
            time.sleep(d)

    print("Done.")

if __name__ == "__main__":
    main()
