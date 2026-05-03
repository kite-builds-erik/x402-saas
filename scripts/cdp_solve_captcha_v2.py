#!/usr/bin/env python3
"""
cdp_solve_captcha_v2.py — decomposed reCAPTCHA solver.

V1 asked Gemini "where to click" in pixels — failed because vision LLMs
struggle at pixel precision. V2 helps the model:

  Step A: read the captcha prompt text (one classification, easy)
  Step B: enumerate visible tile rectangles via JS (precise, deterministic)
  Step C: for each tile, send a CROPPED image of just THAT tile and ask
          "does this contain <object>? yes/no" (binary classification)
  Step D: click the yes-tiles at their JS-known centers
  Step E: click Verify, wait, loop on next puzzle, return done when widget gone

Uses Gemini 3.1 Pro by default (paid key). The decomposition turns the
hard "where on this image" problem into many easy "is this image a car"
problems, which vision models nail.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import List, Optional

from google import genai
from google.genai import types

# Shared CDP / image / JSON helpers.
from _cdp_lib import CDPClient, crop_png, list_pages, strip_json_fences

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    sys.exit("error: set GEMINI_API_KEY in your environment")
DEFAULT_MODEL = "gemini-3.1-pro-preview"


# ── Step A: read the captcha prompt + grid layout ───────────────────────
NORWEGIAN_TARGET_MAP = {
    "biler": "cars", "bil": "cars",
    "sykler": "bicycles", "sykkel": "bicycles",
    "busser": "buses", "buss": "buses",
    "motorsykler": "motorcycles",
    "trafikklys": "traffic lights", "trafikklysene": "traffic lights",
    "fotgjengerfelter": "crosswalks", "fotgjengerfelt": "crosswalks",
    "brannhydranter": "fire hydrants", "brannhydrant": "fire hydrants",
    "broer": "bridges", "bro": "bridges",
    "bater": "boats", "båter": "boats",
    "fly": "airplanes",
    "trapper": "stairs", "trapp": "stairs",
    "skorsteiner": "chimneys",
    "lastebiler": "trucks", "lastebil": "trucks",
    "drosjer": "taxis", "drosje": "taxis",
    "palmer": "palm trees",
}


def read_prompt_and_grid(client: genai.Client, model: str, png: bytes) -> tuple[str, int, int]:
    """Returns (target_word, cols, rows). target_word is English noun phrase."""
    instruction = (
        "Look at this reCAPTCHA challenge screenshot. Return a JSON object with:\n"
        "  - target: the noun phrase from the header describing what to select, "
        "    in ENGLISH (translate from Norwegian/other if needed). Examples: "
        "    'cars', 'traffic lights', 'bicycles', 'crosswalks', 'fire hydrants', "
        "    'motorcycles', 'palm trees', 'bridges', 'boats', 'stairs', 'chimneys'.\n"
        "  - cols: how many tile columns are in the grid (usually 3 or 4)\n"
        "  - rows: how many tile rows are in the grid (usually 3 or 4)\n"
        "Norwegian crib: biler=cars, sykler=bicycles, trafikklys=traffic lights, "
        "fotgjengerfelter=crosswalks, brannhydranter=fire hydrants, lastebiler=trucks, "
        "broer=bridges, busser=buses, motorsykler=motorcycles, palmer=palm trees, "
        "trapper=stairs, skorsteiner=chimneys.\n"
        "Return ONLY the JSON: {\"target\":\"...\",\"cols\":3,\"rows\":3}"
    )
    parts = [
        types.Part.from_text(text=instruction),
        types.Part.from_bytes(data=png, mime_type="image/png"),
    ]
    resp = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=120),
    )
    text = (resp.text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if not m:
        return ("unknown", 3, 3)
    try:
        obj = json.loads(m.group(0))
        target = (obj.get("target") or "").strip().lower()
        if target in NORWEGIAN_TARGET_MAP:
            target = NORWEGIAN_TARGET_MAP[target]
        cols = int(obj.get("cols") or 3)
        rows = int(obj.get("rows") or 3)
        cols = max(2, min(5, cols))
        rows = max(2, min(5, rows))
        return (target or "unknown", cols, rows)
    except Exception:
        return ("unknown", 3, 3)


def read_prompt(client: genai.Client, model: str, png: bytes) -> str:
    """Legacy single-target reader (kept for callers not using grid info)."""
    return read_prompt_and_grid(client, model, png)[0]


# ── Step C: yes/no on a single tile (legacy, kept for fallback) ──────────
def tile_matches(client: genai.Client, model: str, tile_png: bytes, target: str) -> bool:
    parts = [
        types.Part.from_text(
            text=(
                f"Does this image contain {target} (any part visible counts)? "
                "Answer with one word: yes or no."
            )
        ),
        types.Part.from_bytes(data=tile_png, mime_type="image/png"),
    ]
    resp = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(temperature=0.0, max_output_tokens=10),
    )
    ans = (resp.text or "").strip().lower()
    return ans.startswith("y")


# ── Step C': pick tiles via the FULL captcha image (spatial context) ─────
def pick_tiles_from_full(client: genai.Client, model: str, full_png: bytes,
                         target: str, cols: int, rows: int) -> list[tuple[int, int]]:
    """Return list of (col, row) tile coordinates to click. Aggressive
    inclusion: reCAPTCHA expects you to click tiles where ANY part of the
    object is visible, including small/partial/blurred instances."""
    instruction = (
        f"You are solving a reCAPTCHA. The image shows a header (with the "
        f"prompt and possibly a reference picture in the top-right), then a "
        f"{rows}x{cols} grid of {rows*cols} tiles below it, then a footer "
        f"with refresh / audio / info buttons and a Verify button.\n\n"
        f"TASK: identify every tile in the grid that shows {target} — "
        f"INCLUDING tiles where only a portion is visible, where the object "
        f"is small in the background, partially occluded, blurred, at an "
        f"angle, etc. reCAPTCHA penalizes you for missing tiles, so err on "
        f"the side of CLICKING when in doubt. Tiles that share an object "
        f"split across the grid lines should ALL be clicked.\n\n"
        f"Examples for '{target}':\n"
        f" - if even a fender/wheel/headlight of a car is in a tile → click it\n"
        f" - if a traffic light pole partially extends into a tile → click it\n"
        f" - if a bicycle is half-behind a person → click it\n\n"
        f"Tile rows are 0..{rows-1} (top-to-bottom). Cols are 0..{cols-1} "
        f"(left-to-right). Return ONLY a JSON array of [row, col] pairs for "
        f"every tile to click. Examples:\n"
        f"  [[0,1],[1,0],[1,1],[1,2],[2,2]]\n"
        f"  []  (only if NO tile in the grid shows {target} at all)\n\n"
        f"Be aggressive. A typical reCAPTCHA puzzle has 3-7 matching tiles."
    )
    parts = [
        types.Part.from_text(text=instruction),
        types.Part.from_bytes(data=full_png, mime_type="image/png"),
    ]
    resp = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(temperature=0.2, max_output_tokens=2048),
    )
    text = (resp.text or "").strip()
    print(f"    [pick_tiles] model raw ({len(text)} chars): {text[:600]}")
    # Strip code fences and extract first JSON array
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    # Robust: find every [int, int] pair, anywhere in the text.
    pairs = re.findall(r"\[\s*(\d+)\s*,\s*(\d+)\s*\]", text)
    out: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for a, b in pairs:
        r, c = int(a), int(b)
        if 0 <= r < rows and 0 <= c < cols and (c, r) not in seen:
            out.append((c, r))
            seen.add((c, r))
    return out


# ── Step B: get JS-enumerated tile rects from the captcha iframe ─────────
TILE_QUERY_JS = r"""
(() => {
  const recap = [...document.querySelectorAll('iframe[src*=recaptcha]')]
    .find(f => f.offsetWidth > 100 && f.offsetHeight > 100);
  if (!recap) return {ok: false, reason: 'no captcha iframe visible'};
  const r = recap.getBoundingClientRect();
  return {
    ok: true,
    captcha: {x: Math.round(r.left), y: Math.round(r.top),
              w: Math.round(r.width), h: Math.round(r.height)},
  };
})()
"""


def find_captcha_rect(cdp: CDPClient) -> Optional[dict]:
    res = cdp.call("Runtime.evaluate", {"expression": TILE_QUERY_JS, "returnByValue": True})
    val = res.get("result", {}).get("value", {})
    return val.get("captcha") if val.get("ok") else None


def find_verify_button_rect(cdp: CDPClient) -> Optional[dict]:
    """Heuristic: the Verify button is a fixed element below the tile grid."""
    js = r"""
    (() => {
      const recap = [...document.querySelectorAll('iframe[src*=recaptcha]')]
        .find(f => f.offsetWidth > 100 && f.offsetHeight > 100);
      if (!recap) return null;
      const r = recap.getBoundingClientRect();
      // Verify button area is bottom strip of the captcha iframe.
      // We can't see inside cross-origin iframe, so we hardcode the expected
      // position: bottom-right area, ~30px tall, 80px wide, with ~8px padding.
      return {
        x: Math.round(r.left + r.width - 88),
        y: Math.round(r.top + r.height - 36),
        w: 80,
        h: 28,
      };
    })()
    """
    res = cdp.call("Runtime.evaluate", {"expression": js, "returnByValue": True})
    return res.get("result", {}).get("value")


def detect_grid(cap_w: int, cap_h: int) -> tuple[int, int, dict]:
    """
    Return (cols, rows, grid_rect) where grid_rect = {x_off, y_off, w, h}
    in iframe-local coords. reCAPTCHA layouts:
      - 3x3 grid with prompt header (~110px), grid is roughly square
      - 4x4 grid with prompt header (~80px), grid is roughly square
    We probe both layouts based on iframe height.
    """
    # iframe inner area (after header):
    #   3x3 layout: ~410 tall (header 110, grid 300, footer 0 or verify-only)
    #   4x4 layout: ~410-580 tall depending
    # captcha widget total: ~410-580 height.
    # Heuristic: compute aspect of the part below the header.
    # For now assume 3x3 grid. We refine after first round if needed.
    header_h = 110
    footer_h = 60
    grid_w = cap_w
    grid_h = cap_h - header_h - footer_h
    aspect = grid_h / grid_w if grid_w else 1
    # 3x3 if grid is roughly square; 4x4 if shorter (more horizontal cells)
    cols = 3
    rows = 3
    return cols, rows, {"x_off": 0, "y_off": header_h, "w": grid_w, "h": grid_h}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", help="CDP page targetId; defaults to first form page")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-puzzles", type=int, default=5,
                    help="how many puzzle iterations to attempt before giving up")
    ap.add_argument("--save-shots", default="/tmp/captcha-v2")
    args = ap.parse_args()

    pages = list_pages()
    if args.target:
        page = next((p for p in pages if p["id"] == args.target), None)
        if not page:
            print(f"target {args.target} not found")
            return 2
    else:
        page = next((p for p in pages if "forms" in p.get("url", "")), pages[0] if pages else None)
        if not page:
            print("no pages")
            return 2

    print(f"[v2] target {page['id']} url={page.get('url','')[:80]}")
    print(f"[v2] model={args.model}")

    cdp = CDPClient(page["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")

    client = genai.Client(api_key=GEMINI_API_KEY)
    Path(args.save_shots).mkdir(parents=True, exist_ok=True)

    for puzzle in range(1, args.max_puzzles + 1):
        # First, see if captcha is even visible.
        rect = find_captcha_rect(cdp)
        if not rect:
            print(f"[puzzle {puzzle}] no captcha visible — done")
            cdp.close()
            return 0

        print(f"[puzzle {puzzle}] captcha rect: {rect}")
        # Capture full viewport (CDP scale!=1 ate the iframe content), crop in Pillow.
        full_png = cdp.screenshot()
        cap_png = crop_png(full_png, rect["x"], rect["y"], rect["w"], rect["h"])
        cap_path = Path(args.save_shots) / f"puzzle-{puzzle}-cap.png"
        cap_path.write_bytes(cap_png)

        # Step A: read prompt + grid layout in one call
        target, det_cols, det_rows = read_prompt_and_grid(client, args.model, cap_png)
        print(f"[puzzle {puzzle}] target='{target}' detected grid={det_cols}x{det_rows}")
        if target == "unknown" or not target:
            print(f"[puzzle {puzzle}] couldn't read prompt — bailing")
            cdp.close()
            return 3

        # Step B: use detected grid layout
        cols, rows = det_cols, det_rows
        # 3x3 layout: header ~110, grid ~290, footer ~60. 4x4: header ~80 grid ~410.
        if rows == 4:
            header_h, footer_h = 80, 60
        else:
            header_h, footer_h = 110, 60
        grid_h = rect["h"] - header_h - footer_h
        grid_rect = {"x_off": 0, "y_off": header_h, "w": rect["w"], "h": grid_h}
        print(f"[puzzle {puzzle}] grid: {cols}x{rows} inside {grid_rect}")

        # Step C: classify by giving Gemini the FULL captcha image with spatial context.
        # (Per-tile classification was too conservative: Gemini said "no" on
        # tiles where the object spans multiple tiles only partially.)
        tile_w = grid_rect["w"] / cols
        tile_h = grid_rect["h"] / rows
        matches = pick_tiles_from_full(client, args.model, cap_png, target, cols, rows)
        print(f"  picked tiles: {matches}")

        # Step D: click matching tiles at their viewport-space centers
        for (c, r) in matches:
            click_x = int(rect["x"] + grid_rect["x_off"] + (c + 0.5) * tile_w)
            click_y = int(rect["y"] + grid_rect["y_off"] + (r + 0.5) * tile_h)
            print(f"  click ({click_x},{click_y}) tile [{r},{c}]")
            cdp.click(click_x, click_y)
            time.sleep(0.4)

        if not matches:
            print(f"[puzzle {puzzle}] no matches found — clicking Skip if visible")
            # Skip button location: bottom-LEFT area of the verify strip
            skip = {"x": rect["x"] + 12, "y": rect["y"] + rect["h"] - 22}
            cdp.click(skip["x"], skip["y"])
            time.sleep(2)
            continue

        # Step E: click Verify
        time.sleep(1.0)
        verify = find_verify_button_rect(cdp)
        if verify:
            vx = verify["x"] + verify["w"] // 2
            vy = verify["y"] + verify["h"] // 2
            print(f"  verify click ({vx},{vy})")
            cdp.click(vx, vy)
        time.sleep(3.0)  # let new puzzle render or widget collapse

    print("[v2] hit max puzzle iterations without verifying done")
    cdp.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
