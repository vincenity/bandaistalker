# OPTCG Event Watch

Checks Bandai's One Piece Card Game event pages about every 10 minutes and pings
your phone through Discord and/or ntfy when they change. It's free and runs on
GitHub, so your computer doesn't need to be on.

Watched by default:
- https://en.onepiece-cardgame.com/events/ (new tournaments, new Regionals season pages)
- https://en.onepiece-cardgame.com/events/regional-season2-26-27.html (new regionals and registration links)

## What an alert looks like

```
Events page changed
🚨 NEW EVENT PAGE(S):
• Regionals Season 3 -> https://en.onepiece-cardgame.com/events/regional-season3-26-27.html
➕ Added:
• Jan 9, 2027 | Top Cut | Peoria Civic Center ... | Link
```

## Setup (about 10 minutes)

### 1. Make the Discord webhook
1. In Discord, create a server just for yourself (or use one you own) and a channel such as `#optcg-alerts`.
2. Channel settings (gear icon) → **Integrations** → **Webhooks** → **New Webhook** → **Copy Webhook URL**.
3. On your phone's Discord app, open the channel → long-press → **Notification settings** → **All Messages**.
   To get a ping that cuts through muted servers, also copy your user ID
   (Settings → Advanced → turn on Developer Mode, then long-press your name → Copy User ID).

### 2. (Optional) Phone push with ntfy (works like a text)
1. Install the free **ntfy** app (iOS/Android).
2. Subscribe to a hard-to-guess topic name, e.g. `optcg-vince-8f3k2q`. Anyone who knows the name can read it, so make it random.

### 3. Put the code on GitHub
1. Create a free GitHub account if you don't have one, then **New repository** → name it `optcg-watch`.
   **Public** is recommended: Actions minutes are unlimited, and your secrets stay hidden.
2. Upload `monitor.py`, `README.md`, and `.github/workflows/watch.yml`. Keep the folder path exactly
   `.github/workflows/`; on the web, type `.github/workflows/watch.yml` as the file name in **Add file → Create new file**.
3. Repo **Settings → Secrets and variables → Actions → New repository secret**:
   - `DISCORD_WEBHOOK_URL`: the webhook URL from step 1
   - `DISCORD_MENTION` (optional): `<@YOUR_USER_ID>`
   - `NTFY_TOPIC` (optional): your ntfy topic name

### 4. Test it
**Actions** tab → enable workflows if asked → **Watch Bandai OPTCG events** → **Run workflow**.
Within a minute you should get a "✅ Now watching…" message for each page. After that you only hear
from it when something changes.

## Watching more pages
Edit the `PAGES` list at the top of `monitor.py`. When Bandai posts a Season 3 regionals page,
the watcher flags it as a NEW EVENT PAGE. Add it to `PAGES` so its regionals are tracked too.

## What it tells you in Discord (besides changes)

| Situation | What you get |
|---|---|
| Bandai blocks GitHub (403), shows a captcha, or the page comes back empty | ⚠️ after 30 min, with the likely cause and what to do; a reminder every 24 h; ✅ when it recovers |
| A watched page is removed or renamed (404) | ⚠️ telling you to find the new URL and update `PAGES` |
| Bandai's site is down or slow | ⚠️ after 30 min ("nothing to do"), then ✅ when it's back |
| Bandai redesigns a page | A warning inside the change alert that later alerts may be noisy |
| GitHub's 60-day inactivity pause | Prevented automatically: the watcher resets the timer daily. If that stops working for 7 days, ⚠️ with the deadline and how to fix it |
| The script or GitHub crashes | ⚠️ from a backup step with the log link (once per streak, not every 10 min) |
| An alert couldn't be sent | The change is retried on the next run |
| **Everything is dead** (repo deleted, webhook deleted, GitHub outage) | The 🫡 **weekly check-in** stops arriving. That's your signal to look. |

The 30-minute, 24-hour and weekly timings are adjustable at the top of `monitor.py`.

## Good to know
- GitHub's scheduler isn't exact; runs can lag 5-20 minutes during busy periods.
- `watch.yml` needs `actions: write` under `permissions:` for the 60-day keepalive. It's already there.
- If you also set up ntfy, every alert goes to both, so one channel can fail without you missing anything.
- Too many alerts from small edits? Add a pattern to `IGNORE_RE` in `monitor.py`.
