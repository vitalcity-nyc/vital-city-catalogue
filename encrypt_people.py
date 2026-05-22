#!/usr/bin/env python3
"""Encrypt the people dataset for the password-protected network explorer.

Reads  private/people.json   (plaintext, gitignored)
Writes network/data.enc      (AES-256-GCM ciphertext — safe to publish)

The passphrase is taken from $VC_NETWORK_PASS if set, otherwise a strong one is
generated. The passphrase is printed once and NEVER written to disk or git.
Share it with coworkers out-of-band; rotate by re-running with a new value.
"""
import base64, json, os, secrets
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "private" / "people.json"
OUT = ROOT / "network" / "data.enc"
ITERS = 600_000

WORDS = ("amber anchor atlas basin beacon birch borough bridge canyon cedar cipher "
         "cobalt copper cyan delta ember falcon fjord garnet harbor hazel indigo ivory "
         "jasper kelp lantern lattice lumen maple meadow mica north onyx opal orchard "
         "pewter quartz quill raven ridge river saffron slate spruce summit tundra umber "
         "vault verde willow zephyr").split()


def make_passphrase():
    return "-".join(secrets.choice(WORDS) for _ in range(5))


def main():
    data = SRC.read_bytes()
    passphrase = os.environ.get("VC_NETWORK_PASS") or make_passphrase()

    salt = secrets.token_bytes(16)
    iv = secrets.token_bytes(12)
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERS)
    key = kdf.derive(passphrase.encode())
    ct = AESGCM(key).encrypt(iv, data, None)

    blob = {
        "v": 1, "kdf": "PBKDF2-SHA256", "iters": ITERS,
        "salt": base64.b64encode(salt).decode(),
        "iv": base64.b64encode(iv).decode(),
        "ct": base64.b64encode(ct).decode(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(blob))
    print(f"Wrote {OUT} ({OUT.stat().st_size//1024} KB encrypted)")
    print("\n" + "=" * 52)
    print("  PASSPHRASE (save this; share out-of-band):")
    print(f"      {passphrase}")
    print("=" * 52)
    if not os.environ.get("VC_NETWORK_PASS"):
        print("  (auto-generated. To set your own: VC_NETWORK_PASS='...' python3 encrypt_people.py)")


if __name__ == "__main__":
    main()
