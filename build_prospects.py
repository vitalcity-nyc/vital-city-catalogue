#!/usr/bin/env python3
"""Build the fundraising-prospects intelligence page's data.

Reads   private/people.json    (merged contact DB: Ghost + Mailchimp + Donorbox + CRM)
        private/growth.json    (growth pull: Donorbox windows, campaigns)
        data/catalogue.json    (public catalogue, for editorial-fit counts)
        private/employer_inferences.json  (optional; tier-B principals)
Writes  private/prospects.json (plaintext, gitignored)
        prospects/data.enc     (AES-256-GCM, same passphrase as the other tools)

Design rules, deliberately repeated from the rest of the repo:
- Facts and inferences are labelled differently. Curated CRM tags and Donorbox
  amounts are facts; employer inferences and the external funder list are leads
  that carry their basis with them.
- The external grant-source list is a set of LEADS TO VERIFY, not research: the
  page says so. Nothing here should be pasted into a grant application.
- No single opaque score. Every list shows the components (gave, opens, clicks,
  tag) that put a person on it.
"""
import json, os, re, base64, secrets, statistics as st
from datetime import date, datetime, timedelta
from pathlib import Path
from collections import defaultdict, Counter
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

ROOT = Path(__file__).resolve().parent
PEOPLE = Path(os.environ.get("PROSPECTS_PEOPLE", ROOT / "private" / "people.json"))
GROWTH = Path(os.environ.get("PROSPECTS_GROWTH", ROOT / "private" / "growth.json"))
CAT    = ROOT / "data" / "catalogue.json"
INFER  = ROOT / "private" / "employer_inferences.json"
OUT    = ROOT / "private" / "prospects.json"
ENC    = ROOT / "prospects" / "data.enc"
ITERS  = 600_000
TODAY  = date.today()

# ---------------------------------------------------------------------------
# The curated funder list. Two kinds of rows:
#   - warm: the org already reads Vital City (matched to subscribers below)
#   - lead: well-known funders of policy journalism / civic research whose
#           public focus plausibly fits Vital City's beats. These are LEADS —
#           current programs, deadlines and eligibility must be verified on the
#           funder's own site before anyone acts.
# fit: which Vital City beats the funder's stated focus overlaps.
# ---------------------------------------------------------------------------
# "current": named as a funder on vitalcitynyc.org/about (read Aug 2026) —
# these are renewal-and-growth relationships, not prospects.
FUNDERS = [
 {"name":"Arnold Ventures","domain":"arnoldventures.org","current":True,
  "focus":"Criminal justice policy, evidence-based government",
  "fit":["criminal justice","data journalism"],
  "note":"CURRENT FUNDER. John Arnold has a byline and a 99% open rate; 12 staff read. The renewal case writes itself — the expansion case is their government-performance program."},
 {"name":"Harry Frank Guggenheim Foundation","domain":"hfg.org","current":True,
  "focus":"Research on violence and its reduction",
  "fit":["criminal justice","gun violence"],
  "note":"CURRENT FUNDER — and two staff donate personally on top of it. The strongest institutional relationship in the file."},
 {"name":"Charles H. Revson Foundation","domain":"revsonfoundation.org","current":True,
  "focus":"New York City civic life, journalism, urban affairs",
  "fit":["nyc civic","journalism"],
  "note":"CURRENT FUNDER. Julie Sandorf led its local-news grantmaking; four staff read."},
 {"name":"Achelis & Bodman Foundation","domain":"achelis-bodman-fnd.org","current":True,
  "focus":"New York City and area nonprofits",
  "fit":["nyc civic"],
  "note":"CURRENT FUNDER per the about page. No staff found among readers — the relationship exists outside the subscriber file."},
 {"name":"Public Welfare Foundation","domain":"publicwelfare.org","current":True,
  "focus":"Criminal and youth justice reform",
  "fit":["criminal justice"],
  "note":"CURRENT FUNDER per the about page. No staff found among readers."},
 {"name":"Teagle Foundation","domain":"teagle.org","current":True,
  "focus":"Liberal arts education and civic initiatives",
  "fit":["education","nyc civic"],
  "note":"CURRENT FUNDER per the about page. No staff found among readers."},
 {"name":"MacArthur Foundation","domain":"macfound.org",
  "focus":"Journalism and media; criminal justice reform",
  "fit":["journalism","criminal justice"],
  "note":"Laurie Garduque (criminal justice program) is a reader."},
 {"name":"Robin Hood","domain":"robinhood.org",
  "focus":"New York City poverty",
  "fit":["nyc civic","economy"],
  "note":"Richard Buery is a Vital City advisor."},
 {"name":"Bloomberg Philanthropies","domain":"bloomberg.org",
  "focus":"Cities, public health, government innovation",
  "fit":["city government","public health"],
  "note":"Seven readers including Bloomberg Associates staff; Linda Gibbs and Rose Gill read."},
 {"name":"Tiger Foundation","domain":"tigerfoundation.org","current":True,
  "focus":"Breaking the cycle of poverty in New York City",
  "fit":["nyc civic","education"],
  "note":"CURRENT FUNDER. Charles Buice (president) is an engaged reader."},
 {"name":"Clark Foundation","domain":"clarkfoundation.org",
  "focus":"New York City nonprofits and opportunity",
  "fit":["nyc civic"],
  "note":"Doug Bauer (executive director) is a reader."},
 {"name":"Fund for the City of New York","domain":"fcny.org",
  "focus":"NYC government performance and civic innovation",
  "fit":["city government"],
  "note":"Already Vital City's fiscal sponsor — the relationship exists by construction."},
 {"name":"Tow Foundation","domain":"towfoundation.org","current":True,
  "focus":"Juvenile and criminal justice; investigative journalism",
  "fit":["criminal justice","journalism"],
  "note":"CURRENT FUNDER — a reminder that funding and readership are different things: only one subscriber on file."},
 # ---- leads: abundance / state capacity — THE ACTIVE HUNT ----
 {"name":"Coefficient Giving","domain":"coefficientgiving.org","lead":True,"cat":"abundance","pursuit":True,
  "focus":"Abundance and growth: housing, land use, state capacity, evidence-based policy",
  "fit":["housing","city government","economy"],
  "note":"IN PURSUIT and furthest along: Matt Clancy (Abundance & Growth fund) is engaged and was intrigued by COGE as a jumping-off point for 'DOGE done right'; an 18-month government-effectiveness project (~$300K Vital City + $125K partner) is being pitched, intro sent Aug 3. CEO Alexander Berger has amplified Ted's work on X."},
 {"name":"Arnold Ventures — government performance","domain":"arnoldventures.org","lead":True,"cat":"abundance",
  "focus":"Evidence-based policy, government performance (beyond the criminal-justice program above)",
  "fit":["city government","data journalism"],
  "note":"Same warm door, second program: the abundance-adjacent pitch runs through their evidence-based-government side."},
 {"name":"Emergent Ventures (Mercatus)","domain":"mercatus.org","lead":True,"cat":"abundance",
  "focus":"Fast grants to people and projects advancing progress and state capacity",
  "fit":["city government","economy"],
  "note":"Small, fast, person-shaped grants — a fit for a specific Vital City project or fellow rather than general support."},
 {"name":"Hewlett Foundation — Economy and Society","domain":"hewlett.org","lead":True,"cat":"abundance",
  "focus":"Rethinking economic policy ideas and institutions",
  "fit":["economy","city government"],
  "note":"Ideas-infrastructure funder; commentary and policy analysis is squarely what it buys. Verify current strategy docs."},
 {"name":"Omidyar Network","domain":"omidyar.com","lead":True,"cat":"abundance",
  "focus":"Reimagining capitalism, technology and governance",
  "fit":["economy","technology"],
  "note":"Adjacent rather than central; strongest if the pitch leads with data analysis of how city systems perform."},
 {"name":"TransitCenter","domain":"transitcenter.org","lead":True,"cat":"abundance",
  "focus":"US transit improvement and advocacy (an operating foundation)",
  "fit":["transit","city government"],
  "note":"The subway-safety recommendations — NYT exclusive, partly adopted by the governor and MTA — are a ready-made door here. It runs its own programs as much as it grants; the fit is partnership as much as funding."},
 {"name":"Renaissance Philanthropy","domain":"renaissancephilanthropy.org","lead":True,"cat":"abundance",
  "focus":"Ambitious science, technology and state-capacity initiatives",
  "fit":["city government","technology"],
  "note":"Kumar Garg (president; Tom Kalil is CEO). Garg co-wrote the Sept. 2026 'Building the bridge from innovation to impact' post arguing philanthropy should fund the whole path from a finding to adoption, which is Vital City's follow-through pitch in their own words. Warm door: senior contributor Cara Eckholm is a Renaissance fellow. Confidence on current programs is lower — verify before investing time."},
 # ---- leads: journalism — OFF-CENTER for a commentary/analysis shop ----
 {"name":"Press Forward","domain":"pressforward.news","lead":True,"cat":"journalism",
  "focus":"National coalition funding local news (>$500M pooled)",
  "fit":["journalism","nyc civic"],
  "note":"Huge pool, but aimed at news gathering. Vital City is commentary and analysis, not core local news — expect eligibility friction; the data-journalism output is the only natural wedge."},
 {"name":"Knight Foundation","domain":"knightfoundation.org","lead":True,"cat":"journalism",
  "focus":"Journalism, informed communities",
  "fit":["journalism","data journalism"],
  "note":"Reference journalism funder, same caveat: the informed-communities frame fits better than the newsroom frame."},
 {"name":"American Journalism Project","domain":"theajp.org","lead":True,"cat":"journalism",
  "focus":"Venture philanthropy for nonprofit local news",
  "fit":["journalism"],
  "note":"Funds local NEWS operations; a policy journal is off-profile. Low priority."},
 {"name":"Democracy Fund","domain":"democracyfund.org","lead":True,"cat":"journalism",
  "focus":"Local news ecosystems, civic engagement",
  "fit":["journalism","nyc civic"],
  "note":"The civic-engagement side is the fit, not the news side."},
 {"name":"JM Kaplan Fund","domain":"jmkfund.org","lead":True,
  "focus":"NYC civic innovation (Innovation Prize; historic city grantmaking)",
  "fit":["nyc civic","city government"],
  "note":"Small, NYC-specific, unsolicited-friendly historically."},
 {"name":"New York Community Trust","domain":"thenytrust.org","lead":True,
  "focus":"NYC community foundation; journalism and civic programs",
  "fit":["nyc civic","journalism"],
  "note":"Broad NYC funder with a standing grants process."},
 {"name":"Ford Foundation","domain":"fordfoundation.org","lead":True,
  "focus":"Cities and states; disruption of inequality; creativity and free expression",
  "fit":["nyc civic","criminal justice"],
  "note":"Large and slow; usually invitation-driven. A relationship play, not an application play."},
]



