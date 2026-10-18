"""A deterministic fake Etsy Open API v3 server for offline end-to-end tests.

It mimics the response shapes of the public endpoints the radar uses, simulates a market that changes
day by day (themes rise and fall, shops sell, new listings appear) and returns quota headers, an
occasional 429 and 5xx, and a 404 for a deleted shop.
"""
from __future__ import annotations

import io
import json
import random
import re
import threading
from datetime import date, datetime, time, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

START = date(2026, 10, 1)
STATE = {"day": 0, "calls": 0, "limit": 5000, "fail_every": 97}

THEMES = {
    # theme: (base strength, slope per day)  -> strength(d) = max(0.05, base + slope*d)
    "retro halloween": (1.6, -0.12),
    "spooky season": (1.4, -0.10),
    "ghost": (1.2, -0.06),
    "christmas family": (0.3, 0.14),
    "cozy christmas": (0.25, 0.12),
    "thanksgiving": (0.5, 0.05),
    "teacher": (0.9, 0.0),
    "nurse": (0.7, 0.0),
    "dog mom": (0.8, 0.01),
    "cat lover": (0.6, 0.0),
    "bookish": (1.0, 0.02),
    "mama": (0.9, 0.0),
    "western cowgirl": (0.7, 0.03),
    "funny dad": (0.6, 0.0),
    "fall vibes": (1.0, -0.02),
    "custom name": (0.8, 0.02),
    "disney mickey": (1.5, 0.0),  # IP risk -> must never become a brief
    "minimalist botanical": (0.5, 0.0),
}
PRODUCTS = [
    ("Sweatshirt", "Sweatshirt, {t} Crewneck, Comfort Colors Sweater", 1069, 42.0),
    ("T-shirt", "Shirt, {t} Tee, Comfort Colors T-Shirt Gift", 1069, 28.0),
    ("Hoodie", "Hoodie, {t} Hooded Sweatshirt", 1069, 48.0),
    ("Mug", "Mug, {t} Coffee Mug 11oz Ceramic", 1062, 18.0),
    ("Wall art", "Poster, {t} Wall Art Print", 2078, 24.0),
    ("Tote bag", "Tote Bag, {t} Canvas Tote", 1, 22.0),
]
NON_POD_PRODUCTS = [
    ("Necklace", "Handmade {t} Necklace, Sterling Silver Pendant", 1, 55.0, "physical"),
    ("Digital", "{t} Printable Planner, Instant Download PDF", 1, 4.0, "download"),
]


def day_ts(d: int) -> int:
    return int(datetime.combine(START, time(), tzinfo=timezone.utc).timestamp()) + d * 86400


def strength(theme: str, d: int) -> float:
    base, slope = THEMES[theme]
    return max(0.05, base + slope * d)


def cum_strength(theme: str, d_from: int, d_to: int) -> float:
    return sum(strength(theme, t) for t in range(d_from, d_to))


