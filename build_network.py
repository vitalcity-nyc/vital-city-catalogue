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


def norm(s):
    if not s: return ""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode().lower().strip()
    s = re.sub(r"\b(dr|mr|mrs|ms|prof|jr|sr|phd|md|esq)\b\.?", "", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def firstlast(s):
    t = norm(s).split()
    return f"{t[0]} {t[-1]}" if len(t) >= 2 else (t[0] if t else "")

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


def set_email(p, email):
    """Set a person's email, preferring a real address over a made-up
    @vitalcitynyc.org one (Ghost assigns those to authors for organization)."""
    email = email_norm(email)
    if not email:
        return
    if not p["e"]:
        p["e"] = email
    elif p["e"].endswith("vitalcitynyc.org") and not email.endswith("vitalcitynyc.org"):
        p["e"] = email


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


def main():
    # ---- index helpers ----
    people = []                 # list of person dicts
    by_email = {}               # email -> person
    by_name = {}                # norm name -> person
    by_fl = {}                  # first+last -> person

    def get_or_make(email="", name="", fl=""):
        if email and email in by_email: return by_email[email]
        if name and name in by_name: return by_name[name]
        if fl and fl in by_fl: return by_fl[fl]
        p = {"n": "", "ns": "", "e": "", "inst": "", "role": "",
             "types": [], "topics": [], "mem": 0, "since": "", "auth": 0, "arts": 0,
             "don": 0, "damt": 0.0, "dcnt": 0, "dlast": "", "src": []}
        people.append(p)
        return p

    def index(p):
        if p.get("e"): by_email.setdefault(p["e"], p)
        nn = norm(p.get("n"))
        if nn:
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
            p = get_or_make(email=email, name=norm(recorded or guess))
            p["mem"] = 1
            p["src"].append("member")
            set_email(p, email)
            set_name(p, recorded, True) if recorded else set_name(p, guess, False)
            since = (row.get("created_at") or "")[:10]
            if since and not p["since"]: p["since"] = since
            index(p)

    # ---- 2. CRM contacts (types) ----
    crm = load_crm()
    crm_total = len(crm)
    for c in crm:
        p = get_or_make(email=c["email"], name=norm(c["name"]), fl=firstlast(c["name"]))
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
        p["types"] = sorted(set(p["types"]) | {"VC contributor"})   # anyone who wrote for us is a contributor
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
                p = get_or_make(email=email, name=norm(name), fl=firstlast(name))
                set_name(p, name, True)
                set_email(p, email)
                p["don"] = 1
                p["damt"] = round(p["damt"] + amt, 2)
                p["dcnt"] += cnt
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

    # Drop made-up author/contributor emails: Ghost assigns each author a fake
    # @vitalcitynyc.org address. Never list those (only authors' emails are made up).
    scrubbed = 0
    for p in people:
        if p["e"].endswith("vitalcitynyc.org") and (p["auth"] or "VC contributor" in p["types"]):
            p["e"] = ""
            scrubbed += 1

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
