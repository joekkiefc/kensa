#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — Supabase lookup voor Cardmarket-URL.

Tabel: cards
Match op: name ilike '*<pokemon>*' AND card_number = '<number>'
Prefereer language=japanese (want Buyee = JP kaarten).

Returns: {"url": <cardmarket_url>, "name": ..., "set_name": ..., "language": ...} of None.
"""

import json
import urllib.parse
import urllib.request
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SECRETS = Path("/home/pi/.openclaw/secrets.json")

# 3-letter pokemon-namen die WEL door de lookup mogen (Muk hoeft niet — Tommy 2026-09-01).
_SHORT_POKEMON_ALLOWLIST = {"mew"}

# Timeout- en pool-strategie identiek aan supabase_sync (RCA fix #1+#2):
# split (connect=5, read=25) + gedeelde Session ipv per-call urlopen die
# elke keer een verse TLS-handshake doet.
TIMEOUT: tuple[float, float] = (5.0, 25.0)

_SESSION: requests.Session | None = None


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        retry = Retry(
            total=3,
            connect=3,
            read=3,
            backoff_factor=1.0,
            status_forcelist=[502, 503, 504],
            allowed_methods=frozenset(["GET", "HEAD"]),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=retry)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        _SESSION = s
    return _SESSION


def _config() -> tuple[str, dict]:
    s = json.loads(SECRETS.read_text())["supabase"]
    return s["url"], {"apikey": s["service_role_key"],
                       "Authorization": f"Bearer {s['service_role_key']}"}


def _fetch(base: str, headers: dict, pokemon: str, num: str) -> list[dict]:
    """PostgREST OR-filter met word-boundary match: naam begint met pokemon,
    of pokemon staat na een spatie/apostrof. Voorkomt substring-collisions
    zoals 'on' matchend met 'Umbreon'/'Melody'/'Cornerstone'/etc.

    Voor korte pokemon-namen (< 4 chars, whitelist zoals 'mew') gebruiken we
    STRICT EQUAL match op naam — anders matcht 'mew*' ook 'mewtwo'.
    """
    if len(pokemon) < 4:
        # Strict: alleen exact-name matches (of 'X mew' / "'mew" / '-mew')
        or_filter = f"(name.eq.{pokemon},name.ilike.* {pokemon},name.ilike.*'{pokemon},name.ilike.*-{pokemon})"
    else:
        or_filter = f"(name.ilike.{pokemon}*,name.ilike.* {pokemon}*,name.ilike.*'{pokemon}*,name.ilike.*-{pokemon}*)"
    params = {
        "or": or_filter,
        "card_number": f"eq.{num}",
        "select": "name,card_number,set_name,set_number,mint,url,language,era",
        "limit": "10",
    }
    try:
        r = _session().get(f"{base}/rest/v1/cards", params=params,
                            headers=headers, timeout=TIMEOUT)
        if r.status_code != 200:
            return []
        return r.json()
    except Exception:
        return []


import re as _re


def _set_tokens(*strings: str) -> set[str]:
    """Extract alfanumerieke tokens van >=2 chars, lowercase. Voor set-matching."""
    out: set[str] = set()
    for s in strings:
        if not s:
            continue
        for tok in _re.findall(r"[A-Za-z0-9]+", str(s).lower()):
            if len(tok) >= 2:
                out.add(tok)
    return out


def _score_pick(row: dict, slab_tokens: set[str]) -> int:
    """Score een DB-row op hoe goed 'ie matcht met slab-set-info.
    +2 per token dat zowel in DB-set-fields als in slab-tokens zit."""
    if not slab_tokens:
        return 0
    db_tokens = _set_tokens(row.get("set_number"), row.get("set_name"), row.get("era"))
    return 2 * len(db_tokens & slab_tokens)


# --- set-validatie (2026-09-09, Tommy): de set moet KLOPPEN, geen tiebreak ---------
# Catalogus schrijft set_number compact ('swshp', 'smp', 's8b'); labels schrijven
# 'S-P', 'SM-P', 'S8a-P'. Promo-subsets (S8a-P, SV1a-P) vallen in de catalogus onder
# de familie-promo ('swshp', 'svp').
_SET_ALIAS = {"sp": "swshp", "svp": "svp", "smp": "smp", "xyp": "xyp", "mp": "mp", "bwp": "bwp", "dpp": "dpp"}
_RARITY_CODES = {"CSR", "SSR", "SAR", "SR", "AR", "UR", "HR", "CHR", "RR", "RRR", "SIR", "ACE", "IR", "PR", "MUR",
                 "K", "U", "C", "R"}   # losse rarity-letters (gelijkgetrokken met het test-harnas, fase 4)
_SETCODE_RE = _re.compile(r"^[A-Z]{1,3}\d{0,2}[A-Z]?(?:-P)?$")
_GENERIEKE_TOKENS = {"pokemon", "jp", "en", "japanese", "english", "japan", "card", "game", "the", "of", "and",
                     "promo", "promos", "2019", "2020", "2021", "2022", "2023", "2024", "2025", "2026",
                     # subtype/rarity-woorden zijn géén set-bewijs ('Terastal Fest ex' ≠ 'mega dream ex')
                     "ex", "v", "gx", "vmax", "vstar", "sar", "ar", "sr", "ur", "hr", "chr", "break", "holo",
                     "gem", "mt", "psa", "mint"}


def _norm_set(s) -> str:
    return _re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def geldige_setcode(s) -> str | None:
    """'SV-P' -> 'SV-P'; rommel ('2023POKEMONSVGJP', '10b') en rarity-codes ('CSR') -> None."""
    s = _re.sub(r"[^A-Za-z0-9\-]", "", str(s or "")).upper()
    if not s or s in _RARITY_CODES or not _SETCODE_RE.match(s):
        return None
    return s


def _aanvaarde_setnummers(set_code: str) -> set[str]:
    code = _norm_set(set_code)
    ok = {code, _SET_ALIAS.get(code, code)}
    m = _re.match(r"^(sv|sm|xy|bw|dp|s|m)\d*[a-z]?p$", code)   # promo-subset → familie
    if m:
        fam = m.group(1) + "p"
        ok.add(_SET_ALIAS.get(fam, fam))
    return ok


def lookup(pokemon_name: str, card_number: str, set_hint: str | None = None,
           set_code: str | None = None, soft_hint: str | None = None) -> dict | None:
    """Zoek de Cardmarket-URL voor deze kaart. pokemon_name lowercase, card_number str.

    Set-validatie (2026-09-09): als `set_code` (bv 'SV-P') of een betekenisvolle
    `set_hint` (bv 'VMAX Climax') bekend is, MOET de kandidaat daarbij passen —
    anders None. Reden: pokemon+nummer is niet uniek (Pikachu-promo's!) en de
    leading-zero-fallback matchte kaarten uit totaal andere sets (Golden Box
    Pikachu #005 -> Sapporo's Pikachu SM-P5). Liever geen match dan een verkeerde.
    `soft_hint` (bv de letterlijke labeltekst 'TAG TEAM GX ALL STARS'): mag een
    kandidaat alleen BEVESTIGEN (voorrang), nooit afkeuren — labeltekst bevat vaak
    alleen de kaartnaam, dus ontbreken van set-woorden zegt niks.
    Zonder set-info: oud gedrag (eerste kandidaat), ongewijzigd.

    Probeert eerst met leading zeros ('068'), dan zonder ('68'). Supabase-data is
    inconsistent op dit punt.
    """
    if not pokemon_name or not card_number:
        return None
    pokemon = pokemon_name.lower().strip()
    # 3-letter namen worden normaal geblokkeerd (te veel false-positives via
    # ILIKE-wildcards), maar 'mew' is een echte kaart met duidelijke marktvraag.
    # De word-boundary-filter in _fetch voorkomt sowieso Mewtwo/etc. collisions.
    if len(pokemon) < 4 and pokemon not in _SHORT_POKEMON_ALLOWLIST:
        return None
    base, headers = _config()
    num_raw = card_number.split("/")[0].lstrip("#")
    variants = [num_raw]
    stripped = num_raw.lstrip("0") or "0"
    if stripped != num_raw:
        variants.append(stripped)
    rows: list[dict] = []
    for num in variants:
        rows = _fetch(base, headers, pokemon, num)
        if rows:
            break
    if not rows:
        return None
    picks = [r for r in rows if (r.get("language") or "").lower() == "japanese" and r.get("mint")]
    if not picks:
        return None
    # Set-code uit het nummer-suffix ('218/SV-P') als die niet apart is meegegeven.
    if not set_code and "/" in card_number:
        set_code = geldige_setcode(card_number.split("/", 1)[1])
    set_code = geldige_setcode(set_code) if set_code else None
    slab_tokens = (_set_tokens(set_hint) if set_hint else set()) - _GENERIEKE_TOKENS
    # HARDE set-check: bij set-info moet de kandidaat passen, anders geen match.
    if set_code or slab_tokens:
        ok_nrs = _aanvaarde_setnummers(set_code) if set_code else set()
        picks = [r for r in picks if _norm_set(r.get("set_number")) in ok_nrs
                 or (slab_tokens and _score_pick(r, slab_tokens) > 0)]
        if not picks:
            return None
    elif soft_hint:
        # accept-only: kandidaten die de labeltekst bevestigen gaan vóór; niks past → oud gedrag
        soft = _set_tokens(soft_hint) - _GENERIEKE_TOKENS
        bevestigd = [r for r in picks if soft and _score_pick(r, soft) > 0]
        if bevestigd:
            picks = bevestigd
    # Tie-break op set-hint: score elke pick op hoeveel set-tokens overeenkomen.
    if slab_tokens:
        scored = sorted(picks, key=lambda r: (-_score_pick(r, slab_tokens), picks.index(r)))
        p = scored[0]
        p_score = _score_pick(p, slab_tokens)
    else:
        p = picks[0]
        p_score = 0
    return {
        "url": p["mint"],
        "name": p.get("name"),
        "set_name": p.get("set_name"),
        "set_number": p.get("set_number"),
        "language": p.get("language"),
        "n_matches": len(rows),
        "n_picks": len(picks),
        "tiebreak_score": p_score,
    }


if __name__ == "__main__":
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else "lucario"
    num = sys.argv[2] if len(sys.argv) > 2 else "228"
    r = lookup(name, num)
    print(json.dumps(r, indent=2))
