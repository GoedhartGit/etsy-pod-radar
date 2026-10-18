# Etsy POD Radar

Een bot die elke ochtend de best verkopende print-on-demand (POD) shops op Etsy volgt, meet wat er verkoopt, en daar design briefs van maakt voor een designbot.

```
06:17  GitHub Actions  ──►  Etsy API  ──►  database + rapport + feed (branch "data")
07:56  Claude-taak     ──►  leest feed + afbeeldingen  ──►  e-mail met advies
                                                       └──►  Google Sheet "Design Briefs"  ──►  designbot
```

## Wat de bot meet

| Signaal | Hoe | Betrouwbaarheid |
|---|---|---|
| Verkopen per shop per dag | Dagelijkse stijging van `transaction_sold_count` | Gemeten, exact (vanaf dag 2) |
| Welke listings verkopen | Nieuwe reviews per listing, omgerekend met de reviewratio van de shop | Schatting |
| Momentum per listing | Groei in favorieten en views, favorieten per dag sinds publicatie, zoekpositie | Gemeten (vanaf dag 2) |
| Thema's die werken | Woorden en tags die bij de toppers vaker voorkomen dan gemiddeld ("lift") | Vanaf dag 1 |
| Stijgende thema's | Favorietengroei deze week vs vorige week | Vanaf dag 14 |
| Productmix, prijzen, personalisatie, SEO | Verdeling bij de toppers vs alle listings | Vanaf dag 1 |

POD-shops worden automatisch gevonden via ~45 zoekwoorden (plus seizoenswoorden) en alleen gevolgd als ze minstens 500 verkopen hebben en minstens de helft van hun listings POD is (productiepartner zoals Printify, of kenmerken als Comfort Colors/Gildan/made to order). Listings met merk- of fandomnamen (Disney, Taylor Swift, NFL, enz.) worden wel geteld maar nooit als design-voorbeeld gebruikt.

---

## Installatie (eenmalig, ±30 minuten + wachten op Etsy)

### Stap 1. Etsy API-key aanvragen

1. Log in op Etsy (een gewoon koperaccount is genoeg) en ga naar https://www.etsy.com/developers/register
2. Vul in:
   - **Name**: `POD Radar Yorick`
   - **Description**: `Personal research tool that reads public listing and shop data to analyse print-on-demand trends.`
   - **Who will be the users?**: *Just myself or colleagues*
   - **Is your application commercial?**: *No*
3. Na het aanmaken staat de app onder https://www.etsy.com/developers/your-apps. Daar vind je de **Keystring** en het **Shared secret**.
4. Goedkeuring door Etsy kan een paar dagen duren. Zolang de status "pending" is, faalt de bot met een 403-melding; dat is normaal.

### Stap 2. GitHub-repository maken

1. Maak een gratis account op https://github.com/signup (onthoud je gebruikersnaam).
2. Klik rechtsboven op **+** → **New repository**.
   - Repository name: `etsy-pod-radar`
   - Kies **Public** (nodig zodat de Claude-taak de feed kan lezen; je sleutels staan nooit in de repo, die gaan in Secrets)
   - Vink niets aan bij "Initialize". Klik **Create repository**.
3. Klik op de link **uploading an existing file**.
4. Pak `etsy-pod-radar.zip` uit. Open de map in Finder en druk **Cmd + Shift + .** zodat de verborgen map `.github` zichtbaar wordt.
5. Selecteer **alles in de map** (Cmd + A, inclusief `.github`) en sleep het naar de uploadpagina. Klik **Commit changes**.
6. Controleer dat je in de repo de map `.github/workflows` met `daily.yml` ziet. Ontbreekt die: klik **Add file → Create new file**, typ als naam `.github/workflows/daily.yml`, plak de inhoud van dat bestand uit de zip en klik **Commit changes**.

### Stap 3. Etsy-sleutel als geheim opslaan

1. In je repo: **Settings → Secrets and variables → Actions → New repository secret**
2. Name: `ETSY_API_KEY`
3. Secret: je keystring, dubbele punt, shared secret, zonder spaties. Voorbeeld: `abc123def456:xyz789`
4. Klik **Add secret**.

### Stap 4. Eerste run

1. Ga naar het tabblad **Actions**. Klik eventueel op **I understand my workflows, go ahead and enable them**.
2. Klik links op **Etsy POD Radar** → **Run workflow** → **Run workflow**.
3. Na 3 tot 6 minuten staat er een groen vinkje. Er is nu een branch `data` met het rapport: open `reports/latest.md` op die branch.
4. Daarna draait de bot elke dag vanzelf om 06:17 (zomertijd) / 05:17 (wintertijd). Faalt een run, dan stuurt GitHub je een e-mail.

