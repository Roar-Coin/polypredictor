#!/usr/bin/env python3
"""Diagnose v2: finnes historikken bak kortere startTs/endTs-vinduer?"""
import json, time
from datetime import datetime
from pathlib import Path
import pandas as pd, requests

CLOB = "https://clob.polymarket.com"
S = requests.Session()
S.headers.update({"User-Agent": "hindsight-diagnose/0.2"})

def probe(token, **params):
    try:
        r = S.get(f"{CLOB}/prices-history", params={"market": token, **params}, timeout=30)
        if r.status_code != 200:
            return f"HTTP {r.status_code}: {r.text[:70]}"
        n = len((r.json().get("history") or []))
        return f"{n} punkter"
    except Exception as e:
        return f"feil: {e}"

m = pd.read_parquet("data/markets.parquet")
m = m[(m["volume"] > 50000) & (m["token_ids"] != "[]")].copy()
m["year"] = m["end_date"].str[:4]

WINDOWS = [  # (fidelity_min, vindulengde_dager)
    (1440, 30), (1440, 14), (60, 14), (60, 7), (60, 1), (10, 1), (1, 1),
]

for year in ["2021", "2022", "2023", "2024", "2025"]:
    sample = m[m["year"] == year].nlargest(2, "volume")
    if sample.empty:
        print(f"\n=== {year}: ingen markeder ==="); continue
    print(f"\n=== {year} ===")
    for _, row in sample.iterrows():
        tok = json.loads(row["token_ids"])[0]
        end = row["end_date"] or ""
        try:
            end_ts = int(datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp())
        except Exception:
            continue
        print(f"\n  {row['question'][:70]}  (slutt {end[:10]})")
        for fid, days in WINDOWS:
            res = probe(tok, startTs=end_ts - days * 86400, endTs=end_ts, fidelity=fid)
            print(f"    fid={fid:>4}m vindu={days:>2}d : {res}")
            time.sleep(0.3)
print("\nDiagnose v2 ferdig.")
