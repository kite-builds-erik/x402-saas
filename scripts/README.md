# Captcha-solver scripts

Two-pronged autonomous captcha solver, lifted from `~/mac-control/`
(an earlier macOS automation experiment). Both use Gemini 2.5 Flash Vision
on the free tier.

## `solve_captcha.py` — pyautogui path

Uses `mac_control.py`'s real-mouse + screenshot primitives. Works against
any visible screen (not just one browser).

**Requirement:** the target window must be on the **currently visible macOS
Space**. The OpenClaw browser by default lives on its own Space and pyautogui
can't see it. Move the OpenClaw browser window to the current Space first
(Mission Control → drag, or right-click app icon → Options → Assign To: This
Desktop), then run the script.

```bash
python3 solve_captcha.py --max-rounds 20 \
  --browser-app "Google Chrome" \
  --note "On Google Forms Base Grant Nominations. Solve the reCAPTCHA challenge."
```

## `cdp_solve_captcha.py` — CDP path

Uses Chrome DevTools Protocol over WebSocket to screenshot + click the
OpenClaw browser directly, regardless of which Space it's on. Faster and
more reliable than pyautogui — but requires Chrome to be launched with
`--remote-allow-origins=*` (or with no Origin filter).

**Current OpenClaw browser launch is missing that flag**, so this script
errors with `403 Forbidden` on the WS handshake. Two ways to fix:

1. **Edit OpenClaw config** to pass extra Chrome flags. Look for `browser`
   plugin config in `~/.openclaw/openclaw.json` and add
   `chromiumArgs: ["--remote-allow-origins=*"]` (exact key TBD by inspecting
   the OpenClaw plugin schema).
2. **Restart OpenClaw browser manually** with the flag:
   ```bash
   pkill -f "openclaw/browser/openclaw/user-data"
   "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
     --user-data-dir=~/.openclaw/browser/openclaw/user-data \
     --remote-debugging-port=18800 \
     --remote-allow-origins='*' &
   ```
   Then the script works:
   ```bash
   python3 cdp_solve_captcha.py --auto-pick \
     --max-rounds 20 \
     --note "Solve the reCAPTCHA on Google Forms."
   ```

## What both do

1. Screenshot the page (CDP `Page.captureScreenshot` or macOS `screencapture`)
2. Send the PNG to Gemini 2.5 Flash with a structured prompt
3. Gemini returns JSON: `{action: 'click', x, y, label}` or `{done: true}`
4. Click the coordinate (CDP `Input.dispatchMouseEvent` or `pyautogui.click`)
5. Loop until done, error, or max rounds

The CDP path is preferred when available (faster, no Space-management).
The pyautogui path is the fallback (works for ANY GUI app on the desktop).

## Free-tier sustainability

Gemini 2.5 Flash on the free tier: 1500 requests/day, 1M tokens/day.
Each captcha takes 2-12 rounds × 1 image (~150KB) = well under any limit.
At runtime cost ≈ $0 per solve.
