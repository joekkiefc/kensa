"""Schrijf-slot voor het onderzoek. MOET geïmporteerd worden vóór enige Kensa-module.

- sqlite3.connect naar kensa.db → altijd read-only (mode=ro URI). Elke schrijfpoging
  gooit 'attempt to write a readonly database'.
- requests: alleen GET/HEAD; al het andere wordt geweigerd en geteld.
- urllib.request.urlopen: alleen toegestaan als ALLOW_URLLIB (Gemini/Vision in G3);
  en dan alleen naar generativelanguage/vision googleapis. Supabase via urllib = geweigerd.
- Kensa-writers (save_trap, mark_slab_status, price_cache upserts, CM-enqueue, llm-cache set,
  supabase_sync) worden na import vervangen door een val die telt en weigert.
"""
import os
import sqlite3
import urllib.request

import requests

for k in ("KENSA_WRITE_STORAGE", "KENSA_STORAGE"):
    os.environ.pop(k, None)
os.environ["KENSA_READ_STORAGE"] = "supabase"   # leespaden = Supabase (zoals live workers)

BLOCKED = {"http_non_get": [], "urllib": [], "writer_calls": [], "sqlite_rw_open": []}
ALLOW_URLLIB_HOSTS: set[str] = set()
KENSA_DB = "/home/pi/.openclaw/workspace/agents/kensa/kensa.db"

_orig_connect = sqlite3.connect


def _ro_connect(database, *a, **kw):
    db = str(database)
    if db.endswith("kensa.db") and not db.startswith("file:"):
        BLOCKED["sqlite_rw_open"].append(db)
        kw.pop("uri", None)
        return _orig_connect(f"file:{db}?mode=ro", *a, uri=True, **kw)
    return _orig_connect(database, *a, **kw)


sqlite3.connect = _ro_connect

_orig_request = requests.Session.request


def _get_only(self, method, url, *a, **kw):
    if str(method).upper() not in ("GET", "HEAD"):
        BLOCKED["http_non_get"].append(f"{method} {url}"[:160])
        raise RuntimeError(f"ONDERZOEK: {method} geweigerd ({url[:80]})")
    return _orig_request(self, method, url, *a, **kw)


requests.Session.request = _get_only

_orig_urlopen = urllib.request.urlopen


def _urlopen(req, *a, **kw):
    url = req.full_url if hasattr(req, "full_url") else str(req)
    host = url.split("/")[2] if "://" in url else url
    if host not in ALLOW_URLLIB_HOSTS:
        BLOCKED["urllib"].append(url.split("?")[0][:120])
        raise RuntimeError(f"ONDERZOEK: urllib naar {host} geweigerd")
    return _orig_urlopen(req, *a, **kw)


urllib.request.urlopen = _urlopen


def _val(naam):
    def f(*a, **kw):
        BLOCKED["writer_calls"].append(naam)
        raise RuntimeError(f"ONDERZOEK: writer {naam} geweigerd")
    return f


def sluit_writers():
    """Na import van de Kensa-modules: alle bekende writers vervangen."""
    import importlib
    targets = {
        "analyze": ["_save_trap", "_mark_slab_status"],
        "analyze_split.score_only": ["_save_trap", "_mark_slab_status"],
        "analyze_split.analyze_full": ["_save_trap"],
        "analyze_split.ebay_phase": ["_price_cache_upsert_ebay"],
        "analyze_split.enqueue_cardmarket": ["_enq_try_cache_hit", "_enq_fresh_insert"],
        "interpret_slab_split.gemini_interpret": ["_slab_cache_set"],
        "llm_client": ["_slab_cache_set"],
        "storage_supabase.analysis": ["save_trap"],
        "storage_supabase.listings": ["mark_slab_status"],
        "storage_supabase._http": ["post", "patch", "delete"],
        "supabase_sync": ["sync_replace_analysis", "sync_slab_status", "sync_cm_queue_upsert"],
    }
    for mod, names in targets.items():
        try:
            m = importlib.import_module(mod)
        except Exception:
            continue
        for n in names:
            if hasattr(m, n):
                setattr(m, n, _val(f"{mod}.{n}"))
