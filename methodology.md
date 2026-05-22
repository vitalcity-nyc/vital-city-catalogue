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

## Content type classification

Each article is assigned exactly one **type**, plus a `type_basis` field that
records *why* it got that type (so nothing is a black box and you can audit or
reclassify any call). The classifier is rule-based and runs most-specific-first;
the first rule that matches wins:

| Order | Type | Matched when… | `type_basis` |
|---|---|---|---|
| 1 | **book review** | tagged "Book Review", or title says "a review of" / "reviewed" | `tag:book-review` / `title:review` |
| 2 | **q&a** | tagged Podcast, interview, Conversations, or "In Conversation With…"; or the title reads like an interview ("in conversation", "a conversation with", "talks to/with", "Q&A") | `tag:<name>` / `title:conversation` |
| 3 | **map/tool** | title contains "interactive", "explorer", "tracker", "dashboard", "calculator", "quiz", "interactive map"; **or** the article embeds one of Vital City's own hosted apps (`vitalcity-nyc.github.io` iframe); **or** the HTML loads a mapping/viz library (Leaflet, Mapbox, MapLibre, D3, Vega-Lite, deck.gl) | `title:tool-or-map` / `html:vc-app-embed` / `html:map-or-viz-library` |
| 4 | **data analysis** | tagged "Data Stories" / in the `#data-stories` series; title like "by the numbers" / "in N charts" / "mapped"; **or** the piece embeds **3 or more** charts (Flourish / Datawrapper) | `tag:data-stories` / `html:N-chart-embeds` |
| 5 | **something else** | framing pages — title like "About This Project", "Editor's Note", "Masthead", "A Note From…" | `title:framing-page` |
| 6 | **opinion/commentary** | everything else (the default — Vital City is fundamentally an essays/commentary journal) | `default` |

As of the latest run: opinion/commentary 693, q&a 59, data analysis 44,
map/tool 10, book review 5, something else 1.

**Deliberate design choices and their limits:**
- "tool" and bare "map" are **not** matched in titles because they are usually
  metaphorical ("the unlikely *tool* that could transform hiring"). Real tools
  are caught by "interactive", an embedded Vital City app, or a JS map library.
- Mapping/viz libraries are matched by their actual script/CDN references (e.g.
  `leaflet.js`, `api.mapbox.com`, `/d3@`, `vega-lite`), **not** loose words, so
  prose like "Las **Vega**s" or "road**map**" does not trigger a false positive.
- The 3-chart threshold for "data analysis" keeps opinion essays that merely
  include a chart or two in "opinion/commentary"; only genuinely chart-driven
  pieces flip to "data analysis".
- The classifier favors precision over recall on the smaller categories. A piece
  that is mis-typed can be inspected via `type_basis` and the rules adjusted in
  `scrape.py` (`classify_type`).

## Rollups

- `data/authors.json` — one entry per contributor: post count, the article
  slugs, plus bio/socials/profile URL pulled from Ghost's author records.
- `data/issues.json` — one entry per issue/series: post count, first/last
  publish date, top 5 co-occurring topics, issue number and display name.
- `data/tags.json` — every public topic with its post count.
- `data/types.json` — each content type with its post count.
- `data/meta.json` — run timestamp and totals.

## Contact CRM cross-analysis (local-only)

A separate, **private** layer lets us cross-analyze authors by contact type. It
is built by `build_contacts.py` from Vital City's contact agglomeration
spreadsheet and is **never published** — the source `.xlsx` and the entire
`private/` output folder are gitignored, so none of it reaches the public site.

- Source: `private/contacts_source.xlsx`, sheet `combined` (~1,250 contacts).
- Person-type categories used: VC contributor, VC advisor, journalist, academic,
  foundation leadership, nonprofit leadership, city gov, state gov, fed gov,
  judge, architect. (The sheet's criminal-justice / housing / transit columns are
  beat tags, kept as `topics`.)
- Authors are matched to contacts by **name** (exact normalized, then first+last),
  the same method used elsewhere — so it inherits the same name-matching caveats
  (namesakes, spelling variants). 245 of 443 authors (55%) matched.
- Outputs (all gitignored): `private/contacts.json`, `private/author_categories.json`
  (the file the catalogue UI reads), `private/cross_analysis.json` (summary counts).

In the catalogue UI, when this local layer is present the page shows an "author
type" filter and contact-category tags beside each author. On the public
GitHub Pages site the file is absent, so those features simply do not appear —
the public catalogue contains only published-content data, never the CRM.

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
