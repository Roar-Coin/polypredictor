#!/usr/bin/env python3
"""probe_updown.py — hvor lang er handelsperioden i et Up-or-Down-marked?

Forrige probe viste at de 191 727 Up-or-Down-markedene mangler event_start
helt. Dermed er _settle_min NaN og _dur_min 1437 min (startDate = opprettelse),
saa de kvalifiserer verken til vindus-kallet eller til minutt-fallbacket. De
faar timesbarer, og for et femminuttersmarked er det ett punkt.

Vi kan ikke lese vinduet ut av metadata. Men vi kan maale det: hent minuttdata
for de siste timene for end_date og se naar handelen faktisk begynner. Serien
forteller selv hvor lang perioden er.

Proben svarer paa tre ting:
  1. Hvor mange av de 191 727 er intradag, og hvordan kjenner vi dem igjen?
     (klokkeslettet for end_date: femminutters-markeder avsluttes paa 5-minutters-
     merker spredt over hele dognet, dognmarkeder paa ett fast tidspunkt)
  2. Naar starter handelen i forhold til end_date — 5 min for? 60? 1440?
  3. Har de allerede prisfil (lest fra checkpoint.json, ikke fra disk)

Kjores etter at data/markets.parquet og data/checkpoint.json er hentet fra R2.
"""
import json
import re
import sys
import pandas as pd

from ingest import fetch_price_history, DATA_DIR, CHECKPOINT, ERROR

UP_DOWN = re.compile(r"up or down", re.I)
LOOKBACK_MIN = 180   # hvor langt for end_date vi ser etter forste handel
SAMPLES = 12


def main():
    path = DATA_DIR / "markets.parquet"
    if not path.exists():
        print(f"Fant ikke {path}.")
        sys.exit(1)

    mk = pd.read_parquet(path)
    end = pd.to_datetime(mk["end_date"], errors="coerce", utc=True)
    evt = (pd.to_datetime(mk["event_start"], errors="coerce", utc=True)
           if "event_start" in mk.columns else pd.Series(pd.NaT, index=mk.index))

    done = set(json.loads(CHECKPOINT.read_text())) if CHECKPOINT.exists() else set()
    if not done:
        print("checkpoint.json mangler — 'har prisfil' blir uten verdi.\n")

    ud = mk[mk["question"].astype(str).str.contains(UP_DOWN)].assign(
        _end=end, _evt=evt)
    ud = ud[ud["_end"].notna()]
    ud = ud.assign(
        _has_file=ud["market_id"].astype(str).isin(done),
        _min_of_hour=ud["_end"].dt.minute,
        _hour=ud["_end"].dt.hour,
    )

    print(f"{len(ud):,} Up-or-Down-markeder · "
          f"{ud['_evt'].notna().sum():,} har event_start · "
          f"{ud['_has_file'].sum():,} har prisfil\n")

    print("=" * 74)
    print("1. Klokkeslett for end_date — skiller intradag fra dognmarked\n")
    print("Minutt i timen (topp 8):")
    print(ud["_min_of_hour"].value_counts().head(8).to_string())
    print(f"\nAntall ulike timer i dognet: {ud['_hour'].nunique()} av 24")
    on_5 = ud["_min_of_hour"].mod(5).eq(0)
    print(f"Avsluttes paa 5-minutters-merke: {on_5.sum():,} ({on_5.mean():.0%})")
    spread = ud.groupby(ud["_end"].dt.date).size()
    print(f"Markeder per dag: median {spread.median():.0f}, maks {spread.max():.0f}")
    print("  (~288 per aktivum per dag = femminutters; ~1 = dognmarked)\n")

    print("=" * 74)
    print(f"2. Naar starter handelen? Minuttdata {LOOKBACK_MIN} min for end_date\n")

    cand = ud[ud["volume"] >= 5000].nlargest(SAMPLES * 3, "volume")
    print(f"{'marked':<44} {'volum':>10} {'forste':>8} {'punkter':>8} {'bevegelse':>10}")
    print("-" * 84)

    widths = []
    tested = 0
    for _, m in cand.iterrows():
        if tested >= SAMPLES:
            break
        toks = json.loads(m["token_ids"])
        if not toks:
            continue
        end_ts = int(m["_end"].timestamp())
        df = fetch_price_history(toks[0], 1,
                                 window=(end_ts - LOOKBACK_MIN * 60, end_ts + 300))
        if df is ERROR or df is None or df.empty:
            print(f"{str(m['question'])[:44]:<44} {m['volume']:>10,.0f} "
                  f"{'tom':>8}")
            tested += 1
            continue

        ts = pd.to_datetime(df["timestamp"], utc=True).sort_values()
        first_min = (m["_end"] - ts.iloc[0]).total_seconds() / 60
        moves = df["price"].nunique()
        widths.append(first_min)
        tested += 1
        print(f"{str(m['question'])[:44]:<44} {m['volume']:>10,.0f} "
              f"{first_min:>7.0f}m {len(df):>8} {moves:>10}")

    if widths:
        s = pd.Series(widths)
        print(f"\nForste handel for end_date: median {s.median():.0f} min · "
              f"min {s.min():.0f} · maks {s.max():.0f}")
        print("Er medianen ~5, er handelsperioden fem minutter og vinduet kan "
              "settes til end_date minus faa minutter.")
        print(f"Er den ~{LOOKBACK_MIN}, naadde vi taket — oek LOOKBACK_MIN og "
              "kjor igjen.")


if __name__ == "__main__":
    main()
