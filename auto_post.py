import requests
from bs4 import BeautifulSoup

PAGE_ID = "YOUR_PAGE_ID"
ACCESS_TOKEN = "YOUR_PAGE_ACCESS_TOKEN"

def extract_image_from_article(article_url: str) -> str:
    """Lấy ảnh đại diện (og:image) từ bài báo"""
    try:
        resp = requests.get(article_url, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        
        # tìm thẻ og:image
        og_image = soup.find("meta", property="og:image")
        if og_image and og_image.get("content"):
            return og_image["content"]

        # fallback: lấy ảnh đầu tiên trong bài
        first_img = soup.find("img")
        if first_img and first_img.get("src"):
            return first_img["src"]

    except Exception as e:
        print("Lỗi khi lấy ảnh:", e)
    return None


def post_article(article_url: str, caption: str):
    """Đăng bài báo lên Facebook Page kèm ảnh"""
    image_url = extract_image_from_article(article_url)
    if not image_url:
        print("⚠ Không tìm thấy ảnh, chỉ đăng text + link")
        url = f"https://graph.facebook.com/{PAGE_ID}/feed"
        payload = {
            "message": caption + "\n\nĐọc thêm: " + article_url,
            "access_token": ACCESS_TOKEN
        }
    else:
        url = f"https://graph.facebook.com/{PAGE_ID}/photos"
        payload = {
            "url": image_url,
            "caption": caption + "\n\nĐọc thêm: " + article_url,
            "access_token": ACCESS_TOKEN
        }

    res = requests.post(url, data=payload)
    print(res.json())


# ================== TEST ==================
if __name__ == "__main__":
    article = "https://tuoitre.vn/openai-ra-mat-goi-chatgpt-re-nhat-truoc-nay-chua-toi-5-usd-thang-20250820082559068.htm"
    caption = "Cú hích cho AI Việt! Hai ứng dụng AI nội địa lọt top 10 ứng dụng AI phổ biến nhất. #AI"
    
    post_article(article, caption)
