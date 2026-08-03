#!/usr/bin/env python3
"""probe_retention.py — hvor gammelt kan et marked vaere og fortsatt gi minuttdata?

Bakgrunn: 9935 vindus-kall (startTs/endTs, fidelity=1) returnerte HTTP 200 med
tom historikk. Ingen serverfeil, ingen unntak — bare ingenting. Kallet er
velformet (probe_window bekreftet 32 punkter paa 60 sekunder 2. august), saa
enten er dataene borte, eller de serveres bare for ferske markeder.

Denne proben kjorer det samme kallet mot markeder gruppert etter hvor lenge
siden de ble avgjort, og sammenligner med det grove interval=max-kallet paa
nøyaktig samme token. Tre utfall, tre helt forskjellige konklusjoner:

  fint OK, grovt OK   -> vinduet virker; feilen ligger i ingest, ikke i API-et
  fint tomt, grovt OK -> minuttdata har egen, kortere oppbevaring
  begge tomme         -> markedet er falt ut av CLOB helt

Kjores etter en ingest saa data/markets.parquet finnes.
"""
import json
import sys
import pandas as pd

from ingest import fetch_price_history, DATA_DIR, ERROR

PAD_MIN = 15          # samme som --zoom-pad-minutes
MAX_SETTLE_MIN = 60   # samme som --zoom-max-minutes
MIN_VOLUME = 5000     # samme som --zoom-min-volume
PER_BUCKET = 5

BUCKETS = [(0, 1), (1, 2), (2, 3), (3, 7), (7, 14),
           (14, 30), (30, 60), (60, 120), (120, 180)]


def main():
    path = DATA_DIR / "markets.parquet"
    if not path.exists():
        print(f"Fant ikke {path} — kjor ingest forst (eller restore fra R2).")
        sys.exit(1)

    mk = pd.read_parquet(path)
    now = pd.Timestamp.now(tz="UTC")

    end = pd.to_datetime(mk["end_date"], errors="coerce", utc=True)
    evt = (pd.to_datetime(mk["event_start"], errors="coerce", utc=True)
           if "event_start" in mk.columns else pd.Series(pd.NaT, index=mk.index))
    mk = mk.assign(
        _end=end,
        _evt=evt,
        _settle_min=(end - evt).dt.total_seconds() / 60,
        _age_days=(now - end).dt.total_seconds() / 86400,
    )

    cand = mk[mk["_settle_min"].notna()
              & (mk["_settle_min"] >= 0)
              & (mk["_settle_min"] <= MAX_SETTLE_MIN)
              & (mk["volume"] >= MIN_VOLUME)
              & (mk["_age_days"] > 0)
              & (mk["token_ids"] != "[]")]
    print(f"{len(cand)} markeder kvalifiserer (oppgjorsvindu <= {MAX_SETTLE_MIN} min, "
          f"volum >= ${MIN_VOLUME:,})\n")

    print(f"{'alder (dogn)':>13} {'marked':<44} {'volum':>10} "
          f"{'fint':>6} {'sek':>6} {'grovt':>6}")
    print("-" * 92)

    summary = []
    for lo, hi in BUCKETS:
        sub = cand[(cand["_age_days"] >= lo) & (cand["_age_days"] < hi)]
        sub = sub.sort_values("volume", ascending=False).head(PER_BUCKET)
        fine_ok = coarse_ok = tested = 0

        for _, m in sub.iterrows():
            toks = json.loads(m["token_ids"])
            if not toks:
                continue
            tok = toks[0]
            evt_ts = int(m["_evt"].timestamp())
            end_ts = int(m["_end"].timestamp())
            window = (evt_ts - PAD_MIN * 60, end_ts + 300)

            fine = fetch_price_history(tok, 1, window=window)
            coarse = fetch_price_history(tok, 60)

            n_fine = 0 if fine is None or fine is ERROR else len(fine)
            n_coarse = 0 if coarse is None or coarse is ERROR else len(coarse)
            gap = ""
            if n_fine > 1:
                d = fine["timestamp"].sort_values().diff().dt.total_seconds()
                gap = f"{d.median():.0f}"

            tested += 1
            fine_ok += n_fine > 0
            coarse_ok += n_coarse > 0

            q = str(m["question"])[:42]
            mark = "ERR" if fine is ERROR else n_fine
            print(f"{m['_age_days']:>13.1f} {q:<44} {m['volume']:>10,.0f} "
                  f"{mark:>6} {gap:>6} {n_coarse:>6}")

        if tested:
            summary.append((lo, hi, tested, fine_ok, coarse_ok))
        print()

    print("=" * 92)
    print(f"{'alder':>12} {'testet':>8} {'fint OK':>9} {'grovt OK':>10}   tolkning")
    for lo, hi, tested, fine_ok, coarse_ok in summary:
        if fine_ok:
            verdict = "vinduet virker her"
        elif coarse_ok:
            verdict = "minuttdata borte, timesdata igjen"
        else:
            verdict = "markedet er ute av CLOB"
        print(f"{f'{lo}-{hi}d':>12} {tested:>8} {fine_ok:>9} {coarse_ok:>10}   {verdict}")

    print("\nGrensen mellom oeverste 'vinduet virker her' og raden under er "
          "hvor raskt innsamlingen maa vaere.")


if __name__ == "__main__":
    main()
