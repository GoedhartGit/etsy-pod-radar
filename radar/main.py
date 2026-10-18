"""Entry point: python -m radar.main --data-dir out"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from .analyze import Analyzer
from .collect import Collector
from .etsy import EtsyClient, EtsyError, build_api_key
from .report import contact_sheet, download_thumbs, write_feed, write_report
from .store import Store

log = logging.getLogger("radar")

DATA_README = """# Data-branch van Etsy POD Radar

Deze branch wordt elke dag automatisch overschreven door de workflow. Niet handmatig bewerken.

- `feed/latest.json` — machineleesbare feed voor de designbot (briefs, toppers, thema's, prijzen)
- `feed/contact_sheet_1.jpg`, `_2.jpg` — contactvellen met de 24 best lopende designs (nummer = positie in top_listings)
- `feed/thumbs/` — thumbnails van toppers (worden dagelijks vervangen)
- `feed/briefs/<datum>/` — afbeeldingen van brief-voorbeelden (14 dagen bewaard)
- `feed/history/` — dagelijkse feeds (zonder volledige top-50)
- `reports/latest.md` — leesbaar dagrapport (Nederlands)
- `db/radar.sqlite.gz` — alle meetgegevens (SQLite, gzip)
"""


def keep_brief_thumbs(out: Path, today: str, ids: list[int], thumbs: dict[int, str], keep_days: int = 14) -> dict[int, str]:
    """Brief example images stay available for 14 days (feed/briefs/<date>/), so links in the sheet keep working."""
    root = out / "feed" / "briefs"
    day_dir = root / today
    day_dir.mkdir(parents=True, exist_ok=True)
    kept = {}
    for lid in ids:
        if lid in thumbs:
            shutil.copyfile(out / thumbs[lid], day_dir / f"{lid}.jpg")
            kept[lid] = f"feed/briefs/{today}/{lid}.jpg"
    cutoff = (date.fromisoformat(today) - timedelta(days=keep_days)).isoformat()
    for d in root.iterdir():
        if d.is_dir() and d.name < cutoff:
            shutil.rmtree(d, ignore_errors=True)
    return kept


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Etsy POD Radar")
    ap.add_argument("--data-dir", default="out", help="map van de data-branch")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--date", default=None, help="forceer datum (YYYY-MM-DD), voor tests")
    ap.add_argument("--skip-collect", action="store_true", help="alleen analyse/rapport op bestaande data")
    ap.add_argument("--no-images", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = yaml.safe_load(Path(args.config).read_text()) or {}
    today = args.date or datetime.now(timezone.utc).date().isoformat()
    out = Path(args.data_dir)
    out.mkdir(parents=True, exist_ok=True)
    lim = cfg.get("limits", {})
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    store = Store(out)
    client = None
    collector = None
    notes: list[str] = []
    exit_code = 0
    try:
        if not args.skip_collect:
            client = EtsyClient(
                build_api_key(),
                requests_per_second=float(lim.get("requests_per_second", 4)),
                quota_reserve=int(lim.get("min_daily_quota_reserve", 300)),
            )
            collector = Collector(client, store, cfg, today)
            try:
                collector.refresh_taxonomy()
                collector.add_config_shops()
                collector.discover()
                collector.collect_tracked()
                collector.prune_shops()
            except EtsyError as exc:
                # auth problems etc. -> make the workflow fail loudly, but keep what we have
                notes.append(f"Collectie afgebroken: {exc}")
                log.error("Collectie afgebroken: %s", exc)
                if not store.one("SELECT 1 FROM shop_snap WHERE date=?", (today,)):
                    exit_code = 1
            except Exception as exc:  # noqa: BLE001
                notes.append(f"Onverwachte fout tijdens collectie: {exc}")
                log.error("Onverwachte fout: %s", traceback.format_exc())
            notes += collector.notes

        if not store.one("SELECT 1 FROM shop_snap WHERE date=?", (today,)):
            log.error("Geen data voor %s; rapport overgeslagen.", today)
            return exit_code or 1

        result = Analyzer(store, cfg, today).run()

        # images for top listings, new winners and brief examples
        thumbs, sheets, brief_thumbs = {}, [], {}
        if not args.no_images:
            n = int(lim.get("thumbnails", 30))
            wanted = result["top_listings"][:n] + result["new_winners"][:8]
            brief_ids = [e["listing_id"] for b in result["briefs"] for e in b["example_listings"]]
            ids = list(dict.fromkeys([m["listing_id"] for m in wanted] + brief_ids))
            if collector is not None:
                known = {r[0]: r[1] for r in store.q(
                    f"SELECT listing_id, image_url FROM listings WHERE listing_id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
                collector.fetch_images([i for i in ids if not known.get(i)])
            urls = {r[0]: r[1] for r in store.q(
                f"SELECT listing_id, image_url FROM listings WHERE listing_id IN ({','.join('?' * len(ids))})", ids)} if ids else {}
            for coll in (result["top_listings"], result["new_winners"]):
                for m in coll:
                    m["image_url"] = urls.get(m["listing_id"]) or m.get("image_url")
            items = [{"listing_id": i, "image_url": urls.get(i)} for i in ids]
            thumbs = download_thumbs(out, items)
            missing = [i for i in ids if i not in thumbs]
            if missing and collector is not None:
                # stored image URL can be outdated (seller replaced the photo): refresh once via the API
                collector.fetch_images(missing)
                fresh = {r[0]: r[1] for r in store.q(
                    f"SELECT listing_id, image_url FROM listings WHERE listing_id IN ({','.join('?' * len(missing))})", missing)}
                urls.update({k: v for k, v in fresh.items() if v})
                thumbs.update(download_thumbs(out, [{"listing_id": i, "image_url": fresh.get(i)} for i in missing], clear=False))
                for coll in (result["top_listings"], result["new_winners"]):
                    for m in coll:
                        m["image_url"] = urls.get(m["listing_id"]) or m.get("image_url")
            sheets = contact_sheet(out, result["top_listings"][:24], thumbs)
            brief_thumbs = keep_brief_thumbs(out, today, brief_ids, thumbs)

        run_info = {
            "started": started,
            "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "api_calls": client.calls if client else 0,
            "api_errors": client.errors if client else 0,
            "quota_remaining": client.remaining_today if client else None,
            "quota_per_day": client.limit_per_day if client else None,
            "notes": notes,
        }
        write_feed(out, result, run_info, thumbs, sheets, brief_thumbs)
        rep = write_report(out, result, run_info, thumbs, sheets)
        (out / "README.md").write_text(DATA_README)
        store.execute(
            "INSERT OR REPLACE INTO runs(date, started, finished, api_calls, errors, notes) VALUES (?,?,?,?,?,?)",
            (today, started, run_info["finished"], run_info["api_calls"], run_info["api_errors"], "\n".join(notes)),
        )
        store.prune(today, cfg.get("retention_days", {}))
        log.info("Klaar: %s (%d API-calls)", rep, run_info["api_calls"])
        return exit_code
    finally:
        store.close(save=True)


if __name__ == "__main__":
    sys.exit(main())
