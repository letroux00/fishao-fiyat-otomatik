"""Fetch today's fish market prices from the official Fishao wiki and upload them to the price site.

Usage (from fishao-work):
    python -I tools/wiki_prices.py              # fetch, save, upload
    python -I tools/wiki_prices.py --no-upload  # fetch and save only

Each fish page of https://www.fishao.net/wiki loads its data from /wiki/fish/<id>/__data.json, which carries
market.price (today's price) and market.change (percent change shown as "+4% TODAY"). There is no bulk
endpoint, so the fish are fetched one by one, gently. The ids are the game's fish ids (mirror/data/langs).

Saves:
    Desktop/fishao_wiki_fiyatlari.json   today's list (id, name, name_tr, price, change)
    captured/wiki_prices.json            history, one entry per day
Upload settings come from tools/upload.env, the same as auto_prices.py.
"""
import argparse
import gzip
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from auto_prices import settings, upload  # noqa: E402
from extract_fish_worth import names  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WIKI = "https://www.fishao.net/wiki/fish/{}/__data.json"
DESKTOP_JSON = Path.home() / "Desktop" / "fishao_wiki_fiyatlari.json"
HISTORY_JSON = ROOT / "captured" / "wiki_prices.json"
SOURCE = "official Fishao wiki (www.fishao.net/wiki), market price and percent change of the day"
DELAY_SEC = 0.5  # one request at a time, about two a second
UNCHANGED_RETRIES = 2  # if every price equals the last run's, the wiki may not have turned the day yet
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


def retry_after(e):
    try:
        return min(max(int(e.headers.get("Retry-After", 60)), 5), 300)
    except (TypeError, ValueError):
        return 60


def fetch_market(fid):
    """(price, change) for one fish; None if the wiki has no such fish or no market price."""
    req = urllib.request.Request(WIKI.format(fid), headers=BROWSER_HEADERS)
    page = None
    for attempt in range(6):
        try:
            with OPENER.open(req, timeout=30) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                page = json.loads(raw.decode("utf-8", errors="replace"))
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
    node = page.get("nodes", [])
    if not node:
        return None
    last_node = node[-1]
    if not last_node or last_node.get("type") != "data":
        return None
    # SvelteKit's devalue format: values are indexes into the flat data list
    d = last_node.get("data")
    if not d or not isinstance(d, list) or not d[0]:
        return None
    market = d[d[0]["market"]] if "market" in d[0] else None
    if not isinstance(market, dict) or "price" not in market:
        return None
    price, change = d[market["price"]], d[market["change"]] if "change" in market else None
    return (price, change) if isinstance(price, int) and price > 0 else None


def fetch_all():
    en, tr = names()
    ids = sorted(int(k[5:]) for k in en if k.startswith("fish_") and k[5:].isdigit())
    fish, failed = [], []
    total = len(ids)
    for i, fid in enumerate(ids, 1):
        try:
            m = fetch_market(fid)
        except Exception as e:  # keep going; report at the end
            failed.append(f"{fid} ({type(e).__name__})")
            m = None
        if m:
            fish.append({"id": fid, "name": en.get(f"fish_{fid}"), "name_tr": tr.get(f"fish_{fid}"), "price": m[0], "change": m[1]})
        if i % 10 == 0 or i == total:
            print(f"\rBalıklar çekiliyor: {i}/{total} ({len(fish)} fiyat bulundu)...", end="", flush=True)
        time.sleep(DELAY_SEC)
    print()
    return fish, failed


def last_prices(day):
    """Prices saved on an earlier day; a run earlier the same day is not a reason to wait for the wiki."""
    if not DESKTOP_JSON.exists():
        return {}
    saved = json.loads(DESKTOP_JSON.read_text(encoding="utf-8"))
    if saved.get("date") == day:
        return {}
    return {f["id"]: f["price"] for f in saved.get("fish", [])}


def save(fish, day):
    DESKTOP_JSON.write_text(json.dumps({"_source": SOURCE, "date": day, "fish": fish}, ensure_ascii=False, indent=1), encoding="utf-8")
    hist = json.loads(HISTORY_JSON.read_text(encoding="utf-8")) if HISTORY_JSON.exists() else {"_source": SOURCE}
    hist[day] = {str(f["id"]): [f["price"], f["change"]] for f in fish}
    HISTORY_JSON.write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-upload", action="store_true", help="only save the files")
    args = ap.parse_args()
    conf = settings()
    if not args.no_upload and not (conf.get("FISHAO_SITE_URL") and conf.get("FISHAO_UPLOAD_KEY")):
        raise SystemExit("FISHAO_SITE_URL ve FISHAO_UPLOAD_KEY gerekli (tools/upload.env), ya da --no-upload kullan.")

    day = datetime.now().strftime("%Y-%m-%d")
    before = last_prices(day)
    for attempt in range(UNCHANGED_RETRIES + 1):
        fish, failed = fetch_all()
        same = before and all(before.get(f["id"]) == f["price"] for f in fish)
        if not same or attempt == UNCHANGED_RETRIES:
            break
        print(f"Fiyatların hepsi bir önceki çekimle aynı; wiki günü çevirmemiş olabilir, {UNCHANGED_WAIT_SEC // 60} dk sonra tekrar.")
        time.sleep(UNCHANGED_WAIT_SEC)
    if not fish:
        raise SystemExit("Wikiden hiç fiyat gelmedi." + (f" Hatalı: {', '.join(failed[:10])}" if failed else ""))

    save(fish, day)
    changed = sum(1 for f in fish if before.get(f["id"]) not in (None, f["price"]))
    print(f"{len(fish)} balığın fiyatı alındı ({day})" + (f"; önceki güne göre {changed} fiyat değişti." if before else "."))
    if same:
        print("  Uyarı: fiyatlar bir önceki çekimle hâlâ aynı.")
    if failed:
        print(f"  Alınamayan {len(failed)} balık: {', '.join(failed[:20])}")
    if not args.no_upload:
        r = upload([{"name": f["name"], "name_tr": f["name_tr"], "worth": f["price"]} for f in fish], conf)
        print(f"Siteye yüklendi: {r.get('saved')} balık, gün {r.get('day')}.")
        if r.get("missed"):
            print(f"  Sitede eşleşmeyen {len(r['missed'])} balık: {', '.join(r['missed'])}")


if __name__ == "__main__":
    main()
