"""Turns snapshots into signals: shop sales, listing momentum, trending themes, product mix, briefs, advice."""
from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone

from .store import Store
from .textutil import build_ip_regex, theme_terms

SEASON_HINTS = {
    1: "Valentijnsdag (14 feb) en Galentine's: upload uiterlijk half januari. Daarna St. Patrick's Day.",
    2: "St. Patrick's Day en Pasen komen eraan; Valentijn-designs nog tot ~10 februari pushen.",
    3: "Pasen, lente en Moederdag (VS: 2e zondag mei) voorbereiden.",
    4: "Moederdag (VS) en afstuderen (graduation) zijn de grote pieken van mei.",
    5: "Vaderdag (VS: 3e zondag juni) en 4th of July voorbereiden; zomer- en vakantiedesigns.",
    6: "4th of July nu live; begin met back-to-school en teacher-designs.",
    7: "Back-to-school en teacher appreciation; eerste fall/Halloween-designs uploaden.",
    8: "Fall en Halloween moeten nu live staan; Etsy-kopers zoeken al vanaf eind augustus.",
    9: "Halloween piekt; Thanksgiving en de eerste Christmas-designs nu uploaden (indexering duurt 1-2 weken).",
    10: "Halloween is bijna voorbij als verkoopkans; zet de focus op Thanksgiving, Christmas, family matching en gepersonaliseerde cadeaus.",
    11: "Black Friday/Cyber Monday en Christmas zijn de grootste weken van het jaar; levertijd-deadlines communiceren.",
    12: "Laatste Christmas-verkopen tot ~15 december (verzendtijd); daarna New Year en Valentijn voorbereiden.",
}


def _ts(day: str) -> int:
    return int(datetime.combine(date.fromisoformat(day), time(), tzinfo=timezone.utc).timestamp())


def _pct_rank(values: dict[int, float]) -> dict[int, float]:
    if not values:
        return {}
    items = sorted(values.items(), key=lambda kv: kv[1])
    n = len(items)
    out = {}
    i = 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        r = ((i + j) / 2) / max(n - 1, 1)
        for k in range(i, j + 1):
            out[items[k][0]] = r
        i = j + 1
    return out


def _quantiles(xs: list[float]) -> dict:
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return {"p25": None, "median": None, "p75": None, "n": 0}
    if len(xs) == 1:
        return {"p25": xs[0], "median": xs[0], "p75": xs[0], "n": 1}
    q = statistics.quantiles(xs, n=4, method="inclusive")
    return {"p25": round(q[0], 2), "median": round(q[1], 2), "p75": round(q[2], 2), "n": len(xs)}


def _nl(x, nd: int = 0) -> str:
    """Dutch number formatting: 1.234,5"""
    if x is None:
        return "–"
    return f"{x:,.{nd}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _dedupe_terms(rows: list[dict], key: str) -> list[dict]:
    """Drop a term when a more specific term (superset of its words) covers >= 80% of its listings."""
    sets = [(r, set(r["term"].split())) for r in rows]
    return [r for r, w in sets if not any(w < w2 and r2[key] >= 0.8 * r[key] for r2, w2 in sets)]


