# Hindsight — statusdokument
*Backtesting for Polymarket. Sist oppdatert 2. august 2026 (kveld).*

## Ved oppstart av ny chat
Last opp disse fem filene sammen: **denne filen**, `app.html`, `index.html`,
`ingest.py`, `ingest.yml`. Arbeidsfiler lagres ikke mellom samtaler, og de
inneholder mange små fikser som ikke kan gjenskapes fra hukommelsen.

**Last alltid opp den versjonen som faktisk kjører.** Filene endres mellom
samtaler, og en patch bygget på en gammel kopi sletter arbeid stille.

### Der vi står akkurat nå (2. aug, kveld)
En ingest-kjøring er i gang med den nye `ingest.py` (event_start + vindus-kall).
Det første som skal sjekkes i neste samtale er loggen fra den:
- **«Metadata mangler kolonnen event_start — henter paa nytt»** skal stå tidlig.
  Gjør den ikke det, ble feil versjon lagt inn.
- **«herav N med oppgjorsvindu <= 60 min og volum >= $5,000»** — dette tallet
  avgjør om retroaktiv henting er en kort ekstrarunde eller flere fulle kjøringer.
- **«X finkornede vinduer»** i progresjonslinjene. Ventes å være lavt denne
  kjøringen: sjekkpunktet gjør at markeder som allerede er hentet hoppes over,
  så vindus-kallet treffer bare nye markeder. Retroaktiv henting krever at de
  aktuelle markedene fjernes fra `checkpoint.json` — en egen beslutning.

**Rett etter kjøringen:** `app.html` må patches. Varighetsfilteret bruker
fortsatt `(end_epoch - start_epoch)/60` og skal bruke
`COALESCE(event_epoch, start_epoch)`. Uten det finner «≈ 5 min» fortsatt
ingenting, selv om dataene nå er riktige.

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
| X-konto | `@hindsightsw` — egen produktkonto, lenket fra nav og bunntekst |
| E-postfangst | Formspree `https://formspree.io/f/xojgpbvv`, testet og virker |
| Worker-hemmeligheter | `GH_TOKEN` (fine-grained PAT, Actions read+write), valgfritt `ALERT_WEBHOOK`, `TRIGGER_SECRET` |

**Hvorfor cron ligger i Cloudflare:** GitHubs planlagte kjøringer lå konsekvent
2–3 timer bak skjema (04:17 planlagt → 06:39–07:50 faktisk) og kan droppes helt ved
høy last. Worker-en fyrer punktlig, hopper over dispatch hvis forrige kjøring går,
og varsler hvis siste vellykkede ingest er eldre enn 26 timer. Den holder også
repoet aktivt — `schedule`-utløsere deaktiveres etter 60 dagers inaktivitet, men
`workflow_dispatch` gjør det aldri.

**PAT-en utløper.** Da svarer dispatch 401 og innsamlingen stopper stille.
`ALERT_WEBHOOK` er den halvparten av Worker-en som faktisk beskytter arkivet.

## Filer i repoet
| Fil | Hva |
|---|---|
| `ingest.py` | innhenting, kategorisering, publisering |
| `.github/workflows/ingest.yml` | kun `workflow_dispatch` + `concurrency` |
| `.github/workflows/diagnose-categories.yml` | engangsprober, byttes ut etter behov |
| `.github/workflows/cleanup-r2.yml` | rydder duplikater etter omkategorisering |
| `cleanup_r2_categories.py` | tørrkjøring først, `--apply` for å slette |
| `diagnose_categories.py` | hvorfor havner markeder i «other» |
| `probe_short.py` | finnes korte markeder, og har de volum |
| `probe_window.py` | hvor ligger det ekte handelsvinduet |
| `probe_interval.py` | hvilke CLOB-parametere gir fin oppløsning |
| Cloudflare Worker `hindsight-cron` | `worker.js` + `wrangler.toml`, egen deploy |

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
  teller som ett veddemål. **Bruk «subject + dag», ikke bare «dag».** Subject er
  mynt for krypto, by for vær, kamp for sport/esport. Med ren dagsnøkkel måler
  gulvet arkivets lengde, ikke strategien.
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

**Strukturell risiko:** fordelen hviler på få uavhengige hendelser. 2229 handler er
bare **154 mynt-dager** — det finnes rundt seksten mynter i universet, og på en dag
der BTC faller, feiler alle BTC-markedene sammen. Diversifiseringen er dårligere
enn handelstallet antyder.

**Gjenstår:**
- Faktisk fyllkostnad i ordreboken på 93–97¢-markeder med $1000–5000 volum.
  Dette er det eneste som avgjør om fordelen er ekte penger. Over 1,71¢ er den død,
  uansett hvor mye arkiv som samles. **Høyeste prioritet — venter ikke på noe.**
- Klyngegulvet med subject + dag: 154 klynger, ±3,0 pp mot +1,9 pp fordel. Feiler,
  men langt nærmere enn med ren dagsnøkkel (36 klynger, ±6,9 pp).
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

## Vær: den eneste lovende nye kandidaten
Vær, 88¢, volumkrav 0, 0,5¢ kostnad, oracle resolution: 13 762 handler, 94,9 % treff,
+0,9 pp, $13 755.

**Det som taler for:**
- Terskelsveipet har **pukkel, ikke helling** — −1,4 pp ved 70¢, topp +1,3 ved 80¢,
  −1,5 ved 99¢. Sport falt monotont (effisient); krypto har samme pukkelform.
  Nesten hele P&L sitter i 80–93¢.
- Summeringstesten er ren: ≥70¢ gir +$15 840, ≤30¢ gir −$692 105. Ingen umulig
  dobbeltgevinst slik volumfilteret ga i sport.
