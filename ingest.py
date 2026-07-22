#!/usr/bin/env python3
"""
Polymarket ingestion — henter ALLE avsluttede markeder + prishistorikk til Parquet.

Kjøres i skyen (GitHub Actions / VPS). Gjenopptar automatisk der den slapp
via checkpoint-fil, så den tåler å bli avbrutt og kjørt på nytt.

Output:
  data/markets.parquet               <- metadata for alle markeder
  data/prices/<category>/<market_id>.parquet  <- prishistorikk per marked

Bruk:
  pip install requests pandas pyarrow tenacity
  python ingest.py --fidelity 60          # timesoppløsning (rask første kjøring)
  python ingest.py --fidelity 1 --category crypto   # minuttdata for én kategori
"""

import argparse
import json
import time
from pathlib import Path

import pandas as pd
import requests
from tenacity import retry, stop_after_attempt, wait_exponential

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

DATA_DIR = Path("data")
PRICES_DIR = DATA_DIR / "prices"
CHECKPOINT = DATA_DIR / "checkpoint.json"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "poly-backtester-ingest/0.1"})

# Enkel kategorisering basert på tags/spørsmålstekst
CATEGORY_KEYWORDS = {
    "crypto": ["bitcoin", "btc", "ethereum", "eth", "solana", "crypto", "doge"],
    "politics": ["election", "president", "senate", "congress", "trump", "biden",
                 "parliament", "minister", "vote", "poll"],
    "sports": ["nba", "nfl", "mlb", "nhl", "ufc", "premier league", "champions league",
               "world cup", "super bowl", "vs.", "match", "game winner"],
    "economy": ["fed", "rate", "inflation", "cpi", "gdp", "recession", "jobs report"],
}


def categorize(market: dict) -> str:
    text = " ".join([
        str(market.get("question", "")),
        " ".join(t.get("label", "") if isinstance(t, dict) else str(t)
                 for t in (market.get("tags") or [])),
    ]).lower()
    for cat, words in CATEGORY_KEYWORDS.items():
        if any(w in text for w in words):
            return cat
    return "other"


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=60))
def get_json(url: str, params: dict | None = None):
    r = SESSION.get(url, params=params, timeout=30)
    if r.status_code == 429:
        time.sleep(10)
        r.raise_for_status()
    r.raise_for_status()
    return r.json()


def fetch_all_markets() -> pd.DataFrame:
    """Paginert henting av alle avsluttede markeder fra Gamma."""
    rows, offset, limit = [], 0, 500
    while True:
        batch = get_json(f"{GAMMA}/markets", {
            "closed": "true", "limit": limit, "offset": offset,
            "order": "endDate", "ascending": "false",
        })
        if not batch:
            break
        for m in batch:
            token_ids = m.get("clobTokenIds")
            if isinstance(token_ids, str):
                try:
                    token_ids = json.loads(token_ids)
                except json.JSONDecodeError:
                    token_ids = []
            outcomes = m.get("outcomes")
            if isinstance(outcomes, str):
                try:
                    outcomes = json.loads(outcomes)
                except json.JSONDecodeError:
                    outcomes = []
            rows.append({
                "market_id": m.get("id"),
                "condition_id": m.get("conditionId"),
                "question": m.get("question"),
                "category": categorize(m),
                "outcomes": json.dumps(outcomes),
                "token_ids": json.dumps(token_ids or []),
                "start_date": m.get("startDate"),
                "end_date": m.get("endDate"),
                "volume": float(m.get("volumeNum") or m.get("volume") or 0),
                "liquidity": float(m.get("liquidityNum") or m.get("liquidity") or 0),
                "resolved_outcome": m.get("outcomePrices"),
            })
        offset += limit
        print(f"  markets fetched: {offset}", flush=True)
        time.sleep(0.3)  # snill mot API-et
    df = pd.DataFrame(rows).drop_duplicates(subset="market_id")
    return df


def fetch_price_history(token_id: str, fidelity: int) -> pd.DataFrame | None:
    data = get_json(f"{CLOB}/prices-history", {
        "market": token_id, "interval": "max", "fidelity": fidelity,
    })
    hist = data.get("history") or []
    if not hist:
        return None
    df = pd.DataFrame(hist)          # kolonner: t (unix), p (pris)
    df["t"] = pd.to_datetime(df["t"], unit="s", utc=True)
    df = df.rename(columns={"t": "timestamp", "p": "price"})
    return df


def load_checkpoint() -> set:
    if CHECKPOINT.exists():
        return set(json.loads(CHECKPOINT.read_text()))
    return set()


def save_checkpoint(done: set):
    CHECKPOINT.write_text(json.dumps(sorted(done)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fidelity", type=int, default=60,
                    help="Oppløsning i minutter (1=minutt, 60=time, 1440=dag)")
    ap.add_argument("--category", default=None,
                    help="Begrens til én kategori (crypto/politics/sports/economy/other)")
    ap.add_argument("--min-volume", type=float, default=0,
                    help="Hopp over markeder med volum under dette")
    args = ap.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    PRICES_DIR.mkdir(exist_ok=True)

    # 1) Metadata
    markets_path = DATA_DIR / "markets.parquet"
    if markets_path.exists():
        markets = pd.read_parquet(markets_path)
        print(f"Bruker eksisterende metadata: {len(markets)} markeder")
    else:
        print("Henter alle markeder fra Gamma ...")
        markets = fetch_all_markets()
        markets.to_parquet(markets_path, index=False)
        print(f"Lagret {len(markets)} markeder -> {markets_path}")

    # 2) Filtrer
    sel = markets
    if args.category:
        sel = sel[sel["category"] == args.category]
    if args.min_volume > 0:
        sel = sel[sel["volume"] >= args.min_volume]
    print(f"Henter prishistorikk for {len(sel)} markeder (fidelity={args.fidelity}m)")

    # 3) Prishistorikk med resume
    done = load_checkpoint()
    for i, (_, m) in enumerate(sel.iterrows(), 1):
        mid = str(m["market_id"])
        if mid in done:
            continue
        token_ids = json.loads(m["token_ids"])
        frames = []
        for j, tok in enumerate(token_ids):
            try:
                df = fetch_price_history(tok, args.fidelity)
            except Exception as e:
                print(f"  ! {mid} token {j}: {e}")
                df = None
            if df is not None:
                df["outcome_index"] = j
                frames.append(df)
            time.sleep(0.2)
        if frames:
            out_dir = PRICES_DIR / m["category"]
            out_dir.mkdir(exist_ok=True)
            pd.concat(frames).to_parquet(out_dir / f"{mid}.parquet", index=False)
        done.add(mid)
        if i % 50 == 0:
            save_checkpoint(done)
            print(f"  {i}/{len(sel)} markeder ferdig", flush=True)
    save_checkpoint(done)
    print("Ferdig.")


if __name__ == "__main__":
    main()
