"""Kensa storage-laag — Supabase-native reads + writes.

Writes: submodules listings/photos/analysis/cardmarket_queue (nieuw, testcoverage).
Reads: gepromoveerd uit `storage_supabase_legacy.py` (voormalige `storage_supabase.py`
       — hernoemd toen deze package werd toegevoegd zodat het niet meer verstopt
       raakt achter de package-directory).

Re-exports zorgen dat bestaande callers ongewijzigd blijven werken.
"""

from storage_supabase_legacy import (  # noqa: F401
    load_listing_supabase,
    load_photo_urls_supabase,
    load_analysis_trap_latest_supabase,
    load_price_cache_by_key_supabase,
    load_analysis_by_trap_since_supabase,
    pick_cache_batch_supabase,
    pick_ebay_batch_supabase,
    pick_detail_batch_supabase,
    pick_detail_proxy_batch_supabase,
    pick_mercapi_batch_supabase,
    pick_mercapi_recheck_batch_supabase,
    pick_ocr_batch_supabase,
    load_detail_status_supabase,
)
