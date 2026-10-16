# rss-feeds

Makes RSS feeds (headline + excerpt) for sites that don't publish one. Runs free on GitHub every 10 minutes, and pings Inoreader the moment a feed changes, so breaking news shows up quickly.

## One-off setup (about 10 minutes)

1. On GitHub, create a new **public** repository called `rss-feeds`. Public is needed for free GitHub Pages.
2. Upload these files, keeping the folder structure:
   - `sites.yaml`
   - `scrape.py`
   - `.github/workflows/feeds.yml`
   - `README.md`
   Upload `sites.yaml`, `scrape.py` and `README.md` with "Add file > Upload files". Macs hide the `.github` folder, so for the workflow choose "Add file > Create new file", type the name `.github/workflows/feeds.yml` and paste in the contents of `feeds.yml`.
3. Go to **Actions**, open "Build RSS feeds" and press **Run workflow**. After a minute a `docs` folder appears containing `wareable.xml`.
4. Go to **Settings > Pages**. Under "Build and deployment" choose **Deploy from a branch**, branch `main`, folder `/docs`, and save.
5. A minute later your feed is live at:
   `https://YOUR-GITHUB-NAME.github.io/rss-feeds/wareable.xml`
6. Add that address to Inoreader as a new subscription.

From then on it checks every 10 minutes on its own. When there are new stories it saves the feed and pings Inoreader through a public WebSub hub, so Inoreader fetches them straight away instead of waiting for its own hourly check. GitHub can delay scheduled runs when it is busy, so expect the odd gap of 15 to 30 minutes.

## What each story shows

Each story in Inoreader shows the featured image, the site's excerpt in bold and the article's first paragraph. The script opens each new article once to collect these and remembers them in `cache/articles.json`, so later checks don't reload the same pages.

## Adding another site

Edit `sites.yaml` on GitHub (pencil icon) and add an entry:

```yaml
  - slug: new-site
    title: New Site - News
    url: https://www.newsite.com/news
    headline: h2
```

To merge several pages (categories, tags) into one feed, use `urls:` with a list instead of `url:`, as the Wareable entry does. Adding or removing a category is just adding or deleting a line.

Saving triggers a run. The new feed appears at `.../rss-feeds/new-site.xml`.

To find the `headline` value, open the site's news page in Chrome, right-click a headline and choose **Inspect**. If the link sits inside an `<h2>`, use `h2`; inside an `<h3>`, use `h3`. If the page uses the same heading for other things, add the class, for example `h2.entry-title`.

If the excerpt comes out wrong, use precise mode instead (see the notes at the top of `sites.yaml`).

## The 60-day pause

GitHub switches off scheduled workflows in a public repository after 60 days without activity. Its documentation doesn't say whether the robot's own feed commits count, so assume they don't. GitHub emails you before it happens. To restart it, open **Actions**, pick "Build RSS feeds" and press **Enable workflow**. Editing `sites.yaml` also counts as activity.

## When something breaks

Sites redesign. If a feed stops updating, open **Actions** and look at the latest run. A line starting `FAIL` names the site and the reason. Usually the fix is a new `headline` selector. Some sites block GitHub's servers outright, and those can't be fetched this way.

## Competitor watch list

`watch.py` runs after `scrape.py`. It reads every source in `watch.yaml` (the feeds built here plus competitors' own RSS feeds or WordPress REST APIs), keeps only stories on watches, bike computers, sports earphones, other wearables and sports software, and drops reviews, buyer's guides and deals. The result is published as:

- `docs/watch.json`, read by the5krunner's hourly "Just the Facts" task
- `docs/watch.xml`, the same list as one RSS feed for Inoreader

Each story records `first_seen`, the time this repo first saw it, so a site that prints only the date still gets a usable time. Stories stay in the list for `keep_days` (7 by default), even after they roll off a busy site's own feed.

To add a competitor, add one line under `sources:` in `watch.yaml`. To widen or narrow what counts as on-topic, edit the `topics` and `exclude` patterns. A source that fails shows as `FAIL` in the Actions log and the rest carry on.
