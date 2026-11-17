# Fishao wiki prices

Every day at 03:05 Turkey time, GitHub Actions fetches today's fish market prices from the official Fishao wiki
(`https://www.fishao.net/wiki`) and uploads them to the price site.

- `scripts/wiki_prices.py` fetches and uploads; `scripts/wiki_fish.json` lists the fish ids and names.
- `data/last.json` holds the last run's prices. If the wiki has not turned the day yet (all prices unchanged),
  the script waits and tries again.
- The upload key lives only in the repository secret `FISHAO_UPLOAD_KEY`.
