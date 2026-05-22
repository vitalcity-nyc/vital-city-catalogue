# Contact master search — how the logic works

This is the technical reference for the network/contact tool: where data comes
from, how people are matched and categorized, and how to feed in new or
different source files later. (For the team-facing version see `PLAYBOOK.md`;
for the in-app feature guide see `network/about.html`.)

The whole thing is **file-driven and source-agnostic** by design. Nothing is
hard-wired to a particular export — if you can produce a CSV (from Ghost, the
Ghost Admin API, Mailchimp, a fresh donor pull, a new contact sheet, anything),
it drops into `private/` and rebuilds. You can always repopulate later.

---

## Files

| File | Role |
|---|---|
| `build_network.py` | Fuses the sources into one deconflicted dataset → `private/people.json` |
| `encrypt_people.py` | AES-256-GCM encrypts `people.json` → `network/data.enc` (the only thing published) |
| `publish.sh` | One command: build → encrypt → push (auto-deploys the page) |
| `scrape.py` | Pulls the article catalogue (Ghost Content API) → `data/catalogue.json`, `data/authors.json` |
| `refresh.sh` | Weekly catalogue refresh (scrape + push); the article side |
| `network/index.html` | The password-gated search tool |
| `network/about.html` | In-app help |
| `private/` (gitignored) | All raw sources + plaintext + passphrase — never published |

---

## Sources (all in `private/`, all gitignored)