# Who each funder currently funds — the fit check Josh asked for. Researched
# Aug 2026 with sources; where a funder was NOT researched this pass, the field
# says so instead of guessing. Grant claims are examples, not exhaustive lists,
# and current-ness must be verified on the funder's own grants database.
GRANTEES = {
 "Coefficient Giving": {"who":"California YIMBY ($2M general support), YIMBY Action, YIMBY Law, Sightline Institute, Urban Institute, Greater Greater Washington — ~$27M into housing/land-use reform since 2015","src":"openphilanthropy.org grants pages; Inside Philanthropy (verified Aug 2026)"},
 "Hewlett Foundation — Economy and Society": {"who":"Roosevelt Institute, Niskanen Center, MIT Economics (Shaping the Future of Work) — $33.2M across 49 grants in 2024","src":"hewlett.org; Chronicle of Philanthropy (verified Aug 2026)"},
 "Arnold Ventures": {"who":"Council on Criminal Justice and CUNY Institute for State & Local Governance were both launched with Arnold support — and both have staff among Vital City readers","src":"widely reported; verify current grants at arnoldventures.org/grants"},
 "Arnold Ventures — government performance": {"who":"See Arnold Ventures above — same grants database covers the government-performance portfolio","src":"arnoldventures.org/grants"},
 "Harry Frank Guggenheim Foundation": {"who":"Grants to individual scholars researching violence (its core program), plus research prizes — it funds researchers more than organizations","src":"hfg.org (program structure; verify)"},
 "Revson Foundation": {"who":"Helped launch THE CITY; long record of NYC journalism and civic grants","src":"widely reported; verify at revsonfoundation.org/grants"},
 "Renaissance Philanthropy": {"who":"Fellows and programs in science, technology and state capacity; Cara Eckholm (Vital City senior contributor) is a fellow. Kumar Garg, president, co-wrote 'Building the bridge from innovation to impact' (Sept. 2026)","src":"renaissancephilanthropy.substack.com/p/building-the-bridge-from-innovation (added 2026-09-10; verify current programs at renaissancephilanthropy.org)"},
 "Emergent Ventures (Mercatus)": {"who":"Small fast grants to individual researchers, writers and founders rather than organizations — the fit is a person or project, not general support","src":"mercatus.org/emergent-ventures (program structure)"},
}

EVENT_CSV = ROOT / "private" / "events" / "2025-11-fundraiser.csv"

def load_event(people):
    """The November 2025 house party — the one recorded ASK in the file.
    Matches invitees to the contact DB by email, then by normalized name."""
    import csv as _csv
    if not EVENT_CSV.exists():
        return None
    rows = list(_csv.DictReader(open(EVENT_CSV)))
    by_email = {}
    for p in people:
        for e in ([p.get("e")] + (p.get("emails") or [])):
            if e: by_email[e.lower()] = p
    by_name = {(p.get("n") or "").strip().lower(): p for p in people if p.get("n")}
    out = {"invited": len(rows), "attended": [], "regrets": 0, "no_response": 0}
    for r in rows:
        st = (r.get("Status") or "").strip()
        email = (r.get("Email/Phone Number") or "").strip().lower()
        name = (r.get("Full Name") or "").strip()
        p = by_email.get(email) or by_name.get(name.lower())
        if st == "Attending":
            gave_after = bool(p and (p.get("dlast") or "") >= "2025-11")
            rec = person_row(p, "RSVP'd yes to the Nov '25 party") if p else                   {"n": name, "e": email, "inst": "", "damt": 0, "dcnt": 0, "dlast": "",
                   "eopen": 0, "eclick": 0, "wiki": 0, "types": [],
                   "why": "RSVP'd yes; NOT in the contact database"}
            rec["gave_after"] = 1 if gave_after else 0
            if not gave_after and rec["why"].startswith("RSVP"):
                rec["why"] += " — no gift since"
            out["attended"].append(rec)
        elif st == "Regrets":
            out["regrets"] += 1
        else:
            out["no_response"] += 1
    out["attended"].sort(key=lambda x: (x["gave_after"], -x["damt"], -x["eopen"]))
    out["gave_after"] = sum(1 for x in out["attended"] if x["gave_after"])
    out["gave_after_amt"] = round(sum(x["damt"] for x in out["attended"] if x["gave_after"]))
    out["unconverted"] = sum(1 for x in out["attended"] if not x["gave_after"])
    return out

