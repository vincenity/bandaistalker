#!/usr/bin/env python3
"""Watch Bandai's One Piece Card Game event pages and alert on changes.

Everything is reported to Discord (and/or ntfy), including problems:
  * page changes (new events, new regionals, new registration links)
  * Bandai blocking or failing to serve a page (after it lasts FAIL_ALERT_MINUTES),
    with a reminder every REMIND_HOURS while it continues, and an "all clear"
  * the GitHub 60-day auto-disable: the script keeps the workflow alive daily via
    GitHub's API and tells you if that ever stops working
  * a weekly check-in, so if you stop hearing from it you know it's down

Uses only the Python standard library, so there is nothing to install.

Environment variables (set as GitHub Actions secrets):
  DISCORD_WEBHOOK_URL  Discord channel webhook URL
  DISCORD_MENTION      e.g. "<@123456789012345678>" to ping yourself (optional)
  NTFY_TOPIC           ntfy.sh topic name for phone push (optional)
Provided automatically by GitHub Actions:
  GITHUB_TOKEN, GITHUB_REPOSITORY, GITHUB_SERVER_URL, GITHUB_RUN_ID
"""
import datetime as dt
import difflib
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

# ---- Pages to watch. Add more lines here any time. ---------------------------
PAGES = {
    "Events page": "https://en.onepiece-cardgame.com/events/",
    "Regionals Season 2 (26-27)": "https://en.onepiece-cardgame.com/events/regional-season2-26-27.html",
}
# Links matching this are flagged as "NEW EVENT PAGE" when they first appear
# (catches e.g. a Regionals Season 3 page being added to the events index).
EVENT_LINK_RE = re.compile(r"onepiece-cardgame\.com/events/.+", re.I)
# Text lines matching this are ignored (cookie banners, copyright year, etc.).
IGNORE_RE = re.compile(r"^(©|copyright)", re.I)

FAIL_ALERT_MINUTES = 30    # stay quiet about blips shorter than this
REMIND_HOURS = 24          # repeat a still-failing alert this often
HEARTBEAT_DAYS = 7         # weekly "still alive" check-in
KEEPALIVE_HOURS = 24       # how often to reset GitHub's 60-day inactivity timer
KEEPALIVE_WARN_DAYS = 7    # warn if keepalive hasn't worked for this long
WORKFLOW_FILE = "watch.yml"
# -----------------------------------------------------------------------------

SNAP_DIR = Path(__file__).parent / "snapshots"
STATE_FILE = SNAP_DIR / "state.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
BLOCK_TAGS = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5",
              "h6", "br", "section", "article", "dt", "dd", "table", "ul", "ol"}
CELL_TAGS = {"td", "th"}  # table cells are joined with " | " on one line
SKIP_TAGS = {"script", "style", "noscript", "svg", "template"}
NOTIFY_FAILED = False


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.isoformat() if t else None


def parse_t(s):
    return dt.datetime.fromisoformat(s) if s else None


def ago(t):
    secs = int((now() - t).total_seconds())
    if secs < 3600:
        return f"{secs // 60} min"
    if secs < 172800:
        return f"{secs // 3600} h"
    return f"{secs // 86400} days"


