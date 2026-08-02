# Hindsight — statusdokument
*Backtesting for Polymarket. Sist oppdatert 2. august 2026.*

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
| Datainnsamling | GitHub Actions `ingest.yml` — **kun `workflow_dispatch`**, ingen `schedule` |
| Nattlig utløser | **Cloudflare Worker `hindsight-cron`**, cron 04:17 UTC, kaller GitHub-API-et |
| Lagring | Cloudflare R2, bøtte `hindsight-data` (rådata under `raw/`, publisert datasett i rot) |
| Offentlig data-URL | `https://pub-fa09cf81888743609fadfcf28e65ea77.r2.dev` |
| Nettside | Cloudflare Workers (static upload), `hindsight.roar-martinussen.workers.dev` |
| Domene | `hindsight.software` — kjøpt, ikke koblet ennå |
| GitHub secrets | `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` |
| Worker-hemmeligheter | `GH_TOKEN` (fine-grained PAT, Actions read+write), valgfritt `ALERT_WEBHOOK`, `TRIGGER_SECRET` |

**Hvorfor cron ligger i Cloudflare:** GitHubs planlagte kjøringer lå konsekvent
2–3 timer bak skjema (04:17 planlagt → 06:39–07:50 faktisk) og kan droppes helt ved
høy last. Worker-en fyrer punktlig, hopper over dispatch hvis forrige kjøring går,
og varsler hvis siste vellykkede ingest er eldre enn 26 timer. Den holder også
repoet aktivt — `schedule`-utløsere deaktiveres etter 60 dagers inaktivitet, men
`workflow_dispatch` gjør det aldri.

**PAT-en utløper.** Da svarer dispatch 401 og innsamlingen stopper stille.
`ALERT_WEBHOOK` er den halvparten av Worker-en som faktisk beskytter arkivet.

## Dataflyt
1. Ingest henter markeder fra Gamma-API-et (måned-for-måned, nyeste først, splitter
   vinduet rekursivt ved offset-grensen på 2000) og prishistorikk fra CLOB-API-et.
2. Prisløkka dekker kun markeder avsluttet siste 180 dager (eldre finnes ikke),
   sortert etter volum så de verdifulle tas først.
3. Publiseringssteget slår sammen prisfiler per kategori, legger på epoch-kolonner
   (`ts_epoch`, `start_epoch`, `end_epoch`) og laster opp til R2.
4. Appen leser `manifest.json`, `markets.parquet` og `prices-<kategori>.parquet`.

## Kategorisering — les dette før du rører `fetch_window`
**`include_tag=true` er den ene linjen hele taksonomien hviler på.** Gamma
returnerer *ingen* tags på market-nivå uten den. Uten parameteren er `TAG_MAP` død
kode, alt faller til nøkkelordslista, og 72 % av datasettet havner i «other» —
uten en eneste feilmelding. Dette var tilstanden fram til 1. august 2026.

**`PRIORITY` avgjør kategorien, ikke rekkefølgen Polymarket sender taggene i.**
Et CS2-marked er tagget både `sports` og `esports`; med «første treff vinner» ville
det havnet i sport sammen med Wimbledon.

Ni kategorier etter fiksen (802 763 markeder, mot 687 952 før):

| Kategori | Antall | Andel |
|---|---|---|
| sports | 378 713 | 47,2 % |
| crypto | 243 402 | 30,3 % |
| esports | 59 620 | 7,4 % |
| other | 47 311 | 5,9 % |
| weather | 40 967 | 5,1 % |
| politics | 14 904 | 1,9 % |
| stocks | 9 232 | 1,2 % |
| economy | 4 404 | 0,5 % |
| culture | 4 210 | 0,5 % |

Resten i «other» er stort sett markeder eldre enn backfill-vinduet på 300 dager;
de har uansett ingen prishistorikk i CLOB.

## Appens felter
Category · Duration · Condition (≥/≤) · Threshold · Min. volume · Stake ·
Measure time to (last trade / oracle resolution) · Time to close · Min. market age ·
Entry cost (¢) · Fee on payout (%) · Skip decided markets (99/98/95¢ eller av) ·
Settlement (strict = eksakt 0/1, loose = innenfor 0,5¢)

## Appens visninger
- Stats: trades, win rate, avg return, P&L, max drawdown, best/worst trade,
  lengste W/L-rekke, avg hold, return per dag holdt, worst dip after entry, never dipped
- Equity-kurve (kronologisk)
- **Edge by entry price** — bånd med tap, 95 % Wilson-CI og signal/noise-merking.
  Båndene under 50¢ er delt fint (0–1, 1–2, 2–5, 5–10, 10–20, 20–35, 35–50¢) fordi
  ett samlet 0–50¢-bånd skjuler at noen få lodd under 5¢ kan bære hele snittet.
