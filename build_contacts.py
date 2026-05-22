#!/usr/bin/env python3
"""Build the LOCAL-ONLY contacts layer from the Vital City contact CRM and join
it to the article catalogue so we can cross-analyze authors by contact type.

Reads:  private/contacts_source.xlsx  (sheet "combined")
Writes (all gitignored — this is private CRM data, never published):
  private/contacts.json           every contact with categories + topics
  private/author_categories.json  {author name -> {categories, topics, institution}}
                                   for catalogue authors found in the CRM
  private/cross_analysis.json     summary counts (authors & articles by type)

The public catalogue (index.html, data/*) is unchanged and contains no CRM data.
"""
import json
import re
import unicodedata
from pathlib import Path
import openpyxl

ROOT = Path(__file__).resolve().parent
PRIV = ROOT / "private"
SRC = PRIV / "contacts_source.xlsx"

PERSON_CATS = ["VC contributor", "VC advisor", "journalist", "academic",
               "foundation leadership", "nonprofit leadership", "city gov",
               "state gov", "fed gov", "judge", "architect"]
TOPIC_CATS = ["criminal justice", "housing", "transit"]
NONPERSON = {"vital city", "a survey"}


def norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower().strip()
    s = re.sub(r"\b(dr|mr|mrs|ms|prof|jr|sr|phd|md|esq)\b\.?", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def firstlast(s):
    t = norm(s).split()
    return f"{t[0]} {t[-1]}" if len(t) >= 2 else (t[0] if t else "")


def load_contacts():
    wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    ws = wb["combined"]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [str(c) for c in rows[0]]
    idx = {h: i for i, h in enumerate(hdr)}

    def cell(r, h):
        i = idx.get(h)
        return r[i] if (i is not None and i < len(r)) else None

    contacts = []
    for r in rows[1:]:
        if not r or not r[0]:
            continue
        cats = [c for c in PERSON_CATS if cell(r, c) not in (None, "", 0)]
        tops = [c for c in TOPIC_CATS if cell(r, c) not in (None, "", 0)]
        contacts.append({
            "name": r[0],
            "norm": norm(r[0]),
            "fl": firstlast(r[0]),
            "institution": cell(r, "institution") or "",
            "email": cell(r, "email") or "",
            "role": cell(r, "role") or "",
            "categories": cats,
            "topics": tops,
        })
    return contacts


def main():
    contacts = load_contacts()
    by_full, by_fl = {}, {}
    for c in contacts:
        by_full.setdefault(c["norm"], c)
        by_fl.setdefault(c["fl"], c)

    authors = json.loads((ROOT / "data" / "authors.json").read_text())
    catalogue = json.loads((ROOT / "data" / "catalogue.json").read_text())

    author_cat = {}
    matched = 0
    for a in authors:
        nn = norm(a["name"])
        if not nn or nn in NONPERSON:
            continue
        c = by_full.get(nn) or by_fl.get(firstlast(a["name"]))
        if c:
            matched += 1
            author_cat[a["name"]] = {
                "categories": c["categories"],
                "topics": c["topics"],
                "institution": c["institution"],
            }

    # mark which contacts are catalogue authors
    author_norms = {norm(a["name"]) for a in authors}
    for c in contacts:
        c["is_author"] = c["norm"] in author_norms
        del c["norm"], c["fl"]

    # cross-analysis summary
    person_authors = [a for a in authors if norm(a["name"]) and norm(a["name"]) not in NONPERSON]
    by_cat_authors = {c: 0 for c in PERSON_CATS}
    for v in author_cat.values():
        for c in v["categories"]:
            by_cat_authors[c] += 1
    by_cat_articles = {c: 0 for c in PERSON_CATS}
    for art in catalogue:
        acats = set()
        for au in art.get("authors", []):
            acats.update(author_cat.get(au, {}).get("categories", []))
        for c in acats:
            by_cat_articles[c] += 1

    summary = {
        "total_contacts": len(contacts),
        "total_authors": len(person_authors),
        "authors_in_crm": matched,
        "authors_by_category": by_cat_authors,
        "articles_by_category": by_cat_articles,
    }

    PRIV.mkdir(exist_ok=True)
    (PRIV / "contacts.json").write_text(json.dumps(contacts, indent=2, ensure_ascii=False))
    (PRIV / "author_categories.json").write_text(json.dumps(author_cat, indent=2, ensure_ascii=False))
    (PRIV / "cross_analysis.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"Contacts: {len(contacts)} | authors matched to CRM: {matched}/{len(person_authors)}")
    print("Wrote private/contacts.json, author_categories.json, cross_analysis.json")


if __name__ == "__main__":
    main()