# Senior contributors — Josh's slate (docx, Aug 2026): the 34 marked YES
# (yellow+underline). Gelinas declined; Florida and Morgan Williams were
# unconfirmed at read time. Display name -> exact catalogue author name (None =
# no byline yet). Descriptors are pulled from each author's own Ghost bio at
# build time, so they stay current and are never invented here.
# The senior-contributor roster comes from the site (data/senior_contributors.json,
# refreshed nightly by senior_contributors.py). This hand list is only the
# fallback for a checkout that has never pulled it.
SENIORS_FALLBACK = [
  ("Alex Armlovich","Alex Armlovich"),("Neil Barsky","Neil Barsky"),
  ("Richard Buery Jr.","Richard Buery Jr."),("Vishaan Chakrabarti","Vishaan Chakrabarti"),
  ("Aaron Chalfin","Aaron Chalfin"),("Jelani Cobb","Jelani Cobb"),
  ("John Della Volpe","John Della Volpe"),("Brandon del Pozo","Brandon del Pozo"),
  ("Jennifer Doleac","Jennifer Doleac"),("Cara Eckholm","Cara Eckholm"),
  ("Ingrid Gould Ellen","Ingrid Gould Ellen"),("Barry Friedman","Barry Friedman"),
  ("Edward Glaeser","Edward Glaeser"),("Sherry Glied","Sherry Glied"),
  ("Gloria Gong","Gloria Gong"),("Henry Grabar","Henry Grabar"),
  ("Arpit Gupta","Arpit Gupta"),("Anna Harvey","Anna Harvey"),
  ("Nancy La Vigne",None),("Errol Louis","Errol Louis"),
  ("Jens Ludwig","Jens Ludwig"),("John MacDonald","John MacDonald"),
  ("Tracey L. Meares","Tracey L. Meares"),("Peter Moskos","Peter Moskos"),
  ("Alex R. Piquero","Alex R. Piquero"),("Kerri M. Raissian","Kerri M. Raissian"),
  ("John K. Roman","John K. Roman"),("Julie Sandorf","Julie Sandorf"),
  ("David Schleicher","David Schleicher"),("Harry Siegel","Harry Siegel"),
  ("Martha Stark","Martha Stark"),("Carl Weisbrod","Carl Weisbrod"),
  ("Claire Weisz","Claire Weisz"),("Bruce Western","Bruce Western"),
]
def _live_seniors(authors_by_name):
    f = ROOT / "data" / "senior_contributors.json"
    if not f.exists():
        return SENIORS_FALLBACK
    names = [x["name"] for x in (json.loads(f.read_text()) or {}).get("people", [])]
    return [(n, n if n in authors_by_name else None) for n in names] or SENIORS_FALLBACK

# The ten to feature as cards on the influence slide (breadth of discipline:
# economics, architecture, policing, law, housing, media, philanthropy). The
# other 24 roll into the aggregate bench line.
FEATURED = {"Edward Glaeser","Vishaan Chakrabarti","Jelani Cobb","Richard Buery Jr.",
            "Tracey L. Meares","Jens Ludwig","Ingrid Gould Ellen","Aaron Chalfin",
            "Brandon del Pozo","Errol Louis"}
# Six of the featured ten, in the order they read best in a single sentence,
# with a tag short enough for a one-pager. Each tag is a plain restatement of
# the author's own bio on vitalcitynyc.org (nothing here is inferred).
# A short tag for every senior contributor, restating the bio on
# vitalcitynyc.org/contributors. Used by the one-pager's audience variants.
SHORT_TAGS = {
  "Alex Armlovich": "Niskanen Center housing analyst, Rent Guidelines Board member",
  "Neil Barsky": "former Wall Street Journal reporter, director of 'Koch'",
  "Richard Buery Jr.": "CEO of Robin Hood",
  "Vishaan Chakrabarti": "architect and urbanist",
  "Aaron Chalfin": "Penn criminologist",
  "Jelani Cobb": "dean of the Columbia Journalism School",
  "Brandon del Pozo": "Brown University, former Burlington police chief",
  "John Della Volpe": "Harvard Kennedy School polling director",
  "Jennifer Doleac": "Arnold Ventures, criminal justice",
  "Cara Eckholm": "Renaissance Philanthropy fellow, host of Borrow & Steal",
  "Ingrid Gould Ellen": "NYU Furman Center",
  "Barry Friedman": "NYU Law, Policing Project founder",
  "Edward Glaeser": "Harvard economist",
  "Sherry Glied": "NYU Wagner, its dean from 2013 to 2025",
  "Gloria Gong": "Harvard Government Performance Lab",
  "Henry Grabar": "author of 'Paved Paradise'",
  "Arpit Gupta": "NYU Stern economist",
  "Anna Harvey": "president of the Social Science Research Council",
  "Nancy La Vigne": "dean of the Rutgers School of Criminal Justice",
  "Errol Louis": "NY1 anchor",
  "Jens Ludwig": "director of the University of Chicago Crime Lab",
  "John MacDonald": "Penn criminologist",
  "Tracey L. Meares": "Yale Law, Justice Collaboratory founder",
  "Peter Moskos": "John Jay College criminologist",
  "Alex R. Piquero": "University of Miami, former head of the Bureau of Justice Statistics",
  "Kerri M. Raissian": "UConn, gun-injury prevention",
  "John K. Roman": "NORC at the University of Chicago",
  "Julie Sandorf": "president of the Revson Foundation",
  "David Schleicher": "Yale Law professor",
  "Harry Siegel": "The City Reporter, FAQ NYC podcast",
  "Martha Stark": "NYU Wagner, former city finance commissioner",
  "Carl Weisbrod": "former chair of the City Planning Commission",
  "Claire Weisz": "founding partner of WXY",
  "Bruce Western": "Columbia sociologist, Justice Lab director",
  "Morgan C. Williams Jr.": "Barnard economist",
}
SPOTLIGHT = [
  ("Edward Glaeser", "Harvard economist"),
  ("Jens Ludwig", "director of the University of Chicago Crime Lab"),
  ("Ingrid Gould Ellen", "NYU Furman Center"),
  ("Jelani Cobb", "dean of the Columbia Journalism School"),
  ("Errol Louis", "NY1 anchor"),
  ("Richard Buery Jr.", "CEO of Robin Hood"),
]

# Audience-targeted deck variants. Same skeleton, different emphasis: which
# receipts lead, which authors are carded, which pieces are spotlit, which
# product comes first. Spotlight slugs are RESOLVED against the catalogue at
# build time — a missing slug is dropped with a warning, never invented.
VARIANTS = {
 "abundance": {
   "label": "Prepared for funders of abundance & state capacity",
   "spot_title": "Selected work: building, permitting, governing",
   "receipts": ["Permitting","Zohran Mamdani","Subway safety","Crime data","Rikers Island"],
   "authors": ["Edward Glaeser","David Schleicher","Arpit Gupta","Alex Armlovich","Henry Grabar",
               "Ingrid Gould Ellen","Vishaan Chakrabarti","Martha Stark","Carl Weisbrod","Claire Weisz"],
   "products": ["Just Fix It","Rubber Meets Road","What To Do (and Not To Do)"],
   "spots": ["nyc-housing-permits-fast-track-construction-mamdani",
             "government-improvements-mamdani-can-tackle-in-the-first-100-days",
             "nyc-grocery-cost-explained",
             "mamdani-pied-a-terre-surcharge-tax-rollout-mistake-nyc",
             "nyc-economy-zohran-mamdani-efficiency",
             "nyc-joint-developments-public-land-housing",
             "new-yorks-mamdani-ny-civil-service-system",
             "expert-advice-for-mamdanis-commission-on-government-efficiency"],
   "extra_spots": [{"t": "The housing issue — 29 pieces on how New York builds (Issue 14)",
                    "u": "https://www.vitalcitynyc.org/issue-14/", "a": "A full themed issue"}]},
 "justice": {
   "label": "Prepared for funders of criminal-justice policy",
   "spot_title": "Selected work: public safety and justice",
   "receipts": ["Rikers Island","Crime data","Subway safety","Zohran Mamdani","Permitting"],
   "authors": ["Aaron Chalfin","Brandon del Pozo","Tracey L. Meares","Jens Ludwig","Bruce Western",
               "Alex R. Piquero","Jennifer Doleac","John K. Roman","Anna Harvey","John MacDonald"],
   "products": ["What To Do (and Not To Do)","Just Fix It","Rubber Meets Road"],
   "spots": ["crime-in-new-york-city-trends-statistics",
             "twenty-strategies-for-reducing-crime-in-cities",
             "what-to-do-about-subway-safety-nyc-policy-recommendations",
             "what-to-do-about-people-in-crisis-on-streets-and-subways",
             "the-rikers-receivership-risk-and-opportunity",
             "what-we-know-and-dont-know-about-guns",
             "real-crime-numbers-nyc-nypd"],
   "extra_spots": [
     {"t": "What Mamdani Can Learn from a de Blasio Administration Safety Innovation",
      "u": "https://www.vitalcitynyc.org/nstat-should-be-key-to-mamdani-public-safety-plan/",
      "a": "Renita Francois — now NYC Deputy Mayor for Community Safety"},
     {"t": "Inside Rikers: Jails Can Be Safer and More Humane — a 16-piece themed issue",
      "u": "https://www.vitalcitynyc.org/inside-rikers-jails-can-be-safer-and-more-humane/",
      "a": "A full themed issue"},
     {"t": "How Many People Should New York City's Jails Hold? — a 19-piece themed issue",
      "u": "https://www.vitalcitynyc.org/how-many-people-should-new-york-citys-jails-hold/",
      "a": "A full themed issue"}]},
 "civic": {
   "label": "Prepared for funders of civic life & local journalism",
   "spot_title": "Selected work: the civic conversation",
   "receipts": ["Zohran Mamdani","Subway safety","Permitting","Crime data","Rikers Island"],
   "authors": ["Errol Louis","Jelani Cobb","Richard Buery Jr.","Harry Siegel","Julie Sandorf",
               "John Della Volpe","Sherry Glied","Barry Friedman","Cara Eckholm","Kerri M. Raissian"],
   "products": ["Rubber Meets Road","Just Fix It","What To Do (and Not To Do)"],
   "spots": ["what-has-mamdani-done-so-far",
             "assessing-mamdani-six-months-in",
             "zohran-mamdani-talks-public-safety",
             "what-la-guardia-gave-new-york",
             "social-infrastructure-nyc-mamdani",
             "mamdani-first-100-days-scorecard-nyc"]},
}