- **Threshold sweep** — 70–99¢ med usikkerhetsbånd
- **Cost sweep** — 0–3¢, viser hvor fordelen dør (rått, naivt CI, klynget CI)
- **Stability** — første mot andre halvdel av arkivet, med dom i overskriften
- **Cluster correction** — effektiv N med ρ̂ fra ANOVA og et gulv der hver klynge
  teller som ett veddemål
- Kjørelogg i terminalstil
- Fallback: last inn lokale parquet-filer under «Advanced»

## Nøkkelinnsikt: hvordan fordel måles
Kjøper du til effektiv pris *p* (fyllpris + kostnad), er break-even-treffprosenten
nøyaktig *p*. **Fordel = treffprosent − effektiv inngangspris**, i prosentpoeng.
Wilson-intervall brukes fordi naiv standardfeil blir null når det ikke finnes tapere.

To ting som må holdes adskilt:
- **Gevinst er en egenskap ved utfallet, ikke ved P&L.** Definerer man gevinst som
  `avkastning > 0`, faller «treffprosenten» når kostnaden stiger, og fordelen
  straffes to ganger for det samme. Kostnadssveipet blir da meningsløst.
- **Taket er 100¢, ikke 99,9¢.** Du kan aldri betale mer enn $1 for et krav som
  betaler $1. Med tak på 99,9¢ ble en garantert taper til en liten gevinst så snart
  kostnaden dyttet prisen over 100¢.

**Break-even-kostnaden i cent er lik nullkostnads-fordelen i prosentpoeng.**
Fordelen faller eksakt ett prosentpoeng per cent, så kostnadssveipet visualiserer
en lineær sammenheng — det oppdager ikke noe nytt, men gjør marginen konkret.

## Krypto: fordelen står ennå
Krypto, 93¢, 6 t – 7 dager, $1000 volum, 0,5¢ kostnad, strict settlement:

| | Før kategorifiksen | Etter |
|---|---|---|
| Handler | 1843 | 2229 |
| Treffprosent | 97,9 % | 98,2 % |
| Fordel | +1,6 ± 0,7 pp | +1,9 ± 0,6 pp |
| Dør ved (klynget) | 1,36¢ | **1,71¢** |
| P&L | $3138 | $4388 |

Fem hypoteser testet og **eliminert**:
1. *Orakel-forsinkelse* — måling mot siste handel ga identiske tall (1925 → 1928).
2. *Sirkulært oppgjør* — strengt krav om eksakte 0/1-utfall endret ingenting.
3. *Allerede avgjorte markeder* — filteret fjernet bare 8 % av tradene.
4. *For høy kostnad* — fordelen dør først ved 1,71¢; klaringen er 1,21¢.
5. *Skjevt myntutvalg* — da universet vokste 53 % (BNB og småmynter kom med),
   **økte** fordelen. Den var altså ikke en artefakt av nøkkelordslista.

**Gjenstår:**
- Faktisk fyllkostnad i ordreboken på 93–97¢-markeder med $1000–5000 volum.
  Dette er det eneste som avgjør om fordelen er ekte penger. Over 1,71¢ er den død.
- Klyngegulvet feiler fortsatt, men det måler **arkivets lengde**, ikke strategien:
  36 klynger over 37 dager krever ~6,9 pp for å klare null. Ved 180 dager kreves
  ~3 pp, ved 365 dager ~2,1 pp. Løser seg av seg selv hvis innsamlingen fortsetter.
- Andre halvdel gir +2,4 pp mot første halvdels +1,3. Kan være støy, kan være regime.

## Sport: undersøkelsen er lukket
Tre kandidater, tre forklaringer — alle av samme type: **et filter som bruker
informasjon du ikke har på forhånd.**

| Oppsett | Kjennbar på forhånd? | Resultat |
|---|---|---|
| final hour + last trade, $1000 | **nei** | +3,6 pp, +$33 146 |
| final hour + oracle, $1000 | ja | −2,3 %, −$29 849 |
| any, volumkrav 0, ≥70¢ | ja | −0,4 %, −$11 075 (29 280 handler) |
| any, volumkrav 0, ≤30¢ | ja | −5,3 %, −$165 653 (31 240 handler) |

1. **«Final hour» målt mot siste handel har framtidsinnsyn.** Du kan ikke vite
   klokken 19:00 at siste handel kommer 20:00 — det ser du først etterpå.
   *Oracle resolution* (`endDate`) er publisert metadata og er den legitime
   referansen. I krypto spiller valget ingen rolle (gapet er minutter); i sport er
   gapet dager, og da avgjør det alt.
2. **Volumfilteret har samme feil.** Sluttvolum er ikke kjent ved inngang, og det
   er ikke uavhengig av utfallet: markeder der outsideren gjør det bra tiltrekker
   seg oppmerksomhet og volum. Et krav på $20 000 så ut til å gi outsidere +3,1 pp
   og favoritter −5,2 % samtidig — matematisk umulig for to sider av samme binære
   marked. Med volumkrav 0 taper begge sider, slik de må.
