#!/usr/bin/env python3
"""
solve_captcha.py — autonomous captcha solver using Gemini Vision + mac_control.

Lifts the click + screenshot primitives from ~/mac-control/mac_control.py
and pairs them with Gemini Vision (free tier API). Loops until Gemini reports the
challenge is done.

Usage:
    python3 solve_captcha.py [--max-rounds N] [--browser-app "Google Chrome"]

Approach:
    1. Bring browser to foreground
    2. Screenshot full screen
    3. Send to Gemini 2.5 Flash with a structured prompt: "you see a captcha
       challenge on screen. Return JSON for the next click that makes progress."
    4. Click via mac_control (Fitts's-law-timed mouse, jitter, hover delay)
    5. Repeat until Gemini says done OR max rounds reached
"""

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Make mac_control importable
sys.path.insert(0, os.path.expanduser("~/mac-control"))
import mac_control  # type: ignore

# Use the new google-genai SDK
from google import genai
from google.genai import types


GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    sys.exit("error: set GEMINI_API_KEY in your environment")

# Free-tier-friendly vision model
DEFAULT_MODEL = "gemini-2.5-flash"


SYSTEM_PROMPT = """\
You are a captcha-solving assistant guiding a real mouse on a real macOS desktop.

Each turn you see a screenshot of the current screen. Your job is to identify the
captcha challenge that's visible and return ONE click that makes progress, or
report that the challenge is done.

Rules:
- Coordinates must be in the SAME pixel space as the screenshot.
- For "select all images of X" challenges (reCAPTCHA), click ONE matching image
  per turn. The next screenshot will show the new state. If no images match,
  click the Verify / Skip button.
- For Arkose / FunCaptcha, click the rotation arrows or the matching object.
- For "I am not a robot" checkboxes, click the checkbox.
- Always check if a Submit / Verify / Continue button is the right next step.
- If the captcha appears solved (success message, page advanced, or no captcha
  visible) return {"done": true, "reason": "..."}.

Return ONLY raw JSON with one of these shapes:
  {"action": "click", "x": <int>, "y": <int>, "label": "<short description>"}
  {"action": "wait", "seconds": <float>, "reason": "<why>"}
  {"done": true, "reason": "<short>"}
  {"error": "<why I cannot proceed>"}
"""


def focus_app(name: str) -> None:
    """Bring an app to foreground via osascript."""
    try:
        subprocess.run(
            ["osascript", "-e", f'tell application "{name}" to activate'],
            check=False,
            timeout=3,
        )
    except Exception:
        pass
    time.sleep(0.4)


def capture_screen(path: Path) -> tuple[int, int]:
    """Capture full screen via macOS screencapture, return (w, h) in pixels."""
    mac_control.screenshot(str(path))
    # Read width/height via sips (no PIL needed)
    out = subprocess.check_output(
        ["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)]
    ).decode()
    w = int(re.search(r"pixelWidth:\s+(\d+)", out).group(1))
    h = int(re.search(r"pixelHeight:\s+(\d+)", out).group(1))
    return w, h


def parse_decision(text: str) -> dict:
    """Strip code fences and parse JSON."""
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    # Trim trailing junk after the JSON object
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        s = m.group(0)
    return json.loads(s)


def ask_gemini(client: genai.Client, model: str, image_bytes: bytes, history_note: str) -> dict:
    parts = [
        types.Part.from_text(text=SYSTEM_PROMPT + "\n\nContext: " + history_note),
        types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
    ]
    resp = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            temperature=0.1,
            max_output_tokens=512,
        ),
    )
    text = (resp.text or "").strip()
    return parse_decision(text)


def perform(decision: dict, screen_w: int, screen_h: int, capture_w: int, capture_h: int) -> str:
    """Execute the decision. Returns a short status string."""
    if decision.get("done"):
        return f"DONE — {decision.get('reason','')}"
    if decision.get("error"):
        return f"ERROR — {decision['error']}"
    if decision.get("action") == "wait":
        secs = float(decision.get("seconds", 1.5))
        time.sleep(min(secs, 5.0))
        return f"waited {secs:.1f}s — {decision.get('reason','')}"
    if decision.get("action") == "click":
        gx = int(decision.get("x", 0))
        gy = int(decision.get("y", 0))
        # Map from capture pixel space to logical screen space (which pyautogui uses)
        sx = int(gx * mac_control.SCREEN_WIDTH / capture_w)
        sy = int(gy * mac_control.SCREEN_HEIGHT / capture_h)
        mac_control.mouse_click(sx, sy)
        return f"clicked ({sx},{sy}) — {decision.get('label','')}"
    return f"unknown decision: {json.dumps(decision)[:120]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-rounds", type=int, default=20)
    ap.add_argument("--browser-app", default="Google Chrome")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--note", default="")
    args = ap.parse_args()

    print(f"[solve_captcha] starting — model={args.model} max-rounds={args.max_rounds}")
    print(f"[solve_captcha] focusing {args.browser_app}")
    focus_app(args.browser_app)

    client = genai.Client(api_key=GEMINI_API_KEY)
    history: list[str] = []

    for r in range(1, args.max_rounds + 1):
        ts = int(time.time())
        shot = Path(f"/tmp/captcha-shot-{ts}.png")
        cap_w, cap_h = capture_screen(shot)
        with open(shot, "rb") as f:
            data = f.read()

        note = "Round {}/{}. ".format(r, args.max_rounds) + args.note
        if history:
            note += "\nPrevious actions:\n- " + "\n- ".join(history[-5:])

        try:
            decision = ask_gemini(client, args.model, data, note)
        except Exception as e:
            print(f"[round {r}] gemini error: {e}")
            time.sleep(3)
            continue

        result = perform(decision, mac_control.SCREEN_WIDTH, mac_control.SCREEN_HEIGHT, cap_w, cap_h)
        history.append(result)
        print(f"[round {r}] {json.dumps(decision)[:140]}")
        print(f"[round {r}] -> {result}")

        if decision.get("done") or decision.get("error"):
            return 0 if decision.get("done") else 2

        # Brief pause for the page to settle after a click
        time.sleep(1.6)

    print("[solve_captcha] hit max rounds without done")
    return 1


if __name__ == "__main__":
    sys.exit(main())
