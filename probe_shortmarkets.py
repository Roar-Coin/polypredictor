#!/usr/bin/env python3
"""probe_shortmarkets.py — ligger de EKTE femminuttersmarkedene i datasettet?

«≈ 5 min» i appen returnerer bare timesmarkeder av typen «Bitcoin above 65,200
on August 2, 6AM ET». Aarsaken er at varighet regnes som end_date - event_start,
og for et timesmarked er event_start selve streiketidspunktet — altsaa lik
end_date, varighet 0. De faller dermed i samme botte som, og foran, markedene vi
faktisk vil ha.

Denne proben svarer paa om problemet er innsamling eller filter:

  Up-or-Down finnes ikke i markets.parquet  -> ingest henter dem ikke (innsamling)
  finnes, men uten prisfil                  -> hentet, men uten historikk
  finnes med prisfil                        -> rent filterproblem i app.html

Kjores etter at data/markets.parquet er hentet fra R2.
"""
import re
import sys
import pandas as pd

from ingest import DATA_DIR, PRICES_DIR

# Polymarket bruker flere skrivemaater; begge familiene fanges bredt.
UP_DOWN = re.compile(r"up or down", re.I)
STRIKE = re.compile(r"\babove\b.*\bET\b", re.I)


def describe(df, label):
    if df.empty:
        print(f"{label}: INGEN treff\n")
        return
    print(f"{label}: {len(df):,} markeder")
    print(f"  volum      median ${df['volume'].median():,.0f} · "
          f"min ${df['volume'].min():,.0f} · maks ${df['volume'].max():,.0f}")
    print(f"  under $1000 volum: {(df['volume'] < 1000).sum():,} "
          f"({(df['volume'] < 1000).mean():.0%})")
    print(f"  avsluttet  {df['_end'].min():%Y-%m-%d} → {df['_end'].max():%Y-%m-%d}")
    print(f"  varighet (end - event_start)  median {df['_settle'].median():.1f} min")
    print(f"  levetid  (end - start_date)   median {df['_life'].median():.1f} min")
    print(f"  har prisfil paa disk: {df['_has_file'].sum():,} av {len(df):,}")
    print(f"  kategorier: {df['category'].value_counts().head(4).to_dict()}")
    print("  eksempler:")
    for _, r in df.nlargest(4, "volume").iterrows():
        print(f"    {str(r['question'])[:58]:<58} "
              f"vindu {r['_settle']:>6.1f}m  levetid {r['_life']:>8.1f}m  "
              f"${r['volume']:>10,.0f}  {'fil' if r['_has_file'] else 'INGEN FIL'}")
    print()


def main():
    path = DATA_DIR / "markets.parquet"
    if not path.exists():
        print(f"Fant ikke {path}.")
        sys.exit(1)

    mk = pd.read_parquet(path)
    end = pd.to_datetime(mk["end_date"], errors="coerce", utc=True)
    start = pd.to_datetime(mk["start_date"], errors="coerce", utc=True)
    evt = (pd.to_datetime(mk["event_start"], errors="coerce", utc=True)
           if "event_start" in mk.columns else pd.Series(pd.NaT, index=mk.index))

    on_disk = {p.stem for p in PRICES_DIR.rglob("*.parquet")}
    mk = mk.assign(
        _end=end,
        _settle=(end - evt).dt.total_seconds() / 60,
        _life=(end - start).dt.total_seconds() / 60,
        _has_file=mk["market_id"].astype(str).isin(on_disk),
        _q=mk["question"].astype(str),
    )

    print(f"{len(mk):,} markeder i metadata · {len(on_disk):,} prisfiler paa disk\n")
    print("=" * 78)

    describe(mk[mk["_q"].str.contains(UP_DOWN)], "EKTE 5-MIN (question ~ 'up or down')")
    describe(mk[mk["_q"].str.contains(STRIKE)], "TIMESMARKED (question ~ 'above ... ET')")

    print("=" * 78)
    print("Hva appens «≈ 5 min»-botte (0-7 min paa end - event_start) fanger:\n")
    bucket = mk[mk["_settle"].between(0, 7) & mk["_has_file"]]
    fam = pd.Series("annet", index=bucket.index)
    fam[bucket["_q"].str.contains(STRIKE)] = "timesmarked"
    fam[bucket["_q"].str.contains(UP_DOWN)] = "ekte 5-min"
    print(fam.value_counts().to_string())

    print("\nSamme botte, men paa LEVETID (end - start_date) i stedet:\n")
    bucket2 = mk[mk["_life"].between(0, 7) & mk["_has_file"]]
    fam2 = pd.Series("annet", index=bucket2.index)
    fam2[bucket2["_q"].str.contains(STRIKE)] = "timesmarked"
    fam2[bucket2["_q"].str.contains(UP_DOWN)] = "ekte 5-min"
    print(fam2.value_counts().to_string())

    print("\nEr den andre kolonnen bedre, er fiksen i app.html. Er begge tomme "
          "for 'ekte 5-min', er den i ingest.")


if __name__ == "__main__":
    main()
