# OPTCG Event Watch

Checks Bandai's One Piece Card Game event pages about every 10 minutes and pings
your phone through Discord and/or ntfy when they change. It's free and runs on
GitHub, so your computer doesn't need to be on.

## What it watches

It starts at the events page (https://en.onepiece-cardgame.com/events/) and follows every
link to another event page, up to two clicks deep. That covers every event type: Championships,
Regionals, Finals, Last Chance Qualifiers, Extra Grand Battle, Treasure Cups, Store Championships,
Store Tournaments, Heroines Battle, Fests, release events and so on. It watches each of those pages for changes.

- **A new event appears** (e.g. a new Treasure Cup or Extra Battle) → alert with a preview and
  any registration links, and that page is watched from then on.
- **A new sub-page appears** (e.g. Championship → *Regionals Season 3*) → same: alert and auto-watch.
  New regionals added to that page later trigger their own alerts.
- **Something on an existing page changes** (new regional row, registration link, dates) → alert
  showing what was added or removed.
- **A page is taken down** → it's quietly dropped, with a short note.

The "Past Events" archive is skipped on purpose.

## What an alert looks like

```
Championship 26-27 changed
🚨 Links to event pages added:
• Regionals Season 3 -> .../events/regional-season3-26-27.html (🆕 now watching, preview below)

🆕 New event page found: now watching
Regionals Season 3
• Registration opens Jan 10
• Feb 7 | Top Cut | Seattle Convention Center | Link
🔗 Link -> https://rk9.gg/...
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

## Tuning
Settings at the top of `monitor.py`:
- `SEED_PAGES`: pages that are always watched, even if nothing links to them.
- `MAX_DEPTH` (2) and `MAX_PAGES` (80): how far to follow links and how many pages to watch.
  You get a message if the page limit is hit.
- `EXCLUDE_RE`: URL patterns never to follow (the past-events archive by default).
- `IGNORE_RE`: text to ignore if small edits cause noisy alerts.

## What it tells you in Discord (besides changes)

| Situation | What you get |
|---|---|
| Bandai blocks GitHub (403), shows a captcha, or the page comes back empty | ⚠️ after 30 min, with the likely cause and what to do; a reminder every 24 h; ✅ when it recovers |
| A page is removed from Bandai's site | 🗑️ short note; it stops being watched (a ⚠️ only if a page that's still linked breaks) |
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