class World:
    def __init__(self, seed: int = 7, n_shops: int = 90):
        rnd = random.Random(seed)
        self.shops: dict[int, dict] = {}
        self.listings: dict[int, dict] = {}
        lid = 1_000_000_000
        themes = list(THEMES)
        for i in range(n_shops):
            sid = 30_000_000 + i
            pod = i % 4 != 3
            shop = {
                "shop_id": sid, "shop_name": f"{'Print' if pod else 'Craft'}Shop{i:03d}",
                "user_id": 900 + i, "created_timestamp": day_ts(-rnd.randint(200, 2500)),
                "currency_code": "USD" if i % 5 else "EUR", "pod": pod,
                "base_sold": int(rnd.choice([80, 300, 900, 2500, 8000, 25000, 60000]) * rnd.uniform(0.6, 1.4)),
                "skill": rnd.uniform(0.4, 2.0), "deleted_from": 4 if i == 5 else None,
                "listing_ids": [],
            }
            n_list = rnd.randint(40, 220)
            for _ in range(n_list):
                lid += 1
                theme = rnd.choice(themes)
                if pod:
                    ptype, tpl, tax, price = rnd.choice(PRODUCTS)
                    ltype = "physical"
                else:
                    ptype, tpl, tax, price, ltype = rnd.choice(NON_POD_PRODUCTS)
                created_day = rnd.choice([-rnd.randint(20, 700)] * 4 + [rnd.randint(-15, 12)])
                quality = rnd.uniform(0.1, 1.0) ** 2 * shop["skill"]
                self.listings[lid] = {
                    "listing_id": lid, "shop_id": sid, "theme": theme, "ptype": ptype, "ltype": ltype,
                    "title": f"{theme.title()} " + tpl.format(t=theme.title()),
                    "price": price * rnd.uniform(0.8, 1.3), "taxonomy_id": tax, "created_day": created_day,
                    "quality": quality, "pod": pod, "personal": theme == "custom name" or rnd.random() < 0.1,
                    "base_fav": int(rnd.uniform(0, 400) * quality),
                }
                shop["listing_ids"].append(lid)
            self.shops[sid] = shop

    # -- dynamic values -------------------------------------------------
    def alive(self, L: dict, d: int) -> bool:
        return L["created_day"] <= d

    def favs(self, L: dict, d: int) -> int:
        start = max(L["created_day"], 0)
        hist = max(0, -L["created_day"]) * 0.3 * L["quality"] * 3
        return int(L["base_fav"] + hist + 6 * L["quality"] * cum_strength(L["theme"], start, d + 1))

    def views(self, L: dict, d: int) -> int:
        return self.favs(L, d) * 14 + int(L["quality"] * 50)

    def shop_sold(self, S: dict, d: int) -> int:
        daily = sum(0.08 * self.listings[l]["quality"] * strength(self.listings[l]["theme"], d)
                    for l in S["listing_ids"] if self.alive(self.listings[l], d))
        past = sum(0.08 * self.listings[l]["quality"] * strength(self.listings[l]["theme"], t)
                   for t in range(0, d) for l in S["listing_ids"][:60] if self.alive(self.listings[l], t))
        return S["base_sold"] + int(daily * d * 0.6 + past * 0.4) if d > 0 else S["base_sold"]

    def reviews(self, S: dict, d: int) -> list[dict]:
        out = []
        rnd = random.Random(S["shop_id"])
        for t in range(d, d - 40, -1):
            for l in S["listing_ids"]:
                L = self.listings[l]
                if not self.alive(L, t):
                    continue
                p = 0.02 * L["quality"] * strength(L["theme"], max(t, 0))
                if rnd.random() < p:
                    out.append({
                        "shop_id": S["shop_id"], "listing_id": l, "transaction_id": rnd.randint(1, 10**9),
                        "buyer_user_id": rnd.randint(1, 10**7), "rating": rnd.choice([5, 5, 5, 4]),
                        "review": "Love it", "language": "en",
                        "created_timestamp": day_ts(t) + rnd.randint(0, 80000),
                        "create_timestamp": day_ts(t), "updated_timestamp": day_ts(t),
                    })
        out.sort(key=lambda r: r["created_timestamp"], reverse=True)
        return out

    # -- API shapes -----------------------------------------------------
    def listing_json(self, L: dict, d: int, base_url: str, include_images: bool = False) -> dict:
        S = self.shops[L["shop_id"]]
        cur = S["currency_code"]
        amount = int(round(L["price"] * (0.93 if cur == "EUR" else 1.0) * 100))
        tags = [L["theme"], f"{L['theme']} {L['ptype'].lower()}"[:20], "gift for her", "trendy", "comfort colors"
                if L["pod"] else "handmade", "fall", "birthday gift", "unisex", "graphic tee", "cute gift",
                L["ptype"].lower(), "best seller", "for mom"]
        res = {
            "listing_id": L["listing_id"], "user_id": S["user_id"], "shop_id": S["shop_id"],
            "title": L["title"], "description": "Printed on a Comfort Colors 1717 garment-dyed tee." if L["pod"]
            else "Lovingly handmade.", "state": "active",
            "creation_timestamp": day_ts(L["created_day"]), "created_timestamp": day_ts(L["created_day"]),
            "original_creation_timestamp": day_ts(L["created_day"]),
            "last_modified_timestamp": day_ts(d), "updated_timestamp": day_ts(d),
            "quantity": 999, "url": f"https://www.etsy.com/listing/{L['listing_id']}/mock",
            "num_favorers": self.favs(L, d), "views": self.views(L, d),
            "is_customizable": False, "is_personalizable": L["personal"],
            "listing_type": L["ltype"], "tags": tags[:13], "materials": ["cotton"] if L["pod"] else ["silver"],
            "who_made": "i_did", "when_made": "made_to_order" if L["pod"] else "2020_2025",
            "is_supply": False, "taxonomy_id": 1069 if L["ptype"] in ("Sweatshirt", "T-shirt", "Hoodie") else 1062,
            "production_partners": ([{"production_partner_id": 1, "partner_name": "Printify", "location": "US"}]
                                    if L["pod"] and L["listing_id"] % 3 else []),
            "price": {"amount": amount, "divisor": 100, "currency_code": cur},
        }
        if include_images:
            res["images"] = [
                {"listing_image_id": L["listing_id"] * 10 + k, "rank": k + 1,
                 "url_570xN": f"{base_url}/img/{L['listing_id']}.jpg",
                 "url_fullxfull": f"{base_url}/img/{L['listing_id']}.jpg"} for k in range(7)
            ]
        return res

    def shop_json(self, S: dict, d: int) -> dict:
        sold = self.shop_sold(S, d)
        return {
            "shop_id": S["shop_id"], "shop_name": S["shop_name"], "user_id": S["user_id"],
            "created_timestamp": S["created_timestamp"], "currency_code": S["currency_code"],
            "url": f"https://www.etsy.com/shop/{S['shop_name']}", "num_favorers": sold // 7,
            "transaction_sold_count": sold, "review_count": int(sold * 0.15), "review_average": 4.8,
            "listing_active_count": sum(1 for l in S["listing_ids"] if self.alive(self.listings[l], d)),
            "is_vacation": False,
        }


