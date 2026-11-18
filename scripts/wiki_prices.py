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


def get_json(url, data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"User-Agent": "fishao-price-tracker/1.0", "Accept": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def retry_after(e):
    try:
        return min(max(int(e.headers.get("Retry-After", 60)), 5), 300)
    except (TypeError, ValueError):
        return 60


def fetch_market(fid):
    """(price, change) for one fish; None if the wiki has no such fish or no market price."""
    for attempt in range(6):
        try:
            page = get_json(WIKI.format(fid))
            break
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            if attempt == 5:
                raise
            # 429: the wiki is rate limiting; wait as long as it asks (or a minute)
            time.sleep(retry_after(e) if getattr(e, "code", None) == 429 else 5)
    node = page["nodes"][-1]
    if not node or node.get("type") != "data":
        return None
    # SvelteKit's devalue format: values are indexes into the flat data list
    d = node["data"]
    market = d[d[0]["market"]] if "market" in d[0] else None
    if not isinstance(market, dict) or "price" not in market:
        return None
    price = d[market["price"]]
    change = d[market["change"]] if "change" in market else None
    return (price, change) if isinstance(price, int) and price > 0 else None


def fetch_all(fish_list):
    fish, failed = [], []
    for f in fish_list:
        try:
            m = fetch_market(f["id"])
        except Exception as e:  # keep going; report at the end
            failed.append(f"{f['id']} ({type(e).__name__})")
            m = None
        if m:
            fish.append({"id": f["id"], "name": f["en"], "name_tr": f["tr"], "price": m[0], "change": m[1]})
        time.sleep(DELAY_SEC)
    return fish, failed


def main():
    url, key = os.environ.get("FISHAO_SITE_URL", "").rstrip("/"), os.environ.get("FISHAO_UPLOAD_KEY", "")
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
