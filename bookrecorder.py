#!/usr/bin/env python3
"""bookrecorder.py — tar opp ordreboken for aktive Polymarket-markeder.

HVORFOR DETTE FINNES

Prishistorikken er ikke en vollgrav: probe_horizon viste at vinduskall gir
minuttdata 643 dogn tilbake, saa hvem som helst kan hente den paa to uker.
Ordreboken kan de ikke. Polymarket eksponerer ingen historisk bokdybde, og
det som ikke tas opp naa er borte for alltid.

Det er ogsaa det eneste som kan gjore backtestene aerlige. Vaerregelen saa ut
som +1,7 pp fordi backtesten antok 0,5¢ kostnad. Malt kostnad var 2,18¢, og
da var fordelen borte. Med boken lagret kan kostnaden regnes ut av dataene i
stedet for aa antas.

HVA SOM LAGRES

To niaaer, fordi full dybde for alt er for mye aa lagre:

  quotes  — hvert opptak, hvert token: beste bud/tilbud, spread, midtpunkt,
            dybde i USD, og EFFEKTIV KJOPSPRIS for tre innsatsstorrelser,
            regnet ved aa ga gjennom ask-siden niva for niva. Det siste er
            selve kostnadsmaalet, ferdig regnet, saa analysen senere ikke
            trenger raa dybde i det hele tatt.
  levels  — full bid/ask-dybde, men bare for markeder i den siste timen for
            oppgjor. Det er der reglene handler og der realisme betyr noe.

SPREAD-KORREKTHET

Faellene, og hva som gjores med dem:
  - Sorteringen fra API-et antas ikke. Bud sorteres synkende, tilbud stigende.
  - Tom side gir IKKE spread 0. Da er spread udefinert og lagres som null.
  - Kryssende bok (beste bud >= beste tilbud) forekommer og flagges i stedet
    for aa gi negativ spread.
  - Begge tokens tas opp. Prisene er komplementaere, men DYBDEN er det ikke —
    NO-siden kan vaere tynn der YES er tykk.
  - Ufyllbar innsats lagres som null, ikke som siste niva. Aa fylle resten til
    beste pris ville skjult nettopp det problemet vi maaler.
"""
import argparse
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA_DIR = Path("data")
BOOKS_DIR = DATA_DIR / "books"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "hindsight-bookrecorder/1"})


