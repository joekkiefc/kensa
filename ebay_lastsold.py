#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — eBay last-sold scraper.

Fetches the "sold + completed listings, newest first" search results for a
given query using Scrapling StealthyFetcher (2-step: warmup homepage → search
with shared user_data_dir cookies to survive eBay's Kasada anti-bot).

Returns up to 5 most-recent sales with:
  price_usd, sold_date (YYYY-MM-DD), title, url, item_id
"""

import argparse
import json
import random
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from scrapling import StealthyFetcher
from scrapling.parser import Selector

_PROFILE_LOCK = threading.Lock()

SCRIPT_DIR = Path(__file__).resolve().parent

# Ingelogde profile van eBay burner-account (via ebay_login.py) — session cookies
# geven toegang tot sold-endpoint. Zonder login = signin-redirect voor iedereen.
PROFILE_DIR = Path("/home/pi/.cache/kensa-ebay-authed")
PROFILE_DIR.mkdir(parents=True, exist_ok=True)


def _clear_stale_singleton_locks() -> None:
    """Verwijder stale Chromium SingletonLock-files uit PROFILE_DIR.

    Chromium's ProcessSingleton weigert te starten als deze files bestaan (blijven achter
    na crash/kill). Wij weten dat er nooit een parallelle instance op deze profile-dir
    draait (flock via cron_worker_ebay.sh + _PROFILE_LOCK), dus stale = altijd veilig weg."""
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        p = PROFILE_DIR / name
        if p.exists() or p.is_symlink():
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            except Exception as e:
                print(f"[ebay] warn: kon {p} niet verwijderen: {e}", file=sys.stderr)

# BoilingProxies residential NL — 50 sessions, roteer per request.
# Format regel: host:port:user:password  (password bevat session-id + lifetime)
PROXIES_FILE = SCRIPT_DIR / "proxies.txt"


def _load_proxies() -> list[dict]:
    if not PROXIES_FILE.exists():
        return []
    out = []
    for line in PROXIES_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 3)
        if len(parts) != 4:
            continue
        host, port, user, pw = parts
        out.append({
            "server": f"http://{host}:{port}",
            "username": user,
            "password": pw,
            "id": pw.split("_session-", 1)[1].split("_", 1)[0] if "_session-" in pw else "?",
        })
    return out


_PROXIES = _load_proxies()

# Recente Chrome/Edge user-agents (mei 2026). Roteer per query.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
]

PRICE_RE = re.compile(r"\$\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{2})?)")
ITEM_ID_RE = re.compile(r"/itm/(\d{8,15})")
SOLD_DATE_RE = re.compile(r"Sold\s+([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{4})")

MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
          "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def build_url(query: str, tld: str = "com") -> str:
    return (
        f"https://www.ebay.{tld}/sch/i.html?"
        + urllib.parse.urlencode({"_nkw": query, "LH_Sold": 1, "LH_Complete": 1, "_sop": 13})
    )


def _is_captcha(html: str, status: int, final_url: str = "") -> bool:
    """eBay redirect naar splashui/captcha of signin page = block."""
    if not html:
        return False
    if "splashui/captcha" in html or "splashui/captcha" in final_url:
        return True
    if "Pardon Our Interruption" in html or "unusual activity" in html.lower():
        return True
    if status in (403, 429):
        return True
    return False


def _try_fetch_with_proxy(query: str, tld: str, proxy: dict | None, ua: str,
                            verbose: bool) -> tuple[str | None, str]:
    """Search met ingelogde session cookies (via PROFILE_DIR). Login is de sleutel
    voor sold-endpoint — geen warmup of proxy nodig meer."""
    f = StealthyFetcher()
    kwargs_common = {"headless": True, "useragent": ua,
                       "disable_resources": True, "network_idle": False,
                       "user_data_dir": str(PROFILE_DIR)}

    if verbose:
        print(f"[ebay] search met authed session  UA={ua[:60]}...", file=sys.stderr)

    url = build_url(query, tld)
    # Chromium's ProcessSingleton verbiedt >1 instance per user_data_dir.
    # Serialize alle fetches die deze PROFILE_DIR raken + ruim stale locks op vóór spawn.
    with _PROFILE_LOCK:
        _clear_stale_singleton_locks()
        p = f.fetch(url, timeout=120000, wait=3000,
                    wait_selector=".s-card, .s-item, .srp-controls, body",
                    google_search=False, **kwargs_common)
    html = p.html_content or ""
    final_url = getattr(p, "url", "") or ""
    if "signin.ebay" in final_url or "signup.ebay" in final_url:
        return None, f"session verlopen — run ebay_login.py"
    if _is_captcha(html, p.status, final_url):
        return None, f"captcha-op-search (status={p.status})"
    if p.status != 200 or "error-header__headline" in html:
        return None, f"search status={p.status} len={len(html)}"
    return html, "ok"


def fetch_html(query: str, tld: str = "com",
               verbose: bool = False, max_attempts: int = 2) -> str | None:
    """Gebruik ingelogde profile (session cookies) → sold-endpoint direct toegankelijk.
    Geen proxy nodig; login is de sleutel, niet het IP.
    Bij falen: retry met andere UA. Als 2× fout → check of session verlopen is via ebay_login.py --check."""
    for attempt in range(1, max_attempts + 1):
        ua = random.choice(USER_AGENTS)
        html, reason = _try_fetch_with_proxy(query, tld, None, ua, verbose)
        if html:
            if verbose:
                print(f"[ebay] ✓ success op poging {attempt}", file=sys.stderr)
            return html
        if verbose:
            print(f"[ebay] ✗ {reason} — retry ({attempt}/{max_attempts})", file=sys.stderr)
    if verbose:
        print(f"[ebay] {max_attempts} pogingen faalden — mogelijk session verlopen? "
                f"Run ebay_login.py --check", file=sys.stderr)
    return None


def parse_sold_date(text: str) -> str | None:
    m = SOLD_DATE_RE.search(text or "")
    if not m:
        return None
    mon = MONTHS.get(m.group(1))
    if not mon:
        return None
    return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"


def _first_text(card, selectors: list[str]) -> str | None:
    for sel in selectors:
        for el in card.css(sel):
            t = (el.text or "").strip()
            if t:
                return t
    return None


# `parse_card` verhuisd naar _split/ (v2). Verwijderd 2026-09-01 (regel 2 cleanup).
# Backup: _legacy_pre_refactor/... — alias-override staat lager in de file.



def parse_results(html: str, limit: int = 5) -> list[dict]:
    doc = Selector(content=html)
    cards = doc.css(".s-card.s-card--horizontal")
    out = []
    for c in cards:
        row = parse_card(c)
        if row and row.get("price_usd") and row.get("sold_date"):
            out.append(row)
        if len(out) >= limit:
            break
    return out


def search(query: str, limit: int = 5, verbose: bool = False) -> dict:
    html = fetch_html(query, verbose=verbose)
    if html is None:
        return {"query": query, "error": "fetch_failed", "sales": []}
    sales = parse_results(html, limit=limit)
    return {"query": query, "fetched_at": datetime.now(timezone.utc).isoformat(), "sales": sales}



# Refactor-switch(es) voor if __name__ zodat CLI-run de alias ook heeft.
# Refactor switch (2026-09-01, regel 2 #9): parse_card -> v2.
# Backup: _legacy_pre_refactor/ebay_lastsold.py.20260901-regel2-func9
from parse_card_split import parse_card_v2 as _parse_card_new  # noqa: E402
parse_card = _parse_card_new

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default="Espeon VMAX 189 PSA 10")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    r = search(args.query, limit=args.limit, verbose=args.verbose)
    print(json.dumps(r, ensure_ascii=False, indent=2))


