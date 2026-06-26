"""
Attendance poller — replaces the Google Apps Script.

Polls the eTimeOffice biometric API and posts each NEW punch to Slack.
State (the API bookmark + recently-posted punches) lives in a small JSON file
so it never re-posts or misses anything across runs.

Two ways to run:
  * One shot   (RUN_ONCE=1 python poller.py)  — used by GitHub Actions cron.
  * Always-on  (python poller.py)             — loops forever; for a VM/PC.

All config comes from environment variables — see .env.example.
"""

import base64
import json
import os
import sys
import time

import requests


def _load_dotenv(path=".env"):
    """Minimal .env loader (no extra dependency). KEY=VALUE per line; existing
    environment variables always win, so a host's real env vars override .env."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


_load_dotenv()

# ---- Config (env vars) -----------------------------------------------------
ETIME_BASE     = os.environ.get("ETIME_BASE", "https://api.etimeoffice.com/api")
CORPORATE_ID   = os.environ.get("CORPORATE_ID", "landeed")
ETIME_USERNAME = os.environ.get("ETIME_USERNAME", "")
ETIME_PASSWORD = os.environ.get("ETIME_PASSWORD", "")
SLACK_WEBHOOK  = os.environ.get("SLACK_WEBHOOK", "")
# Always-on loop interval, in seconds (ignored when RUN_ONCE is set).
POLL_SECONDS   = int(os.environ.get("POLL_SECONDS", "60"))
# Run exactly one poll then exit (used by the GitHub Actions cron).
RUN_ONCE       = os.environ.get("RUN_ONCE", "").lower() in ("1", "true", "yes")
# Safety cap: if a single poll returns more than this, assume the bookmark was
# reset and we're about to re-dump history. Advance the bookmark but DON'T post,
# so the channel never gets spammed. (Ported from the Apps Script.)
MAX_POST_PER_RUN = int(os.environ.get("MAX_POST_PER_RUN", "25"))
STATE_PATH     = os.environ.get("STATE_PATH", "state.json")
# How many recent punch-keys to remember (dedup guard; bounded so it can't grow).
SEEN_CAP       = int(os.environ.get("SEEN_CAP", "500"))


def log(msg):
    print(msg, flush=True)


# ---- State (JSON file) -----------------------------------------------------
def load_state():
    state = {"bookmark": "", "seen": []}
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                loaded = json.load(f)
            state["bookmark"] = str(loaded.get("bookmark", "")).strip()
            state["seen"] = list(loaded.get("seen", []))
        except Exception as e:
            log(f"WARNING: could not read {STATE_PATH} ({e}); starting fresh.")
    return state


def save_state(state):
    state["seen"] = state["seen"][-SEEN_CAP:]
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        # Deterministic output so an unchanged poll produces identical bytes
        # (keeps the git commit-back in CI a no-op when nothing changed).
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_PATH)


def ensure_bookmark(state):
    """Returns (bookmark, cold_start). cold_start is True when we had to
    initialise the bookmark from scratch — the only time a huge backlog batch
    is expected (and should be suppressed rather than spammed to Slack)."""
    bm = str(state.get("bookmark") or "").strip()
    if not bm or bm == "0":
        # Same init scheme as the Apps Script: MMYYYY$0 for the current month.
        t = time.localtime()
        bm = f"{t.tm_mon:02d}{t.tm_year}$0"
        state["bookmark"] = bm
        log(f"Initialised bookmark to: {bm} (cold start)")
        return bm, True
    return bm, False


# ---- Slack -----------------------------------------------------------------
def post_to_slack(text):
    if not SLACK_WEBHOOK:
        log("WARNING: SLACK_WEBHOOK not set — would have posted: " + text)
        return False
    try:
        resp = requests.post(SLACK_WEBHOOK, json={"text": text}, timeout=10)
        if resp.status_code != 200:
            # Unlike the old code, we DO check the response. A revoked webhook
            # returns 403 'invalid_token' — surface it instead of silently
            # swallowing it (that was the bug in the old app.py).
            log(f"Slack post failed {resp.status_code}: {resp.text}")
            return False
        return True
    except Exception as e:
        log(f"Slack post error: {e}")
        return False


# ---- eTimeOffice -----------------------------------------------------------
def fetch_punches(bookmark):
    token = base64.b64encode(
        f"{CORPORATE_ID}:{ETIME_USERNAME}:{ETIME_PASSWORD}:True".encode()
    ).decode()
    url = f"{ETIME_BASE}/DownloadLastPunchData?Empcode=ALL&LastRecord={requests.utils.quote(bookmark)}"
    resp = requests.get(url, headers={"Authorization": "Basic " + token}, timeout=30)
    if resp.status_code != 200:
        log(f"API returned {resp.status_code}: {resp.text[:300]}")
        return None, None
    try:
        data = resp.json()
    except Exception as e:
        log(f"JSON parse error: {e}\nBody: {resp.text[:300]}")
        return None, None

    max_record = str(
        data.get("MaxRecord") or data.get("maxRecord") or data.get("MAXRECORD")
        or data.get("MaxRecordId") or data.get("maxRecordId") or ""
    ).strip()
    punches = data.get("PunchData") or data.get("punchData") or data.get("Punchdata") or []
    return punches, max_record


def extract(punch):
    # Real eTimeOffice fields: Name, Empcode, PunchDate ("DD/MM/YYYY HH:MM:SS"),
    # ID (unique record id). Fallbacks kept in case the API casing ever varies.
    rid   = str(punch.get("ID") or punch.get("Id") or "")
    name  = punch.get("Name") or punch.get("EmpName") or punch.get("EmployeeName") or "Unknown"
    code  = str(punch.get("Empcode") or punch.get("EmpCode") or punch.get("EmployeeCode") or "")
    stamp = str(punch.get("PunchDate") or punch.get("Date") or punch.get("PunchTime") or "").strip()
    pdate, _, ptime = stamp.partition(" ")   # split "DD/MM/YYYY HH:MM:SS"
    return rid, name, code, pdate, ptime, stamp


# ---- One poll --------------------------------------------------------------
def poll_once(state):
    """Returns True if state changed (so the caller knows to persist it)."""
    bookmark, cold_start = ensure_bookmark(state)
    punches, max_record = fetch_punches(bookmark)
    if punches is None:
        return False  # error already logged; try again next run

    seen = set(state["seen"])
    # Only suppress on a cold start (a fresh bookmark pulling the whole month).
    # In normal operation, a big batch is just a busy morning — post it all.
    too_many = cold_start and len(punches) > MAX_POST_PER_RUN
    if too_many:
        log(f"WARNING: cold start with {len(punches)} punches (> {MAX_POST_PER_RUN}). "
            "Advancing the bookmark but NOT posting the backlog, to avoid spam.")

    changed = False
    posted = 0
    for punch in punches:
        rid, name, code, pdate, ptime, stamp = extract(punch)
        # Unique record ID is the best dedup key; fall back to person+timestamp.
        key = rid or f"{code}|{stamp}"
        if key in seen:
            continue
        when = f"*{ptime}* — {pdate}" if ptime else f"*{stamp}*"
        if not too_many:
            if post_to_slack(f"🟡 *{name}* punched in at {when}"):
                seen.add(key)
                state["seen"].append(key)
                changed = True
                posted += 1
        else:
            # Suppressed batch: record as seen so we don't post it later.
            seen.add(key)
            state["seen"].append(key)
            changed = True

    # The API returns MaxRecord="0" when there are NO new punches. Never save
    # that — it would force a full-month re-scan next run, whose oversized batch
    # gets suppressed by the safety cap and would silently drop a real punch.
    # Only advance the bookmark on a genuine new record.
    if max_record and max_record != "0" and max_record != state.get("bookmark"):
        state["bookmark"] = max_record
        changed = True
    if punches:
        log(f"Poll: {len(punches)} punches, {posted} posted, bookmark -> {max_record or bookmark}")
    return changed


def main():
    missing = [k for k in ("ETIME_USERNAME", "ETIME_PASSWORD", "SLACK_WEBHOOK")
               if not os.environ.get(k)]
    if missing:
        log("FATAL: missing required env vars: " + ", ".join(missing))
        log("Set them (see .env.example) and re-run.")
        sys.exit(1)

    state = load_state()

    if RUN_ONCE:
        log(f"One-shot poll of {ETIME_BASE}. STATE={STATE_PATH}")
        if poll_once(state):
            save_state(state)
        return

    log(f"Poller started. Polling {ETIME_BASE} every {POLL_SECONDS}s. STATE={STATE_PATH}")
    while True:
        try:
            if poll_once(state):
                save_state(state)
        except Exception as e:
            log(f"Unexpected error in poll loop: {e}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