### Stap 5. Claude-adviestaak aanzetten

Stuur Claude je GitHub-gebruikersnaam. Claude zet dan een dagelijkse geplande taak aan (07:56) die de feed leest, de best lopende designs bekijkt, je een e-mail met advies stuurt en de briefs in de Google Sheet zet. De instructies van die taak staan in `claude/dagelijks-advies-prompt.md`.

---

## Wat je elke dag krijgt

- **E-mail** met kernpunten, top-5 shops, designpatronen, 3 design-opdrachten en IP-waarschuwingen
- **Google Sheet "Etsy POD Radar – Design Briefs"**: per brief een rij met status `nieuw`, concept, stijl, slogans, kleuren, tags en prijs
- **Rapport** `reports/latest.md` op de `data`-branch met alle tabellen

## Feed voor de designbot

De designbot kan twee bronnen gebruiken:

1. **Google Sheet** (aanbevolen): lees rijen met Status = `nieuw`, zet ze op `in productie` en na publicatie op `gepubliceerd`. Werkt ook met Make.com ("Google Sheets → Watch Rows").
2. **JSON-feed**: `https://raw.githubusercontent.com/<gebruikersnaam>/etsy-pod-radar/data/feed/latest.json`

Belangrijkste velden in `latest.json`:

| Veld | Inhoud |
|---|---|
| `briefs[]` | `brief_id`, `product_type`, `theme`, `related_terms`, `recommended_tags` (13), `price_usd` (p25/median/p75), `personalization_share`, `confidence` (0-1), `evidence`, `example_listings[]` met `thumb` |
| `top_listings[]` | 50 best lopende listings zonder IP-risico, met `momentum` (0-100), `est_sales_30d`, `fav_per_day_recent`, `price_usd`, `tags`, `thumb` |
| `new_winners[]` | Toppers van maximaal 30 dagen oud |
| `trending_terms[]` / `rising_terms[]` | Thema's met `lift` en week-op-week `growth` |
| `product_mix[]` | Aandeel per producttype bij toppers vs totaal, met prijsband |
| `top_shops[]` | Shops met `sales_per_day`, `sales_7d`, `est_revenue_per_day_usd`, `new_listings_7d`, `top_listings` |
| `contact_sheets` | Afbeeldingen met de top-24 designs (12 per vel) |
| `phase` | `opbouw` (eerste 7 dagen) of `stabiel` |

## Instellingen aanpassen

Open `config.yaml` in GitHub en klik op het potloodje. Je kunt:

- zoekwoorden toevoegen of weghalen (`seed_keywords`, `seasonal_keywords`)
- specifieke shops altijd laten volgen (`extra_shops: [ShopNaam1, ShopNaam2]`)
- de IP-blocklist uitbreiden (`ip_blocklist`)
- limieten aanpassen (aantal shops, listings, drempels)

## Eerlijke beperkingen

- Etsy geeft geen verkopen per listing. De bot meet verkopen per shop exact en schat per listing via reviews (ongeveer 10-20% van de kopers schrijft een review).
- De "Bestseller"-badge en Etsy-advertenties zijn niet zichtbaar via de API.
- De eerste 7 dagen is de bot in opbouwfase: groeicijfers worden dan steeds betrouwbaarder. Week-op-week-trends verschijnen vanaf dag 14.
- Gebruik de briefs als richting, niet als kopieerlijst: maak altijd eigen designs. Namaken van bestaande designs of gebruik van merken is een inbreuk en kan je Etsy-shop laten sluiten.

## Problemen oplossen

| Melding | Oplossing |
|---|---|
| `Etsy weigert de API-key (403)` | App nog niet goedgekeurd, of het secret heeft niet het formaat `keystring:sharedsecret`. |
| `ETSY_API_KEY ontbreekt` | Secret niet aangemaakt of verkeerde naam (moet exact `ETSY_API_KEY` zijn). |
| `Etsy-quotum bijna op` | Verlaag `max_tracked_shops` of het aantal zoekwoorden in `config.yaml`. |
| Workflow draait niet meer | GitHub pauzeert schema's na 60 dagen zonder activiteit in publieke repo's. Klik in Actions op **Enable workflow**. |

## Lokaal testen (voor ontwikkelaars)

```
pip install -r requirements.txt
python tests/run_simulation.py 16      # 16 dagen tegen een nep-Etsy-API
python tests/test_edge_cases.py        # foute key, quotum, verwijderde shop
```
