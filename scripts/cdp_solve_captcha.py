#!/usr/bin/env python3
"""
cdp_solve_captcha.py — autonomous captcha solver via Chrome DevTools Protocol + Gemini Vision.

Lifts the *idea* from /Users/botbot/mac-control/live_dual_agent.py (Gemini-vision-driven
mouse control) but uses CDP instead of pyautogui, so it works even when the browser is
on a different macOS Space.

Pipeline per round:
  1. CDP Page.captureScreenshot → PNG bytes
  2. Gemini 2.5 Flash sees the screenshot + a one-line context note
  3. Gemini returns JSON: click / wait / done / error
  4. CDP Input.dispatchMouseEvent at the returned (x, y) viewport coords

Usage:
    python3 cdp_solve_captcha.py --target <CDP-targetId> [--max-rounds N] [--note "..."]
    python3 cdp_solve_captcha.py --auto-pick   # picks the first non-newtab page
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional, List, Tuple
from urllib.request import urlopen, Request

import websocket  # pip install websocket-client

# google-genai SDK (new)
from google import genai
from google.genai import types

GEMINI_API_KEY = os.environ.get(
    "GEMINI_API_KEY",
    "REDACTED-USE-ENV-VAR",
)
DEFAULT_MODEL = "gemini-2.5-flash"
CDP_HTTP = os.environ.get("CDP_HTTP", "http://127.0.0.1:18800")


SYSTEM_PROMPT = """\
You are a captcha-solving assistant guiding mouse clicks inside a browser viewport.

Each turn you see one screenshot of the current viewport. Identify the captcha
challenge and emit ONE click that makes progress, OR mark done.

Rules:
- All coordinates are viewport pixels in the SAME image you see.
- For "select all images of X" reCAPTCHA challenges:
    * Click the next matching image tile, ONE per turn.
    * After all matching tiles look selected (highlighted), click "Verify".
    * If a new puzzle appears, continue.
    * If no matching images exist, click "Skip".
- For "I am not a robot" checkboxes, click the checkbox once.
- For Cloudflare Turnstile / Arkose, click the indicated checkbox or rotation arrow.
- If the captcha widget shows a green checkmark or success state, return done.
- Don't click "Send" / "Submit" / "Continue" buttons unless the user note tells you to.

Return ONLY raw JSON, one of:
  {"action":"click","x":<int>,"y":<int>,"label":"<short>"}
  {"action":"wait","seconds":<float>,"reason":"<short>"}
  {"done":true,"reason":"<short>"}
  {"error":"<why I cannot proceed>"}
