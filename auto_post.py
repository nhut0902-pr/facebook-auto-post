import os
import json
import time
import random
import datetime
import requests
import feedparser
from bs4 import BeautifulSoup

# ================== CẤU HÌNH CHUNG ==================
PAGE_ID = os.getenv("PAGE_ID", "").strip()
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Số bài tối đa sẽ đăng trong mỗi lần chạy (để tránh spam)
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "3"))

# Bật/tắt delay giữa các bài (chỉ nên bật khi chạy local/server)
ENABLE_DELAY = os.getenv("ENABLE_DELAY", "false").lower() in ("1", "true", "yes")
DELAY_MIN_SEC = int(os.getenv("DELAY_MIN_SEC", str(60 * 60)))       # 1h
DELAY_MAX_SEC = int(os.getenv("DELAY_MAX_SEC", str(2 * 60 * 60)))   # 2h

# Từ khóa lọc bài
KEYWORDS = [kw.strip() for kw in os.getenv(
    "KEYWORDS",
    "AI,Artificial Intelligence,Machine Learning,Deep Learning,ChatGPT,Nvidia,OpenAI,LLM,Generative AI"
).split(",") if kw.strip()]

# Nguồn RSS tin AI (ổn định hơn so với crawl HTML tùy biến)
RSS_FEEDS = [
    # Việt Nam
    "https://vnexpress.net/rss/so-hoa.rss",
    "https://vietnamnet.vn/rss/khoa-hoc.rss",
    # Quốc tế
    "https://www.theverge.com/artificial-intelligence/rss/index.xml",
    "https://venturebeat.com/category/ai/feed/",
    "https://www.wired.com/feed/tag/artificial-intelligence/latest/rss"  # có thể ít bài
]

LOG_FILE = "post_log.json"

# ================== TIỆN ÍCH ==================
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
        json.dump(logs, f, indent=2, ensure_ascii=False)

def is_already_posted(url, logs):
    for item in logs:
        if item.get("url") == url and item.get("status") == "success":
            return True
    return False

def add_log(title, url, status, extra=None):
    logs = load_log()
    entry = {
        "title": title,
        "url": url,
        "status": status,
        "time": datetime.datetime.now().isoformat()
    }
    if extra:
        entry.update(extra)
    logs.append(entry)
    save_log(logs)

def safe_get(url, timeout=12):
    try:
        return requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    except Exception:
        return None

def extract_og_image(article_url):
    """Ưu tiên ảnh từ thẻ og:image của trang bài viết."""
    resp = safe_get(article_url)
    if not resp or resp.status_code != 200:
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        return og["content"].strip()
    # fallback: lấy ảnh đầu tiên trong bài (nếu có)
    img = soup.find("img")
    if img and (img.get("src") or img.get("data-src")):
        return (img.get("src") or img.get("data-src")).strip()
    return None

def extract_meta_description(article_url):
    """Lấy mô tả ngắn (để hỗ trợ LLM tóm tắt tốt hơn)."""
    resp = safe_get(article_url)
    if not resp or resp.status_code != 200:
        return ""
    soup = BeautifulSoup(resp.text, "html.parser")
    m = soup.find("meta", attrs={"name": "description"})
    if m and m.get("content"):
        return m["content"].strip()
    og_desc = soup.find("meta", property="og:description")
    if og_desc and og_desc.get("content"):
        return og_desc["content"].strip()
    # fallback: lấy vài dòng text đầu
    text = soup.get_text(separator=" ", strip=True)
    return text[:500]

def contains_keyword(title, summary):
    blob = f"{title} {summary}".lower()
    return any(kw.lower() in blob for kw in KEYWORDS)

def unique_by_url(items):
    seen = set()
    out = []
    for it in items:
        u = it["url"]
        if u not in seen:
            seen.add(u)
            out.append(it)
    return out

# ================== LẤY TIN TỪ RSS ==================
def fetch_from_feeds():
    print("📰 Đang lấy tin từ RSS...")
    articles = []
    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for e in feed.entries[:10]:  # tối đa 10 bài/nguồn
                title = (getattr(e, "title", "") or "").strip()
                link = (getattr(e, "link", "") or "").strip()
                summary = (getattr(e, "summary", "") or "").strip()
                if not title or not link:
                    continue
                articles.append({"title": title, "url": link, "summary": summary})
        except Exception as ex:
            print(f"⚠️ Lỗi đọc feed {feed_url}: {ex}")
    articles = unique_by_url(articles)
    print(f"🔎 Tổng thu thập: {len(articles)} bài.")
    return articles

