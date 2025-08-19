import os
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import google.generativeai as genai

# Lấy biến môi trường từ GitHub Secrets hoặc .env
PAGE_ID = os.environ["PAGE_ID"]
PAGE_ACCESS_TOKEN = os.environ["PAGE_ACCESS_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# -----------------------------
# 1. Lấy tin tức mới nhất từ VNExpress
# -----------------------------
def get_latest_news():
    url = "https://vnexpress.net/khoa-hoc-cong-nghe/ai"
    res = requests.get(url)
    soup = BeautifulSoup(res.text, "html.parser")
    
    articles = soup.select("article.item-news a.thumb")[:3]  # lấy 3 bài mới
    news_list = []
    for a in articles:
        link = a["href"]
        title = a.get("title", "")
        news_list.append({"title": title, "url": link})
    return news_list

# -----------------------------
# 2. Tóm tắt bằng Gemini API
# -----------------------------
def summarize_with_gemini(article_title, article_url):
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-pro")

    prompt = f"""
    Tóm tắt ngắn gọn bài báo: {article_title} ({article_url})
    Thêm emoji phù hợp và hashtag (#AI #CôngNghệ #TinTức).
    """
    response = model.generate_content(prompt)
    return response.text.strip()

# -----------------------------
# 3. Đăng bài lên Facebook
# -----------------------------
def post_to_facebook(message):
    url = f"https://graph.facebook.com/{PAGE_ID}/feed"
    payload = {
        "message": message,
        "access_token": PAGE_ACCESS_TOKEN
    }
    res = requests.post(url, data=payload)
    if res.status_code == 200:
        print("✅ Đăng bài thành công!")
    else:
        print("❌ Lỗi khi đăng:", res.text)

# -----------------------------
# MAIN
# -----------------------------
if __name__ == "__main__":
    news_list = get_latest_news()
    print(f"📰 Tìm thấy {len(news_list)} bài mới.")

    for news in news_list:
        summary = summarize_with_gemini(news["title"], news["url"])
        message = f"{summary}\n\n📌 Nguồn: {news['url']}"
        post_to_facebook(message)