# Online giving opened in November 2025 (Donorbox; the first gift was Nov. 12, 2025).
GIVING_OPENED = "November 2025"

def _current_funders():
    """The underwriters named on vitalcitynyc.org/about. Fetched live; the last
    good list is cached so a fetch failure never blanks the section."""
    import urllib.request
    cache = ROOT / "data" / "funders_current.json"
    names = []
    try:
        req = urllib.request.Request("https://www.vitalcitynyc.org/about/", headers={"User-Agent": "Mozilla/5.0 (vital-city-catalogue build)"})
        page = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")
        txt = re.sub(r"<[^>]+>", " ", page); txt = re.sub(r"\s+", " ", txt).replace("&amp;", "&")
        m = re.search(r"underwritten by (.+?), as well as", txt) or re.search(r"underwritten by (.+?)\.\s", txt)
        if m:
            names = [re.sub(r"^the\s+", "", x.strip()) for x in re.split(r",\s*|\s+and\s+", m.group(1)) if x.strip()]
    except Exception as e:
        print(f"  WARNING: about-page fetch failed ({e}); using the cached funder list")
    if len(names) >= 3:
        cache.write_text(json.dumps({"as_of": TODAY.isoformat(), "source": "https://www.vitalcitynyc.org/about/", "names": names}, indent=1) + "\n")
        return {"names": names, "as_of": TODAY.isoformat()}
    if cache.exists():
        c = json.loads(cache.read_text()); return {"names": c.get("names", []), "as_of": c.get("as_of", "")}
    return {"names": [], "as_of": ""}

def bio_descriptor(bio):
    """First clause of the author's own Ghost bio, as the safe descriptor."""
    b = re.sub(r"^\s*(is|was|became)\s+(an?|the)?\s*", "", (bio or "").strip(), flags=re.I)
    # Named chairs read "the Edwin A. and Betty L. Bergman Distinguished Service
    # Professor" — splitting on ". " chopped that to "Edwin A". Split only on a
    # sentence end (period followed by a capital, not an initial).
    b = re.split(r"(?<=[a-z\)])\.\s+(?=[A-Z])|, where| and (?:a |an |the |former )", b)[0].strip(" .,;")
    return b[:96]

# Funder conversations on record as of an Aug 2026 review of fundraising email.
# NO status field and NO quoted email text: this page gets shared, and neither
# the current state of a conversation nor anyone's words are ours to publish.
# Each row is a neutral summary of the subject matter, dated to the last record
# found. Treat every row as possibly out of date.
PIPELINE = [
 {"name":"Coefficient Giving — COGE project",
  "date":"2026-08-04","note":"18-month government-effectiveness project. Roughly $300K in Vital City costs plus $125K for a partner. Budget was being worked out at the time of this review."},
 {"name":"New America (reporting grants)",
  "date":"2026-07-15","note":"Reporting-grant program that could support a story on the Commission on Government Efficiency. Small and fast by design."},
 {"name":"ABNY Foundation",
  "date":"2026-07-23","note":"Summer Youth Employment Program application was declined, from a field of 190 applicants. A partnership around convenings and a playbook release was raised separately."},
 {"name":"William T. Grant Foundation",
  "date":"2025-03-27","note":"Funder-initiated contact in March 2025. Youth-outcomes research funder. No later record found in this review."},
 {"name":"Education funders (via Charles Sahm)",
  "date":"2026-07-31","note":"Funder maps for The 74, Chalkbeat/Civic News and peers, with notes on current interests. A ready-made target list for an education-vertical pitch."},
 {"name":"SocialSphere Index (Della Volpe)",
  "date":"2026-02-16","note":"Joint Vital City/SocialSphere research index with a funder pitch built into the final report. A packaged product to put in front of funders."},
]

# Press mentions per year, from the hand-kept "media mentions and praise"
# archive (Josh's docx, 293 dated entries parsed Aug 2026, last entry
# 2026-06-22). This is the ONLY consistent multi-year mentions series: the
# automated tracker begins Nov 2025 and covers a ~25-outlet whitelist, while
# this archive is broader (Atlantic, Reason, HKS, Washington Times...) but
# updated by hand. The two are never mixed in one chart.
MENTIONS_ARCHIVE = {"2021": 1, "2022": 6, "2023": 14, "2024": 57, "2025": 133, "2026": 82}
MENTIONS_ARCHIVE_ASOF = "2026-06-22"

def load(path, default=None):
    try:
        return json.load(open(path))
    except Exception:
        return default


def years_since(s):
    try:
        d = datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
        return (TODAY - d).days / 365
    except Exception:
        return None


def person_row(r, why):
    """The displayed record for one prospect — components, never a single score."""
    return {"n": r.get("n") or "", "e": r.get("e") or "",
            "inst": (r.get("inst") or "").strip(),
            "damt": round(r.get("damt") or 0), "dcnt": r.get("dcnt") or 0,
            "dlast": (r.get("dlast") or "")[:10],
            "eopen": r.get("eopen") or 0, "eclick": r.get("eclick") or 0,
            "wiki": 1 if r.get("wiki") else 0,
            "types": r.get("types") or [], "why": why,
            "role": (r.get("role") or "").strip(), "seg": r.get("seg") or "",
            "nyc": 1 if r.get("nyc") else 0, "pros": r.get("pros") or 0,
            "prosw": r.get("prosw") or ""}


