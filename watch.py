"""Merge competitor feeds into one topic-filtered watch list, driven by watch.yaml.

Writes docs/watch.json (read by the5krunner's hourly "Just the Facts" task) and
docs/watch.xml (the same list as RSS, for Inoreader).

Each story keeps the time this script first saw it, so sites that only publish a
date (Wareable) still get a usable "how new is this" time.
"""
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html import unescape
from pathlib import Path
from xml.sax.saxutils import escape

import feedparser
import requests
import yaml

ROOT = Path(__file__).parent
OUT_JSON = ROOT / "docs" / "watch.json"
OUT_XML = ROOT / "docs" / "watch.xml"
OUT_STATUS = ROOT / "docs" / "watch-status.json"  # per-source result of the last run
GRACE_HOURS = 3  # a source counts as failing only after this long without a good read
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept-Language": "en-GB,en;q=0.9",
}


def clean(html):
    text = re.sub(r"<[^>]+>", " ", html or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def entry_time(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def fetch(url, params=None, tries=2):
    """GET with one retry, for sites whose server times out or errors now and then."""
    for attempt in range(tries):
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=20)
            r.raise_for_status()
            return r.content
        except requests.RequestException:
            if attempt == tries - 1:
                raise
            time.sleep(5)


def snippet(body):
    return re.sub(r"\s+", " ", body[:120].decode("utf-8", "replace"))


def parse_feed(body):
    """Parse RSS/Atom, dropping any junk (PHP notices, blank lines) printed before the XML."""
    start = re.search(rb"<\?xml|<rss|<feed", body)
    parsed = feedparser.parse(body[start.start():] if start else body)
    if not parsed.entries:
        raise ValueError(f"no items (bozo: {parsed.get('bozo_exception', 'none')}; "
                         f"starts: {snippet(body)!r})")
    return parsed


def parse_json_list(body):
    """Parse a JSON list, dropping any junk printed before it."""
    start = body.find(b"[")
    if start < 0:
        raise ValueError(f"not JSON (starts: {snippet(body)!r})")
    try:
        data = json.loads(body[start:])
    except ValueError as e:
        raise ValueError(f"{e} (starts: {snippet(body)!r})") from None
    if not isinstance(data, list) or not data:
        raise ValueError(f"no posts (starts: {snippet(body)!r})")
    return data


def read_rss(src, url=None):
    if src["type"] == "local":
        parsed = feedparser.parse((ROOT / src["url"]).read_bytes())
        if not parsed.entries:
            raise ValueError(f"no items (bozo: {parsed.get('bozo_exception', 'none')})")
    else:
        parsed = parse_feed(fetch(url or src["url"]))
    for e in parsed.entries:
        published = entry_time(e)
        # The local Wareable feed only knows the date; scrape.py stamps it at 12:00.
        date_only = None
        if src["type"] == "local" and published and published.hour == 12 and published.minute == 0:
            date_only, published = published, None
        yield {
            "title": clean(e.get("title")),
            "link": e.get("link", ""),
            "published": published,
            "date_only": date_only,
            "excerpt": clean(e.get("summary"))[:400],
        }


def read_wp(src):
    """WordPress REST API, falling back to the site's /feed/ if the API fails."""
    root = src["url"].rstrip("/")
    try:
        posts = parse_json_list(fetch(root + "/wp-json/wp/v2/posts",
                                      {"per_page": 30, "_fields": "date_gmt,link,title,excerpt"}))
    except Exception as api_error:
        print(f"     {src['name']}: REST API failed ({api_error}), trying /feed/", file=sys.stderr)
        try:
            items = list(read_rss(src, root + "/feed/"))
        except Exception as feed_error:
            raise ValueError(f"REST API: {api_error} | /feed/: {feed_error}") from None
        yield from items
        return
    for p in posts:
        yield {
            "title": clean(p["title"]["rendered"]),
            "link": p["link"],
            "published": datetime.fromisoformat(p["date_gmt"]).replace(tzinfo=timezone.utc),
            "excerpt": clean(p.get("excerpt", {}).get("rendered"))[:400],
        }


def story_time(s):
    """Best known time for a stored story: publish time, else its date, capped at first seen."""
    if s.get("published"):
        t = s["published"]
    elif s.get("date"):
        t = min(s["first_seen"], s["date"] + "T23:59:59Z")
    else:
        t = s["first_seen"]
    return datetime.fromisoformat(t.replace("Z", "+00:00"))


def compile_patterns(cfg):
    def rx(s):
        return re.compile(r"\b(?:" + re.sub(r"\s+", "", s) + r")\b", re.I)
    return {k: rx(v) for k, v in cfg["topics"].items()}, rx(cfg["exclude"])


def main():
    cfg = yaml.safe_load((ROOT / "watch.yaml").read_text())
    topics, exclude = compile_patterns(cfg)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=cfg.get("keep_days", 7))

    try:
        previous = {s["link"]: s for s in json.loads(OUT_JSON.read_text())["stories"]}
    except (OSError, ValueError, KeyError):
        previous = {}
    try:
        old_status = json.loads(OUT_STATUS.read_text())
    except (OSError, ValueError):
        old_status = {}
    hour = now.strftime("%Y-%m-%dT%H:00Z")  # hour precision, so the file doesn't change every run

    stories, failures, status = {}, 0, {}
    for src in cfg["sources"]:
        try:
            reader = read_wp if src["type"] == "wp" else read_rss
            kept = total = 0
            for it in reader(src):
                total += 1
                if not it["link"] or not it["title"]:
                    continue
                text = f"{it['title']} {it['excerpt']}"
                allowed = src.get("topics") or list(topics)  # a source can be limited to some topics
                matched = [k for k in allowed if topics[k].search(text)]
                if exclude.search(it["title"]):
                    continue
                if src.get("filter", True) and not matched:
                    continue
                old = previous.get(it["link"], {})
                first_seen = old.get("first_seen") or iso(now)
                when = it["published"] or it.get("date_only") \
                    or datetime.fromisoformat(first_seen.replace("Z", "+00:00"))
                if when < cutoff:
                    continue
                stories[it["link"]] = {
                    "source": src["name"],
                    "source_home": src["home"],
                    "title": it["title"],
                    "link": it["link"],
                    "published": iso(it["published"]),
                    "date": (it["published"] or it.get("date_only") or now).strftime("%Y-%m-%d"),
                    "first_seen": first_seen,
                    "topics": matched,
                    "excerpt": it["excerpt"],
                }
                kept += 1
            print(f"OK   {src['name']}: kept {kept} of {total}")
            status[src["name"]] = {"ok": True, "items": total, "kept": kept, "last_ok": hour}
        except Exception as e:  # one broken source shouldn't stop the rest
            failures += 1
            print(f"FAIL {src['name']}: {e}", file=sys.stderr)
            # Sites behind a bot check (Garmin Rumors' SiteGround captcha) block some GitHub
            # runners and not others, so a single failure loses nothing while the source was
            # read recently. Only report it as failing after GRACE_HOURS without a good read.
            last_ok = old_status.get(src["name"], {}).get("last_ok")
            recent = last_ok and now - datetime.fromisoformat(last_ok.replace("Z", "+00:00")) \
                < timedelta(hours=GRACE_HOURS)
            status[src["name"]] = {"ok": bool(recent), "last_ok": last_ok,
                                   "error": str(e)[:200]}

    # Keep stories that have rolled off a busy feed (or whose source failed this run)
    # until they age out, so the hourly task never misses one between runs.
    for link, s in previous.items():
        if link not in stories and story_time(s) >= cutoff:
            stories[link] = s

    # Newest first, by publish time where the site gives one, else when first seen.
    ordered = sorted(stories.values(), key=story_time, reverse=True)
    # No build time in the file, so it only changes (and commits) when the stories do.
    OUT_JSON.write_text(json.dumps({"stories": ordered}, indent=1, ensure_ascii=False) + "\n",
                        encoding="utf-8")

    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>',
           "<title>the5krunner competitor watch</title>",
           "<link>https://the5krunner.com/</link>",
           "<description>Competitor stories on watches, bike computers, sports earphones, "
           "wearables and sports software</description>",
           '<atom:link rel="hub" href="https://pubsubhubbub.appspot.com/"/>']
    for s in ordered[:100]:
        when = story_time(s)
        out += ["<item>",
                f"<title>{escape(s['source'])}: {escape(s['title'])}</title>",
                f"<link>{escape(s['link'])}</link>",
                f'<guid isPermaLink="true">{escape(s["link"])}</guid>',
                f"<description>{escape(s['excerpt'])}</description>",
                f"<category>{escape(', '.join(s['topics']))}</category>",
                f"<pubDate>{format_datetime(when)}</pubDate>",
                "</item>"]
    out.append("</channel></rss>")
    OUT_XML.write_text("\n".join(out), encoding="utf-8")
    OUT_STATUS.write_text(json.dumps(status, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"     watch list: {len(ordered)} stories from the last {cfg.get('keep_days', 7)} days")
    sys.exit(1 if failures == len(cfg["sources"]) else 0)


if __name__ == "__main__":
    main()
