"""Turn news listing pages into RSS feeds (title + excerpt), driven by sites.yaml."""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urljoin
from xml.sax.saxutils import escape

import requests
import yaml
from bs4 import BeautifulSoup, Tag

OUT = Path(__file__).parent / "docs"
CACHE = Path(__file__).parent / "cache" / "articles.json"  # image + first paragraph per story
MAX_ITEMS = 50          # stories kept in each feed
MAX_NEW_ARTICLES = 40   # article pages opened per run; any extra are picked up next run
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
    "Accept-Language": "en-GB,en;q=0.9",
}
# Public hub that pushes "new stories" pings to Inoreader (WebSub).
HUB = "https://pubsubhubbub.appspot.com/"


def feed_base():
    """Public address of the feeds, worked out from the GitHub repository name."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" not in repo:
        return ""
    owner, name = repo.split("/", 1)
    host = f"https://{owner.lower()}.github.io/"
    return host if name.lower() == f"{owner.lower()}.github.io" else f"{host}{name}/"


DATE_RE = re.compile(
    r"\b(?:\d{1,2}\s+)?(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*"
    r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}\b|\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|"
    r"Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+\d{4}\b|\b\d{4}-\d{2}-\d{2}\b"
)


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def parse_date(raw, fmt):
    raw = clean(raw)
    if not raw:
        return None
    try:  # ISO dates from article metadata, e.g. 2026-10-21T09:00:00+00:00
        d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    raw = re.sub(r"(\d)(st|nd|rd|th)", r"\1", raw)
    for f in [fmt, "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y", "%Y-%m-%d"]:
        if not f:
            continue
        try:
            return datetime.strptime(raw, f).replace(hour=12, tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def simple_mode(soup, site):
    """Headline-driven: the excerpt is the longest text between this headline and the next."""
    items, sel = [], site["headline"]
    heads = soup.select(sel)
    head_ids = {id(h) for h in heads}
    for head in heads:
        # The link can sit inside the headline (<h3><a>) or wrap it (<a><h3>).
        a = head if head.name == "a" else (head.find("a", href=True) or head.find_parent("a", href=True))
        if not a or not a.get("href"):
            continue
        title = clean(a.get_text())
        texts, date_raw = [], None
        for el in head.next_elements:
            if isinstance(el, Tag) and id(el) in head_ids:
                break  # reached the next story
            if isinstance(el, str):
                t = clean(el)
                if not t or t == title:
                    continue
                if not date_raw and (m := DATE_RE.search(t)):
                    date_raw = m.group(0)
                    continue
                texts.append(t)
        excerpt = max((t for t in texts if len(t) > 40), key=len, default="")
        items.append((title, urljoin(site["url"], a["href"]), excerpt, date_raw))
    return items


def precise_mode(soup, site):
    items = []
    for block in soup.select(site["item"]):
        head = block.select_one(site["item_title"])
        if not head:
            continue
        if site.get("item_link"):  # link kept apart from the headline, e.g. "Read more"
            a = block.select_one(site["item_link"])
        else:
            a = head if head.name == "a" else (head.find("a", href=True) or head.find_parent("a", href=True))
        if not a or not a.get("href"):
            continue
        ex = block.select_one(site["excerpt"]) if site.get("excerpt") else None
        dt = block.select_one(site["date"]) if site.get("date") else None
        date_raw = (dt.get("datetime") or dt.get_text()) if dt else None
        link = urljoin(site["url"], a["href"])
        if site.get("strip_query"):  # drop tracking codes so each story has one address
            link = link.split("?")[0].split("#")[0]
        items.append((clean(head.get_text()), link,
                      clean(ex.get_text()) if ex else "", date_raw))
    return items


def meta(soup, name):
    tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
    return clean(tag.get("content")) if tag and tag.get("content") else ""


def article_details(url, site, excerpt):
    """Open one article and take its featured image and first real paragraph."""
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    image = meta(soup, "og:image") or meta(soup, "twitter:image")
    root = (soup.select_one(site["article_body"]) if site.get("article_body") else None) \
        or soup.find("article") or soup.find("main") or soup
    para = ""
    for p in root.find_all("p"):
        if p.find_parent(["figure", "figcaption", "aside", "nav", "header", "footer", "form"]):
            continue
        text = clean(p.get_text())
        if len(text) >= 80 and text != excerpt:  # skip captions, bylines and the standfirst
            para = text
            break
    published = meta(soup, "article:published_time") or meta(soup, "og:updated_time")
    if not published and (t := soup.find("time", attrs={"datetime": True})):
        published = t["datetime"]
    return {"image": urljoin(url, image) if image else "", "para": para, "published": published}


def attr(text):
    return escape(text, {'"': "&quot;"})


def build_feed(site, items):
    # No build time in the feed, so the file only changes when the stories do.
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom" '
        'xmlns:media="http://search.yahoo.com/mrss/"><channel>',
        f"<title>{escape(site['title'])}</title>",
        f"<link>{escape(site['url'])}</link>",
        f"<description>{escape(site['title'])} (unofficial feed)</description>",
    ]
    if base := feed_base():
        self_url = f"{base}{site['slug']}.xml"
        out.append(f'<atom:link rel="self" href="{attr(self_url)}" type="application/rss+xml"/>')
        out.append(f'<atom:link rel="hub" href="{HUB}"/>')
    for it in items:
        out.append("<item>")
        out.append(f"<title>{escape(it['title'])}</title>")
        out.append(f"<link>{escape(it['link'])}</link>")
        out.append(f'<guid isPermaLink="true">{escape(it["link"])}</guid>')
        body = ""
        if it.get("image"):
            body += f'<p><img src="{attr(it["image"])}" alt=""></p>'
        if it.get("excerpt"):
            body += f"<p><strong>{escape(it['excerpt'])}</strong></p>"
        if it.get("para"):
            body += f"<p>{escape(it['para'])}</p>"
        if body:
            out.append(f"<description>{escape(body)}</description>")
        if it.get("image"):
            out.append(f'<media:content url="{attr(it["image"])}" medium="image"/>')
        if d := parse_date(it["date"], site.get("date_format")):
            out.append(f"<pubDate>{format_datetime(d)}</pubDate>")
        out.append("</item>")
    out.append("</channel></rss>")
    return "\n".join(out)


def main():
    config = yaml.safe_load((Path(__file__).parent / "sites.yaml").read_text())
    OUT.mkdir(exist_ok=True)
    try:
        cache = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    in_use, fetched = set(), 0
    failures = 0
    for site in config["sites"]:
        try:
            pages = site.get("urls") or [site["url"]]
            site.setdefault("url", pages[0])
            items = []
            for page in dict.fromkeys(pages):  # each page once, even if listed twice
                try:
                    r = requests.get(page, headers=HEADERS, timeout=30)
                    r.raise_for_status()
                    soup = BeautifulSoup(r.text, "html.parser")
                    page_site = {**site, "url": page}
                    found = (precise_mode(soup, page_site) if site.get("item")
                             else simple_mode(soup, page_site))
                    items += found
                    print(f"     {page}: {len(found)} stories")
                except Exception as e:  # one bad page shouldn't sink the whole feed
                    print(f"WARN {page}: {e}", file=sys.stderr)
            seen, unique = set(), []
            for title, link, excerpt, date_raw in items:  # drop duplicates across categories
                if link not in seen:
                    seen.add(link)
                    unique.append({"title": title, "link": link, "excerpt": excerpt,
                                   "date": date_raw})
            if not unique:
                raise ValueError("no stories found - check the selectors in sites.yaml")
            epoch = datetime.min.replace(tzinfo=timezone.utc)
            unique.sort(key=lambda it: parse_date(it["date"], site.get("date_format")) or epoch,
                        reverse=True)  # newest first across all pages
            unique = unique[:site.get("max_items", MAX_ITEMS)]
            for it in unique:  # featured image + first paragraph, each article opened once
                if site.get("fetch_articles", True) and it["link"] not in cache \
                        and fetched < MAX_NEW_ARTICLES:
                    fetched += 1
                    try:
                        cache[it["link"]] = article_details(it["link"], site, it["excerpt"])
                    except Exception as e:
                        print(f"WARN article {it['link']}: {e}", file=sys.stderr)
                    time.sleep(1)  # be polite to the site
                it.update(cache.get(it["link"], {}))
                if not it["date"] and it.get("published"):  # listing shows no date
                    it["date"] = it["published"]
                in_use.add(it["link"])
            unique.sort(key=lambda it: parse_date(it["date"], site.get("date_format")) or epoch,
                        reverse=True)
            (OUT / f"{site['slug']}.xml").write_text(build_feed(site, unique), encoding="utf-8")
            print(f"OK   {site['slug']}: {len(unique)} stories")
        except Exception as e:  # keep going so one broken site doesn't stop the rest
            failures += 1
            print(f"FAIL {site['slug']}: {e}", file=sys.stderr)
    # Keep the cache to stories still in a feed, so it never grows without limit.
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps({k: cache[k] for k in sorted(in_use) if k in cache},
                                indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"     opened {fetched} new article pages")
    # A simple index page so GitHub Pages has an entry point, plus .nojekyll so
    # Pages serves the files as they are instead of running them through Jekyll.
    (OUT / ".nojekyll").write_text("")
    links = "\n".join(
        f'<li><a href="{escape(s["slug"])}.xml">{escape(s["title"])}</a></li>'
        for s in config["sites"] if (OUT / f"{s['slug']}.xml").exists())
    (OUT / "index.html").write_text(
        f"<!doctype html><meta charset=utf-8><title>Feeds</title><ul>\n{links}\n</ul>\n",
        encoding="utf-8")
    sys.exit(1 if failures == len(config["sites"]) else 0)


if __name__ == "__main__":
    main()