WORLD = World()


def make_image(lid: int) -> bytes:
    from PIL import Image, ImageDraw

    L = WORLD.listings.get(lid)
    theme = L["theme"] if L else "unknown"
    h = abs(hash(theme)) % 255
    im = Image.new("RGB", (570, 456), (h, 255 - h, (h * 3) % 255))
    dr = ImageDraw.Draw(im)
    dr.text((30, 200), f"{theme}\n{L['ptype'] if L else ''}", fill="white")
    buf = io.BytesIO()
    im.save(buf, "JPEG")
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def _send(self, code: int, body, headers: dict | None = None, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        STATE["calls"] += 1
        self.send_header("x-limit-per-day", str(STATE["limit"]))
        self.send_header("x-remaining-today", str(max(0, STATE["limit"] - STATE["calls"])))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):  # noqa: C901
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        d = STATE["day"]
        base = f"http://{self.headers['Host']}"
        if u.path.startswith("/img/"):
            lid = int(re.findall(r"\d+", u.path)[0])
            return self._send(200, make_image(lid), ctype="image/jpeg")
        key = self.headers.get("x-api-key", "")
        if ":" not in key:
            return self._send(403, {"error": "Invalid API key: expected keystring:shared_secret"})
        if STATE["fail_every"] and (STATE["calls"] + 1) % STATE["fail_every"] == 0:
            return self._send(429, {"error": "slow down"}, {"retry-after": "0.05"})
        if (STATE["calls"] + 1) % 151 == 0:
            return self._send(503, {"error": "temporary"})
        p = u.path.replace("/v3/application", "")
        limit, offset = int(qs.get("limit", 25)), int(qs.get("offset", 0))

        if p == "/openapi-ping":
            return self._send(200, {"application_id": 1})
        if p == "/seller-taxonomy/nodes":
            return self._send(200, {"count": 2, "results": [
                {"id": 1, "name": "Clothing", "children": [{"id": 1069, "name": "T-shirts", "children": []}]},
                {"id": 2, "name": "Home & Living", "children": [{"id": 1062, "name": "Mugs", "children": []},
                                                                 {"id": 2078, "name": "Prints", "children": []}]},
            ]})
        if p == "/listings/active":
            words = set(re.findall(r"[a-z]+", qs.get("keywords", "").lower()))
            hits = []
            for L in WORLD.listings.values():
                if not WORLD.alive(L, d) or WORLD.shops[L["shop_id"]]["deleted_from"] is not None and d >= WORLD.shops[L["shop_id"]]["deleted_from"]:
                    continue
                tw = set(re.findall(r"[a-z]+", L["title"].lower()))
                overlap = len(words & tw)
                if overlap:
                    hits.append((overlap * L["quality"] * strength(L["theme"], d), L))
            hits.sort(key=lambda x: x[0], reverse=True)
            page = [WORLD.listing_json(L, d, base) for _, L in hits[offset:offset + limit]]
            return self._send(200, {"count": len(hits), "results": page})
        m = re.fullmatch(r"/shops/(\d+)", p)
        if m:
            S = WORLD.shops.get(int(m.group(1)))
            if not S or (S["deleted_from"] is not None and d >= S["deleted_from"]):
                return self._send(404, {"error": "Shop not found"})
            return self._send(200, WORLD.shop_json(S, d))
        if p == "/shops":
            name = qs.get("shop_name", "").lower()
            res = [WORLD.shop_json(S, d) for S in WORLD.shops.values() if S["shop_name"].lower() == name]
            return self._send(200, {"count": len(res), "results": res})
        m = re.fullmatch(r"/shops/(\d+)/listings/active", p)
        if m:
            S = WORLD.shops.get(int(m.group(1)))
            if not S:
                return self._send(404, {"error": "Shop not found"})
            ls = [WORLD.listings[l] for l in S["listing_ids"] if WORLD.alive(WORLD.listings[l], d)]
            ls.sort(key=lambda L: L["created_day"], reverse=True)
            page = [WORLD.listing_json(L, d, base) for L in ls[offset:offset + limit]]
            return self._send(200, {"count": len(ls), "results": page})
        m = re.fullmatch(r"/shops/(\d+)/reviews", p)
        if m:
            S = WORLD.shops.get(int(m.group(1)))
            if not S:
                return self._send(404, {"error": "Shop not found"})
            revs = WORLD.reviews(S, d)
            return self._send(200, {"count": len(revs), "results": revs[offset:offset + limit]})
        if p == "/listings/batch":
            ids = [int(x) for x in qs.get("listing_ids", "").split(",") if x]
            if len(ids) > 100:
                return self._send(400, {"error": "too many ids"})
            inc = "Images" in qs.get("includes", "")
            res = [WORLD.listing_json(WORLD.listings[i], d, base, include_images=inc)
                   for i in ids if i in WORLD.listings and WORLD.alive(WORLD.listings[i], d)]
            return self._send(200, {"count": len(res), "results": res})
        return self._send(404, {"error": f"unknown path {p}"})


def serve(port: int = 0) -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]
