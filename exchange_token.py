# exchange_token.py
import os
import requests
import json

APP_ID = os.getenv("FB_APP_ID", "").strip()
APP_SECRET = os.getenv("FB_APP_SECRET", "").strip()
SHORT_USER_TOKEN = os.getenv("FB_USER_SHORT_TOKEN", "").strip()
GRAPH = "https://graph.facebook.com/v19.0"

def get_long_user_token():
    url = f"{GRAPH}/oauth/access_token"
    params = {
        "grant_type": "fb_exchange_token",
        "client_id": APP_ID,
        "client_secret": APP_SECRET,
        "fb_exchange_token": SHORT_USER_TOKEN
    }
    r = requests.get(url, params=params); r.raise_for_status()
    return r.json().get("access_token")

def get_page_token(long_user_token):
    url = f"{GRAPH}/me/accounts"
    r = requests.get(url, params={"access_token": long_user_token}); r.raise_for_status()
    data = r.json().get("data", [])
    # return mapping of pages
    return data

if __name__ == "__main__":
    if not APP_ID or not APP_SECRET or not SHORT_USER_TOKEN:
        print("Set FB_APP_ID, FB_APP_SECRET, FB_USER_SHORT_TOKEN in env.")
        exit(1)
    long_user = get_long_user_token()
    print("LONG_USER_TOKEN:", long_user)
    pages = get_page_token(long_user)
    print(json.dumps(pages, indent=2))
