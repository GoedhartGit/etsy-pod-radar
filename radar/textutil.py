"""Text helpers: product-type detection, POD detection, theme extraction, IP-risk check."""
from __future__ import annotations

import html
import re
from functools import lru_cache

# Ordered: on equal position the first pattern wins. The earliest match in the title wins overall,
# because Etsy sellers put the primary product first ("Retro Halloween Sweatshirt, Ghost Tee, ...").
PRODUCT_PATTERNS: list[tuple[str, str]] = [
    ("Hoodie", r"\bhood(?:ie|ies|ed sweatshirts?)\b"),
    ("Sweatshirt", r"\b(?:sweatshirts?|crewnecks?|crew neck|sweaters?|pullovers?)\b"),
    ("Baby bodysuit", r"\b(?:onesies?|bodysuits?|baby rompers?)\b"),
    ("Tank top", r"\btank(?: tops?)?s?\b"),
    ("T-shirt", r"\b(?:tshirts?|t shirts?|tees?|shirts?)\b"),
    ("Mug", r"\b(?:mugs?|coffee cups?|tea cups?)\b"),
    ("Tumbler", r"\b(?:tumblers?|water bottles?)\b"),
    ("Wall art", r"\b(?:posters?|wall art|art prints?|canvas prints?|wall decor|wall hangings?|framed prints?|giclee)\b"),
    ("Tote bag", r"\b(?:tote ?bags?|totes?)\b"),
    ("Sticker", r"\b(?:stickers?|decals?)\b"),
    ("Phone case", r"\b(?:phone cases?|iphone cases?|samsung cases?|magsafe case)\b"),
    ("Hat", r"\b(?:hats?|caps?|beanies?|truckers?)\b"),
    ("Pillow", r"\b(?:pillows?|cushions?)\b"),
    ("Blanket", r"\b(?:blankets?|throws?)\b"),
    ("Ornament", r"\bornaments?\b"),
    ("Doormat", r"\b(?:doormats?|door mats?)\b"),
    ("Socks", r"\bsocks\b"),
    ("Apron", r"\baprons?\b"),
    ("Pouch", r"\b(?:pouch(?:es)?|makeup bags?|cosmetic bags?)\b"),
    ("Mousepad", r"\bmouse ?pads?\b"),
    ("Puzzle", r"\bpuzzles?\b"),
    ("Canvas", r"\bcanvas\b"),
]
_PRODUCT_RX = [(name, re.compile(rx)) for name, rx in PRODUCT_PATTERNS]

POD_PRODUCT_TYPES = {name for name, _ in PRODUCT_PATTERNS}

POD_MARKERS = [
    "comfort colors", "bella canvas", "bella+canvas", "bella + canvas", "gildan", "next level",
    "printify", "printful", "gelato", "dtg", "direct to garment", "heavy cotton", "ultra cotton",
    "softstyle", "1717", "3001", "18000", "64000", "11oz", "15oz", "11 oz", "15 oz", "sublimation",
    "unisex jersey", "garment dyed", "dtf", "made to order", "printed on demand", "print on demand",
]

STOPWORDS = set(
    """a an and are as at be but by for from has have i if in into is it its me my of on or our
    so than that the their them then there these they this to too was we were what when which who
    with you your yours u ur her him his he she hers them""".split()
)

# Words that describe the product/format, not the design theme.
PRODUCT_WORDS = set(
    """shirt shirts tshirt tshirts tee tees sweatshirt sweatshirts hoodie hoodies crewneck crewnecks crew
    neck sweater sweaters pullover top tops tank tanks mug mugs cup cups tumbler tumblers poster posters
    print prints printed canvas tote totes bag bags sticker stickers decal decals case cases phone iphone
    samsung hat hats cap caps beanie beanies pillow pillows cushion blanket blankets throw ornament
    ornaments onesie onesies bodysuit bodysuits romper doormat doormats mat socks apron aprons pouch
    unisex womens women's women mens men's men ladies adult adults comfort colors colored bella gildan
    gift gifts present presents idea ideas custom customized customizable personalized personalised
    personalization design designs graphic graphics trendy cute oversized size sizes plus coffee
    tshirt oz 11oz 15oz 20oz ceramic cotton soft vintage-wash garment dyed wash dtg sweatshirts
    wall art decor framed frame unframed giclee hooded trucker""".split()
)

GENERIC_TERMS = {
    "", "for her", "for him", "her", "him", "gift for her", "gift for him", "best", "new", "funny gift",
    "birthday gift", "christmas gift", "holiday gift", "s", "m", "l", "xl", "2xl", "3xl", "t",
}

