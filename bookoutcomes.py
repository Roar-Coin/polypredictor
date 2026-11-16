#!/usr/bin/env python3
"""Hent det faktiske utfallet for hvert marked opptakeren har tatt opp.

Opptakene sier hva boken viste, ikke hvem som vant. Uten utfallet kan man maale
hva en handel koster, men ikke om den lonte seg — og heller ikke det som betyr
mest for hvilende ordrer: om man blir fylt nettopp naar man tar feil.

Utfallet hentes fra Gamma, ett marked om gangen paa /markets/{id}. Det er den
eneste veien som virker (maalt 16. november): ?id= gir tom liste, kommaliste
gir 422, og datovinduet for lukkede markeder kappes paa 2100 treff.

Resultatet er books/outcomes.parquet: market_id, p0, p1 — utbetalingen per
andel for hvert av de to utfallene, slik oraklet avgjorde det. Bare markeder
som er endelig avgjort lagres; resten proves igjen neste kjoring.

Kjoringen er inkrementell og har tidsbudsjett: nyeste markeder forst, og det
som ikke rekkes, tas neste gang.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests

BUCKET = "hindsight-data"
DAILY = "books/daily/"
UT = "books/outcomes.parquet"
GAMMA = "https://gamma-api.polymarket.com"

KONTO = os.environ.get("R2_ACCOUNT_ID", "")
if not KONTO:
    sys.exit("R2_ACCOUNT_ID mangler — kan ikke naa bucketen.")
EP = f"https://{KONTO}.r2.cloudflarestorage.com"

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "hindsight-bookoutcomes/1.0"


def aws(*args):
    r = subprocess.run(["aws", *args, "--endpoint-url", EP],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args[:3])} …: {r.stderr.strip()[:400]}")
    return r.stdout


def hent_utfall(market_id):
    """(market_id, p0, p1) naar markedet er endelig avgjort, ellers None."""
    for forsok in range(3):
        try:
            r = SESSION.get(f"{GAMMA}/markets/{market_id}", timeout=20)
        except requests.RequestException:
            time.sleep(1.5 * (forsok + 1))
            continue
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2.0 * (forsok + 1))
            continue
        if r.status_code != 200:
            return None
        try:
            m = r.json()
            priser = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        except (ValueError, TypeError):
            return None
        if not m.get("closed") or len(priser) < 2:
            return None
        # Et lukket marked kan fortsatt staa til avgjorelse. Da er prisene siste
        # handel, ikke utbetalingen — akkurat forskjellen som snudde
        # sportsregelen fra pluss til minus. Bare endelige utfall slipper inn.
        status = (m.get("umaResolutionStatus") or "").lower()
        endelig = all(p in (0.0, 0.5, 1.0) for p in priser[:2])
        if status not in ("resolved", "") or not endelig:
            return None
        if abs(priser[0] + priser[1] - 1.0) > 1e-6:
            return None
        return (str(market_id), priser[0], priser[1])
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-seconds", type=int, default=900)
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    t0 = time.monotonic()

    with tempfile.TemporaryDirectory() as d:
        mappe = Path(d)
        aws("s3", "cp", f"s3://{BUCKET}/{DAILY}", str(mappe / "daily"),
            "--recursive", "--only-show-errors")
        slutt = {}
        for f in sorted((mappe / "daily").glob("quotes-*.parquet")):
            try:
                df = pd.read_parquet(f, columns=["market_id", "end_ts"])
            except Exception as e:
                print(f"  hopper over {f.name}: {e}", flush=True)
                continue
            for mid, ts in df.groupby("market_id")["end_ts"].max().items():
                slutt[str(mid)] = max(slutt.get(str(mid), 0), int(ts))
        if not slutt:
            sys.exit("Ingen dagfiler aa lese — kjor bookindex forst.")

        kjent = pd.DataFrame(columns=["market_id", "p0", "p1"])
        try:
            aws("s3", "cp", f"s3://{BUCKET}/{UT}", str(mappe / "ut.parquet"),
                "--only-show-errors")
            kjent = pd.read_parquet(mappe / "ut.parquet")
        except Exception:
            pass
        har = set(kjent["market_id"].astype(str))

        # Bare markeder som er over, med en time til oraklet. Nyeste forst.
        naa = int(time.time())
        ko = sorted((m for m, ts in slutt.items() if m not in har and ts < naa - 3600),
                    key=lambda m: -slutt[m])
        print(f"{len(slutt):,} markeder i opptakene, {len(har):,} med kjent utfall, "
              f"{len(ko):,} aa hente.", flush=True)

        nye, sjekket = [], 0
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for i in range(0, len(ko), 200):
                if time.monotonic() - t0 > args.max_seconds:
                    break
                bolk = ko[i:i + 200]
                nye.extend(r for r in pool.map(hent_utfall, bolk) if r)
                sjekket += len(bolk)
                if sjekket % 1000 == 0:
                    print(f"  {sjekket:,} sjekket · {len(nye):,} avgjort", flush=True)

        if nye:
            alle = pd.concat([kjent, pd.DataFrame(nye, columns=["market_id", "p0", "p1"])],
                             ignore_index=True).drop_duplicates("market_id", keep="last")
            alle["market_id"] = alle["market_id"].astype(str)
            alle["p0"] = alle["p0"].astype(float)
            alle["p1"] = alle["p1"].astype(float)
            alle.to_parquet(mappe / "ny.parquet", index=False)
            aws("s3", "cp", str(mappe / "ny.parquet"), f"s3://{BUCKET}/{UT}",
                "--content-type", "application/octet-stream",
                "--cache-control", "max-age=60", "--only-show-errors")
            print(f"Utfall lagret: {len(alle):,} markeder ({len(nye):,} nye).", flush=True)
        else:
            print("Ingen nye avgjorte markeder denne gangen.", flush=True)

        rest = len(ko) - sjekket
        if rest > 0:
            print(f"GJENSTAAR {rest:,} markeder — kjor jobben en gang til.", flush=True)
        uavgjort = sjekket - len(nye)
        if uavgjort:
            print(f"{uavgjort:,} sjekkede markeder var ikke endelig avgjort ennaa "
                  f"og proves igjen neste gang.", flush=True)


if __name__ == "__main__":
    main()