"""


class CDPClient:
    def __init__(self, ws_url: str):
        # Chrome rejects WS with non-empty/cross-origin Origin header. Use empty origin.
        self.ws = websocket.create_connection(ws_url, timeout=20, origin="")
        self._next_id = 1

    def call(self, method: str, params: Optional[dict] = None, timeout: float = 20.0) -> dict:
        msg_id = self._next_id
        self._next_id += 1
        payload = {"id": msg_id, "method": method, "params": params or {}}
        self.ws.send(json.dumps(payload))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.ws.settimeout(max(0.1, deadline - time.time()))
            data = self.ws.recv()
            try:
                obj = json.loads(data)
            except Exception:
                continue
            if obj.get("id") == msg_id:
                if "error" in obj:
                    raise RuntimeError(f"CDP error on {method}: {obj['error']}")
                return obj.get("result", {})
        raise TimeoutError(f"CDP timeout waiting for {method}")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def list_targets() -> List[dict]:
    req = Request(f"{CDP_HTTP}/json", headers={"Accept": "application/json"})
    return json.loads(urlopen(req, timeout=5).read())


def pick_target(target_id: Optional[str], auto_pick: bool) -> dict:
    targets = list_targets()
    pages = [t for t in targets if t.get("type") == "page"]
    if target_id:
        for t in pages:
            if t.get("id") == target_id:
                return t
        raise SystemExit(f"target {target_id} not found among CDP pages")
    if auto_pick:
        for t in pages:
            url = t.get("url", "")
            if not url.startswith(("chrome://", "about:", "devtools://")):
                return t
        raise SystemExit("no non-newtab page found; pass --target <id>")
    raise SystemExit("pass --target <id> or --auto-pick")


def parse_decision(text: str) -> dict:
    s = text.strip()
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        s = m.group(0)
    return json.loads(s)


def ask_gemini(client: genai.Client, model: str, image_bytes: bytes, note: str) -> dict:
    parts = [
        types.Part.from_text(text=SYSTEM_PROMPT + "\n\nContext: " + note),
        types.Part.from_bytes(data=image_bytes, mime_type="image/png"),
    ]
    resp = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(temperature=0.1, max_output_tokens=512),
    )
    return parse_decision((resp.text or "").strip())


def cdp_screenshot(cdp: CDPClient) -> bytes:
    res = cdp.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
    return base64.b64decode(res["data"])


def cdp_viewport_size(cdp: CDPClient) -> Tuple[int, int]:
    """Returns the viewport pixel size for coordinate mapping."""
    res = cdp.call("Page.getLayoutMetrics", {})
    vp = res.get("cssVisualViewport") or res.get("layoutViewport") or {}
    w = int(vp.get("clientWidth") or 1280)
    h = int(vp.get("clientHeight") or 720)
    return w, h


def cdp_click(cdp: CDPClient, x: int, y: int) -> None:
    """Move + click + release at viewport coordinates."""
    cdp.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    time.sleep(0.08)
    cdp.call("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1
    })
    time.sleep(0.04)
    cdp.call("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1
    })


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target")
    ap.add_argument("--auto-pick", action="store_true")
    ap.add_argument("--max-rounds", type=int, default=20)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--note", default="")
    ap.add_argument("--save-shots", default="/tmp")
    args = ap.parse_args()

    target = pick_target(args.target, args.auto_pick)
    print(f"[cdp-solver] target id={target['id']} url={target.get('url','')[:80]}")
    print(f"[cdp-solver] model={args.model} max-rounds={args.max_rounds}")

    cdp = CDPClient(target["webSocketDebuggerUrl"])
    cdp.call("Page.enable", {})
    cdp.call("Runtime.enable", {})

    vp_w, vp_h = cdp_viewport_size(cdp)
    print(f"[cdp-solver] viewport ~{vp_w}x{vp_h}")

    client = genai.Client(api_key=GEMINI_API_KEY)
    history: List[str] = []

    for r in range(1, args.max_rounds + 1):
        try:
            png = cdp_screenshot(cdp)
        except Exception as e:
            print(f"[round {r}] screenshot error: {e}")
            time.sleep(2)
            continue

        Path(args.save_shots).mkdir(parents=True, exist_ok=True)
        shot_path = Path(args.save_shots) / f"cdp-shot-{int(time.time())}.png"
        shot_path.write_bytes(png)

        note = f"Round {r}/{args.max_rounds}. {args.note}"
        if history:
            note += "\nLast actions:\n- " + "\n- ".join(history[-5:])

        try:
            decision = ask_gemini(client, args.model, png, note)
        except Exception as e:
            print(f"[round {r}] gemini error: {e}")
            time.sleep(3)
            continue

        if decision.get("done"):
            print(f"[round {r}] DONE — {decision.get('reason','')}")
            cdp.close()
            return 0
        if decision.get("error"):
            print(f"[round {r}] ERROR — {decision['error']}")
            cdp.close()
            return 2
        if decision.get("action") == "wait":
            secs = float(decision.get("seconds", 1.5))
            print(f"[round {r}] wait {secs}s — {decision.get('reason','')}")
            time.sleep(min(secs, 5.0))
            history.append(f"wait {secs}s")
            continue
        if decision.get("action") == "click":
            x = int(decision.get("x", 0))
            y = int(decision.get("y", 0))
            label = decision.get("label", "")
            print(f"[round {r}] click ({x},{y}) — {label}")
            try:
                cdp_click(cdp, x, y)
            except Exception as e:
                print(f"[round {r}] click error: {e}")
            history.append(f"click ({x},{y}) [{label[:40]}]")
            time.sleep(1.5)  # let page settle
            continue

        print(f"[round {r}] unknown decision: {json.dumps(decision)[:140]}")
        history.append(f"unknown: {json.dumps(decision)[:60]}")

    print("[cdp-solver] hit max rounds without done")
    cdp.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
