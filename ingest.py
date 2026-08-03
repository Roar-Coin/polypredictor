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
ZOOMED = DATA_DIR / "zoomed.json"
META_DONE = DATA_DIR / "meta_complete.flag"

ERROR = object()   # skiller "API-et feilet" fra "API-et sa: ingen data"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "poly-backtester-ingest/0.3"})

import re as _re

# Gamma returnerer tags kun naar ?include_tag=true sendes med. Uten den var
# hele dette kartet doed kode og alt ble kategorisert paa nokkelord alene.
#
# Rekkefolgen i PRIORITY avgjor — IKKE rekkefolgen Polymarket sender taggene i.
# Et CS2-marked er tagget baade "sports" og "esports"; uten prioritet ville det
# havnet i sport sammen med Wimbledon.
PRIORITY = ["crypto", "esports", "weather", "stocks",
            "sports", "politics", "economy", "culture"]

TAG_MAP = {
    "crypto": {
        "crypto", "crypto-prices", "bitcoin", "ethereum", "solana", "memecoins",
        "stablecoins", "defi", "nft", "altcoins", "xrp", "dogecoin", "bnb",
        "cardano", "chainlink", "avalanche", "litecoin", "pepe", "shiba-inu",
        "crypto-etf", "hourly-crypto",
    },
    "esports": {
        "esports", "counter-strike-2", "counter-strike", "cs2", "league-of-legends",
        "dota-2", "valorant", "call-of-duty", "rocket-league", "overwatch",
        "starcraft", "apex-legends",
    },
    "weather": {"weather", "climate", "temperature", "hurricane", "hurricanes"},
    "stocks": {
        "stocks", "earnings", "equities", "ipo", "etf", "nasdaq", "sp500",
        "companies", "tech-stocks",
    },
    "sports": {
        "sports", "nba", "nfl", "mlb", "nhl", "soccer", "epl", "ufc", "mma",
        "tennis", "golf", "olympics", "formula-1", "cricket", "basketball",
        "baseball", "football", "hockey", "boxing", "atp", "wta", "itf",
        "champions-league", "europa-league", "fifa-world-cup", "wnba", "ncaa",
        "college-football", "college-basketball", "rugby", "cycling", "chess",
        "major-league-cricket", "nba-playoffs", "nba-finals", "wc-tournament-futures",
    },
    "politics": {
        "politics", "elections", "geopolitics", "us-current-affairs", "world",
        "trump", "foreign-affairs", "international-affairs", "house-races",
        "us-elections", "democratic-party", "republican-party", "legal-cases",
        "senate-races", "governor-races",
    },
    "economy": {
        "economy", "fed", "macro", "finance", "business", "economics",
        "macro-graph", "macro-single", "inflation", "fdic", "tariffs",
    },
    "culture": {
        "pop-culture", "movies", "music", "awards", "oscars", "entertainment",
        "tv", "celebrities", "openai", "ai", "science", "space",
    },
}

# Tags som ikke sier noe om emne — de skal aldri styre kategorien.
NOISE_TAGS = {"all", "recurring", "hide-from-new", "multi-strikes", "games",
              "1h", "1d", "weekly", "monthly", "daily", "featured", "new"}

# Nokkelord er naa bare et sikkerhetsnett for markeder uten brukbare tags.
CATEGORY_KEYWORDS = {
    "crypto": ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto",
               "doge", "dogecoin", "xrp", "memecoin", "altcoin", "bnb", "cardano",
               "ada", "chainlink", "avax", "litecoin", "pepe"],
    "weather": ["highest temperature", "lowest temperature", "rainfall",
                "snowfall", "hurricane"],
    "stocks": ["up or down on", "beat quarterly earnings", "market cap",
               "finish week of", "ipo day"],
    "politics": ["election", "president", "senate", "congress", "trump", "biden",
                 "parliament", "minister", "vote", "poll", "ceasefire", "impeach"],
    "sports": ["nba", "nfl", "mlb", "nhl", "ufc", "premier league", "champions league",
               "world cup", "super bowl", "grand slam", "wimbledon", "goalscorer",
               "playoffs", "draft", "itf", "o/u", "spread", "handicap", "exact score",
               "set 1 winner", "set 2 winner", "set 3 winner"],
    "economy": ["fed", "rate hike", "rate cut", "inflation", "cpi", "gdp",
                "recession", "jobs report", "tariff"],
}
_WORD_RES = {cat: [_re.compile(r"\b" + _re.escape(w) + r"\b") for w in words]
             for cat, words in CATEGORY_KEYWORDS.items()}