def main():
    people = load(PEOPLE) or []
    growth = load(GROWTH) or {}
    cat = load(CAT) or []
    # FAIL LOUD. A missing or truncated source must never produce a plausible-
    # looking page of zeros — that already happened once (a build ran against
    # deleted /tmp copies, silently wrote zero-subscriber data, and was
    # committed). Empty in, error out.
    if len(people) < 1000:
        raise SystemExit(f"REFUSING TO BUILD: people source {PEOPLE} has {len(people)} rows (floor 1000). "
                         "Wrong path or truncated file — fix the source, do not publish zeros.")
    if not (growth.get("mailchimp") or {}).get("total_subscribers"):
        raise SystemExit(f"REFUSING TO BUILD: growth source {GROWTH} lacks mailchimp.total_subscribers. "
                         "Wrong path or truncated file — fix the source, do not publish zeros.")
    if len(cat) < 100:
        raise SystemExit(f"REFUSING TO BUILD: catalogue has {len(cat)} pieces (floor 100).")
    infer = (load(INFER) or {}).get("rows", [])

    sub = [r for r in people if r.get("mem") and not r.get("unsub") and r.get("e")]
    donors = [r for r in people if r.get("don")]
    db = growth.get("donorbox", {}) or {}

    # ---------------- topline ----------------
    amts = sorted((r.get("damt") or 0) for r in donors)
    total = sum(amts)
    top5 = sum(amts[-5:]) if amts else 0
    repeat = sum(1 for r in donors if (r.get("dcnt") or 0) > 1)
    monthly = db.get("monthly_series") or []
    topline = {
        "raised": round(total), "donors": len(donors),
        "repeat": repeat, "repeat_pct": round(100 * repeat / len(donors)) if donors else 0,
        "top5_pct": round(100 * top5 / total) if total else 0,
        "median_gift": round(st.median(amts)) if amts else 0,
        "recurring_donors": db.get("active_recurring_donors"),
        "mrr": db.get("mrr_estimate"),
        "last_gift_month": max((r.get("dlast") or "" for r in donors), default="")[:7],
        "best_month": max(monthly, key=lambda m: m.get("amt", 0))["m"] if monthly else None,
        "best_month_amt": round(max((m.get("amt", 0) for m in monthly), default=0)),
    }

    # ---------------- warm doors: funders already reading ----------------
    by_funder = defaultdict(list)
    for r in sub:
        dom = (r.get("e") or "").split("@")[-1].lower()
        inst = (r.get("inst") or "").lower()
        for f in FUNDERS:
            if dom == f["domain"] or (len(f["name"]) > 6 and f["name"].lower() in inst):
                by_funder[f["name"]].append(r)
                break
    funders_out = []
    for f in FUNDERS:
        ppl = by_funder.get(f["name"], [])
        engaged = [r for r in ppl if (r.get("eopen") or 0) >= 50]
        funders_out.append({
            "name": f["name"], "focus": f["focus"], "fit": f["fit"], "note": f["note"],
            "lead": bool(f.get("lead")), "cat": f.get("cat"), "pursuit": bool(f.get("pursuit")),
            "current": bool(f.get("current")),
            "grantees": GRANTEES.get(f["name"]),
            "readers": len(ppl), "engaged": len(engaged),
            "donors": sum(1 for r in ppl if r.get("don")),
            "names": [r.get("n") for r in sorted(ppl, key=lambda r: -(r.get("eopen") or 0))
                      if r.get("n")][:4],
        })
    funders_out.sort(key=lambda f: (f["lead"], -f["engaged"], -f["readers"]))

    # ---------------- editorial fit: what VC can show funders ----------------
    y2 = [p for p in cat if (p.get("published_date") or "") >= f"{TODAY.year-1}-01-01"]
    tc = Counter()
    for p in y2:
        for t in (p.get("topics") or []):
            tc[t] += 1
    beats = tc.most_common(12)

    # ---------------- individual prospect tiers ----------------
    def eng(r): return (r.get("eopen") or 0)

    advisors = [person_row(r, "Formal advisor; no recorded gift")
                for r in sub if "VC advisor" in (r.get("types") or []) and not r.get("don")]
    advisors.sort(key=lambda x: -x["eopen"])

    upgrade = [person_row(r, "Already gives at level; reads consistently")
               for r in sub if (r.get("damt") or 0) >= 500 and eng(r) >= 40]
    upgrade.sort(key=lambda x: -x["damt"])

    second = [person_row(r, "One gift, still highly engaged — the second ask")
              for r in sub if r.get("don") and (r.get("dcnt") or 0) == 1
              and 100 <= (r.get("damt") or 0) < 500 and eng(r) >= 50]
    second.sort(key=lambda x: (-x["eopen"], -x["damt"]))

    notables = [person_row(r, "Public profile; never asked")
                for r in sub if r.get("wiki") and not r.get("don") and eng(r) >= 50]
    notables.sort(key=lambda x: -x["eopen"])

    fstaff = [person_row(r, "Foundation leadership — institutional door, do NOT ask for a personal check")
              for r in sub if "foundation leadership" in (r.get("types") or [])]
    fstaff.sort(key=lambda x: -x["eopen"])

    # principals: their own firm in their email domain (employer inference tier B)
    by_email = {r.get("e"): r for r in sub}
    principals = []
    for row in infer:
        if row.get("conf") != "B":
            continue
        r = by_email.get(row.get("email"))
        if r and eng(r) >= 50 and not r.get("don"):
            pr = person_row(r, "Own-name firm or practice — likely a principal")
            pr["inst"] = pr["inst"] or (row.get("employer") or "")
            principals.append(pr)
    principals.sort(key=lambda x: -x["eopen"])

    # Newly identified by the August 2026 research passes: people who were an
    # anonymous email handle until this month, who now have a name, a job and a
    # reason to be on this page. Sitting officials, role mailboxes, students and
    # Vital City's own staff are excluded upstream and can never appear here.
    SEG_LABEL = {"senior-private": "Senior private sector", "private": "Private sector",
                 "funder": "Foundation staff", "nonprofit": "Nonprofit leadership",
                 "academic": "Academic", "media": "Media", "peer": "Peer publisher"}
    researched = []
    for r in sub:
        if (r.get("pros") or 0) < 4 or r.get("don") or r.get("excl"):
            continue
        if (r.get("seg") or "") not in ("senior-private", "funder"):
            continue
        why = SEG_LABEL.get(r.get("seg"), "Identified")
        if r.get("role"):
            why = f"{r['role']}"
        pr = person_row(r, why)
        researched.append(pr)
    researched.sort(key=lambda x: (-(x["eclick"] or 0), -(x["eopen"] or 0)))

    event = load_event(people)

    lybunt = [person_row(r, "Gave last year, nothing this year")
              for r in donors if (r.get("dlast") or "") < f"{TODAY.year}-01"
              and not r.get("unsub")]
    lybunt.sort(key=lambda x: -x["damt"])


    # ---------------- what we can show funders: computed, not typed ----------
    mc = growth.get("mailchimp", {}) or {}
    gt = growth.get("ghost_traffic", {}) or {}
    m26 = [r for r in (mc.get("monthly_campaigns") or []) if r.get("month", "") >= f"{TODAY.year}-01" and r.get("open_pct")]
    def _avg(rows, k):
        v = [r[k] for r in rows if r.get(k) is not None]
        return round(st.mean(v), 1) if v else None
    ts = [t for t in (gt.get("traffic_series") or []) if not t.get("partial")]
    half = len(ts) // 2
    tgrow = None
    if half >= 4:
        a = st.mean([t["visitors"] for t in ts[:half]]); b = st.mean([t["visitors"] for t in ts[half:]])
        tgrow = round((b - a) / a * 100)
    # Year-to-date reach for the one-pager: GA4 unique visitors this calendar
    # year, and visits over the same weeks of the prior year from the weekly
    # series (visits, not visitors, so the comparison is labelled as visits).
    ga4 = growth.get("ga4") or {}
    _yrs = {str(r.get("year")): r for r in ((ga4.get("by_year") or {}).get("years") or [])}
    ytd_users = (_yrs.get(str(TODAY.year)) or {}).get("users")
    _tw = ga4.get("traffic_weekly") or []
    def _ytd_visits(y):
        cut = f"{y}-{TODAY.month:02d}-{TODAY.day:02d}"
        return sum(w.get("visits") or 0 for w in _tw if (w.get("wk") or "")[:4] == str(y) and w.get("wk") <= cut)
    _v_now, _v_prev = _ytd_visits(TODAY.year), _ytd_visits(TODAY.year - 1)
    ytd_visits_pct = round((_v_now - _v_prev) / _v_prev * 100) if _v_prev >= 1000 and _v_now else None
    _sc = ((growth.get("search_console") or {}).get("windows") or {}).get("28") or {}
    _sct = _sc.get("totals") or {}
    _top26 = ((ga4.get("top_pages_by_year") or {}).get(str(TODAY.year)) or [{}])[0]
    _mbx = {m.get("label"): m for m in ((growth.get("engagement_extras") or {}).get("mailbox_engagement") or [])}
    _AP = {1:"Jan.",2:"Feb.",3:"March",4:"April",5:"May",6:"June",7:"July",8:"Aug.",9:"Sept.",10:"Oct.",11:"Nov.",12:"Dec."}
    _md = lambda d: f"{_AP[d.month]} {d.day}"
    # Last four full weeks against the same four weeks a year earlier (GA4 weekly visits).
    def _wk_sum(rows): return sum(w.get("visits") or 0 for w in rows)
    _tw_sorted = sorted(_tw, key=lambda w: w.get("wk") or "")
    _last4 = [w for w in _tw_sorted if (w.get("wk") or "") <= TODAY.isoformat()][-5:-1]
    _last4_prev = []
    for w in _last4:
        # 52 weeks back lands on the same weekday, so the week keys line up.
        try: back = (date.fromisoformat(w.get("wk")) - timedelta(days=364)).isoformat()
        except Exception: continue
        _last4_prev += [x for x in _tw_sorted if (x.get("wk") or "") == back]
    _v4, _v4p = _wk_sum(_last4), _wk_sum(_last4_prev)
    _v30_pct = round(100 * (_v4 - _v4p) / _v4p) if len(_last4) == 4 and len(_last4_prev) == 4 and _v4p >= 1000 else None
    # Reader giving, Donorbox only (see project notes): gifts of $1,000 and up.
    _big_gifts = sum(1 for r in donors if (r.get("damt") or 0) >= 1000)
    def _ym(v):
        """2021-10 -> Oct. 2021 (AP month style); anything else passes through."""
        try: y, m = str(v)[:7].split("-"); return f"{_AP[int(m)]} {y}"
        except Exception: return v
    dom = lambda r: (r.get("e") or "").split("@")[-1].lower()
    gov = [r for r in sub if dom(r).endswith(".gov")]
    edu = [r for r in sub if dom(r).endswith(".edu")]
    org = [r for r in sub if dom(r).endswith(".org")]
    core = sum(1 for r in sub if (r.get("eopen") or 0) >= 50)
    mentions = growth.get("news_mentions") or []
    m_outlets = len({(x.get("domain") or x.get("source") or "") for x in mentions if not x.get("own_post")})
    authors = {a for p in cat for a in (p.get("authors") or [])}
    # The site's own senior-contributor roster (senior_contributors.py, nightly).
    _roster = (json.loads((ROOT / "data" / "senior_contributors.json").read_text())
               if (ROOT / "data" / "senior_contributors.json").exists() else {}) or {}
    _roster_names = {x["name"] for x in _roster.get("people", [])}
    _roster_n = len(_roster_names)
    # v2: a showcase, not a stat dump. Tiles for the big numbers, receipts with
    # verified links, real press citations, and the named policy products.
    yoy_now = None; yoy_prev = None
    _cum = {r["month"]: r.get("cum_subs") for r in (mc.get("monthly_signups") or [])}
    if _cum:
        _mo = max(_cum); _prev = f"{int(_mo[:4])-1}{_mo[4:]}"
        yoy_now, yoy_prev = _cum.get(_mo), _cum.get(_prev)
    yoy_pct = round(100*(yoy_now-yoy_prev)/yoy_prev) if (yoy_now and yoy_prev) else None
    # Drop rows dated before Vital City existed (Sept 2021): those are social
    # profile pages whose account-creation dates leak in as publication dates,
    # not press citations. Genuine backfill from 2022 on stays.
    press = [x for x in mentions if not x.get("own_post")
             and (x.get("published_iso") or "9999") >= "2021-09"]
    p_out = Counter(x.get("domain") or "" for x in press)
    p_first = min((x.get("published_iso") or "9999" for x in press), default="")[:7]
    TOP_OUT = {"nytimes.com":"The New York Times","gothamist.com":"Gothamist","politico.com":"Politico",
               "thecity.nyc":"THE CITY","nydailynews.com":"Daily News","ny1.com":"NY1","wnyc.org":"WNYC",
               "nymag.com":"New York Magazine","cityandstateny.com":"City & State","nypost.com":"New York Post",
               "therealdeal.com":"The Real Deal","citylimits.org":"City Limits","crainsnewyork.com":"Crain's"}
    samples, seen_out = [], set()
    for x in sorted(press, key=lambda x: x.get("published_iso") or "", reverse=True):
        d0 = x.get("domain")
        _t = (x.get("title") or "").strip()
        # the tracker sometimes yields junk titles like "- The New York Times"
        if len(_t) < 16 or _t.startswith("-"):
            continue
        if d0 in TOP_OUT and d0 not in seen_out:
            samples.append({"outlet": TOP_OUT[d0], "date": (x.get("published_iso") or "")[:10],
                            "title": x.get("title") or "", "url": x.get("url") or ""})
            seen_out.add(d0)
        if len(samples) >= 5: break
    funder_facts = {
      "asof": TODAY.isoformat(),
      "tiles": [
        {"n": f"{mc.get('total_subscribers', len(sub)):,}", "l": "Newsletter subscribers",
         "s": (f"up {yoy_pct}% on {_ym(_prev)}" if yoy_pct else "Mailchimp, current")},
        {"n": f"{len(press):,}", "l": "Press citations",
         "s": f"in {sum(1 for v in p_out.values() if v)} outlets since {_ym(p_first)}; an undercount, since only a fixed set of outlets is watched"},
        {"n": f"{p_out.get('nytimes.com', 0)}", "l": "New York Times citations",
         "s": "no outlet cites Vital City more often"},
      ] + ([{"n": f"{ytd_users:,}", "l": f"Visitors, Jan. 1 to {_md(TODAY)}, {TODAY.year}",
             "s": (f"visits up {ytd_visits_pct}% on the same dates in {TODAY.year-1}" if ytd_visits_pct is not None
                   else "unique visitors, Google Analytics")}] if ytd_users else []) + [
        {"n": f"{gt.get('visitors_30d') or 0:,}", "l": f"Site visitors, {_md(TODAY - timedelta(days=29))} to {_md(TODAY)}",
         "s": (f"{'up' if _v30_pct >= 0 else 'down'} {abs(_v30_pct)}% on the same four weeks of {TODAY.year-1}" if _v30_pct is not None else "the last 30 days, Ghost analytics")},
      ] + ([{"n": f"{_sct['impressions']:,}", "l": "Times shown in Google results, last 28 days",
             "s": f"{_sct.get('clicks') or 0:,} clicks through to the site in those 28 days"}] if _sct.get("impressions") else []) + [
        {"n": f"{len(gov)+len(edu):,}", "l": "Government + university subscribers",
         "s": f"{sum(1 for r in gov if 'nyc.gov' in dom(r)):,} on nyc.gov — City Hall, the courts, the DAs"},
        {"n": f"{len(cat):,}", "l": "Pieces published",
         "s": (f"by {len(authors):,} contributors since 2021, {_roster_n} of them named senior contributors"
               if _roster_n else f"by {len(authors):,} contributors since 2021")},
      ],
      "receipts": [
        {"head": "Zohran Mamdani", "claim": "As a candidate, called himself 'quite taken' by the annual crime analysis, then sat with Vital City for an hour on public safety. He is now the mayor",
         "note": "",
         "links": [{"t":"the interview","u":"https://www.vitalcitynyc.org/zohran-mamdani-talks-public-safety/"},
                   {"t":"'quite taken' (NY Editorial Board)","u":"https://nyeditorialboard.substack.com/p/zohran-mamdani-interview-transcript"},
                   {"t":"the crime analysis","u":"https://www.vitalcitynyc.org/crime-in-new-york-city-trends-statistics/"}]},
        {"head": "City Hall's safety chief", "claim": "The first deputy mayor for community safety is a Vital City contributor who previewed her office's approach in its pages",
         "note": "Renita Francois, December 2025",
         "links": [{"t":"the essay","u":"https://www.vitalcitynyc.org/nstat-should-be-key-to-mamdani-public-safety-plan/"},
                   {"t":"the interview","u":"https://www.vitalcitynyc.org/renita-francois-interview-neighborhood-safety/"}]},
        {"head": "Rikers Island", "claim": "Made the case for a federal receiver; a judge has since appointed one", "note": "",
         "links": [{"t":"the case","u":"https://www.vitalcitynyc.org/the-rikers-receivership-risk-and-opportunity/"},
                   {"t":"the order (THE CITY)","u":"https://www.thecity.nyc/2025/05/13/federal-judge-rikers-oversight-remediation-manager/"},
                   {"t":"the receiver's powers (Queens Eagle)","u":"https://queenseagle.com/all/2025/12/22/judge-details-sweeping-powers-of-receiver-set-to-run-rikers"}]},
        {"head": "Subway safety", "claim": "Recommendations drove New York Times coverage and were adopted in part by the governor and the MTA",
         "note": "",
         "links": [{"t":"the recommendations","u":"https://www.vitalcitynyc.org/what-to-do-about-subway-safety-nyc-policy-recommendations/"},
                   {"t":"NYT, March 2025","u":"https://www.nytimes.com/2025/03/14/nyregion/subway-crime-nyc.html"},
                   {"t":"NYT, September 2025","u":"https://www.nytimes.com/2025/09/10/nyregion/nyc-subway-hochul-white-house.html"},
                   {"t":"the governor's program","u":"https://www.governor.ny.gov/news/safer-subways-one-year-after-deploying-additional-law-enforcement-and-safety-measures-governor"}]},
        {"head": "Permitting", "claim": "Days after publishing fixes for the permitting mess, City Hall released a report echoing them", "note": "",
         "links": [{"t":"the 8 fixes","u":"https://www.vitalcitynyc.org/nyc-housing-permits-fast-track-construction-mamdani/"}]},
        {"head": "Crime data", "claim": "When reporters dig into the city's numbers, it is often Vital City's analyses they build on",
         "note": f"{p_out.get('gothamist.com',0)} Gothamist and {p_out.get('politico.com',0)} Politico citations tracked",
         "links": [{"t":"the annual analysis","u":"https://www.vitalcitynyc.org/crime-in-new-york-city-trends-statistics/"},
                   {"t":"why the numbers change","u":"https://www.vitalcitynyc.org/real-crime-numbers-nyc-nypd/"}]},
        {"head": "Housing", "claim": "The housing issue won the Citizens Housing and Planning Council's Insight Award",
         "note": "for helping guide a conversation in which progressives, liberals, independents and conservatives increasingly agree the city must build",
         "links": [{"t":"the issue","u":"https://www.vitalcitynyc.org/build-big-without-delay/"}]},
        {"head": "Observation decks", "claim": "Months after Vital City argued for free public observation decks, the administration created one",
         "note": "Moses Gates, January 2026",
         "links": [{"t":"the piece","u":"https://www.vitalcitynyc.org/free-observation-decks-new-york/"}]},
      ],
      # Borrowed from the editable donor-deck text (Sept 2026). Quotations are
      # as delivered publicly or in writing, per Vital City's influence record.
      "testimonials": [
        {"q": "The crime nerds' New Yorker.", "who": "Thomas Abt", "role": "founding director, Violence Reduction Center"},
        {"q": "By far the best journal for anyone interested in urban issues.", "who": "Peter Moskos", "role": "criminologist, John Jay College"},
        {"q": "It gives space to sharp, well-articulated arguments backed up with data that can even move a hack like me.", "who": "Patrick Gaspard", "role": "adviser to the mayor, December 2025"},
      ],
      "cited_by_notable": ["The New York Times", "The Atlantic", "The Guardian", "City Journal", "Reason", "Mother Jones", "Last Week Tonight"],
      "republished_by": ["the New York Daily News", "Crain's New York Business", "The City Reporter", "Next City"],
      "oneliner": "Cheap to run, hard to replace and read by the people who make the decisions that shape every part of New York City.",
      # People in Vital City's orbit by role, counted live from the contact
      # database (named people, never estimates).
      "roles": [{"label": lab, "n": sum(1 for r in people if t in (r.get("types") or []))}
                for t, lab in [("VC contributor", "Contributors"), ("journalist", "Journalists"),
                               ("current nyc.gov", "Current city government"), ("nonprofit leadership", "Nonprofit leadership"),
                               ("academic", "Academics"), ("foundation leadership", "Foundation leadership"),
                               ("state gov", "State government"), ("judge", "Judges")]],
      "power_readers": (lambda pr: {"count": pr.get("count"), "pct": pr.get("as_pct_of_list"), "open": pr.get("avg_open_pct")}
                        if pr.get("count") else None)((growth.get("engagement_extras") or {}).get("power_readers") or {}),
      # The senior-contributor roster from the site (senior_contributors.py).
      "senior": {"count": _roster_n, "as_of": _roster.get("as_of", ""),
                 "names": sorted(_roster_names, key=lambda n: n.split()[-1]),
                 "people": _roster.get("people", []),
                 # a handful to name in a sentence; only people still on the live roster
                 "spotlight": [{"n": n, "tag": t} for n, t in SPOTLIGHT if n in _roster_names],
                 "tags": {n: t for n, t in SHORT_TAGS.items() if n in _roster_names},
                 "pool": len(authors)},
      "press": {"total": len(press), "outlets": sum(1 for v in p_out.values() if v), "since": p_first,
                "y2026": sum(1 for x in press if (x.get("published_iso") or "").startswith(str(TODAY.year))),
                "permonth": round(sum(1 for x in press if (x.get("published_iso") or "").startswith(str(TODAY.year))) / max(1, TODAY.month - 0.5), 1),
                "by_year": [{"y": y, "n": n} for y, n in sorted(MENTIONS_ARCHIVE.items()) if y >= "2022"],
                "by_year_asof": MENTIONS_ARCHIVE_ASOF,
                "top": [{"outlet": TOP_OUT[k], "n": v} for k, v in p_out.most_common(30) if k in TOP_OUT][:8],
                "samples": samples},
      "products": [
        {"name": "Just Fix It", "desc": "A standing series pressing specific, doable fixes on City Hall — permitting, government efficiency, a 100-day scorecard.",
         "count": 5, "links": [{"t":"8 permitting fixes","u":"https://www.vitalcitynyc.org/nyc-housing-permits-fast-track-construction-mamdani/"},
                               {"t":"Mamdani's first 100 days","u":"https://www.vitalcitynyc.org/mamdani-first-100-days-scorecard-nyc/"}]},
        {"name": "What To Do (and Not To Do)", "desc": "Policy playbooks that separate what works from what merely sounds tough — subway safety, people in crisis.",
         "count": 2, "links": [{"t":"subway safety","u":"https://www.vitalcitynyc.org/what-to-do-about-subway-safety-nyc-policy-recommendations/"},
                               {"t":"people in crisis","u":"https://www.vitalcitynyc.org/what-to-do-about-people-in-crisis-on-streets-and-subways/"}]},
        {"name": "Rubber Meets Road", "desc": "An eight-piece issue on execution — how the city gets things done, with an interactive map of where darkness and crime overlap.",
         "count": 8, "links": [{"t":"how to get it done","u":"https://www.vitalcitynyc.org/rubber-meets-road-lighting-policy-details/"},
                               {"t":"the darkness-and-crime map","u":"https://www.vitalcitynyc.org/rubber-meets-road-lighting-satellite-crime-map/"}]},
      ],
      "engagement": [
        {"label": "Click-to-open rate", "value": f"{_avg(m26,'ctor_pct')}%", "note": "cross-industry benchmark 5.3-8.6% — the metric Apple's auto-opens cannot inflate"},
        {"label": "Click rate", "value": f"{_avg(m26,'click_pct')}%", "note": "all-industry average 2.27%; media band 3-6%"},
        {"label": "Open rate", "value": f"{_avg(m26,'open_pct')}%", "note": "30-40% is 'solid' for media; inflated industry-wide by Apple Mail"},
      ],
      "audience": [
        {"label": "Email list, full size", "value": f"{mc.get('total_subscribers', len(sub)):,}",
         "note": (f"up {yoy_pct}% year over year" if yoy_pct else "Mailchimp, current")},
        {"label": "Nonprofit addresses", "value": f"{len(org):,}", "note": "Vera, Osborne, Arnold Ventures, CBC, Court Innovation among the densest"},
        {"label": "Staff at grantmaking foundations", "value": "Arnold Ventures, Bloomberg Philanthropies, Robin Hood, Guggenheim, Revson, Tiger, Clark, MacArthur", "note": "counts and names in the warm-doors table"},
        {"label": "Wikipedia-notable subscribers", "value": f"{sum(1 for r in sub if r.get('wiki')):,}", "note": "conservative floor — matched, not estimated"},
      ] + ([{"label": "Government readers open", "value": f"{_mbx['Government'].get('avg_open_pct')}% of sends",
             "note": (f"{_mbx['Government'].get('subs'):,} government addresses" +
                      (f"; academic addresses {_mbx['Academic'].get('avg_open_pct')}%" if _mbx.get("Academic") else ""))}]
            if _mbx.get("Government") and _mbx["Government"].get("avg_open_pct") else []),
      # Who funds Vital City: the "underwritten by" sentence on vitalcitynyc.org/about,
      # read at build time and cached in data/funders_current.json.
      "funders_current": _current_funders(),
      "giving": {"donors": len(donors), "raised": round(total), "median": round(st.median(amts)) if amts else 0,
                 "big_gifts": _big_gifts, "since": GIVING_OPENED},
      # The most-read piece of the current year, for the one-pager.
      "mostread": ({"title": _top26.get("title"), "url": "https://www.vitalcitynyc.org" + (_top26.get("path") or ""),
                    "visitors": _top26.get("visitors"), "year": TODAY.year} if _top26.get("visitors") else None),
      # One sourced line for a pull quote. Wording matches the impact receipt.
      "quote": {"text": "As a candidate, Zohran Mamdani called himself 'quite taken' by Vital City's annual crime analysis, then sat with Vital City for an hour on public safety. He is now the mayor.",
                "source": "The New York Editorial Board, Feb. 2025; Columbia Journalism School forum, Sept. 2025",
                "url": "https://nyeditorialboard.substack.com/p/zohran-mamdani-interview-transcript"},
      "seniors": (lambda au: {
          "people": [
            {"n": disp,
             "who": bio_descriptor((au.get(cn) or {}).get("bio")) if cn else "",
             "npieces": len([p for p in cat if cn and (cn in (p.get("authors") or []) or p.get("primary_author") == cn)]),
             "feat": 1 if disp in FEATURED else 0,
             "roster": 1 if disp in _roster_names else 0,
             "pieces": [{"t": p["title"], "u": p["url"]} for p in sorted(
                 [p for p in cat if cn and (cn in (p.get("authors") or []) or p.get("primary_author") == cn)],
                 key=lambda p: p.get("published_date") or "", reverse=True)[:1]]}
            for disp, cn in _live_seniors(au)],
          "count": len(_live_seniors(au)),
          "roster_count": _roster_n,
          "pool": len(authors),
          "pieces_total": sum(len([p for p in cat if cn and (cn in (p.get("authors") or []) or p.get("primary_author") == cn)])
                              for _, cn in _live_seniors(au)),
      })({x["name"]: x for x in (load(ROOT / "data" / "authors.json") or [])}),
      "longview": {
        # list size at each year end (current year = latest month available)
        "list": [{"y": y, "n": v} for y, v in sorted({
            m[:4]: c for m, c in sorted(_cum.items()) if c
        }.items()) if y >= "2022"],
        # unique visitors per year, from GA4 (property history starts 2023 —
        # there is no web-traffic data for 2021-22, and that absence is stated
        # on the slide rather than papered over)
        "visitors": [{"y": str(r["year"]), "n": r["users"]}
                     for r in ((growth.get("ga4") or {}).get("by_year") or {}).get("years", [])
                     if r.get("users")],
        "visitors_alltime": (((growth.get("ga4") or {}).get("by_year") or {}).get("alltime") or {}).get("users"),
        "pieces": [{"y": y, "n": n} for y, n in sorted(Counter(
            (p.get("published_date") or "")[:4] for p in cat
            if (p.get("published_date") or "")[:4].isdigit()).items()) if y >= "2022"],
      },
      "benchmark_note": ("Impact items are Vital City's own accounting, from the draft positioning language (Aug 2026) — "
                         "reuse the wording, but keep the causal framing as stated. "
                         "Press counts come from the growth dashboard's whitelist of ~25 outlets, so they undercount. "
                         "Benchmarks: Letterhead/ClickMinded/Brevo 2026 email compilations; publisher traffic decline "
                         "from Chartbeat data via Press Gazette. Third-party aggregates — bands, not lines. "
                         "Giving figures are deliberately absent here: audience evidence only, and internal gift data is Donorbox-only."),
    }

    out = {
        "generated_at": TODAY.isoformat(),
        "topline": topline,
        "funders": funders_out,
        "beats": beats,
        "funder_facts": funder_facts,
        "variants": (lambda: {
            k: {"label": v["label"], "spot_title": v["spot_title"],
                "receipts": v["receipts"], "authors": v["authors"], "products": v["products"],
                "spots": [{"t": p["title"], "u": p["url"],
                           "a": p.get("primary_author") or ", ".join((p.get("authors") or [])[:2])}
                          for slug in v["spots"]
                          for p in [next((x for x in cat if (x.get("url") or "").rstrip("/").endswith("/"+slug)), None)]
                          if p or print(f"  WARNING variant {k}: slug not found: {slug}")]
                + (v.get("extra_spots") or [])}
            for k, v in VARIANTS.items()})(),
        "pipeline": PIPELINE,
        "readiness": {
            "sponsor": "Fund for the City of New York (FCNY)",
            "ein": "13-2612524", "status": "501(c)(3) via fiscal sponsorship",
            "note": "Grant applications and checks route through the fiscal sponsor. "
                    "Confirm current sponsorship terms with FCNY before quoting them to a funder."},
        "tiers": {
            "advisors": advisors, "upgrade": upgrade, "second": second,
            "researched": researched,
            "notables": notables, "principals": principals,
            "foundation_staff": fstaff, "lybunt": lybunt,
            "party": (event or {}).get("attended", [])},
        "event": event,
        "counts": {"subscribers": len(sub), "donors": len(donors)},
    }
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1))

    # ---------------- encrypt, same blob format as the other tools ----------
    passphrase = os.environ.get("VC_NETWORK_PASS") or \
        (ROOT / "private" / ".netpass").read_text().strip()
    salt, iv = secrets.token_bytes(16), secrets.token_bytes(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=ITERS).derive(passphrase.encode())
    ct = AESGCM(key).encrypt(iv, json.dumps(out).encode(), None)
    ENC.parent.mkdir(exist_ok=True)
    ENC.write_text(json.dumps({
        "v": 1, "kdf": "PBKDF2-SHA256", "iters": ITERS,
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ct": base64.b64encode(ct).decode()}))

    print(f"prospects: {len(sub):,} subscribers, {len(donors)} donors -> "
          f"advisors {len(advisors)}, upgrade {len(upgrade)}, second-gift {len(second)}, "
          f"notables {len(notables)}, principals {len(principals)}, "
          f"foundation staff {len(fstaff)}, lapsed {len(lybunt)}")
    print(f"wrote {OUT.name} and {ENC}")


if __name__ == "__main__":
    main()
