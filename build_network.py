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
               "state gov", "fed gov", "judge", "architect"]
TOPIC_CATS = ["criminal justice", "housing", "transit"]
NONPERSON = {"vital city", "a survey", "a photo essay", "a conversation",
             "the editors", "editorial board", "vital city staff", "various"}


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

def name_from_email(e):
    """Extrapolate a display name + name tokens from an email local-part."""
    local = email_norm(e).split("@")[0].split("+")[0]
    parts = [re.sub(r"[^a-z]", "", p) for p in re.split(r"[._\-]+", local)]
    parts = [p for p in parts if p and p not in GENERIC and len(p) >= 2]
    if len(parts) >= 2:
        return " ".join(p.capitalize() for p in parts[:2]), f"{parts[0]} {parts[-1]}"
    if len(parts) == 1:
        return parts[0].capitalize(), parts[0]
    return "", ""


def load_crm():
    wb = openpyxl.load_workbook(PRIV / "contacts_source.xlsx", read_only=True, data_only=True)
    ws = wb["combined"]; rows = list(ws.iter_rows(values_only=True))
    hdr = [str(c) for c in rows[0]]; idx = {h: i for i, h in enumerate(hdr)}
    def cell(r, h):
        i = idx.get(h); return r[i] if (i is not None and i < len(r)) else None
    out = []
    for r in rows[1:]:
        if not r or not r[0]: continue
        out.append({
            "name": str(r[0]).strip(),
            "email": email_norm(cell(r, "email")),
            "institution": (cell(r, "institution") or "").strip(),
            "role": (cell(r, "role") or "").strip(),
            "types": [c for c in PERSON_CATS if cell(r, c) not in (None, "", 0)],
            "topics": [c for c in TOPIC_CATS if cell(r, c) not in (None, "", 0)],
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
        p = {"n": "", "e": "", "inst": "", "role": "",
             "types": [], "topics": [], "mem": 0, "since": "", "auth": 0, "arts": 0,
             "don": 0, "damt": 0.0, "dcnt": 0, "src": []}
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
            disp, _tok = (recorded, norm(recorded)) if recorded else name_from_email(email)
            p = get_or_make(email=email, name=norm(disp))
            p["mem"] = 1
            p["src"].append("member")
            if email and not p["e"]: p["e"] = email
            if disp and not p["n"]: p["n"] = disp
            since = (row.get("created_at") or "")[:10]
            if since and not p["since"]: p["since"] = since
            index(p)

    # ---- 2. CRM contacts (types) ----
    crm = load_crm()
    crm_total = len(crm)
    for c in crm:
        p = get_or_make(email=c["email"], name=norm(c["name"]), fl=firstlast(c["name"]))
        if not p["n"]: p["n"] = c["name"]
        if c["email"] and not p["e"]: p["e"] = c["email"]
        if c["institution"] and not p["inst"]: p["inst"] = c["institution"]
        if c["role"] and not p["role"]: p["role"] = c["role"]
        p["types"] = sorted(set(p["types"]) | set(c["types"]))
        p["topics"] = sorted(set(p["topics"]) | set(c["topics"]))
        if "crm" not in p["src"]: p["src"].append("crm")
        index(p)

    # ---- 3. Authors (article counts) ----
    authors = json.loads((ROOT / "data" / "authors.json").read_text())
    authors_total = 0
    for a in authors:
        nn = norm(a["name"])
        if not nn or nn in NONPERSON: continue
        authors_total += 1
        p = get_or_make(name=nn, fl=firstlast(a["name"]))
        if not p["n"]: p["n"] = a["name"]
        p["auth"] = 1
        p["arts"] = a.get("post_count", 0)
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
                if not p["n"] and name:
                    p["n"] = name
                if email and not p["e"]:
                    p["e"] = email
                p["don"] = 1
                p["damt"] = round(p["damt"] + amt, 2)
                p["dcnt"] += cnt
                if "donor" not in p["src"]:
                    p["src"].append("donor")
                index(p)

    # NB: people with no real name (email-only members whose address yields no
    # name) keep an empty name and are identified by email in the UI.

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
