"""Qwen-opdracht mét fix 2 (label-prefix niet uitspellen) en fix 3 (label_name letterlijk).
Sinds fase 4 (9-9) woont de opdracht in productie: qwen_lezer.PROMPT_FIXED. Dit is een
doorgeefluik zodat schaduw en test-harnas exact dezelfde opdracht gebruiken als live."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qwen_lezer import PROMPT_FIXED  # noqa: E402,F401
