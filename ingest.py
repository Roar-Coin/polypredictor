#!/usr/bin/env python3
"""
Polymarket ingestion v2 — robust paginering + tydelig feillogging.
Se README for bruk. Lim denne over den gamle ingest.py i repoet.
"""

import argparse
import json
import time
from pathlib import Path

import pandas as pd
import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

DATA_DIR = Path("data")
PRICES_DIR = DATA_DIR / "prices"
CHECKPOINT = DATA_DIR / "checkpoint.json"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "poly-backtester-ingest/0.2"})

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


def get_json(url: str, params: dict | None = None, attempts: int = 5):
    """GET med backoff. Logger statuskode + svar ved feil. Returnerer None hvis alt feiler."""
    for attempt in range(1, attempts + 1):
        try:
            r = SESSION.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            print(f"  ! nettverksfeil ({attempt}/{attempts}): {e}", flush=True)
            time.sleep(min(2 ** attempt, 60))
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                print(f"  ! ugyldig JSON ({attempt}/{attempts}): {r.text[:200]}", flush=True)
        elif r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 0)) or min(10 * attempt, 120)
            print(f"  ! rate limit 429, venter {wait}s", flush=True)
            time.sleep(wait)
            continue
        else:
            print(f"  ! HTTP {r.status_code} pa {url} params={params}: {r.text[:200]}", flush=True)
        time.sleep(min(2 ** attempt, 60))
    return None


def parse_market(m: dict) -> dict:
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
    return {
        "market_id": str(m.get("id")),
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
    }


def fetch_all_markets() -> pd.DataFrame:
    """Paginert henting. Faller tilbake til mindre sidestorrelse hvis et offset feiler."""
    rows = []
    offset = 0
    while True:
        batch = None
        for limit in (500, 100, 20):
            batch = get_json(f"{GAMMA}/markets", {
                "closed": "true", "limit": limit, "offset": offset,
                "order": "id", "ascending": "true",
            }, attempts=3)
            if batch is not None:
                break
            print(f"  ! offset {offset} feilet med limit={limit}, prover mindre side", flush=True)
        if batch is None:
            print(f"  !! gir opp ved offset {offset} — lagrer det vi har ({len(rows)} markeder)", flush=True)
            break
        if not batch:
            break  # tom liste = ferdig
        rows.extend(parse_market(m) for m in batch)
        offset += len(batch)
        if offset % 2000 < len(batch):
            print(f"  markets fetched: {offset}", flush=True)
        time.sleep(0.5)
    df = pd.DataFrame(rows).drop_duplicates(subset="market_id")
    return df


def fetch_price_history(token_id: str, fidelity: int) -> pd.DataFrame | None:
    data = get_json(f"{CLOB}/prices-history", {
        "market": token_id, "interval": "max", "fidelity": fidelity,
    }, attempts=3)
    if not data:
        return None
    hist = data.get("history") or []
    if not hist:
        return None
    df = pd.DataFrame(hist)
    df["t"] = pd.to_datetime(df["t"], unit="s", utc=True)
    return df.rename(columns={"t": "timestamp", "p": "price"})


def load_checkpoint() -> set:
    if CHECKPOINT.exists():
        return set(json.loads(CHECKPOINT.read_text()))
    return set()


def save_checkpoint(done: set):
    CHECKPOINT.write_text(json.dumps(sorted(done)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fidelity", type=int, default=60)
    ap.add_argument("--category", default=None)
    ap.add_argument("--min-volume", type=float, default=0)
    args = ap.parse_args()

    DATA_DIR.mkdir(exist_ok=True)
    PRICES_DIR.mkdir(exist_ok=True)

    markets_path = DATA_DIR / "markets.parquet"
    if markets_path.exists():
        markets = pd.read_parquet(markets_path)
        print(f"Bruker eksisterende metadata: {len(markets)} markeder")
    else:
        print("Henter alle markeder fra Gamma ...")
        markets = fetch_all_markets()
        if markets.empty:
            raise SystemExit("Fikk ingen markeder — se loggen over for HTTP-feil.")
        markets.to_parquet(markets_path, index=False)
        print(f"Lagret {len(markets)} markeder -> {markets_path}")

    sel = markets
    if args.category:
        sel = sel[sel["category"] == args.category]
    if args.min_volume > 0:
        sel = sel[sel["volume"] >= args.min_volume]
    print(f"Henter prishistorikk for {len(sel)} markeder (fidelity={args.fidelity}m)")

    done = load_checkpoint()
    for i, (_, m) in enumerate(sel.iterrows(), 1):
        mid = str(m["market_id"])
        if mid in done:
            continue
        frames = []
        for j, tok in enumerate(json.loads(m["token_ids"])):
            df = fetch_price_history(tok, args.fidelity)
            if df is not None:
                df["outcome_index"] = j
                frames.append(df)
            time.sleep(0.25)
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
