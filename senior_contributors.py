#!/usr/bin/env python3
"""
Pull the senior-contributor roster from vitalcitynyc.org/contributors/ and
record it where the other products can read it.

Writes data/senior_contributors.json (names, author slugs, bios, all public on
the site) and flags matching authors in data/authors.json with senior: true.
Fails loudly if the page comes back without the roster: a silent empty list
would quietly strip the flag from every product on the next rebuild.
"""
import html, json, re, sys, urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent
URL = "https://www.vitalcitynyc.org/contributors/"
OUT = ROOT / "data" / "senior_contributors.json"
AUTHORS = ROOT / "data" / "authors.json"
MIN_EXPECTED = 10

def fetch():
    req = urllib.request.Request(URL, headers={"User-Agent": "vital-city-catalogue/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")

def clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()

def parse(page):
    sec = re.search(r'<section id="senior-contributors".*?</section>', page, re.S)
    if not sec:
        return []
    people = []
    for card in re.findall(r'<li class="senior-card">(.*?)</li>', sec.group(0), re.S):
        name = re.search(r'senior-card__name"><a href="([^"]+)">(.*?)</a>', card, re.S)
        bio = re.search(r'senior-card__bio">(.*?)</p>', card, re.S)
        if not name:
            continue
        slug = name.group(1).strip("/").split("/")[-1]
        people.append({"name": clean(name.group(2)), "slug": slug,
                       "bio": clean(bio.group(1)) if bio else ""})
    return people

def norm(s):
    s = re.sub(r"[^a-z ]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()

def main():
    people = parse(fetch())
    if len(people) < MIN_EXPECTED:
        sys.exit(f"senior contributors: found {len(people)} on {URL} (expected at least {MIN_EXPECTED}) "
                 f"- the page changed or the section is gone. Not overwriting {OUT.name}.")
    OUT.write_text(json.dumps({"as_of": date.today().isoformat(), "source": URL,
                               "count": len(people), "people": people}, indent=1))
    # annotate the author roster; strip stale flags first so a demotion propagates
    if AUTHORS.exists():
        authors = json.loads(AUTHORS.read_text())
        by = {norm(p["name"]): p for p in people}
        slugs = {p["slug"] for p in people}
        hit = 0
        for a in authors:
            a.pop("senior", None)
            if norm(a.get("name", "")) in by or a.get("slug") in slugs:
                a["senior"] = True; hit += 1
        AUTHORS.write_text(json.dumps(authors, indent=1))
        print(f"senior contributors: {len(people)} on the site; {hit} matched in authors.json")
        miss = [p["name"] for p in people if norm(p["name"]) not in {norm(a.get("name","")) for a in authors}]
        if miss:
            print("  not in authors.json (no byline yet?):", ", ".join(miss))
    else:
        print(f"senior contributors: {len(people)} on the site")

if __name__ == "__main__":
    main()
