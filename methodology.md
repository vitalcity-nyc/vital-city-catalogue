# Vital City content catalogue — methodology

This document explains exactly where the catalogue data comes from, how every
field is derived, what is included and excluded, and the known limitations.
Nothing here is a black box.

## Source

All data is pulled from the **Ghost Content API** that powers
vitalcitynyc.org. Vital City runs on Ghost, and Ghost exposes a read-only
Content API for its own on-site search feature.

- API base: `https://vital-city.ghost.io/ghost/api/content/`
- Endpoint used: `/posts/` with `include=authors,tags`
- Key: a public, read-only Content API key that the site itself embeds in its
  front-end search widget (`data-key` on the page). It grants read access to
  already-published content only. It cannot edit, delete or read drafts.

We page through every post (50 per request, ordered by publish date) until the
API reports no further pages. No scraping of rendered HTML pages is involved —
we read the same structured data Ghost uses internally.

## What counts as "published content"

- **Included:** every Ghost *post* (article) returned by the Content API. The
  Content API only returns published, public-visible posts — drafts, scheduled
  and members-only content are not exposed by this key.
- **Excluded:** Ghost *pages* (static pages like About, Masthead, Submissions).
  These are site furniture, not editorial articles. They can be added later if
  wanted by also querying the `/pages/` endpoint.

As of the latest run this is **812 articles** spanning **2021-09-15 to
2026-05-20**.

## Field definitions

Per article (`data/catalogue.json`):

| Field | Source / derivation |
|---|---|
| `title` | Ghost `title` |
| `slug` | Ghost `slug` |
| `url` | `https://www.vitalcitynyc.org/<slug>/` |
| `published_date` | Date portion of Ghost `published_at` (UTC) |
| `published_at` / `updated_at` | Ghost timestamps, verbatim |
| `primary_author` | Ghost `primary_author.name` (the lead byline) |
| `authors` | All bylined authors, in Ghost order |
| `topics` | Public-facing tags (see tag classification below) |
| `issues` | Internal series/issue tags (see below), with the `#` stripped |
| `issue_numbers` | Integer pulled from any `#issue-N` tag |
| `excerpt` | Ghost `custom_excerpt` if set, otherwise Ghost's auto-generated `excerpt` |
| `feature_image` | Ghost `feature_image` URL |
| `featured` | Ghost `featured` flag (editor-promoted) |
| `visibility` | Ghost `visibility` (public/members/paid) |
| `word_count` | Count of whitespace-separated tokens in the stripped article HTML |
| `reading_minutes` | `word_count / 230`, rounded, floor of 1 (230 wpm is a standard reading-speed assumption) |

## Tag classification — how topics and issues are separated

Ghost stores two kinds of tags. We split them by Ghost's own `visibility` flag:

- **Public tags → `topics`.** These are subject tags shown to readers (Crime,
  Housing, History, Gun Violence, etc.). ~200 distinct topics.
- **Internal tags (name begins with `#`) → `issues`.** Vital City uses internal
  tags to group articles into themed **issues and series**, e.g. `#issue-14`,
  `#congestion-pricing`, `#whither-new-york`, `#rubber-meets-road`,
  `#data-stories`. We strip the leading `#` for display. Numbered issues get a
  friendly `display_name` ("Issue 14"); named series are title-cased.

A single article can belong to more than one issue/series and to many topics.

### Tags deliberately dropped as junk

Two internal tags are migration/system artifacts, not real classifications, and
are excluded everywhere:

- `#ImagesUploaded` (slug `hash-imagesuploaded`) — auto-applied during a media import
- `#Import 2026-02-26 13:34` (slug prefix `hash-import-`) — a one-time content import marker
- `#none` (slug `hash-none`) — a stray empty tag on 4 posts

## Rollups

- `data/authors.json` — one entry per contributor: post count, the article
  slugs, plus bio/socials/profile URL pulled from Ghost's author records.
- `data/issues.json` — one entry per issue/series: post count, first/last
  publish date, top 5 co-occurring topics, issue number and display name.
- `data/tags.json` — every public topic with its post count.
- `data/meta.json` — run timestamp and totals.

## Known limitations

1. **Publish-date quirks.** A few issues span a wide date range (e.g. the
   current/rolling issue) because individual articles were published or
   re-dated over time. Dates are whatever Ghost records as `published_at`.
2. **Author name as identity.** Authors are keyed by display name. If the same
   person is entered under two spellings in Ghost, they would appear as two
   contributors. No de-duplication or identity matching is applied.
3. **Word counts are approximate.** They are computed from the article HTML
   with tags stripped; embedded charts, images, pull-quotes and captions are
   not counted as prose, and code/HTML cards are excluded from the text.
4. **Public content only.** Drafts, scheduled posts and any members-only
   content are not visible through this API key and are therefore not catalogued.
5. **Snapshot in time.** The catalogue reflects the moment `scrape.py` last ran.
   See README for the refresh schedule.
