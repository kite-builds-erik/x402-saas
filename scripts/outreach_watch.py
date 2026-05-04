#!/usr/bin/env python3
"""x402-saas outreach watcher.

Polls the 7 GitHub PR threads where we have either filed an ecosystem
submission (#140 on coinbase/x402, #326 on xpaysh/awesome-x402) or
posted a comment as kite-builds-erik (#1, #21, #33, #43, #131 on
coinbase/x402). Reports any new activity to Telegram via the standard
print-to-stdout convention picked up by OpenClaw's operator framework.

State (last-seen comment IDs + PR state) lives next to the script in
.outreach_state.json so we don't re-spam the same signal.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib import request, parse, error

# Make tools/telegram_send importable for dedup + retry + logging
WORKSPACE = Path("/Users/botbot/.openclaw/workspace")
sys.path.insert(0, str(WORKSPACE))
try:
    from tools.telegram_send import send as tg_send
except Exception:
    tg_send = None

JOB_NAME = "x402-outreach-watch"
STATE_PATH = Path(__file__).resolve().parent.parent / ".outreach_state.json"
US_LOGIN = "kite-builds-erik"

# Direct Telegram delivery (used when run outside the OpenClaw agent loop, e.g.
# from launchd / system crontab — bypasses Anthropic API entirely).
OPENCLAW_CONFIG = Path("/Users/botbot/.openclaw/openclaw.json")
TG_CHAT_ID_DEFAULT = "6648541632"  # SP's Telegram chat


def _telegram_token() -> str | None:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if tok:
        return tok
    if not OPENCLAW_CONFIG.exists():
        return None
    try:
        cfg = json.loads(OPENCLAW_CONFIG.read_text(encoding="utf-8"))
        return (cfg.get("channels", {})
                   .get("telegram", {})
                   .get("botToken"))
    except Exception:
        return None


def _telegram_post(text: str) -> bool:
    """POST directly to Telegram Bot API. Returns True on 200."""
    token = _telegram_token()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", TG_CHAT_ID_DEFAULT)
    if not token or not chat_id:
        return False
    payload = parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False

# (label, owner, repo, number, kind)
# kind="ours" → we filed it; alert on any new comment + state change
# kind="commented" → we commented; alert on replies from non-us authors
PRS = [
    ("PR #140 (own coinbase ecosystem)",   "coinbase",        "x402",          140, "ours"),
    ("PR #326 (own xpaysh awesome-x402)",  "xpaysh",          "awesome-x402",  326, "ours"),
    ("PR #174 (own merit awesome-x402)",   "Merit-Systems",   "awesome-x402",  174, "ours"),
    ("PR #43 (DevDrops)",                  "coinbase",        "x402",           43, "commented"),
    ("PR #21 (Carbon&Cashmere)",           "coinbase",        "x402",           21, "commented"),
    ("PR #1 (WalletIQ)",                   "coinbase",        "x402",            1, "commented"),
    ("PR #33 (AgentLair)",                 "coinbase",        "x402",           33, "commented"),
    ("PR #131 (Gatefare)",                 "coinbase",        "x402",          131, "commented"),
]

UA = "x402-saas-outreach-watch/1.0 (+https://github.com/kite-builds-erik/x402-saas)"


def _http_json(url: str, timeout: int = 15) -> object:
    req = request.Request(url, headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _short(s: str, n: int = 200) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def check_pr(label: str, owner: str, repo: str, number: int, kind: str, state: dict) -> list[str]:
    """Return a list of new-signal lines for this PR."""
    key = f"{owner}/{repo}#{number}"
    prev = state.get(key, {})
    seen_comment_ids: set[int] = set(prev.get("seen_comment_ids", []))
    prev_pr_state = prev.get("pr_state")
    prev_merged = prev.get("merged", False)

    lines: list[str] = []

    # Fetch PR meta
    try:
        pr_meta = _http_json(f"https://api.github.com/repos/{owner}/{repo}/pulls/{number}")
    except error.HTTPError as exc:
        return [f"❌ {label}: HTTP {exc.code} fetching PR meta"]
    except Exception as exc:
        return [f"❌ {label}: error fetching PR meta — {exc}"]

    cur_state = pr_meta.get("state", "?")
    cur_merged = bool(pr_meta.get("merged"))
    merged_at = pr_meta.get("merged_at") or ""

    # State change
    if prev_pr_state is not None:
        if cur_state != prev_pr_state or cur_merged != prev_merged:
            if cur_merged:
                lines.append(f"✅ {label} MERGED at {merged_at}")
            elif cur_state == "closed":
                lines.append(f"🔴 {label} CLOSED unmerged")
            else:
                lines.append(f"ℹ️ {label} state {prev_pr_state}→{cur_state}")

    # Fetch comments
    try:
        comments = _http_json(f"https://api.github.com/repos/{owner}/{repo}/issues/{number}/comments?per_page=100")
    except Exception as exc:
        comments = []

    new_comment_ids: set[int] = set()
    for c in comments:
        cid = c.get("id")
        if cid is None:
            continue
        new_comment_ids.add(cid)
        if cid in seen_comment_ids:
            continue
        author = (c.get("user") or {}).get("login", "?")
        # Skip our own comments
        if author == US_LOGIN:
            continue
        # For "ours" PRs, any new comment is signal.
        # For "commented" PRs, also any non-us comment is signal (could be reply).
        body = c.get("body", "")
        mentions_us = US_LOGIN in body or "x402-saas" in body
        marker = " 👋 mentions us" if mentions_us else ""
        url = c.get("html_url") or ""
        lines.append(f"💬 {label}: @{author}{marker} — {_short(body)}\n   {url}")

    # Update state for this PR
    state[key] = {
        "pr_state": cur_state,
        "merged": cur_merged,
        # Cap stored IDs to avoid unbounded growth (keep last 500)
        "seen_comment_ids": sorted(new_comment_ids)[-500:],
    }
    return lines


def main() -> int:
    state = _load_state()
    all_lines: list[str] = []

    for label, owner, repo, number, kind in PRS:
        try:
            lines = check_pr(label, owner, repo, number, kind, state)
        except Exception as exc:
            lines = [f"❌ {label}: unhandled — {exc}"]
        all_lines.extend(lines)
        # Light pacing to stay polite to unauth API (60/hr cap)
        time.sleep(0.4)

    _save_state(state)

    if not all_lines:
        # Nothing to say. Print sentinel so OpenClaw forwards nothing.
        print("NO_REPLY")
        return 0

    header = f"📡 x402 outreach signal — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
    body = "\n\n".join(all_lines)
    msg = f"{header}\n\n{body}"

    # Delivery order:
    #   1) Direct Telegram POST when token is reachable (works for launchd /
    #      system cron — no Anthropic credit needed).
    #   2) tools.telegram_send wrapper (OpenClaw stdout convention).
    #   3) Plain stdout fallback.
    delivered = _telegram_post(msg)
    if not delivered and tg_send is not None:
        delivered = tg_send(msg, job=JOB_NAME)
    if not delivered:
        print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
