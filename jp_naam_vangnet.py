"""jp_naam_vangnet — Japanse Pokémon-naam (katakana óf klank-spelling) → officiële Engelse naam.

Tommy 9-9: "Japanse tekst is gewoon nooit goed — het is altijd een Amerikaanse slab."
Dus: het label is Engels; als een lezer toch Japans aflevert (カイリキー, 'Kairiki',
'Nendorei', 'Denryu'), zet dit vangnet het om naar de officiële naam (Machamp,
Claydol, Ampharos) via het bestaande pokedex_ja_en.json (1025 soorten).

- katakana → direct opzoeken
- klank-spelling (romaji) → opzoeken in een romaji-index die we uit de katakana afleiden
  (Hepburn), genormaliseerd (lange klinkers samengevouwen), met kleine tolerantie.
- al Engels (staat in de pokédex) → ongemoeid.
Trainers zitten bewust NIET in dit woordenboek (buiten scope).
"""
from __future__ import annotations
import difflib, json, re
from pathlib import Path

_POKEDEX = json.load(open(Path(__file__).resolve().parent / "pokedex_ja_en.json"))
EN_NAMEN = {v.lower() for v in _POKEDEX.values()}

# ---- katakana → Hepburn -----------------------------------------------------------
_K = {
 'ア':'a','イ':'i','ウ':'u','エ':'e','オ':'o','カ':'ka','キ':'ki','ク':'ku','ケ':'ke','コ':'ko',
 'サ':'sa','シ':'shi','ス':'su','セ':'se','ソ':'so','タ':'ta','チ':'chi','ツ':'tsu','テ':'te','ト':'to',
 'ナ':'na','ニ':'ni','ヌ':'nu','ネ':'ne','ノ':'no','ハ':'ha','ヒ':'hi','フ':'fu','ヘ':'he','ホ':'ho',
 'マ':'ma','ミ':'mi','ム':'mu','メ':'me','モ':'mo','ヤ':'ya','ユ':'yu','ヨ':'yo','ラ':'ra','リ':'ri',
 'ル':'ru','レ':'re','ロ':'ro','ワ':'wa','ヲ':'o','ン':'n','ガ':'ga','ギ':'gi','グ':'gu','ゲ':'ge','ゴ':'go',
 'ザ':'za','ジ':'ji','ズ':'zu','ゼ':'ze','ゾ':'zo','ダ':'da','ヂ':'ji','ヅ':'zu','デ':'de','ド':'do',
 'バ':'ba','ビ':'bi','ブ':'bu','ベ':'be','ボ':'bo','パ':'pa','ピ':'pi','プ':'pu','ペ':'pe','ポ':'po',
 'ヴ':'vu','ァ':'a','ィ':'i','ゥ':'u','ェ':'e','ォ':'o','ャ':'ya','ュ':'yu','ョ':'yo',
}
_COMBO = {'キャ':'kya','キュ':'kyu','キョ':'kyo','シャ':'sha','シュ':'shu','ショ':'sho','チャ':'cha','チュ':'chu','チョ':'cho',
          'ニャ':'nya','ニュ':'nyu','ニョ':'nyo','ヒャ':'hya','ヒュ':'hyu','ヒョ':'hyo','ミャ':'mya','ミュ':'myu','ミョ':'myo',
          'リャ':'rya','リュ':'ryu','リョ':'ryo','ギャ':'gya','ギュ':'gyu','ギョ':'gyo','ジャ':'ja','ジュ':'ju','ジョ':'jo',
          'ビャ':'bya','ビュ':'byu','ビョ':'byo','ピャ':'pya','ピュ':'pyu','ピョ':'pyo','ファ':'fa','フィ':'fi','フェ':'fe','フォ':'fo',
          'ティ':'ti','ディ':'di','デュ':'du','ウィ':'wi','ウェ':'we','ウォ':'wo','ヴァ':'va','ヴィ':'vi','ヴェ':'ve','ヴォ':'vo',
          'シェ':'she','ジェ':'je','チェ':'che','ツァ':'tsa','ツェ':'tse','ツォ':'tso','トゥ':'tu'}


def kana_naar_romaji(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        two = s[i:i+2]
        if two in _COMBO:
            out.append(_COMBO[two]); i += 2; continue
        ch = s[i]
        if ch == 'ッ':                       # kleine tsu: verdubbel volgende medeklinker
            nxt = _COMBO.get(s[i+1:i+3]) or _K.get(s[i+1] if i+1 < len(s) else '', '')
            out.append(nxt[0] if nxt else ''); i += 1; continue
        if ch == 'ー':                       # lange klinker: herhaal vorige klinker
            if out and out[-1] and out[-1][-1] in 'aiueo':
                out.append(out[-1][-1])
            i += 1; continue
        out.append(_K.get(ch, '')); i += 1
    return ''.join(out)


def _norm(s: str) -> str:
    s = re.sub(r"[^a-z]", "", str(s or "").lower())
    s = s.replace("ph", "f").replace("ck", "k").replace("y", "i")   # spellingsvarianten
    s = re.sub(r"c(?!h)", "k", s)
    for lang in ("aa", "ii", "uu", "ee", "oo", "ou"):      # lange klinkers samenvouwen
        s = s.replace(lang, lang[0])
    s = re.sub(r"([bcdfghjklmnpqrstvwxz])\1", r"\1", s)   # dubbele medeklinkers (kodakku→kodaku)
    return s


ROMAJI_INDEX: dict[str, str] = {}
for _jp, _en in _POKEDEX.items():
    ROMAJI_INDEX.setdefault(_norm(kana_naar_romaji(_jp)), _en)

_HEEFT_JP = re.compile(r"[぀-ヿ一-鿿]")
_KATAKANA_RUN = re.compile(r"[゠-ヿ]+")


def naar_engels(naam: str | None) -> tuple[str | None, str]:
    """→ (officiële Engelse naam of None, reden). Al-Engels blijft ongemoeid."""
    if not naam:
        return None, "leeg"
    tokens = [t.lower() for t in re.split(r"[^A-Za-z]+", naam) if t]
    if any(t in EN_NAMEN for t in tokens):
        return None, "al_engels"
    if _HEEFT_JP.search(naam):
        for run in _KATAKANA_RUN.findall(naam):
            if run in _POKEDEX:
                return _POKEDEX[run], "katakana_direct"
            hit = ROMAJI_INDEX.get(_norm(kana_naar_romaji(run)))
            if hit:
                return hit, "katakana_via_romaji"
        return None, "japans_onbekend"
    for t in [ "".join(tokens) ] + tokens:              # eerst alles aaneen (Todoroku Tsuki), dan per woord
        n = _norm(t)
        if len(n) < 4:
            continue
        if n in ROMAJI_INDEX:
            return ROMAJI_INDEX[n], "romaji_exact"
        kand = difflib.get_close_matches(n, ROMAJI_INDEX.keys(), n=1, cutoff=0.84)
        if kand:
            return ROMAJI_INDEX[kand[0]], f"romaji_fuzzy({kand[0]})"
    return None, "geen_match"


if __name__ == "__main__":
    for p in ["Kairiki", "Nendorei", "Denryu", "Ninphia", "Brakky", "カイリキー", "メガミミロップ&プリン",
              "Sandaasu", "Thunders", "Pikachu", "Charizard VSTAR", "Sylphie", "Ribombee", "Todoroku Tsuki"]:
        print(f"{p!r:24s} → {naar_engels(p)}")
