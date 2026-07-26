#!/usr/bin/env python3
"""
Polymarket ingestion v3 — henter markeder maaned for maaned (unngaar dype offsets),
og hopper over markeder uten CLOB-historikk med tydelig logging.
"""

import argparse
import json
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

DATA_DIR = Path("data")
PRICES_DIR = DATA_DIR / "prices"
CHECKPOINT = DATA_DIR / "checkpoint.json"
NOHISTORY = DATA_DIR / "nohistory.json"
META_DONE = DATA_DIR / "meta_complete.flag"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "poly-backtester-ingest/0.3"})

import re as _re

TAG_MAP = {
    "crypto": "crypto", "bitcoin": "crypto", "ethereum": "crypto", "solana": "crypto",
    "memecoins": "crypto", "stablecoins": "crypto", "defi": "crypto", "nft": "crypto",
    "politics": "politics", "elections": "politics", "geopolitics": "politics",
    "us-current-affairs": "politics", "world": "politics", "trump": "politics",
    "sports": "sports", "nba": "sports", "nfl": "sports", "mlb": "sports",
    "nhl": "sports", "soccer": "sports", "epl": "sports", "ufc": "sports",
    "mma": "sports", "tennis": "sports", "golf": "sports", "esports": "sports",
    "olympics": "sports", "formula-1": "sports", "cricket": "sports",
    "economy": "economy", "fed": "economy", "macro": "economy", "finance": "economy",
    "business": "economy", "economics": "economy",
}

CATEGORY_KEYWORDS = {
    "crypto": ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto",
               "doge", "dogecoin", "xrp", "memecoin", "altcoin"],
    "politics": ["election", "president", "senate", "congress", "trump", "biden",
                 "parliament", "minister", "vote", "poll", "ceasefire", "impeach"],
    "sports": ["nba", "nfl", "mlb", "nhl", "ufc", "premier league", "champions league",
               "world cup", "super bowl", "grand slam", "wimbledon", "goalscorer",
               "playoffs", "draft"],
    "economy": ["fed", "rate hike", "rate cut", "inflation", "cpi", "gdp",
                "recession", "jobs report", "tariff"],
}
_WORD_RES = {cat: [_re.compile(r"\b" + _re.escape(w) + r"\b") for w in words]
             for cat, words in CATEGORY_KEYWORDS.items()}


def tag_labels(market: dict):
    out = []
    for t in (market.get("tags") or []):
        label = t.get("slug") or t.get("label") if isinstance(t, dict) else str(t)
        if label:
            out.append(str(label).lower())
    return out


def categorize(market: dict) -> str:
    for label in tag_labels(market):
        if label in TAG_MAP:
            return TAG_MAP[label]
    text = str(market.get("question", "")).lower()
    for cat, regexes in _WORD_RES.items():
        if any(r.search(text) for r in regexes):
            return cat
    return "other"


def get_json(url, params=None, attempts=5):
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
                print(f"  ! ugyldig JSON: {r.text[:200]}", flush=True)
        elif r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 0)) or min(10 * attempt, 120)
            print(f"  ! rate limit, venter {wait}s", flush=True)
            time.sleep(wait)
            continue
        else:
            print(f"  ! HTTP {r.status_code} params={params}: {r.text[:200]}", flush=True)
        time.sleep(min(2 ** attempt, 60))
    return None


def parse_market(m):
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
        "tags": json.dumps(tag_labels(m)),
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


def month_windows(start_year=2020):
    """(fra, til)-par per maaned frem til i dag."""
    windows = []
    y, m = start_year, 1
    today = date.today()
    while (y, m) <= (today.year, today.month):
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        windows.append((f"{y}-{m:02d}-01T00:00:00Z", f"{ny}-{nm:02d}-01T00:00:00Z"))
        y, m = ny, nm
    return windows


OFFSET_LIMIT = 2000     # Gamma nekter dypere paginering enn dette


