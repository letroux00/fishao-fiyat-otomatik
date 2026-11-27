"""Fetch today's fish market prices from the official Fishao wiki and upload them to this site.

Runs daily in GitHub Actions (.github/workflows/wiki-prices.yml); can also be run by hand:
    FISHAO_SITE_URL=https://fishao-fiyat.onrender.com FISHAO_UPLOAD_KEY=... python scripts/wiki_prices.py
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from curl_cffi import requests
    HAS_CURL_CFFI = True
except ImportError:
    import urllib.error
    import urllib.request
    import http.cookiejar
    import gzip
    HAS_CURL_CFFI = False

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
}

if HAS_CURL_CFFI:
    SESSION = requests.Session(impersonate="chrome120")
    SESSION.headers.update(BROWSER_HEADERS)
else:
    COOKIE_JAR = http.cookiejar.CookieJar()
    OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(COOKIE_JAR))


def fetch_market(fid):
    """(price, change) for one fish; None if not found/no price."""
    url = WIKI.format(fid)
    page = None
    for attempt in range(3):
        try:
            if HAS_CURL_CFFI:
                r = SESSION.get(url, timeout=20)
                if r.status_code == 404:
                    return None
                if r.status_code == 403:
                    raise RuntimeError(f"Cloudflare 403 Forbidden (Bot Block)")
                if r.status_code == 429:
                    time.sleep(15)
                    continue
                r.raise_for_status()
                page = r.json()
            else:
                req = urllib.request.Request(url, headers=BROWSER_HEADERS)
                with OPENER.open(req, timeout=20) as r:
                    raw = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    page = json.loads(raw.decode("utf-8", errors="replace"))
            break
        except Exception as e:
            err_msg = str(e)
            if "404" in err_msg:
                return None
            if "403" in err_msg:
                raise RuntimeError(f"Cloudflare 403 Forbidden: {e}")
            if attempt == 2:
                raise
            time.sleep(3)

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
    consecutive_blocks = 0
    for i, f in enumerate(fish_list, 1):
        try:
            m = fetch_market(f["id"])
            consecutive_blocks = 0
        except Exception as e:
            failed.append(f"{f['id']} ({e})")
            m = None
            if "403" in str(e):
                consecutive_blocks += 1
                if consecutive_blocks >= 3:
                    print(f"\n[HATA] Cloudflare bot korumasi (403) art arda 3 kez engelledi! Istekler durduruluyor.")
                    print(f"Hata detayi: {e}")
                    raise SystemExit("Cloudflare 403 engeli: IP veya istekler Cloudflare WAF tarafindan engellendi.")
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
    for attempt in range(3):
        try:
            if HAS_CURL_CFFI:
                r = SESSION.post(url + "/api/otomatik-yukle", data=body, headers=headers, timeout=120)
                r.raise_for_status()
                return r.json()
            else:
                req = urllib.request.Request(url + "/api/otomatik-yukle", data=body, method="POST", headers=headers)
                with OPENER.open(req, timeout=120) as r:
                    return json.loads(r.read())
        except Exception as e:
            if attempt == 2:
                sys.exit(f"Site unreachable or upload failed: {e}")
            time.sleep(20)


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
        print(f"All prices equal previous day; wiki may not have turned day yet. Retrying in {UNCHANGED_WAIT_SEC // 60} min.")
        time.sleep(UNCHANGED_WAIT_SEC)
    if not fish:
        sys.exit("No prices from the wiki." + (f" Failed: {', '.join(failed[:10])}" if failed else ""))

    LAST_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_FILE.write_text(json.dumps({"date": day, "prices": {f["id"]: f["price"] for f in fish}}), encoding="utf-8")
    changed = sum(1 for f in fish if before.get(f["id"]) not in (None, f["price"]))
    print(f"{len(fish)} fish priced ({day})" + (f"; {changed} changed since previous day." if before else "."))
    if same:
        print("Warning: prices still equal previous day.")
    if failed:
        print(f"Could not fetch {len(failed)} fish: {', '.join(failed[:20])}")

    r = upload_to_site(url, key, fish)
    print(f"Uploaded: {r.get('saved')} fish for {r.get('day')}.")
    if r.get("missed"):
        print(f"Not matched on the site ({len(r['missed'])}): {', '.join(r['missed'])}")


if __name__ == "__main__":
    main()
