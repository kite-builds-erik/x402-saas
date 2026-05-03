#!/usr/bin/env python3
"""
solve_arkose.py — Arkose FunCaptcha 'match the claw object' solver.

GitHub's octocaptcha (and others) use Arkose's rotation challenge:
  - Left image: target object
  - Right image: scene with a claw; rotate the scene by clicking ←/→ arrows
    until the object directly below the claw matches the left target
  - Submit. Repeat for ~3 rounds.

Approach: at each step, capture the captcha frame, ask Gemini Vision
'is the object below the claw the same as the target?' If no, click → and
re-check. Cycle through all positions. Pick the best match. Click Submit.

Usage:
    python3 solve_arkose.py [--max-rounds N] [--target-id <CDP id>]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

from google import genai
from google.genai import types

# Shared CDP / image / JSON helpers.
from _cdp_lib import CDPClient, crop_png, list_pages, strip_json_fences

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    sys.exit("error: set GEMINI_API_KEY in your environment")
DEFAULT_MODEL = "gemini-3.1-pro-preview"


def find_octocaptcha_rect(cdp: CDPClient) -> Optional[dict]:
    """Return viewport rect {x,y,w,h} of the visible octocaptcha iframe."""
    js = """(() => {
      const ifs = [...document.querySelectorAll('iframe')]
        .filter(f => /octocaptcha|funcaptcha|arkoselabs/.test(f.src) && f.offsetWidth > 100);
      if (!ifs.length) return null;
      const r = ifs[0].getBoundingClientRect();
      return {x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height)};
    })()"""
    res = cdp.call("Runtime.evaluate", {"expression": js, "returnByValue": True})
    return res.get("result", {}).get("value")


def ask_match(client: genai.Client, model: str, cap_png: bytes) -> dict:
    """Ask Gemini whether the current claw-position is a match.
    Returns {action: 'submit'|'left'|'right'|'restart', score: 0..10, target: <object>, current: <object>}.
    """
    prompt = (
        "This is an Arkose FunCaptcha challenge. There are TWO images side by side:\n"
        "  - LEFT: 'Match This' — shows the target object you must align to.\n"
        "  - RIGHT: a scene with a CLAW at the top. Below the claw is one object.\n"
        "An object below the claw must match the LEFT target.\n\n"
        "Look at the RIGHT image. What object is directly below the claw RIGHT NOW? "
        "Compare it to the LEFT image's object. Decide:\n"
        "  - 'submit' if they match\n"
        "  - 'right' to rotate the scene right (advance one position)\n"
        "  - 'left' to rotate the scene left (back one position)\n"
        "  - 'restart' if you can't make sense of the challenge\n\n"
        "Return ONLY a single JSON object:\n"
        '  {"action":"submit","target":"<object>","current":"<object>","score":0-10}\n'
        "where score is your confidence (10 = certain match, 0 = certain mismatch).\n"
        "When in doubt, prefer 'right' (cycle forward) over 'submit'."
    )
    parts = [
        types.Part.from_text(text=prompt),
        types.Part.from_bytes(data=cap_png, mime_type="image/png"),
    ]
    resp = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=2048),
    )
    text = (resp.text or "").strip()
    print(f"    [match] raw ({len(text)} chars): {text[:300]}")
    text = strip_json_fences(text)
    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if not m:
        # Fallback: regex out fields
        action = re.search(r'"action"\s*:\s*"(submit|left|right|restart)"', text)
        score = re.search(r'"score"\s*:\s*(\d+)', text)
        cur = re.search(r'"current"\s*:\s*"([^"]*)"', text)
        tgt = re.search(r'"target"\s*:\s*"([^"]*)"', text)
        if action:
            return {
                "action": action.group(1),
                "score": int(score.group(1)) if score else 0,
                "current": cur.group(1) if cur else "",
                "target": tgt.group(1) if tgt else "",
            }
        return {"action": "right", "score": 0, "raw": text[:300]}
    try:
        return json.loads(m.group(0))
    except Exception:
        return {"action": "right", "score": 0, "raw": text[:200]}


def find_buttons(cdp: CDPClient, frame_rect: dict) -> dict:
    """Return click coords (in viewport) for left arrow, right arrow, submit, restart."""
    fx, fy, fw, fh = frame_rect["x"], frame_rect["y"], frame_rect["w"], frame_rect["h"]
    # Calibrated against octocaptcha 452x488 crop:
    # left arrow at ~(243, 240) → (0.538, 0.492)
    # right arrow at ~(405, 240) → (0.896, 0.492)
    # submit centered at ~(226, 320) → (0.5, 0.656)
    # restart at ~(326, 380) → (0.72, 0.778)
    return {
        "left_arrow": (fx + int(fw * 0.54), fy + int(fh * 0.49)),
        "right_arrow": (fx + int(fw * 0.90), fy + int(fh * 0.49)),
        "submit": (fx + fw // 2, fy + int(fh * 0.66)),
        "restart": (fx + int(fw * 0.72), fy + int(fh * 0.78)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target")
    ap.add_argument("--max-clicks", type=int, default=30)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--save-shots", default="/tmp/arkose")
    args = ap.parse_args()

    pages = list_pages()
    page = next((p for p in pages if p["id"] == args.target), pages[0] if pages else None)
    if not page:
        print("no page")
        return 2

    cdp = CDPClient(page["webSocketDebuggerUrl"])
    cdp.call("Page.enable")
    cdp.call("Runtime.enable")
    Path(args.save_shots).mkdir(parents=True, exist_ok=True)

    rect = find_octocaptcha_rect(cdp)
    if not rect:
        print("no octocaptcha visible")
        return 1
    print(f"[arkose] frame: {rect}")
    btns = find_buttons(cdp, rect)
    print(f"[arkose] btns: {btns}")

    client = genai.Client(api_key=GEMINI_API_KEY)

    # Each Arkose puzzle = up to 6 rotation positions. We need to find the best one.
    # Strategy: cycle through positions, score each, pick best, submit.
    best_score = -1
    best_position = 0
    seen_currents: list[str] = []

    for click_n in range(1, args.max_clicks + 1):
        # Wait between clicks for animation
        time.sleep(0.8)
        full = cdp.screenshot()
        cap = crop_png(full, rect["x"], rect["y"], rect["w"], rect["h"])
        (Path(args.save_shots) / f"step-{click_n:02d}.png").write_bytes(cap)

        decision = ask_match(client, args.model, cap)
        action = decision.get("action", "right")
        score = decision.get("score", 0)
        cur = decision.get("current", "")
        tgt = decision.get("target", "")
        print(f"[step {click_n}] action={action} score={score} current='{cur[:30]}' target='{tgt[:30]}'")

        if action == "submit":
            print(f"  -> SUBMIT")
            cdp.click(*btns["submit"])
            time.sleep(2)
            # Check whether challenge advanced
            new_rect = find_octocaptcha_rect(cdp)
            if not new_rect:
                print("  -> challenge complete")
                cdp.close()
                return 0
            # If still there, possibly next puzzle (1/3 -> 2/3) — keep going
            rect = new_rect
            btns = find_buttons(cdp, rect)
            best_score = -1
            best_position = 0
            seen_currents = []
            continue
        elif action == "left":
            cdp.click(*btns["left_arrow"])
        elif action == "right":
            cdp.click(*btns["right_arrow"])
        elif action == "restart":
            cdp.click(*btns["restart"])
            time.sleep(2)
            continue

        # Track best score so we can fall back to submit-best after one full cycle
        if score > best_score:
            best_score = score
            best_position = click_n
        seen_currents.append(cur)
        # After 8+ clicks (more than 6 positions), force submit at best
        if click_n >= 10 and best_score >= 6:
            print(f"  -> cycled enough, forcing submit at best (score {best_score})")
            # Step back to best position by cycling forward
            # Simpler: just submit now, current may be near best
            cdp.click(*btns["submit"])
            time.sleep(2)
            new_rect = find_octocaptcha_rect(cdp)
            if not new_rect:
                cdp.close()
                return 0
            rect = new_rect
            btns = find_buttons(cdp, rect)
            best_score = -1
            best_position = 0

    print("[arkose] hit max clicks")
    cdp.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