class Analyzer:
    def __init__(self, store: Store, cfg: dict, today: str):
        self.s = store
        self.cfg = cfg
        self.today = today
        self.now_ts = _ts(today)
        self.ip_rx = build_ip_regex(cfg.get("ip_blocklist", []))
        self.dates = [r[0] for r in store.q("SELECT DISTINCT date FROM shop_snap WHERE date <= ? ORDER BY date", (today,))]
        self.data_days = len(self.dates)

    # ------------------------------------------------------------ helpers
    def _base_date(self, days_back: int) -> str | None:
        """Earliest snapshot date within [today - days_back, today)."""
        lo = (date.fromisoformat(self.today) - timedelta(days=days_back)).isoformat()
        cands = [d for d in self.dates if lo <= d < self.today]
        return cands[0] if cands else None

    def _nearest_on_or_before(self, day: str) -> str | None:
        cands = [d for d in self.dates if d <= day]
        return cands[-1] if cands else None

    @staticmethod
    def _span(d_from: str, d_to: str) -> int:
        return max(1, (date.fromisoformat(d_to) - date.fromisoformat(d_from)).days)

    # -------------------------------------------------------------- shops
    def shop_metrics(self) -> dict[int, dict]:
        shops = {r["shop_id"]: dict(r) for r in self.s.q("SELECT * FROM shops WHERE status='tracked'")}
        snaps = defaultdict(dict)
        for r in self.s.q("SELECT * FROM shop_snap WHERE date >= date(?, '-31 day')", (self.today,)):
            snaps[r["shop_id"]][r["date"]] = dict(r)
        prices = defaultdict(list)
        for r in self.s.q(
            """SELECT l.shop_id, l.price_usd FROM listings l JOIN listing_snap ls
               ON ls.listing_id=l.listing_id AND ls.date=? WHERE l.price_usd IS NOT NULL""", (self.today,)):
            prices[r["shop_id"]].append(r["price_usd"])
        new7 = Counter(r["shop_id"] for r in self.s.q(
            "SELECT shop_id FROM listings WHERE created_ts >= ?", (self.now_ts - 7 * 86400,)))
        out = {}
        for sid, sh in shops.items():
            ss = snaps.get(sid, {})
            cur = ss.get(self.today)
            if not cur:
                continue
            m = {
                "shop_id": sid, "shop_name": sh["shop_name"], "url": sh["url"], "source": sh["source"],
                "sold_total": cur["sold"], "active_listings": cur["active_listings"],
                "favorers": cur["favorers"], "review_count": cur["review_count"], "review_avg": cur["review_avg"],
                "pod_share": sh["pod_share"], "new_listings_7d": new7.get(sid, 0),
            }
            age_days = max(30.0, (self.now_ts - (sh["created_ts"] or self.now_ts)) / 86400)
            m["lifetime_sales_per_day"] = round((cur["sold"] or 0) / age_days, 1)
            for label, back in (("1d", 1), ("7d", 7), ("30d", 30)):
                base_d = self._base_date(back)
                base = ss.get(base_d) if base_d else None
                if base and base["sold"] is not None and cur["sold"] is not None:
                    span = self._span(base_d, self.today)
                    delta = cur["sold"] - base["sold"]
                    m[f"sales_{label}"] = delta
                    m[f"sales_per_day_{label}"] = round(delta / span, 1)
                    m[f"span_{label}"] = span
                else:
                    m[f"sales_{label}"] = None
                    m[f"sales_per_day_{label}"] = None
            med = statistics.median(prices[sid]) if prices.get(sid) else None
            m["median_price_usd"] = round(med, 2) if med else None
            rate = m["sales_per_day_7d"] if m["sales_per_day_7d"] is not None else m["lifetime_sales_per_day"]
            m["sales_per_day"] = rate
            m["sales_basis"] = "gemeten" if m["sales_per_day_7d"] is not None else "levensduur-gemiddelde"
            m["est_revenue_per_day_usd"] = round(rate * med, 0) if (rate is not None and med) else None
            rc, sold = cur["review_count"] or 0, cur["sold"] or 0
            m["review_rate"] = min(0.6, max(0.03, rc / sold)) if sold > 0 else 0.12
            out[sid] = m
        return out

    # ----------------------------------------------------------- listings
    def listing_metrics(self, shops: dict[int, dict]) -> dict[int, dict]:
        base7 = self._base_date(7)
        rows = self.s.q(
            """SELECT l.*, ls.views AS views_now, ls.favorers AS fav_now
               FROM listings l JOIN listing_snap ls ON ls.listing_id=l.listing_id AND ls.date=?
               WHERE l.is_pod=1""", (self.today,))
        hist = defaultdict(dict)
        if base7:
            for r in self.s.q(
                "SELECT listing_id, date, views, favorers FROM listing_snap WHERE date >= ? AND date < ?",
                (base7, self.today)):
                hist[r["listing_id"]][r["date"]] = (r["views"], r["favorers"])
        rev30, rev7 = Counter(), Counter()
        for r in self.s.q("SELECT listing_id, created_ts FROM reviews WHERE created_ts >= ?", (self.now_ts - 30 * 86400,)):
            rev30[r["listing_id"]] += 1
            if r["created_ts"] >= self.now_ts - 7 * 86400:
                rev7[r["listing_id"]] += 1
        search = defaultdict(list)
        for r in self.s.q("SELECT keyword, rank, listing_id FROM search_rank WHERE date=?", (self.today,)):
            search[r["listing_id"]].append((r["keyword"], r["rank"]))
        shop_names = {r["shop_id"]: r["shop_name"] for r in self.s.q("SELECT shop_id, shop_name FROM shops")}

        out = {}
        for r in rows:
            lid = r["listing_id"]
            age = max(1.0, (self.now_ts - (r["created_ts"] or self.now_ts)) / 86400)
            fav_now = r["fav_now"] or 0
            m = {
                "listing_id": lid, "shop_id": r["shop_id"], "shop_name": shop_names.get(r["shop_id"]),
                "title": r["title"], "url": r["url"], "product_type": r["product_type"],
                "price_usd": r["price_usd"], "currency": r["currency"], "price": r["price"],
                "age_days": int(age), "favorers": fav_now, "views": r["views_now"],
                "fav_per_day_life": round(fav_now / age, 2),
                "personalizable": bool(r["personalizable"]), "tags": json.loads(r["tags"] or "[]"),
                "partners": json.loads(r["partners"] or "[]"), "ip_risk": r["ip_risk"],
                "image_url": r["image_url"], "image_count": r["image_count"],
                "reviews_30d": rev30.get(lid, 0), "reviews_7d": rev7.get(lid, 0),
                "search_hits": sorted(search.get(lid, []), key=lambda x: x[1])[:5],
            }
            h = hist.get(lid)
            if h:
                d0 = min(h)
                v0, f0 = h[d0]
                span = self._span(d0, self.today)
                m["fav_per_day_recent"] = round((fav_now - (f0 or 0)) / span, 2) if f0 is not None else None
                vnow = r["views_now"]
                m["views_per_day_recent"] = (
                    round((vnow - v0) / span, 1) if (vnow is not None and v0 is not None and vnow >= v0) else None
                )
                m["delta_span_days"] = span
            else:
                m["fav_per_day_recent"] = None
                m["views_per_day_recent"] = None
                m["delta_span_days"] = 0
            shop = shops.get(r["shop_id"])
            rr = shop["review_rate"] if shop else 0.12
            m["est_sales_30d"] = round(m["reviews_30d"] / rr, 1)
            m["search_score"] = round(sum((101 - rk) / 100 for _, rk in search.get(lid, [])), 2)
            out[lid] = m

        # momentum score (percentile mix)
        have_delta = sum(1 for m in out.values() if m["fav_per_day_recent"] is not None) >= max(10, len(out) * 0.3)
        A = _pct_rank({k: (m["fav_per_day_recent"] if m["fav_per_day_recent"] is not None else m["fav_per_day_life"])
                       for k, m in out.items()})
        B = _pct_rank({k: (m["views_per_day_recent"] or 0) for k, m in out.items()})
        C = _pct_rank({k: m["est_sales_30d"] for k, m in out.items()})
        D = _pct_rank({k: m["fav_per_day_life"] for k, m in out.items()})
        E = _pct_rank({k: m["search_score"] for k, m in out.items()})
        any_views = any((m["views_per_day_recent"] or 0) > 0 for m in out.values())
        if have_delta:
            w = {"A": 0.30, "B": 0.15 if any_views else 0.0, "C": 0.30, "D": 0.10, "E": 0.15}
        else:
            w = {"A": 0.0, "B": 0.0, "C": 0.40, "D": 0.35, "E": 0.25}
        tot = sum(w.values())
        for k, m in out.items():
            sc = (w["A"] * A.get(k, 0) + w["B"] * B.get(k, 0) + w["C"] * C.get(k, 0)
                  + w["D"] * D.get(k, 0) + w["E"] * E.get(k, 0)) / tot
            m["momentum"] = round(sc * 100, 1)
        self.momentum_basis = "gemeten groei" if have_delta else "opbouwfase (levensduur, reviews, zoekpositie)"
        return out

    # -------------------------------------------------------------- terms
    def term_stats(self, listings: dict[int, dict], top_ids: set[int]) -> list[dict]:
        cnt_all, cnt_top = Counter(), Counter()
        terms_by_listing = {}
        for lid, m in listings.items():
            terms = {t for t in theme_terms(m["title"], m["tags"]) if not (self.ip_rx and self.ip_rx.search(t))}
            terms_by_listing[lid] = terms
            cnt_all.update(terms)
            if lid in top_ids:
                cnt_top.update(terms)
        n_all, n_top = max(1, len(listings)), max(1, len(top_ids))
        min_top = max(3, int(n_top * 0.02))
        rows = []
        for t, ct in cnt_top.items():
            ca = cnt_all[t]
            if ct < min_top or ca < 5:
                continue
            lift = (ct / n_top) / (ca / n_all)
            rows.append({"term": t, "lift": round(lift, 2), "top_count": ct, "all_count": ca,
                         "score": round(lift * math.log2(1 + ct), 2)})
        rows = [r for r in rows if r["lift"] >= 1.15 and r["all_count"] / n_all <= 0.5]
        rows.sort(key=lambda r: r["score"], reverse=True)
        self.terms_by_listing = terms_by_listing
        return _dedupe_terms(rows, "top_count")

    def rising_terms(self, listings: dict[int, dict]) -> list[dict]:
        """Week-over-week favorite growth per term. Needs ~14 days of data."""
        d7 = self._nearest_on_or_before((date.fromisoformat(self.today) - timedelta(days=7)).isoformat())
        d14 = self._nearest_on_or_before((date.fromisoformat(self.today) - timedelta(days=14)).isoformat())
        if not d7 or not d14 or d7 == d14 or self._span(d14, self.today) < 12:
            return []
        f7 = {r[0]: r[1] for r in self.s.q("SELECT listing_id, favorers FROM listing_snap WHERE date=?", (d7,))}
        f14 = {r[0]: r[1] for r in self.s.q("SELECT listing_id, favorers FROM listing_snap WHERE date=?", (d14,))}
        last, prev, n = Counter(), Counter(), Counter()
        for lid, m in listings.items():
            if lid in f7 and lid in f14 and f7[lid] is not None and f14[lid] is not None:
                gl = max(0, m["favorers"] - f7[lid])
                gp = max(0, f7[lid] - f14[lid])
                for t in self.terms_by_listing.get(lid, ()):
                    last[t] += gl
                    prev[t] += gp
                    n[t] += 1
        rows = []
        for t, gl in last.items():
            if gl < 20 or n[t] < 4:
                continue
            growth = (gl + 5) / (prev[t] + 5)
            rows.append({"term": t, "growth": round(growth, 2), "fav_last_7d": gl, "fav_prev_7d": prev[t],
                         "listings": n[t]})
        rows = _dedupe_terms([r for r in rows if r["growth"] >= 1.3], "listings")
        rows.sort(key=lambda r: (r["growth"], r["fav_last_7d"]), reverse=True)
        return rows[:25]

    # ------------------------------------------------------------- briefs
    def briefs(self, listings: dict[int, dict], ranked: list[int], terms: list[dict], rising: list[dict] | None = None,
               max_briefs: int = 12) -> list[dict]:
        strong = {t["term"]: t for t in terms if t["lift"] >= 1.3}
        growth = {r["term"]: r["growth"] for r in (rising or [])}
        pool = [lid for lid in ranked[:120] if not listings[lid]["ip_risk"]]
        groups: dict[tuple[str, str], list[int]] = defaultdict(list)
        for lid in pool:
            m = listings[lid]
            cands = [strong[t] for t in self.terms_by_listing.get(lid, ()) if t in strong]
            if not cands:
                continue
            best = max(cands, key=lambda t: (len(t["term"].split()) > 1, t["score"]))
            groups[(m["product_type"], best["term"])].append(lid)
        scored = []
        for (ptype, theme), lids in groups.items():
            shops_ = {listings[l]["shop_id"] for l in lids}
            if len(lids) < 2 and listings[lids[0]]["momentum"] < 90:
                continue
            # evidence (momentum) x distinctiveness (lift) x week-over-week growth: rising themes first
            weight = (sum(listings[l]["momentum"] for l in lids)
                      * min(strong[theme]["lift"], 3.0) ** 0.5
                      * min(growth.get(theme, 1.0), 3.0) ** 0.5)
            scored.append((weight, ptype, theme, lids, shops_))
        scored.sort(reverse=True)
        out, used_themes = [], Counter()
        for weight, ptype, theme, lids, shops_ in scored:
            if used_themes[theme] >= 2:
                continue
            used_themes[theme] += 1
            ms = [listings[l] for l in lids]
            tag_counts = Counter(t for m in ms for t in m["tags"] if not (self.ip_rx and self.ip_rx.search(t)))
            related = Counter(t for l in lids for t in self.terms_by_listing.get(l, ()) if t in strong and t != theme)
            theme_words = set(theme.split())
            tags = [t for t, _ in sorted(tag_counts.items(),
                                         key=lambda kv: (bool(theme_words & set(kv[0].split())), kv[1]),
                                         reverse=True)[:13]]
            for t, _ in related.most_common(13):
                if len(tags) >= 13:
                    break
                if t not in tags and len(t) <= 20:
                    tags.append(t)
            n_shops = len(shops_)
            conf = min(1.0, 0.2 + 0.15 * min(n_shops, 4) + 0.1 * min(len(lids), 3))
            if self.data_days < 7:
                conf *= 0.6
            examples = sorted(ms, key=lambda m: m["momentum"], reverse=True)[:4]
            out.append({
                "brief_id": f"{self.today}-{len(out) + 1:02d}",
                "product_type": ptype,
                "theme": theme,
                "theme_lift": strong[theme]["lift"],
                "theme_growth_wow": growth.get(theme),
                "related_terms": [t for t, _ in related.most_common(6)],
                "evidence": {
                    "listings": len(lids), "shops": n_shops,
                    "est_sales_30d": round(sum(m["est_sales_30d"] for m in ms), 1),
                    "avg_fav_per_day": round(statistics.mean(
                        (m["fav_per_day_recent"] if m["fav_per_day_recent"] is not None else m["fav_per_day_life"])
                        for m in ms), 2),
                    "avg_momentum": round(statistics.mean(m["momentum"] for m in ms), 1),
                },
                "price_usd": _quantiles([m["price_usd"] for m in ms]),
                "personalization_share": round(sum(m["personalizable"] for m in ms) / len(ms), 2),
                "recommended_tags": tags[:13],
                "example_listings": [
                    {"listing_id": m["listing_id"], "title": m["title"], "url": m["url"], "shop": m["shop_name"],
                     "price_usd": m["price_usd"], "momentum": m["momentum"],
                     "thumb": f"feed/thumbs/{m['listing_id']}.jpg"}
                    for m in examples
                ],
                "design_direction": (
                    f"Origineel {ptype.lower()}-design rond '{theme}'"
                    + (f" (combineer met: {', '.join(t for t, _ in related.most_common(3))})" if related else "")
                    + ". Gebruik eigen tekst en illustratie; kopieer geen bestaande ontwerpen."
                ),
                "confidence": round(conf, 2),
            })
            if len(out) >= max_briefs:
                break
        return out

    # ---------------------------------------------------------------- run
    def run(self) -> dict:
        shops = self.shop_metrics()
        listings = self.listing_metrics(shops)
        ranked = sorted(listings, key=lambda k: listings[k]["momentum"], reverse=True)
        n_top = max(30, int(len(ranked) * 0.15)) if ranked else 0
        top_ids = set(ranked[:n_top])
        terms = self.term_stats(listings, top_ids)
        rising = self.rising_terms(listings)
        briefs = self.briefs(listings, ranked, terms, rising)

        # product mix
        mix = []
        all_types = Counter(m["product_type"] for m in listings.values())
        top_types = Counter(listings[l]["product_type"] for l in top_ids)
        for ptype, ct in top_types.most_common():
            share_top = ct / max(1, len(top_ids))
            share_all = all_types[ptype] / max(1, len(listings))
            q = _quantiles([listings[l]["price_usd"] for l in top_ids if listings[l]["product_type"] == ptype])
            mix.append({"product_type": ptype, "share_top": round(share_top, 3), "share_all": round(share_all, 3),
                        "lift": round(share_top / share_all, 2) if share_all else None, "price_top_usd": q})

        def share(ids, key):
            ids = list(ids)
            return round(sum(1 for i in ids if listings[i][key]) / len(ids), 3) if ids else None

        top_list = [listings[i] for i in ranked[:n_top]]
        hygiene = {
            "avg_tags_top": round(statistics.mean(len(m["tags"]) for m in top_list), 1) if top_list else None,
            "avg_tags_all": round(statistics.mean(len(m["tags"]) for m in listings.values()), 1) if listings else None,
            "avg_title_len_top": round(statistics.mean(len(m["title"] or "") for m in top_list)) if top_list else None,
            "avg_images_top": (round(statistics.mean(m["image_count"] for m in top_list if m["image_count"]), 1)
                               if any(m["image_count"] for m in top_list) else None),
        }
        personalization = {"share_top": share(top_ids, "personalizable"), "share_all": share(listings, "personalizable")}
        clean = [i for i in ranked if not listings[i]["ip_risk"]]
        new_winners = [listings[i] for i in clean if listings[i]["age_days"] <= 30][:15]
        new_in_top = sum(1 for i in ranked[:n_top] if listings[i]["age_days"] <= 30 and not listings[i]["ip_risk"])
        shop_rank = sorted(shops.values(),
                           key=lambda m: (m["sales_basis"] == "gemeten", m["sales_per_day"] or 0), reverse=True)
        for i, m in enumerate(shop_rank, 1):
            m["rank"] = i
            m["top_listings"] = [
                {"listing_id": l["listing_id"], "title": l["title"], "momentum": l["momentum"], "url": l["url"]}
                for l in sorted((x for x in listings.values() if x["shop_id"] == m["shop_id"]),
                                key=lambda x: x["momentum"], reverse=True)[:3]
            ]
        ip_top = sum(1 for i in ranked[:n_top] if listings[i]["ip_risk"])
        partners = Counter(p for m in top_list for p in m["partners"])

        result = {
            "date": self.today,
            "data_days": self.data_days,
            "momentum_basis": getattr(self, "momentum_basis", None),
            "counts": {"shops_tracked": len(shops), "listings_analyzed": len(listings), "top_set": n_top},
            "top_shops": shop_rank[:25],
            "top_listings": [listings[i] for i in clean[:50]],
            "new_winners": new_winners,
            "new_in_top": new_in_top,
            "ip_risk_top": [
                {"listing_id": listings[i]["listing_id"], "title": listings[i]["title"], "url": listings[i]["url"],
                 "shop_name": listings[i]["shop_name"], "product_type": listings[i]["product_type"],
                 "momentum": listings[i]["momentum"], "ip_term": listings[i]["ip_risk"]}
                for i in ranked[:n_top] if listings[i]["ip_risk"]][:15],
            "trending_terms": terms[:40],
            "rising_terms": rising,
            "product_mix": mix,
            "personalization": personalization,
            "listing_hygiene": hygiene,
            "production_partners_top": partners.most_common(8),
            "ip_flagged_in_top": ip_top,
            "briefs": briefs,
        }
        result["advice"] = self.advice(result)
        result["summary"] = self.summary(result)
        return result

    # ------------------------------------------------------------- advice
    def advice(self, r: dict) -> list[str]:
        tips = []
        mix = [m for m in r["product_mix"] if m["share_top"] >= 0.05]
        if mix:
            best = max(mix, key=lambda m: (m["lift"] or 0) * m["share_top"])
            p = best["price_top_usd"]["median"]
            if (best["lift"] or 0) >= 1.2:
                tips.append(
                    f"Productfocus: {best['product_type']} is oververtegenwoordigd bij de snelste stijgers "
                    f"({best['share_top']:.0%} van de top vs {best['share_all']:.0%} van alles)"
                    + (f"; mediaanprijs van toppers ${_nl(p, 2)}." if p else ".")
                )
            else:
                top3 = ", ".join(f"{m['product_type']} ({m['share_top']:.0%})" for m in mix[:3])
                tips.append(f"Geen product springt eruit; de top is verdeeld over {top3}. Kies per thema het product "
                            f"uit de briefs in plaats van één productsoort.")
        pers = r["personalization"]
        if pers["share_top"] is not None and pers["share_all"]:
            ratio = pers["share_top"] / pers["share_all"] if pers["share_all"] else 0
            if ratio >= 1.2:
                tips.append(f"Personalisatie werkt: {pers['share_top']:.0%} van de toppers is personaliseerbaar "
                            f"(gemiddeld {pers['share_all']:.0%}). Bouw designs met een naam/jaartal/tekstvak.")
            elif ratio <= 0.8:
                tips.append(f"Vaste designs scoren nu beter dan personalisatie ({pers['share_top']:.0%} van de top is "
                            f"personaliseerbaar vs {pers['share_all']:.0%} gemiddeld).")
        if r["trending_terms"]:
            t = ", ".join(f"'{x['term']}'" for x in r["trending_terms"][:5])
            tips.append(f"Sterkste thema's bij de toppers: {t}.")
        if r["rising_terms"]:
            t = ", ".join(f"'{x['term']}' (x{_nl(x['growth'], 1)})" for x in r["rising_terms"][:5])
            tips.append(f"Snelst stijgend t.o.v. vorige week: {t}.")
        nw = r["new_winners"]
        if nw and r["new_in_top"]:
            tips.append(f"{r['new_in_top']} listings van max. 30 dagen oud staan al in de top: nieuwe designs kunnen "
                        f"snel scoren, bv. '{(nw[0]['title'] or '')[:70]}'.")
        h = r["listing_hygiene"]
        if h["avg_tags_top"]:
            tips.append(f"Listing-SEO: toppers gebruiken gemiddeld {_nl(h['avg_tags_top'], 1)} van de 13 tags en titels van "
                        f"~{h['avg_title_len_top']} tekens" + (f" en {_nl(h['avg_images_top'], 1)} foto's" if h["avg_images_top"] else "")
                        + ". Gebruik altijd alle 13 tags en minimaal 5 mockups.")
        if r["ip_flagged_in_top"]:
            tips.append(f"Let op: {r['ip_flagged_in_top']} toppers bevatten merk- of fandomnamen (IP-risico). Die zijn "
                        f"uitgesloten van de briefs; maak ze niet na.")
        month = date.fromisoformat(self.today).month
        tips.append("Seizoen: " + SEASON_HINTS[month])
        return tips

    def summary(self, r: dict) -> list[str]:
        out = []
        if r["top_shops"]:
            s = r["top_shops"][0]
            out.append(f"Grootste POD-verkoper nu: {s['shop_name']} (~{_nl(s['sales_per_day'], 1)} verkopen/dag, {s['sales_basis']}).")
        tot = sum((s["sales_per_day"] or 0) for s in r["top_shops"])
        if tot:
            out.append(f"Top-{len(r['top_shops'])} shops samen: ~{_nl(tot)} verkopen per dag.")
        if r["product_mix"]:
            pm = r["product_mix"][0]
            out.append(f"Meest voorkomend product in de top: {pm['product_type']} ({pm['share_top']:.0%}).")
        if r["trending_terms"]:
            out.append("Hete thema's: " + ", ".join(x["term"] for x in r["trending_terms"][:3]) + ".")
        if r["briefs"]:
            out.append(f"{len(r['briefs'])} design briefs klaar voor de designbot.")
        if self.data_days < 7:
            out.append(f"Opbouwfase: dag {self.data_days} van de meting. Groeicijfers zijn betrouwbaar vanaf dag 7, "
                       f"week-op-week-trends vanaf dag 14.")
        return out