_WORD_RX = re.compile(r"[a-z0-9][a-z0-9'&+]*")
_SPLIT_RX = re.compile(r"\s*(?:[,|/–—•·;:!()\[\]]+|\s-\s)\s*")


@lru_cache(maxsize=50000)
def normalize(text: str | None) -> str:
    if not text:
        return ""
    t = html.unescape(text).lower()
    t = re.sub(r"\bt[- ]?shirt", "tshirt", t.replace("’", "'"))
    return re.sub(r"\s+", " ", t).strip()


def detect_product_type(title: str, taxonomy_name: str | None = None) -> str:
    t = normalize(title)
    best: tuple[int, int, str] | None = None
    for order, (name, rx) in enumerate(_PRODUCT_RX):
        m = rx.search(t)
        if m and (best is None or (m.start(), order) < best[:2]):
            best = (m.start(), order, name)
    if best:
        return best[2]
    if taxonomy_name:
        tx = normalize(taxonomy_name)
        for name, rx in _PRODUCT_RX:
            if rx.search(tx):
                return name
        return taxonomy_name
    return "Overig"


def pod_score(listing: dict, product_type: str) -> int:
    """Heuristic: >= 2 means very likely print-on-demand."""
    if (listing.get("listing_type") or "physical") == "download":
        return -5
    score = 0
    if listing.get("production_partners"):
        score += 2
    blob = " ".join(
        [
            normalize(listing.get("title")),
            " ".join(normalize(x) for x in (listing.get("tags") or [])),
            " ".join(normalize(x) for x in (listing.get("materials") or [])),
            normalize((listing.get("description") or "")[:3000]),
        ]
    )
    if any(m in blob for m in POD_MARKERS):
        score += 1
    if product_type in POD_PRODUCT_TYPES:
        score += 1
    if listing.get("when_made") == "made_to_order":
        score += 1
    return score


_PRODUCT_PREFIX_WORDS = sorted(w for w in PRODUCT_WORDS if len(w) >= 3)


def _truncated_product_word(w: str) -> bool:
    """Etsy tags are max 20 chars, so they often end in a cut-off word like 'hoo' or 'sweat'."""
    return any(pw.startswith(w) and pw != w for pw in _PRODUCT_PREFIX_WORDS)


def _clean_phrase(words: list[str], from_tag: bool = False) -> str:
    while from_tag and words and (len(words[-1]) <= 1 or _truncated_product_word(words[-1])):
        words = words[:-1]
    words = [w for w in words if w not in PRODUCT_WORDS]
    while words and words[0] in STOPWORDS:
        words.pop(0)
    while words and words[-1] in STOPWORDS:
        words.pop()
    if not words or len(words) > 4:
        return ""
    phrase = " ".join(words)
    if phrase in GENERIC_TERMS or all(w in STOPWORDS for w in words) or len(phrase) < 3:
        return ""
    return phrase


def theme_terms(title: str, tags: list[str] | None) -> set[str]:
    """Theme terms of a listing: cleaned title segments, cleaned tags, title unigrams and bigrams."""
    terms: set[str] = set()
    t = normalize(title)
    for seg in _SPLIT_RX.split(t):
        words = _WORD_RX.findall(seg)
        p = _clean_phrase(words)
        if p:
            terms.add(p)
        core = [w for w in words if w not in PRODUCT_WORDS and w not in STOPWORDS and len(w) > 2]
        terms.update(w for w in core if w not in GENERIC_TERMS)
        for a, b in zip(core, core[1:]):
            terms.add(f"{a} {b}")
    for tag in tags or []:
        p = _clean_phrase(_WORD_RX.findall(normalize(tag)), from_tag=True)
        if p:
            terms.add(p)
    return terms


def clean_tags(tags: list[str] | None) -> list[str]:
    return [normalize(t) for t in (tags or []) if normalize(t)]


def build_ip_regex(blocklist: list[str]) -> re.Pattern | None:
    words = [re.escape(normalize(w)) for w in blocklist if w and w.strip()]
    if not words:
        return None
    return re.compile(r"\b(" + "|".join(sorted(words, key=len, reverse=True)) + r")\b")


def ip_risk(rx: re.Pattern | None, title: str, tags: list[str] | None) -> str | None:
    if rx is None:
        return None
    blob = normalize(title) + " | " + " | ".join(normalize(t) for t in (tags or []))
    m = rx.search(blob)
    return m.group(1) if m else None
