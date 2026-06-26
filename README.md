# Attendance → Slack poller

Replaces the Google Apps Script. Checks the eTimeOffice biometric API and posts
each **new** punch to Slack. State (the bookmark + recently-posted punches) lives
in `state.json`, so it never double-posts or re-dumps history.

Runs for **free** on GitHub Actions — no server, nothing to keep powered on.

## How it runs (free, on GitHub Actions)

`.github/workflows/poll.yml` runs the poller every ~5 minutes, posts any new
punches to Slack, and commits the updated `state.json` back to the repo so the
next run continues where the last one stopped.

> Scheduled runs on GitHub are best-effort — usually on time, occasionally a few
> minutes late under load. So expect posts "within ~5 minutes," not to-the-second.

### Setup (one time)

1. **Push this folder to GitHub** as the repo root.
2. Make the repo **Public** — public repos get *unlimited* free Actions minutes.
   (Safe to do: there are no secrets in the code; they live in encrypted Secrets.)
3. Add the secrets: repo **Settings → Secrets and variables → Actions → New
   repository secret**, three of them:
   - `ETIME_USERNAME`
   - `ETIME_PASSWORD`
   - `SLACK_WEBHOOK`
4. Go to the **Actions** tab → enable workflows → run **attendance-poll** once
   via **Run workflow** to confirm it works. After that it runs on its own.

### Cut over from Google (after the workflow is posting)

1. Confirm punches are posting from the Action.
2. In the old Apps Script editor: **Triggers** → delete the `checkAttendance`
   trigger so Google stops (otherwise both post → duplicates).
3. Rotate the Slack webhook (the old URLs were exposed) and update the
   `SLACK_WEBHOOK` secret.

## Test locally first (optional)

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in ETIME_USERNAME, ETIME_PASSWORD, SLACK_WEBHOOK
RUN_ONCE=1 python poller.py # one poll, then exits
```

Leave off `RUN_ONCE` to run it as an always-on loop (for a VM/PC instead of CI).

## Config

All via env vars — see `.env.example`. Required: `ETIME_USERNAME`,
`ETIME_PASSWORD`, `SLACK_WEBHOOK`.
