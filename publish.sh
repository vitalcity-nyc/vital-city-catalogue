#!/bin/bash
# One-command publish for the network explorer.
# Rebuilds the deconflicted people dataset from whatever source files are in
# private/, re-encrypts it (reusing the saved passphrase), and pushes — which
# auto-deploys the live page. Run this after refreshing any source list.
#
#   bash publish.sh
#
# Source files it reads (all in private/, all gitignored):
#   contacts_source.csv   ← export of the maintained Google Sheet (preferred)
#   contacts_source.xlsx  ← fallback if no CSV present
#   members_source.csv    ← Ghost members export
#   donors_source.csv     ← FCNY donors export

set -uo pipefail
cd "$(dirname "$0")" || exit 1
PY=/usr/bin/python3

echo "1/3  Rebuilding people dataset…"
"$PY" build_network.py || { echo "build failed"; exit 1; }

echo "2/3  Encrypting…"
"$PY" encrypt_people.py || { echo "encrypt failed"; exit 1; }

echo "3/3  Publishing…"
if git diff --quiet -- network/data.enc; then
  echo "no change to publish."
  exit 0
fi
git add network/data.enc
git -c user.name="Vital City" -c user.email="josh.greenman@gmail.com" \
  commit -q -m "Refresh network explorer $(date '+%Y-%m-%d %H:%M')"
if git push -q origin main; then
  echo "Published → https://vitalcity-nyc.github.io/vital-city-catalogue/network/"
else
  echo "Push failed. Run: gh auth switch --user vitalcity-nyc   then: git push"
  exit 1
fi
