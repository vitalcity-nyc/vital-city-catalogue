#!/usr/bin/env python3
"""Pull UNSUBSCRIBED members from the Vital City newsletter Mailchimp audience and
write private/unsubscribed_source.csv (Email, First Name, Last Name).

Mailchimp is used ONLY for the unsubscribe signal — Ghost stays the source of
truth for who *is* subscribed. build_network flags these emails `unsub`, which
forces them off the Ghost member list.

Key from $MAILCHIMP_KEY or private/.mailchimp_key (format <key>-<dc>, e.g. ...-us5).
Audience id from $MAILCHIMP_LIST or the default below.
"""
import base64, csv, json, os, sys, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRIV = ROOT / "private"
KEY = (os.environ.get("MAILCHIMP_KEY") or (PRIV / ".mailchimp_key").read_text()).strip()
DC = KEY.split("-")[-1]
LIST = os.environ.get("MAILCHIMP_LIST", "ec30bf0c4b")   # "Vital City Newsletter Contacts"
BASE = f"https://{DC}.api.mailchimp.com/3.0"
AUTH = base64.b64encode(f"anystring:{KEY}".encode()).decode()


def get(url):
    req = urllib.request.Request(url, headers={"Authorization": "Basic " + AUTH})
    return json.load(urllib.request.urlopen(req, timeout=120))


def main():
    rows, offset, count = [], 0, 1000
    while True:
        url = (f"{BASE}/lists/{LIST}/members?status=unsubscribed&count={count}&offset={offset}"
               "&fields=members.email_address,members.merge_fields,members.last_changed,total_items")
        d = get(url)
        members = d.get("members", [])
        for m in members:
            mf = m.get("merge_fields", {}) or {}
            email = (m.get("email_address") or "").strip()
            if email:
                # last_changed ~= when they unsubscribed (their last status change)
                udate = (m.get("last_changed") or "")[:10]
                rows.append([email, (mf.get("FNAME") or "").strip(), (mf.get("LNAME") or "").strip(), udate])
        offset += len(members)
        if not members or offset >= d.get("total_items", 0):
            break
    out = PRIV / "unsubscribed_source.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Email", "First Name", "Last Name", "Unsub Date"])
        w.writerows(rows)
    print(f"wrote {len(rows)} unsubscribed contacts -> {out.name}", file=sys.stderr)


if __name__ == "__main__":
    main()
