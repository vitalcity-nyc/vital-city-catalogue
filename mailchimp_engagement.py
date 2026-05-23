#!/usr/bin/env python3
"""Pull per-member ENGAGEMENT from the Vital City Mailchimp audience and write
private/engagement_source.csv (Email, Rating, Open Rate, Click Rate).

Mailchimp's member_rating (1-5 stars) + avg open/click rates let the tool rank
"most engaged subscribers". Joined to people by email in build_network.

Key from $MAILCHIMP_KEY or private/.mailchimp_key (format <key>-<dc>).
"""
import base64, csv, json, os, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRIV = ROOT / "private"
KEY = (os.environ.get("MAILCHIMP_KEY") or (PRIV / ".mailchimp_key").read_text()).strip()
DC = KEY.split("-")[-1]
LIST = os.environ.get("MAILCHIMP_LIST", "ec30bf0c4b")
BASE = f"https://{DC}.api.mailchimp.com/3.0"
AUTH = base64.b64encode(f"anystring:{KEY}".encode()).decode()


def get(url):
    req = urllib.request.Request(url, headers={"Authorization": "Basic " + AUTH})
    return json.load(urllib.request.urlopen(req, timeout=120))


def main():
    rows, offset, count = [], 0, 1000
    while True:
        url = (f"{BASE}/lists/{LIST}/members?status=subscribed&count={count}&offset={offset}"
               "&fields=members.email_address,members.member_rating,members.stats,total_items")
        d = get(url)
        members = d.get("members", [])
        for m in members:
            email = (m.get("email_address") or "").strip()
            if not email:
                continue
            st = m.get("stats", {}) or {}
            rows.append([email, m.get("member_rating") or 0,
                         round((st.get("avg_open_rate") or 0) * 100),
                         round((st.get("avg_click_rate") or 0) * 100)])
        offset += len(members)
        if not members or offset >= d.get("total_items", 0):
            break
    out = PRIV / "engagement_source.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Email", "Rating", "Open Rate", "Click Rate"])
        w.writerows(rows)
    print(f"wrote engagement for {len(rows)} members -> {out.name}", file=sys.stderr)


if __name__ == "__main__":
    main()