_TAG_TO_CAT = {slug: cat for cat, slugs in TAG_MAP.items() for slug in slugs}


def tag_labels(market: dict):
    out = []
    for t in (market.get("tags") or []):
        label = t.get("slug") or t.get("label") if isinstance(t, dict) else str(t)
        if label:
            out.append(str(label).lower())
    # Gamma legger av og til taggene paa event-objektet i stedet
    if not out:
        for ev in (market.get("events") or []):
            for t in (ev.get("tags") or []):
                label = t.get("slug") or t.get("label") if isinstance(t, dict) else str(t)
                if label:
                    out.append(str(label).lower())
    return out


def categorize(market: dict) -> str:
    hits = {_TAG_TO_CAT[l] for l in tag_labels(market)
            if l not in NOISE_TAGS and l in _TAG_TO_CAT}
    if hits:
        for cat in PRIORITY:          # var: forste tag Polymarket tilfeldigvis sendte
            if cat in hits:
                return cat
    text = str(market.get("question", "")).lower()
    for cat in PRIORITY:
        for r in _WORD_RES.get(cat, []):
            if r.search(text):
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
            if 500 <= r.status_code < 600:
                # serveren deres er overbelastet — vent lenger enn vanlig
                time.sleep(min(15 * attempt, 90))
                continue
        time.sleep(min(2 ** attempt, 60))
    return ERROR


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
        # Det EKTE handelsvinduet. startDate er naar markedet ble opprettet — for
        # "Bitcoin Up or Down - 6:15PM-6:20PM" er den et dogn for oppgjor, saa
        # varighet regnet fra start_date blir 1430 min i stedet for 5.
        "event_start": m.get("eventStartTime") or m.get("gameStartTime"),
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
            "include_tag": "true",
        }, attempts=4)
        if batch is ERROR:
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


def fetch_all_markets(since_iso=None, deadline=None, on_month=None) -> pd.DataFrame:
    """since_iso: hopp over maaneder som slutter for dette. deadline: monotonic-frist.

    Nyeste maaned forst, slik at en avbrutt kjoring likevel har hentet det som
    betyr noe — CLOB har uansett slettet prishistorikken for eldre markeder.
    """
    rows = []
    wins = list(month_windows())
    if since_iso:
        wins = [w for w in wins if w[1] >= since_iso]
    for start, end in reversed(wins):
        if deadline is not None and time.monotonic() > deadline:
            print(f"  !! frist naadd i metadata — stopper ved {start[:7]}", flush=True)
            break
        got = fetch_window(start, end)
        rows.extend(got)
        print(f"  {start[:7]}: {len(got)} markeder (totalt {len(rows)})", flush=True)
        if on_month:
            on_month(rows)          # delvis lagring, saa arbeidet ikke gaar tapt
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).drop_duplicates(subset="market_id")


def fetch_price_history(token_id, fidelity, window=None):
    """DataFrame = data, None = bekreftet ingen historikk, ERROR = kallet feilet.

    window=(fra_epoch, til_epoch) ber om et smalt utsnitt i stedet for hele
    levetiden. Det er den eneste maaten aa faa fin opplosning paa: CLOB gir
    ~144 punkter uansett fidelity naar interval=max, saa et dognlangt marked
    med fem minutters oppgjorsvindu ender paa ti minutter mellom punktene.
    Med startTs/endTs over samme vindu faar man 60 sekunder. Merk at interval
    og startTs ikke kan kombineres — da returnerer CLOB tomt.
    """
    params = {"market": token_id, "fidelity": fidelity}
    if window:
        params["startTs"], params["endTs"] = int(window[0]), int(window[1])
    else:
        params["interval"] = "max"
    data = get_json(f"{CLOB}/prices-history", params, attempts=3)
    if data is ERROR:
        return ERROR
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


