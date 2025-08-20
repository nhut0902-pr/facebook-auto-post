import os
import requests
from bs4 import BeautifulSoup

# 🔑 Lấy thông tin từ biến môi trường
PAGE_ID = os.getenv("PAGE_ID")
ACCESS_TOKEN = os.getenv("PAGE_ACCESS_TOKEN")

# Trang báo để lấy tin tức AI
NEWS_URL = "https://vnexpress.net/tri-tue-nhan-tao"

def get_ai_news():
    """Lấy danh sách bài viết AI (tiêu đề, link, ảnh)."""
    resp = requests.get(NEWS_URL, timeout=10)
    soup = BeautifulSoup(resp.text, "html.parser")

    articles = []
    for item in soup.select("article a[href]")[:5]:  # lấy 5 bài đầu tiên
        link = item["href"]
        title = item.get_text(strip=True)

        if not title or not link.startswith("http"):
            continue

        # Lấy ảnh từ thẻ og:image trong từng bài
        try:
            sub_html = requests.get(link, timeout=10).text
            sub_soup = BeautifulSoup(sub_html, "html.parser")
            og_img = sub_soup.find("meta", property="og:image")
            image_url = og_img["content"] if og_img else None
        except:
            image_url = None

        articles.append({
            "title": title,
            "link": link,
            "image": image_url
        })
    return articles

def post_to_facebook(article):
    """Đăng bài viết kèm ảnh lên Fanpage."""
    if article["image"]:
        url = f"https://graph.facebook.com/{PAGE_ID}/photos"
        payload = {
            "url": article["image"],
            "caption": f"{article['title']}\n\nĐọc thêm: {article['link']}",
            "access_token": ACCESS_TOKEN
        }
    else:
        url = f"https://graph.facebook.com/{PAGE_ID}/feed"
        payload = {
            "message": f"{article['title']}\n\nĐọc thêm: {article['link']}",
            "access_token": ACCESS_TOKEN
        }

    r = requests.post(url, data=payload)
    print("✅ Đăng thành công:", r.json() if r.ok else r.text)

def main():
    print("📰 Đang lấy tin tức AI...")
    articles = get_ai_news()
    print(f"🔎 Tìm thấy {len(articles)} bài.")

    for art in articles:
        print(f"📌 Đăng: {art['title']}")
        post_to_facebook(art)

if __name__ == "__main__":
    main()
