# Vital City content catalogue

A searchable catalogue of everything published on
[vitalcitynyc.org](https://www.vitalcitynyc.org/), organized by author,
headline, topic, publish date and the issue/series each piece appeared in.

## What's here

```
scrape.py        Pulls the full catalogue from the Ghost Content API
index.html       Interactive browsable catalogue (search + filters + sortable table)
methodology.md   Where every number and field comes from — read this
data/
  catalogue.json   Full structured records, one per article
  catalogue.csv    Flat spreadsheet view (open in Excel / Google Sheets)
  authors.json     Per-contributor rollup (counts, bio, socials)
  issues.json      Per-issue/series rollup (date range, counts, top topics)
  tags.json        Topic rollup with counts
  meta.json        Run metadata + what was new on the last run
```

## Viewing the catalogue

The page reads the JSON files over http, so serve the folder rather than
double-clicking the file:

```
cd vital-city-catalogue
python3 -m http.server 8860
# then open http://localhost:8860
```

## Refreshing the data

Re-run the scraper any time to pull newly published articles:

```
python3 scrape.py
```

It records what changed since the previous run in `data/meta.json`
(`new_article_count` and a `new_articles` list), and prints the new pieces.

## Current totals

812 articles · 445 contributors · 28 issues & series · ~200 topics ·
spanning 2021–2026.

See `methodology.md` for sources, field definitions, tag classification and
limitations.
