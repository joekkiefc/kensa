"""Qwen-opdracht mét fix 2 (label-prefix niet uitspellen) en fix 3 (label_name letterlijk).
Eén plek, zodat schaduw en test-harnas exact dezelfde opdracht gebruiken."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from llm_client import PHOTO_INTERPRET_PROMPT

PROMPT_FIXED = PHOTO_INTERPRET_PROMPT.replace(
    '  "subtype":',
    '  "label_name": "<de NAAM-regel van het PSA-label LETTERLIJK zoals gedrukt, bv \'FA/JOLTEON V\' of \'TM.MAG.GROUDON-HOLO\' — kopieer, interpreteer niet>",\n  "subtype":',
).replace(
    "REGELS:",
    "REGELS:\n- FA/, SA/, RR/ vooraan op het label zijn AFKORTINGEN (Full Art, Special Art). NOOIT uitspellen tot een woord (dus nooit \'Fairy\'); laat staan of weglaten.",
)
assert PROMPT_FIXED != PHOTO_INTERPRET_PROMPT
