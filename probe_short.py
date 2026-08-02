#!/usr/bin/env python3
"""
Probe: hvorfor finnes ingen 5-/15-minutters kryptomarkeder i arkivet?

Symptom: minste tidsavstand i hele datasettet er 59-60 minutter. Ingen handel
ligger naermere oppgjor enn en time, og appens Duration-filter for "~5 min" og
"~15 min" gir null treff.

Fire mulige aarsaker, og de krever helt ulike fikser:
  A) Markedene finnes ikke i Gamma i det hele tatt.       -> ingenting aa gjore.
  B) De finnes, men volumet ligger under gulvet paa $200. -> senk gulvet.
  C) De finnes og hentes, men fidelity=60 gir 0-1 punkt,  -> minutt-fallback maa
     og fallbacken til fidelity=1 utloses aldri fordi        utloses paa faa punkter,
     CLOB returnerer ett punkt i stedet for ingenting.       ikke bare paa tomt svar.
  D) De finnes, men startDate er opprettelsestidspunktet,  -> appens varighet maa
     saa varigheten regnes feil og filteret bommer.           regnes fra noe annet.

Leser bare. Endrer ingenting.
"""
import json
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def get(url, **params):
    r = requests.get(url, params=params, timeout=40)
    r.raise_for_status()
    return r.json()


def iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def tokens(m):
    t = m.get("clobTokenIds")
    if isinstance(t, str):
        try:
            t = json.loads(t)
        except json.JSONDecodeError:
            t = []
    return t or []


def history(token, fidelity):
    """Antall punkter CLOB gir. -1 = kallet feilet."""
    try:
        d = get(f"{CLOB}/prices-history", market=token, interval="max", fidelity=fidelity)
    except requests.HTTPError:
        return -1
    return len(((d or {}).get("history")) or [])


def main():
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    print(f"Henter lukkede kryptomarkeder avsluttet etter {since} ...\n")

    rows, offset = [], 0
    while offset < 6000:
        batch = get(f"{GAMMA}/markets", closed="true", limit=500, offset=offset,
                    include_tag="true", end_date_min=since,
                    order="endDate", ascending="false")
        if not batch:
            break
        rows += batch
        offset += 500
    print(f"{len(rows)} markeder hentet\n")

    # Varighet fra startDate/endDate — det appen bruker.
    short, buckets = [], Counter()
    for m in rows:
        a, b = iso(m.get("startDate")), iso(m.get("endDate"))
        if not (a and b):
            continue
        mins = (b - a).total_seconds() / 60
        if mins <= 0:
            continue
        m["_mins"] = mins
        buckets["<=7 min" if mins <= 7 else
                "8-20 min" if mins <= 20 else
                "21-90 min" if mins <= 90 else
                "91-300 min" if mins <= 300 else "> 5 t"] += 1
        if mins <= 20:
            short.append(m)

    print("=== varighet regnet som endDate - startDate (appens definisjon) ===")
    for k in ["<=7 min", "8-20 min", "21-90 min", "91-300 min", "> 5 t"]:
        print(f"  {k:<12} {buckets[k]:>6}")

    # Titler som ROPER kortlivet, uansett hva datoene sier.
    pat = [t for t in rows
           if "up or down" in str(t.get("question", "")).lower()
           or "-4:05" in str(t.get("question", ""))
           or ":05pm" in str(t.get("question", "")).lower()]
    print(f"\n=== {len(pat)} markeder med 'Up or Down'-tittel ===")
    for m in pat[:8]:
        a, b = iso(m.get("startDate")), iso(m.get("endDate"))
        d = (b - a).total_seconds() / 60 if a and b else float("nan")
        print(f"  {str(m.get('question'))[:58]:<60} varighet {d:8.1f} min  vol ${float(m.get('volume') or 0):>10,.0f}")

    if not short:
        print("\n=== INGEN markeder under 20 minutters beregnet varighet ===")
        print("  --> D er sannsynlig: startDate er opprettelsestidspunkt, ikke handelsstart.")
        print("      Sjekker om gameStartTime/eventStartTime gir riktigere varighet ...")
        alt = [m for m in rows if m.get("gameStartTime") or m.get("eventStartTime")]
        print(f"      {len(alt)} av {len(rows)} har et slikt felt.")
        for m in alt[:5]:
            g = iso(m.get("gameStartTime") or m.get("eventStartTime"))
            b = iso(m.get("endDate"))
            d = (b - g).total_seconds() / 60 if g and b else float("nan")
            print(f"        {str(m.get('question'))[:50]:<52} {d:8.1f} min fra start til slutt")
        return

    vols = [float(m.get("volume") or 0) for m in short]
    print(f"\n=== {len(short)} markeder <= 20 min ===")
    print(f"  volum: median ${statistics.median(vols):,.0f} · "
          f"min ${min(vols):,.0f} · maks ${max(vols):,.0f}")
    print(f"  under $200-gulvet: {sum(1 for v in vols if v < 200)} av {len(short)}")

    print("\n=== gir CLOB historikk for disse? (fidelity 60 mot 1) ===")
    tested = 0
    for m in sorted(short, key=lambda x: -float(x.get("volume") or 0)):
        tk = tokens(m)
        if not tk:
            continue
        h60, h1 = history(tk[0], 60), history(tk[0], 1)
        print(f"  vol ${float(m.get('volume') or 0):>8,.0f}  {m['_mins']:5.1f} min  "
              f"fidelity60={h60:>4}  fidelity1={h1:>4}   {str(m.get('question'))[:42]}")
        tested += 1
        if tested >= 10:
            break

    print("\n" + "=" * 60)
    print("LES SLIK: gir fidelity1 flere punkter enn fidelity60, er C bekreftet —")
    print("fallbacken maa utloses paa FAA punkter, ikke bare paa tomt svar.")
    print("Er begge 0, har CLOB ingen historikk for saa korte markeder (A).")
    print("=" * 60)


if __name__ == "__main__":
    main()
