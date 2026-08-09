#!/usr/bin/env python3
"""probe_horizon.py v2 — hvor langt tilbake kan vinduskall bygge arkivet?

v1 filtrerte paa femminuttersmarkeder og fant at de bare finnes fra 27. mai
2026. Riktig svar paa feil sporsmaal: den serien er ti uker gammel, saa det
finnes ikke noe eldre femminuttersarkiv aa hente.

Det som betyr noe er VAeR. Det er den eneste kandidaten som klarer
klyngegulvet, og den klarer det med +0,3 pp margin — den trenger flere
klynger, ikke flere handler. En klynge er by + dag, saa hvert ekstra dogn med
vaerarkiv er direkte det den mangler.

`--since-days 180` kutter alt eldre enn ca. 9. februar, begrunnet med at
«eldre historikk finnes ikke i API-et». Den paastanden ble maalt med
`interval=max`. probe_retention viste at vinduskall lever der interval=max
doer. Denne proben tester det paa vaer, og paa alle andre kategorier.

Punkter alene beviser ingenting: CLOB fyller hele det etterspurte vinduet med
ett punkt per minutt uansett. Bare ULIKE priser er bevis paa ekte handel.
"""
import json
import sys
import pandas as pd

from ingest import fetch_price_history, load_checkpoint, DATA_DIR, ERROR

LOOKBACK_H = 12       # timer for end_date vi ber om
PER_BUCKET = 5
MIN_VOLUME = 250
BUCKETS = [(150, 180), (180, 210), (210, 270), (270, 365), (365, 500), (500, 800)]
KATEGORIER = ["weather", "sports", "crypto", "politics"]


def main():
    path = DATA_DIR / "markets.parquet"
    if not path.exists():
        print(f"Fant ikke {path}."); sys.exit(1)

    mk = pd.read_parquet(path)
    now = pd.Timestamp.now(tz="UTC")
    end = pd.to_datetime(mk["end_date"], errors="coerce", utc=True)
    done = load_checkpoint()

    mk = mk.assign(
        _end=end,
        _alder=(now - end).dt.total_seconds() / 86400,
        _har_fil=mk["market_id"].astype(str).isin(done),
    )
    mk = mk[mk["_end"].notna() & (mk["_alder"] > 0)
            & (mk["volume"] >= MIN_VOLUME)
            & mk["resolved_outcome"].notna()]

    print(f"{len(mk):,} avgjorte markeder over ${MIN_VOLUME} · "
          f"{mk['_end'].min():%Y-%m-%d} → {mk['_end'].max():%Y-%m-%d}\n")

    print("=" * 92)
    print("1. HVA LIGGER UTENFOR 180-DAGERSGRENSEN\n")
    print(f"{'alder (dogn)':>14} " + "".join(f"{k:>12}" for k in KATEGORIER)
          + f"{'har fil':>10}")
    print("-" * 92)
    utsikter = {}
    for lo, hi in BUCKETS:
        sub = mk[(mk["_alder"] >= lo) & (mk["_alder"] < hi)]
        if sub.empty:
            continue
        rad = [len(sub[sub["category"] == k]) for k in KATEGORIER]
        utsikter[(lo, hi)] = dict(zip(KATEGORIER, rad))
        utsikter[(lo, hi)]["_alle"] = len(sub)
        print(f"{f'{lo}-{hi}':>14} " + "".join(f"{n:>12,}" for n in rad)
              + f"{sub['_har_fil'].sum():>10,}")

    print("\n" + "=" * 92)
    print(f"2. VIRKER VINDUSKALLET? Siste {LOOKBACK_H} t for oppgjor, "
          f"fidelity=1\n")
    print(f"{'alder':>7} {'kat':<9} {'marked':<40} {'volum':>10} "
          f"{'punkt':>7} {'priser':>7}")
    print("-" * 92)

    dom = {}
    for lo, hi in BUCKETS:
        sub = mk[(mk["_alder"] >= lo) & (mk["_alder"] < hi)]
        if sub.empty:
            continue
        # Vaer forst, saa de storste uansett kategori
        utvalg = pd.concat([
            sub[sub["category"] == "weather"].nlargest(PER_BUCKET // 2 + 1, "volume"),
            sub.nlargest(PER_BUCKET, "volume"),
        ]).drop_duplicates(subset="market_id").head(PER_BUCKET + 1)

        levende = testet = 0
        for _, m in utvalg.iterrows():
            toks = json.loads(m["token_ids"])
            if not toks:
                continue
            end_ts = int(m["_end"].timestamp())
            df = fetch_price_history(
                toks[0], 1, window=(end_ts - LOOKBACK_H * 3600, end_ts + 300))
            testet += 1
            q = str(m["question"])[:40]
            if df is ERROR or df is None or df.empty:
                print(f"{m['_alder']:>7.0f} {str(m['category']):<9} {q:<40} "
                      f"{m['volume']:>10,.0f} {'tom':>7}")
                continue
            priser = df["price"].nunique()
            levende += priser > 1
            print(f"{m['_alder']:>7.0f} {str(m['category']):<9} {q:<40} "
                  f"{m['volume']:>10,.0f} {len(df):>7} {priser:>7}")
        dom[(lo, hi)] = (levende, testet)
        print()

    print("=" * 92)
    print(f"{'alder':>12} {'ekte data':>11} {'vaer':>8} {'alle':>10}   tolkning")
    vaer_sum = alle_sum = 0
    for (lo, hi), (levende, testet) in dom.items():
        u = utsikter.get((lo, hi), {})
        if levende == 0:
            tolkning = "tomt — grensen gaar her"
        elif levende < testet:
            tolkning = "delvis dekning"
        else:
            tolkning = "full dekning"
            vaer_sum += u.get("weather", 0)
            alle_sum += u.get("_alle", 0)
        print(f"{f'{lo}-{hi}d':>12} {f'{levende}/{testet}':>11} "
              f"{u.get('weather', 0):>8,} {u.get('_alle', 0):>10,}   {tolkning}")

    if alle_sum:
        print(f"\nI baand med full dekning ligger {vaer_sum:,} vaermarkeder "
              f"og {alle_sum:,} totalt.")
        print(f"Aa hente alt tar ~{alle_sum * 2 * 0.36 / 3600:.0f} t; "
              f"bare vaer tar ~{vaer_sum * 2 * 0.36 / 3600:.1f} t "
              f"(--category weather).")
        print("Vaer alene er den billige veien: kandidaten trenger klynger, "
              "og hver ny bydag er en ny klynge.")
    else:
        print("\nIngen baand ga ekte data. 180-dagersgrensen staar som den er.")


if __name__ == "__main__":
    main()
