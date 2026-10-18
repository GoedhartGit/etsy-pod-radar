"""Minimal Etsy Open API v3 client (public endpoints only, API key auth)."""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterable

import requests

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.etsy.com/v3/application"


class EtsyError(RuntimeError):
    pass


class QuotaExhausted(EtsyError):
    pass


def build_api_key() -> str:
    """Etsy expects `x-api-key: <keystring>:<shared_secret>`.

    Accepts ETSY_API_KEY already in that form, or ETSY_API_KEY (keystring) + ETSY_SHARED_SECRET.
    """
    key = (os.environ.get("ETSY_API_KEY") or "").strip()
    secret = (os.environ.get("ETSY_SHARED_SECRET") or "").strip()
    if not key:
        raise EtsyError(
            "ETSY_API_KEY ontbreekt. Zet hem als GitHub-secret (formaat: keystring:shared_secret)."
        )
    if ":" not in key and secret:
        key = f"{key}:{secret}"
    return key


class EtsyClient:
    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        requests_per_second: float = 4.0,
        quota_reserve: int = 300,
        session: requests.Session | None = None,
    ):
        self.base_url = (base_url or os.environ.get("ETSY_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.session = session or requests.Session()
        self.session.headers.update(
            {"x-api-key": api_key, "Accept": "application/json", "User-Agent": "etsy-pod-radar/1.0"}
        )
        self.min_interval = 1.0 / max(requests_per_second, 0.1)
        self.quota_reserve = quota_reserve
        self._last_call = 0.0
        self.calls = 0
        self.errors = 0
        self.remaining_today: int | None = None
        self.limit_per_day: int | None = None

    # ------------------------------------------------------------------ core
    def _get(self, path: str, params: dict[str, Any] | None = None, optional: bool = False) -> Any:
        """GET with throttling, retries and quota tracking.

        optional=True: skip (return None) when the daily quota is nearly used up.
        """
        if optional and self.remaining_today is not None and self.remaining_today < self.quota_reserve:
            raise QuotaExhausted(f"Etsy-quotum bijna op ({self.remaining_today} over)")
        params = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{self.base_url}{path}"
        for attempt in range(6):
            wait = self.min_interval - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            try:
                resp = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as exc:  # network hiccup
                log.warning("Netwerkfout %s (poging %d): %s", path, attempt + 1, exc)
                time.sleep(2 ** attempt)
                continue
            self.calls += 1
            self._track_quota(resp)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                retry = float(resp.headers.get("retry-after") or 2 ** attempt)
                if self.remaining_today is not None and self.remaining_today <= 0:
                    raise QuotaExhausted("Etsy-daglimiet bereikt")
                log.warning("429 rate limit op %s, wacht %.1fs", path, retry)
                time.sleep(min(retry, 60))
                continue
            if resp.status_code >= 500:
                log.warning("Etsy %s op %s (poging %d)", resp.status_code, path, attempt + 1)
                time.sleep(2 ** attempt)
                continue
            if resp.status_code in (401, 403):
                raise EtsyError(
                    f"Etsy weigert de API-key ({resp.status_code}): {resp.text[:300]}. "
                    "Controleer of je app is goedgekeurd en het secret het formaat keystring:shared_secret heeft."
                )
            # 400/404 etc: caller decides
            self.errors += 1
            log.info("Etsy %s op %s: %s", resp.status_code, path, resp.text[:200])
            return None
        self.errors += 1
        raise EtsyError(f"Etsy bleef falen op {path}")

    def _track_quota(self, resp: requests.Response) -> None:
        rem = resp.headers.get("x-remaining-today")
        lim = resp.headers.get("x-limit-per-day")
        if rem is not None:
            try:
                self.remaining_today = int(rem)
            except ValueError:
                pass
        if lim is not None:
            try:
                self.limit_per_day = int(lim)
            except ValueError:
                pass

    # ------------------------------------------------------------- endpoints
    def ping(self) -> Any:
        return self._get("/openapi-ping")

    def search_listings(self, keywords: str, limit: int = 100, offset: int = 0, sort_on: str = "score",
                        optional: bool = False) -> list[dict]:
        data = self._get(
            "/listings/active",
            {"keywords": keywords, "limit": limit, "offset": offset, "sort_on": sort_on, "sort_order": "desc"},
            optional=optional,
        )
        return (data or {}).get("results", []) or []

    def get_shop(self, shop_id: int, optional: bool = False) -> dict | None:
        return self._get(f"/shops/{shop_id}", optional=optional)

    def find_shops(self, shop_name: str) -> list[dict]:
        data = self._get("/shops", {"shop_name": shop_name, "limit": 25})
        return (data or {}).get("results", []) or []

    def shop_active_listings(self, shop_id: int, limit: int = 100, offset: int = 0, sort_on: str = "created",
                             optional: bool = False) -> list[dict]:
        data = self._get(
            f"/shops/{shop_id}/listings/active",
            {"limit": limit, "offset": offset, "sort_on": sort_on, "sort_order": "desc"},
            optional=optional,
        )
        return (data or {}).get("results", []) or []

    def shop_reviews(self, shop_id: int, limit: int = 100, offset: int = 0, optional: bool = False) -> list[dict]:
        data = self._get(f"/shops/{shop_id}/reviews", {"limit": limit, "offset": offset}, optional=optional)
        return (data or {}).get("results", []) or []

    def listings_batch(self, listing_ids: Iterable[int], includes: str | None = None,
                       optional: bool = False) -> list[dict]:
        ids = [str(i) for i in listing_ids]
        if not ids:
            return []
        if len(ids) > 100:
            raise ValueError("listings_batch accepteert maximaal 100 ids")
        data = self._get(
            "/listings/batch", {"listing_ids": ",".join(ids), "includes": includes}, optional=optional
        )
        return (data or {}).get("results", []) or []

    def seller_taxonomy(self) -> list[dict]:
        data = self._get("/seller-taxonomy/nodes")
        return (data or {}).get("results", []) or []
