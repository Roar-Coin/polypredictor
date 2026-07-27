# Hindsight 
Backtesting for Polymarket — samler prishistorikk som API-et sletter

## Hva produktet er
Nettside der brukeren tester handelsregler mot avsluttede Polymarket-markeder.
Alt regnes lokalt i nettleseren med DuckDB-WASM; data hentes fra Cloudflare R2.
Gratis i beta, Pro-nivå senere.

**Nøkkelfunn som definerer produktet:** Polymarkets CLOB-API sletter prishistorikk
for eldre markeder (0 datapunkter uansett oppløsning eller tidsvindu). Data som ikke
samles inn løpende, er borte for godt. Derfor er det daglige innsamlingsarkivet
selve vollgraven — det kan ikke kopieres i etterkant.

## Infrastruktur
| Del | Hvor |
|---|---|
| Kode / ingest | GitHub: `Roar-Coin/polypredictor` (offentlig, for gratis Actions-minutter) |
| Datainnsamling | GitHub Actions `ingest.yml`, nattlig cron 04:17 UTC + manuell kjøring |
| Lagring | Cloudflare R2, bøtte `hindsight-data` (rådata under `raw/`, publisert datasett i rot) |
| Offentlig data-URL | `https://pub-fa09cf81888743609fadfcf28e65ea77.r2.dev` |
| Nettside | Cloudflare Workers (static upload), `hindsight.roar-martinussen.workers.dev` |
| Domene | `hindsight.software` — kjøpt, ikke koblet ennå |
| GitHub secrets | `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` |

## Dataflyt
1. Ingest henter markeder fra Gamma-API-et (måned-for-måned, splitter vinduet automatisk
   ved offset-grensen på 2000) og prishistorikk fra CLOB-API-et.
2. Prisløkka dekker kun markeder avsluttet siste 180 dager (eldre finnes ikke), nyeste først.
3. Publiseringssteget slår sammen prisfiler per kategori, legger på epoch-kolonner
   (`ts_epoch`, `start_epoch`, `end_epoch`) og laster opp til R2.
4. Appen leser `manifest.json`, `markets.parquet` og `prices-<kategori>.parquet` direkte.

## Ved oppstart av ny chat
Last opp `app.html`, `index.html` og `ingest.py` sammen med denne filen —
arbeidsfilene lagres ikke mellom samtaler, og de inneholder mange små fikser.

## Appens funksjoner
- Regler: kategori, varighet, betingelse (≥/≤), terskel, min. volum, innsats,
  tid til resolution, min. markedsalder, inngangskostnad (¢), gebyr (%)
- Resultater: trades, treffprosent, snittavkastning, P&L, max drawdown,
  beste/verste trade, lengste vinner/taper-rekke
- Equity-kurve (kronologisk), båndtabell «fordel etter inngangspris» med
  tapsantall og standardfeil, terskel-sveip 70–99¢ med usikkerhetsbånd
- Kapitaleffektivitet: gjennomsnittlig holdetid og avkastning per dag holdt
  (per-dag-tallet forutsetter kontinuerlig gjenbruk av kapital — sammenligningsmål, ikke prognose)
- Usikkerhet: 95 % Wilson-intervall på all fordel, og «signal»/«noise»-merking
  per rad. Wilson brukes fordi naiv standardfeil blir null når det ikke finnes tapere.
- Kjørelogg i terminalstil, sier fra når ingen terskel skiller seg fra null
- Mobiltilpasset (felt i to kolonner, 16px skrift i inputfelt, kolonner skjules på smal skjerm)
- Fallback: last inn lokale parquet-filer under «Advanced»

## Nøkkelinnsikt: hvordan fordel måles
Kjøper du til effektiv pris *p* (fyllpris + kostnad), er break-even-treffprosenten
nøyaktig *p*. **Fordel = treffprosent − effektiv inngangspris**, i prosentpoeng.

Målinger på sport (1704 trades, 27. juli 2026, kostnad 0,5¢, alle tidsvinduer):
- 70¢: +1,0 ± 0,8 pp · 92–93¢: +0,7 til +0,8 pp · over 97¢: negativ
- Kostnad på 0,5¢ halverte fordelen sammenlignet med rådata
- **Max drawdown $2592 mot P&L $2159** — strategien tapte mer enn den tjente underveis
- Snitt holdetid 15 dager → 0,043 % per dag. Kapitalbindingen er avgjørende.
- Med «final hour»-filter: 217 trades, alle fordeler innenfor konfidensintervallet
  fra null — arkivet er ennå for ungt til å svare.

Statistiske forbehold: tersklene er overlappende utvalg, ikke uavhengige tester.
Sport i denne perioden domineres av VM 2026, så mange utfall er korrelerte —
effektiv utvalgsstørrelse er lavere enn antall trades.

## Fallgruver som er løst (ikke gjenta)
- **GitHub-cache er upålitelig lagring** → R2 er kilden, hentes ned ved start.
- **Actions-minutter**: 2000/mnd på private repoer → repoet er offentlig.
- **Tidsbudsjett**: scriptet stopper pent etter 5t15m og lagrer alt.
- **Gamma offset-grense (2000)** → vinduer splittes rekursivt.
- **DuckDB-WASM mangler ICU** → ingen `date_diff` eller tidsstempel-subtraksjon;
  all tidsregning skjer på heltalls-epoch fra Python.
- **Norsk tastatur**: skriv `0.5`, ikke `0,5`, i tallfelt — komma leses som 0.

## Gjenstår før lansering
1. Formspree-endepunkt inn i `index.html` (`SIGNUP_ENDPOINT`) for e-postfangst
2. Koble `hindsight.software` i Workers-prosjektet (navnetjener-bytte hos registraren)
3. Første X-post: retention-funnet + sveip-grafen som bilde

## Senere
- Pro: privat R2-bøtte bak en Cloudflare Worker med lisenssjekk, betaling via
  Lemon Squeezy (merchant of record — håndterer EU-moms)
- Gratisnivå: siste 90 dager, dagsoppløsning, én kategori
- Kortlivede kryptomarkeder (5m/15m/1h) fanges fremover: volumfilteret slipper
  gjennom markeder ≤5 timer ned til $50
- Mulig fortidsarkiv: on-chain handler via Polygon-subgraph (stor jobb, data forsvinner ikke)
