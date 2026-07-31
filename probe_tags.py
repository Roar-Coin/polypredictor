#!/usr/bin/env python3
"""
Probe: hvordan faar vi tags ut av Gamma?

Kategoriseringen er doed fordi /markets ikke returnerer tags slik vi kaller det.
Dette skriptet tester fire veier og sier hvilken som virker. Forskjellen i
arbeidsmengde er stor:

  1) /markets med include_tag=true  -> en linje aa endre. Best.
  2) tags ligger nestet i m["events"][0]["tags"]  -> en linje i tag_labels.
  3) maa hente via /events           -> hele fetch_all_markets maa skrives om.
  4) ingen av delene               -> fall tilbake paa utvidet nokkelordsliste.

Leser bare. Endrer ingenting.
"""
import json

import requests

GAMMA = "https://gamma-api.polymarket.com"


def get(path, **params):
    r = requests.get(f"{GAMMA}{path}", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def tags_of(obj):
    """Samme uttrekk som ingest.tag_labels, men tolerant for nestede former."""
    out = []
    for t in (obj.get("tags") or []):
        label = (t.get("slug") or t.get("label")) if isinstance(t, dict) else str(t)
        if label:
            out.append(str(label).lower())
    return out


def show(title, markets):
    print(f"\n--- {title} ---")
    if not markets:
        print("  tomt svar")
        return 0
    hits = 0
    for m in markets[:3]:
        direct = tags_of(m)
        nested = []
        for ev in (m.get("events") or []):
            nested += tags_of(ev)
        if direct or nested:
            hits += 1
        print(f"  {str(m.get('question'))[:64]}")
        print(f"    tags paa market : {direct or '(ingen)'}")
        print(f"    tags paa event  : {nested or '(ingen)'}")
    return hits


def main():
    # Lukkede markeder — det er dem arkivet bestaar av.
    base = dict(limit=5, closed="true", order="volume", ascending="false")

    print("=== 1) /markets slik ingest.py kaller det i dag ===")
    plain = get("/markets", **base)
    h1 = show("uten ekstra parametre", plain)

    if plain:
        print("\n  nokler paa market-objektet:")
        print("   ", ", ".join(sorted(plain[0].keys())))

    print("\n=== 2) /markets?include_tag=true ===")
    try:
        h2 = show("include_tag=true", get("/markets", include_tag="true", **base))
    except requests.HTTPError as e:
        print(f"  avvist: {e}")
        h2 = 0

    print("\n=== 3) /events (tags hoerer trolig hjemme her) ===")
    evs = get("/events", **base)
    h3 = 0
    for ev in evs[:3]:
        et = tags_of(ev)
        if et:
            h3 += 1
        mk = ev.get("markets") or []
        print(f"  event: {str(ev.get('title'))[:60]}")
        print(f"    tags          : {et or '(ingen)'}")
        print(f"    antall markeder: {len(mk)}")
        if mk:
            print(f"    forste market : {str(mk[0].get('question'))[:60]}")
            print(f"    market_id     : {mk[0].get('id')}")

    print("\n=== 4) /tags — hvilke sluger finnes i det hele tatt? ===")
    try:
        tg = get("/tags", limit=60)
        print(f"  {len(tg)} tags hentet. Forste 40 sluger:")
        print("   ", ", ".join(sorted(str(t.get("slug")) for t in tg)[:40]))
    except requests.HTTPError as e:
        print(f"  avvist: {e}")

    print("\n" + "=" * 58)
    print("KONKLUSJON")
    if h2:
        print("  -> include_tag=true virker. Legg parameteren inn i fetch_window.")
    elif h1:
        print("  -> tags finnes allerede; feilen ligger i uttrekket, ikke i kallet.")
    elif h3:
        print("  -> tags ligger paa event. Hent metadata via /events i stedet,")
        print("     eller berik markeder med event-tags i et eget steg.")
    else:
        print("  -> ingen av veiene ga tags. Utvid noekkelordslista i stedet.")
    print("=" * 58)

    # Raadump av ett market, for oeynene dine — feltnavn endrer seg over tid.
    if plain:
        print("\n=== raadump av ett market (forkortet) ===")
        m = dict(plain[0])
        for k in ("description", "outcomePrices", "clobTokenIds", "outcomes"):
            m.pop(k, None)
        print(json.dumps(m, indent=2)[:2500])


if __name__ == "__main__":
    main()
