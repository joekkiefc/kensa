"""Kensa — eenmalige eBay login-flow om sold-endpoint toegang te krijgen.

Draait 1x om cookies+session te warmen op in de PROFILE-dir. Daarna gebruikt
ebay_lastsold.py die profile → geen login meer nodig, sold-URL is toegankelijk.

Als eBay 2FA of extra verificatie vraagt: script pauzeert en toont een screenshot,
gebruiker lost handmatig op (headless=False), sessie wordt bewaard.

Usage:
  ./ebay_login.py           # normale run (headless)
  ./ebay_login.py --headed  # zichtbaar Chromium (helpt bij 2FA)
  ./ebay_login.py --check   # skip login, alleen kijken of huidige sessie geldig is
"""

import argparse
import json
import sys
import time
from pathlib import Path

from scrapling import StealthyFetcher

PROFILE_DIR = Path("/home/pi/.cache/kensa-ebay-authed")
PROFILE_DIR.mkdir(parents=True, exist_ok=True)
SECRETS = Path("/home/pi/.openclaw/secrets.json")


def _load_creds() -> tuple[str, str]:
    s = json.loads(SECRETS.read_text())
    e = s.get("kensa", {}).get("ebay_login") or {}
    email = e.get("email")
    pw = e.get("password")
    if not email or not pw:
        raise RuntimeError("secrets.json → kensa.ebay_login.email/password ontbreekt")
    return email, pw


def _typing_action(email: str, pw: str):
    """page_action die de login-flow uitvoert. Wordt aangeroepen door Scrapling
    ná page.goto(login-url)."""
    def action(page):
        try:
            page.wait_for_selector("#userid, input[name='userid']", timeout=30000)
            page.fill("#userid, input[name='userid']", email)
            # eBay: 'Continue' klikken voor password veld verschijnt
            page.click("#signin-continue-btn, button[type='submit']", timeout=10000)
            page.wait_for_selector("#pass, input[name='pass']", timeout=30000)
            page.fill("#pass, input[name='pass']", pw)
            page.click("#sgnBt, button[type='submit']", timeout=10000)
            # Wacht op post-login navigation (30s max)
            page.wait_for_load_state("domcontentloaded", timeout=30000)
            time.sleep(5)  # extra tijd voor cookies te settelen
        except Exception as e:
            print(f"[login] page_action fout: {type(e).__name__}: {e}", file=sys.stderr)
        return page
    return action


def check_session(headless: bool = True) -> bool:
    """Test of huidige profile is ingelogd. True = sessie geldig."""
    f = StealthyFetcher()
    r = f.fetch("https://www.ebay.com/mye/myebay/summary",
                headless=headless, user_data_dir=str(PROFILE_DIR),
                network_idle=False, wait=3000, timeout=60000,
                useragent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36")
    html = r.html_content or ""
    logged_in = ("myebay" in (r.url or "").lower() and "signin" not in (r.url or "").lower()
                  and r.status == 200 and "Sign in" not in html[:5000])
    print(f"[check] status={r.status}  url={(r.url or '')[:80]}  logged_in={logged_in}", file=sys.stderr)
    return logged_in


def do_login(headless: bool = True) -> bool:
    email, pw = _load_creds()
    f = StealthyFetcher()
    print(f"[login] naar signin.ebay.com ({email[:5]}...)", file=sys.stderr)
    r = f.fetch("https://signin.ebay.com/signin",
                headless=headless, user_data_dir=str(PROFILE_DIR),
                network_idle=False, wait=3000, timeout=90000,
                useragent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
                page_action=_typing_action(email, pw))
    print(f"[login] eindresultaat: status={r.status}  url={(r.url or '')[:100]}", file=sys.stderr)
    if not r.html_content:
        print("[login] geen HTML terug", file=sys.stderr)
        return False
    lower = r.html_content.lower()
    if "captcha" in lower or "verifiëren" in lower or "verify" in lower:
        print("[login] ⚠ captcha/verify pagina — handmatig oplossen nodig (--headed)", file=sys.stderr)
    time.sleep(2)
    return check_session(headless=headless)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true", help="Zichtbare browser (voor 2FA)")
    ap.add_argument("--check", action="store_true", help="Alleen sessie-check, geen login")
    args = ap.parse_args()

    headless = not args.headed
    if args.check:
        ok = check_session(headless=headless)
        print("Session geldig" if ok else "Session NIET geldig")
        sys.exit(0 if ok else 1)

    ok = do_login(headless=headless)
    print("Login gelukt + sessie opgeslagen" if ok else "Login FAALT — probeer --headed")
    sys.exit(0 if ok else 1)
