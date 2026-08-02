#!/usr/bin/env python3
"""
Probe 3: hvordan far vi fin opplosning i oppgjorsvinduet?

Probe 2 viste at CLOB med interval=max returnerer ~144 punkter uansett fidelity,
fordelt over hele markedets levetid. Et "5-minutters" BTC-marked er i praksis
apent i 24 timer, saa 144 punkter gir ti minutters opplosning — og de fem
minuttene som avgjor utfallet glattes bort.

Losningen er ikke hoyere fidelity, men et smalere vindu. Dette skriptet prover
parameternavnene CLOB kan tenkes aa stotte, og maaler hva hver variant gir.

Leser bare.
"""
import json
from datetime import datetime, timedelta, timezone

import requests

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


def get(url, **params):
    r = requests.get(url, params=params, timeout=40)
    r.raise_for_status()
    return r.json()


def points(**params):
    """(antall punkter, sekunder mellom punktene) eller (-kode, 0) ved feil."""
    try:
        d = get(f"{CLOB}/prices-history", **params)
    except requests.HTTPError as e:
        return -e.response.status_code, 0
    h = (d or {}).get("history") or []
    if len(h) < 2:
        return len(h), 0
    span = h[-1]["t"] - h[0]["t"]
    return len(h), span / (len(h) - 1)


def main():
    today = datetime.now(timezone.utc).date()
    target = None
    for d in range(1, 8):
        day = (today - timedelta(days=d)).isoformat()
        nxt = (today - timedelta(days=d - 1)).isoformat()
        for m in get(f"{GAMMA}/markets", closed="true", limit=500,
                     end_date_min=day, end_date_max=nxt,
                     order="volume", ascending="false"):
            q = str(m.get("question") or "")
            if "up or down" in q.lower() and m.get("eventStartTime"):
                a = datetime.fromisoformat(m["eventStartTime"].replace("Z", "+00:00"))
                b = datetime.fromisoformat(m["endDate"].replace("Z", "+00:00"))
                if 0 < (b - a).total_seconds() <= 20 * 60:
                    target = (m, a, b)
                    break
        if target:
            break

    if not target:
        print("Fant ingen passende marked. Prov flere dogn.")
        return

    m, a, b = target
    tok = m.get("clobTokenIds")
    if isinstance(tok, str):
        tok = json.loads(tok)
    token = tok[0]

    print(f"{m.get('question')}")
    print(f"  volum ${float(m.get('volume') or 0):,.0f}")
    print(f"  oppgjorsvindu {a:%Y-%m-%d %H:%M} -> {b:%H:%M} UTC "
          f"({(b - a).total_seconds() / 60:.0f} min)\n")

    ts_a, ts_b = int(a.timestamp()), int(b.timestamp())
    # Litt slakk rundt vinduet, ellers kan vi bomme paa siste handel
    lo, hi = ts_a - 900, ts_b + 300

    print(f"{'variant':<44}{'punkter':>9}{'sek mellom':>12}")
    print("-" * 65)
    trials = [
        ("interval=max, fidelity=1 (dagens)", dict(market=token, interval="max", fidelity=1)),
        ("interval=1h, fidelity=1", dict(market=token, interval="1h", fidelity=1)),
        ("interval=6h, fidelity=1", dict(market=token, interval="6h", fidelity=1)),
        ("startTs/endTs, fidelity=1", dict(market=token, startTs=lo, endTs=hi, fidelity=1)),
        ("start_ts/end_ts, fidelity=1", dict(market=token, start_ts=lo, end_ts=hi, fidelity=1)),
        ("startTs/endTs uten fidelity", dict(market=token, startTs=lo, endTs=hi)),
    ]
    for label, params in trials:
        n, sec = points(**params)
        note = f"HTTP {-n}" if n < 0 else f"{n:>9}{sec:>12.0f}"
        print(f"{label:<44}{note}")

    print("\n" + "=" * 65)
    print("LES SLIK: den varianten som gir flest punkter og faerrest sekunder")
    print("mellom dem, er den ingest.py skal bruke for korte oppgjorsvinduer.")
    print("Gir en variant HTTP 4xx, stotter ikke CLOB de parameternavnene.")
    print("=" * 65)


if __name__ == "__main__":
    main()
