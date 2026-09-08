#!/usr/bin/env python3
"""
Mirror the contacts database into a Google Sheet.

The contacts dashboard is the source of record, but a spreadsheet lets you
filter, pivot and write formulas against it. This keeps a Sheet in step with
the published, encrypted payload (network/data.enc), so it runs anywhere the
passphrase and the service-account key are available -- no plaintext needed.

Runs from GitHub Actions shortly after each daily refresh (see
.github/workflows/sheet-sync.yml). Overwrites the "contacts" tab and stamps
"refreshed at" on a "about" tab. The stamp only advances when a write
succeeds, so a stale stamp means the sync broke.

Environment:
  VC_NETWORK_PASS     passphrase for network/data.enc
  GA4_CREDS_JSON      the Google service-account key (raw JSON or base64)
  CONTACTS_SHEET_ID   the spreadsheet id (the long token in its URL)

Local dry run (no network):  python3 sheet_sync.py --dry-run
"""
import argparse, base64, datetime, json, os, pathlib, sys, time, urllib.request, urllib.error

ROOT = pathlib.Path(__file__).resolve().parent
ENC = ROOT / "network" / "data.enc"
TAB = "contacts"
META_TAB = "about"
CHUNK = 4000            # rows per API call; 14k rows x 51 cols is ~3 MB total


def log(m): print(m, flush=True)


# ---------------------------------------------------------------- decrypt
def load_people():
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    pw = os.environ.get("VC_NETWORK_PASS") or ""
    if not pw:
        f = ROOT / "private" / ".netpass"
        pw = f.read_text().strip() if f.exists() else ""
    if not pw:
        sys.exit("VC_NETWORK_PASS not set and private/.netpass missing")
    b = json.loads(ENC.read_text())
    k = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=base64.b64decode(b["salt"]),
                   iterations=b["iters"]).derive(pw.encode())
    d = json.loads(AESGCM(k).decrypt(base64.b64decode(b["iv"]), base64.b64decode(b["ct"]), None))
    return d if isinstance(d, list) else list(d.values())[0]


# ---------------------------------------------------------------- columns
# Same fields the dashboard holds, with headers a person can read. Kept in
# one place so the sheet, the CSV export and the page never drift apart.
def name_status(p):
    n = (p.get("n") or "").strip()
    if not n: return "blank"
    w = len(n.split()); g = p.get("ns") == "guess"
    return ("guessed, full" if w >= 2 else "guessed, partial") if g else ("confirmed, full" if w >= 2 else "confirmed, partial")

def parts(n):
    t = (n or "").strip().split()
    return (t[0] if t else "", " ".join(t[1:]) if len(t) > 1 else "")

yn = lambda v: "yes" if v else "no"
j = lambda v: "; ".join(v) if isinstance(v, list) else (v or "")
MAILBOX = {1: "scanner", 2: "role mailbox"}

COLS = [
 ("first_name", lambda p: p.get("fn") or parts(p.get("n"))[0]),
 ("last_name",  lambda p: p.get("ln") or parts(p.get("n"))[1]),
 ("name",       lambda p: p.get("n", "")),
 ("name_status", name_status),
 ("primary_email", lambda p: p.get("e", "")),
 ("all_emails", lambda p: j(p.get("emails"))),
 ("newsletter_emails", lambda p: j(p.get("sub_emails"))),
 ("most_recently_opened_email", lambda p: p.get("recent_email", "")),
 ("institution", lambda p: p.get("inst", "")),
 ("role", lambda p: p.get("role", "")),
 ("segment", lambda p: p.get("seg", "")),
 ("types", lambda p: j(p.get("types"))),
 ("specialties", lambda p: j(p.get("topics"))),
 ("new_york_link", lambda p: yn(p.get("nyc"))),
 ("never_solicit", lambda p: yn(p.get("excl"))),
 ("oom_flag", lambda p: yn(p.get("oom"))),
 ("mailbox_type", lambda p: MAILBOX.get(p.get("gw"), "")),
 ("subscriber", lambda p: yn(p.get("mem"))),
 ("subscribed_since", lambda p: p.get("since", "")),
 ("unsubscribed", lambda p: yn(p.get("unsub"))),
 ("unsubscribed_on", lambda p: p.get("udate", "")),
 ("active_30d", lambda p: yn(p.get("mau"))),
 ("active_1y", lambda p: yn(p.get("aau"))),
 ("power_reader", lambda p: yn(p.get("power_reader"))),
 ("at_risk", lambda p: yn(p.get("at_risk"))),
 ("sunset_candidate", lambda p: yn(p.get("sunset_candidate"))),
 ("engagement_rating_0_5", lambda p: p.get("erate") or 0),
 ("open_rate_pct", lambda p: p.get("eopen") or 0),
 ("click_rate_pct", lambda p: p.get("eclick") or 0),
 ("engagement_score_0_200", lambda p: (p.get("erate") or 0) * 20 + (p.get("eopen") or 0)),
 ("rate_warning_possible_scanner", lambda p: yn(p.get("ratewarn"))),
 ("opens_7d", lambda p: p.get("d7c") or 0),
 ("opens_30d", lambda p: p.get("d30c") or 0),
 ("donor", lambda p: yn(p.get("don"))),
 ("donation_total", lambda p: p.get("damt") or 0),
 ("donation_count", lambda p: p.get("dcnt") or 0),
 ("last_donation", lambda p: p.get("dlast", "")),
 ("prospect_score", lambda p: p.get("pros") or 0),
 ("likely_prospect", lambda p: yn((p.get("pros") or 0) >= 4)),
 ("prospect_why", lambda p: p.get("prosw", "")),
 ("author", lambda p: yn(p.get("auth"))),
 ("articles", lambda p: p.get("arts") or 0),
 ("byline", lambda p: p.get("aname", "")),
 ("last_article", lambda p: p.get("alast", "")),
 ("press", lambda p: yn(p.get("press"))),
 ("press_outlet", lambda p: p.get("poutlet", "")),
 ("press_twitter", lambda p: p.get("ptw", "")),
 ("notable_wikipedia", lambda p: yn(p.get("wiki"))),
 ("starred", lambda p: yn(p.get("star"))),
 ("sources", lambda p: j(p.get("src"))),
 ("notes", lambda p: p.get("note", "")),
]

