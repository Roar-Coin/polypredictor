#!/usr/bin/env python3
"""probe_updown.py v2 — mal handelsperioden i de EKTE femminuttersmarkedene.

v1 sorterte paa volum og fikk bare dognmarkedene ("Bitcoin Up or Down on
June 2?", $1.8M), som handler i timevis. De er ikke det vi er ute etter.

Ingen av de 191 727 har event_start, saa vi kan ikke lese kadensen ut av
metadata. Men klokkeslettet for end_date avsloerer den: tre kadenser ligger
oppaa hverandre, og et marked som avsluttes paa et minuttmerke som IKKE er
delelig med 15 kan bare komme fra femminuttersserien.

  :00                 dogn + time + kvarter + fem      39 227
  :15 :30 :45         kvarter + fem                  ~21 830 hver
  :05 :10 :20 ...     BARE fem                       ~10 875 hver

Proben deler familien etter kadens, viser volum og filedekning per klasse,
og maaler saa naar handelen faktisk starter i den rene femminuttersklassen.
"""
import json
import re
import sys
import pandas as pd

from ingest import fetch_price_history, DATA_DIR, CHECKPOINT, ERROR

UP_DOWN = re.compile(r"up or down", re.I)
LOOKBACK_MIN = 45     # femminuttersmarked skal metter lenge for dette
SAMPLES = 10


def cadence(minute):
    if minute % 15 != 0:
        return "ren femminutters"
    if minute != 0:
        return "kvarter eller fem"
    return "dogn/time/kvarter/fem"


def main():
    path = DATA_DIR / "markets.parquet"
    if not path.exists():
        print(f"Fant ikke {path}."); sys.exit(1)

    mk = pd.read_parquet(path)
    end = pd.to_datetime(mk["end_date"], errors="coerce", utc=True)
    done = set(json.loads(CHECKPOINT.read_text())) if CHECKPOINT.exists() else set()

    ud = mk[mk["question"].astype(str).str.contains(UP_DOWN)].assign(_end=end)
    ud = ud[ud["_end"].notna()]
    ud = ud.assign(
        _has_file=ud["market_id"].astype(str).isin(done),
        _klasse=ud["_end"].dt.minute.map(cadence),
    )

    print("=" * 86)
    print("1. Kadens-klasser\n")
    g = ud.groupby("_klasse")
    tab = pd.DataFrame({
        "markeder": g.size(),
        "median volum": g["volume"].median().round(0),
        "over $1000": g["volume"].apply(lambda s: (s >= 1000).sum()),
        "har prisfil": g["_has_file"].sum(),
    }).sort_values("markeder", ascending=False)
    print(tab.to_string())

    ren = ud[ud["_klasse"] == "ren femminutters"]
    print(f"\nEksempler paa spoersmaalstekst i ren femminutters-klasse:")
    for q in ren.nlargest(3, "volume")["question"].head(3):
        print(f"  {q}")
    for q in ren.sample(min(3, len(ren)), random_state=0)["question"]:
        print(f"  {q}")

    print("\n" + "=" * 86)
    print(f"2. Naar starter handelen? Minuttdata {LOOKBACK_MIN} min for end_date")
    print("   (kun ren femminutters-klasse, sortert paa volum)\n")

    cand = ren[ren["volume"] >= 1000].nlargest(SAMPLES * 3, "volume")
    print(f"{'marked':<46} {'volum':>9} {'forste':>7} {'punkter':>8} {'priser':>7}")
    print("-" * 86)

    widths, tested = [], 0
    for _, m in cand.iterrows():
        if tested >= SAMPLES:
            break
        toks = json.loads(m["token_ids"])
        if not toks:
            continue
        end_ts = int(m["_end"].timestamp())
        df = fetch_price_history(toks[0], 1,
                                 window=(end_ts - LOOKBACK_MIN * 60, end_ts + 300))
        tested += 1
        q = str(m["question"])[:46]
        if df is ERROR or df is None or df.empty:
            print(f"{q:<46} {m['volume']:>9,.0f} {'tom':>7}")
            continue
        ts = pd.to_datetime(df["timestamp"], utc=True).sort_values()
        first = (m["_end"] - ts.iloc[0]).total_seconds() / 60
        widths.append(first)
        print(f"{q:<46} {m['volume']:>9,.0f} {first:>6.0f}m "
              f"{len(df):>8} {df['price'].nunique():>7}")

    if widths:
        s = pd.Series(widths)
        print(f"\nForste handel for end_date: median {s.median():.0f} min "
              f"(min {s.min():.0f}, maks {s.max():.0f})")
        if s.median() >= LOOKBACK_MIN - 1:
            print("METTET — oek LOOKBACK_MIN. Markedene handler lenger enn "
                  "kadensen tilsier, saa vinduet maa settes bredere enn 5 min.")
        else:
            print(f"Handelsperioden er ~{s.median():.0f} min. Vindus-kallet kan "
                  f"da settes til end_date minus {s.max():.0f} min, uten event_start.")


if __name__ == "__main__":
    main()
