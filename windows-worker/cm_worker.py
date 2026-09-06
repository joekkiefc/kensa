"""Kensa Cardmarket-worker — draait silent in de achtergrond op Windows.

Zie README.md voor volledige spec + Task Scheduler setup.

Kernfunctionaliteit:
    - Poll Pi (via Tailscale) voor pending Cardmarket-URLs
    - Scrape elke URL met Scrapling (solve_cloudflare=True)
    - Filter listings met 'PSA10' of 'PSA 10' in description
    - Pak 3 goedkoopste, sorteer op prijs
    - POST resultaat terug naar Pi

Log gaat naar C:\\kensa\\worker.log (rotating 5 MB × 3 files).
Geen console-output — zichtbaar met `python` (foreground), silent met `pythonw.exe`.

Config: pas PI_URL / LOG_PATH aan onderaan indien nodig.
"""

import json
import logging
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from scrapling.fetchers import StealthyFetcher

# ── config ──────────────────────────────────────────────────────────────
PI_URL              = "http://100.74.4.23:8899"
POLL_INTERVAL_SEC   = 60      # bij lege queue
BETWEEN_SCRAPES_SEC = 4       # respectvolle pauze
MAX_LISTINGS        = 3       # top-N goedkoopste
LOG_PATH            = Path(__file__).with_name("worker.log")

OTHER_GRADERS_RE = re.compile(r"\b(BGS|CGC|SGC|CGS|HGA|MPG)\b", re.IGNORECASE)

# "Possible PSA 10" / "PSA 10 candidate" / "gradeable" / "raw" listings zijn GÉÉN echte PSA-slabs
# — sellers claimen alleen dat de kaart "waarschijnlijk" die grade zou halen. Uitsluiten.
NOT_GRADED_RE = re.compile(
    r"\b(possible|potential|candidate|worthy|gradeable|gradable|ungraded|raw|near\s*mint|pack\s*fresh|"
    r"would\s*grade|will\s*grade|could\s*grade|should\s*grade|"
    r"psa\s*ready|psa[- ]?worthy|psa[- ]?candidate|psa[- ]?potential|psa[- ]?possible|"
    r"gem\s*mt\s*candidate)\b",
    re.IGNORECASE,
)


def _psa_regex(grade: str) -> "re.Pattern":
    """Return regex die matcht op 'PSA <grade>' — bv 'PSA 10', 'PSA 9', 'PSA9.5'.
    Grade wordt geescaped zodat '9.5' correct '9\\.5' wordt."""
    return re.compile(rf"\bPSA\s*{re.escape(grade)}\b(?!\d)", re.IGNORECASE)


# ── logging (file only, geen stdout, silent voor Task Scheduler) ──────
log = logging.getLogger("kensa-cm")
log.setLevel(logging.INFO)
_h = RotatingFileHandler(str(LOG_PATH), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-5s  %(message)s"))
log.addHandler(_h)


# ── prijs parsen (EU + US formats) ────────────────────────────────────
def _parse_price(raw: str) -> float | None:
    m = re.search(r"([\d,\.]+)\s*€", raw or "")
    if not m:
        return None
    s = m.group(1)
    if "," in s and "." in s:
        # 1.234,56 (EU) of 1,234.56 (US) — laatste is decimaal separator
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        s = s.replace(",", ".") if (len(parts) == 2 and len(parts[1]) == 2) else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


# ── scrape ────────────────────────────────────────────────────────────
# Default: 1 page (originele gedrag, snel + goedkoop).
# Per URL-substring een override zetten voor kaarten waarvan we WETEN dat
# de PSA10-slabs pas op page 2+ staan (zeer liquide items waar CM eerst
# tientallen raw MT-listings toont).
DEFAULT_MAX_PAGES = 1
PAGINATE_OVERRIDES: list[tuple[str, int]] = [
    # (URL-substring, max_pages) — case-insensitive contains
    ("Pikachu-M-P020", 5),      # Pikachu M-P020, meest gegrade kaart ooit
]


def _max_pages_for(url: str) -> int:
    """Return max pages voor deze URL — 1 tenzij er een override matcht."""
    low = (url or "").lower()
    for needle, n in PAGINATE_OVERRIDES:
        if needle.lower() in low:
            return n
    return DEFAULT_MAX_PAGES


def _page_url(base_url: str, page_num: int) -> str:
    """Voeg &site=N toe (CM's pagination-param). Vervang bestaande site=N als aanwezig."""
    if "site=" in base_url:
        return re.sub(r"([?&])site=\d+", rf"\1site={page_num}", base_url)
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}site={page_num}"


