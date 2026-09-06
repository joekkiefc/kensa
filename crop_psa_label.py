#!/home/pi/.openclaw/workspace/agents/scraper-tools/venv/bin/python3
"""Kensa — Rode PSA-label crop-detector.

Detecteert de rode omranding van een PSA-slab-label in een foto en cropt
alleen die rechthoek uit. Bedoeld als preprocessing voor OCR — verwijdert
kaart-body (Japanse tekst) zodat de parser schoon werk krijgt.
"""

import sys
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


def _red_mask(img_bgr: np.ndarray) -> np.ndarray:
    """HSV-mask voor PSA-rood (twee hue-ranges want rood wraps rond 0)."""
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    lower1 = np.array([0, 100, 70])
    upper1 = np.array([12, 255, 255])
    lower2 = np.array([168, 100, 70])
    upper2 = np.array([179, 255, 255])
    mask = cv2.inRange(hsv, lower1, upper1) | cv2.inRange(hsv, lower2, upper2)
    # Aggressiever mergen: grote kernel + meer iterations. Half-labels worden hersteld
    # doordat losse contour-stukken (bijv. onderbroken door glare) gecombineerd worden.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 11))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel, iterations=3)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                             cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5)),
                             iterations=1)
    return mask


def detect_red_label_bbox(img_bgr: np.ndarray) -> tuple[int, int, int, int] | None:
    """Return axis-aligned (x, y, w, h) van het PSA-label-gebied.
    Merged alle horizontaal-nabij liggende rode contours in bovenste helft van foto tot
    één bounding box (PSA-rand kan opgesplitst zijn in linker/rechter/onder-stukken)."""
    mask = _red_mask(img_bgr)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    H, W = img_bgr.shape[:2]
    min_area = 0.003 * H * W  # 0.3% van foto (contour-stukken kunnen kleiner zijn)
    y_max_center = int(0.55 * H)  # PSA-label in bovenste 55% (rekbaarder tegen foto-variatie)

    # Kandidaten: rode blobs in bovenste helft, ongeacht aspect (kan smal/breed zijn)
    cands = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w * h < min_area: continue
        cy = y + h // 2
        if cy > y_max_center: continue
        cands.append((x, y, w, h))
    if not cands:
        return None

    # Merge: bepaal union-bounding-box van alle kandidaten. Dat is het PSA-label-gebied
    # (linker+rechter+onder-rand stukken samen).
    xs = [c[0] for c in cands]
    ys = [c[1] for c in cands]
    xe = [c[0] + c[2] for c in cands]
    ye = [c[1] + c[3] for c in cands]
    x0, y0 = min(xs), min(ys)
    x1, y1 = max(xe), max(ye)
    w, h = x1 - x0, y1 - y0
    # Sanity: union moet zelf ook labelvormig zijn (breder dan hoog, > min_area)
    if w * h < 0.008 * H * W:  # totaal minstens 0.8%
        return None
    if w / max(1, h) < 1.3:
        return None
    return (x0, y0, w, h)


def detect_red_label_rotated(img_bgr: np.ndarray):
    """Return (cv2 rotated rect) of None. Werkt ook op gekantelde/gedraaide slabs.
    Filtert contours op y-position (label zit in bovenste 60% van foto)."""
    mask = _red_mask(img_bgr)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    H, W = img_bgr.shape[:2]
    min_area = 0.008 * H * W
    y_max_center = int(0.55 * H)  # PSA-label in bovenste 55% (rekbaarder tegen foto-variatie)
    all_pts = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w * h < min_area: continue
        aspect = w / max(1, h)
        if aspect < 1.3 or aspect > 7.0: continue
        cy = y + h // 2
        if cy > y_max_center: continue  # skip als in onderste 40%
        all_pts.append(c)
    if not all_pts:
        return None
    merged = np.vstack(all_pts)
    rect = cv2.minAreaRect(merged)  # ((cx, cy), (w, h), angle)
    (cx, cy), (rw, rh), angle = rect
    # Sorteer zodat w>=h en aspect binnen bereik
    if rw < rh:
        rw, rh = rh, rw
        angle += 90
    if rh == 0 or rw / rh < 1.3 or rw / rh > 7.0:
        return None
    if rw * rh < min_area:
        return None
    return ((cx, cy), (rw, rh), angle)


