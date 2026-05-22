#!/usr/bin/env python3
"""Pull donations from the Donorbox API and aggregate them per donor into
private/donors_source.csv — the exact shape build_network expects:
  First Name, Last Name, Email, Summed Donation Amount, Donations Count, Last Donation at

Replaces the manual Donorbox CSV export. Auth = account email + API key via HTTP
Basic (Donorbox also requires a User-Agent header).

Key from $DONORBOX_KEY or private/.donorbox_key; email from $DONORBOX_EMAIL
(default below).
"""
import base64, csv, datetime, json, os, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRIV = ROOT / "private"
KEY = (os.environ.get("DONORBOX_KEY") or (PRIV / ".donorbox_key").read_text()).strip()
EMAIL = os.environ.get("DONORBOX_EMAIL", "info@vitalcitynyc.org").strip()
AUTH = base64.b64encode(f"{EMAIL}:{KEY}".encode()).decode()
BASE = "https://donorbox.org/api/v1"


def get(path):
    req = urllib.request.Request(BASE + path, headers={
        "Authorization": "Basic " + AUTH,
        "User-Agent": "VitalCity-ContactSync/1.0",
        "Accept": "application/json",
    })
    return json.load(urllib.request.urlopen(req, timeout=120))


def main():
    today = datetime.date.today()
    cut7 = (today - datetime.timedelta(days=7)).isoformat()
    cut30 = (today - datetime.timedelta(days=30)).isoformat()
    agg = {}   # email -> {first,last,amount,count,ldate, d7,d7c,d30,d30c}
    page, per = 1, 100
    total = 0
    while True:
        rows = get(f"/donations?page={page}&per_page={per}")
        if not rows:
            break
        for d in rows:
            if (d.get("status") or "").lower() != "paid":
                continue            # skip refunded / failed
            don = d.get("donor") or {}
            email = (don.get("email") or "").strip().lower()
            name = (don.get("name") or "").strip()
            key = email or ("name:" + name.lower())
            if not key.strip(":"):
                continue
            try:
                amt = float(d.get("amount") or 0)
            except ValueError:
                amt = 0.0
            date = (d.get("donation_date") or "")[:10]   # YYYY-MM-DD
            a = agg.setdefault(key, {"first": "", "last": "", "email": email, "amount": 0.0,
                                     "count": 0, "ldate": "", "d7": 0.0, "d7c": 0, "d30": 0.0, "d30c": 0})
            a["amount"] += amt
            a["count"] += 1
            if not a["first"]: a["first"] = (don.get("first_name") or "").strip()
            if not a["last"]: a["last"] = (don.get("last_name") or "").strip()
            if not a["email"] and email: a["email"] = email
            if date > a["ldate"]: a["ldate"] = date
            if date >= cut30:
                a["d30"] += amt; a["d30c"] += 1
                if date >= cut7:
                    a["d7"] += amt; a["d7c"] += 1
        total += len(rows)
        print(f"  page {page}: {total} donations so far", file=sys.stderr)
        page += 1

    out = PRIV / "donors_source.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["First Name", "Last Name", "Email", "Summed Donation Amount",
                    "Donations Count", "Last Donation at",
                    "Amount 7d", "Count 7d", "Amount 30d", "Count 30d"])
        for a in agg.values():
            # build expects "Last Donation at" as M/D/YYYY
            last = ""
            if a["ldate"].count("-") == 2:
                y, m, dd = a["ldate"].split("-")
                last = f"{int(m)}/{int(dd)}/{y}"
            w.writerow([a["first"], a["last"], a["email"], round(a["amount"], 2),
                        a["count"], last, round(a["d7"], 2), a["d7c"],
                        round(a["d30"], 2), a["d30c"]])
    raised = sum(a["amount"] for a in agg.values())
    print(f"wrote {len(agg)} donors (${raised:,.0f} from {total} paid donations) -> {out.name}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
