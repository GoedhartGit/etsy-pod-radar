"""Writes the daily outputs: feed/latest.json (for the design bot), thumbnails, contact sheet, markdown report."""
from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path

import requests

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _num(x, nd=0) -> str:
    if x is None:
        return "–"
    s = f"{x:,.{nd}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def _money(x) -> str:
    return f"${_num(x, 2)}" if isinstance(x, (int, float)) else "–"


def _cut(s: str | None, n: int) -> str:
    s = (s or "").replace("|", "/").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


# ------------------------------------------------------------------ images
def download_thumbs(out_dir: Path, items: list[dict], timeout: int = 20, clear: bool = True) -> dict[int, str]:
    """Download first images (570px) for the given listings. Returns listing_id -> relative path."""
    thumbs = out_dir / "feed" / "thumbs"
    if clear and thumbs.exists():
        shutil.rmtree(thumbs)
    thumbs.mkdir(parents=True, exist_ok=True)
    sess = requests.Session()
    sess.headers["User-Agent"] = "Mozilla/5.0 (etsy-pod-radar)"
    done: dict[int, str] = {}
    for it in items:
        lid, url = it.get("listing_id"), it.get("image_url")
        if not lid or not url or lid in done:
            continue
        try:
            resp = sess.get(url, timeout=timeout)
            if resp.status_code == 200 and resp.content[:2] in (b"\xff\xd8", b"\x89P", b"RI", b"GI"):
                path = thumbs / f"{lid}.jpg"
                path.write_bytes(resp.content)
                done[lid] = f"feed/thumbs/{lid}.jpg"
        except requests.RequestException as exc:
            log.info("Thumbnail %s mislukt: %s", lid, exc)
    return done


