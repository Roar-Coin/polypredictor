#!/usr/bin/env python3
"""
Probe 2: hvor finner vi den EKTE handelsperioden for 5-/15-minutters markeder?

Probe 1 viste at startDate er opprettelsestidspunktet — et "Up or Down"-marked
med et femminutters vindu faar beregnet varighet paa ~1435 minutter. Baade
appens varighetsfilter og is_short-testen i ingest.py bommer derfor.

Tre mulige kilder til riktig vindu, i synkende rekkefolge etter hvor robuste
de er:
  1) et metadatafelt (gameStartTime / eventStartTime / startDateIso / ...)
  2) prishistorikken selv — forste og siste tidsstempel ved fidelity=1
  3) tittelen ("7:50PM-7:55PM ET") — virker, men brekker naar de endrer format

Skriptet dumper alle datofelter for noen faktiske korte markeder og henter
minutthistorikken, saa vi kan se hvilken kilde som stemmer.

Leser bare.
"""
import json
import re
from datetime import datetime, timedelta, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# "7:50PM-7:55PM", "4:00PM-8:00PM"
WINDOW = re.compile(r"(\d{1,2}:\d{2}\s*[AP]M)\s*[-–]\s*(\d{1,2}:\d{2}\s*[AP]M)", re.I)


def get(url, **params):
    r = requests.get(url, params=params, timeout=40)
    r.raise_for_status()
    return r.json()


def tokens(m):
    t = m.get("clobTokenIds")
    if isinstance(t, str):
        try:
            t = json.loads(t)
        except json.JSONDecodeError:
            t = []
    return t or []


def main():
    today = datetime.now(timezone.utc).date()
    rows = []
    for d in range(1, 4):
        day = (today - timedelta(days=d)).isoformat()
        nxt = (today - timedelta(days=d - 1)).isoformat()
        for off in (0, 500, 1000, 1500, 2000):
            try:
                b = get(f"{GAMMA}/markets", closed="true", limit=500, offset=off,
                        include_tag="true", end_date_min=day, end_date_max=nxt)
            except requests.HTTPError:
                break
            if not b:
                break
            rows += b

    # Bare markeder der tittelen oppgir et vindu paa <= 20 minutter
    short = []
    for m in rows:
        q = str(m.get("question") or "")
        w = WINDOW.search(q)
        if not w:
            continue
        try:
            a = datetime.strptime(w.group(1).replace(" ", "").upper(), "%I:%M%p")
            b_ = datetime.strptime(w.group(2).replace(" ", "").upper(), "%I:%M%p")
        except ValueError:
            continue
        mins = (b_ - a).total_seconds() / 60
        if 0 < mins <= 20:
            m["_title_mins"] = mins
            short.append(m)

    print(f"{len(rows)} markeder hentet · {len(short)} med kort vindu i tittelen\n")
    if not short:
        print("Fant ingen. Prov flere dogn.")
        return

    short.sort(key=lambda x: -float(x.get("volume") or 0))
    for m in short[:4]:
        print("=" * 70)
        print(f"{m.get('question')}")
        print(f"  volum ${float(m.get('volume') or 0):,.0f} · vindu iflg. tittel: {m['_title_mins']:.0f} min")
        print("  --- alle datofelter ---")
        for k, v in sorted(m.items()):
            if v and ("ate" in k or "ime" in k) and not isinstance(v, (dict, list)):
                print(f"    {k:<22} {v}")

        tk = tokens(m)
        if not tk:
            print("  (ingen clobTokenIds)")
            continue
        try:
            d = get(f"{CLOB}/prices-history", market=tk[0], interval="max", fidelity=1)
        except requests.HTTPError as e:
            print(f"  CLOB feilet: {e}")
            continue
        h = (d or {}).get("history") or []
        print(f"  --- prishistorikk ved fidelity=1: {len(h)} punkter ---")
        if h:
            t0 = datetime.fromtimestamp(h[0]["t"], timezone.utc)
            t1 = datetime.fromtimestamp(h[-1]["t"], timezone.utc)
            span = (t1 - t0).total_seconds() / 60
            print(f"    forste  {t0:%Y-%m-%d %H:%M} UTC  p={h[0]['p']}")
            print(f"    siste   {t1:%Y-%m-%d %H:%M} UTC  p={h[-1]['p']}")
            print(f"    spenn   {span:.0f} min   <-- den EKTE handelsperioden")

    print("\n" + "=" * 70)
    print("LES SLIK: stemmer et metadatafelt med 'spenn', bruk det feltet.")
    print("Gjor ingen det, maa varigheten utledes fra prishistorikken selv.")
    print("=" * 70)


if __name__ == "__main__":
    main()