def _parse_iso(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_window(start, end, depth=0):
    """Hent alle markeder som avsluttes i [start, end). Deler vinduet i to hvis
    offset-grensen treffes, saa ingen markeder gaar tapt."""
    rows, offset, hit_limit = [], 0, False
    while True:
        if offset >= OFFSET_LIMIT:
            hit_limit = True
            break
        batch = get_json(f"{GAMMA}/markets", {
            "closed": "true", "limit": 500, "offset": offset,
            "end_date_min": start, "end_date_max": end,
        }, attempts=4)
        if batch is None:
            print(f"  !! {start[:10]}–{end[:10]}: gir opp ved offset {offset}", flush=True)
            break
        if not batch:
            break
        rows.extend(parse_market(m) for m in batch)
        offset += len(batch)
        time.sleep(0.35)

    if hit_limit:
        a, b = _parse_iso(start), _parse_iso(end)
        if depth >= 8 or (b - a) <= timedelta(hours=1):
            print(f"  !! {start[:10]}–{end[:10]}: kan ikke deles finere, "
                  f"noen markeder kan mangle", flush=True)
            return rows
        mid = a + (b - a) / 2
        print(f"  · {start[:10]}–{end[:10]} traff offset-grensen — deler vinduet", flush=True)
        return fetch_window(start, _iso(mid), depth + 1) + fetch_window(_iso(mid), end, depth + 1)
    return rows


def fetch_all_markets() -> pd.DataFrame:
    rows = []
    for start, end in month_windows():
        got = fetch_window(start, end)
        rows.extend(got)
        print(f"  {start[:7]}: {len(got)} markeder (totalt {len(rows)})", flush=True)
    df = pd.DataFrame(rows).drop_duplicates(subset="market_id")
    return df


def fetch_price_history(token_id, fidelity):
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


def load_checkpoint():
    if CHECKPOINT.exists():
        return set(json.loads(CHECKPOINT.read_text()))
    return set()


def save_checkpoint(done):
    CHECKPOINT.write_text(json.dumps(sorted(done)))


def load_nohistory():
    if NOHISTORY.exists():
        return set(json.loads(NOHISTORY.read_text()))
    return set()


def save_nohistory(nohist):
    NOHISTORY.write_text(json.dumps(sorted(nohist)))


EPOCH0 = pd.Timestamp("1970-01-01", tz="UTC")


def to_epoch(series):
    """ISO-tekst eller tidsstempel -> sekunder siden 1970 (float, NaN hvis ugyldig).
    Nettleseren regner da kun med tall — ingen tidssone-funksjoner kreves."""
    d = pd.to_datetime(series, utc=True, errors="coerce")
    return (d - EPOCH0).dt.total_seconds()


def publish(markets):
    """Slaa sammen prisfiler per kategori og legg alt klart for opplasting."""
    pub = DATA_DIR / "publish"
    pub.mkdir(exist_ok=True)
    manifest = {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "categories": {}, "schema": 2}
    for catdir in sorted(p for p in PRICES_DIR.iterdir() if p.is_dir()):
        frames = []
        for f in catdir.glob("*.parquet"):
            df = pd.read_parquet(f)
            df["market_id"] = f.stem
            frames.append(df)
        if not frames:
            continue
        big = pd.concat(frames, ignore_index=True)
        big["ts_epoch"] = to_epoch(big["timestamp"])
        out = pub / f"prices-{catdir.name}.parquet"
        big.to_parquet(out, index=False)
        manifest["categories"][catdir.name] = {
            "markets": len(frames), "rows": int(len(big)),
            "bytes": out.stat().st_size,
        }
    mk = markets.copy()
    mk["start_epoch"] = to_epoch(mk["start_date"])
    mk["end_epoch"] = to_epoch(mk["end_date"])
    mk.to_parquet(pub / "markets.parquet", index=False)
    (pub / "manifest.json").write_text(json.dumps(manifest))
    print(f"Publisert {len(manifest['categories'])} kategorier til data/publish/ "
          f"(schema 2 med epoch-kolonner)", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fidelity", type=int, default=60)
    ap.add_argument("--category", default=None)
    ap.add_argument("--min-volume", type=float, default=0)
    ap.add_argument("--since", default=None,
                    help="Kun markeder avsluttet etter denne datoen, f.eks. 2024-01-01")
    ap.add_argument("--since-days", type=int, default=180,
                    help="Brukes hvis --since ikke er satt: hent kun markeder avsluttet "
                         "de siste N dagene (eldre historikk er permanent slettet av API-et). "
                         "0 = ingen grense.")
    ap.add_argument("--max-seconds", type=int, default=18900,
                    help="Avslutt pent etter saa mange sekunder (default 5t15m)")
    args = ap.parse_args()
    t0 = time.monotonic()

    DATA_DIR.mkdir(exist_ok=True)
    PRICES_DIR.mkdir(exist_ok=True)

    markets_path = DATA_DIR / "markets.parquet"
    meta_ok = False
    if markets_path.exists() and META_DONE.exists():
        markets = pd.read_parquet(markets_path)
        if "tags" in markets.columns:
            meta_ok = True
            print(f"Bruker komplett metadata: {len(markets)} markeder")
        else:
            print("Metadata mangler tags (gammel versjon) — henter paa nytt for riktig kategorisering.")
    if meta_ok:
        # Inkrementell oppdatering: refetch siste 60 dager (nye markeder + endelige utfall)
        cutoff = (date.today() - timedelta(days=60)).isoformat() + "T00:00:00Z"
        horizon = (date.today() + timedelta(days=2)).isoformat() + "T00:00:00Z"
        fresh = fetch_window(cutoff, horizon)
        if fresh:
            fresh_df = pd.DataFrame(fresh).drop_duplicates(subset="market_id")
            n_new = len(set(fresh_df["market_id"]) - set(markets["market_id"]))
            markets = pd.concat([
                markets[~markets["market_id"].isin(fresh_df["market_id"])],
                fresh_df,
            ], ignore_index=True)
            markets.to_parquet(markets_path, index=False)
            print(f"Metadata oppdatert: {len(fresh_df)} markeder refetchet, {n_new} nye. Totalt {len(markets)}.")
    if not meta_ok:
        if markets_path.exists():
            print("Fant ufullstendig metadata fra tidligere kjoring — henter paa nytt.")
        print("Henter alle markeder (maaned for maaned) ...")
        markets = fetch_all_markets()
        if markets.empty:
            raise SystemExit("Fikk ingen markeder — se loggen for HTTP-feil.")
        markets.to_parquet(markets_path, index=False)
        META_DONE.write_text("ok")
        print(f"Lagret {len(markets)} markeder -> {markets_path}")

    # Rydd: flytt prisfiler som ligger i feil kategorimappe
    cat_by_id = dict(zip(markets["market_id"].astype(str), markets["category"]))
    moved = 0
    if PRICES_DIR.exists():
        for f in PRICES_DIR.glob("*/*.parquet"):
            correct = cat_by_id.get(f.stem)
            if correct and f.parent.name != correct:
                dest = PRICES_DIR / correct
                dest.mkdir(exist_ok=True)
                f.rename(dest / f.name)
                moved += 1
    if moved:
        print(f"Flyttet {moved} prisfiler til riktig kategorimappe.")

    sel = markets
    since = args.since
    if not since and args.since_days > 0:
        since = (date.today() - timedelta(days=args.since_days)).isoformat()
    if since:
        before = len(sel)
        sel = sel[sel["end_date"].fillna("") >= since]
        print(f"Tidsfilter: {len(sel)} av {before} markeder avsluttet etter {since[:10]} "
              f"(eldre historikk finnes ikke i API-et)")
    sel = sel.sort_values("end_date", ascending=False, na_position="last")
    if args.category:
        sel = sel[sel["category"] == args.category]
    if args.min_volume > 0:
        dur_min = (pd.to_datetime(sel["end_date"], errors="coerce", utc=True)
                   - pd.to_datetime(sel["start_date"], errors="coerce", utc=True)
                   ).dt.total_seconds() / 60
        short = dur_min.notna() & (dur_min <= 300)  # <= 5 timer (5m/15m/1h/4h-markeder)
        sel = sel[(sel["volume"] >= args.min_volume) | (short & (sel["volume"] >= 50))]
    sel = sel[sel["token_ids"] != "[]"]
    print(f"Henter prishistorikk for {len(sel)} markeder (fidelity={args.fidelity}m)")

    on_disk = {f.stem for f in PRICES_DIR.glob("*/*.parquet")}
    nohist = load_nohistory()
    old_done = load_checkpoint()
    done = on_disk | nohist
    lost = len(old_done - done)
    print(f"Verifisert checkpoint: {len(on_disk)} filer paa disk, "
          f"{len(nohist)} bekreftet uten historikk"
          + (f", {lost} tidligere 'ferdige' uten fil — proves paa nytt" if lost else ""),
          flush=True)
    save_checkpoint(done)

    def stop_cleanly():
        save_checkpoint(done)
        save_nohistory(nohist)
        publish(markets)
        print(f"Tidsbudsjett naadd — lagrer og avslutter pent. "
              f"({len(done)}/{len(sel)} avklart, {len(nohist)} uten historikk totalt)", flush=True)

    for i, (_, m) in enumerate(sel.iterrows(), 1):
        if time.monotonic() - t0 > args.max_seconds:
            stop_cleanly()
            return
        mid = str(m["market_id"])
        if mid in done:
            continue
        frames = []
        used_fallback = False
        for j, tok in enumerate(json.loads(m["token_ids"])):
            df = fetch_price_history(tok, args.fidelity)
            if df is None and args.fidelity > 1:
                df = fetch_price_history(tok, 1)   # kortlivet marked? prov minuttdata
                if df is not None:
                    used_fallback = True
            if df is not None:
                df["outcome_index"] = j
                frames.append(df)
            time.sleep(0.25)
        if frames:
            out_dir = PRICES_DIR / m["category"]
            out_dir.mkdir(exist_ok=True)
            if used_fallback:
                for fr in frames:
                    fr["fidelity_min"] = 1
            pd.concat(frames).to_parquet(out_dir / f"{mid}.parquet", index=False)
        else:
            nohist.add(mid)
        done.add(mid)
        if i % 50 == 0:
            save_checkpoint(done)
            save_nohistory(nohist)
            print(f"  {i}/{len(sel)} ferdig ({len(nohist)} uten historikk)", flush=True)
    save_checkpoint(done)
    save_nohistory(nohist)
    publish(markets)
    print(f"Ferdig. {len(nohist)} markeder manglet CLOB-historikk.")


if __name__ == "__main__":
    main()
