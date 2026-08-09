#!/usr/bin/env python3
"""probe_horizon.py — hvor langt tilbake kan vinduskall bygge arkivet?

`--since-days 180` kutter alt som ble avsluttet for ca. 9. februar, og loggen
skriver «eldre historikk finnes ikke i API-et». Den paastanden ble maalt med
`interval=max` — nettopp kallet probe_retention viste at doer med alder mens
vinduskallet ikke gjor det. Up-or-Down-familien gaar tilbake til mai 2025.

Proben svarer paa to ting:

  1. VIRKER DET? Samme vinduskall mot femminuttersmarkeder i aldersbaand fra
     180 til 500 dogn. probe_retention testet bare til 62 dogn.
  2. ER DET VERDT DET? Hvor mange markeder ligger i hvert baand over
     volumgulvet, og hvor lang tid ville hentingen ta.

Et vinduskall som gir punkter MED prisvariasjon er ekte data. Gir det punkter
uten variasjon, er serien baaret fremover fra ingenting og verdilos. Derfor
telles bade punkter og antall ULIKE priser.

Kjores med probe.yml, script=probe_horizon.
"""
import json
import sys
import pandas as pd

from ingest import (fetch_price_history, window_minutes, load_checkpoint,
                    DATA_DIR, ERROR)

PAD_MIN = 15
PER_BUCKET = 6
MIN_VOLUME = 250
BUCKETS = [(150, 180), (180, 210), (210, 240), (240, 300),
           (300, 360), (360, 420), (420, 500)]


def main():
    path = DATA_DIR / "markets.parquet"
    if not path.exists():
        print(f"Fant ikke {path}."); sys.exit(1)

    mk = pd.read_parquet(path)
    now = pd.Timestamp.now(tz="UTC")
    end = pd.to_datetime(mk["end_date"], errors="coerce", utc=True)
    span = mk["question"].astype(str).map(window_minutes)

    done = load_checkpoint()
    ud = mk.assign(
        _end=end,
        _span=span,
        _alder=(now - end).dt.total_seconds() / 86400,
    )
    ud = ud[(ud["_span"] == 5) & ud["_end"].notna()]
    ud = ud.assign(_har_fil=ud["market_id"].astype(str).isin(done))

    print(f"{len(ud):,} femminuttersmarkeder i metadata, "
          f"{ud['_end'].min():%Y-%m-%d} → {ud['_end'].max():%Y-%m-%d}\n")

    print("=" * 88)
    print("1. HVA LIGGER DER — markeder per aldersbaand\n")
    print(f"{'alder (dogn)':>14} {'markeder':>10} {'over $250':>11} "
          f"{'har fil':>9} {'median volum':>13}")
    print("-" * 88)
    utsikter = {}
    for lo, hi in BUCKETS:
        sub = ud[(ud["_alder"] >= lo) & (ud["_alder"] < hi)]
        if sub.empty:
            continue
        over = sub[sub["volume"] >= MIN_VOLUME]
        utsikter[(lo, hi)] = len(over)
        print(f"{f'{lo}-{hi}':>14} {len(sub):>10,} {len(over):>11,} "
              f"{sub['_har_fil'].sum():>9,} ${sub['volume'].median():>12,.0f}")

    print("\n" + "=" * 88)
    print("2. VIRKER VINDUSKALLET SAA LANGT TILBAKE?\n")
    print(f"{'alder':>7} {'marked':<44} {'volum':>9} {'punkter':>8} {'priser':>7}")
    print("-" * 88)

    dom = {}
    for lo, hi in BUCKETS:
        sub = ud[(ud["_alder"] >= lo) & (ud["_alder"] < hi)
                 & (ud["volume"] >= MIN_VOLUME)]
        if sub.empty:
            continue
        sub = sub.nlargest(PER_BUCKET, "volume")
        levende = 0
        for _, m in sub.iterrows():
            toks = json.loads(m["token_ids"])
            if not toks:
                continue
            end_ts = int(m["_end"].timestamp())
            df = fetch_price_history(
                toks[0], 1, window=(end_ts - (5 + PAD_MIN) * 60, end_ts + 300))
            q = str(m["question"])[:44]
            if df is ERROR or df is None or df.empty:
                print(f"{m['_alder']:>7.0f} {q:<44} {m['volume']:>9,.0f} "
                      f"{'tom':>8}")
                continue
            priser = df["price"].nunique()
            # Ett punkt per minutt uten variasjon er en baaret serie, ikke data.
            if priser > 1:
                levende += 1
            print(f"{m['_alder']:>7.0f} {q:<44} {m['volume']:>9,.0f} "
                  f"{len(df):>8} {priser:>7}")
        dom[(lo, hi)] = (levende, len(sub))
        print()

    print("=" * 88)
    print(f"{'alder':>12} {'ekte data':>11} {'markeder over $250':>20}   tolkning")
    total_verdt = 0
    for (lo, hi), (levende, testet) in dom.items():
        n = utsikter.get((lo, hi), 0)
        if levende == 0:
            tolkning = "tomt — grensen gaar her"
        elif levende < testet:
            tolkning = "delvis — ujevn dekning"
        else:
            tolkning = "full dekning"
            total_verdt += n
        print(f"{f'{lo}-{hi}d':>12} {f'{levende}/{testet}':>11} {n:>20,}   {tolkning}")

    if total_verdt:
        timer = total_verdt * 2 * 0.36 / 3600
        print(f"\n{total_verdt:,} markeder ligger i baand med full dekning. "
              f"Aa hente dem tar ~{timer:.1f} t, altsaa ~{timer/3:.0f} kjoringer "
              f"med 3-timers vindusbudsjett.")
        print("Sett --since-days deretter. Husk at det ogsaa utvider den "
              "generelle koen, ikke bare vindus-koen.")
    else:
        print("\nIngen baand ga ekte data. 180-dagersgrensen staar.")


if __name__ == "__main__":
    main()
