#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — LIVE-bewaker voor de lokale slab-OCR op de thuis-PC.

Waarom: de lokale lezers (GOT-OCR poort 8898 + Qwen poort 1234) op Tommy's
thuis-PC gaan STIL uit zonder waarschuwing. Kensa valt dan terug op de BETAALDE
Google Vision/Gemini en Tommy merkt het pas aan de Google-afschrijving. Deze
bewaker checkt elke 5 min (cron) of beide lezers leven en stuurt bij een
STATUSWISSEL één Discord-melding naar #algemeen. Zie:
memory/kensa-vision-lokaal-analyse.md

Checks (BEIDE moeten leven om "WERKEND" te zijn):
  (a) TCP-poort 8898 open  EN  HTTP /ocr bereikbaar (GOT-OCR).
  (b) Qwen 1234 /v1/models geeft NIET-leeg terug (model echt geladen).

Anti-spam: alleen pingen bij overgang (down->up of up->down). Laatste status in
lokale_ocr_status.json. Hergebruikt post_discord uit report_supabase_writes.py.
"""

import json
import socket
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Stille uren: geen Discord-melding tussen 00:00 en 07:00 lokale tijd (Tommy
# slaapt). De check blijft wel elke 5 min draaien; een statuswissel in dit
# venster wordt NIET als "gemeld" vastgelegd, zodat na 07:00 de eerstvolgende
# check de nog-actuele status alsnog één keer kan melden (inhaal).
STILLE_TZ = ZoneInfo("Europe/Amsterdam")
STILLE_START_UUR = 0   # inclusief
STILLE_EIND_UUR = 7    # exclusief


def _in_stille_uren() -> bool:
    uur = datetime.now(STILLE_TZ).hour
    return STILLE_START_UUR <= uur < STILLE_EIND_UUR

# Hergebruik de bestaande, werkende Discord-helper (openclaw message send -> #algemeen).
from report_supabase_writes import post_discord

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_PATH = SCRIPT_DIR / "lokale_ocr_status.json"

# Zelfde host/poorten als de live lezers (ocr_router.LOCAL_OCR_URL / qwen_lezer).
PC_HOST = "100.125.116.37"           # thuis-PC 'tommy' via Tailscale
OCR_PORT = 8898                      # GOT-OCR
OCR_URL = f"http://{PC_HOST}:{OCR_PORT}/ocr"
QWEN_MODELS_URL = f"http://{PC_HOST}:1234/v1/models"

TCP_TIMEOUT = 4.0
HTTP_TIMEOUT = 5.0


def _tcp_open(host: str, port: int, timeout: float = TCP_TIMEOUT) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ocr_leeft() -> tuple[bool, str]:
    """(a) poort 8898 open + /ocr HTTP bereikbaar."""
    if not _tcp_open(PC_HOST, OCR_PORT):
        return False, "poort 8898 dicht"
    # Poort open: even kijken of de HTTP-dienst reageert. GET /ocr geeft normaal
    # 405/400/404 (verwacht POST) maar DAT bewijst dat de server leeft. Alleen
    # een connectie-/timeout-fout telt als dood.
    try:
        urllib.request.urlopen(OCR_URL, timeout=HTTP_TIMEOUT)
        return True, "ok"
    except urllib.error.HTTPError:
        return True, "ok"  # server antwoordde (methode/route-fout) => leeft
    except Exception as e:  # noqa: BLE001 — timeout/connreset/URLError => dood
        return False, f"8898 geen HTTP-antwoord ({type(e).__name__})"


def _qwen_leeft() -> tuple[bool, str]:
    """(b) Qwen 1234 /v1/models geeft een NIET-lege modellenlijst terug."""
    try:
        with urllib.request.urlopen(QWEN_MODELS_URL, timeout=HTTP_TIMEOUT) as r:
            data = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        return False, f"Qwen 1234 onbereikbaar ({type(e).__name__})"
    modellen = data.get("data") or []
    if not modellen:
        return False, "Qwen 1234 open maar /v1/models LEEG (geen model geladen)"
    return True, f"ok ({len(modellen)} model(len))"


def _lees_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _schrijf_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    nu = datetime.now(timezone.utc).isoformat(timespec="seconds")

    ocr_ok, ocr_detail = _ocr_leeft()
    qwen_ok, qwen_detail = _qwen_leeft()
    werkend = ocr_ok and qwen_ok
    status = "up" if werkend else "down"

    reden = f"OCR(8898): {ocr_detail} | Qwen(1234): {qwen_detail}"
    print(f"[{nu}] status={status} | {reden}", flush=True)

    state = _lees_state()
    vorige = state.get("status")  # None bij eerste run

    # Bij eerste run (geen vorige status) behandelen we een DOWN als een wissel
    # zodat Tommy meteen weet dat het eruit ligt. Een eerste UP is geen alarm.
    wissel = (vorige is not None and vorige != status) or (vorige is None and status == "down")

    stil = _in_stille_uren()

    melding_verstuurd = False
    if wissel and stil:
        # Stille uren: niet pingen. State-status NIET bijwerken, zodat na 07:00
        # de eerstvolgende check de wissel alsnog oppakt als 'ie nog actueel is.
        print(f"[{nu}] WISSEL {vorige}->{status} onderdrukt (stille uren 00:00-07:00); "
              f"wordt na 07:00 alsnog gemeld indien nog actueel.",
              file=sys.stderr, flush=True)
    elif wissel:
        if status == "down":
            tekst = ("⚠️ Lokale slab-OCR ligt eruit (poort 8898/Qwen). "
                     "Kensa valt nu terug op betaalde Google Vision = kost geld. "
                     "Zet Ollama/OCR weer aan op je PC.\n"
                     f"(detail: {reden})")
        else:
            tekst = ("✅ Lokale slab-OCR is weer online (poort 8898 + Qwen geladen). "
                     "Kensa leest weer gratis lokaal.")
        melding_verstuurd = post_discord(tekst)
        print(f"[{nu}] WISSEL {vorige}->{status}: Discord {'VERSTUURD' if melding_verstuurd else 'MISLUKT'}",
              file=sys.stderr, flush=True)

    # Overige velden altijd bijwerken (laatste check-moment + detail).
    state.update({
        "ocr_ok": ocr_ok,
        "qwen_ok": qwen_ok,
        "reden": reden,
        "laatste_check": nu,
    })
    # status alleen vastleggen als we NIET in de stille uren een wissel onderdrukken,
    # anders blijft 'vorige' staan en pakt de check na 07:00 de wissel alsnog op.
    if not (wissel and stil):
        state["status"] = status
    if wissel and not stil:
        state["laatste_wissel"] = nu
        state["laatste_melding_verstuurd"] = melding_verstuurd
    _schrijf_state(state)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