# ================== GEMINI: SINH CAPTION ==================
def generate_caption_with_gemini(title, url, context_text):
    if not GEMINI_API_KEY:
        # fallback khi không có API key
        base = f"{title}\n\nĐọc thêm: {url}"
        hashtags = " ".join(sorted({f"#{kw.replace(' ','')}" for kw in ["AI","Tech"]}))
        return f"{base}\n\n{hashtags}"

    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-1.5-flash")

        # Chuẩn bị hashtag từ KEYWORDS (gọn tối đa 3-5 cái)
        core_tags = ["AI", "Tech", "MachineLearning", "DeepLearning", "ChatGPT", "Nvidia"]
        chosen = []
        lower_blob = (title + " " + context_text).lower()
        for tag in core_tags:
            if tag.lower() in lower_blob and len(chosen) < 5:
                chosen.append(tag)
        if not chosen:
            chosen = ["AI", "Tech"]
        hashtags = " ".join(f"#{t}" for t in chosen)

        prompt = (
            "Bạn là biên tập viên mạng xã hội. Hãy viết caption Facebook ngắn gọn (2–3 câu), "
            "rõ ràng, hấp dẫn, tóm tắt nội dung bài viết dưới đây. Không quá 600 ký tự. "
            "Không thêm dấu ngoặc kép ngoài cùng. Kết thúc bằng các hashtag đã gợi ý.\n\n"
            f"Tiêu đề: {title}\n"
            f"Mô tả: {context_text[:1200]}\n"
            f"Link: {url}\n"
            f"Hashtags: {hashtags}\n"
        )
        resp = model.generate_content(prompt)
        text = (resp.text or "").strip()
        if not text:
            raise ValueError("Empty caption from Gemini")
        # đảm bảo có link
        if "http" not in text:
            text = f"{text}\n\nĐọc thêm: {url}"
        # đảm bảo có hashtags
        if "#" not in text:
            text = f"{text}\n\n{hashtags}"
        return text
    except Exception as e:
        print(f"⚠️ Gemini lỗi: {e}")
        # fallback
        base = f"{title}\n\nĐọc thêm: {url}"
        fallback_tags = " ".join(["#AI", "#Tech"])
        return f"{base}\n\n{fallback_tags}"

# ================== ĐĂNG LÊN FACEBOOK ==================
def post_text(message):
    url = f"https://graph.facebook.com/{PAGE_ID}/feed"
    r = requests.post(url, data={"message": message, "access_token": PAGE_ACCESS_TOKEN})
    return r

def post_photo(image_url, caption):
    url = f"https://graph.facebook.com/{PAGE_ID}/photos"
    r = requests.post(url, data={"url": image_url, "caption": caption, "access_token": PAGE_ACCESS_TOKEN})
    return r

def publish_article(article):
    title = article["title"]
    url = article["url"]

    # Lấy mô tả + ảnh cho bài
    meta_desc = article.get("summary") or extract_meta_description(url) or ""
    image_url = extract_og_image(url)

    # Sinh caption bằng Gemini
    caption = generate_caption_with_gemini(title, url, meta_desc)

    # Đăng bài
    if image_url:
        resp = post_photo(image_url, caption)
    else:
        resp = post_text(caption)

    if resp.ok:
        print(f"✅ Đăng thành công: {title}")
        add_log(title, url, "success", {"response": safe_json(resp)})
        return True
    else:
        print(f"❌ Lỗi khi đăng: {resp.text}")
        add_log(title, url, "error", {"response": resp.text})
        return False

def safe_json(resp):
    try:
        return resp.json()
    except Exception:
        return {"text": resp.text[:500]}

# ================== LUỒNG CHÍNH ==================
def main():
    # Kiểm tra cấu hình
    if not PAGE_ID or not PAGE_ACCESS_TOKEN:
        print("❌ Thiếu PAGE_ID hoặc PAGE_ACCESS_TOKEN (biến môi trường).")
        return

    print("📰 Bắt đầu lấy tin và đăng…")
    logs = load_log()
    all_articles = fetch_from_feeds()

    # Lọc theo từ khóa
    filtered = [a for a in all_articles if contains_keyword(a["title"], a.get("summary", ""))]
    print(f"🔍 Sau lọc từ khóa: {len(filtered)} bài.")

    # Loại bỏ bài đã đăng trước đó
    filtered = [a for a in filtered if not is_already_posted(a["url"], logs)]
    print(f"🧹 Sau khi loại trùng (đã đăng): {len(filtered)} bài.")

    # Giới hạn số bài đăng mỗi lượt
    to_post = filtered[:MAX_POSTS_PER_RUN]
    print(f"🗓️ Sẽ đăng {len(to_post)}/{len(filtered)} bài trong lượt này.")

    for idx, article in enumerate(to_post, start=1):
        print(f"➡️  [{idx}/{len(to_post)}] {article['title']}")
        publish_article(article)

        # Delay ngẫu nhiên nếu bật
        if ENABLE_DELAY and idx < len(to_post):
            delay = random.randint(DELAY_MIN_SEC, DELAY_MAX_SEC)
            print(f"⏳ Chờ {delay//60} phút trước bài kế tiếp…")
            time.sleep(delay)

    print("🏁 Hoàn tất.")

if __name__ == "__main__":
    main()
