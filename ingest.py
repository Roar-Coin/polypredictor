# .github/workflows/ingest.yml — v4
# R2 er kilden: datasettet hentes ned ved start og lastes opp ved slutt.
# GitHub-cachen brukes kun som ekstra backup.

name: polymarket-ingest

on:

  workflow_dispatch:
    inputs:
      fidelity:
        description: "Opplosning i minutter (60 = time)"
        default: "60"
      category:
        description: "Kategori (tom = alle)"
        default: ""
      min_volume:
        description: "Min. volum i USD"
        default: "250"
      zoom_min_volume:
        description: "Volumgulv for finkornet vindus-kall"
        default: "250"
      zoom_max_minutes:
        description: "Maks oppgjorsvindu som gir finkornet kall (240 = tar med 4-timers)"
        default: "240"
      since_days:
        description: "Hent markeder avsluttet siste N dager (0 = alle)"
        default: "180"
      zoom_budget:
        description: "Sekunder til vindus-etterfylling (5400 = 1t30m, 10800 = 3t)"
        default: "5400"
      reset_zoom:
        description: "Nullstill vindus-sjekkpunktet forst (true/false)"
        default: "false"

concurrency:
  group: polymarket-ingest
  cancel-in-progress: false

env:
  R2_ACCOUNT_ID: ${{ secrets.R2_ACCOUNT_ID }}
  AWS_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}
  AWS_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}
  AWS_REQUEST_CHECKSUM_CALCULATION: when_required
  AWS_RESPONSE_CHECKSUM_VALIDATION: when_required

jobs:
  ingest:
    runs-on: ubuntu-latest
    timeout-minutes: 350
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install deps
        run: pip install --quiet requests pandas pyarrow awscli

      - name: Restore from cache (backup)
        uses: actions/cache/restore@v4
        with:
          path: data
          key: poly-data-${{ github.run_id }}
          restore-keys: poly-data-

      - name: Restore dataset from R2
        run: |
          if [ -z "$R2_ACCOUNT_ID" ]; then
            echo "R2-secrets ikke satt — hopper over."; exit 0
          fi
          mkdir -p data
          aws s3 sync "s3://hindsight-data/raw/" data/ \
            --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com" || true
          echo "Filer i data/prices etter R2-restore:"
          find data/prices -name '*.parquet' 2>/dev/null | wc -l

      # MAA ligge etter begge restore-stegene: cachen henter ellers tilbake fila
      # vi nettopp slettet. Ingest skriver en fersk zoomed.json ved slutten, og
      # publiseringssteget overskriver R2-kopien — ingen manuell rydding i R2.
      - name: Nullstill vindus-sjekkpunkt
        if: ${{ inputs.reset_zoom == 'true' }}
        run: |
          if [ -f data/zoomed.json ]; then
            python -c "import json;print('  inneholdt',len(json.load(open('data/zoomed.json'))),'markeder')"
            rm -f data/zoomed.json
            echo "zoomed.json slettet — alle kvalifiserte markeder proves paa nytt."
          else
            echo "zoomed.json fantes ikke — ingenting aa nullstille."
          fi

      - name: Run ingestion
        run: |
          ARGS="--fidelity ${{ inputs.fidelity || '60' }} --min-volume ${{ inputs.min_volume || '1000' }}"
          ARGS="$ARGS --since-days ${{ inputs.since_days || '180' }}"
          ARGS="$ARGS --zoom-budget-seconds ${{ inputs.zoom_budget || '5400' }}"
          ARGS="$ARGS --zoom-min-volume ${{ inputs.zoom_min_volume || '250' }}"
          ARGS="$ARGS --zoom-max-minutes ${{ inputs.zoom_max_minutes || '240' }}"
          if [ -n "${{ inputs.category }}" ]; then ARGS="$ARGS --category ${{ inputs.category }}"; fi
          python ingest.py $ARGS

      - name: Publish to R2
        if: always()
        run: |
          if [ -z "$R2_ACCOUNT_ID" ]; then
            echo "R2-secrets ikke satt — hopper over publisering."; exit 0
          fi
          EP="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
          # 1) raadata + checkpoint (kilden for neste kjoring)
          aws s3 sync data/ "s3://hindsight-data/raw/" --endpoint-url "$EP" --exclude "publish/*"
          # 2) ferdig pakket datasett som nettsiden leser
          if [ -d data/publish ]; then
            aws s3 sync data/publish "s3://hindsight-data/" --endpoint-url "$EP"
            echo "Publisert til R2."
          fi

      - name: Save cache (backup)
        if: always()
        uses: actions/cache/save@v4
        with:
          path: data
          key: poly-data-${{ github.run_id }}
