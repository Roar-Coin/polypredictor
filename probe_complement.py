#!/usr/bin/env python3
"""probe_complement.py — er Up og Down eksakte komplementer i historikken?

Hvis p_up + p_down = 1 til enhver tid, kan vi hente ETT token per marked og
utlede det andre. Det halverer ~100 000 vindus-kall, altsaa en uke ned til
tre-fire dogn.

To ting maa stemme, og de er ikke det samme:

  1. PRISENE. Median og maks avvik |p_up + p_down - 1| paa felles tidsstempler.
     Boken er en ordrebok, ikke en formel — spread og tynn likviditet kan gi
     ekte avvik, saerlig i halen mot 0 og 1 der pengene ligger.
  2. DEKNINGEN. Har begge tokens de samme tidsstemplene? Har det ene flere
     punkter enn det andre, taper vi de punktene ved aa utlede.

Feiler enten, er halveringen ikke gratis, og da skal vi vite hva den koster
foer vi tar den.
"""
import json
import sys
import pandas as pd

from ingest import fetch_price_history, window_minutes, DATA_DIR, ERROR

PAD_MIN = 15
SAMPLES = 20
TOL = 0.005          # 0,5 oere — under dette er avviket uten betydning for en backtest


def main():
    path = DATA_DIR / "markets.parquet"
    if not path.exists():
        print(f"Fant ikke {path}."); sys.exit(1)

    mk = pd.read_parquet(path)
    end = pd.to_datetime(mk["end_date"], errors="coerce", utc=True)
    span = mk["question"].astype(str).map(window_minutes)

    cand = mk.assign(_end=end, _span=span)
    cand = cand[(cand["_span"] == 5) & cand["_end"].notna() & (cand["volume"] >= 250)]
    print(f"{len(cand):,} femminuttersmarkeder over $250 aa velge fra\n")

    # Bland topp-volum og tilfeldige: halen er der komplementet foerst brister.
    pick = pd.concat([cand.nlargest(SAMPLES // 2, "volume"),
                      cand.sample(min(SAMPLES // 2, len(cand)), random_state=7)])

    print(f"{'marked':<44} {'volum':>9} {'felles':>7} {'kun ett':>8} "
          f"{'median':>8} {'maks':>8} {'>tol':>6}")
    print("-" * 96)

    all_dev, tested, mismatch_total = [], 0, 0
    for _, m in pick.iterrows():
        toks = json.loads(m["token_ids"])
        if len(toks) < 2:
            continue
        end_ts = int(m["_end"].timestamp())
        window = (end_ts - (5 + PAD_MIN) * 60, end_ts + 300)

        series = []
        for tok in toks[:2]:
            df = fetch_price_history(tok, 1, window=window)
            if df is ERROR or df is None or df.empty:
                series.append(None)
            else:
                s = (df.assign(ts=pd.to_datetime(df["timestamp"], utc=True))
                       .set_index("ts")["price"].sort_index())
                series.append(s[~s.index.duplicated()])

        q = str(m["question"])[:44]
        if series[0] is None or series[1] is None:
            print(f"{q:<44} {m['volume']:>9,.0f}   ett av tokenene ga ingenting")
            tested += 1
            continue

        up, down = series
        both = up.index.intersection(down.index)
        only_one = len(up.index.symmetric_difference(down.index))
        mismatch_total += only_one
        dev = (up.loc[both] + down.loc[both] - 1.0).abs()
        all_dev.append(dev)
        tested += 1

        over = (dev > TOL).sum()
        print(f"{q:<44} {m['volume']:>9,.0f} {len(both):>7} {only_one:>8} "
              f"{dev.median():>8.4f} {dev.max():>8.4f} {over:>6}")

    if not all_dev:
        print("\nIngen markeder ga data fra begge tokens.")
        return

    d = pd.concat(all_dev)
    print("\n" + "=" * 96)
    print(f"{tested} markeder · {len(d):,} felles tidsstempler · "
          f"{mismatch_total} tidsstempler fantes bare i det ene tokenet")
    print(f"|p_up + p_down - 1|:  median {d.median():.4f} · "
          f"95-persentil {d.quantile(.95):.4f} · maks {d.max():.4f}")
    print(f"Punkter over {TOL}: {(d > TOL).sum():,} ({(d > TOL).mean():.2%})")

    if d.quantile(.95) <= TOL and mismatch_total == 0:
        print("\nGROENT LYS: hent ett token, utled det andre. Halverer kalltallet.")
    elif d.quantile(.95) <= TOL:
        print(f"\nPRISENE stemmer, men {mismatch_total} tidsstempler finnes bare i "
              "det ene tokenet. Utledning fyller de hullene med et anslag i stedet "
              "for maalte data — greit for en terskelregel, ikke for spread-analyse.")
    else:
        print("\nROEDT LYS: avviket er for stort. Utledning ville legge en systematisk "
              "feil inn i inngangsprisene, og det er nettopp inngangsprisen hele "
              "fordelsmaalingen henger paa. Behold begge kallene.")


if __name__ == "__main__":
    main()
