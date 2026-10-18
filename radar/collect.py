"""Discovery of POD shops and daily snapshotting of shops, listings and reviews."""
from __future__ import annotations

import json
import logging
import math
from collections import Counter
from datetime import date

from .etsy import EtsyClient, QuotaExhausted
from .store import Store
from .textutil import build_ip_regex, clean_tags, detect_product_type, ip_risk, pod_score

log = logging.getLogger(__name__)


class Collector:
    def __init__(self, client: EtsyClient, store: Store, cfg: dict, today: str):
        self.c = client
        self.s = store
        self.cfg = cfg
        self.lim = cfg.get("limits", {})
        self.today = today
        self.fx = cfg.get("fx_to_usd", {"USD": 1.0})
        self.ip_rx = build_ip_regex(cfg.get("ip_blocklist", []))
        self.snapped_today: set[int] = {
            r[0] for r in store.q("SELECT listing_id FROM listing_snap WHERE date=?", (today,))
        }
        self.notes: list[str] = []
        self._tax_names: dict[int, str] | None = None

    # ------------------------------------------------------------ helpers
    def taxonomy_name(self, taxonomy_id) -> str | None:
        if self._tax_names is None:
            self._tax_names = {r["id"]: r["name"] for r in self.s.q("SELECT id, name FROM taxonomy")}
        return self._tax_names.get(taxonomy_id) if taxonomy_id else None

    def price_usd(self, price: dict | None) -> tuple[float | None, str | None, float | None]:
        if not price:
            return None, None, None
        try:
            amount = float(price["amount"]) / float(price.get("divisor") or 100)
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return None, None, None
        cur = price.get("currency_code") or "USD"
        rate = self.fx.get(cur)
        return amount, cur, (round(amount * rate, 2) if rate else None)

    def ingest_listing(self, L: dict, source: str) -> None:
        lid = L.get("listing_id")
        if not lid:
            return
        title = L.get("title") or ""
        tags = clean_tags(L.get("tags"))
        ptype = detect_product_type(title, self.taxonomy_name(L.get("taxonomy_id")))
        score = pod_score(L, ptype)
        amount, cur, usd = self.price_usd(L.get("price"))
        partners = [p.get("partner_name") for p in (L.get("production_partners") or []) if isinstance(p, dict)]
        created = L.get("original_creation_timestamp") or L.get("creation_timestamp") or L.get("created_timestamp")
        risk = ip_risk(self.ip_rx, title, tags)
        image_url, image_count = None, None
        if L.get("images"):
            imgs = sorted(L["images"], key=lambda i: i.get("rank") or 99)
            image_url = imgs[0].get("url_570xN") or imgs[0].get("url_fullxfull")
            image_count = len(imgs)
        existing = self.s.q("SELECT source, first_seen, image_url, image_count FROM listings WHERE listing_id=?", (lid,))
        if existing:
            ex = existing[0]
            sources = set((ex["source"] or "").split(","))
            sources.add(source)
            self.s.execute(
                """UPDATE listings SET shop_id=?, title=?, tags=?, materials=?, taxonomy_id=?, product_type=?,
                   created_ts=?, url=?, price=?, currency=?, price_usd=?, personalizable=?, partners=?,
                   listing_type=?, is_pod=?, ip_risk=?, last_seen=?, state=?, image_url=?, image_count=?, source=?
                   WHERE listing_id=?""",
                (
                    L.get("shop_id"), title, json.dumps(tags), json.dumps(L.get("materials") or []),
                    L.get("taxonomy_id"), ptype, created, L.get("url"), amount, cur, usd,
                    int(bool(L.get("is_personalizable"))), json.dumps(partners), L.get("listing_type"),
                    int(score >= 2), risk, self.today, L.get("state") or "active",
                    image_url or ex["image_url"], image_count or ex["image_count"],
                    ",".join(sorted(x for x in sources if x)), lid,
                ),
            )
        else:
            self.s.execute(
                """INSERT INTO listings(listing_id, shop_id, title, tags, materials, taxonomy_id, product_type,
                   created_ts, url, price, currency, price_usd, personalizable, partners, listing_type, is_pod,
                   ip_risk, first_seen, last_seen, state, image_url, image_count, has_video, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    lid, L.get("shop_id"), title, json.dumps(tags), json.dumps(L.get("materials") or []),
                    L.get("taxonomy_id"), ptype, created, L.get("url"), amount, cur, usd,
                    int(bool(L.get("is_personalizable"))), json.dumps(partners), L.get("listing_type"),
                    int(score >= 2), risk, self.today, self.today, L.get("state") or "active",
                    image_url, image_count, None, source,
                ),
            )
        if (L.get("state") or "active") == "active":
            self.s.execute(
                "INSERT OR REPLACE INTO listing_snap(date, listing_id, views, favorers, price_usd) VALUES (?,?,?,?,?)",
                (self.today, lid, L.get("views"), L.get("num_favorers"), usd),
            )
            self.snapped_today.add(lid)

    def snap_shop(self, shop: dict) -> None:
        self.s.execute(
            """INSERT OR REPLACE INTO shop_snap(date, shop_id, sold, favorers, review_count, review_avg, active_listings)
               VALUES (?,?,?,?,?,?,?)""",
            (
                self.today, shop["shop_id"], shop.get("transaction_sold_count"), shop.get("num_favorers"),
                shop.get("review_count"), shop.get("review_average"), shop.get("listing_active_count"),
            ),
        )

    def upsert_shop(self, shop: dict, status: str, source: str, pod_share: float | None, reason: str | None = None):
        sid = shop["shop_id"]
        exists = self.s.one("SELECT 1 FROM shops WHERE shop_id=?", (sid,))
        if exists:
            self.s.execute(
                """UPDATE shops SET shop_name=?, url=?, status=?, reject_reason=?, pod_share=COALESCE(?, pod_share),
                   created_ts=?, currency=?, last_evaluated=? WHERE shop_id=?""",
                (shop.get("shop_name"), shop.get("url"), status, reason, pod_share,
                 shop.get("created_timestamp") or shop.get("create_date"), shop.get("currency_code"), self.today, sid),
            )
        else:
            self.s.execute(
                """INSERT INTO shops(shop_id, shop_name, url, first_seen, source, status, reject_reason, pod_share,
                   created_ts, currency, last_evaluated, search_hits) VALUES (?,?,?,?,?,?,?,?,?,?,?,0)""",
                (sid, shop.get("shop_name"), shop.get("url"), self.today, source, status, reason, pod_share,
                 shop.get("created_timestamp") or shop.get("create_date"), shop.get("currency_code"), self.today),
            )

    def store_reviews(self, shop_id: int, reviews: list[dict]) -> set[int]:
        listing_ids = set()
        for r in reviews:
            created = r.get("created_timestamp") or r.get("create_timestamp")
            lid = r.get("listing_id")
            key = f"{shop_id}-{r.get('transaction_id') or ''}-{lid}-{created}-{r.get('buyer_user_id') or ''}"
            self.s.execute(
                "INSERT OR IGNORE INTO reviews(review_key, shop_id, listing_id, rating, created_ts, first_seen) VALUES (?,?,?,?,?,?)",
                (key, shop_id, lid, r.get("rating"), created, self.today),
            )
            if lid:
                listing_ids.add(lid)
        return listing_ids

    # ----------------------------------------------------------- taxonomy
    def refresh_taxonomy(self) -> None:
        last = self.s.get_meta("taxonomy_fetched")
        if last and (date.fromisoformat(self.today) - date.fromisoformat(last)).days < 30:
            return
        try:
            nodes = self.c.seller_taxonomy()
        except QuotaExhausted:
            return
        rows = []

        def walk(node: dict, path: list[str]):
            p = path + [node.get("name") or ""]
            rows.append((node["id"], node.get("name"), " > ".join(p)))
            for ch in node.get("children") or []:
                walk(ch, p)

        for n in nodes:
            walk(n, [])
        if rows:
            self.s.execute("DELETE FROM taxonomy")
            self.s.conn.executemany("INSERT OR REPLACE INTO taxonomy(id, name, path) VALUES (?,?,?)", rows)
            self.s.set_meta("taxonomy_fetched", self.today)
            self._tax_names = None
            log.info("Taxonomie bijgewerkt: %d nodes", len(rows))

    # ---------------------------------------------------------- discovery
    def keywords(self) -> list[str]:
        d = date.fromisoformat(self.today)
        seasonal = self.cfg.get("seasonal_keywords", {}) or {}
        nxt = (d.month % 12) + 1
        kws = list(self.cfg.get("seed_keywords", []))
        for m in (d.month, nxt):
            kws += seasonal.get(m, seasonal.get(str(m), [])) or []
        seen, out = set(), []
        for k in kws:
            k = (k or "").strip().lower()
            if k and k not in seen:
                seen.add(k)
                out.append(k)
        return out

    def discover(self) -> None:
        pages = int(self.lim.get("search_pages_per_keyword", 1))
        hits: Counter = Counter()
        pending: dict[int, list[dict]] = {}
        for kw in self.keywords():
            rank = 0
            for page in range(pages):
                try:
                    results = self.c.search_listings(kw, limit=100, offset=page * 100, optional=True)
                except QuotaExhausted as exc:
                    self.notes.append(f"Discovery gestopt: {exc}")
                    break
                for L in results:
                    rank += 1
                    sid = L.get("shop_id")
                    if not sid:
                        continue
                    self.s.execute(
                        "INSERT OR REPLACE INTO search_rank(date, keyword, rank, listing_id, shop_id) VALUES (?,?,?,?,?)",
                        (self.today, kw, rank, L.get("listing_id"), sid),
                    )
                    hits[sid] += 1
                    pending.setdefault(sid, []).append(L)
                if len(results) < 100:
                    break
        self.s.commit()

        status = {r["shop_id"]: r for r in self.s.q("SELECT shop_id, status, last_evaluated FROM shops")}
        for sid, n in hits.items():
            if sid in status:
                self.s.execute("UPDATE shops SET search_hits = COALESCE(search_hits,0) + ? WHERE shop_id=?", (n, sid))
        # search listings of tracked shops are ingested (they rank -> proven demand)
        for sid, listings in pending.items():
            if sid in status and status[sid]["status"] == "tracked":
                for L in listings:
                    self.ingest_listing(L, "search")

        # evaluate new candidates (or rejected shops older than 30 days)
        def eligible(sid: int) -> bool:
            row = status.get(sid)
            if row is None:
                return True
            if row["status"] == "rejected" and row["last_evaluated"]:
                return (date.fromisoformat(self.today) - date.fromisoformat(row["last_evaluated"])).days >= 30
            return False

        candidates = [sid for sid, _ in hits.most_common() if eligible(sid)]
        budget = int(self.lim.get("new_shops_per_day", 15))
        evaluated = added = 0
        for sid in candidates:
            if evaluated >= budget:
                break
            try:
                ok = self.evaluate_shop(sid, source="search", seed_listings=pending.get(sid, []))
            except QuotaExhausted as exc:
                self.notes.append(f"Shop-evaluatie gestopt: {exc}")
                break
            evaluated += 1
            added += int(ok)
        log.info("Discovery: %d zoekwoorden, %d shops gezien, %d beoordeeld, %d toegevoegd",
                 len(self.keywords()), len(hits), evaluated, added)
        self.s.set_meta("last_discovery", {"date": self.today, "shops_seen": len(hits),
                                           "evaluated": evaluated, "added": added})
        self.s.commit()

    def evaluate_shop(self, shop_id: int, source: str, seed_listings: list[dict] | None = None,
                      force: bool = False) -> bool:
        shop = self.c.get_shop(shop_id, optional=True)
        if not shop or not shop.get("shop_id"):
            return False
        sold = shop.get("transaction_sold_count") or 0
        if not force and sold < int(self.lim.get("min_shop_sales", 500)):
            self.upsert_shop(shop, "rejected", source, None, f"te weinig verkopen ({sold})")
            return False
        listings = self.c.shop_active_listings(shop_id, limit=100, sort_on="created", optional=True)
        sample = listings or seed_listings or []
        scores = [pod_score(L, detect_product_type(L.get("title") or "", self.taxonomy_name(L.get("taxonomy_id"))))
                  for L in sample]
        share = (sum(1 for x in scores if x >= 2) / len(scores)) if scores else 0.0
        if not force and share < float(self.lim.get("min_pod_share", 0.5)):
            self.upsert_shop(shop, "rejected", source, share, f"geen POD-shop (POD-aandeel {share:.0%})")
            return False
        self.upsert_shop(shop, "tracked", source, share)
        self.snap_shop(shop)
        for L in listings:
            self.ingest_listing(L, "shop_new")
        for L in seed_listings or []:
            self.ingest_listing(L, "search")
        return True

    def add_config_shops(self) -> None:
        names = [n for n in (self.cfg.get("extra_shops") or []) if n]
        known = {(r["shop_name"] or "").lower(): r for r in self.s.q("SELECT shop_id, shop_name, status FROM shops")}
        for name in names:
            row = known.get(name.lower())
            if row and row["status"] == "tracked":
                continue
            try:
                found = self.c.find_shops(name)
            except QuotaExhausted:
                break
            match = next((s for s in found if (s.get("shop_name") or "").lower() == name.lower()), None)
            if not match:
                self.notes.append(f"Shop '{name}' uit config niet gevonden")
                continue
            self.evaluate_shop(match["shop_id"], source="config", force=True)
        self.s.commit()

    # ------------------------------------------------------- daily collect
    def collect_tracked(self) -> None:
        shops = self.s.q(
            """SELECT s.shop_id, s.shop_name,
                      (SELECT COUNT(*) FROM reviews r WHERE r.shop_id = s.shop_id) AS n_reviews
               FROM shops s WHERE s.status='tracked'
               ORDER BY COALESCE((SELECT sold FROM shop_snap WHERE shop_id=s.shop_id ORDER BY date DESC LIMIT 1),0) DESC"""
        )
        need_meta: set[int] = set()
        new_limit = int(self.lim.get("shop_new_listings", 100))
        first_pages = int(self.lim.get("reviews_pages_first_run", 3))
        for row in shops:
            sid = row["shop_id"]
            try:
                shop = self.c.get_shop(sid)
                if not shop or not shop.get("shop_id"):
                    self.s.execute("UPDATE shops SET status='dropped', reject_reason='shop niet meer gevonden' WHERE shop_id=?", (sid,))
                    continue
                self.upsert_shop(shop, "tracked", "keep", None)
                self.snap_shop(shop)
                for L in self.c.shop_active_listings(sid, limit=new_limit, sort_on="created", optional=True):
                    self.ingest_listing(L, "shop_new")
                pages = first_pages if row["n_reviews"] == 0 else 1
                for p in range(pages):
                    revs = self.c.shop_reviews(sid, limit=100, offset=p * 100, optional=True)
                    need_meta |= self.store_reviews(sid, revs)
                    if len(revs) < 100:
                        break
            except QuotaExhausted as exc:
                self.notes.append(f"Shop-collectie gestopt bij {row['shop_name']}: {exc}")
                break
        self.s.commit()

        # refresh tracked listings (and fetch metadata for reviewed listings we don't know yet)
        known = {r[0] for r in self.s.q("SELECT listing_id FROM listings")}
        missing_meta = [lid for lid in need_meta if lid not in known]
        cap = int(self.lim.get("max_tracked_listings", 8000))
        since_30 = _ts(self.today) - 30 * 86400
        candidates = self.s.q(
            """SELECT l.listing_id,
                      (SELECT COUNT(*) FROM reviews r WHERE r.listing_id=l.listing_id
                         AND r.created_ts >= ?) AS rev30,
                      COALESCE((SELECT favorers FROM listing_snap ls WHERE ls.listing_id=l.listing_id
                                ORDER BY date DESC LIMIT 1),0) AS fav,
                      l.created_ts
               FROM listings l JOIN shops s ON s.shop_id=l.shop_id
               WHERE s.status='tracked' AND l.state IN ('active') AND l.last_seen >= date(?, '-30 day')""",
            (since_30, self.today),
        )
        now_ts = _ts(self.today)

        def prio(r) -> float:
            age_days = max(1.0, (now_ts - (r["created_ts"] or now_ts)) / 86400)
            return r["rev30"] * 10 + (5 if age_days <= 60 else 0) + math.log1p(r["fav"]) + math.log1p(r["fav"] / age_days) * 2

        ranked = sorted(candidates, key=prio, reverse=True)[:cap]
        to_refresh = [r["listing_id"] for r in ranked if r["listing_id"] not in self.snapped_today]
        ids = missing_meta + to_refresh
        returned: set[int] = set()
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            try:
                results = self.c.listings_batch(chunk, optional=True)
            except QuotaExhausted as exc:
                self.notes.append(f"Listing-verversing gestopt na {i} van {len(ids)}: {exc}")
                break
            for L in results:
                returned.add(L.get("listing_id"))
                self.ingest_listing(L, "review" if L.get("listing_id") in missing_meta else "refresh")
            for lid in chunk:
                if lid not in returned and lid in to_refresh:
                    self.s.execute("UPDATE listings SET state='missing' WHERE listing_id=?", (lid,))
        self.s.set_meta("last_refresh", {"date": self.today, "refreshed": len(returned), "requested": len(ids),
                                         "tracked_total": len(ranked)})
        self.s.commit()

    def prune_shops(self) -> None:
        cap = int(self.lim.get("max_tracked_shops", 80))
        rows = self.s.q(
            """SELECT s.shop_id, s.source,
                 COALESCE((SELECT sold FROM shop_snap a WHERE a.shop_id=s.shop_id ORDER BY date DESC LIMIT 1),0) -
                 COALESCE((SELECT sold FROM shop_snap b WHERE b.shop_id=s.shop_id AND b.date >= date(?, '-30 day')
                           ORDER BY date ASC LIMIT 1),0) AS sales30,
                 COALESCE((SELECT sold FROM shop_snap a WHERE a.shop_id=s.shop_id ORDER BY date DESC LIMIT 1),0) AS sold
               FROM shops s WHERE s.status='tracked'""",
            (self.today,),
        )
        if len(rows) <= cap:
            return
        removable = sorted([r for r in rows if r["source"] != "config"], key=lambda r: (r["sales30"], r["sold"]))
        for r in removable[: len(rows) - cap]:
            self.s.execute("UPDATE shops SET status='dropped', reject_reason='laagste verkopen (limiet)' WHERE shop_id=?",
                           (r["shop_id"],))
        self.s.commit()

    # ---------------------------------------------------------- images
    def fetch_images(self, listing_ids: list[int]) -> None:
        ids = [i for i in listing_ids if i]
        for i in range(0, len(ids), 100):
            try:
                results = self.c.listings_batch(ids[i:i + 100], includes="Images", optional=True)
            except QuotaExhausted:
                return
            for L in results:
                if not L.get("images"):
                    continue
                imgs = sorted(L["images"], key=lambda im: im.get("rank") or 99)
                self.s.execute(
                    "UPDATE listings SET image_url=?, image_count=? WHERE listing_id=?",
                    (imgs[0].get("url_570xN") or imgs[0].get("url_fullxfull"), len(imgs), L["listing_id"]),
                )
        self.s.commit()


def _ts(day: str) -> int:
    from datetime import datetime, time, timezone

    return int(datetime.combine(date.fromisoformat(day), time(), tzinfo=timezone.utc).timestamp())
