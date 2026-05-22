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
  types.json       Content-type rollup with counts
  meta.json        Run metadata + what was new on the last run
```

Each article is also classified by **type** — opinion/commentary, data analysis,
map/tool, q&a, book review or something else — with a `type_basis` field
recording why. See `methodology.md` for the rules.

## Viewing the catalogue

The page reads the JSON files over http, so serve the folder rather than
double-clicking the file:

```
cd vital-city-catalogue
python3 -m http.server 8860
# then open http://localhost:8860
```

## Refreshing the data

The catalogue refreshes itself **weekly** (Mondays 8am) via a local launchd job
(`com.vitalcity.catalogue-refresh`, defined in
`com.vitalcity.catalogue-refresh.plist`). The job runs `refresh.sh`, which
re-scrapes and pushes any changes so the live site stays current. No
notifications are sent.

To refresh manually at any time:

```
python3 scrape.py        # update local data only
# or
bash refresh.sh          # update + commit + push to the live site
```

Each run records what changed since the previous one in `data/meta.json`
(`new_article_count` and a `new_articles` list).

## Network explorer (password-protected, separate page)

`network/index.html` is a confidential people explorer — every person in Vital
City's orbit (members/subscribers, contributors, contacts) fused and
deconflicted across the members export, the contact CRM and the author list,
categorized by type and organized around membership status.

It is **client-side encrypted** (AES-256-GCM): only `network/data.enc`
(ciphertext) is published, and the page decrypts it in the browser when the
shared passphrase is entered. Live at `…/vital-city-catalogue/network/`.

Rebuild + re-encrypt (keeps your chosen passphrase out of git):

```
python3 build_network.py                                  # private/people.json (gitignored)
VC_NETWORK_PASS='your-passphrase' python3 encrypt_people.py   # writes network/data.enc
git add network/data.enc && git commit -m "refresh network" && git push
```

Source data (`private/`, `*.xlsx`, the members CSV) and the plaintext
`people.json` are gitignored and never published. The passphrase is shared
out-of-band; rotate it by re-encrypting with a new value.

## Current totals

812 articles · 445 contributors · 28 issues & series · ~200 topics ·
spanning 2021–2026.

See `methodology.md` for sources, field definitions, tag classification and
limitations.
