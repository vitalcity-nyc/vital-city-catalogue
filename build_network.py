#!/usr/bin/env python3
"""Build the unified, deconflicted PEOPLE dataset for the network explorer.

Fuses three sources, matching the same person across them by email (primary)
and name (fallback):
  - private/members_source.csv      Ghost members/subscribers (~10.9k)
  - private/contacts_source.xlsx    contact CRM, sheet "combined" (~1.25k, typed)
  - data/authors.json + catalogue   Vital City authors (article counts)

Writes (gitignored — sensitive):
  private/people.json         one record per person, deconflicted
  private/network_stats.json  headline counts + type x membership matrix

The plaintext never ships; encrypt_people.py produces the public encrypted blob.
"""
import csv, json, re, unicodedata
from pathlib import Path
import openpyxl

ROOT = Path(__file__).resolve().parent
PRIV = ROOT / "private"
PERSON_CATS = ["VC contributor", "VC advisor", "journalist", "academic",
               "foundation leadership", "nonprofit leadership", "city gov",
               "current nyc.gov", "state gov", "fed gov", "judge", "architect"]
# Domain-area interests (specialties).
TOPIC_CATS = ["criminal justice", "housing", "transit", "budget", "urban planning",
              "education", "public health", "economy", "technology",
              "politics & government", "race & equity", "culture"]
NONPERSON = {"vital city", "a survey", "a photo essay", "a conversation",
             "the editors", "editorial board", "vital city staff", "various"}

# Map an article's public topic tag (lowercased) -> a specialty domain. Used to
# infer a person's domain interests from what they've written for us.
TOPIC_MAP = {
    "crime": "criminal justice", "justice": "criminal justice",
    "police reform": "criminal justice", "policing": "criminal justice",
    "jails": "criminal justice", "incarceration": "criminal justice",
    "gun violence": "criminal justice", "subway crime": "criminal justice",
    "drugs": "criminal justice",
    "housing": "housing", "homelessness": "housing",
    "transit": "transit", "transportation": "transit",
    "budget": "budget",
    "city planning": "urban planning", "neighborhood life": "urban planning",
    "quality of life": "urban planning", "infrastructure": "urban planning",
    "education": "education",
    "public health": "public health", "mental health": "public health",
    "economics": "economy", "inequality": "economy",
    "technology": "technology",
    "politics": "politics & government", "city government": "politics & government",
    "government operations": "politics & government", "corruption": "politics & government",
    "race": "race & equity",
    "culture": "culture", "history": "culture",
}

# Email domains that indicate the person works in journalism/media.
MEDIA_DOMAINS = {
    "nytimes.com", "wsj.com", "washingtonpost.com", "theatlantic.com", "newyorker.com",
    "nymag.com", "vox.com", "axios.com", "politico.com", "bloomberg.net", "reuters.com",
    "apnews.com", "npr.org", "wnyc.org", "gothamist.com", "thecity.nyc", "hellgatenyc.com",
    "nydailynews.com", "nypost.com", "amny.com", "cityandstateny.com", "citylimits.org",
    "documentedny.com", "themarshallproject.org", "propublica.org", "chalkbeat.org",
    "the74million.org", "brooklyneagle.com", "gothamgazette.com", "thenation.com",
    "motherjones.com", "slate.com", "theguardian.com", "cnn.com", "cbsnews.com",
    "ny1.com", "pix11.com", "news12.com", "abc.com", "nbcuni.com", "spectrumnews.org",
    "epicenter-nyc.com", "thecity.org", "qns.com", "observer.com", "crainsnewyork.com",
}


