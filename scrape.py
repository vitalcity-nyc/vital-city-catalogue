#!/usr/bin/env python3
"""Pull the full Vital City catalogue from the Ghost Content API.

Outputs (into ./data):
  - catalogue.json   full structured records, one per published post
  - catalogue.csv    flat spreadsheet view (one row per post)
  - authors.json     per-author rollup (post counts, bio, socials)
  - issues.json      per-issue rollup (date range, post count, sections)
  - tags.json        topic-tag rollup with post counts
  - meta.json        run metadata (timestamp, totals)

No third-party dependencies: standard library only.
"""

import csv
import json
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

API_BASE = "https://vital-city.ghost.io/ghost/api/content"
# Public, read-only key exposed on the site for its own search feature.
API_KEY = "dd8e178e9ddfc883537e71dd07"
SITE = "https://www.vitalcitynyc.org"

DATA_DIR = Path(__file__).resolve().parent / "data"

# Internal Ghost tags that are migration artifacts, not real classifications.
JUNK_TAG_SLUGS = {"hash-imagesuploaded", "hash-none"}
JUNK_TAG_PREFIXES = ("hash-import-",)


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "vital-city-catalogue/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def fetch_all_posts():
    """Page through every published post with authors + tags included."""
    posts = []
    page = 1
    while True:
        url = (
            f"{API_BASE}/posts/?key={API_KEY}"
            f"&include=authors,tags&limit=50&page={page}"
            f"&order=published_at%20desc"
        )
        try:
            data = fetch_json(url)
        except urllib.error.HTTPError as e:
            print(f"  HTTP {e.code} on page {page}: {e.reason}", file=sys.stderr)
            raise
        batch = data.get("posts", [])
        posts.extend(batch)
        pagination = data.get("meta", {}).get("pagination", {})
        total = pagination.get("total")
        next_page = pagination.get("next")
        print(f"  page {page}: {len(batch)} posts (running total {len(posts)}/{total})")
        if not next_page:
            break
        page = next_page
        time.sleep(0.3)  # be polite to the API
    return posts


TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def html_to_text(html):
    if not html:
        return ""
    text = TAG_RE.sub(" ", html)
    text = unescape(text)
    return WS_RE.sub(" ", text).strip()


def word_count(html):
    return len(html_to_text(html).split())


def classify_tags(tags):
    """Split a post's tags into topics, issues, and dropped junk.

    Issues are internal (#-prefixed) tags such as #issue-14 or named series
    like #rubber-meets-road. Topics are the public-facing subject tags.
    """
    topics, issues = [], []
    for t in tags:
        slug = t.get("slug", "")
        if slug in JUNK_TAG_SLUGS or slug.startswith(JUNK_TAG_PREFIXES):
            continue
        if t.get("visibility") == "internal":
            issues.append(t)
        else:
            topics.append(t)
    return topics, issues


ISSUE_NUM_RE = re.compile(r"^#issue-(\d+)$")


def issue_number(name):
    m = ISSUE_NUM_RE.match(name.strip())
    return int(m.group(1)) if m else None


SENT_END_RE = re.compile(r"(?<=[.!?])\s")


def one_line_summary(p):
    """A single-line statement of the article's main idea.

    Prefers the editorial custom excerpt (Ghost `custom_excerpt`). Where none
    exists, falls back to the first sentence of Ghost's auto excerpt, trimmed.
    Returns None when neither is available.
    """
    ce = (p.get("custom_excerpt") or "").strip()
    if ce:
        return ce
    ex = html_to_text(p.get("excerpt") or "")
    if not ex:
        return None
    first = SENT_END_RE.split(ex, 1)[0].strip()
    if len(first) > 160:
        first = first[:157].rsplit(" ", 1)[0] + "…"
    return first or None


def normalize_post(p):
    topics, issues = classify_tags(p.get("tags", []))
    authors = [a.get("name") for a in p.get("authors", []) if a.get("name")]
    primary = (p.get("primary_author") or {}).get("name")
    pub = p.get("published_at")
    pub_date = pub.split("T")[0] if pub else None

    issue_names = [i["name"].lstrip("#") for i in issues]
    numbered = [issue_number(i["name"]) for i in issues]
    numbered = [n for n in numbered if n is not None]

    return {
        "title": p.get("title"),
        "slug": p.get("slug"),
        "url": f"{SITE}/{p.get('slug')}/",
        "published_date": pub_date,
        "published_at": pub,
        "updated_at": p.get("updated_at"),
        "primary_author": primary,
        "authors": authors,
        "topics": [t["name"] for t in topics],
        "issues": issue_names,
        "issue_numbers": numbered,
        "summary": one_line_summary(p),
        "excerpt": p.get("custom_excerpt") or (p.get("excerpt") or "").strip() or None,
        "feature_image": p.get("feature_image"),
        "featured": p.get("featured", False),
        "visibility": p.get("visibility"),
        "word_count": word_count(p.get("html")),
        "reading_minutes": max(1, round(word_count(p.get("html")) / 230)) if p.get("html") else None,
        "id": p.get("id"),
    }


