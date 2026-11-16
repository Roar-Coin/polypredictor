#!/usr/bin/env python3
"""Slaa sammen ordrebok-opptakene til en fil per dag, og skriv en indeks.

Opptakeren skriver en parquet per runde — rundt 270 filer per skift, fire skift
i dognet. Det er riktig under opptak (en drept jobb mister bare siste runde),
men ubrukelig aa lese fra nettleseren: tusen HTTP-kall for aa svare paa ett
sporsmaal. Denne jobben leser raafilene fra R2, skriver EN fil per dag under
books/daily/, og en books/index.json som sier hvilke dager som finnes.

Raafilene slettes ikke. De er originalen, og bokdybde kan ikke hentes om igjen.

Kjoringen er idempotent: en dag hoppes over naar indeksen allerede teller like
mange raafiler som bottene inneholder. Dagens dato blir derfor skrevet om hver
gang, siden den fortsatt vokser.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
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

# Kolonner som MAA vaere tall. None fra et tomt bokside gjor at pandas leser
# kolonnen som object i en fil og float i den neste; uten tvang feiler
# sammenslaaingen eller skriver en kolonne nettleseren ikke kan regne paa.
TALL = ["mins_to_end", "volume", "best_bid", "best_ask", "mid", "spread",
        "bid_depth_usd", "ask_depth_usd"]
HELTALL = ["ts", "end_ts", "outcome_index", "bid_levels", "ask_levels"]


def aws(*args, tekst=True):
    r = subprocess.run(["aws", *args, "--endpoint-url", EP],
                       capture_output=True, text=tekst)
    if r.returncode != 0:
        raise RuntimeError(f"aws {' '.join(args[:3])} …: {r.stderr.strip()[:400]}")
    return r.stdout


def list_alle():
    """Alle noekler under books/. awscli paginerer selv."""
    ut = aws("s3api", "list-objects-v2", "--bucket", BUCKET,
             "--prefix", PREFIX, "--output", "json")
    data = json.loads(ut or "{}")
    return data.get("Contents", []) or []


def les_indeks():
    with tempfile.TemporaryDirectory() as d:
        maal = Path(d) / "index.json"
        try:
            aws("s3", "cp", f"s3://{BUCKET}/{INDEX}", str(maal))
        except RuntimeError:
            return {}
        try:
            gammel = json.loads(maal.read_text())
        except json.JSONDecodeError:
            return {}
    return {d["date"]: d for d in gammel.get("days", [])}


def slaa_sammen(dato, mappe):
    """Les alle quotes-filene for en dag og returner en ramme."""
    filer = sorted(mappe.glob("quotes-*.parquet"))
    rammer = []
    for f in filer:
        try:
            rammer.append(pd.read_parquet(f))
        except Exception as e:                       # en halv fil skal ikke
            print(f"  hopper over {f.name}: {e}", flush=True)   # felle dagen
    if not rammer:
        return None
    big = pd.concat(rammer, ignore_index=True)
    for k in TALL + [c for c in big.columns
                     if c.startswith(("fill_", "cost_"))]:
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


def main():
    objekter = list_alle()
    raa = defaultdict(int)
    for o in objekter:
        m = RAAFIL.match(o["Key"])
        if m:
            raa[m.group(1)] += 1
    if not raa:
        sys.exit("Fant ingen raafiler under books/ — ingenting aa gjore.")

    finnes = {o["Key"] for o in objekter}
    gammel = les_indeks()
    i_dag = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    print(f"{len(raa)} dager med opptak, {sum(raa.values())} raafiler", flush=True)

    dager = []
    for dato in sorted(raa):
        noekkel = f"{DAILY}quotes-{dato}.parquet"
        kjent = gammel.get(dato)
        ferdig = (kjent and kjent.get("raw") == raa[dato]
                  and noekkel in finnes and dato != i_dag)
        if ferdig:
            dager.append(kjent)
            print(f"{dato}: uendret ({raa[dato]} filer) — hopper over", flush=True)
            continue

        with tempfile.TemporaryDirectory() as d:
            mappe = Path(d)
            aws("s3", "cp", f"s3://{BUCKET}/{PREFIX}{dato}/", str(mappe),
                "--recursive", "--exclude", "*", "--include", "quotes-*.parquet",
                "--only-show-errors")
            big = slaa_sammen(dato, mappe)
            if big is None or big.empty:
                print(f"{dato}: ingen lesbare rader", flush=True)
                continue
            lokal = mappe / "dag.parquet"
            big.to_parquet(lokal, index=False)
            aws("s3", "cp", str(lokal), f"s3://{BUCKET}/{noekkel}",
                "--content-type", "application/octet-stream", "--only-show-errors")
            bytes_ = lokal.stat().st_size

        kat = big["category"].value_counts().to_dict() if "category" in big else {}
        dager.append({
            "date": dato, "file": noekkel, "raw": raa[dato],
            "rows": int(len(big)), "bytes": int(bytes_),
            "from_ts": int(big["ts"].min()), "to_ts": int(big["ts"].max()),
            "categories": {k: int(v) for k, v in kat.items()},
        })
        print(f"{dato}: {len(big):,} rader fra {raa[dato]} filer "
              f"→ {bytes_/1048576:.1f} MB", flush=True)

    indeks = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "days": dager}
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "index.json"
        p.write_text(json.dumps(indeks, indent=1))
        aws("s3", "cp", str(p), f"s3://{BUCKET}/{INDEX}",
            "--content-type", "application/json",
            "--cache-control", "max-age=60", "--only-show-errors")
    print(f"Indeks skrevet: {len(dager)} dager, "
          f"{sum(d['rows'] for d in dager):,} rader totalt.", flush=True)


if __name__ == "__main__":
    main()
