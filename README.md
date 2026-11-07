# Data-branch van Etsy POD Radar

Deze branch wordt elke dag automatisch overschreven door de workflow. Niet handmatig bewerken.

- `feed/latest.json` — machineleesbare feed voor de designbot (briefs, toppers, thema's, prijzen)
- `feed/contact_sheet_1.jpg`, `_2.jpg` — contactvellen met de 24 best lopende designs (nummer = positie in top_listings)
- `feed/thumbs/` — thumbnails van toppers (worden dagelijks vervangen)
- `feed/briefs/<datum>/` — afbeeldingen van brief-voorbeelden (14 dagen bewaard)
- `feed/history/` — dagelijkse feeds (zonder volledige top-50)
- `reports/latest.md` — leesbaar dagrapport (Nederlands)
- `db/radar.sqlite.gz` — alle meetgegevens (SQLite, gzip)