def _crop_rotated(img_bgr: np.ndarray, rect, padding: int = 12) -> np.ndarray:
    """Snijd geroteerde rechthoek uit + roteer terug naar horizontaal (perspective warp)."""
    (cx, cy), (w, h), angle = rect
    w_pad = w + 2 * padding
    h_pad = h + 2 * padding
    # BoxPoints van de gepadded rect
    rect_pad = ((cx, cy), (w_pad, h_pad), angle)
    box = cv2.boxPoints(rect_pad).astype(np.float32)
    # Doel: assen-uitgelijnd (w_pad × h_pad)
    dst = np.array([[0, h_pad - 1], [0, 0], [w_pad - 1, 0], [w_pad - 1, h_pad - 1]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(box, dst)
    warped = cv2.warpPerspective(img_bgr, M, (int(w_pad), int(h_pad)))
    return warped


def crop_psa_label(img_bgr: np.ndarray, padding: int = 8) -> np.ndarray | None:
    """Return alleen de PSA-label-crop, of None als niet gevonden."""
    bbox = detect_red_label_bbox(img_bgr)
    if bbox is None:
        return None
    x, y, w, h = bbox
    H, W = img_bgr.shape[:2]
    x0 = max(0, x - padding)
    y0 = max(0, y - padding)
    x1 = min(W, x + w + padding)
    y1 = min(H, y + h + padding)
    return img_bgr[y0:y1, x0:x1]


def crop_from_bytes(img_bytes: bytes, padding: int = 12) -> tuple[bytes | None, tuple[int, int, int, int] | None]:
    """Return (jpeg_bytes_of_crop, bbox) — probeert eerst rotated (voor gekantelde slabs),
    valt terug op axis-aligned. Bbox is altijd axis-aligned voor weergave (van uiteindelijke crop)."""
    img = np.array(Image.open(BytesIO(img_bytes)).convert("RGB"))
    img_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

    # Probeer eerst rotated rectangle — werkt op gekantelde/gedraaide slabs
    rect = detect_red_label_rotated(img_bgr)
    crop = None
    bbox_report = None
    if rect is not None:
        (cx, cy), (rw, rh), angle = rect
        norm_angle = abs(angle) % 90
        if 5 < norm_angle < 85:
            crop = _crop_rotated(img_bgr, rect, padding=padding)
            bbox_report = (int(cx - rw / 2), int(cy - rh / 2), int(rw), int(rh))

    # Fallback naar axis-aligned als rotated niks gaf OF rotated was bijna horizontaal
    if crop is None:
        bbox = detect_red_label_bbox(img_bgr)
        if bbox is not None:
            x, y, w, h = bbox
            H, W = img_bgr.shape[:2]
            x0 = max(0, x - padding); y0 = max(0, y - padding)
            x1 = min(W, x + w + padding); y1 = min(H, y + h + padding)
            crop = img_bgr[y0:y1, x0:x1]
            bbox_report = bbox

    if crop is None:
        return None, None

    crop_rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    buf = BytesIO()
    Image.fromarray(crop_rgb).save(buf, format="JPEG", quality=95)
    return buf.getvalue(), bbox_report


if __name__ == "__main__":
    # Standalone test — python crop_psa_label.py <url_or_path> [out.jpg]
    import urllib.request
    src = sys.argv[1] if len(sys.argv) > 1 else "https://static.mercdn.net/item/detail/orig/photos/m27984770699_1.jpg"
    out = sys.argv[2] if len(sys.argv) > 2 else "crop_out.jpg"
    if src.startswith("http"):
        req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
        img_bytes = urllib.request.urlopen(req, timeout=15).read()
    else:
        img_bytes = Path(src).read_bytes()
    crop_bytes, bbox = crop_from_bytes(img_bytes)
    if crop_bytes is None:
        print("NO RED LABEL DETECTED")
        sys.exit(1)
    Path(out).write_bytes(crop_bytes)
    print(f"cropped to {out}  bbox={bbox}  bytes={len(crop_bytes)}")
