#!/usr/bin/env python3
"""Diagnose: har eldre Polymarket-markeder prishistorikk i det hele tatt?
Plukker hoyvolum-markeder per aar og prover ulike API-varianter."""
import json, time
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd, requests

CLOB = "https://clob.polymarket.com"
S = requests.Session()
S.headers.update({"User-Agent": "hindsight-diagnose/0.1"})

def probe(token, **params):
    try:
        r = S.get(f"{CLOB}/prices-history", params={"market": token, **params}, timeout=30)
        if r.status_code != 200:
            return f"HTTP {r.status_code}: {r.text[:80]}"
        n = len((r.json().get("history") or []))
        return f"{n} punkter"
    except Exception as e:
        return f"feil: {e}"

m = pd.read_parquet("data/markets.parquet")
m = m[(m["volume"] > 50000) & (m["token_ids"] != "[]")].copy()
m["year"] = m["end_date"].str[:4]

for year in ["2021", "2022", "2023", "2024", "2025", "2026"]:
    sample = m[m["year"] == year].nlargest(3, "volume")
    if sample.empty:
        print(f"\n=== {year}: ingen markeder i utvalget ==="); continue
    print(f"\n=== {year} ===")
    for _, row in sample.iterrows():
        tok = json.loads(row["token_ids"])[0]
        end = row["end_date"] or ""
        try:
            end_ts = int(datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp())
        except Exception:
            end_ts = int(time.time())
        start_ts = end_ts - 90 * 86400
        print(f"\n  {row['question'][:70]}  (vol ${row['volume']:,.0f}, slutt {end[:10]})")
        print(f"    interval=max fid=60 : {probe(tok, interval='max', fidelity=60)}")
        print(f"    interval=max fid=1440: {probe(tok, interval='max', fidelity=1440)}")
        print(f"    startTs/endTs fid=60 : {probe(tok, startTs=start_ts, endTs=end_ts, fidelity=60)}")
        print(f"    startTs/endTs fid=1  : {probe(tok, startTs=start_ts, endTs=end_ts, fidelity=1)}")
        time.sleep(0.5)
print("\nDiagnose ferdig.")
