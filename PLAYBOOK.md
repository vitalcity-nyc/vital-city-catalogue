# Network explorer — maintenance playbook

> For the full technical logic (every matching/inference rule + how to feed in
> new data sources like a Ghost API or Mailchimp export), see **LOGIC.md**.

The explorer at **https://vitalcity-nyc.github.io/vital-city-catalogue/network/**
is generated from three lists. Here's how the team keeps it current.

Passphrase to open the page: shared separately (stored locally in
`private/.netpass`; never in the repo). Rotate any time — see bottom.

## The three source lists

| List | Who maintains it | Where it lives |
|---|---|---|
| **Contacts** (names + categories: journalist, academic, funder, gov, judge, etc.) | The team, collaboratively | **Google Sheet:** `vital-city-contacts-master` — https://docs.google.com/spreadsheets/d/1GXNFKKspPgXK_ubUB2XptrNQfXqmHeHocyN2c_6O6Q8/edit (owner jgreenman@vitalcitynyc.org) |
| **Members / subscribers** | Comes from Ghost | Export from Ghost admin |
| **Donors** | Comes from FCNY | Export from FCNY |

Everything is fused and de-duplicated by email, then name, into one record per
person. A person can be several things at once (e.g. academic + member + donor).

## Day to day: editing contacts (anyone on the team)

Edit the shared **Google Sheet** — add people, fix details, set their
categories. One row per person. Keep these columns:

`name, email, institution, role, categories, specialties`

- **categories**: a semicolon-separated list from: VC contributor, VC advisor,
  journalist, academic, foundation leadership, nonprofit leadership, city gov,
  state gov, fed gov, judge, architect. e.g. `journalist; academic`
- **specialties**: same idea, from: criminal justice, housing, transit.

(The build also still understands the older one-column-per-category layout, so
either works.)

## Publishing updates to the live page

Editing the Sheet does **not** change the live page by itself — someone
publishes when ready (a minute, start to finish):

1. **Contacts:** either ask Claude to pull the Google Sheet directly (via the
   Drive connector → it writes `private/contacts_source.csv`), or in the Sheet
   do *File ▸ Download ▸ Comma-separated values* and save it as
   `private/contacts_source.csv`.
2. **Members (if you have a fresh export):** save it as
   `private/members_source.csv`.
3. **Donors (if you have a fresh export):** save it as
   `private/donors_source.csv`.
   *(Skip 2 or 3 to keep the last version — only contacts change often.)*
4. Run:
   ```
   bash publish.sh
   ```
   It rebuilds, re-encrypts and pushes. The live page updates in ~1 minute.

## Using the explorer

Open the URL, enter the passphrase. Filter by any combination — type/specialty
(left checkboxes), Members / Non-members, Authors, Donors — and **Export CSV**
to download the exact list (e.g. "academics who are donors but not members").

## Editing people (every field)

Click the **✎** on any row to edit a person's **name, institution, emails,
types and specialties**. Gray italic names are email guesses; **✓** confirms one.

1. Edits save in your browser instantly.
2. To make them permanent for everyone, click **Export edits** (downloads
   `vital-city-edits.json`), save it as `private/people_overrides.json`, and run
   `bash publish.sh`. The build bakes them in for all users.

## Unsubscribed (former contacts)

People who left the newsletter (from the Mailchimp `unsubscribed` export, saved
as `private/unsubscribed_source.csv`) are kept **separate**, shown in **red**,
and hidden by default. Use the **Include unsubscribed** checkbox or the red
**unsubscribed** banner number to see them.

## Consolidated spreadsheet

A full spreadsheet mirroring the tool (everyone, all fields, sortable) lives in
the Workspace Drive as **Vital City — Network (master)**. Regenerate it any time
from the tool's data and re-import; it replaces the old agglomeration sheet as
the reference view.

## Rotating the passphrase

```
VC_NETWORK_PASS='new-words-here' python3 encrypt_people.py
echo -n 'new-words-here' > private/.netpass
git add network/data.enc && git commit -m "rotate" && git push
```
Share the new passphrase with the group. The old one stops working immediately.

## Notes / limits

- This is a static, client-side-encrypted page: great for a shared, filterable,
  exportable view, but it is **not** a live multi-user database. The Google
  Sheet is the place people edit; `publish.sh` is how edits go live.
- Source files (`private/`) and the plaintext people data never leave this
  machine — only the encrypted blob is published.