def run_url():
    srv, repo, rid = (os.environ.get(k) for k in
                      ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{srv}/{repo}/actions/runs/{rid}" if srv and repo and rid else None


def actions_url():
    srv, repo = os.environ.get("GITHUB_SERVER_URL"), os.environ.get("GITHUB_REPOSITORY")
    return f"{srv}/{repo}/actions" if srv and repo else "your repo's Actions tab"


# ---- Page parsing ----------------------------------------------------------
class PageParser(HTMLParser):
    """Pull visible text (one line per block element) and all links."""

    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.lines, self.links = [], []
        self._buf, self._skip = [], 0
        self._href, self._atext = None, []

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if text and not IGNORE_RE.search(text):
            self.lines.append(text)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag in BLOCK_TAGS:
            self._flush()
        elif tag in CELL_TAGS and not self._skip and "".join(self._buf).strip():
            self._buf.append(" | ")
        if tag == "a":
            href = dict(attrs).get("href")
            if href and not href.startswith(("javascript:", "mailto:", "#")):
                self._href = urllib.parse.urljoin(self.base, href).split("#")[0]
                self._atext = []

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in BLOCK_TAGS:
            self._flush()
        if tag == "a" and self._href:
            label = re.sub(r"\s+", " ", "".join(self._atext)).strip()
            self.links.append(f"{label} -> {self._href}" if label else self._href)
            self._href = None

    def handle_data(self, data):
        if self._skip:
            return
        self._buf.append(data)
        if self._href:
            self._atext.append(data)

    def close(self):
        super().close()
        self._flush()


class PageProblem(Exception):
    """A fetch/parse failure with a human-friendly explanation and fix."""

    def __init__(self, short, advice):
        super().__init__(short)
        self.short, self.advice = short, advice


def fetch(url, tries=3):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept-Language": "en-US,en"})
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode(r.headers.get_content_charset() or "utf-8",
                                       errors="replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 404:
                break  # retrying won't help
        except Exception as e:  # noqa: BLE001
            last = e
        if attempt < tries - 1:
            print(f"  retry {attempt + 1} after error: {last}")
            time.sleep(5 * (attempt + 1))
    raise classify(last)


def classify(err):
    if isinstance(err, urllib.error.HTTPError):
        if err.code in (401, 403, 429):
            return PageProblem(
                f"Bandai's site refused access (HTTP {err.code})",
                "This usually means Bandai's bot protection is blocking GitHub's servers. "
                "If it doesn't clear up within a day, the watcher can't see the site from "
                "GitHub. Run `monitor.py` from your own computer, or switch to a hosted "
                "watcher such as Distill.io or Visualping.")
        if err.code == 404:
            return PageProblem(
                "Page not found (HTTP 404)",
                "Bandai probably moved or renamed this page (a season page may have been "
                "replaced). Find the new URL on the events page and update `PAGES` in "
                "`monitor.py`.")
        if err.code >= 500:
            return PageProblem(
                f"Bandai's server error (HTTP {err.code})",
                "Their site is having problems. You don't need to do anything. You'll get "
                "an all-clear when it's back.")
        return PageProblem(f"Unexpected HTTP {err.code}",
                           "Open the page in a browser to see what's going on.")
    if isinstance(err, (socket.timeout, TimeoutError)) or "timed out" in str(err):
        return PageProblem("Bandai's site timed out",
                           "The site may be down or very slow. Nothing to do yet.")
    return PageProblem(f"Couldn't reach Bandai's site ({err})",
                       "The site or network may be down. Nothing to do yet.")


def extract(html, url):
    p = PageParser(url)
    p.feed(html)
    p.close()
    data = {"lines": list(dict.fromkeys(p.lines)), "links": sorted(set(p.links))}
    if len(data["lines"]) < 5:
        low = html.lower()
        hint = ("It looks like a bot-check/captcha page."
                if any(w in low for w in ("captcha", "challenge", "access denied",
                                          "cloudflare", "akamai", "verify you are"))
                else "Bandai may now be loading the page with JavaScript, or serving "
                     "a placeholder.")
        raise PageProblem("Page loaded but was almost empty",
                          hint + " Open it in a browser. If it looks normal there, "
                          "the watcher needs updating to read the new format.")
    return data


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


# ---- Change reporting ------------------------------------------------------
def diff(old, new):
    added, removed = [], []
    for op in difflib.ndiff(old, new):
        if op.startswith("+ "):
            added.append(op[2:])
        elif op.startswith("- "):
            removed.append(op[2:])
    return added, removed


def build_message(name, url, old, new):
    add_l, rem_l = diff(old["lines"], new["lines"])
    old_links, new_links = set(old["links"]), set(new["links"])
    add_k = sorted(new_links - old_links)
    rem_k = sorted(old_links - new_links)
    if not (add_l or rem_l or add_k or rem_k):
        return None

    parts = [f"**{name} changed**\n<{url}>"]
    if old["lines"] and len(rem_l) > 0.6 * len(old["lines"]):
        parts.append("⚠️ Most of the page changed at once, so Bandai may have redesigned it. "
                     "If the next alerts look like noise, the watcher may need an update.")
    event_pages = [k for k in add_k if EVENT_LINK_RE.search(k.split(" -> ")[-1])]
    if event_pages:
        parts.append("🚨 **NEW EVENT PAGE(S):**\n" +
                     "\n".join(f"• {k}" for k in event_pages[:10]) +
                     "\n_Add new season pages to `PAGES` in `monitor.py` to watch them too._")
    other_links = [k for k in add_k if k not in event_pages]
    if other_links:
        parts.append("🔗 **New links** (often registration):\n" +
                     "\n".join(f"• {k}" for k in other_links[:10]))
    if add_l:
        parts.append("➕ **Added:**\n" + "\n".join(f"• {l}" for l in add_l[:15]) +
                     (f"\n…and {len(add_l) - 15} more" if len(add_l) > 15 else ""))
    if rem_l:
        parts.append("➖ **Removed:**\n" + "\n".join(f"• {l}" for l in rem_l[:8]) +
                     (f"\n…and {len(rem_l) - 8} more" if len(rem_l) > 8 else ""))
    if rem_k and not rem_l:
        parts.append(f"(also {len(rem_k)} link(s) removed)")
    return "\n\n".join(parts)


# ---- Notifications ---------------------------------------------------------
def post(url, data, headers):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def notify(title, body, click_url=None, ping=True):
    """Send to Discord and ntfy. Returns True if at least one succeeded."""
    sent = False
    hook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if hook:
        mention = os.environ.get("DISCORD_MENTION", "").strip() if ping else ""
        content = (f"{mention}\n" if mention else "") + body
        if len(content) > 1990:
            content = content[:1950] + "\n…(truncated, open the page)"
        payload = {"content": content, "username": "OPTCG Event Watch",
                   "allowed_mentions": {"parse": ["users", "roles", "everyone"]}}
        try:
            post(hook, json.dumps(payload).encode(),
                 {"Content-Type": "application/json", "User-Agent": UA})
            sent = True
        except Exception as e:  # noqa: BLE001
            print(f"Discord send failed: {e}  (webhook deleted? re-create it and "
                  "update the DISCORD_WEBHOOK_URL secret)")
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        plain = re.sub(r"\*\*|<|>|`|_", "", body)[:3800]
        headers = {"Title": title.encode("ascii", "ignore").decode(),
                   "Priority": "high" if ping else "low", "Tags": "rotating_light"}
        if click_url:
            headers["Click"] = click_url
        try:
            post(f"https://ntfy.sh/{topic}", plain.encode("utf-8"), headers)
            sent = True
        except Exception as e:  # noqa: BLE001
            print(f"ntfy send failed: {e}")
    if not sent:
        global NOTIFY_FAILED
        NOTIFY_FAILED = True
        print("No notifier configured/succeeded. Message was:\n" + body)
    return sent


# ---- Health checks ---------------------------------------------------------
def handle_failure(name, url, ps, problem):
    t = now()
    if not ps.get("fail_since"):
        ps["fail_since"] = iso(t)
    ps["last_error"] = problem.short
    since = parse_t(ps["fail_since"])
    last_alert = parse_t(ps.get("last_fail_alert"))
    long_enough = t - since >= dt.timedelta(minutes=FAIL_ALERT_MINUTES)
    due = last_alert is None or t - last_alert >= dt.timedelta(hours=REMIND_HOURS)
    if long_enough and due:
        prefix = "Still failing" if last_alert else "Can't check"
        msg = (f"⚠️ **{prefix}: {name}** (for {ago(since)})\n<{url}>\n"
               f"**Problem:** {problem.short}\n**What to do:** {problem.advice}\n"
               f"_While this lasts, you won't get change alerts for this page. "
               f"Next reminder in {REMIND_HOURS} h if it's still broken._")
        if run_url():
            msg += f"\nRun log: <{run_url()}>"
        if notify(f"OPTCG watcher problem: {name}", msg, url):
            ps["last_fail_alert"] = iso(t)


def handle_recovery(name, url, ps):
    if ps.get("last_fail_alert"):
        notify(f"OPTCG watcher OK: {name}",
               f"✅ **Back to normal: {name}**. The checks are working again "
               f"(it was down for {ago(parse_t(ps['fail_since']))}).\n<{url}>",
               url, ping=False)
    for k in ("fail_since", "last_fail_alert", "last_error"):
        ps.pop(k, None)


def keepalive(state):
    """Reset GitHub's 60-day inactivity timer by calling the 'enable workflow' API."""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    if not (repo and token):
        return  # running locally
    t = now()
    last_try = parse_t(state.get("keepalive_last_try"))
    if last_try and t - last_try < dt.timedelta(hours=KEEPALIVE_HOURS):
        return
    state["keepalive_last_try"] = iso(t)
    api = f"https://api.github.com/repos/{repo}/actions/workflows/{WORKFLOW_FILE}/enable"
    req = urllib.request.Request(api, method="PUT", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "optcg-watch"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        state["keepalive_ok"] = iso(t)
        state.pop("keepalive_warned", None)
        print("Keepalive OK")
        return
    except Exception as e:  # noqa: BLE001
        err = str(e)
        print(f"Keepalive failed: {err}")
    ok = parse_t(state.get("keepalive_ok")) or parse_t(state.get("created")) or t
    warned = parse_t(state.get("keepalive_warned"))
    if t - ok >= dt.timedelta(days=KEEPALIVE_WARN_DAYS) and (
            warned is None or t - warned >= dt.timedelta(days=KEEPALIVE_WARN_DAYS)):
        deadline = (ok + dt.timedelta(days=60)).strftime("%b %d")
        if notify("OPTCG watcher: action needed",
                  f"⚠️ **The watcher can't keep itself running** (the keepalive has failed for {ago(ok)}: {err}).\n"
                  f"GitHub may pause it around **{deadline}** for inactivity.\n"
                  f"**What to do (pick one):**\n"
                  f"• Check that `watch.yml` has `actions: write` under `permissions:`.\n"
                  f"• Or edit any file in the repo (e.g. README) before {deadline}. That resets the timer.\n"
                  f"• If it's already paused: <{actions_url()}> → **Watch Bandai OPTCG events** → **Enable workflow**."):
            state["keepalive_warned"] = iso(t)


def heartbeat(state, page_states):
    t = now()
    last = parse_t(state.get("last_heartbeat"))
    if last and t - last < dt.timedelta(days=HEARTBEAT_DAYS):
        return
    lines = []
    for name in PAGES:
        ps = page_states.get(slug(name), {})
        if ps.get("fail_since"):
            lines.append(f"• {name}: ⚠️ failing ({ps.get('last_error', '?')})")
        else:
            lc = parse_t(ps.get("last_change"))
            lines.append(f"• {name}: OK, last change {ago(lc) + ' ago' if lc else 'none yet'}")
    ka, ka_try = parse_t(state.get("keepalive_ok")), parse_t(state.get("keepalive_last_try"))
    if ka_try and (not ka or ka_try > ka):
        lines.append(f"• GitHub keepalive: ⚠️ failing (last worked "
                     f"{ago(ka) + ' ago' if ka else 'never'})")
    else:
        lines.append(f"• GitHub keepalive: {'OK, ' + ago(ka) + ' ago' if ka else 'not run yet'}")
    if notify("OPTCG watcher weekly check-in",
              "🫡 **Weekly check-in: still watching.**\n" + "\n".join(lines) +
              f"\n_If you **don't** get this message next week, the watcher has stopped. "
              f"Open <{actions_url()}> to see why._", ping=False):
        state["last_heartbeat"] = iso(t)


# ---- Main ------------------------------------------------------------------
def main():
    SNAP_DIR.mkdir(exist_ok=True)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    first_ever = not state
    state.setdefault("created", iso(now()))
    page_states = state.setdefault("pages", {})

    for name, url in PAGES.items():
        print(f"Checking {name}: {url}")
        key = slug(name)
        ps = page_states.setdefault(key, {})
        snap = SNAP_DIR / f"{key}.json"
        try:
            new = extract(fetch(url), url)
        except PageProblem as p:
            print(f"  PROBLEM: {p.short}")
            handle_failure(name, url, ps, p)
            continue
        handle_recovery(name, url, ps)
        if not snap.exists():
            snap.write_text(json.dumps(new, indent=1, ensure_ascii=False))
            notify(f"Now watching: {name}",
                   f"✅ **Now watching {name}**\n<{url}>\n"
                   f"({len(new['lines'])} text lines, {len(new['links'])} links saved as baseline)",
                   url, ping=False)
            continue
        old = json.loads(snap.read_text())
        msg = build_message(name, url, old, new)
        if msg:
            print("  CHANGED")
            if notify(f"OPTCG: {name} changed", msg, url):
                snap.write_text(json.dumps(new, indent=1, ensure_ascii=False))
                ps["last_change"] = iso(now())
            # If sending failed, keep the old snapshot so the alert retries next run.
        else:
            print("  no change")

    # Forget pages removed from PAGES.
    for k in list(page_states):
        if k not in {slug(n) for n in PAGES}:
            del page_states[k]

    keepalive(state)
    if first_ever:
        state["last_heartbeat"] = iso(now())  # the "Now watching" messages count
    else:
        heartbeat(state, page_states)

    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True))
    if NOTIFY_FAILED:
        # Couldn't deliver an alert: fail the run so the workflow's backup
        # reporter (and, as a last resort, GitHub's email) kicks in.
        sys.exit(1)


if __name__ == "__main__":
    main()
