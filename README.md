# Hindsight — statusdokument
*Backtesting for Polymarket. Sist oppdatert 29. juli 2026.*

## Ved oppstart av ny chat
Last opp disse fem filene sammen: **denne filen**, `app.html`, `index.html`,
`ingest.py`, `ingest.yml`. Arbeidsfiler lagres ikke mellom samtaler, og de
inneholder mange små fikser som ikke kan gjenskapes fra hukommelsen.

## Hva produktet er
Nettside der brukeren tester handelsregler mot avsluttede Polymarket-markeder.
Alt regnes lokalt i nettleseren med DuckDB-WASM; data hentes fra Cloudflare R2.
Gratis i beta, Pro-nivå senere.

**Nøkkelfunn som definerer produktet:** Polymarkets CLOB-API sletter prishistorikk
for eldre markeder (0 datapunkter uansett oppløsning eller tidsvindu, bekreftet med
diagnose-script). Data som ikke samles inn løpende, er borte for godt. Det daglige
arkivet er derfor vollgraven — det kan ikke kopieres i etterkant.

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
1. Ingest henter markeder fra Gamma-API-et (måned-for-måned, splitter vinduet
   rekursivt ved offset-grensen på 2000) og prishistorikk fra CLOB-API-et.
2. Prisløkka dekker kun markeder avsluttet siste 180 dager (eldre finnes ikke),
   sortert etter volum så de verdifulle tas først.
3. Publiseringssteget slår sammen prisfiler per kategori, legger på epoch-kolonner
   (`ts_epoch`, `start_epoch`, `end_epoch`) og laster opp til R2.
4. Appen leser `manifest.json`, `markets.parquet` og `prices-<kategori>.parquet`.

## Appens felter
Category · Duration · Condition (≥/≤) · Threshold · Min. volume · Stake ·
Measure time to (last trade / oracle resolution) · Time to close · Min. market age ·
Entry cost (¢) · Fee on payout (%) · Skip decided markets (99/98/95¢ eller av) ·
Settlement (strict = eksakt 0/1, loose = innenfor 0,5¢)

## Appens visninger
- Stats: trades, win rate, avg return, P&L, max drawdown, best/worst trade,
  lengste W/L-rekke, avg hold, return per dag holdt, worst dip after entry, never dipped
- Equity-kurve (kronologisk)
- **Edge by entry price** — bånd med tap, 95 % Wilson-CI og signal/noise-merking
- **Threshold sweep** — 70–99¢ med usikkerhetsbånd
- **Stability** — første mot andre halvdel av arkivet, med dom i overskriften
- Kjørelogg i terminalstil
- Fallback: last inn lokale parquet-filer under «Advanced»

## Nøkkelinnsikt: hvordan fordel måles
Kjøper du til effektiv pris *p* (fyllpris + kostnad), er break-even-treffprosenten
nøyaktig *p*. **Fordel = treffprosent − effektiv inngangspris**, i prosentpoeng.
Wilson-intervall brukes fordi naiv standardfeil blir null når det ikke finnes tapere.

## Pågående undersøkelse: er krypto-fordelen ekte?
Krypto, 93¢, 6 t – 7 dager, kostnad 0,5¢: ~1900 trades, 98 % treff, ca. $2900,
max drawdown $676 (114 % av peak). Mønsteret som utløste undersøkelsen: null tap
i 583 trades over 97¢, der ~11 var forventet under antakelsen om effisiens.

Tre hypoteser testet og **eliminert**:
1. *Orakel-forsinkelse* — måling mot siste handel ga identiske tall (1925 → 1928).
2. *Sirkulært oppgjør* — strengt krav om eksakte 0/1-utfall endret ingenting.
3. *Allerede avgjorte markeder* — filteret fjernet bare 8 % av tradene, og
   76 % av posisjonene lå under vann på et tidspunkt (verste fall −14,1¢).

**Viktig metodisk korreksjon:** «1 av 60 000»-argumentet forutsetter at markedet er
effisient priset — men det er nettopp det som testes. Favoritt-outsider-skjevhet er
et veletablert funn i veddemålsmarkeder, så null tap kan være forventet hvis sann
treffrate på 97,5¢ er ~99,5 %.

**Gjenstår å teste:**
- Kostnadstest: kjør 0 / 0,5 / 1 / 2¢ og finn hvor fordelen dør (anslag: 1–1,5¢)
- Stabilitet: holder fordelen i begge halvdeler av arkivet?
- Klyngekorreksjon: krypto-utfall er korrelerte, så effektiv N er lavere enn antall trades

## Fallgruver som er løst (ikke gjenta)
- **GitHub-cache er upålitelig lagring** → R2 er kilden, hentes ned ved start.
- **Actions-minutter**: 2000/mnd på private repoer → repoet er offentlig.
- **Tidsbudsjett**: skriptet stopper pent etter 5t15m og lagrer alt.
- **Gamma offset-grense (2000)** → vinduer splittes rekursivt.
- **DuckDB-WASM mangler ICU** → ingen `date_diff` eller tidsstempel-subtraksjon;
  all tidsregning skjer på heltalls-epoch fra Python.
- **Serverfeil ≠ ingen historikk** → markeder som feiler med HTTP 5xx markeres ikke
  som avklart, men prøves på nytt neste kjøring (ellers stille datatap).
- **Minutt-fallback kun for markeder ≤5 timer** — ellers dobles API-kallene uten gevinst.
- **Norsk tastatur**: appen tolker nå både `0,5` og `0.5`.

## Gjenstår før lansering
1. Formspree-endepunkt inn i `index.html` (`SIGNUP_ENDPOINT`) for e-postfangst
2. Koble `hindsight.software` i Workers-prosjektet (navnetjener-bytte hos registraren)
3. Fjerne artifact-opplastingen fra `ingest.yml` (R2 er kilden; sparer lagring og
   hindrer at datasettet lastes ned fra et offentlig repo)
4. Første X-post: retention-funnet + sveip-grafen

## Senere
- Pro: privat R2-bøtte bak en Cloudflare Worker med lisenssjekk, betaling via
  Lemon Squeezy (merchant of record — håndterer EU-moms)
- Gratisnivå: siste 90 dager, dagsoppløsning, én kategori
- Kortlivede kryptomarkeder fanges fremover (volumgulv $200 for markeder ≤5 t)
- Mulig fortidsarkiv: on-chain handler via Polygon-subgraph (stor jobb, data forsvinner ikke)
