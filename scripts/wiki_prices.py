"""Fetch today's fish market prices from the official Fishao wiki and upload them to this site.

Runs daily in GitHub Actions (.github/workflows/wiki-prices.yml); can also be run by hand:
    FISHAO_SITE_URL=https://fishao-fiyat.onrender.com FISHAO_UPLOAD_KEY=... python scripts/wiki_prices.py
"""
import itertools
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests

HERE = Path(__file__).resolve().parent
WIKI = "https://www.fishao.net/wiki/fish/{}/__data.json"
LAST_FILE = Path(os.environ.get("WIKI_LAST_FILE", HERE / ".wiki-last.json"))
DELAY_SEC = 0.3  # gentle rate limiting
UNCHANGED_RETRIES = 2
UNCHANGED_WAIT_SEC = 15 * 60

# Webshare 10 Proxy Havuzu
PROXIES = [
    "http://jiafwagk:2zdbw74nnc7h@64.137.96.74:6641",
    "http://jiafwagk:2zdbw74nnc7h@198.23.243.226:6361",
    "http://jiafwagk:2zdbw74nnc7h@84.247.60.125:6095",
    "http://jiafwagk:2zdbw74nnc7h@191.96.254.138:6185",
    "http://jiafwagk:2zdbw74nnc7h@198.46.161.42:5092",
    "http://jiafwagk:2zdbw74nnc7h@104.164.49.38:7693",
    "http://jiafwagk:2zdbw74nnc7h@23.26.71.145:5628",
    "http://jiafwagk:2zdbw74nnc7h@45.38.107.97:6014",
    "http://jiafwagk:2zdbw74nnc7h@38.154.185.97:6370",
    "http://jiafwagk:2zdbw74nnc7h@64.137.10.153:5803",
]

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.fishao.net/wiki",
}

PROXY_CYCLE = itertools.cycle(PROXIES)
CURRENT_PROXY = next(PROXY_CYCLE)

SESSION = requests.Session(impersonate="chrome120")
SESSION.headers.update(BROWSER_HEADERS)
SESSION.proxies = {"http": CURRENT_PROXY, "https": CURRENT_PROXY}


def rotate_proxy():
    global CURRENT_PROXY, SESSION
    CURRENT_PROXY = next(PROXY_CYCLE)
    SESSION.proxies = {"http": CURRENT_PROXY, "https": CURRENT_PROXY}


def fetch_market(fid):
    """(price, change) for one fish; None if not found/no price."""
    url = WIKI.format(fid)
    page = None
    for attempt in range(len(PROXIES)):
        try:
            r = SESSION.get(url, timeout=12)
            if r.status_code == 404:
                return None
            if r.status_code == 200:
                page = r.json()
                break
            # 403 veya 429 gelirse diğer proxy'ye geç
            rotate_proxy()
            time.sleep(0.5)
        except Exception:
            rotate_proxy()
            time.sleep(0.5)

    if not page:
        return None
    nodes = page.get("nodes", [])
    if not nodes:
        return None
    node = nodes[-1]
    if not node or node.get("type") != "data":
        return None
    d = node.get("data")
    if not d or not isinstance(d, list) or not d[0]:
        return None
    market = d[d[0]["market"]] if "market" in d[0] else None
    if not isinstance(market, dict) or "price" not in market:
        return None
    price = d[market["price"]]
    change = d[market["change"]] if "change" in market else None
    return (price, change) if isinstance(price, int) and price > 0 else None


def fetch_all(fish_list):
    fish, failed = [], []
    total = len(fish_list)
    for i, f in enumerate(fish_list, 1):
        try:
            m = fetch_market(f["id"])
        except Exception as e:
            failed.append(f"{f['id']} ({e})")
            m = None
        if m:
            fish.append({"id": f["id"], "name": f["en"], "name_tr": f["tr"], "price": m[0], "change": m[1]})
        if i % 10 == 0 or i == total:
            print(f"\rFetching prices: {i}/{total} ({len(fish)} found)...", end="", flush=True)
        time.sleep(DELAY_SEC)
    print()
    return fish, failed


def upload_to_site(url, key, fish):
    body = json.dumps({"fish": [{"name": f["name"], "name_tr": f["name_tr"], "worth": f["price"]} for f in fish]}).encode()
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    upload_session = requests.Session(impersonate="chrome120")
    for attempt in range(3):
        try:
            r = upload_session.post(url + "/api/otomatik-yukle", data=body, headers=headers, timeout=120)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 2:
