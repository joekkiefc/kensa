# Testplan — lokale Qwen als slab-lezer (naast, daarna i.p.v. Gemini)

Datum: 2026-09-08 · Eigenaar: Jowi · Opdracht: Tommy
Doel van het geheel: bewijzen dat de lokale Qwen VL (Bionic, PC `100.125.116.37:1234`) de
kaart-lezing exact goed doet — en pas daarna gecontroleerd laten overnemen.
De kaart-lezing kost nu: Gemini multimodaal ~2.500/dag + Cloud Vision vangnet ~186/dag.

## Spelregels (gelden voor elke fase)
1. Elke fase heeft een meetbaar slaag-criterium. Rapport in chat vóór de volgende fase.
2. Fail = stoppen → oorzaak → fix → **dezelfde fase opnieuw**. Nooit een fase overslaan.
3. De testset wordt bewaard (foto's lokaal op de Pi) → elke test is later exact herhaalbaar.
4. Definitie "exact": cert cijfer-voor-cijfer gelijk · grade gelijk · kaartnummer gelijk ·
   naam gelijk na normalisatie van hoofdletters/spaties (opmaak is geen inhoud). Geen half goed.
5. Een **verzonnen** antwoord is erger dan "kan ik niet lezen": gefantaseerd cert = directe fail.
6. Niets raakt de live pijplijn t/m fase 3 (alles read-only). Fase 4 alleen op expliciete go van Tommy.
7. Fase 0-3: bij groen ga ik zelf door (nul risico), mét rapport per fase.

## Laag-voor-laag bewijs (pre-flight — geen laag gebruiken vóór z'n bewijsmoment)
| # | Laag | Status | Bewijs / bewijsmoment |
|---|------|--------|------------------------|
| 1 | Pi → Bionic verbinding | ✅ bewezen 8-9 | HTTP 200 in 44 ms op `/v1/models` via tailscale serve |
| 2 | Foto's ophalen op de Pi | ✅ bewezen 8-9 | Buyee/Yahoo 295 KB opgehaald; UA-header nodig; resize-params worden genegeerd |
| 3 | Waarheid + Gemini-referentie uit Supabase | ✅ bewezen 8-9 | 4 testkaarten incl. slab_ocr result_json opgehaald |
| 4 | PSA-register nakijken | ✅ bewezen 8-9 | `scrape_psa_cert()` uit `agents/webshop/psa-slab.py` (scraper-tools venv) draaide live op certs 145628559/560/561 → naam/nummer/grade/jaar/set. ~5-7 s/cert. **Alleen de functie aanroepen, nooit `main()`** (die maakt Woo-producten aan). PSA onvolledig/laadt niet → kaart valt af |
| 5 | Qwen vision-call | ⏳ fase 0 | Request-formaat al door server geaccepteerd (nette fouten, geen formaat-fouten); wacht op geladen VL-model |
| 6 | Vergelijker ("scheidsrechter") | ⏳ vóór fase 2 | **Test-de-test:** waarheid-tegen-zichzelf = 100%, expres-corrupte antwoorden = alles afgekeurd. Pas dan telt een score |
| 7 | Schaduwscript | ⏳ vóór fase 3 | 1 uur proefdraai (smoke-run) vóór het 48-72u-venster; sjabloon `dev/shadow_test_easyocr.py` |
| 8 | Vangnet/stekker-gedrag | ⏳ fase 3 | Stekker-test: PC ~1 uur uit → live pijplijn merkt niks |
| 9 | Flip-schakelaar + rollback | ⏳ fase 4 | Zelfde schakelaar-patroon als de Supabase-cutover (bewezen patroon), 1 regel terug |

Veld-vergelijkregels (liggen vast vóór fase 2, geen schuivende doelpalen):
- **cert**: cijfer-voor-cijfer stringgelijk.
- **grade**: PSA "GEM MT 10" → `10`; numeriek gelijk (`9.5` == `9.5`).
- **nummer**: PSA toont soms `205`, label soms `205/172` → regel per kaart vastgelegd in het
  antwoordenboekje (deel vóór de slash moet altijd matchen; volledige vorm als PSA die heeft).
- **naam**: gelijk na hoofdletters/spaties/accents-normalisatie (PSA schrijft ALL-CAPS).

## Fase 0 — Verbinding & oog
- [x] Pi bereikt Bionic-endpoint via Tailscale (bewezen: HTTP 200 in 44 ms)
- [x] Qwen2.5-VL-7B staat in de modellenlijst (`qwen2.5-vl-7b-instruct`, 8-9 21:40)
- [x] Proefplaatjes gelezen: 3/3 certs cijfer-voor-cijfer exact op volle resolutie (8-9)
**GOAL:** het VL-model draait en kan aantoonbaar een plaatje zien.
**GESLAAGD ALS:** beide vinkjes ja.

## Fase 1 — Antwoordenboekje (gouden set)
**GOAL:** 35 foto's met 100% zekere waarheid — onafhankelijk van Gemini.
**HOE:**
- 30 slab-kaarten uit de eigen stroom; elk cert **geverifieerd bij het officiële PSA-register**
  (eigen opzoekscript `agents/webshop/psa-slab.py::scrape_psa_cert`, scraper-tools venv).
  PSA's naam/nummer/grade = de waarheid, niet wat Gemini ervan vond.
- Mix: Mercari (m*) én Buyee/Yahoo (z*), grades 10/9/lager, en ~5 bewust lastige foto's
  (schuin, reflectie, label klein in beeld).
- PLUS 5 **valstrik-foto's** zonder leesbaar PSA-label (losse kaart, multi-lot, wazig):
  het juiste antwoord dáár is "geen label leesbaar". Dit test tegen fantaseren.
**GESLAAGD ALS:** vast setbestand bestaat: 30 rijen (foto + PSA-waarheid) + 5 valstrikken,
foto's lokaal opgeslagen, herdraaibaar.
**✅ GESLAAGD — 2026-09-09 01:08.** `dev/qwen_goldenset/answers.json` status `bevroren`:
30 kaarten PSA-geverifieerd (13 Mercari, 17 Buyee/Yahoo; grades 8/9/10) + 5 geschouwde
valstrikken, alle foto's lokaal in `dev/qwen_goldenset/photos/`. Afwijking (al gelogd 8-9):
de "moeilijk"-subset verviel — 0 bruikbare vision_fallback-certs; lastige gevallen komen in
fase 3 via de echte stroom.

## Fase 2 — Backwards test: exacte replica (kerneis)
**GOAL:** Qwen geeft van elke foto exact de bekende waarheid terug.
**HOE:** elke foto → Qwen → `{cert, grade, card_name, number}` → vergelijken met PSA-waarheid
(vergelijkingsregels: zie spelregel 4). Gemini's uitslag op dezelfde kaarten ernaast als
referentie, plus leestijd per kaart.
**VOORWAARDE:** laag 6 groen — de vergelijker heeft de test-de-test doorstaan.
**GESLAAGD ALS:**
- cert **30/30** exact · grade **30/30** · nummer **30/30** · naam **30/30** (na normalisatie)
- valstrikken **5/5** "geen label" — **nul** verzonnen certs
- Eén herkansing toegestaan na prompt-/beeldinstelling-fix; daarna nog fail → stoppen + verslag.
**UITKOMST:** tabel per kaart: waarheid | Qwen | Gemini | ✓/✗ | tijd.

## Fase 3 — Schaduwdraai op de verse stroom
**GOAL:** bewijzen dat het ook klopt op de rommelige dagstroom, op volume, zonder live iets te raken.
**HOE:** 48-72 uur draait op de Pi een schaduwscript: elke nieuwe slab-lezing van de pijplijn
gaat óók door Qwen; resultaat naar een apart logbestand (read-only t.o.v. live). Dagelijkse
samenvatting; van de verschillen een handmatige steekproef: **wie had er gelijk** (Qwen kan
ook béter zijn dan Gemini — verschillen zijn niet automatisch Qwen-fouten).
**PLUS de stekker-test:** PC bewust ~1 uur uit → de live pijplijn mag er niets van merken.
**GESLAAGD ALS:**
- ≥1.000 kaarten vergeleken
- cert-overeenstemming ≥98% op leesbare kaarten, en de verschillen-steekproef valt niet
  structureel uit in Gemini's voordeel
- 0 verstoringen in de live pijplijn · stekker-test doorstaan
- gemiddelde leestijd per kaart bekend en past ruim binnen de worker-rondes.

## Fase 4 — Proef-overname met vangnet (ALLEEN op go van Tommy)
**GOAL:** Qwen wordt de eerste lezer in live; de betaalde route wordt het vangnet. 1 regel terug te draaien.
**HOE:** route-schakelaar: Qwen eerst → antwoord onvolledig/fout/PC onbereikbaar → automatisch
Gemini/Vision zoals nu. Rollback = schakelaar terug.
**GESLAAGD ALS (na 48 uur):**
- kensa_bewaker groen, dashboard-kwaliteit onveranderd
- vangnet-gebruik <10%
- Google-tellers zichtbaar omlaag: Gemini ~4.000 → ~1.500/dag (tekststappen blijven), Vision → ~0.

## Fase 5 — Later, apart besluit
Tekst-stappen (titel/omschrijving/ROI/oordeel = rest van de ~€79/mnd) via dezelfde trechter:
backwards test → schaduw → flip. Pas bespreken ná een geslaagde fase 4; PC-uptime wordt dan belangrijker.

## Status-log (nieuwste bovenaan)
- 2026-09-09 16:45: **FASE 4 LIVE — Qwen is de slab-lezer (GO Tommy 16:35: "gewoon overstappen").**
  Tommy's koers: 48u-schaduw vervalt; Gemini ALLEEN als Qwen niet bereikbaar is; onvolledige
  lezing → Vision-vangnet zoals altijd. Gebouwd: `qwen_lezer.py` (full-res foto, PROMPT_FIXED
  nu in productie, QwenOnbereikbaar, stroomonderbreker 120 s, cert-sanity: 'PSA'/'DOPA!'/
  77777777/komma-lijsten → geen cert), `lezing_opschonen.py` (= uitkomst5-trechter: pokémon-woord
  + prefix, JP→EN vangnet, échte set-code als nummer-suffix), `check_slab_hybrid.py` schakelaar
  `KENSA_SLAB_LEZER=qwen` (_source qwen / gemini_fallback / vision_fallback), enqueue geeft
  label_name als accept-only set-hint door, card_key zonder dubbele set-code. Tests 159 groen.
  Smoke 3 kaarten + onbereikbaar-smoke ok. Flip 16:44:54 (commit 0d34e3c). Eerste uur live:
  52 lezingen, 100 % Qwen, vangnet 0 %, 7 rommel-certs terecht afgekeurd, Qwen 7,4 s gem /
  12 s p95 per foto (6 parallel). Schaduw-cron uit; PSA-cron loopt de URL-verschillen af tot
  ±17:45 en gaat dan weg. Dagrapport 20:45 = `dev/qwen_live_rapport.py`.
  **ROLLBACK: regel `export KENSA_SLAB_LEZER=qwen` uit `cron_worker_ocr.sh` → volgende run Gemini.**
  Uitkomsten vandaag die de flip droegen: test-60 (48 met PSA) URL 31-31, eBay 46-43 Qwen,
  nummer 48-47 Qwen, cert 48-47 Gemini, 1 verkeerde URL Qwen (Pikachu V Start Deck 100) /
  0 Gemini; schaduw URL-verschillen tegen PSA (39 gecheckt): Qwen 12 goed / 0 fout, Gemini 0
  goed / 2 FOUT (Steelix, Articuno: geen set → oude set-URL) / 10 gemist.
- 2026-09-09 19:40: **FASE 3b GESTART (Tommy: 'die 1000 zijn met de oude opdracht, achterhaald').**
  Schaduw draait nu de deploy-config: PROMPT_FIXED (fix 2+3, gedeeld via dev/qwen_prompt_fixed.py),
  full-res, geen titel, en per kaart de Cardmarket-uitkomst via set-check + JP→EN-vangnet voor
  Qwen én Gemini (`cm` in het log; hoofdlat in --rapport). Fase-3a-log gearchiveerd
  (shadow_log_fase3a_oude_opdracht.jsonl). Vandaag LIVE in productie: harde set-check in
  cm_lookup (2359a46 + vervolg), subtype-woorden geen set-bewijs, soft_hint accept-only.
  Steekproeven vandaag: gouden set 27-26, test-60 (26 PSA) CM 15-15 / eBay 25-23 / nummer 26-25,
  Pikachu-30 set-bevestigd Qwen 16 vs Gemini 13, 0 verkeerde URLs. Tommy's oordeel: Qwen is nu
  beter. Fase 3b = 48u volume-bewijs → dan fase 4 (Qwen lezer, Gemini vangnet) op Tommy's go.
- 2026-09-09 12:35: **MEETLAT AANGESCHERPT DOOR TOMMY (scope-correctie).** Het doel van de
  lezing is de exacte KAART identificeren → juiste Cardmarket-vergelijking/URL. Het cert is
  een extra check, géén business-identiteit (code bevestigt: `card_key = pokemon:nummer:
  grade[:set]`; 0× cert in bevoorrader/queue-flow; cert voedt alleen cert_sightings).
  **Hoofdlat eindrapport = kaart-identificatie** (key-kern-overeenstemming + wie er bij
  verschillen gelijk had), cert-lat wordt tweede meting (datakwaliteit). Eerlijke stand op
  de hoofdlat vandaag: Gemini wint (naamkwestie, oorzaak+fix bekend); op de cert-lat wint
  Qwen (scheidsrechter 8-0). Consequentie: faalt fase 3 op de hoofdlat met de bekende
  naam-oorzaak → **fase 3b**: kort schaduwvenster mét naam-fix (label-only + woordenboek)
  vóór enige flip. Geen flip zonder herbewijs op de hoofdlat.
- 2026-09-09 10:30: **Absolute-waarheid-arm toegevoegd (opdracht Tommy: "vergelijk ook met de
  absolute waarheid").** `dev/qwen_shadow_psa_check.py`: PSA-register als scheidsrechter over
  (1) alle cert-verschillen (beide kanten), (2) qwen-extra-certs, (3) dagelijkse controle-
  steekproef op agree-rijen (vangt "allebei fout"). Budget ≤10 lookups/run, 15-20s pauze
  (PSA-429-les fase 1). Draait mee in het 20:45-dagrapport.
  **Eerste ronde (8 lookups): PSA bevestigt Qwen op 8/8 cert-verschillen** — alle acht
  Qwen-certs bestaan én passen exact (naam+nummer+grade) bij de kaart; allemaal Shops-
  listings waar Gemini een ander (niet op het label staand) cert produceerde. Gemini's
  kant van deze verschillen wordt in de volgende rondes gecheckt (bestaat-niet vs andere-kaart).
  Cron-schaduw na 40 min: 135 kaarten vergeleken, 3 runs, 0 fouten.
- 2026-09-09 10:20: **FASE 3 GESTART (go Tommy).** `dev/qwen_shadow.py` + cron elke 10 min
  (`dev/cron_qwen_shadow.sh`, flock). Ontwerp — appels-met-appels met live:
  * ZELFDE contract: `PHOTO_INTERPRET_PROMPT` geïmporteerd uit `llm_client` + identieke
    user-msg met titel EN/JP; zelfde bronfoto (`source_photo_idx`); temperature 0.
  * BEWUSTE afwijking: Qwen krijgt volle resolutie (live verkleint naar 768px puur om
    Gemini-tokens; lokale GPU heeft die kosten niet → we meten de vervangings-configuratie).
    Foto-upgrade: mercdn `thumb→item/detail/orig` (810×1080) · Shops `-/small→-/large`
    (tot 952×1600) — empirisch bevestigd; upgrade-status per rij gelogd.
  * Vergelijking: cert/grade/nummer/naam (fase-2-normalisatie) + business-sleutel-kern
    (`pokemon:nummer:grade`, want live-keys missen structureel de set_code) via
    `analyze._build_card_key` over Qwens velden vs `listings.card_key`.
  * Read-only op live; PC uit → run stopt zonder checkpoint-opschuif ('stekker'-gedrag);
    achterstand >24u = `missed_offline` (geen backfill). Checkpoint gestart 09:35 UTC.
  * Bewaking: 1u-proefdraai-oordeel (laag 7) via automation 11:25 · dagrapport 20:45 mét
    PSA-steekproef op cert-verschillen (max ~8/dag, rustig tempo).
  **Smoke (8 kaarten) — grote vondst:** op Mercari-Shops-listings staat in de fototabel
  alléén een 143px-thumbnail → live Gemini **verzint daar certs én kaartnummers**
  (3 van 3 door Jowi op de foto geverifieerd: label 161320999/997/912 — Qwen alle drie
  cijfer-perfect; Gemini 61559994/58155061/81129912 = niet op het label, en nummers die
  de verkoperstitel tegenspreken). Qwens bekende zwakte blijft naam-vertaling
  (サンダース→"Sandshrew" i.p.v. Jolteon) — precies wat de key-kern-vergelijking vangt.
  Buyee/Mercari full-res in de smoke: alle velden correct. Qwen 1,49 s/kaart gemiddeld.
- 2026-09-09 07:50: **Fase 2 eindstand na 2e herkansing (Tommy's go).** V3 (regel-per-veld) was een ontwerpfout van Jowi — veld-hussel + voorbeeld-lek ("339/SM-P" uit de prompt gekopieerd), 6/30, teruggedraaid. **V4** (= bewezen V2 + apart `other_label_text`-laatje, lek-voorbeeld verwijderd): **25/30 alles-exact · cert 29/30 · grade 30/30 · nummer 29/30 · naam 25/30 · valstrikken 5/5 · 0 verzonnen · 1,24 s/kaart.**
  Ontleding van de 5 restfouten: 1× oneerlijke input (z679038666 = foto met TWEE slabs; Qwen las de buurslab — Sylveon 105862384 #068 — foutloos; kaart uitgesloten per set-kwaliteitsregel) → effectief **26/29, cert 29/29, grade 29/29, nummer 29/29, naam 25/29**. Blijven over: **4 échte naamfouten** (PKACHU mist I; SYLVN→SYLVAN + variety-regel aangeplakt; 1× set-regel als naam gekozen; 1× kennis-substitutie LANA'S AID→ASSISTANCE).
  **Formeel oordeel: fase 2 NIET gehaald** (lat = 100% op alle velden). Feitelijk: cert/grade/nummer 100% op eerlijke foto's, en Qwen ving 2 échte cert-fouten van de betaalde pijplijn. Naam = 86%. Beslisvraag bij Tommy: door naar fase 3 met naam-lat als head-to-head Qwen vs Gemini op ≥1000 echte kaarten (cert-lat blijft heilig), of stoppen.
- 2026-09-09 07:25: **FASE 2 na herkansing: GEFAALD op de letter (13/30 alles-exact) — STOP conform plan, beslissing bij Tommy.** Maar de ontleding verandert het beeld:
  - Valstrikken **5/5**, **0 verzonnen certs** (prompt V2 fixte hallucinatie — ook ARS-slab en AXCI-screenshot correct "no_label").
  - **Answer-key-corruptie ontdekt (2×):** pipeline (Gemini) had certs verhaspeld (88587580 i.p.v. 161081580; 15663398 mist een 8) en die foute nummers bestonden bij PSA als honkbal 2001/hockey 1972. **Qwen las beide kaarten GOED — per PSA-register bevestigd** (161081580=PIKACHU ex #234 g10; 156633988=DARK ARBOK-HOLO #24 g9). → Gecorrigeerd scorebeeld: **cert 30/30, number 30/30, grade 29/30** (1 notatie: label "NM-MT" → 8), naam blijft zwak.
  - **Naam 14/30**, vrijwel volledig **segmentatie**: Qwen plakt de variety/set-regel achter de naam ("... SPECIAL ART RARE", "... EEVEE HEROES") — geen leesfout, verkeerde regel-afbakening. Fix-idee: aparte JSON-velden voor variety/set (bucket), + grade-tekstmapping (PSA-schaal NM-MT=8 etc.), + 2 corrupte antwoordrijen herstellen.
  - **Echte leesfouten: 4** (FA/PKACHU mist I; SYLVAN vs SYLVN; FRM-punt weg; "LANA'S ASSISTANCE" = eigen kennis i.p.v. lezen).
  - Snelheid op GPU: **~1,4 s/kaart gemiddeld** (vol beeld) — sneller dan Gemini's ~1,7 s.
- 2026-09-09 07:16: Fase 2 run 1: GEFAALD 12/30 — hoofdzaak: 18 setfoto's bleken 240px-duimnagels (set-kwaliteitsfout in de bouw) + 2 verzonnen certs op valstrikken. Set gerepareerd (alle foto's naar volle resolutie: Mercari orig / Shops large), 2 nachtelijk toegevoegde valstrikken alsnog geschouwd (beide geldig), prompt V2 verhard (letterlijk kopiëren, andere graders = no_label). Run 1 gearchiveerd als backtest_results_run1.json.
- 2026-09-09 ~07:10: Laag 6 bewezen: scheidsrechter-selftest GESLAAGD (35 waarheid-checks + 7 corruptie-checks gevangen).
- 2026-09-09 ~03:20: **GPU-mysterie opgelost:** 47 s kwam doordat het 27B-model (mijn testcall) de VRAM bezette → VL naar CPU. Na nachtelijke unload: **1,9 s/kaart vol beeld** op de 4070 Ti Super. Geen instelling nodig; les: nooit 27B via dit endpoint aanroepen naast de VL.
- 2026-09-08: plan opgesteld. Fase 0 half: verbinding bewezen; wacht op geladen VL-model.
- 2026-09-08 ~21:45: **Fase 0 GESLAAGD op nauwkeurigheid** — qwen2.5-vl-7b-instruct in lijst; 3/3 certs exact op volle resolutie (Pikachu 150469346, Det. Pikachu 158103948, Ho-Oh 74174316); JSON-formaat netjes gevolgd.
- OPEN PUNT snelheid: vol beeld ~47 s/kaart (ook warm), 1024px ~32 s, 768px ~12 s. MAAR kleiner beeld kost precisie: 768px cert onleesbaar; 1024px plakte Ho-Oh een verzonnen "/172" aan het nummer (strenge test ving dit — bewijs dat exact-matchen werkt). Prefill ~40 tok/s ruikt naar CPU i.p.v. GPU → Tommy checkt GPU-offload in Bionic (Loaded Instances). Rode-label-crop (OpenCV) faalde op testfoto 1 → geen betrouwbare snelweg.
- 2026-09-08 ~21:55: Fase 1 build gestart: dev/qwen_goldenset_build.py — 30 kaarten PSA-geverifieerd + 5 valstrikken (found_psa=false), foto's naar dev/qwen_goldenset/photos/, antwoorden naar answers.json. Valstrikken worden vóór bevriezing handmatig geschouwd (view_image).
- 2026-09-08 23:20: Fase 1 tussenstand — **10/30 kaarten PSA-geverifieerd**; PSA remt af na ~10 vlotte opvragingen (429) → script hervat-baar gemaakt (chunks GS_MAX_PSA/GS_PAUZE), nachtchunks via automation elke ~45 min (eerste 00:05). **Valstrikken 5/5 gedownload én geschouwd door Jowi**: allemaal écht zonder leesbaar PSA-label (2 app-screenshots losse kaarten, sealed promo, toploader, centering-app-screenshot met "PSA 10"-schátting in beeld = extra gemene fantasie-test). Afwijking: vision_fallback-kandidaten leverden 0 bruikbare certs → geplande "moeilijk"-subset vervalt in fase 2; moeilijke gevallen komen in fase 3 massaal langs via de echte stroom.
- 2026-09-09 01:08: **Fase 1 GESLAAGD** — laatste nachtchunk (8 PSA-pogingen) bracht de set op **30/30 kaarten PSA-geverifieerd** + 5/5 geschouwde valstrikken. `answers.json` bevroren (status `bevroren`, 62 foto's lokaal). Samenstelling: 13 Mercari + 17 Buyee/Yahoo, mix grades 8/9/10, 0 "moeilijk" (zie afwijking hierboven). **Fase 2 wacht** op Tommy's PC/Bionic aan + GPU-offload-check (laag 6 test-de-test gaat eraan vooraf).