def build_rollups(records, raw_posts):
    # Authors
    authors = {}
    raw_by_id = {p["id"]: p for p in raw_posts}
    for r in records:
        for a in r["authors"]:
            authors.setdefault(a, {"name": a, "post_count": 0, "slugs": []})
            authors[a]["post_count"] += 1
            authors[a]["slugs"].append(r["slug"])
    # enrich author bios/socials from raw author objects
    bio_by_name = {}
    for p in raw_posts:
        for a in p.get("authors", []):
            if a.get("name") and a["name"] not in bio_by_name:
                bio_by_name[a["name"]] = {
                    "slug": a.get("slug"),
                    "bio": a.get("bio"),
                    "url": a.get("url"),
                    "twitter": a.get("twitter"),
                    "website": a.get("website"),
                }
    for name, info in authors.items():
        info.update(bio_by_name.get(name, {}))
        info["slugs"] = sorted(set(info["slugs"]))

    # Issues
    issues = {}
    for r in records:
        for name in r["issues"]:
            issues.setdefault(name, {"name": name, "post_count": 0, "dates": [], "topics": {}})
            issues[name]["post_count"] += 1
            if r["published_date"]:
                issues[name]["dates"].append(r["published_date"])
            for t in r["topics"]:
                issues[name]["topics"][t] = issues[name]["topics"].get(t, 0) + 1
    for name, info in issues.items():
        dates = sorted(d for d in info["dates"] if d)
        info["first_published"] = dates[0] if dates else None
        info["last_published"] = dates[-1] if dates else None
        info["number"] = issue_number("#" + name)
        if info["number"] is not None:
            info["display_name"] = f"Issue {info['number']}"
        else:
            info["display_name"] = name.replace("-", " ").title()
        info["top_topics"] = sorted(info["topics"].items(), key=lambda x: -x[1])[:5]
        del info["dates"], info["topics"]

    # Topics
    topics = {}
    for r in records:
        for t in r["topics"]:
            topics[t] = topics.get(t, 0) + 1
    topics = [{"name": k, "post_count": v} for k, v in sorted(topics.items(), key=lambda x: -x[1])]

    return authors, issues, topics


def write_csv(records, path):
    cols = [
        "published_date", "title", "summary", "primary_author", "authors", "topics",
        "issues", "issue_numbers", "word_count", "reading_minutes",
        "featured", "visibility", "url",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in records:
            w.writerow([
                r["published_date"], r["title"], r["summary"] or "", r["primary_author"],
                "; ".join(r["authors"]), "; ".join(r["topics"]),
                "; ".join(r["issues"]), "; ".join(str(n) for n in r["issue_numbers"]),
                r["word_count"], r["reading_minutes"], r["featured"],
                r["visibility"], r["url"],
            ])


def load_previous_slugs():
    path = DATA_DIR / "catalogue.json"
    if not path.exists():
        return None  # first ever run
    try:
        return {r["slug"] for r in json.loads(path.read_text())}
    except Exception:
        return None


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    prev_slugs = load_previous_slugs()
    print("Fetching all posts from Ghost Content API...")
    raw = fetch_all_posts()
    print(f"Fetched {len(raw)} posts. Normalizing...")
    records = [normalize_post(p) for p in raw]
    records.sort(key=lambda r: r["published_at"] or "", reverse=True)

    # What changed since the last run (None on the very first run).
    new_articles = []
    if prev_slugs is not None:
        new_articles = [
            {"title": r["title"], "url": r["url"], "published_date": r["published_date"],
             "primary_author": r["primary_author"]}
            for r in records if r["slug"] not in prev_slugs
        ]

    authors, issues, topics = build_rollups(records, raw)

    (DATA_DIR / "catalogue.json").write_text(json.dumps(records, indent=2, ensure_ascii=False))
    write_csv(records, DATA_DIR / "catalogue.csv")
    (DATA_DIR / "authors.json").write_text(json.dumps(
        sorted(authors.values(), key=lambda a: -a["post_count"]), indent=2, ensure_ascii=False))
    (DATA_DIR / "issues.json").write_text(json.dumps(
        sorted(issues.values(), key=lambda i: (i["number"] is None, -(i["number"] or 0))),
        indent=2, ensure_ascii=False))
    (DATA_DIR / "tags.json").write_text(json.dumps(topics, indent=2, ensure_ascii=False))

    meta = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "site": SITE,
        "total_posts": len(records),
        "total_authors": len(authors),
        "total_issues": len(issues),
        "total_topics": len(topics),
        "date_range": [
            min((r["published_date"] for r in records if r["published_date"]), default=None),
            max((r["published_date"] for r in records if r["published_date"]), default=None),
        ],
        "first_run": prev_slugs is None,
        "new_article_count": len(new_articles),
        "new_articles": new_articles,
    }
    (DATA_DIR / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False))

    print("\nDone. Summary:")
    print(json.dumps({k: v for k, v in meta.items() if k != "new_articles"}, indent=2))
    if new_articles:
        print(f"\n{len(new_articles)} new article(s) since last run:")
        for a in new_articles:
            print(f"  - {a['published_date']}  {a['title']}  ({a['primary_author']})")


if __name__ == "__main__":
    main()