def get_json(url, params=None, attempts=3, timeout=20):
    for i in range(attempts):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(1.5 * (i + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(1.5 * (i + 1))
    return None


# ---------------------------------------------------------------- utvalg

def categorize(market):
    """Samme kategorier som ingest.py, forenklet til tag-oppslag."""
    tags = [t.get("label", "").lower() for t in (market.get("tags") or [])
            if isinstance(t, dict)]
    tekst = " ".join(tags)
    for nokkel, kat in (
        ("weather", "weather"), ("temperature", "weather"), ("hurricane", "weather"),
        ("crypto", "crypto"), ("bitcoin", "crypto"), ("ethereum", "crypto"),
        ("esports", "esports"), ("counter-strike", "esports"), ("dota", "esports"),
        ("sports", "sports"), ("nfl", "sports"), ("nba", "sports"), ("soccer", "sports"),
        ("politics", "politics"), ("election", "politics"),
        ("economy", "economy"), ("fed", "economy"), ("inflation", "economy"),
        ("stocks", "stocks"), ("earnings", "stocks"),
        ("culture", "culture"), ("pop-culture", "culture"),
    ):
        if nokkel in tekst:
            return kat
    return "other"


def velg_markeder(min_volume, horizon_hours, max_markets):
    """Aapne markeder i alle kategorier som gjores opp innen horisonten.

    Sorteres paa naerhet til oppgjor, ikke paa volum: det er minuttene for
    oppgjor som er uerstattelige, og et marked som gjores opp om ti minutter
    faar aldri en ny sjanse."""
    naa = datetime.now(timezone.utc)
    grense = naa + timedelta(hours=horizon_hours)
    ut, offset = [], 0
    while offset < 20000:
        batch = get_json(f"{GAMMA}/markets", {
            "closed": "false", "limit": 500, "offset": offset,
            "end_date_min": naa.isoformat().replace("+00:00", "Z"),
            "end_date_max": grense.isoformat().replace("+00:00", "Z"),
            "include_tag": "true",
        })
        if not batch:
            break
        for m in batch:
            try:
                toks = json.loads(m.get("clobTokenIds") or "[]")
            except json.JSONDecodeError:
                toks = []
            if len(toks) < 2:
                continue
            vol = float(m.get("volumeNum") or m.get("volume") or 0)
            if vol < min_volume:
                continue
            slutt = pd.to_datetime(m.get("endDate"), errors="coerce", utc=True)
            if pd.isna(slutt):
                continue
            ut.append({"market_id": str(m.get("id")), "question": m.get("question"),
                       "category": categorize(m), "tokens": toks[:2],
                       "end_ts": int(slutt.timestamp()), "volume": vol})
        if len(batch) < 500:
            break
        offset += 500
    ut.sort(key=lambda r: r["end_ts"])
    return ut[:max_markets]


# ---------------------------------------------------------------- boken

def rene_nivaaer(raa, stigende):
    """Sorter og rens ett bok-side. API-ets rekkefolge antas ikke."""
    ut = []
    for niv in raa or []:
        try:
            p, s = float(niv["price"]), float(niv["size"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 < p < 1 and s > 0:
            ut.append((p, s))
    ut.sort(key=lambda x: x[0], reverse=not stigende)
    return ut


def gaa_gjennom(asks, usd):
    """Effektiv kjopspris for USD ved aa spise ask-siden niva for niva.

    None naar boken ikke rekker — det er et funn, ikke en feil, og maa ikke
    fylles opp til beste pris."""
    brukt = andeler = 0.0
    for pris, storrelse in asks:
        tilgjengelig = pris * storrelse
        ta = min(usd - brukt, tilgjengelig)
        if ta <= 0:
            break
        andeler += ta / pris
        brukt += ta
    if brukt < usd - 1e-9 or andeler <= 0:
        return None
    return brukt / andeler


def ta_opp(marked, stakes, med_dybde):
    """Ett opptak av begge tokens i ett marked."""
    ts = int(time.time())
    quotes, levels = [], []
    for j, tok in enumerate(marked["tokens"]):
        bok = get_json(f"{CLOB}/book", {"token_id": tok})
        if not bok:
            continue
        bids = rene_nivaaer(bok.get("bids"), stigende=False)
        asks = rene_nivaaer(bok.get("asks"), stigende=True)
        beste_bud = bids[0][0] if bids else None
        beste_tilbud = asks[0][0] if asks else None

        # Tom side gir udefinert spread, ikke null. Kryssende bok flagges.
        spread = mid = None
        kryssende = False
        if beste_bud is not None and beste_tilbud is not None:
            spread = beste_tilbud - beste_bud
            mid = (beste_bud + beste_tilbud) / 2
            if spread < 0:
                kryssende = True

        rad = {
            "ts": ts, "market_id": marked["market_id"], "token_id": tok,
            "outcome_index": j, "category": marked["category"],
            "end_ts": marked["end_ts"], "mins_to_end": (marked["end_ts"] - ts) / 60.0,
            "volume": marked["volume"],
            "best_bid": beste_bud, "best_ask": beste_tilbud,
            "mid": mid, "spread": spread, "crossed": kryssende,
            "bid_levels": len(bids), "ask_levels": len(asks),
            "bid_depth_usd": sum(p * s for p, s in bids),
            "ask_depth_usd": sum(p * s for p, s in asks),
        }
        for usd in stakes:
            fyll = gaa_gjennom(asks, usd)
            rad[f"fill_{int(usd)}"] = fyll
            # Kostnaden backtesten trenger: hva fyllet koster over midtpunkt.
            rad[f"cost_{int(usd)}"] = (fyll - mid) if (fyll and mid) else None
        quotes.append(rad)

        if med_dybde:
            for side, nivaaer in (("bid", bids), ("ask", asks)):
                for k, (p, s) in enumerate(nivaaer):
                    levels.append({"ts": ts, "token_id": tok, "side": side,
                                   "level": k, "price": p, "size": s})
    return quotes, levels


# ---------------------------------------------------------------- lagring

def skriv(rader, mappe, navn):
    """Atomisk: skriv til .tmp, bytt navn. En drept jobb skal ikke etterlate
    en halv parquet-fil — den leksa tok vi paa zoomed.json."""
    if not rader:
        return None
    mappe.mkdir(parents=True, exist_ok=True)
    maal = mappe / navn
    tmp = maal.with_suffix(".tmp")
    pd.DataFrame(rader).to_parquet(tmp, index=False)
    os.replace(tmp, maal)
    return maal


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-volume", type=float, default=5000)
    ap.add_argument("--horizon-hours", type=int, default=24,
                    help="Ta opp markeder som gjores opp innen saa mange timer.")
    ap.add_argument("--interval-seconds", type=int, default=60)
    ap.add_argument("--max-markets", type=int, default=300)
    ap.add_argument("--depth-within-minutes", type=int, default=60,
                    help="Full dybde lagres bare naar det er mindre enn saa "
                         "mange minutter igjen til oppgjor.")
    ap.add_argument("--stakes", default="25,100,500")
    ap.add_argument("--run-seconds", type=int, default=17400,
                    help="Avslutt pent etter dette (default 4t50m).")
    ap.add_argument("--reselect-minutes", type=int, default=30,
                    help="Hvor ofte utvalget hentes paa nytt — nye markeder "
                         "aapner hele tiden.")
    args = ap.parse_args()

    stakes = [float(x) for x in args.stakes.split(",") if x.strip()]
    t0 = time.monotonic()
    runde = 0
    markeder, sist_valgt = [], -1e9
    tot_quotes = tot_levels = 0

    print(f"Opptaker startet. Horisont {args.horizon_hours} t, "
          f"volum >= ${args.min_volume:,.0f}, {args.interval_seconds}s mellom "
          f"opptak, innsatser {stakes}", flush=True)

    while time.monotonic() - t0 < args.run_seconds:
        syklus = time.monotonic()

        if syklus - sist_valgt > args.reselect_minutes * 60:
            markeder = velg_markeder(args.min_volume, args.horizon_hours,
                                     args.max_markets)
            sist_valgt = syklus
            fordeling = pd.Series([m["category"] for m in markeder]).value_counts()
            print(f"Utvalg: {len(markeder)} markeder · "
                  + " ".join(f"{k}={v}" for k, v in fordeling.items()), flush=True)

        naa = int(time.time())
        quotes, levels = [], []
        for m in markeder:
            if m["end_ts"] < naa - 300:
                continue          # gjort opp, boken er tom
            med_dybde = (m["end_ts"] - naa) / 60.0 <= args.depth_within_minutes
            q, l = ta_opp(m, stakes, med_dybde)
            quotes.extend(q)
            levels.extend(l)

        stempel = datetime.now(timezone.utc).strftime("%Y-%m-%d/%H%M%S")
        dag, klokke = stempel.split("/")
        skriv(quotes, BOOKS_DIR / dag, f"quotes-{klokke}.parquet")
        skriv(levels, BOOKS_DIR / dag, f"levels-{klokke}.parquet")
        tot_quotes += len(quotes)
        tot_levels += len(levels)
        runde += 1

        if runde % 10 == 0:
            med_spread = [q["spread"] for q in quotes if q["spread"] is not None]
            median = pd.Series(med_spread).median() if med_spread else float("nan")
            ufyllbare = sum(1 for q in quotes if q.get("fill_100") is None)
            print(f"  runde {runde} · {tot_quotes:,} quotes · {tot_levels:,} nivaaer "
                  f"· median spread {median*100:.2f}¢ "
                  f"· {ufyllbare}/{len(quotes)} ufyllbare paa $100", flush=True)

        brukt = time.monotonic() - syklus
        time.sleep(max(0, args.interval_seconds - brukt))

    print(f"Ferdig. {runde} runder, {tot_quotes:,} quotes, {tot_levels:,} nivaaer.",
          flush=True)


if __name__ == "__main__":
    main()