3. **Grunnlinjen er lærebokresultatet.** Outsidere −5,3 %, favoritter −0,4 %:
   klassisk favoritt-outsider-skjevhet. Ikke handelbar — å utnytte at outsidere er
   for dyre betyr å kjøpe favoritten, og den er allerede priset til fair.

**Konklusjon:** terskelregler på pris alene virker ikke i sport. Det er *ikke* det
samme som at sport er uinteressant — se «Ubesvart» under.

## Ubesvart, i prioritert rekkefølge
1. **`gameStartTime`** finnes på sportsmarkeder i Gamma og er publisert på forhånd.
   Det gjør «kjøp 30 minutter etter avspark» til en implementerbar regel — den
   legitime versjonen av det «final hour» forsøkte. Krever feltet inn i ingest og et
   «measure time from game start»-valg i appen.
2. **Undernivå-filter.** 378 713 sportsmarkeder spenner fra NBA-finaler til ITF i
   Kiseljak. At snittet er effisient sier ingenting om delene, men appen mangler
   tekst- eller tag-filter, så spørsmålet kan ikke stilles.
3. **Vær og esports** er aldri testet. 40 967 og 59 620 markeder.

## Fallgruver som er løst (ikke gjenta)
- **`include_tag=true`** — se eget avsnitt. Feiler stille.
- **GitHub-cache er upålitelig lagring** → R2 er kilden, hentes ned ved start.
- **GitHub-cron er 2–3 timer forsinket** → Cloudflare Worker utløser i stedet.
- **`meta_ok` sjekket bare at kolonnen `tags` fantes** — den fantes hele tiden, full
  av tomme lister. Nå måles faktisk innhold; under 50 % fylte tags utløser backfill.
- **Full metadata-innhenting rekker ikke innenfor Actions-grensen** (5t50m hardt).
  Backfill er derfor avgrenset til 300 dager, nyeste måned først, med lagring etter
  hver måned og halvt tidsbudsjett.
- **`aws s3 sync` uten `--delete` fjerner ikke gamle stier** → 50 822 duplikater
  etter omkategorisering. Ryddet med `cleanup_r2_categories.py` (tørrkjøring først;
  sletter kun når riktig plassert kopi finnes med samme størrelse).
- **Actions-minutter**: 2000/mnd på private repoer → repoet er offentlig.
- **Tidsbudsjett**: prisløkka stopper pent etter 5t15m og lagrer alt. Merk at
  sjekken *kun* ligger i prisløkka.
- **Gamma offset-grense (2000)** → vinduer splittes rekursivt. Enkelte døgn har over
  2000 markeder og kan ikke deles finere — noen markeder faller stille ut. Uløst.
- **DuckDB-WASM mangler ICU** → ingen `date_diff` eller tidsstempel-subtraksjon;
  all tidsregning skjer på heltalls-epoch fra Python.
- **Serverfeil ≠ ingen historikk** → markeder som feiler med HTTP 5xx markeres ikke
  som avklart, men prøves på nytt neste kjøring (ellers stille datatap).
- **Minutt-fallback kun for markeder ≤5 timer** — ellers dobles API-kallene uten gevinst.
- **Norsk tastatur**: appen tolker nå både `0,5` og `0.5`.
- **`\b` matcher ikke foran kolon** — nøkkelordet «spread:» traff aldri.

## Gjenstår før lansering
1. Formspree-endepunkt inn i `index.html` (`SIGNUP_ENDPOINT`) for e-postfangst
2. Koble `hindsight.software` i Workers-prosjektet (navnetjener-bytte hos registraren)
3. Sjekk at appens kategorimeny leser `manifest.json` dynamisk (skal vise ni valg)
4. Første X-post: retention-funnet + sveip-grafen

**Innholdsidé med god dekning:** «Hvorfor de fleste backtester lyver» — de tre
sportskandidatene er et rent, konkret eksempel på framtidsinnsyn i filtre, og
appen er verktøyet som avslørte det. Sterkere salgsargument enn en pen kurve.

## Senere
- Pro: privat R2-bøtte bak en Cloudflare Worker med lisenssjekk, betaling via
  Lemon Squeezy (merchant of record — håndterer EU-moms)
- Gratisnivå: siste 90 dager, dagsoppløsning, én kategori
- Kortlivede kryptomarkeder fanges fremover (volumgulv $200 for markeder ≤5 t)
- Mulig fortidsarkiv: on-chain handler via Polygon-subgraph (stor jobb, data forsvinner ikke)
- Ukentlig nullstilling av `nohistory` for markeder avsluttet siste 7 dager, som
  sikkerhetsventil mot tilfeldige tomme API-svar
