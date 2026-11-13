# .github/workflows/bookrecorder.yml
# Tar opp ordreboken for aktive markeder i alle kategorier.
#
# Kjorer i skift paa ~4t50m. Cron hver sjette time gir sammenhengende dekning
# med margin — og til forskjell fra ingest er hull her UGJENKALLELIGE, siden
# Polymarket ikke eksponerer historisk bokdybde.
#
# VOLUMGULVET: 250, ikke 5000. Med $5000 og 24 timers horisont ga utvalget
# 49 markeder i august og bare 3-5 i november — nesten bare sport og politikk,
# aldri vaer eller krypto. Backtestene kjorer paa $250, saa gulvet her maa
# matche dem, ellers tar vi opp boken for markeder vi ikke tester.
#
# TAKET paa 150 henger sammen med gulvet: hvert marked koster to HTTP-kall per
# runde, og med 5 markeder tok en runde saa kort tid at sleepen padet den til
# 60 sekunder (290 runder paa 17400s). Rundt 150 markeder er ~300 kall, altsaa
# like i overkant av et minutt. Blir rundetallet i loggen mye lavere enn
# run_seconds/60, har takten sklidd og taket maa ned.
#
# Opplasting skjer ETTER hvert skift, men filene skrives per opptak, saa en
# drept jobb mister bare det siste kvarteret. `if: always()` sikrer at det som
# ble tatt opp kommer opp selv om jobben avbrytes.

name: bookrecorder

on:
  schedule:
    - cron: "3 0,6,12,18 * * *"
  workflow_dispatch:
    inputs:
      min_volume:
        description: "Volumgulv i USD for aa tas opp (250 = samme som backtestene)"
        default: "250"
      horizon_hours:
        description: "Ta opp markeder som gjores opp innen N timer"
        default: "24"
      interval_seconds:
        description: "Sekunder mellom opptak"
        default: "60"
      max_markets:
        description: "Tak paa antall markeder per syklus (150 holder 60s-takten)"
        default: "150"
      run_seconds:
        description: "Lengde paa skiftet (17400 = 4t50m)"
        default: "17400"

concurrency:
  group: hindsight-bookrecorder
  cancel-in-progress: false

env:
  R2_ACCOUNT_ID: ${{ secrets.R2_ACCOUNT_ID }}
  AWS_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}
  AWS_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}
  AWS_REQUEST_CHECKSUM_CALCULATION: when_required
  AWS_RESPONSE_CHECKSUM_VALIDATION: when_required

jobs:
  record:
    runs-on: ubuntu-latest
    timeout-minutes: 330
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install deps
        run: pip install --quiet requests pandas pyarrow awscli

      - name: Ta opp
        run: |
          python bookrecorder.py \
            --min-volume ${{ inputs.min_volume || '250' }} \
            --horizon-hours ${{ inputs.horizon_hours || '24' }} \
            --interval-seconds ${{ inputs.interval_seconds || '60' }} \
            --max-markets ${{ inputs.max_markets || '150' }} \
            --run-seconds ${{ inputs.run_seconds || '17400' }}

      - name: Last opp til R2
        if: always()
        run: |
          if [ -z "$R2_ACCOUNT_ID" ]; then
            echo "R2-secrets ikke satt — opptaket gaar tapt."; exit 1
          fi
          EP="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
          echo "Filer aa laste opp: $(find data/books -name '*.parquet' | wc -l)"
          du -sh data/books || true
          aws s3 sync data/books "s3://hindsight-data/books/" --endpoint-url "$EP"
          echo "Opptak publisert til R2."
