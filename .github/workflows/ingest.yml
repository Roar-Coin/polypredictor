# .github/workflows/ingest.yml — v2
# Lagrer cache + artifact UANSETT utfall, saa fremdrift aldri gaar tapt.

name: polymarket-ingest

on:
  schedule:
    - cron: "17 4 * * *"   # hver natt 04:17 UTC — fanger data foer den prunes
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
        default: "1000"

jobs:
  ingest:
    runs-on: ubuntu-latest
    timeout-minutes: 350
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Restore checkpoint + data
        uses: actions/cache/restore@v4
        with:
          path: data
          key: poly-data-${{ github.run_id }}
          restore-keys: poly-data-

      - name: Install deps
        run: pip install requests pandas pyarrow

      - name: Run ingestion
        run: |
          ARGS="--fidelity ${{ inputs.fidelity || '60' }} --min-volume ${{ inputs.min_volume || '1000' }}"
          if [ -n "${{ inputs.category }}" ]; then ARGS="$ARGS --category ${{ inputs.category }}"; fi
          python ingest.py $ARGS

      - name: Save checkpoint + data
        if: always()
        uses: actions/cache/save@v4
        with:
          path: data
          key: poly-data-${{ github.run_id }}

      - name: Publish to R2
        if: always()
        env:
          R2_ACCOUNT_ID: ${{ secrets.R2_ACCOUNT_ID }}
          AWS_ACCESS_KEY_ID: ${{ secrets.R2_ACCESS_KEY_ID }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.R2_SECRET_ACCESS_KEY }}
          AWS_REQUEST_CHECKSUM_CALCULATION: when_required
          AWS_RESPONSE_CHECKSUM_VALIDATION: when_required
        run: |
          if [ -z "$R2_ACCOUNT_ID" ]; then
            echo "R2-secrets ikke satt — hopper over publisering."; exit 0
          fi
          if [ ! -d data/publish ]; then
            echo "Ingen publish-mappe denne kjoringen."; exit 0
          fi
          pip install --quiet awscli
          aws s3 sync data/publish "s3://hindsight-data/" \
            --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
          echo "Publisert til R2."

      - name: Upload data as artifact
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: polymarket-data
          path: data/
          retention-days: 30