def rows_for(people):
    head = [c for c, _ in COLS]
    body = [[fn(p) for _, fn in COLS] for p in people]
    return head, body


# ---------------------------------------------------------------- sheets api
def creds():
    raw = os.environ.get("GA4_CREDS_JSON", "").strip()
    if not raw:
        sys.exit("GA4_CREDS_JSON not set - the service-account key is the only credential this needs")
    try:
        info = json.loads(raw)
    except ValueError:
        info = json.loads(base64.b64decode(raw))
    from google.oauth2 import service_account
    c = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"])
    from google.auth.transport.requests import Request
    c.refresh(Request())
    return c.token, info.get("client_email", "?")

def api(token, method, url, body=None, tries=4):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    for i in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:600]
            if e.code in (429, 500, 502, 503) and i < tries - 1:
                time.sleep(3 * (i + 1)); continue
            raise SystemExit(f"Sheets API {e.code} on {method} {url.split('?')[0]}: {msg}")

def ensure_tabs(token, sid):
    meta = api(token, "GET", f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=sheets.properties")
    have = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}
    reqs = [{"addSheet": {"properties": {"title": t}}} for t in (TAB, META_TAB) if t not in have]
    if reqs:
        api(token, "POST", f"https://sheets.googleapis.com/v4/spreadsheets/{sid}:batchUpdate", {"requests": reqs})
        meta = api(token, "GET", f"https://sheets.googleapis.com/v4/spreadsheets/{sid}?fields=sheets.properties")
        have = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}
    return have

def push(sid, head, body):
    token, who = creds()
    log(f"authorised as {who}")
    tabs = ensure_tabs(token, sid)
    base = f"https://sheets.googleapis.com/v4/spreadsheets/{sid}"
    # Clear, then write in chunks. RAW so an email or a note is never parsed as
    # a formula or a date.
    api(token, "POST", f"{base}/values/{TAB}!A:ZZ:clear", {})
    api(token, "PUT", f"{base}/values/{TAB}!A1?valueInputOption=RAW", {"values": [head]})
    for i in range(0, len(body), CHUNK):
        chunk = body[i:i + CHUNK]
        api(token, "PUT", f"{base}/values/{TAB}!A{i + 2}?valueInputOption=RAW", {"values": chunk})
        log(f"  wrote rows {i + 1}-{i + len(chunk)}")
    # Freeze the header and keep the sheet sized to the data.
    api(token, "POST", f"{base}:batchUpdate", {"requests": [
        {"updateSheetProperties": {"properties": {"sheetId": tabs[TAB], "gridProperties": {"frozenRowCount": 1}},
                                   "fields": "gridProperties.frozenRowCount"}},
    ]})
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    api(token, "PUT", f"{base}/values/{META_TAB}!A1?valueInputOption=RAW", {"values": [
        ["refreshed at", stamp],
        ["people", len(body)],
        ["columns", len(head)],
        ["source", "network/data.enc in vitalcity-nyc/vital-city-catalogue, mirrored by sheet_sync.py"],
        ["note", "This tab is overwritten on every sync. Edits made here do not flow back to the dashboard; "
                 "use the dashboard's edit tools for that. If 'refreshed at' stops advancing, the sync broke."],
    ]})
    log(f"done: {len(body):,} people x {len(head)} columns, stamped {stamp}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="build the rows, touch nothing")
    a = ap.parse_args()
    people = load_people()
    if len(people) < 5000:          # the list is ~14k; a small payload is a broken one
        sys.exit(f"refusing to mirror {len(people)} people - that is not the contacts database")
    head, body = rows_for(people)
    log(f"{len(body):,} people x {len(head)} columns")
    if a.dry_run:
        log("dry run: first row -> " + json.dumps(dict(zip(head, body[0])))[:300])
        return
    sid = os.environ.get("CONTACTS_SHEET_ID", "").strip()
    if not sid:
        sys.exit("CONTACTS_SHEET_ID not set")
    push(sid, head, body)

if __name__ == "__main__":
    main()