| What | File | Key columns the build reads |
|---|---|---|
| **Subscribers** | `members_source.csv` | `email`, `name`, `created_at` |
| **Contacts** | `contacts_source.csv` (Google Sheet export) or `contacts_source.xlsx` (sheet `combined`) | `name`, `email`, `institution`, `role`, `categories`, `specialties` (or one boolean column per category) |
| **Donors** | `donors_source.csv` (FCNY export) | `First Name`, `Last Name`, `Email`, `Summed Donation Amount`, `Donations Count`, `Last Donation at` |
| **Authors** | `data/authors.json` + `data/catalogue.json` (Ghost Content API via `scrape.py`) | name, post count, each article's authors + topics |
| **Author roster** | `vc_authors.csv` (Google Contacts export — authoritative contributors) | First/Middle/Last, E-mail 1/2/3 (first non-@vitalcitynyc.org wins) |
| **Donors** | `donors_source.csv` (FCNY) | First/Last, Email, Summed Donation Amount, Donations Count, Last Donation at |
| **Unsubscribed** | `unsubscribed_source.csv` (Mailchimp export) | Email, First/Last Name → flags people `unsub` (former contacts; excluded by default, shown red) |
| **Extra contributors** | `extra_contributors.csv` (optional) | `email` → force VC-contributor tag (nickname stragglers) |
| **In-tool edits** | `people_overrides.json` (optional, exported from the tool's ✎) | `{personKey: {n, inst, emails, types, topics}}`, applied last |
| **Ghost members (live)** | `ghost_members.py` (read-only Admin API `/members/`; key in `.ghost_admin_key`) | refreshes the subscriber CSV; never reads `/users/` (fake author emails) |

Key data-model notes: each person holds an **emails list** (multiple addresses; matching dedups on any), an `unsub` flag, and `ns` (name source: given/guess). Made-up `@vitalcitynyc.org` author emails are always scrubbed. Institution is inferred from the email domain where blank. The final build step **merges exact-full-name duplicates** (single first-names never merge).

A consolidated spreadsheet mirroring the whole tool lives in Drive: **Vital City — Network (master)** (`17v3wa1OMW5XXIcu0oN6tp4-NfOje7xPTVUv5Zb3XVVE`).

The current contacts Google Sheet: **vital-city-contacts-master** —
`https://docs.google.com/spreadsheets/d/1GXNFKKspPgXK_ubUB2XptrNQfXqmHeHocyN2c_6O6Q8/edit`

---

## Deconfliction (one row per person)

People are merged across all sources, in this order, keyed by **email first,
then normalized name, then first+last name**:

1. **Subscribers** (the spine) → sets the `Subscriber` badge + join year (`since`).
2. **Contacts** → institution, role, and category/specialty tags.
3. **Authors** → `author` flag, article count, and inferred domains.
4. **Donors** → total giving, gift count, most-recent gift date.
5. **Email-domain inference** (journalist / current nyc.gov — see below).
6. **Manual name fixes** applied last.

Email is the reliable key; name matching is the fallback (so it inherits the
usual namesake/spelling caveats). A person can be several things at once.

---

## Names

1. **Clean** — strip leading/trailing junk (`*`, stray punctuation/symbols), keep letters, digits, periods, parens, hyphens, apostrophes.
2. **Capitalize** — names entered all-lowercase or ALL-CAPS are title-cased (`deirdre hamill` → Deirdre Hamill). Intentional mixed case is preserved (VanNostrand, McDonnell, DeFabbia-Kane).
3. **Source** — each name is `given` (authoritative: from a subscriber/contact/donor/author record or a manual fix) or `guess` (extrapolated from the email). Guesses render **gray italic**; a `given` name always upgrades a guess.
4. **Email extrapolation** (`name_from_email`): `jane.doe@x.com` → Jane Doe; `aaron@naparstek.com` → Aaron Naparstek (personal-domain surname, only for non-webmail domains); single-token webmail → just the handle.
5. **Manual fixes** (`name_overrides.csv`, `email,name`): force a `given` name. The in-app ✎ (edit) and ✓ (confirm a guess) buttons collect these in the browser; "Export name fixes" downloads this file to bake them in permanently.

Sorting the Name column is by **last name** (suffixes like Jr./III and non-alphabetic tokens are skipped).

---

## Categories

**Types** (`PERSON_CATS`): VC contributor, VC advisor, journalist, academic,
foundation leadership, nonprofit leadership, city gov, **current nyc.gov**,
state gov, fed gov, judge, architect.

- From the **contacts sheet** (`categories` column or boolean columns).
- **journalist** is *also* inferred from the email domain — if it's a known
  news outlet (`MEDIA_DOMAINS` in `build_network.py`: nytimes.com, wnyc.org,
  thecity.nyc, politico.com, …), tag journalist. Add outlets to that set.
- **current nyc.gov** is inferred from any email ending in `nyc.gov` (agencies,
  city hall, DAs). It is intentionally separate from `city gov` (the
  hand-maintained list, which can include former officials) so you can find who
  is *currently* inside city government after an administration change.

**Specialty domains** (`TOPIC_CATS`): criminal justice, housing, transit,
budget, urban planning, education, public health, economy, technology,
politics & government, race & equity, culture.

- From the **contacts sheet** (`specialties` column).
- **Also inferred from what someone has written for us**: each article's public
  topic tags are mapped to a domain via `TOPIC_MAP`, and every author of that
  piece gets those domains. (e.g., wrote a Housing-tagged piece → `housing`;
  Budget → `budget`.) Extend `TOPIC_MAP` to map more topic tags.

---

## New authors are added automatically

When a new piece is published, `scrape.py` picks up the new article + author,
and the next `build_network` run adds that author as a person: tagged
`author`, with the article count and the **domains they wrote about**, and their
**email filled in if they already appear in any source list** (contacts,
subscribers or donors — matched by name).

If a brand-new author isn't in any list, their email is blank. To fill it,
either add them to the contacts sheet, or have Claude look them up in Gmail /
Google Contacts and append to `name_overrides`-style data. (A headless cron
can't do a Gmail lookup; a Claude-run publish can.)

---

## Publishing / refreshing

```
# new articles? refresh the catalogue first:
python3 scrape.py
# rebuild the network from whatever's in private/, encrypt, push:
bash publish.sh
```

`publish.sh` = `build_network.py` → `encrypt_people.py` → commit & push
`network/data.enc`. The live page updates in ~1 minute. Source files and the
plaintext `people.json` never leave the machine.

**Encryption:** AES-256-GCM, key via PBKDF2-SHA256 (600k iterations). The
passphrase lives only in `private/.netpass` (gitignored); only the ciphertext
(`network/data.enc`) is published. The page decrypts in-browser and remembers
the passphrase per device (clearable via "Lock / forget password").

---

## Feeding in new / different data later

The build only cares about the **filenames in `private/` and a few column
names** — not where the data came from. To repopulate or switch sources:

- **New subscriber list** (Ghost export, **Ghost Admin API**, **Mailchimp**, etc.):
  produce a CSV at `private/members_source.csv` with `email`, `name`, and a
  join-date column. If the column names differ (e.g., Mailchimp uses
  `Email Address`, `OPTIN_TIME`), tell Claude and the small reader in
  `build_network.py` (the "Members" section) gets a one-line column remap.
- **New contacts**: re-export the Google Sheet to `private/contacts_source.csv`
  (or point Claude at a different Sheet/file).
- **New donor pull**: drop it at `private/donors_source.csv`.
- Then run `bash publish.sh`.

**Adding a brand-new source type** (e.g., event attendees, a board list): it's a
~15-line addition to `build_network.py` mirroring the donor block — read the
file, `get_or_make` by email/name, set a flag/field. Ask Claude.

Because everything rebuilds from these files, you can always start over or move
to a better pipeline (a live Ghost/Mailchimp API pull instead of CSV exports)
without losing the architecture.
