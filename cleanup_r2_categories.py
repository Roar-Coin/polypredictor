#!/usr/bin/env python3
"""
Rydder bort prisfiler som ligger igjen i feil kategorimappe i R2.

Etter kategorifiksen flytter ingest.py filene lokalt, men `aws s3 sync` uten
--delete fjerner aldri den gamle stien. Kopien under raw/prices/other/ blir
liggende og lastes ned paa nytt hver natt.

Dette er hygiene, ikke en datafeil: opprydderen i ingest.py overskriver
duplikatet lokalt for publisering, saa datasettet appen leser er riktig
uansett. Skriptet sparer lagring og restore-tid.

Kjorer i toermodus som standard. Legg til --apply for aa faktisk slette.

  python cleanup_r2_categories.py            # vis hva som ville blitt slettet
  python cleanup_r2_categories.py --apply    # slett

SIKKERHET: en fil slettes kun naar den korrekt plasserte kopien er bekreftet
til stede i R2, med samme stoerrelse. Finnes den ikke, roeres ingenting.
"""
import argparse
import os
import sys
from collections import Counter

import boto3
import pandas as pd

BUCKET = "hindsight-data"
PREFIX = "raw/prices/"
MARKETS = "data/markets.parquet"


def client():
    acct = os.environ.get("R2_ACCOUNT_ID")
    if not acct:
        sys.exit("R2_ACCOUNT_ID mangler.")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{acct}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def list_all(s3):
    """Alle prisfiler i R2 -> {noekkel: storrelse}."""
    out = {}
    token = None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": PREFIX, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        r = s3.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            if o["Key"].endswith(".parquet"):
                out[o["Key"]] = o["Size"]
        if not r.get("IsTruncated"):
            return out
        token = r["NextContinuationToken"]
        print(f"  ... {len(out)} filer listet", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="slett faktisk")
    args = ap.parse_args()

    if not os.path.exists(MARKETS):
        sys.exit(f"Fant ikke {MARKETS} — hent den ned fra R2 forst.")
    markets = pd.read_parquet(MARKETS, columns=["market_id", "category"])
    correct = dict(zip(markets["market_id"].astype(str), markets["category"]))
    print(f"{len(correct)} markeder i metadata")

    s3 = client()
    print("Lister prisfiler i R2 ...")
    files = list_all(s3)
    print(f"{len(files)} prisfiler i R2\n")

    # noekkel: raw/prices/<kategori>/<market_id>.parquet
    placed = {}          # market_id -> {kategori: (noekkel, storrelse)}
    for key, size in files.items():
        parts = key[len(PREFIX):].split("/")
        if len(parts) != 2 or not parts[1].endswith(".parquet"):
            continue
        cat, mid = parts[0], parts[1][:-len(".parquet")]
        placed.setdefault(mid, {})[cat] = (key, size)

    stale, orphan, moved_from = [], [], Counter()
    for mid, locs in placed.items():
        want = correct.get(mid)
        if want is None:
            orphan.append(mid)          # ukjent marked — roeres ikke
            continue
        good = locs.get(want)
        for cat, (key, size) in locs.items():
            if cat == want:
                continue
            # Slett kun naar den riktige kopien finnes OG er like stor.
            # Ulik storrelse betyr at de ikke er samme fil — da er sletting utrygt.
            if good and good[1] == size:
                stale.append((key, size))
                moved_from[f"{cat} -> {want}"] += 1

    total = sum(sz for _, sz in stale)
    print(f"=== {len(stale)} foreldede filer ({total/1e6:.0f} MB) ===")
    for pair, n in moved_from.most_common(20):
        print(f"  {pair:<28} {n:>6}")
    if orphan:
        print(f"\n{len(orphan)} filer for markeder som ikke finnes i metadata — hoppes over.")

    # Filer som bare finnes ett sted, men i feil mappe: ikke duplikater.
    # De skal flyttes, ikke slettes — og ingest.py gjor det ved neste kjoring.
    only_wrong = sum(1 for mid, locs in placed.items()
                     if correct.get(mid) and correct[mid] not in locs)
    if only_wrong:
        print(f"{only_wrong} filer ligger i feil mappe uten en riktig kopi — "
              f"la ingest.py flytte dem forst, kjor dette igjen etterpaa.")

    if not stale:
        print("\nIngenting aa rydde.")
        return
    if not args.apply:
        print("\nTOERRKJORING. Kjor med --apply for aa slette.")
        return

    print("\nSletter ...")
    done = 0
    for i in range(0, len(stale), 1000):
        chunk = stale[i:i + 1000]
        s3.delete_objects(
            Bucket=BUCKET,
            Delete={"Objects": [{"Key": k} for k, _ in chunk], "Quiet": True},
        )
        done += len(chunk)
        print(f"  {done}/{len(stale)}", flush=True)
    print(f"Ferdig. {total/1e6:.0f} MB frigjort.")


if __name__ == "__main__":
    main()
