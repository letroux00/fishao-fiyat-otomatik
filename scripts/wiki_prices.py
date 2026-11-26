"""Fetch today's fish market prices from the official Fishao wiki and upload them to this site.

Runs daily in GitHub Actions (.github/workflows/wiki-prices.yml); can also be run by hand:
    FISHAO_SITE_URL=https://fishao-fiyat.onrender.com FISHAO_UPLOAD_KEY=... python scripts/wiki_prices.py

Each fish page of https://www.fishao.net/wiki loads its data from /wiki/fish/<id>/__data.json, which carries
market.price (today's price) and market.change (percent change against yesterday). There is no bulk endpoint,
so the fish are fetched one by one, gently. wiki_fish.json lists the game's fish ids with their names; the
site matches the names to its own fish.

If every price equals the previous day's run (kept in LAST_FILE, which the workflow commits), the wiki
has probably not turned the day yet: wait and fetch again a few times before uploading.
"""
import gzip
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
WIKI = "https://www.fishao.net/wiki/fish/{}/__data.json"
LAST_FILE = Path(os.environ.get("WIKI_LAST_FILE", HERE / ".wiki-last.json"))
DELAY_SEC = 0.5  # one request at a time, about two a second
UNCHANGED_RETRIES = 2
UNCHANGED_WAIT_SEC = 15 * 60

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.fishao.net/wiki",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}

COOKIE_JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIE_JAR))


def get_json(url, data=None, headers=None, timeout=30):
    merged_headers = {**BROWSER_HEADERS, **(headers or {})}
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET", headers=merged_headers)
    with OPENER.open(req, timeout=timeout) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        return json.loads(raw.decode("utf-8", errors="replace"))


def retry_after(e):
    try:
        return min(max(int(e.headers.get("Retry-After", 60)), 5), 300)
    except (TypeError, ValueError):
        return 60


def fetch_market(fid):
    """(price, change) for one fish; None if the wiki has no such fish or no market price."""
    page = None
    for attempt in range(6):
        try:
            page = get_json(WIKI.format(fid))
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == 5:
                raise
            time.sleep(retry_after(e) if e.code == 429 else 5)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            if attempt == 5:
                raise
            time.sleep(5)
    if not page:
        return None
    nodes = page.get("nodes", [])
    if not nodes:
        return None
    node = nodes[-1]
    if not node or node.get("type") != "data":
        return None
    # SvelteKit's devalue format: values are indexes into the flat data list
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
        except Exception as e:  # keep going; report at the end
            failed.append(f"{f['id']} ({type(e).__name__})")
            m = None
        if m:
            fish.append({"id": f["id"], "name": f["en"], "name_tr": f["tr"], "price": m[0], "change": m[1]})
        if i % 10 == 0 or i == total:
            print(f"\rFetching prices: {i}/{total} ({len(fish)} found)...", end="", flush=True)
        time.sleep(DELAY_SEC)
    print()
    return fish, failed


def main():
    url = os.environ.get("FISHAO_SITE_URL", "").rstrip("/")
    key = os.environ.get("FISHAO_UPLOAD_KEY", "")
    if not url or not key:
        sys.exit("FISHAO_SITE_URL and FISHAO_UPLOAD_KEY are required.")
    fish_list = json.loads((HERE / "wiki_fish.json").read_text(encoding="utf-8"))
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last = json.loads(LAST_FILE.read_text(encoding="utf-8")) if LAST_FILE.exists() else {}
    before = {int(k): v for k, v in last.get("prices", {}).items()} if last.get("date") != day else {}

    for attempt in range(UNCHANGED_RETRIES + 1):
        fish, failed = fetch_all(fish_list)
        same = bool(before) and all(before.get(f["id"]) == f["price"] for f in fish)
        if not same or attempt == UNCHANGED_RETRIES:
            break
        print(f"All prices equal the previous day's; the wiki may not have turned the day yet. Retrying in {UNCHANGED_WAIT_SEC // 60} min.")
        time.sleep(UNCHANGED_WAIT_SEC)
    if not fish:
        sys.exit("No prices from the wiki." + (f" Failed: {', '.join(failed[:10])}" if failed else ""))

    LAST_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_FILE.write_text(json.dumps({"date": day, "prices": {f["id"]: f["price"] for f in fish}}), encoding="utf-8")
    changed = sum(1 for f in fish if before.get(f["id"]) not in (None, f["price"]))
    print(f"{len(fish)} fish priced ({day})" + (f"; {changed} changed since the previous day." if before else "."))
    if same:
        print("Warning: prices still equal the previous day's.")
    if failed:
        print(f"Could not fetch {len(failed)} fish: {', '.join(failed[:20])}")

    body = json.dumps({"fish": [{"name": f["name"], "name_tr": f["name_tr"], "worth": f["price"]} for f in fish]}).encode()
    for attempt in range(3):  # the free Render instance can take about a minute to wake up
        try:
            r = get_json(url + "/api/otomatik-yukle", data=body, timeout=120,
                         headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
            break
        except urllib.error.HTTPError as e:
            sys.exit(f"Upload refused ({e.code}): {e.read().decode(errors='replace')[:300]}")
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 2:
                sys.exit(f"Site unreachable: {e}")
            time.sleep(20)
    print(f"Uploaded: {r.get('saved')} fish for {r.get('day')}.")
    if r.get("missed"):
        print(f"Not matched on the site ({len(r['missed'])}): {', '.join(r['missed'])}")


if __name__ == "__main__":
    main()
