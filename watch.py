"""Merge competitor feeds into one topic-filtered watch list, driven by watch.yaml.

Writes docs/watch.json (read by the5krunner's hourly "Just the Facts" task) and
docs/watch.xml (the same list as RSS, for Inoreader).

Each story keeps the time this script first saw it, so sites that only publish a
date (Wareable) still get a usable "how new is this" time.
"""
import json
import re
import sys
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


def read_rss(src):
    if src["type"] == "local":
        parsed = feedparser.parse((ROOT / src["url"]).read_bytes())
    else:
        r = requests.get(src["url"], headers=HEADERS, timeout=30)
        r.raise_for_status()
        parsed = feedparser.parse(r.content)
    if not parsed.entries:
        raise ValueError(f"no items (bozo: {parsed.get('bozo_exception', 'none')})")
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
    url = src["url"].rstrip("/") + "/wp-json/wp/v2/posts"
    r = requests.get(url, headers=HEADERS, timeout=30,
                     params={"per_page": 30, "_fields": "date_gmt,link,title,excerpt"})
    r.raise_for_status()
    for p in r.json():
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

    stories, failures = {}, 0
    for src in cfg["sources"]:
        try:
            reader = read_wp if src["type"] == "wp" else read_rss
            kept = total = 0
            for it in reader(src):
                total += 1
                if not it["link"] or not it["title"]:
                    continue
                text = f"{it['title']} {it['excerpt']}"
                matched = [k for k, rx in topics.items() if rx.search(text)]
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
        except Exception as e:  # one broken source shouldn't stop the rest
            failures += 1
            print(f"FAIL {src['name']}: {e}", file=sys.stderr)

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
    print(f"     watch list: {len(ordered)} stories from the last {cfg.get('keep_days', 7)} days")
    sys.exit(1 if failures == len(cfg["sources"]) else 0)


if __name__ == "__main__":
    main()