def contact_sheet(out_dir: Path, items: list[dict], thumbs: dict[int, str], cols: int = 4, rows: int = 3,
                  cell: int = 390, max_items: int = 24) -> list[str]:
    """Grid images with numbered thumbnails (12 per sheet), so a few image reads show all top designs."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return []
    for old in (out_dir / "feed").glob("contact_sheet*.jpg"):
        old.unlink()
    entries = [(i, it) for i, it in enumerate(items, 1) if it.get("listing_id") in thumbs][:max_items]
    if not entries:
        return []
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 22)
        small = ImageFont.truetype("DejaVuSans.ttf", 16)
    except OSError:
        font = small = ImageFont.load_default()
    per = cols * rows
    label_h = 36
    paths = []
    for s_idx in range(0, len(entries), per):
        chunk = entries[s_idx:s_idx + per]
        n_rows = (len(chunk) + cols - 1) // cols
        sheet = Image.new("RGB", (cols * cell, n_rows * (cell + label_h)), "white")
        draw = ImageDraw.Draw(sheet)
        for k, (num, it) in enumerate(chunk):
            x, y = (k % cols) * cell, (k // cols) * (cell + label_h)
            try:
                im = Image.open(out_dir / thumbs[it["listing_id"]]).convert("RGB")
                im.thumbnail((cell - 6, cell - 6))
                sheet.paste(im, (x + (cell - im.width) // 2, y + (cell - im.height) // 2))
            except Exception:  # noqa: BLE001 - a broken image must not kill the run
                pass
            draw.rectangle([x, y + cell, x + cell, y + cell + label_h], fill=(25, 25, 25))
            draw.text((x + 8, y + cell + 6), f"#{num}", fill=(255, 210, 0), font=font)
            draw.text((x + 70, y + cell + 10), _cut(it.get("product_type"), 16) + f" · {_num(it.get('momentum'), 1)}",
                      fill="white", font=small)
        name = f"feed/contact_sheet_{s_idx // per + 1}.jpg"
        sheet.save(out_dir / name, "JPEG", quality=85)
        paths.append(name)
    return paths


# ------------------------------------------------------------------- feed
def write_feed(out_dir: Path, result: dict, run_info: dict, thumbs: dict[int, str], sheets: list[str],
               brief_thumbs: dict[int, str] | None = None) -> Path:
    feed_dir = out_dir / "feed"
    (feed_dir / "history").mkdir(parents=True, exist_ok=True)

    def with_thumb(items):
        out = []
        for it in items:
            it = dict(it)
            it["thumb"] = thumbs.get(it["listing_id"])
            out.append(it)
        return out

    briefs = []
    for b in result["briefs"]:
        b = json.loads(json.dumps(b))
        for ex in b["example_listings"]:
            ex["thumb"] = (brief_thumbs or {}).get(ex["listing_id"]) or thumbs.get(ex["listing_id"])
        briefs.append(b)

    feed = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": result["date"],
        "data_days": result["data_days"],
        "phase": "opbouw" if result["data_days"] < 7 else "stabiel",
        "momentum_basis": result["momentum_basis"],
        "run": run_info,
        "summary": result["summary"],
        "advice": result["advice"],
        "counts": result["counts"],
        "top_shops": result["top_shops"],
        "top_listings": with_thumb(result["top_listings"]),
        "new_winners": with_thumb(result["new_winners"]),
        "trending_terms": result["trending_terms"],
        "rising_terms": result["rising_terms"],
        "product_mix": result["product_mix"],
        "personalization": result["personalization"],
        "listing_hygiene": result["listing_hygiene"],
        "production_partners_top": result["production_partners_top"],
        "ip_flagged_in_top": result["ip_flagged_in_top"],
        "ip_risk_top": result["ip_risk_top"],
        "new_in_top": result["new_in_top"],
        "briefs": briefs,
        "contact_sheets": sheets,
        "contact_sheet_legend": "Nummers op de contactvellen = positie in top_listings (1 = hoogste momentum). "
                                "Vel 1 = #1-12, vel 2 = #13-24.",
        "field_notes": {
            "momentum": "0-100; percentielmix van favorieten-groei, views-groei, geschatte verkopen (reviews), "
                        "favorieten per dag sinds publicatie en zoekpositie.",
            "est_sales_30d": "Schatting: reviews laatste 30 dagen / reviewratio van de shop. Indicatief, geen exacte verkopen.",
            "sales_per_day (shop)": "Gemeten via dagelijkse stijging van transaction_sold_count; in opbouwfase het levensduur-gemiddelde.",
            "price_usd": "Omgerekend met vaste koersen uit config.yaml.",
        },
    }
    latest = feed_dir / "latest.json"
    latest.write_text(json.dumps(feed, ensure_ascii=False, indent=1, default=str))
    (feed_dir / "history" / f"{result['date']}.json").write_text(
        json.dumps({k: v for k, v in feed.items() if k not in ("top_listings",)}, ensure_ascii=False, default=str)
    )
    return latest


# ----------------------------------------------------------------- report
def write_report(out_dir: Path, result: dict, run_info: dict, thumbs: dict[int, str], sheets: list[str]) -> Path:
    L: list[str] = []
    d = result["date"]
    L.append(f"# Etsy POD Radar — {d}\n")
    phase = "opbouwfase" if result["data_days"] < 7 else "stabiel"
    L.append(f"_Dag {result['data_days']} van de meting ({phase}) · {result['counts']['shops_tracked']} shops · "
             f"{_num(result['counts']['listings_analyzed'])} POD-listings geanalyseerd · momentum: {result['momentum_basis']}_\n")

    L.append("## Samenvatting\n")
    L += [f"- {s}" for s in result["summary"]]
    L.append("")

    L.append("## Advies\n")
    L += [f"- {s}" for s in result["advice"]]
    L.append("")

    L.append("## Top POD-shops\n")
    L.append("| # | Shop | Verkopen/dag | 7 dagen | Totaal verkocht | Actieve listings | Nieuw (7d) | Mediaanprijs | Omzet/dag (schatting) |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for s in result["top_shops"][:20]:
        L.append(
            f"| {s['rank']} | [{_cut(s['shop_name'], 28)}]({s['url']}) | {_num(s['sales_per_day'], 1)}"
            f"{'' if s['sales_basis'] == 'gemeten' else '*'} | {_num(s['sales_7d'])} | {_num(s['sold_total'])} | "
            f"{_num(s['active_listings'])} | {s['new_listings_7d']} | {_money(s['median_price_usd'])} | "
            f"{('$' + _num(s['est_revenue_per_day_usd'])) if s['est_revenue_per_day_usd'] else '–'} |"
        )
    L.append("\n_* = levensduur-gemiddelde (nog geen meetreeks)._\n")

    L.append("## Best lopende listings (top 25, zonder IP-risico)\n")
    L.append("| # | Listing | Shop | Product | Prijs | Leeftijd | Fav/dag | Gesch. verkopen 30d | Momentum |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for i, m in enumerate(result["top_listings"][:25], 1):
        fpd = m["fav_per_day_recent"] if m["fav_per_day_recent"] is not None else m["fav_per_day_life"]
        flag = " ⚠️IP" if m["ip_risk"] else ""
        L.append(
            f"| {i} | [{_cut(m['title'], 60)}]({m['url']}){flag} | {_cut(m['shop_name'], 20)} | {m['product_type']} | "
            f"{_money(m['price_usd'])} | {m['age_days']}d | {_num(fpd, 1)} | {_num(m['est_sales_30d'])} | {_num(m['momentum'], 1)} |"
        )
    L.append("")

    if result.get("ip_risk_top"):
        L.append(f"_{len(result['ip_risk_top'])} toppers met merk-/fandomnamen zijn hierboven weggelaten (IP-risico), "
                 f"o.a.: " + ", ".join(sorted({x['ip_term'] for x in result['ip_risk_top']})) + "._\n")

    if result["new_winners"]:
        L.append("## Nieuwe winnaars (≤ 30 dagen oud)\n")
        for m in result["new_winners"][:10]:
            fpd = m["fav_per_day_recent"] if m["fav_per_day_recent"] is not None else m["fav_per_day_life"]
            L.append(f"- [{_cut(m['title'], 80)}]({m['url']}) — {m['shop_name']}, {m['product_type']}, "
                     f"{_money(m['price_usd'])}, {m['age_days']} dagen, {_num(fpd, 1)} fav/dag, momentum {_num(m['momentum'], 1)}")
        L.append("")

    L.append("## Thema's die werken\n")
    L.append("| Thema | Lift | In top | Totaal |")
    L.append("|---|---|---|---|")
    for t in result["trending_terms"][:20]:
        L.append(f"| {t['term']} | {_num(t['lift'], 2)}x | {t['top_count']} | {t['all_count']} |")
    L.append("\n_Lift = hoe veel vaker het thema voorkomt bij de toppers dan gemiddeld._\n")

    if result["rising_terms"]:
        L.append("## Snelst stijgend (week op week)\n")
        for t in result["rising_terms"][:12]:
            L.append(f"- **{t['term']}** — x{_num(t['growth'], 2)} ({t['fav_prev_7d']} → {t['fav_last_7d']} nieuwe favorieten, "
                     f"{t['listings']} listings)")
        L.append("")

    L.append("## Productmix en prijzen bij de toppers\n")
    L.append("| Product | Aandeel top | Aandeel totaal | Lift | Prijs p25 | Mediaan | p75 |")
    L.append("|---|---|---|---|---|---|---|")
    for m in result["product_mix"][:12]:
        q = m["price_top_usd"]
        L.append(f"| {m['product_type']} | {m['share_top']:.0%} | {m['share_all']:.0%} | {_num(m['lift'], 2)}x | "
                 f"{_money(q['p25'])} | {_money(q['median'])} | {_money(q['p75'])} |")
    p = result["personalization"]
    if p["share_top"] is not None:
        L.append(f"\nPersonaliseerbaar: {p['share_top']:.0%} van de top vs {p['share_all']:.0%} van alles.")
    if result["production_partners_top"]:
        L.append("Productiepartners bij toppers: " + ", ".join(f"{n} ({c})" for n, c in result["production_partners_top"]) + ".")
    L.append("")

    L.append("## Design briefs voor de designbot\n")
    for b in result["briefs"]:
        q = b["price_usd"]
        L.append(f"### {b['brief_id']} · {b['product_type']} · \"{b['theme']}\"  (zekerheid {b['confidence']:.0%})")
        L.append(f"- Richting: {b['design_direction']}")
        L.append(f"- Bewijs: {b['evidence']['listings']} listings bij {b['evidence']['shops']} shops, "
                 f"~{_num(b['evidence']['est_sales_30d'])} geschatte verkopen/30d, gem. momentum {_num(b['evidence']['avg_momentum'], 1)}")
        L.append(f"- Prijs: {_money(q['p25'])} – {_money(q['p75'])} (mediaan {_money(q['median'])}); "
                 f"personalisatie {b['personalization_share']:.0%}")
        L.append(f"- Tags: {', '.join(b['recommended_tags'])}")
        L.append("- Voorbeelden: " + "; ".join(f"[{_cut(e['title'], 50)}]({e['url']})" for e in b["example_listings"]))
        L.append("")

    L.append("## Data en kwaliteit\n")
    L.append(f"- API-calls vandaag: {run_info.get('api_calls')} (fouten: {run_info.get('api_errors')}), "
             f"resterend dagquotum: {run_info.get('quota_remaining', '?')}")
    L.append(f"- Thumbnails: {len(thumbs)}" + (f" · contactvellen: {', '.join(sheets)}" if sheets else ""))
    for n in run_info.get("notes", []):
        L.append(f"- ⚠️ {n}")
    L.append("- Verkopen per listing zijn schattingen op basis van reviews; shopverkopen zijn gemeten.")
    L.append("")

    text = "\n".join(L)
    rep_dir = out_dir / "reports"
    rep_dir.mkdir(parents=True, exist_ok=True)
    (rep_dir / f"{d}.md").write_text(text)
    (rep_dir / "latest.md").write_text(text)
    return rep_dir / f"{d}.md"