# Curated email-domain -> institution names (high confidence).
INST_DOMAINS = {
    "nytimes.com": "The New York Times", "wsj.com": "The Wall Street Journal",
    "washingtonpost.com": "The Washington Post", "theatlantic.com": "The Atlantic",
    "newyorker.com": "The New Yorker", "nymag.com": "New York Magazine",
    "politico.com": "POLITICO", "vox.com": "Vox", "axios.com": "Axios",
    "bloomberg.net": "Bloomberg", "bloomberg.org": "Bloomberg Philanthropies",
    "reuters.com": "Reuters", "apnews.com": "Associated Press", "npr.org": "NPR",
    "wnyc.org": "WNYC", "gothamist.com": "Gothamist", "thecity.nyc": "THE CITY",
    "hellgatenyc.com": "Hell Gate", "nydailynews.com": "New York Daily News",
    "nypost.com": "New York Post", "amny.com": "amNewYork", "cityandstateny.com": "City & State",
    "citylimits.org": "City Limits", "documentedny.com": "Documented",
    "themarshallproject.org": "The Marshall Project", "propublica.org": "ProPublica",
    "chalkbeat.org": "Chalkbeat", "thenation.com": "The Nation", "crainsnewyork.com": "Crain's New York",
    "ny1.com": "NY1", "cbsnews.com": "CBS News", "abc.com": "ABC News",
    "allrise.org": "All Rise", "counciloncj.org": "Council on Criminal Justice",
    "rand.org": "RAND Corporation", "manhattan-institute.org": "Manhattan Institute",
    "vera.org": "Vera Institute of Justice", "urban.org": "Urban Institute",
    "brookings.edu": "Brookings Institution", "cbcny.org": "Citizens Budget Commission",
}
WEBMAIL = {"gmail.com","googlemail.com","yahoo.com","ymail.com","hotmail.com","outlook.com",
 "live.com","msn.com","aol.com","icloud.com","me.com","mac.com","proton.me","protonmail.com",
 "pm.me","gmx.com","fastmail.com","comcast.net","verizon.net","att.net","sbcglobal.net",
 "optimum.net","rcn.com","earthlink.net","mindspring.com","nyc.rr.com","mail.com","ms.com","aim.com"}


def infer_institution(emails):
    """Best-guess institution from an email domain. Curated map first, then
    nyc.gov/.gov/.edu, then hyphenated org domains. Webmail → no guess."""
    for e in emails:
        dom = e.split("@")[-1].strip().lower()
        if dom in INST_DOMAINS:
            return INST_DOMAINS[dom]
        if dom == "nyc.gov" or dom.endswith(".nyc.gov"):
            return "New York City government"
        if dom in WEBMAIL:
            continue
        if dom.endswith(".edu"):
            root = dom[:-4].split(".")[-1]
            return root.upper() if len(root) <= 4 else root.capitalize()
        if dom.endswith(".gov"):
            root = dom[:-4].split(".")[-1]
            return root.upper() if len(root) <= 5 else root.capitalize()
        sld = dom.split(".")[0]
        if "-" in sld and len(sld) >= 5:                    # e.g. court-innovation -> Court Innovation
            return " ".join(w.capitalize() for w in sld.split("-"))
    return ""


# Latin letters that NFKD+ascii would silently DROP (so Synøve != Synove). Map
# them to their conventional ASCII spelling before stripping accents.
TRANSLIT = {"ø": "o", "Ø": "o", "æ": "ae", "Æ": "ae", "å": "a", "Å": "a",
            "ß": "ss", "ð": "d", "Ð": "d", "þ": "th", "Þ": "th", "ł": "l",
            "Ł": "l", "đ": "d", "Đ": "d", "ı": "i", "œ": "oe", "Œ": "oe"}
_TRANSLIT = str.maketrans(TRANSLIT)


