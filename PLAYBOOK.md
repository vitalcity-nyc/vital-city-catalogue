# Network explorer — maintenance playbook

The explorer at **https://vitalcity-nyc.github.io/vital-city-catalogue/network/**
is generated from three lists. Here's how the team keeps it current.

Passphrase to open the page: shared separately (stored locally in
`private/.netpass`; never in the repo). Rotate any time — see bottom.

## The three source lists

| List | Who maintains it | Where it lives |
|---|---|---|
| **Contacts** (names + categories: journalist, academic, funder, gov, judge, etc.) | The team, collaboratively | A shared **Google Sheet** |
| **Members / subscribers** | Comes from Ghost | Export from Ghost admin |
| **Donors** | Comes from FCNY | Export from FCNY |

Everything is fused and de-duplicated by email, then name, into one record per
person. A person can be several things at once (e.g. academic + member + donor).

## Day to day: editing contacts (anyone on the team)

Edit the shared **Google Sheet** — add people, fix details, check the category
columns. That's it. Keep the columns as they are:

`name, email, institution, role, VC contributor, VC advisor, journalist,
academic, foundation leadership, nonprofit leadership, city gov, state gov,
fed gov, judge, architect, criminal justice, housing, transit`

For a category, put anything in the cell (a `1`, an `x`, `yes`) to tag the
person; leave it blank if not. One row per person.

## Publishing updates to the live page

Editing the Sheet does **not** change the live page by itself — someone
publishes when ready (a minute, start to finish):

1. **Contacts:** in the Google Sheet → *File ▸ Download ▸ Comma-separated values*.
   Save it as `private/contacts_source.csv` in this folder.
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
