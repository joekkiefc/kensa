"""15-9: pickers via database-functie moeten hetzelfde antwoord geven als de oude
REST-pickers (1 + N chunk-requests). Live-vergelijking op hetzelfde moment; bij een lege
wachtrij zijn beide leeg (test slaagt dan triviaal — dat is bewust: gedrag blijft gelijk)."""
import storage_supabase_legacy as L


def _gelijk(oud, nieuw):
    assert sorted(oud) == sorted(nieuw), f"verschil: alleen oud={sorted(set(oud)-set(nieuw))[:5]} alleen nieuw={sorted(set(nieuw)-set(oud))[:5]}"
    assert oud == nieuw, "zelfde items maar andere volgorde"


def test_pick_cache_rpc_matches_rest():
    _gelijk(L.pick_cache_batch_supabase_rest(200, 3), L.pick_cache_batch_supabase(200, 3))


def test_pick_ebay_rpc_matches_rest():
    _gelijk(L.pick_ebay_batch_supabase_rest(20, 3), L.pick_ebay_batch_supabase(20, 3))


def test_pick_mercapi_recheck_rpc_matches_rest():
    _gelijk(L.pick_mercapi_recheck_batch_supabase_rest(100, 6), L.pick_mercapi_recheck_batch_supabase(100, 6))