def norm(s):
    if not s: return ""
    s = str(s).translate(_TRANSLIT)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().strip()
    s = re.sub(r"\b(dr|mr|mrs|ms|prof|jr|sr|phd|md|esq)\b\.?", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def firstlast(s):
    t = norm(s).split()
    return f"{t[0]} {t[-1]}" if len(t) >= 2 else (t[0] if t else "")

# Common first-name nicknames -> formal form, so "Jeff Asher" the author matches
# "Jeffrey Asher" the subscriber. Bidirectional via canonicalization.
NICKNAMES = {
    "jeff": "jeffrey", "geoff": "geoffrey", "ben": "benjamin", "benji": "benjamin",
    "mike": "michael", "mick": "michael", "chris": "christopher", "dave": "david",
    "dan": "daniel", "danny": "daniel", "tom": "thomas", "tommy": "thomas",
    "rob": "robert", "bob": "robert", "bobby": "robert", "rich": "richard",
    "rick": "richard", "dick": "richard", "jim": "james", "jimmy": "james",
    "bill": "william", "will": "william", "billy": "william", "steve": "stephen",
    "matt": "matthew", "nick": "nicholas", "tony": "anthony", "alex": "alexander",
    "sam": "samuel", "greg": "gregory", "joe": "joseph", "ed": "edward",
    "ted": "edward", "andy": "andrew", "drew": "andrew", "ken": "kenneth",
    "ron": "ronald", "pat": "patrick", "cathy": "catherine", "kate": "katherine",
    "katie": "katherine", "kathy": "katherine", "liz": "elizabeth", "beth": "elizabeth",
    "betsy": "elizabeth", "sue": "susan", "jen": "jennifer", "jenny": "jennifer",
    "becky": "rebecca", "meg": "margaret", "peggy": "margaret", "abby": "abigail",
    "josh": "joshua", "zach": "zachary", "nate": "nathaniel", "gabe": "gabriel",
    "fred": "frederick", "ray": "raymond", "vince": "vincent", "cy": "cyrus",
}


def email_norm(e):
    return (e or "").strip().lower()

GENERIC = {"info","contact","hello","admin","office","press","news","mail","email","team",
 "support","editor","editors","subscriptions","membership","members","help","newsletter",
 "comms","media","outreach","development","dev","marketing","desk","general","inquiries"}

# Webmail / ISP domains — their domain root is NOT a person's surname.
PROVIDERS = {"gmail","googlemail","yahoo","ymail","rocketmail","hotmail","outlook","live",
 "msn","aol","icloud","me","mac","proton","protonmail","pm","gmx","fastmail","hey","duck",
 "comcast","verizon","att","sbcglobal","optimum","rcn","earthlink","mindspring","zoho",
 "mail","email","ms","cloud","inbox","aim"}


def name_from_email(e):
    """Best-guess display name from an email address. These are educated guesses
    (shown in gray in the UI), never treated as authoritative.
      jane.doe@x.com   -> Jane Doe        (split local part)
      aaron@naparstek.com -> Aaron Naparstek  (personal-domain surname)
      jsmith@gmail.com -> Jsmith           (single token, provider domain)
    Returns "" when nothing reasonable can be derived.
    """
    e = email_norm(e)
    if "@" not in e:
        return ""
    local, domain = e.split("@", 1)
    local = local.split("+")[0]
    parts = [re.sub(r"[^a-z]", "", p) for p in re.split(r"[._\-]+", local)]
    parts = [p for p in parts if p and p not in GENERIC and len(p) >= 2]
    if len(parts) >= 2:
        return f"{parts[0].capitalize()} {parts[1].capitalize()}"
    if len(parts) == 1:
        first = parts[0]
        droot = domain.split(".")[0]
        tld = domain.rsplit(".", 1)[-1]
        # Personal/vanity domain → use the domain root as a likely surname.
        if (3 <= len(first) <= 11 and droot not in PROVIDERS and droot != first
                and re.fullmatch(r"[a-z]{4,12}", droot)
                and tld in ("com", "net", "co", "io")
                and not domain.endswith((".edu", ".gov"))):
            return f"{first.capitalize()} {droot.capitalize()}"
        return first.capitalize()
    return ""


def prettify_name(name):
    """Capitalize names entered all-lowercase or ALL-CAPS (deirdre hamill ->
    Deirdre Hamill; SAM SCHWARTZ -> Sam Schwartz). Names already in mixed case
    are assumed intentional (VanNostrand, McDonnell, DeFabbia-Kane) and kept."""
    if not name or not any(c.isalpha() for c in name):
        return name
    if name != name.lower() and name != name.upper():
        return name
    return re.sub(r"[A-Za-z]+", lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(), name)


def clean_name(name):
    """Strip leading/trailing junk (asterisks, stray punctuation, symbols) while
    keeping letters, digits, periods, parens, hyphens and apostrophes."""
    if not name:
        return name
    junk = r"[\s\*\|/\\_#~:;,\"'<>\[\]{}!?@^&+=]"
    name = re.sub(r"^" + junk + r"+", "", name)
    name = re.sub(junk + r"+$", "", name)
    return name.strip()


def primary_email(emails):
    """First real address; a made-up @vitalcitynyc.org one is last resort."""
    reals = [e for e in emails if not e.endswith("vitalcitynyc.org")]
    return reals[0] if reals else (emails[0] if emails else "")


def set_email(p, email):
    """Add an email to the person's list (a person can have several) and keep
    `e` as the primary, preferring a real address over a @vitalcitynyc.org one."""
    email = email_norm(email)
    if not email:
        return
    if email not in p["emails"]:
        p["emails"].append(email)
    p["e"] = primary_email(p["emails"])


def set_name(p, name, given):
    """Set a person's display name, tracking whether it's authoritative ('given')
    or an email guess ('guess'). A given name upgrades a previous guess."""
    name = prettify_name(clean_name(name))
    if not name:
        return
    if not p["n"]:
        p["n"], p["ns"] = name, "given" if given else "guess"
    elif given and p.get("ns") == "guess":
        p["n"], p["ns"] = name, "given"


def _set(v):
    """A category column counts as 'set' for any truthy, non-empty value
    (1, x, yes, TRUE, etc.) — tolerant of however the team marks the sheet."""
    if v is None:
        return False
    s = str(v).strip().lower()
    return s not in ("", "0", "no", "false", "n", "-")


def _crm_rows():
    """Yield (header_list, row_list) from the contacts source. Prefers a CSV
    export of the maintained Google Sheet (private/contacts_source.csv); falls
    back to the original Excel agglomeration (sheet 'combined')."""
    csv_path = PRIV / "contacts_source.csv"
    if csv_path.exists():
        with open(csv_path, newline="") as f:
            rows = list(csv.reader(f))
        return rows[0], rows[1:]
    wb = openpyxl.load_workbook(PRIV / "contacts_source.xlsx", read_only=True, data_only=True)
    ws = wb["combined"]
    rows = list(ws.iter_rows(values_only=True))
    return [str(c) for c in rows[0]], rows[1:]


def load_crm():
    hdr, rows = _crm_rows()
    idx = {str(h).strip(): i for i, h in enumerate(hdr)}
    def cell(r, h):
        i = idx.get(h)
        return r[i] if (i is not None and i < len(r)) else None
    def cats_from(r, allowed):
        """Categories for a row, from either per-category boolean columns OR a
        single 'categories'/'specialties' column with a ;- or ,-separated list."""
        found = {c for c in allowed if _set(cell(r, c))}
        for col in ("categories", "specialties", "specialty", "type", "types"):
            v = cell(r, col)
            if v:
                wanted = {x.strip().lower() for x in re.split(r"[;,]", str(v)) if x.strip()}
                found |= {c for c in allowed if c.lower() in wanted}
        return sorted(found)

    out = []
    for r in rows:
        if not r or not r[0]:
            continue
        out.append({
            "name": str(r[0]).strip(),
            "email": email_norm(cell(r, "email")),
            "institution": (cell(r, "institution") or "").strip(),
            "role": (cell(r, "role") or "").strip(),
            "types": cats_from(r, PERSON_CATS),
            "topics": cats_from(r, TOPIC_CATS),
        })
    return out


def fold(q, p):
    """Fold person p into person q (q is kept). Combines flags, sums giving,
    unions categories/emails, prefers a real email and a confirmed name."""
    q["mem"], q["auth"], q["don"] = q["mem"] or p["mem"], q["auth"] or p["auth"], q["don"] or p["don"]
    q["unsub"] = q["unsub"] or p["unsub"]
    q["arts"] = max(q["arts"], p["arts"])
    q["damt"] = round(q["damt"] + p["damt"], 2)
    q["dcnt"] += p["dcnt"]
    q["d7"] = round(q["d7"] + p["d7"], 2); q["d7c"] += p["d7c"]
    q["d30"] = round(q["d30"] + p["d30"], 2); q["d30c"] += p["d30c"]
    if p["udate"] > q["udate"]: q["udate"] = p["udate"]
    q["types"] = sorted(set(q["types"]) | set(p["types"]))
    q["topics"] = sorted(set(q["topics"]) | set(p["topics"]))
    q["src"] = sorted(set(q["src"]) | set(p["src"]))
    for e in p["emails"]:
        if e not in q["emails"]:
            q["emails"].append(e)
    q["e"] = primary_email(q["emails"])
    if p["inst"] and not q["inst"]: q["inst"] = p["inst"]
    if p["role"] and not q["role"]: q["role"] = p["role"]
    if p.get("aname") and not q.get("aname"): q["aname"] = p["aname"]
    if p["since"] and (not q["since"] or p["since"] < q["since"]): q["since"] = p["since"]
    if p["dlast"] > q["dlast"]: q["dlast"] = p["dlast"]
    if q["ns"] == "guess" and p["ns"] == "given": q["n"], q["ns"] = p["n"], "given"


def merge_key(name, nick=False):
    """first|last merge key from a name: drops middle names/initials and (with
    nick=True) maps nicknames to a formal form. norm() already transliterates
    accents, so 'Synøve N. Andersen' and 'Synove Andersen' share a key."""
    parts = norm(name).split()
    if len(parts) < 2:                 # single tokens never merge by name
        return None
    first, last = parts[0], parts[-1]
    if nick:
        first = NICKNAMES.get(first, first)
    return first + "|" + last


def known(p):
    """A 'known' person carries identity beyond a bare subscription — a category,
    authorship or a gift. Used to gate the looser nickname merge so two unrelated
    subscribers ('Dan Lee'/'Daniel Lee') are never fused."""
    return bool(p["auth"] or p["don"] or p["types"])


def merge_people(people):
    """Consolidate the same person split across sources/emails. Two passes:
      1. exact key (first|last, accent- and middle-name-insensitive) — always.
      2. nickname key (Dan->Daniel) — only when at least one side is 'known',
         to keep namesake risk low.
    Within a pass, only merge when exactly one prior record shares the key."""
    def run(rows, keyfn, guard):
        seen, out = {}, []
        for p in rows:
            k = keyfn(p["n"])
            if k and k in seen and (guard is None or guard(seen[k], p)):
                fold(seen[k], p)
            else:
                if k:
                    seen.setdefault(k, p)
                out.append(p)
        return out
    people = run(people, lambda n: merge_key(n, nick=False), None)
    people = run(people, lambda n: merge_key(n, nick=True), lambda a, b: known(a) or known(b))
    return people


def load_author_file():
    """Authoritative contributor roster (Google Contacts export). Returns
    [{name, email}] using the first non-@vitalcitynyc.org email as the real one."""
    path = PRIV / "vc_authors.csv"
    if not path.exists():
        return []
    out = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            parts = [(r.get(c) or "").strip() for c in ("First Name", "Middle Name", "Last Name", "Name Suffix")]
            name = " ".join(p for p in parts if p).strip()
            if not name:
                continue
            emails = []
            for col in ("E-mail 1 - Value", "E-mail 2 - Value", "E-mail 3 - Value"):
                for e in re.split(r":::|,", r.get(col) or ""):
                    e = e.strip().lower()
                    if "@" in e and not e.endswith("vitalcitynyc.org") and e not in emails:
                        emails.append(e)
            out.append({"name": name, "emails": emails})
    return out


def main():
    # ---- index helpers ----
    people = []                 # list of person dicts
    by_email = {}               # email -> person
    by_name = {}                # norm name -> person
    by_fl = {}                  # first+last -> person

    def get_or_make(emails=None, name="", fl=""):
        for e in (emails or []):
            if e and e in by_email: return by_email[e]
        if name and name in by_name: return by_name[name]
        if fl and fl in by_fl: return by_fl[fl]
        p = {"n": "", "ns": "", "e": "", "emails": [], "inst": "", "role": "",
             "types": [], "topics": [], "mem": 0, "since": "", "auth": 0, "arts": 0,
             "aname": "", "don": 0, "damt": 0.0, "dcnt": 0, "dlast": "", "unsub": 0, "udate": "",
             "d7": 0.0, "d7c": 0, "d30": 0.0, "d30c": 0, "src": []}
        people.append(p)
        return p

    def index(p):
        for e in p["emails"]:
            by_email.setdefault(e, p)
        nn = norm(p.get("n"))
        if nn and len(nn.split()) >= 2:        # only first+last names are merge keys; single tokens match by email only
            by_name.setdefault(nn, p)
            by_fl.setdefault(firstlast(p["n"]), p)

    # ---- 1. Members (the spine) ----
    members_total = 0
    with open(PRIV / "members_source.csv", newline="") as f:
        for row in csv.DictReader(f):
            members_total += 1
            email = email_norm(row.get("email"))
            recorded = (row.get("name") or "").strip()
            guess = name_from_email(email) if not recorded else ""
            p = get_or_make(emails=[email], name=norm(recorded or guess))
            p["mem"] = 1
            p["src"].append("member")
            set_email(p, email)
            set_name(p, recorded, True) if recorded else set_name(p, guess, False)
            since = (row.get("created_at") or "")[:10]
            if since and not p["since"]: p["since"] = since
            index(p)

    # (Subscribers come from Ghost only — Mailchimp subscribed list intentionally
    #  not used; the two drift and mixing them caused confusion.)

    # ---- 2. CRM contacts (types) ----
    crm = load_crm()
    crm_total = len(crm)
    for c in crm:
        p = get_or_make(emails=[c["email"]], name=norm(c["name"]), fl=firstlast(c["name"]))
        set_name(p, c["name"], True)
        set_email(p, c["email"])
        if c["institution"] and not p["inst"]: p["inst"] = c["institution"]
        if c["role"] and not p["role"]: p["role"] = c["role"]
        p["types"] = sorted(set(p["types"]) | set(c["types"]))
        p["topics"] = sorted(set(p["topics"]) | set(c["topics"]))
        if "crm" not in p["src"]: p["src"].append("crm")
        index(p)

    # ---- 3. Authors (article counts + specialties inferred from their pieces) ----
    authors = json.loads((ROOT / "data" / "authors.json").read_text())
    try:
        catalogue = json.loads((ROOT / "data" / "catalogue.json").read_text())
    except Exception:
        catalogue = []
    author_specs = {}   # norm author name -> set of specialty domains they've written about
    for art in catalogue:
        specs = {TOPIC_MAP[t.lower()] for t in art.get("topics", []) if t.lower() in TOPIC_MAP}
        if specs:
            for au in art.get("authors", []):
                author_specs.setdefault(norm(au), set()).update(specs)
    authors_total = 0
    for a in authors:
        nn = norm(a["name"])
        if not nn or nn in NONPERSON: continue
        authors_total += 1
        p = get_or_make(name=nn, fl=firstlast(a["name"]))
        set_name(p, a["name"], True)
        p["auth"] = 1
        p["arts"] = a.get("post_count", 0)
        if not p.get("aname"): p["aname"] = a["name"]   # exact catalogue byline, for deep-linking
        p["types"] = sorted(set(p["types"]) | {"VC contributor"})   # anyone who wrote for us is a contributor
        p["topics"] = sorted(set(p["topics"]) | author_specs.get(nn, set()))
        if "author" not in p["src"]: p["src"].append("author")
        index(p)

    # ---- 3b. Authoritative contributor roster (real emails) ----
    roster = load_author_file()
    for a in roster:
        nn = norm(a["name"])
        if not nn or nn in NONPERSON:
            continue
        p = get_or_make(emails=a["emails"], name=nn, fl=firstlast(a["name"]))
        set_name(p, a["name"], True)
        for e in a["emails"]:
            set_email(p, e)
        p["types"] = sorted(set(p["types"]) | {"VC contributor"})
        p["auth"] = 1
        p["topics"] = sorted(set(p["topics"]) | author_specs.get(nn, set()))
        if "author" not in p["src"]: p["src"].append("author")
        index(p)

    # ---- 4. Donors (FCNY giving) ----
    donors_path = PRIV / "donors_source.csv"
    donors_total = 0
    if donors_path.exists():
        with open(donors_path, newline="") as f:
            for row in csv.DictReader(f):
                email = email_norm(row.get("Email"))
                fname = (row.get("First Name") or "").strip()
                lname = (row.get("Last Name") or "").strip()
                name = f"{fname} {lname}".strip()
                if not email and not name:
                    continue
                donors_total += 1
                try:
                    amt = float(row.get("Summed Donation Amount") or 0)
                except ValueError:
                    amt = 0.0
                try:
                    cnt = int(float(row.get("Donations Count") or 0))
                except ValueError:
                    cnt = 0
                p = get_or_make(emails=[email], name=norm(name), fl=firstlast(name))
                set_name(p, name, True)
                set_email(p, email)
                p["don"] = 1
                p["damt"] = round(p["damt"] + amt, 2)
                p["dcnt"] += cnt
                # recent-window giving (for the activity bar)
                try: p["d7"] = round(p["d7"] + float(row.get("Amount 7d") or 0), 2)
                except ValueError: pass
                try: p["d7c"] += int(float(row.get("Count 7d") or 0))
                except ValueError: pass
                try: p["d30"] = round(p["d30"] + float(row.get("Amount 30d") or 0), 2)
                except ValueError: pass
                try: p["d30c"] += int(float(row.get("Count 30d") or 0))
                except ValueError: pass
                # most-recent gift date (M/D/YYYY ... -> YYYY-MM-DD), keep the latest
                ld = (row.get("Last Donation at") or "").strip()
                m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", ld)
                if m:
                    iso = f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
                    if iso > p["dlast"]:
                        p["dlast"] = iso
                if "donor" not in p["src"]:
                    p["src"].append("donor")
                index(p)

    # ---- 4c. Unsubscribed (Mailchimp export) — former newsletter contacts ----
    unsub_path = PRIV / "unsubscribed_source.csv"
    if unsub_path.exists():
        with open(unsub_path, newline="") as f:
            for row in csv.DictReader(f):
                email = email_norm(row.get("Email"))
                if not email:
                    continue
                fn, ln = (row.get("First Name") or "").strip(), (row.get("Last Name") or "").strip()
                name = f"{fn} {ln}".strip()
                p = get_or_make(emails=[email], name=norm(name), fl=firstlast(name))
                set_email(p, email)
                if name:
                    set_name(p, name, True)
                p["unsub"] = 1
                ud = (row.get("Unsub Date") or "").strip()[:10]
                if ud > p["udate"]:
                    p["udate"] = ud
                if "unsub" not in p["src"]:
                    p["src"].append("unsub")
                index(p)

    # ---- 6. Infer categories from email domain ----
    #   journalist     -> known news-outlet domains
    #   current nyc.gov -> an active NYC city email (anything ending nyc.gov)
    media_inferred = nycgov_inferred = 0
    for p in people:
        if not p["e"]:
            continue
        dom = p["e"].split("@")[-1].strip().lower()
        if dom in MEDIA_DOMAINS and "journalist" not in p["types"]:
            p["types"] = sorted(set(p["types"]) | {"journalist"})
            media_inferred += 1
        if (dom == "nyc.gov" or dom.endswith(".nyc.gov")) and "current nyc.gov" not in p["types"]:
            p["types"] = sorted(set(p["types"]) | {"current nyc.gov"})
            nycgov_inferred += 1

    # Finalize emails + institution:
    #  - drop made-up @vitalcitynyc.org emails from authors/contributors,
    #  - recompute the primary email,
    #  - infer institution from an email domain where it's blank.
    for p in people:
        if p["auth"] or "VC contributor" in p["types"]:
            p["emails"] = [e for e in p["emails"] if not e.endswith("vitalcitynyc.org")]
        p["e"] = primary_email(p["emails"])
        if not p["inst"]:
            inst = infer_institution(p["emails"])
            if inst:
                p["inst"] = inst

    # ---- Force VC-contributor tag for emails in extra_contributors.csv ----
    # (catches contributors whose byline name didn't match a catalogue author,
    #  e.g. nickname variants like Bill vs William Bratton)
    extra = PRIV / "extra_contributors.csv"
    if extra.exists():
        with open(extra, newline="") as f:
            for row in csv.DictReader(f):
                em = email_norm(row.get("email"))
                if em and em in by_email:
                    by_email[em]["types"] = sorted(set(by_email[em]["types"]) | {"VC contributor"})

    # ---- 5. Manual name fixes (email -> corrected name) ----
    # Edits made in the explorer's edit mode are exported here and become
    # permanent for everyone on the next publish.
    overrides_path = PRIV / "name_overrides.csv"
    overrides_applied = 0
    if overrides_path.exists():
        with open(overrides_path, newline="") as f:
            for row in csv.DictReader(f):
                em = email_norm(row.get("email"))
                fixed = (row.get("name") or "").strip()
                if em and fixed and em in by_email:
                    by_email[em]["n"] = fixed
                    by_email[em]["ns"] = "given"
                    overrides_applied += 1

    # ---- consolidate duplicates: exact key, then nickname key (last) ----
    # Catches accents/middle initials (Synøve N. Andersen == Synove Andersen),
    # nicknames (Dan Garodnick == Daniel Garodnick) and contributors who
    # subscribed under a name variant (Jeff Asher == Jeffrey Asher).
    before = len(people)
    people = merge_people(people)
    print(f"merged {before - len(people)} duplicate-name records", file=__import__("sys").stderr)

    for p in people:                 # unsubscribed wins: a former contact is not a current subscriber
        if p["unsub"]:
            p["mem"] = 0

    # ---- apply exported in-tool edits (every-field) permanently ----
    # private/people_overrides.json: {personKey: {n, inst, emails, types, topics}}
    # personKey = primary email, else "name:<lowercased name>" (matches the UI).
    ov_path = PRIV / "people_overrides.json"
    if ov_path.exists():
        try:
            ov = json.loads(ov_path.read_text())
        except Exception:
            ov = {}
        deleted_keys = 0
        matched = set()
        for p in people:
            k = p["e"] if p["e"] in ov else ("name:" + (p["n"] or "").lower())
            o = ov.get(k)
            if not isinstance(o, dict):
                continue
            matched.add(k)
            if o.get("deleted"):
                p["_deleted"] = True       # extraneous entry removed in the tool
                deleted_keys += 1
                continue
            if o.get("n"):
                p["n"], p["ns"] = o["n"], "given"
            if "inst" in o:
                p["inst"] = o["inst"]
            if o.get("emails"):
                p["emails"] = o["emails"]
                p["e"] = primary_email(o["emails"])
            if o.get("types") is not None:
                p["types"] = o["types"]
            if o.get("topics") is not None:
                p["topics"] = o["topics"]
        people = [p for p in people if not p.get("_deleted")]
        if deleted_keys:
            print(f"removed {deleted_keys} entries flagged deleted in people_overrides.json", file=__import__("sys").stderr)

        # Manually-added people (add:true overrides that matched no existing record).
        added = 0
        for k, o in ov.items():
            if k in matched or not isinstance(o, dict) or not o.get("add") or o.get("deleted"):
                continue
            emails = [email_norm(e) for e in (o.get("emails") or []) if email_norm(e)]
            name = (o.get("n") or "").strip()
            if not name and not emails:
                continue
            people.append({
                "n": name, "ns": "given", "e": primary_email(emails), "emails": emails,
                "inst": o.get("inst") or "", "role": "",
                "types": list(o.get("types") or []), "topics": list(o.get("topics") or []),
                "mem": 1 if o.get("mem") else 0, "since": "",
                "auth": 1 if o.get("auth") else 0, "arts": 0,
                "aname": name if o.get("auth") else "",
                "don": 1 if o.get("don") else 0, "damt": float(o.get("damt") or 0),
                "dcnt": 1 if o.get("don") else 0, "dlast": "", "unsub": 0,
                "src": ["manual"], "added": True,
            })
            added += 1
        if added:
            print(f"added {added} manually-entered people from people_overrides.json", file=__import__("sys").stderr)

    # ---- drop people with no way to act on them ----
    # No email AND not a subscriber, author, donor or unsubscribed = just a name
    # in the contacts sheet (e.g. an official with no email). Not useful here.
    def keep(p):
        return bool(p["emails"] or p["mem"] or p["auth"] or p["don"] or p["unsub"] or p.get("added"))
    dropped = [p for p in people if not keep(p)]
    people = [p for p in people if keep(p)]
    print(f"dropped {len(dropped)} no-contact-info entries", file=__import__("sys").stderr)

    # ---- stats ----
    members = sum(1 for p in people if p["mem"])
    crm_people = sum(1 for p in people if "crm" in p["src"])
    author_people = sum(1 for p in people if p["auth"])
    typed = [p for p in people if p["types"]]
    type_matrix = {}
    for c in PERSON_CATS:
        grp = [p for p in people if c in p["types"]]
        type_matrix[c] = {"total": len(grp), "members": sum(1 for p in grp if p["mem"])}
    donors = sum(1 for p in people if p["don"])
    stats = {
        "total_people": len(people),
        "members_total_rows": members_total,
        "members": members,
        "crm_contacts": crm_people,
        "authors": author_people,
        "donors": donors,
        "donors_total_rows": donors_total,
        "total_raised": round(sum(p["damt"] for p in people), 2),
        "authors_who_are_members": sum(1 for p in people if p["auth"] and p["mem"]),
        "donors_who_are_members": sum(1 for p in people if p["don"] and p["mem"]),
        "donors_who_are_authors": sum(1 for p in people if p["don"] and p["auth"]),
        "crm_who_are_members": sum(1 for p in people if "crm" in p["src"] and p["mem"]),
        "typed_people": len(typed),
        "type_matrix": type_matrix,
    }

    PRIV.mkdir(exist_ok=True)
    (PRIV / "people.json").write_text(json.dumps(people, ensure_ascii=False, separators=(",", ":")))
    (PRIV / "network_stats.json").write_text(json.dumps(stats, indent=2, ensure_ascii=False))
    print(f"People (deconflicted): {len(people)}")
    print(f"  members: {members} | CRM contacts: {crm_people} | authors: {author_people} | donors: {donors}")
    print(f"  authors who are members: {stats['authors_who_are_members']}")
    print(f"  donors who are members: {stats['donors_who_are_members']} | donors who are authors: {stats['donors_who_are_authors']}")
    print(f"  total raised: ${stats['total_raised']:,.0f}")
    sz = (PRIV / "people.json").stat().st_size
    print(f"people.json: {sz//1024} KB")


if __name__ == "__main__":
    main()