def _parse_page(html: str, grade: str) -> list[dict]:
    """Extract PSA<grade> listings uit één page's HTML."""
    psa_re = _psa_regex(grade)
    soup = BeautifulSoup(html, "html.parser")
    blocks = soup.select("div.article-row")
    hits: list[dict] = []
    for block in blocks:
        txt = block.get_text(" ", strip=True)
        if not psa_re.search(txt) or OTHER_GRADERS_RE.search(txt) or NOT_GRADED_RE.search(txt):
            continue
        price = _parse_price(block.select_one("span.color-primary").get_text(strip=True)
                             if block.select_one("span.color-primary") else "")
        if price is None:
            continue
        seller_el = block.select_one("a.seller-name, span.seller-name, .seller-info a")
        seller = seller_el.get_text(strip=True) if seller_el else None
        info_el = (block.select_one(".product-info, .article-info, .comments, .col-comments")
                   or block.select_one(".product-attributes"))
        raw_info = info_el.get_text(" ", strip=True) if info_el else txt
        if psa_re.search(raw_info):
            desc = raw_info[:200]
        else:
            desc = re.sub(r"\s+", " ", txt)[:200]
        hits.append({"seller": seller, "price_eur": price, "description": desc})
    return hits


def scrape_cardmarket(url: str, grade: str = "10") -> list[dict]:
    """Return top-N goedkoopste PSA <grade> listings. Doorpageert tot cap
    OF genoeg hits, want CM sorteert op prijs (raw MT vooraan, PSA10 later).
    Raise op fetch-fail van page 1; latere page-fails worden gelogd en genegeerd."""
    all_hits: list[dict] = []
    page1_blocks = 0
    max_pages = _max_pages_for(url)
    for page_num in range(1, max_pages + 1):
        page_url = _page_url(url, page_num)
        try:
            page = StealthyFetcher.fetch(
                page_url, headless=True, network_idle=True, solve_cloudflare=True,
            )
        except Exception as e:
            if page_num == 1:
                raise
            log.warning("  page%d fetch faal (%s), stop paginate", page_num, e)
            break

        if not page or page.status != 200:
            if page_num == 1:
                raise RuntimeError(f"HTTP {getattr(page, 'status', '?')}")
            log.warning("  page%d HTTP %s, stop paginate", page_num, getattr(page, 'status', '?'))
            break

        html = page.html_content or ""
        if "Just a moment" in html:
            if page_num == 1:
                raise RuntimeError("cloudflare block after solve")
            log.warning("  page%d cloudflare block, stop paginate", page_num)
            break

        page_hits = _parse_page(html, grade)
        all_hits.extend(page_hits)

        # Detecteer "einde van listings": als deze page 0 article-rows heeft OF
        # significant minder dan page 1, dan is er niks meer om te pagineren.
        soup = BeautifulSoup(html, "html.parser")
        n_blocks = len(soup.select("div.article-row"))
        if page_num == 1:
            page1_blocks = n_blocks
        elif n_blocks == 0 or n_blocks < page1_blocks // 2:
            log.info("  page%d slechts %d blocks (page1=%d) — einde bereikt",
                     page_num, n_blocks, page1_blocks)
            break

        # Early exit: als we al genoeg PSA<grade> hebben, geen extra pages
        if len(all_hits) >= MAX_LISTINGS:
            log.info("  page%d: al %d PSA%s hits, klaar", page_num, len(all_hits), grade)
            break

    all_hits.sort(key=lambda h: h["price_eur"])
    return all_hits[:MAX_LISTINGS]


# ── job runner ────────────────────────────────────────────────────────
def process_job(item_id: str, url: str, grade: str = "10") -> None:
    log.info("scrape %s  PSA%s  %s", item_id, grade, url[:80])
    try:
        listings = scrape_cardmarket(url, grade=grade)
        log.info("  → %d listings (PSA %s)", len(listings), grade)
        r = requests.post(f"{PI_URL}/api/cm/result",
                          json={"item_id": item_id, "url": url, "listings": listings},
                          timeout=30)
        r.raise_for_status()
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        log.warning("  ✗ %s", msg[:200])
        try:
            requests.post(f"{PI_URL}/api/cm/result",
                          json={"item_id": item_id, "url": url, "error": msg[:250]},
                          timeout=15)
        except Exception:
            pass


# ── main-loop ─────────────────────────────────────────────────────────
def main() -> None:
    log.info("Kensa CM worker start — polling %s", PI_URL)
    while True:
        try:
            r = requests.get(f"{PI_URL}/api/cm/pending", timeout=15)
            r.raise_for_status()
            pending = r.json()
        except Exception as e:
            log.warning("poll error: %s", e)
            time.sleep(POLL_INTERVAL_SEC)
            continue

        if not pending:
            time.sleep(POLL_INTERVAL_SEC)
            continue

        log.info("%d pending jobs", len(pending))
        for job in pending:
            raw_grade = job.get("grade")
            grade = str(raw_grade).strip() if raw_grade not in (None, "") else ""
            if not grade:
                log.warning("skip %s: geen grade (nooit default naar PSA 10)", job.get("item_id"))
                continue
            process_job(job["item_id"], job["url"], grade=grade)
            time.sleep(BETWEEN_SCRAPES_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("stopped by user")
    except Exception as e:
        log.exception("crash: %s", e)
        sys.exit(1)
