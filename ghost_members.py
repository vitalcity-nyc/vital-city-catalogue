#!/usr/bin/env python3
"""Pull Vital City's newsletter SUBSCRIBERS from the Ghost Admin API.

READ-ONLY. Hits only the /members/ endpoint (real subscriber emails) — never
/users/ (where Ghost's made-up @vitalcitynyc.org author emails live). Writes a
fresh subscriber CSV in the same shape as members_source.csv.

Usage:
  python3 ghost_members.py            # dry run: write members_ghost.csv + print a diff
  python3 ghost_members.py --apply    # also replace members_source.csv (after you've seen the diff)

Key is read from private/.ghost_admin_key (gitignored), format id:secret.
"""
import base64, csv, hashlib, hmac, json, sys, time, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRIV = ROOT / "private"
BASE = "https://vital-city.ghost.io/ghost/api/admin"
KID, SECRET = (PRIV / ".ghost_admin_key").read_text().strip().split(":")


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=")


def _token():
    iat = int(time.time())
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": KID}).encode())
    payload = _b64(json.dumps({"iat": iat, "exp": iat + 300, "aud": "/admin/"}).encode())
    signing = header + b"." + payload
    sig = hmac.new(bytes.fromhex(SECRET), signing, hashlib.sha256).digest()
    return (signing + b"." + _b64(sig)).decode()


def fetch_members():
    members, page = [], 1
    while True:
        url = f"{BASE}/members/?limit=500&page={page}"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Ghost {_token()}",   # regenerate each page (5-min expiry)
            "Accept-Version": "v5.0",
        })
        data = json.load(urllib.request.urlopen(req, timeout=90))
        members.extend(data.get("members", []))
        pg = data.get("meta", {}).get("pagination", {})
        print(f"  page {page}: {len(members)} so far (of {pg.get('total')})", file=sys.stderr)
        if not pg.get("next"):
            break
        page = pg["next"]
    return members


def write_csv(members, path):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["email", "name", "created_at"])
        for m in members:
            w.writerow([(m.get("email") or "").strip(),
                        (m.get("name") or "").strip(),
                        m.get("created_at") or ""])


def load_emails(path):
    if not path.exists():
        return {}
    out = {}
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            e = (r.get("email") or "").strip().lower()
            if e:
                out[e] = (r.get("name") or "").strip()
    return out


def main():
    apply = "--apply" in sys.argv
    print("Pulling members from Ghost Admin API (read-only, /members/ only)…", file=sys.stderr)
    members = fetch_members()
    out = PRIV / "members_ghost.csv"
    write_csv(members, out)

    ghost = {m["email"].strip().lower(): (m.get("name") or "") for m in members if m.get("email")}
    current = load_emails(PRIV / "members_source.csv")
    added = sorted(set(ghost) - set(current))
    removed = sorted(set(current) - set(ghost))
    vc = sorted(e for e in ghost if e.endswith("vitalcitynyc.org"))

    print("\n=== DRY RUN: Ghost members vs current members_source.csv ===")
    print(f"  Ghost members:        {len(ghost):,}")
    print(f"  Current CSV members:  {len(current):,}")
    print(f"  New in Ghost (added): {len(added):,}")
    print(f"  In CSV, not in Ghost: {len(removed):,}")
    print(f"  @vitalcitynyc.org in Ghost members (should be ~0): {len(vc)}")
    if vc[:10]:
        print("     e.g.:", vc[:10])
    print(f"  with a name: {sum(1 for v in ghost.values() if v.strip()):,}")
    print(f"\n  wrote {out.name} (not applied to the live source)")

    if apply:
        (PRIV / "members_ghost.csv").replace(PRIV / "members_source.csv")
        print("  APPLIED → members_source.csv replaced. Run build + publish next.")


if __name__ == "__main__":
    main()