def load_zoomed():
    """Markeder som allerede har faatt det finkornede vindus-kallet.

    Egen liste fordi `checkpoint.json` betyr 'grov historikk hentet'. Uten
    dette skillet hopper loekka over hvert eneste marked som ble hentet for
    vindus-kallet fantes — som er nettopp de vi vil ha finkornet."""
    if ZOOMED.exists():
        return set(json.loads(ZOOMED.read_text()))
    return set()


def save_zoomed(zoomdone):
    ZOOMED.write_text(json.dumps(sorted(zoomdone)))


def merge_price_file(path, frames):
    """Legg nye barer inn i en eksisterende prisfil. Finest fidelity vinner."""
    if path.exists():
        try:
            frames = [pd.read_parquet(path)] + frames
        except Exception:
            pass
    out = pd.concat(frames)
    out = (out.sort_values("fidelity_min")
              .drop_duplicates(subset=["outcome_index", "timestamp"], keep="first")
              .sort_values(["outcome_index", "timestamp"]))
    out.to_parquet(path, index=False)


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
    # Appen kan naa regne varighet fra handelsvinduet i stedet for opprettelsen
    mk["event_epoch"] = (to_epoch(mk["event_start"]) if "event_start" in mk.columns
                         else pd.Series(pd.NA, index=mk.index))
    mk.to_parquet(pub / "markets.parquet", index=False)
    (pub / "manifest.json").write_text(json.dumps(manifest))
    print(f"Publisert {len(manifest['categories'])} kategorier til data/publish/ "
          f"(schema 2 med epoch-kolonner)", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fidelity", type=int, default=60)
    ap.add_argument("--zoom-max-minutes", type=int, default=60,
                    help="Hent ekstra finkornet vindu naar oppgjorsvinduet er "
                         "kortere enn dette (0 = av).")
    ap.add_argument("--zoom-min-volume", type=float, default=5000,
                    help="Bare for markeder over dette volumet — ett ekstra kall hver.")
    ap.add_argument("--zoom-pad-minutes", type=int, default=15,
                    help="Hvor lenge for event_start vinduet skal begynne.")
    ap.add_argument("--zoom-budget-seconds", type=int, default=5400,
                    help="Maks tid brukt paa aa etterfylle vinduer i markeder som "
                         "allerede har grov historikk (default 1t30m). Resten av "
                         "kjoringen samler nye markeder som vanlig. 0 = av.")
    ap.add_argument("--category", default=None)
    ap.add_argument("--min-volume", type=float, default=0)
    ap.add_argument("--short-min-volume", type=float, default=200,
                    help="Volumgulv for kortlivede markeder (<=5t). Polymarket lager "
                         "tusenvis av smaa 5-minuttersmarkeder; de fleste har ingen handler.")
    ap.add_argument("--since", default=None,
                    help="Kun markeder avsluttet etter denne datoen, f.eks. 2024-01-01")
    ap.add_argument("--since-days", type=int, default=180,
                    help="Brukes hvis --since ikke er satt: hent kun markeder avsluttet "
                         "de siste N dagene (eldre historikk er permanent slettet av API-et). "
                         "0 = ingen grense.")
    ap.add_argument("--tag-backfill-days", type=int, default=300,
                    help="Ved manglende tags: hent metadata bare saa langt tilbake. "
                         "Eldre markeder har uansett ingen prishistorikk i CLOB.")
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
        # Kolonnen "tags" fantes ogsaa for include_tag=true — den var bare full av
        # tomme lister. Sjekk innholdet, ikke bare at kolonnen er der, ellers ville
        # den inkrementelle veien beholdt 687k feilkategoriserte rader for alltid.
        filled = (markets["tags"].astype(str).str.len() > 2).mean() \
            if "tags" in markets.columns else 0.0
        # Samme selvhelbredende monster som for tags: mangler en kolonne vi naa
        # trenger, hentes metadata paa nytt av seg selv. Aa maatte huske aa slette
        # meta_complete.flag manuelt er en feil som skjer stille.
        has_evt = "event_start" in markets.columns
        if filled > 0.5 and has_evt:
            meta_ok = True
            print(f"Bruker komplett metadata: {len(markets)} markeder "
                  f"({filled*100:.0f} % med tags)")
        elif not has_evt:
            print("Metadata mangler kolonnen event_start (det ekte handelsvinduet) "
                  "— henter paa nytt.")
        else:
            print(f"Metadata har tags paa bare {filled*100:.1f} % av radene — "
                  f"henter alt paa nytt med include_tag=true.")
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
        have = markets_path.exists()
        old_df = pd.read_parquet(markets_path) if have else pd.DataFrame()
        # Full gjenoppbygging tar lengre tid enn Actions tillater. Hent bare den
        # perioden som faktisk har prishistorikk, og flett inn i det vi har.
        since = None
        if have and len(old_df):
            since = (date.today() - timedelta(days=args.tag_backfill_days)).isoformat()
            print(f"Henter metadata paa nytt fra {since} og flettes inn i "
                  f"{len(old_df)} eksisterende rader.")
        else:
            print("Henter alle markeder (maaned for maaned) ...")

        # Reserver tid til prisløkka og publisering — metadata faar hoyst halve budsjettet.
        meta_deadline = t0 + args.max_seconds * 0.5

        def save_partial(rows):
            if not rows:
                return
            part = pd.DataFrame(rows).drop_duplicates(subset="market_id")
            merged = (pd.concat([old_df[~old_df["market_id"].isin(part["market_id"])], part],
                                ignore_index=True) if len(old_df) else part)
            merged.to_parquet(markets_path, index=False)

        fresh = fetch_all_markets(since_iso=since, deadline=meta_deadline,
                                  on_month=save_partial)
        if fresh.empty and not len(old_df):
            raise SystemExit("Fikk ingen markeder — se loggen for HTTP-feil.")
        markets = (pd.concat([old_df[~old_df["market_id"].isin(fresh["market_id"])], fresh],
                             ignore_index=True) if len(old_df) and len(fresh)
                   else (fresh if len(fresh) else old_df))
        markets = markets.drop_duplicates(subset="market_id")
        print(f"Metadata: {len(fresh)} hentet paa nytt, {len(markets)} totalt.")

        # Regn om kategori for ALLE rader — ogsaa de gamle, som naa nyter godt av
        # den utvidede nokkelordslista selv om de mangler tags.
        def _recat(r):
            try:
                tg = json.loads(r["tags"]) if isinstance(r["tags"], str) else (r["tags"] or [])
            except (json.JSONDecodeError, TypeError):
                tg = []
            return categorize({"tags": tg, "question": r["question"]})
        markets["category"] = markets.apply(_recat, axis=1)
        markets.to_parquet(markets_path, index=False)
        META_DONE.write_text("ok")
        print(f"Lagret {len(markets)} markeder -> {markets_path}")

    print("\n=== kategorifordeling ===")
    for cat, n in markets["category"].value_counts().items():
        print(f"  {cat:<10} {n:>7}  {n/len(markets)*100:5.1f} %")

    # Tag-sensus over det som fortsatt er ukategorisert, vektet paa volum:
    # grunnlaget for aa utvide TAG_MAP med bevis i stedet for gjetning.
    rest = markets[(markets["category"] == "other") & (markets["volume"] >= 1000)]
    if len(rest):
        from collections import Counter
        c = Counter()
        for v in rest["tags"]:
            try:
                for t in (json.loads(v) if isinstance(v, str) else (v or [])):
                    t = str(t).lower()
                    if t not in NOISE_TAGS and t not in _TAG_TO_CAT:
                        c[t] += 1
            except (json.JSONDecodeError, TypeError):
                pass
        print(f"\n=== 30 vanligste ukjente tags i 'other' ({len(rest)} med volum) ===")
        for slug, n in c.most_common(30):
            print(f"  {slug:<34} {n:>6}")

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
    # Varighet i minutter — brukes til aa avgjore om minutt-fallback er meningsfullt
    sel = sel.copy()
    _end = pd.to_datetime(sel["end_date"], errors="coerce", utc=True)
    _evt = (pd.to_datetime(sel["event_start"], errors="coerce", utc=True)
            if "event_start" in sel.columns else pd.Series(pd.NaT, index=sel.index))
    _beg = _evt.fillna(pd.to_datetime(sel["start_date"], errors="coerce", utc=True))
    sel["_dur_min"] = (_end - _beg).dt.total_seconds() / 60
    # Oppgjorsvinduet alene — finnes bare naar event_start er satt
    sel["_settle_min"] = (_end - _evt).dt.total_seconds() / 60
    # NB: pandas 3 parser ISO-tekst til datetime64[us], ikke [ns]. astype("int64")
    # gir da MIKROsekunder, og // 10**9 la vinduet i januar 1970. CLOB svarte 200
    # med tom historikk — aldri en feil, bare ingenting. to_epoch regner via
    # total_seconds() og er uavhengig av opplosningen.
    sel["_evt_ts"] = to_epoch(_evt)
    sel["_end_ts"] = to_epoch(_end)
    # Mest verdifulle markeder forst, saa et avbrutt lop aldri mister det som betyr noe
    sel = sel.sort_values("volume", ascending=False, na_position="last")
    if args.category:
        sel = sel[sel["category"] == args.category]
    if args.min_volume > 0:
        dur_min = sel["_dur_min"]
        short = dur_min.notna() & (dur_min <= 300)  # <= 5 timer (5m/15m/1h/4h-markeder)
        sel = sel[(sel["volume"] >= args.min_volume)
                  | (short & (sel["volume"] >= args.short_min_volume))]
    sel = sel[sel["token_ids"] != "[]"]
    print(f"Henter prishistorikk for {len(sel)} markeder (fidelity={args.fidelity}m)")
    # Hvor stort er vindus-arbeidet? To ekstra kall per marked, saa dette avgjor
    # om det er en kort ekstrarunde eller flere fulle kjoringer.
    _z = sel[sel["_settle_min"].notna()
             & (sel["_settle_min"] <= args.zoom_max_minutes)
             & (sel["volume"] >= args.zoom_min_volume)]
    print(f"  herav {len(_z)} med oppgjorsvindu <= {args.zoom_max_minutes} min "
          f"og volum >= ${args.zoom_min_volume:,.0f} — disse faar et ekstra "
          f"finkornet kall (~{len(_z) * 2 * 0.24 / 3600:.1f} t for de som ikke "
          f"allerede staar i sjekkpunktet)", flush=True)

    on_disk = {f.stem for f in PRICES_DIR.glob("*/*.parquet")}
    nohist = load_nohistory()
    old_done = load_checkpoint()
    done = on_disk | nohist
    on_disk_new = set()
    err_skipped = 0
    consec_err = 0
    lost = len(old_done - done)
    print(f"Verifisert checkpoint: {len(on_disk)} filer paa disk, "
          f"{len(nohist)} bekreftet uten historikk"
          + (f", {lost} tidligere 'ferdige' uten fil — proves paa nytt" if lost else ""),
          flush=True)
    save_checkpoint(done)
    zoomdone = load_zoomed()
    zoom_pending = sum(1 for x in _z["market_id"].astype(str) if x not in zoomdone)
    pending = sum(1 for x in sel["market_id"].astype(str) if x not in done)
    print(f"Koe: {pending} av {len(sel)} markeder i vinduet gjenstaar aa sjekke", flush=True)
    print(f"Vindus-koe: {zoom_pending} av {len(_z)} kvalifiserte mangler finkornet vindu "
          f"(budsjett {args.zoom_budget_seconds}s for etterfylling)", flush=True)

    def stop_cleanly():
        save_checkpoint(done)
        save_nohistory(nohist)
        save_zoomed(zoomdone)
        publish(markets)
        print(f"Tidsbudsjett naadd — lagrer og avslutter pent. "
              f"({len(done)}/{len(sel)} avklart, {len(nohist)} uten historikk totalt"
              + (f", {err_skipped} utsatt pga. serverfeil" if err_skipped else "") + ")", flush=True)

    zoomed = 0
    backfilled = 0
    bad_window = 0
    zoom_tries = 0
    zoom_spent = 0.0
    for i, (_, m) in enumerate(sel.iterrows(), 1):
        if time.monotonic() - t0 > args.max_seconds:
            stop_cleanly()
            return
        mid = str(m["market_id"])
        # Kort oppgjorsvindu i et marked som har vaert apent lenge: der ligger
        # hele avgjorelsen i minuttene rundt event_start, og interval=max
        # glatter dem bort. Kostnaden er ett ekstra kall, saa bare der volumet
        # gjor det verdt det.
        #
        # VIKTIG: dette maa avgjores FOR sjekkpunkt-hoppet under. `checkpoint.json`
        # betyr «grov historikk hentet», ikke «ferdig». De mest verdifulle korte
        # markedene ble hentet lenge for vindus-kallet fantes, saa et `continue`
        # her gir null finkornede vinduer uansett hvor mange som kvalifiserer.
        settle = m.get("_settle_min")
        zoom = None
        if (args.zoom_max_minutes > 0 and pd.notna(settle)
                and 0 <= settle <= args.zoom_max_minutes
                and float(m["volume"] or 0) >= args.zoom_min_volume
                and pd.notna(m.get("_evt_ts")) and pd.notna(m.get("_end_ts"))):
            zoom = (int(m["_evt_ts"]) - args.zoom_pad_minutes * 60,
                    int(m["_end_ts"]) + 300)
            # Et vindu utenfor rimelighet gir HTTP 200 med tom historikk, ikke en
            # feil. Uten denne sjekken ser en enhetsbug ut som «ingen data finnes».
            if not (1_000_000_000 < zoom[0] < zoom[1] < 4_000_000_000):
                if bad_window == 0:
                    print(f"  !! ugyldig vindus-tidsstempel {zoom} for {mid} — "
                          f"hopper over vindus-kall", flush=True)
                bad_window += 1
                zoom = None

        if mid in done:
            if zoom is None or mid in zoomdone or zoom_spent >= args.zoom_budget_seconds:
                continue
            # Etterfylling: kun vindus-kallet. Den grove historikken finnes alt,
            # saa dette er halve arbeidet av en full refetch.
            t_zoom = time.monotonic()
            zoom_tries += 1
            zframes, zerr = [], False
            for j, tok in enumerate(json.loads(m["token_ids"])):
                zf = fetch_price_history(tok, 1, window=zoom)
                if zf is ERROR:
                    zerr = True
                elif zf is not None and len(zf):
                    zf["outcome_index"] = j
                    zf["fidelity_min"] = 1
                    zframes.append(zf)
                time.sleep(0.12)
            zoom_spent += time.monotonic() - t_zoom
            if zframes:
                out_dir = PRICES_DIR / m["category"]
                out_dir.mkdir(exist_ok=True)
                merge_price_file(out_dir / f"{mid}.parquet", zframes)
                on_disk_new.add(mid)
                nohist.discard(mid)   # vinduet fant det interval=max ikke gjorde
                backfilled += 1
            if not zerr:
                zoomdone.add(mid)     # serverfeil skal proves paa nytt neste kjoring
            if zoom_tries % 25 == 0:
                save_zoomed(zoomdone)
                save_nohistory(nohist)
                print(f"  vindus-etterfylling: {backfilled} filer utvidet av "
                      f"{zoom_tries} forsok · {zoom_spent/60:.0f} min brukt", flush=True)
            continue

        frames = []
        used_fallback = False
        had_error = False
        # Minuttdata er bare relevant for markeder som er for korte til aa gi timesbarer.
        # Aa prove det paa alle doblet antall API-kall uten aa gi ny data.
        is_short = bool(pd.notna(m["_dur_min"]) and m["_dur_min"] <= 300)
        for j, tok in enumerate(json.loads(m["token_ids"])):
            df = fetch_price_history(tok, args.fidelity)
            if df is ERROR:
                had_error = True
            elif df is None and is_short and args.fidelity > 1:
                df = fetch_price_history(tok, 1)
                if df is ERROR:
                    had_error = True
                elif df is not None:
                    used_fallback = True
            if df is not None and df is not ERROR:
                df["outcome_index"] = j
                df["fidelity_min"] = args.fidelity
                frames.append(df)
            if zoom:
                zf = fetch_price_history(tok, 1, window=zoom)
                if zf is ERROR:
                    had_error = True
                elif zf is not None and len(zf):
                    # Supplerer, erstatter ikke: dognet for oppgjor er kontekst
                    # for enhver regel om hva som skjedde i vinduet.
                    zf["outcome_index"] = j
                    zf["fidelity_min"] = 1
                    frames.append(zf)
                    zoomed += 1
                time.sleep(0.12)
            time.sleep(0.12)

        if frames:
            out_dir = PRICES_DIR / m["category"]
            out_dir.mkdir(exist_ok=True)
            if used_fallback:
                for fr in frames:
                    fr["fidelity_min"] = 1
            out = pd.concat(frames)
            # Vindus-kallet overlapper det grove; behold det fineste per tidspunkt
            out = (out.sort_values("fidelity_min")
                      .drop_duplicates(subset=["outcome_index", "timestamp"], keep="first")
                      .sort_values(["outcome_index", "timestamp"]))
            out.to_parquet(out_dir / f"{mid}.parquet", index=False)
            on_disk_new.add(mid)
            done.add(mid)
            if zoom and not had_error:
                zoomdone.add(mid)
            consec_err = 0
        elif had_error:
            # VIKTIG: ikke marker som avklart. Serverfeil != ingen historikk.
            # Markedet provers paa nytt neste kjoring, saa data ikke gaar tapt.
            err_skipped += 1
            consec_err += 1
            if consec_err >= 5:
                print("  ! API-et svarer med serverfeil — pauser 60s", flush=True)
                time.sleep(60)
                consec_err = 0
        else:
            nohist.add(mid)
            done.add(mid)
            consec_err = 0
        if i % 50 == 0:
            save_checkpoint(done)
            save_nohistory(nohist)
            save_zoomed(zoomdone)
            print(f"  {i}/{len(sel)} sjekket · {zoomed} finkornede vinduer "
                  f"· {backfilled} etterfylte · {len(on_disk_new)} nye prisfiler "
                  f"· {len(nohist)} uten historikk"
                  + (f" · {err_skipped} utsatt pga. serverfeil" if err_skipped else ""),
                  flush=True)
    save_checkpoint(done)
    save_nohistory(nohist)
    save_zoomed(zoomdone)
    publish(markets)
    print(f"Ferdig. {len(nohist)} markeder manglet CLOB-historikk. "
          f"{zoomed} nye markeder fikk finkornet vindu, {backfilled} eldre ble etterfylt "
          f"({len(zoomdone)} av {len(_z)} kvalifiserte er naa daekket)."
          + (f" {bad_window} markeder hadde ugyldig vindus-tidsstempel." if bad_window else "")
          + (f" {err_skipped} markeder ble utsatt pga. serverfeil og provers neste kjoring."
             if err_skipped else ""))


if __name__ == "__main__":
    main()
