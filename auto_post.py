import os
import requests
import feedparser
import google.generativeai as genai

# ==== Cấu hình ====
PAGE_ID = os.getenv("PAGE_ID")
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# RSS feed về AI
RSS_FEEDS = [
    "https://vnexpress.net/rss/so-hoa.rss",
    "https://www.theverge.com/artificial-intelligence/rss/index.xml",
    "https://venturebeat.com/category/ai/feed/",
]

# ==== Hàm lấy tin tức AI ====
def get_latest_news():
    print("📰 Đang lấy tin tức AI...")
    news_items = []
    for feed_url in RSS_FEEDS:
        feed = feedparser.parse(feed_url)
        for entry in feed.entries[:5]:  # lấy 5 tin từ mỗi nguồn
            news_items.append({
                "title": entry.title,
                "url": entry.link
            })
    print(f"📰 Tìm thấy {len(news_items)} bài AI.")
    return news_items

# ==== Tóm tắt bằng Gemini ====
def summarize_with_gemini(title, url):
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-1.5-flash")
    prompt = f"Tóm tắt ngắn gọn tin tức sau:\nTiêu đề: {title}\nNguồn: {url}"
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print(f"❌ Lỗi Gemini: {e}")
        return title  # fallback: chỉ đăng tiêu đề

# ==== Đăng lên Facebook ====
def post_to_facebook(message):
    url = f"https://graph.facebook.com/{PAGE_ID}/feed"
    params = {
        "message": message,
        "access_token": PAGE_ACCESS_TOKEN
    }
    response = requests.post(url, params=params)
    if response.status_code == 200:
        print("✅ Đăng thành công!")
    else:
        print(f"❌ Lỗi khi đăng: {response.text}")

# ==== Main ====
if __name__ == "__main__":
    news_list = get_latest_news()
    for news in news_list:
        print(f"🔎 Xử lý: {news['title']}")
        summary = summarize_with_gemini(news["title"], news["url"])
        post_message = f"{summary}\n\nNguồn: {news['url']}"
        post_to_facebook(post_message)
