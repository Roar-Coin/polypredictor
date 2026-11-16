#!/usr/bin/env python3
"""Slaa sammen ordrebok-opptakene til en fil per dag, og skriv en indeks.

Opptakeren skriver en parquet per runde — over tusen filer i dognet. Det er
riktig under opptak (en drept jobb mister bare siste runde), men ubrukelig aa
lese fra nettleseren: tusen HTTP-kall for aa svare paa ett sporsmaal. Denne
jobben leser raafilene fra R2, skriver EN fil per dag under books/daily/, og en
books/index.json som sier hvilke dager som finnes.

Raafilene slettes ikke. De er originalen, og bokdybde kan ikke hentes om igjen.

TRE TING DEN FORSTE VERSJONEN TOK FEIL OM, og som er rettet her:

  Arkivet er stort. Det gaar tilbake til september, og en full gjennomgang
  rekker ikke innenfor en jobb. Derfor et tidsbudsjett som stopper RENT, og en
  indeks som skrives etter HVER dag — ikke til slutt. En avbrutt kjoring skal
  etterlate alt den rakk, slik at neste kjoring fortsetter der den slapp.

  Nyeste dag forst. Blir jobben likevel kuttet, er det septemberdagene som
  mangler, ikke dagene fra i gaar.

  En dag regnes som ferdig naar dagfilen er NYERE enn den ferskeste raafilen
  for den dagen. Det staar i objektlistingen og trenger hverken indeks eller
  telling, saa dager som ble slaatt sammen for indeksen gikk tapt, gjores ikke
  om igjen.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

BUCKET = "hindsight-data"
PREFIX = "books/"
DAILY = "books/daily/"
INDEX = "books/index.json"

KONTO = os.environ.get("R2_ACCOUNT_ID", "")
if not KONTO:
    sys.exit("R2_ACCOUNT_ID mangler — kan ikke naa bucketen.")
EP = f"https://{KONTO}.r2.cloudflarestorage.com"

RAAFIL = re.compile(r"^books/(\d{4}-\d{2}-\d{2})/quotes-(\d{6})\.parquet$")
DAGFIL = re.compile(r"^books/daily/quotes-(\d{4}-\d{2}-\d{2})\.parquet$")

# Kolonner som MAA vaere tall. None fra en tom bokside gjor at pandas leser
# kolonnen som object i en fil og float i den neste; uten tvang feiler
# sammenslaaingen eller skriver en kolonne nettleseren ikke kan regne paa.
TALL = ["mins_to_end", "volume", "best_bid", "best_ask", "mid", "spread",
        "bid_depth_usd", "ask_depth_usd"]
HELTALL = ["ts", "end_ts", "outcome_index", "bid_levels", "ask_levels"]


def aws(*args):
    r = subprocess.run(["aws", *args, "--endpoint-url", EP],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args[:3])} …: {r.stderr.strip()[:400]}")
    return r.stdout


def list_alle():
    """Alle noekler under books/, med tidspunkt. awscli paginerer selv."""
    ut = aws("s3api", "list-objects-v2", "--bucket", BUCKET,
             "--prefix", PREFIX, "--output", "json")
    return json.loads(ut or "{}").get("Contents", []) or []


def les_indeks():
    with tempfile.TemporaryDirectory() as d:
        maal = Path(d) / "index.json"
        try:
            aws("s3", "cp", f"s3://{BUCKET}/{INDEX}", str(maal), "--only-show-errors")
            gammel = json.loads(maal.read_text())
        except (RuntimeError, json.JSONDecodeError):
            return {}
    return {d["date"]: d for d in gammel.get("days", []) if d.get("date")}


def skriv_indeks(dager):
    """Skrives etter hver dag. En avbrutt kjoring skal etterlate en gyldig fil."""
    indeks = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "days": [dager[d] for d in sorted(dager)]}
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "index.json"
        p.write_text(json.dumps(indeks, indent=1))
        aws("s3", "cp", str(p), f"s3://{BUCKET}/{INDEX}",
            "--content-type", "application/json",
            "--cache-control", "max-age=60", "--only-show-errors")


def rydd(big):
    for k in TALL + [c for c in big.columns if c.startswith(("fill_", "cost_"))]:
        if k in big.columns:
            big[k] = pd.to_numeric(big[k], errors="coerce")
    for k in HELTALL:
        if k in big.columns:
            big[k] = pd.to_numeric(big[k], errors="coerce").astype("int64")
    if "crossed" in big.columns:
        big["crossed"] = big["crossed"].fillna(False).astype(bool)
    for k in ("market_id", "token_id", "category"):
        if k in big.columns:
            big[k] = big[k].astype(str)
    return big.sort_values("ts", ignore_index=True)


def slaa_sammen(mappe):
    """Les alle quotes-filene i en mappe og returner en ramme."""
    rammer = []
    for f in sorted(mappe.glob("quotes-*.parquet")):
        try:
            rammer.append(pd.read_parquet(f))
        except Exception as e:                        # en halv fil skal ikke
            print(f"  hopper over {f.name}: {e}", flush=True)   # felle dagen
    if not rammer:
        return None
    return rydd(pd.concat(rammer, ignore_index=True))


def statistikk(dato, big, raa_antall, bytes_):
    kat = big["category"].value_counts().to_dict() if "category" in big else {}
    return {"date": dato, "file": f"{DAILY}quotes-{dato}.parquet",
            "raw": raa_antall, "rows": int(len(big)), "bytes": int(bytes_),
            "from_ts": int(big["ts"].min()), "to_ts": int(big["ts"].max()),
            "categories": {k: int(v) for k, v in kat.items()}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-seconds", type=int, default=2400,
                    help="stopp rent for jobben blir drept")
    ap.add_argument("--days", type=int, default=0,
                    help="bare de N nyeste dagene (0 = alle)")
    args = ap.parse_args()
    t0 = time.monotonic()

    objekter = list_alle()
    raa, ferskest = defaultdict(int), {}
    dagfil = {}
    for o in objekter:
        m = RAAFIL.match(o["Key"])
        if m:
            d = m.group(1)
            raa[d] += 1
            ferskest[d] = max(ferskest.get(d, ""), o["LastModified"])
            continue
        m = DAGFIL.match(o["Key"])
        if m:
            dagfil[m.group(1)] = o
    if not raa:
        sys.exit("Fant ingen raafiler under books/ — ingenting aa gjore.")

    dager = les_indeks()
    i_dag = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rekkefolge = sorted(raa, reverse=True)            # nyeste forst
    if args.days:
        rekkefolge = rekkefolge[:args.days]

    print(f"{len(raa)} dager med opptak, {sum(raa.values())} raafiler. "
          f"Indeksen har {len(dager)} fra for.", flush=True)

    gjenstaar = []
    for dato in rekkefolge:
        if time.monotonic() - t0 > args.max_seconds:
            gjenstaar.append(dato)
            continue

        # Dagfilen er fersk nok naar den er skrevet ETTER siste raafil.
        fersk = (dato in dagfil and dato != i_dag
                 and dagfil[dato]["LastModified"] > ferskest[dato])

        if fersk and dato in dagers_med_tall(dager):
            print(f"{dato}: uendret — hopper over", flush=True)
            continue

        with tempfile.TemporaryDirectory() as d:
            mappe = Path(d)
            if fersk:
                # Dagfilen finnes, men indeksen mangler tallene. Hent den ene
                # fila i stedet for aa gjore hele dagen om igjen.
                lokal = mappe / "dag.parquet"
                aws("s3", "cp", f"s3://{BUCKET}/{DAILY}quotes-{dato}.parquet",
                    str(lokal), "--only-show-errors")
                big = rydd(pd.read_parquet(lokal))
                dager[dato] = statistikk(dato, big, raa[dato], lokal.stat().st_size)
                print(f"{dato}: allerede slaatt sammen, "
                      f"{len(big):,} rader indeksert", flush=True)
            else:
                aws("s3", "cp", f"s3://{BUCKET}/{PREFIX}{dato}/", str(mappe),
                    "--recursive", "--exclude", "*", "--include", "quotes-*.parquet",
                    "--only-show-errors")
                big = slaa_sammen(mappe)
                if big is None or big.empty:
                    print(f"{dato}: ingen lesbare rader", flush=True)
                    continue
                lokal = mappe / "dag.parquet"
                big.to_parquet(lokal, index=False)
                aws("s3", "cp", str(lokal), f"s3://{BUCKET}/{DAILY}quotes-{dato}.parquet",
                    "--content-type", "application/octet-stream", "--only-show-errors")
                dager[dato] = statistikk(dato, big, raa[dato], lokal.stat().st_size)
                print(f"{dato}: {len(big):,} rader fra {raa[dato]} filer "
                      f"→ {lokal.stat().st_size/1048576:.1f} MB", flush=True)

        skriv_indeks(dager)                            # etter HVER dag

    skriv_indeks(dager)
    print(f"Indeks: {len(dager)} dager, "
          f"{sum(d['rows'] for d in dager.values()):,} rader.", flush=True)
    if gjenstaar:
        print(f"GJENSTAAR {len(gjenstaar)} dager ({min(gjenstaar)} → "
              f"{max(gjenstaar)}) — kjor jobben en gang til.", flush=True)


def dagers_med_tall(dager):
    """Dager som allerede har rader i indeksen, ikke bare en plass."""
    return {d for d, v in dager.items() if v.get("rows")}


if __name__ == "__main__":
    main()
