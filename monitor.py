#!/usr/bin/env python3
"""Watch Bandai's One Piece Card Game event pages and alert on changes.

How it works
  * Starts from SEED_PAGES (the events index, plus any page you always want watched).
  * Follows every link to another event page on Bandai's site, up to MAX_DEPTH
    clicks away (events index -> event page -> sub-page such as a regionals season),
    and watches all of them. Store events, Extra/Grand Battles, Treasure Cups,
    Championships, Regionals, etc. are all covered automatically.
  * When a brand-new event page shows up (e.g. "Regionals Season 3"), you get an
    alert with a preview, and from then on changes to that page are alerted too.
  * Pages that disappear from the site for DROP_AFTER_DAYS stop being watched.

Everything is reported to Discord (and/or ntfy), including problems:
  * Bandai blocking or failing to serve pages (after FAIL_ALERT_MINUTES), with a
    reminder every REMIND_HOURS while it continues, and an all-clear
  * the GitHub 60-day auto-disable: kept alive daily via GitHub's API, and you're
    told if that stops working
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

# ---- Settings ----------------------------------------------------------------
# Pages that are always watched (never dropped). Everything else is discovered.
SEED_PAGES = {
    "https://en.onepiece-cardgame.com/events/": "Events page",
    "https://en.onepiece-cardgame.com/events/regional-season2-26-27.html": "Regionals Season 2 (26-27)",
}
SITE_HOST = "en.onepiece-cardgame.com"
EVENT_PATH_PREFIX = "/events/"   # only pages under this path are followed
# Never follow these (the "Past Events" archive would add dozens of finished events).
EXCLUDE_RE = re.compile(r"list_end", re.I)
MAX_DEPTH = 2          # clicks away from a seed page to follow
MAX_PAGES = 80         # safety cap on how many pages to watch
REQUEST_GAP = 1.0      # seconds between page requests (be polite to Bandai)
DROP_AFTER_DAYS = 3    # stop watching a page after it's unlinked this long

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
SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "title"}
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


# ---- URLs --------------------------------------------------------------------
def normalize(url):
    p = urllib.parse.urlsplit(url)
    path = p.path or "/"
    if path.endswith("/index.html"):
        path = path[:-len("index.html")]
    return urllib.parse.urlunsplit(("https", p.netloc.lower(), path, p.query, ""))


def is_event_page(url):
    p = urllib.parse.urlsplit(url)
    if p.netloc.lower() != SITE_HOST or not p.path.startswith(EVENT_PATH_PREFIX):
        return False
    if p.path == EVENT_PATH_PREFIX or EXCLUDE_RE.search(url):
        return False  # the index itself (it's a seed), or excluded
    return p.path.endswith(("/", ".html", ".htm", ".php"))


TAG_WORDS = ("OFFICIAL EVENTS", "Convention Event", "Regularly Held", "Official Shop Only",
             "Large Scale", "Tournament", "Beginner", "Demo")


def clean_label(label):
    """'Tournament Treasure Cup Dec 2026 Event Period: ...' -> 'Treasure Cup Dec 2026'."""
    name = re.split(r"\bEvent Period\b", label.replace("\u200b", ""))[0].strip()
    changed = True
    while changed:
        changed = False
        for w in TAG_WORDS:
            if name.lower().startswith(w.lower() + " "):
                name, changed = name[len(w):].strip(), True
    return (name or label)[:80]


def snap_key(url):
    p = urllib.parse.urlsplit(url)
    s = re.sub(r"[^a-z0-9]+", "-", (p.path + ("?" + p.query if p.query else "")).lower())
    return s.strip("-") or "index"


# ---- Page parsing ------------------------------------------------------------
class PageParser(HTMLParser):
    """Pull visible text (one line per block element), links, and the title."""

    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.lines, self.links = [], []
        self.link_labels = {}           # absolute URL -> first non-empty label
        self.title, self.h1 = "", ""
        self._buf, self._skip = [], 0
        self._href, self._atext = None, []
        self._in_title = self._in_h1 = False

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if text and not IGNORE_RE.search(text):
            self.lines.append(text)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag == "h1" and not self.h1:
            self._in_h1 = True
        if tag in SKIP_TAGS:
            self._skip += 1
        elif tag in BLOCK_TAGS:
            self._flush()
        elif tag in CELL_TAGS and not self._skip and "".join(self._buf).strip():
            self._buf.append(" | ")
        if tag == "a":
            # Keep side-by-side links from running together ("Season 1Season 2").
            if not self._skip and self._buf and not "".join(self._buf)[-1:].isspace():
                self._buf.append(" · ")
            href = dict(attrs).get("href")
            if href and not href.startswith(("javascript:", "mailto:", "tel:", "#")):
                self._href = urllib.parse.urljoin(self.base, href).split("#")[0]
                self._atext = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "h1":
            self._in_h1 = False
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        elif tag in BLOCK_TAGS:
            self._flush()
        if tag == "a" and self._href:
            label = re.sub(r"\s+", " ", "".join(self._atext)).strip()
            self.links.append(f"{label} -> {self._href}" if label else self._href)
            if label:
                self.link_labels.setdefault(normalize(self._href), label)
            else:
                self.link_labels.setdefault(normalize(self._href), "")
            self._href = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if self._in_h1:
            self.h1 += data
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
                "Bandai removed or renamed this page. Discovered pages are dropped "
                "automatically after a few days. If it's one of the `SEED_PAGES` in "
                "`monitor.py`, remove it or replace it with the new URL.")
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
    event_links = {u: lbl for u, lbl in p.link_labels.items() if is_event_page(u)}
    title = re.sub(r"\s+", " ", p.h1 or p.title).strip()
    return data, event_links, title


# ---- Change reporting --------------------------------------------------------
def diff(old, new):
    added, removed = [], []
    for op in difflib.ndiff(old, new):
        if op.startswith("+ "):
            added.append(op[2:])
        elif op.startswith("- "):
            removed.append(op[2:])
    return added, removed


def build_message(name, url, old, new, newly_watched=()):
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
    event_pages = [k for k in add_k if is_event_page(normalize(k.split(" -> ")[-1]))]
    if event_pages:
        lines = []
        for k in event_pages[:10]:
            tag = (" _(🆕 now watching, preview below)_"
                   if normalize(k.split(" -> ")[-1]) in newly_watched else "")
            lines.append(f"• {k}{tag}")
        parts.append("🚨 **Links to event pages added:**\n" + "\n".join(lines))
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


def new_page_message(pages, boilerplate):
    """pages: list of (name, url, data). Preview skips site-wide menu/footer lines."""
    parts = [f"🆕 **New event page{'s' if len(pages) > 1 else ''} found: now watching**"]
    for name, url, data in pages[:6]:
        body = [l for l in data["lines"] if l not in boilerplate][:8]
        regs = [k for k in data["links"]
                if urllib.parse.urlsplit(k.split(" -> ")[-1]).netloc.lower() != SITE_HOST][:5]
        block = f"**{name}**\n<{url}>"
        if body:
            block += "\n" + "\n".join(f"• {l[:180]}" for l in body)
        if regs:
            block += "\n🔗 " + "\n🔗 ".join(regs)
        parts.append(block)
    if len(pages) > 6:
        parts.append(f"…and {len(pages) - 6} more: " +
                     ", ".join(n for n, _, _ in pages[6:20]))
    return "\n\n".join(parts)


# ---- Notifications -----------------------------------------------------------
def post(url, data, headers):
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def notify(title, body, click_url=None, ping=True):
    """Send to Discord and ntfy. Returns True if at least one succeeded."""
    global NOTIFY_FAILED
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
        NOTIFY_FAILED = True
        print("No notifier configured/succeeded. Message was:\n" + body)
    return sent


# ---- Health checks -----------------------------------------------------------
def report_failures(watch, failures):
    """One combined message for every page whose failure is long enough and due."""
    t = now()
    due = []
    for url, problem in failures:
        w = watch[url]
        w.setdefault("fail_since", iso(t))
        w["last_error"] = problem.short
        since, last_alert = parse_t(w["fail_since"]), parse_t(w.get("last_fail_alert"))
        if (t - since >= dt.timedelta(minutes=FAIL_ALERT_MINUTES) and
                (last_alert is None or t - last_alert >= dt.timedelta(hours=REMIND_HOURS))):
            due.append((url, problem))
    if not due:
        return
    groups = {}
    for url, problem in due:
        groups.setdefault(problem.short, (problem, []))[1].append(url)
    still = any(watch[u].get("last_fail_alert") for u, _ in due)
    total = len(watch)
    parts = [f"⚠️ **{'Still failing' if still else 'Can’t check'}: {len(due)} of {total} watched page(s)**"]
    for short, (problem, urls) in groups.items():
        names = "\n".join(f"• {watch[u]['name']} (for {ago(parse_t(watch[u]['fail_since']))}) <{u}>"
                          for u in urls[:8])
        more = f"\n…and {len(urls) - 8} more" if len(urls) > 8 else ""
        parts.append(f"**Problem:** {short}\n{names}{more}\n**What to do:** {problem.advice}")
    parts.append(f"_While this lasts you won't get change alerts for these pages. "
                 f"Next reminder in {REMIND_HOURS} h if it's still broken._")
    if run_url():
        parts.append(f"Run log: <{run_url()}>")
    if notify("OPTCG watcher problem", "\n\n".join(parts), due[0][0]):
        for url, _ in due:
            watch[url]["last_fail_alert"] = iso(t)


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


def heartbeat(state, watch):
    t = now()
    last = parse_t(state.get("last_heartbeat"))
    if last and t - last < dt.timedelta(days=HEARTBEAT_DAYS):
        return
    failing = [w for w in watch.values() if w.get("fail_since")]
    changed = sorted(((parse_t(w["last_change"]), w["name"]) for w in watch.values()
                      if w.get("last_change")), reverse=True)
    week_new = [w["name"] for w in watch.values()
                if parse_t(w.get("found")) and t - parse_t(w["found"]) < dt.timedelta(days=7)
                and not w.get("seed")]
    lines = [f"• Watching **{len(watch)}** event pages"
             f"{f', {len(failing)} failing' if failing else ', all OK'}"]
    for w in failing[:5]:
        lines.append(f"  ⚠️ {w['name']}: {w.get('last_error', '?')}")
    if changed:
        lines.append(f"• Most recent change: {changed[0][1]}, {ago(changed[0][0])} ago")
    if week_new:
        lines.append(f"• New pages this week: {', '.join(week_new[:8])}")
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


# ---- Main --------------------------------------------------------------------
def main():
    SNAP_DIR.mkdir(exist_ok=True)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    state.setdefault("created", iso(now()))
    state.pop("pages", None)                 # old (v1) format
    bootstrap = "watch" not in state         # first run: baseline quietly
    watch = state.setdefault("watch", {})

    seeds = {normalize(u): n for u, n in SEED_PAGES.items()}
    for url, name in seeds.items():
        w = watch.setdefault(url, {"found": iso(now())})
        w.update(name=name, depth=0, seed=True)
    for url, w in watch.items():             # un-seeded pages stop being seeds
        if url not in seeds and w.pop("seed", None):
            w["depth"] = 1

    queue = sorted(watch, key=lambda u: (watch[u]["depth"], u))
    seen, linked, failures, recovered = set(), set(), [], []
    changes, new_pages, newly_watched = [], [], set()
    page_data, cap_hit = {}, False

    i = 0
    while i < len(queue):
        url = queue[i]
        i += 1
        if url in seen:
            continue
        seen.add(url)
        w = watch[url]
        if len(seen) > 1:
            time.sleep(REQUEST_GAP)
        print(f"Checking [{w['depth']}] {w['name']}: {url}")
        try:
            data, event_links, title = extract(fetch(url), url)
        except PageProblem as p:
            print(f"  PROBLEM: {p.short}")
            failures.append((url, p))
            continue
        page_data[url] = data
        if w.get("last_fail_alert"):
            recovered.append(url)
        for k in ("fail_since", "last_fail_alert", "last_error"):
            w.pop(k, None)
        generic = {"learn more", "more", "details", "read more", "click here", "here", "view more"}
        if title and not w.get("seed") and (not w.get("name") or w["name"].startswith("http")
                                            or w["name"].lower() in generic):
            w["name"] = title.replace("\u200b", "")[:80]

        # Discover linked event pages.
        for link, label in event_links.items():
            linked.add(link)
            if link in watch:
                if watch[link]["depth"] > w["depth"] + 1:
                    watch[link]["depth"] = w["depth"] + 1
                continue
            if w["depth"] + 1 > MAX_DEPTH:
                continue
            if len(watch) >= MAX_PAGES:
                cap_hit = True
                continue
            watch[link] = {"name": clean_label(label) if label else link, "depth": w["depth"] + 1,
                           "found": iso(now()), "new": True}
            newly_watched.add(link)
            queue.append(link)
            print(f"  discovered: {label or link}")

        snap = SNAP_DIR / f"{snap_key(url)}.json"
        if not snap.exists():
            snap.write_text(json.dumps(data, indent=1, ensure_ascii=False))
            if w.pop("new", False) and not bootstrap:
                new_pages.append((w["name"], url, data))
            continue
        w.pop("new", None)
        old = json.loads(snap.read_text())
        changes.append((url, old, data))

    # ---- Send change alerts (index first, then the rest).
    for url, old, data in changes:
        msg = build_message(watch[url]["name"], url, old, data, newly_watched)
        if not msg:
            print(f"  no change: {watch[url]['name']}")
            continue
        print(f"  CHANGED: {watch[url]['name']}")
        if notify(f"OPTCG: {watch[url]['name']} changed", msg, url):
            (SNAP_DIR / f"{snap_key(url)}.json").write_text(
                json.dumps(data, indent=1, ensure_ascii=False))
            watch[url]["last_change"] = iso(now())
        # If sending failed, keep the old snapshot so the alert retries next run.

    if new_pages:
        boiler = set(page_data.get(next(iter(seeds)), {}).get("lines", []))
        if not notify("OPTCG: new event page", new_page_message(new_pages, boiler),
                      new_pages[0][1]):
            for _, url, _ in new_pages:   # retry the announcement next run
                watch[url]["new"] = True
                (SNAP_DIR / f"{snap_key(url)}.json").unlink(missing_ok=True)

    if bootstrap:
        names = sorted(w["name"] for w in watch.values())
        notify("OPTCG watcher started",
               f"✅ **Now watching {len(watch)} event pages** (anything new on these, "
               f"and any new event page linked from them, will be alerted):\n" +
               "\n".join(f"• {n}" for n in names[:40]) +
               (f"\n…and {len(names) - 40} more" if len(names) > 40 else ""),
               ping=False)

    if recovered:
        notify("OPTCG watcher OK",
               "✅ **Back to normal.** Checks are working again for: " +
               ", ".join(watch[u]["name"] for u in recovered[:15]), ping=False)
    removed_now = [u for u, p in failures
                   if p.short.startswith("Page not found") and not watch[u].get("seed")
                   and u not in linked]
    failures = [(u, p) for u, p in failures if u not in removed_now]
    report_failures(watch, failures)

    if cap_hit and not state.get("cap_warned"):
        if notify("OPTCG watcher: page limit",
                  f"ℹ️ Found more than {MAX_PAGES} event pages, so some new ones aren't being watched. "
                  f"Raise `MAX_PAGES` in `monitor.py` if you want them all.", ping=False):
            state["cap_warned"] = iso(now())

    # ---- Drop discovered pages that have been unlinked for a while.
    # Only judge this on runs where every page loaded (otherwise links could be missing).
    t, dropped = now(), []
    for url in removed_now:
        dropped.append(watch.pop(url)["name"])
    if not failures:
        for url in list(watch):
            w = watch[url]
            if w.get("seed") or url in linked:
                w.pop("missing_since", None)
                continue
            w.setdefault("missing_since", iso(t))
            if t - parse_t(w["missing_since"]) >= dt.timedelta(days=DROP_AFTER_DAYS):
                dropped.append(w["name"])
                del watch[url]
    if dropped:
        notify("OPTCG watcher: pages removed",
               "🗑️ No longer listed on Bandai's site, so I stopped watching: " +
               ", ".join(dropped[:20]), ping=False)

    # Clean up snapshot files for pages no longer watched.
    keep = {f"{snap_key(u)}.json" for u in watch} | {STATE_FILE.name}
    for f in SNAP_DIR.glob("*.json"):
        if f.name not in keep:
            f.unlink()

    keepalive(state)
    if bootstrap:
        state["last_heartbeat"] = iso(now())  # the "Now watching" message counts
    else:
        heartbeat(state, watch)

    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False))
    if NOTIFY_FAILED:
        # Couldn't deliver an alert: fail the run so the workflow's backup
        # reporter (and, as a last resort, GitHub's email) kicks in.
        sys.exit(1)


if __name__ == "__main__":
    main()
