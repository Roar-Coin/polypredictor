#!/usr/bin/env python3
"""
Diagnose: hvorfor havner markeder i kategorien "other"?

Leser data/markets.parquet (allerede hentet ned fra R2) og svarer paa
det ene spoerssmaalet som avgjoer hvilken fiks som er riktig:

  A) Er tags-lista tom?          -> Gamma legger tags paa event-nivaa, ikke market.
                                    Fiks: hent tags fra event-objektet.
  B) Finnes det tags som ikke     -> TAG_MAP er for smal.
     staar i TAG_MAP?               Fiks: utvid kartet med de vanligste slugene.

Skriver ingenting og endrer ingenting. Ren lesing.
"""
import json
from collections import Counter
from pathlib import Path

import pandas as pd

from ingest import TAG_MAP, categorize  # samme logikk som produksjon

MARKETS = Path("data/markets.parquet")


def main():
    if not MARKETS.exists():
        raise SystemExit(f"Fant ikke {MARKETS} — kjor R2-restore forst.")

    df = pd.read_parquet(MARKETS)
    print(f"{len(df)} markeder i metadata\n")

    print("=== fordeling paa kategori (slik den er lagret) ===")
    for cat, n in df["category"].value_counts().items():
        print(f"  {cat:<10} {n:>7}  {n/len(df)*100:5.1f} %")

    other = df[df["category"] == "other"].copy()
    if other.empty:
        print("\nIngen markeder i 'other'. Ingenting aa diagnostisere.")
        return

    # Bare markeder med volum er interessante — resten handler du aldri i.
    tradable = other[other["volume"] >= 1000]
    print(f"\n{len(other)} i 'other', hvorav {len(tradable)} med volum >= $1000\n")

    def parse(v):
        try:
            out = json.loads(v) if isinstance(v, str) else (v or [])
            return [str(x).lower() for x in out]
        except (json.JSONDecodeError, TypeError):
            return []

    other["taglist"] = other["tags"].map(parse)
    empty = int((other["taglist"].str.len() == 0).sum())

    print("=== HOVEDSPORSMAALET ===")
    print(f"  uten tags i det hele tatt : {empty:>7}  ({empty/len(other)*100:.1f} %)")
    print(f"  med tags, men ingen traff : {len(other)-empty:>7}  "
          f"({(len(other)-empty)/len(other)*100:.1f} %)")
    if empty / len(other) > 0.5:
        print("\n  --> A: tags mangler paa market-nivaa. Hent dem fra event-objektet.")
    else:
        print("\n  --> B: tags finnes, men slugene staar ikke i TAG_MAP. Utvid kartet.")

    print("\n=== 40 vanligste tag-sluger i 'other' (ikke i TAG_MAP) ===")
    c = Counter(t for tags in other["taglist"] for t in tags if t not in TAG_MAP)
    for slug, n in c.most_common(40):
        print(f"  {slug:<32} {n:>6}")
    if not c:
        print("  (ingen — alle markeder i 'other' er helt uten tags)")

    print("\n=== hvilke sluger baerer volumet? (volum >= $1000) ===")
    tl = other.loc[tradable.index, "taglist"]
    cv = Counter(t for tags in tl for t in tags if t not in TAG_MAP)
    for slug, n in cv.most_common(25):
        print(f"  {slug:<32} {n:>6}")

    print("\n=== 30 tilfeldige spoerssmaal i 'other' med volum ===")
    for q in tradable["question"].dropna().sample(min(30, len(tradable)),
                                                  random_state=0):
        print(f"  {str(q)[:96]}")

    # Sanity: gir dagens categorize() samme svar som det som ligger lagret?
    # Hvis ikke, er den lagrede kategorien fra en eldre versjon av logikken.
    sample = df.sample(min(3000, len(df)), random_state=0)
    drift = sum(
        1 for _, r in sample.iterrows()
        if categorize({"tags": parse(r["tags"]), "question": r["question"]}) != r["category"]
    )
    print(f"\n=== drift: {drift}/{len(sample)} avviker fra dagens categorize() ===")
    if drift:
        print("  Lagret kategori er utdatert — regn om for alle rader etter fiksen.")


if __name__ == "__main__":
    main()
