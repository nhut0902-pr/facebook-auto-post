import os
import requests
import feedparser
import google.generativeai as genai

# ==== Cấu hình API ====
PAGE_ID = os.getenv("PAGE_ID")
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Cấu hình Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# ==== Nguồn tin tức AI ====
RSS_FEEDS = [
    "https://vnexpress.net/rss/so-hoa/tri-tue-nhan-tao.rss",
    "https://www.technologyreview.com/feed/ai/"
]

def get_latest_news():
    print("📰 Đang lấy tin tức AI...")
    news_items = []
    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries[:3]:  # lấy 3 bài gần nhất mỗi nguồn
            news_items.append({
                "title": entry.title,
                "url": entry.link
            })
    print(f"📰 Tìm thấy {len(news_items)} bài AI mới.")
    return news_items

def summarize_with_gemini(title, url):
    prompt = f"Tóm tắt ngắn gọn tin tức AI sau:\n\nTiêu đề: {title}\nLink: {url}\n\n➡️ Yêu cầu: viết súc tích, dễ hiểu, tiếng Việt."
    response = model.generate_content(prompt)
    return response.text.strip()

def post_to_facebook(message):
    url = f"https://graph.facebook.com/{PAGE_ID}/feed"
    payload = {"message": message, "access_token": PAGE_ACCESS_TOKEN}
    response = requests.post(url, data=payload)
    if response.status_code == 200:
        print("✅ Đăng thành công!")
    else:
        print("❌ Lỗi khi đăng:", response.text)

if __name__ == "__main__":
    news_list = get_latest_news()
    for news in news_list:
        print(f"🔎 Xử lý: {news['title']}")
        try:
            summary = summarize_with_gemini(news["title"], news["url"])
            message = f"{summary}\n\n📌 Nguồn: {news['url']}"
            post_to_facebook(message)
        except Exception as e:
            print("❌ Lỗi:", e)
