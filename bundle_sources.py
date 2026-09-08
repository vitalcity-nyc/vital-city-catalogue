#!/usr/bin/env python3
"""Pack/unpack the slow-changing private source files, encrypted with the same
passphrase as data.enc, so the daily GitHub Actions job can rebuild people.json
in the cloud without the plaintext sources ever living in the public repo.

  python3 bundle_sources.py pack     # private/<sources>  -> private_sources.enc  (commit this)
  python3 bundle_sources.py unpack   # private_sources.enc -> private/<sources>   (CI uses this)

Passphrase: $VC_NETWORK_PASS, else private/.netpass. AES-256-GCM, PBKDF2-SHA256 600k
(identical scheme to encrypt_people.py). Only the slow-changing sources are
bundled; newsletter members and article authors are refreshed live each run from
the Ghost API, so they are deliberately excluded here.

Re-run `pack` and commit private_sources.enc whenever you update any of the
bundled CSVs (contacts, donors, author roster, unsubscribes, contributors).
"""
import base64, io, json, os, sys, tarfile, secrets
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

ROOT = Path(__file__).resolve().parent
PRIV = ROOT / "private"
ENC = ROOT / "private_sources.enc"
ITERS = 600_000

# Slow-changing sources only. members_source.csv + data/authors.json are refreshed
# live each run, so they are intentionally NOT bundled.
SOURCES = [
    "contacts_source.csv",
    "donors_source.csv",
    "press_source.csv",        # curated media/press contacts
    "x_followers_source.csv",  # point-in-time X follower export (manual snapshot)
    "x_followers_asof.txt",    # its as-of date
    "vc_authors.csv",
    "unsubscribed_source.csv",
    "extra_contributors.csv",
    "name_overrides.csv",      # optional
    "people_overrides.json",   # optional (in-tool edits)
    "wiki_cache.json",         # optional (Wikipedia influence cache)
    "events/2025-11-fundraiser.csv",  # house-party guest list (Paperless export, RSVP statuses)
    "events/2026-09-fundraiser.csv",  # house-party guest list (name,email,status)
]


def passphrase():
    p = os.environ.get("VC_NETWORK_PASS")
    if p:
        return p
    np = PRIV / ".netpass"
    if np.exists() and np.read_text().strip():
        return np.read_text().strip()
    sys.exit("No passphrase: set $VC_NETWORK_PASS or create private/.netpass")


def derive(pw, salt):
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                      iterations=ITERS).derive(pw.encode())


def pack():
    buf = io.BytesIO()
    n = 0
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in SOURCES:
            f = PRIV / name
            if f.exists():
                tar.add(f, arcname=name)
                n += 1
    if n == 0:
        sys.exit("nothing to pack — no source files found in private/")
    salt, iv = secrets.token_bytes(16), secrets.token_bytes(12)
    ct = AESGCM(derive(passphrase(), salt)).encrypt(iv, buf.getvalue(), None)
    ENC.write_text(json.dumps({
        "v": 1, "kdf": "PBKDF2-SHA256", "iters": ITERS,
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ct": base64.b64encode(ct).decode(),
    }))
    print(f"packed {n} source files -> {ENC.name} ({ENC.stat().st_size // 1024} KB)")


def unpack():
    blob = json.loads(ENC.read_text())
    key = derive(passphrase(), base64.b64decode(blob["salt"]))
    plain = AESGCM(key).decrypt(base64.b64decode(blob["iv"]),
                                base64.b64decode(blob["ct"]), None)
    PRIV.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
        tar.extractall(PRIV, filter="data")
    print(f"unpacked sources -> {PRIV}/")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "pack":
        pack()
    elif mode == "unpack":
        unpack()
    else:
        sys.exit("usage: bundle_sources.py [pack|unpack]")