- Utfallet avgjøres av en målestasjon, ikke av orakelskjønn. Ingen tvistrisiko.
- **Flest uavhengige hendelser av alle kategoriene**: 1362 by-dager mot kryptos 154.

**Det som taler mot:**
- Stabilitetstesten svikter — bare andre halvdel klarer null.
- Fordelen dør ved ~1,4¢. Klaringen er ~0,9¢, mot kryptos 1,21¢.
- Gulvet klarer ikke null: ±1,2 pp mot +0,9 pp fordel.
- ρ̂ = 0,00 selv med by-nøkkel. Overraskende — burde «36 °C i Taipei» og «37 °C i
  Taipei» ikke falle sammen? Kan bety at markedene er «minst X» heller enn «eksakt
  X», eller at 94,9 % treff gir estimatoren for få tapere å måle på.

## Begge kandidatene venter på arkivlengde
Verken krypto eller vær er avkreftet — de er ikke bevist. Begge feiler klyngegulvet,
og det løses av tid, ikke av flere sveip. Flere kjøringer nå øker bare sjansen for å
finne noe tilfeldig.

| | Klynger nå | Kreves | Estimert tid |
|---|---|---|---|
| Krypto (+1,9 pp, p=98,2 %) | 154 | ~240 | ~3 uker |
| Vær (+0,9 pp, p=94,9 %) | 1362 | ~2400 | ~4 uker |

Dette er den beste illustrasjonen av hvorfor det daglige arkivet er vollgraven:
spørsmålet svarer seg selv hvis innsamlingen bare fortsetter.

## Korte kryptomarkeder: hva som faktisk er sant
Undersøkt 2. august med tre prober. Konklusjonene er ikke intuitive:

**«5-minutters» markeder er 24-timersmarkeder.** «Bitcoin Up or Down – July 19,
6:15PM-6:20PM ET» har `eventStartTime` 22:15Z og `endDate` 22:20Z — de fem
minuttene er *oppgjørsvinduet*. Boken åpner et døgn før, og første prispunkt
ligger på 50,5¢ dagen i forveien. `startDate` er opprettelsestidspunktet, så
varighet regnet derfra blir 1430 minutter i stedet for 5.

**De har alltid ligget i arkivet.** BTC-markedene har $120 000–140 000 i volum,
langt over ethvert gulv. De var bare umulige å finne, fordi appens
varighetsfilter la dem i «> 1 dag».

**Bare BTC handles.** Av seks samtidige 7:50–7:55-markeder: Bitcoin $34 762,
Dogecoin $349, BNB $159, og Ethereum, Solana og XRP null.

**Den reelle begrensningen var aldri fidelity.** CLOB returnerer ~144 punkter
uansett `fidelity` når `interval=max`. Et døgnlangt marked får derfor ti
minutters oppløsning, og de avgjørende minuttene glattes bort. Målt:

| Variant | Punkter | Sek mellom |
|---|---|---|
| `interval=max`, fidelity=1 | 145 | 600 |
| `interval=1h` eller `6h` | 0 | — |
| `startTs`/`endTs` | 32 | **60** |
| `start_ts`/`end_ts` | HTTP 400 | — |

`interval` og `startTs` kan ikke kombineres. Løsningen er smalere vindu, ikke
høyere fidelity. 60 sekunder er gulvet CLOB serverer.

**Hva dette åpner:** en helt ny regelklasse — noe som skjer i minuttene rundt
oppgjør, i markeder med sekssifret volum og en bok som har stått åpen i et døgn.
Kapasitetsproblemet som begrenser kryptofordelen i $1000–5000-markedene ser
fundamentalt annerledes ut her.

## Ubesvart, i prioritert rekkefølge
1. **`gameStartTime`** finnes på sportsmarkeder i Gamma og er publisert på forhånd.
   Det gjør «kjøp 30 minutter etter avspark» til en implementerbar regel — den
   legitime versjonen av det «final hour» forsøkte. Krever feltet inn i ingest og et
   «measure time from game start»-valg i appen.
2. **Undernivå-filter.** 378 713 sportsmarkeder spenner fra NBA-finaler til ITF i
   Kiseljak. At snittet er effisient sier ingenting om delene, men appen mangler
   tekst- eller tag-filter, så spørsmålet kan ikke stilles.
3. **Esports** er aldri testet. 59 620 markeder. Merk at mange er `Map Handicap` og
   `Games Total` på obskure kamper — sportens ITF-hale uten toppsjiktet. Vær ekstra
   streng med stabilitetstesten; esports-scener endrer seg raskt.

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
1. ~~Formspree~~ — koblet og testet 2. august
2. ~~X-konto og lenker på siden~~ — `@hindsightsw` opprettet, ikon i nav og bunntekst
3. ~~Tall på forsiden~~ — rettet til «120 000+», ni kategorier
4. **Koble `hindsight.software`** i Workers-prosjektet (navnetjener-bytte hos
   registraren). Siste tekniske hinder før første post.
5. `app.html`: varighetsfilter til `COALESCE(event_epoch, start_epoch)`
6. Første X-post. Utkast klart (se under). Post tirsdag–torsdag 14–16 norsk tid,
   som treffer amerikansk formiddag. **Ikke før domenet er koblet.**

**Festet innlegg, klart til bruk:**

> A sports rule I tested showed +$33,146 over 7,423 trades.
>
> Then I changed one setting — from "last trade" to "oracle resolution."
>
> Same rule. Same markets. −$11,075.
>
> The filter used information I couldn't have had at entry time. That's why I
> built Hindsight.

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
