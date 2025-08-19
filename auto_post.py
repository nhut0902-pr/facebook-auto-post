import os
import requests
import google.generativeai as genai

# ================== Cấu hình ==================
PAGE_ID = os.getenv("PAGE_ID")
PAGE_ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# URL Graph API của Facebook
FB_GRAPH_URL = f"https://graph.facebook.com/{PAGE_ID}/feed"

# Cấu hình Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")
# ===============================================


def get_latest_news():
    """
    Lấy tin mới từ Google News RSS (ví dụ: công nghệ)
    """
    import feedparser
    rss_url = "https://news.google.com/rss?hl=vi&gl=VN&ceid=VN:vi"
    feed = feedparser.parse(rss_url)
    news_list = []
    for entry in feed.entries[:3]:  # lấy 3 bài gần nhất
        news_list.append({"title": entry.title, "url": entry.link})
    return news_list


def summarize_with_gemini(title, url):
    """
    Gửi tiêu đề và link tới Gemini để tóm tắt
    """
    prompt = f"Tóm tắt ngắn gọn (2-3 câu) về bài báo này bằng tiếng Việt:\nTiêu đề: {title}\nLink: {url}"
    try:
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        print("❌ Lỗi khi gọi Gemini:", e)
        return title  # fallback: chỉ đăng tiêu đề


def post_to_facebook(message):
    """
    Đăng bài lên Fanpage bằng Graph API
    """
    try:
        response = requests.post(
            FB_GRAPH_URL,
            params={
                "access_token": PAGE_ACCESS_TOKEN,
                "message": message,
            },
        )
        if response.status_code == 200:
            print("✅ Đăng thành công:", response.json())
        else:
            print("❌ Lỗi khi đăng:", response.text)
    except Exception as e:
        print("❌ Lỗi kết nối tới Facebook:", e)


if __name__ == "__main__":
    print("📰 Đang lấy tin tức...")
    news_list = get_latest_news()
    print(f"📰 Tìm thấy {len(news_list)} bài mới.")

    for news in news_list:
        print(f"🔎 Xử lý: {news['title']}")
        summary = summarize_with_gemini(news["title"], news["url"])
        message = f"{summary}\n\nĐọc thêm: {news['url']}"
        post_to_facebook(message)
