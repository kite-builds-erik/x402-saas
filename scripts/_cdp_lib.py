"""
_cdp_lib.py — shared Chrome DevTools Protocol helpers.

Used by:
  - cdp_solve_captcha.py        (v1 ad-hoc reCAPTCHA solver)
  - cdp_solve_captcha_v2.py     (decomposed reCAPTCHA solver)
  - solve_arkose.py             (Arkose FunCaptcha solver)

What used to be ~140 lines of CDP boilerplate per script (CDPClient class,
list_pages/pick_page, screenshot, click, JSON-fence stripping, Pillow crop)
now lives here once. Captcha-specific logic (Gemini prompts, button-position
heuristics, decision logic) stays per-script.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from typing import List, Optional, Tuple
from urllib.request import urlopen

import websocket  # pip install websocket-client


CDP_HTTP = os.environ.get("CDP_HTTP", "http://127.0.0.1:18800")


class CDPClient:
    """Minimal CDP-over-WebSocket client. Sends a request, blocks until the
    matching id comes back, returns `result`. Strips Origin header so a
    locally-launched Chrome with `--remote-allow-origins=*` accepts us."""

    def __init__(self, ws_url: str, default_timeout: float = 30.0):
        self.ws = websocket.create_connection(ws_url, timeout=default_timeout, origin="")
        self._next_id = 0
        self.default_timeout = default_timeout

    def call(self, method: str, params: Optional[dict] = None, timeout: Optional[float] = None) -> dict:
        self._next_id += 1
        msg_id = self._next_id
        self.ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + (timeout or self.default_timeout)
        while time.time() < deadline:
            try:
                self.ws.settimeout(max(0.1, deadline - time.time()))
                msg = json.loads(self.ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if msg.get("id") == msg_id:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"CDP timeout waiting for {method}")

    def screenshot(self, clip: Optional[dict] = None) -> bytes:
        """Full viewport, or clipped if `clip={x,y,width,height,scale?}` provided."""
        params: dict = {"format": "png", "captureBeyondViewport": False}
        if clip:
            params["clip"] = {**clip, "scale": clip.get("scale", 1)}
        res = self.call("Page.captureScreenshot", params)
        return base64.b64decode(res["data"])

    def click(self, x: int, y: int, settle: float = 0.05) -> None:
        """Move + press + release at viewport coords."""
        self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
        time.sleep(settle)
        self.call("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1,
        })
        time.sleep(settle)
        self.call("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1,
        })

    def evaluate(self, expression: str) -> object:
        """Convenience: Runtime.evaluate with returnByValue, returns the JS value."""
        res = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return res.get("result", {}).get("value")

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:
            pass


def list_pages() -> List[dict]:
    """All `type='page'` CDP targets via the HTTP /json endpoint."""
    return [t for t in json.loads(urlopen(f"{CDP_HTTP}/json", timeout=5).read())
            if t.get("type") == "page"]


def pick_page(target_id: Optional[str] = None,
              url_substring: Optional[str] = None,
              auto_pick: bool = False) -> dict:
    """
    Resolve a page to drive. Priority:
      1) target_id matches exactly
      2) url_substring is a substring of the page URL
      3) auto_pick: first non-newtab page
    Raises SystemExit if no match.
    """
    pages = list_pages()
    if target_id:
        for p in pages:
            if p["id"] == target_id:
                return p
        raise SystemExit(f"target id={target_id} not found among {len(pages)} pages")
    if url_substring:
        for p in pages:
            if url_substring in p.get("url", ""):
                return p
        raise SystemExit(f"no page URL contains {url_substring!r}")
    if auto_pick:
        for p in pages:
            if not p.get("url", "").startswith(("chrome://", "about:", "devtools://")):
                return p
        raise SystemExit("no non-newtab page found")
    raise SystemExit("pass target_id, url_substring, or auto_pick=True")


def crop_png(png: bytes, x: int, y: int, w: int, h: int) -> bytes:
    """Crop a region from a PNG; clamps to image bounds. Requires Pillow."""
    from PIL import Image  # local import keeps the lib optional for non-image flows
    im = Image.open(io.BytesIO(png))
    box = (max(0, x), max(0, y), min(im.width, x + w), min(im.height, y + h))
    out = io.BytesIO()
    im.crop(box).save(out, format="PNG")
    return out.getvalue()


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def strip_json_fences(text: str) -> str:
    """Remove ```json … ``` markdown wrappers Gemini sometimes emits."""
    return _FENCE_RE.sub("", text or "").strip()


def extract_json_object(text: str) -> Optional[dict]:
    """Find and parse the first balanced JSON object in `text`. Returns None on failure."""
    s = strip_json_fences(text)
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def extract_int_pairs(text: str) -> List[Tuple[int, int]]:
    """Pull every `[int, int]` pair out of arbitrary text. Used for tile-coord lists."""
    return [(int(a), int(b)) for a, b in re.findall(r"\[\s*(\d+)\s*,\s*(\d+)\s*\]", text or "")]
