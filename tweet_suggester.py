#!/usr/bin/env python3
"""
Scan vitalcitynyc.org for newly published pieces and draft 2-3 posts for each.

Reads the Ghost Content API, keeps a seen-list so nothing is suggested twice,
sends each new piece's full text to Claude, and asks for posts written in
plain declarative English rather than the shapes that make writing sound
machine-generated. Delivers to Slack, or prints.

  python3 tweet_suggester.py                    # last 7 days, print
  python3 tweet_suggester.py --days 30          # wider window
  python3 tweet_suggester.py --slack            # post to Slack
  python3 tweet_suggester.py --dry-run          # list what it would draft, no API spend
  python3 tweet_suggester.py --piece <slug>     # redo one piece, ignoring the seen-list
  python3 tweet_suggester.py --self-test        # offline checks

Costs money: one Claude call per new piece (~$0.02 each at current Sonnet
prices). --dry-run first if a wide window might pick up dozens.

Secrets: ANTHROPIC_API_KEY from env or macOS keychain. Slack needs
SLACK_BOT_TOKEN and SLACK_DM_TO in the environment.
"""

import argparse, json, os, re, subprocess, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE  = Path(__file__).resolve().parent
STATE = HERE / "data" / "tweet_suggester_seen.json"
OUT   = HERE / "data" / "tweet_suggestions.json"

GHOST_KEY = "dd8e178e9ddfc883537e71dd07"          # public content key
GHOST_API = "https://vital-city.ghost.io/ghost/api/content"
API_URL   = "https://api.anthropic.com/v1/messages"
MODEL     = "claude-sonnet-5"
MAX_CHARS = 260        # X allows 280; leave room for the link X appends


class Abort(Exception):
    """Something is wrong enough that a silent partial run would mislead."""


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# --------------------------------------------------------------- the prompt
# The ban list is Josh's anti-style guide. It is stated as concrete sentence
# shapes rather than "write naturally", because a model told to write
# naturally writes the same six shapes every time.

STYLE = """You are drafting posts for X (Twitter) for Vital City, a journal of
urban policy and civic life in New York City. The account's readers are
reporters, city officials, academics and engaged New Yorkers. They can tell
when something was written by a machine, and they discount it.

WRITE LIKE THIS
- Lead with the finding. A number, a specific claim, a concrete fact from the
  piece. The account's own analytics show posts carrying a claim outperform
  posts carrying a headline.
- Plain declarative sentences. Short ones are good. Starting a sentence with
  "But" or "And" is good.
- Assume the reader is smart and busy. No throat-clearing.
- One idea per post.

NEVER WRITE THESE (they are the tells that make copy read as AI-written)
- Negation-then-reveal in any form: "It's not X, it's Y." "This isn't about
  X. It's about Y." "X, not Y, is the real story." Split across two sentences
  counts too. Just state the positive claim and delete the thing it isn't.
- Significance inflation: "the single most", "the clearest example", "a
  watershed", "marks a turning point", "what everyone is missing", "the real
  story". Give the number and let it rank itself.
- Announcing that something matters: "Here's the thing." "Worth noting."
  "Read that again." "Let that sink in." "The tell is..." Say the thing.
- Purple abstraction: "the machinery of", "the architecture of", "the rhythm
  of", "lays bare", "grapples with", "sheds light on", "dives deep into",
  "unpacks", "explores the complex interplay".
- Aphorism formulas: "X is the language of Y", "the tail is where the
  variation lives", any slot-fill line that sounds quotable and says less
  than the plain version.
- Rhetorical question openers: "What if...?" "Ever wonder why...?" "The
  question nobody is asking?"
- Em dash used to stage a pause before a punchline. A two-sentence build
  where the second sentence exists only to deliver a reveal.
- Hollow intensifiers: genuinely, truly, simply, incredibly, remarkably,
  fascinating, powerful, essential, must-read, deeply.
- Engagement bait: "A thread.", "Read the full piece:", "Link below", "Our
  latest", "New from us", "ICYMI", "Don't miss".
- Hashtags. Emoji. Title Case. Exclamation marks.

MECHANICS
- "New York City" spelled out, never NYC. Spell out an acronym on first use
  (NYPD is New York City Police Department; MTA is Metropolitan
  Transportation Authority).
- No serial comma: "housing, transit and policing".
- Straight quotes and apostrophes only.
- Under 260 characters. The link is appended automatically, so do not
  include a URL, and do not write "link in bio" or similar.
- Do not name the author unless their identity is the reason to read it (a
  former official, a named expert, someone with standing on the subject).
- Never write "in Vital City", "Vital City reports" or similar. The account
  posting this is Vital City; saying so is redundant.
- Do not guess anyone's pronouns. If the piece does not make a person's
  pronouns explicit, use their name again or write "they". Getting this
  wrong misgenders a real person; the neutral version never does. A
  first-person essay does NOT establish its author's pronouns - writing "I"
  for 2,000 words tells you nothing about how to refer to them.

THE THREE POSTS MUST DIFFER IN KIND, not be paraphrases of each other:
1. The finding. The most specific, checkable claim in the piece.
2. The stake or the argument. What the piece says should happen, or what it
   says everyone gets wrong, in the piece's own terms.
3. The hook. Either the human detail, the historical fact, or the
   counterintuitive number that makes someone stop scrolling. If the piece
   has no such detail, write a second finding instead of inventing one.

Every claim must come from the text given to you. Do not add numbers,
attributions or context that are not in the piece. If the piece is a
personal essay or a reminiscence rather than an argument, do not force it
into a policy frame; quote its concreteness instead."""

EXAMPLES = """Posts the editor approved, as a calibration:

"Redlining ended in 1968. You can still find it on a map of New York City today."
"Twenty things that bring crime down in cities. None of them replace policing. All of them work alongside it."
"Rents are too high for tenants and too low to keep the buildings standing. Both are true, which is what makes the Rent Guidelines Board vote so hard."
"Five things that work on rats, according to the research. New York City keeps trying the other things."
"We asked a former Secretary of Homeland Security where immigration enforcement stops being enforcement. He answered."
"Government already spends billions putting food on New York City tables. Anyone calling municipal groceries radical might start with SNAP."
"A French aristocrat spent nine months here in 1831 working out why Americans join things. His answer still explains the nonprofit sector."
"Summer jobs get proposed every June as a violence strategy. The research says most violence is a crime of passion, which complicates the pitch."

Posts the editor would reject, and why:

"This piece unpacks the complex interplay between housing policy and homelessness." (purple abstraction, says nothing)
"It's not about the money. It's about who decides." (negation-then-reveal)
"The single most important thing you'll read about Rikers this year." (significance inflation)
"Ever wonder why your rent keeps going up? A must-read from our latest issue." (rhetorical question, engagement bait, hollow intensifier)
"New from us: a deep dive into NYPD staffing. A thread." (engagement bait, unspelled acronym)"""

TASK = """Here is a piece published by Vital City.

TITLE: {title}
DEK: {dek}
AUTHOR: {author}
TAGS: {tags}
PUBLISHED: {date}

FULL TEXT:
{body}

Write three posts following the rules above. Return only JSON, no other
text, in this exact shape:

{{"posts": [
  {{"kind": "finding", "text": "..."}},
  {{"kind": "argument", "text": "..."}},
  {{"kind": "hook", "text": "..."}}
]}}"""


# ----------------------------------------------------------------- plumbing
def http_json(url, headers=None, data=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def anthropic_key():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ["ANTHROPIC_API_KEY"]
    try:
        return subprocess.check_output(
            ["security", "find-generic-password", "-s", "ANTHROPIC_API_KEY", "-w"],
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        raise Abort("No ANTHROPIC_API_KEY in the environment or the keychain.")


def fetch_posts(days, limit=100):
    """Recent posts with full text. An empty result is an error, not a quiet
    'nothing new' - a broken key or a changed endpoint looks identical to a
    quiet week otherwise."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    flt = urllib.parse.quote(f"published_at:>'{since}'")
    url = (f"{GHOST_API}/posts/?key={GHOST_KEY}&limit={limit}"
           f"&order={urllib.parse.quote('published_at desc')}&filter={flt}"
           "&include=authors,tags&formats=plaintext")
    d = http_json(url, timeout=45)
    if "posts" not in d:
        raise Abort(f"Ghost returned no posts array: {str(d)[:200]}")
    # A zero-length window is plausible; a zero-length 90-day window is not.
    if not d["posts"] and days >= 30:
        raise Abort(f"Ghost returned 0 posts for the last {days} days. "
                    "That is almost certainly a broken query, not a quiet quarter.")
    return d["posts"]


def load_seen():
    if STATE.exists():
        try:
            return set(json.loads(STATE.read_text()).get("slugs", []))
        except Exception:
            log("! seen-list unreadable, treating every piece as new")
    return set()


def save_seen(slugs):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(
        {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "slugs": sorted(slugs)}, indent=1))


# ------------------------------------------------------------------- drafting
BANNED = [
    r"\bit'?s not\b.{0,60}\bit'?s\b", r"\bisn'?t (?:about|just)\b.{0,60}\bit'?s\b",
    r"\bnot (?:just|only)\b.{0,40}\bbut\b",
    r"\bthe single (?:most|biggest|loudest)\b", r"\bwatershed\b", r"\bturning point\b",
    r"\bmust[- ]read\b", r"\bdeep dive\b", r"\bdives? deep\b", r"\bunpacks?\b",
    r"\bsheds? light\b", r"\blays? bare\b", r"\bgrapples? with\b",
    r"\bthe (?:machinery|architecture|rhythm) of\b",
    r"\bhere'?s the thing\b", r"\bworth noting\b", r"\blet that sink in\b",
    r"\bread that again\b", r"\bICYMI\b", r"\ba thread\b", r"\bdon'?t miss\b",
    r"\bour latest\b", r"\bnew from us\b", r"\bread the full\b", r"\blink in bio\b",
    r"\bgenuinely\b", r"\btruly\b", r"\bincredibly\b", r"\bremarkably\b",
    r"\bfascinating\b", r"\bever wonder\b", r"\bwhat if\b",
    r"#\w", r"[\U0001F300-\U0001FAFF☀-➿]", r"[’‘“”]", r"\bNYC\b",
]


def screen(text):
    """Return the reasons a draft should not go out as written."""
    bad = []
    for pat in BANNED:
        m = re.search(pat, text, re.I if not pat.startswith("#") else 0)
        if m:
            bad.append(repr(m.group(0)))
    if len(text) > MAX_CHARS:
        bad.append(f"{len(text)} chars")
    if re.search(r"https?://", text):
        bad.append("contains a URL")
    return bad


def draft(key, post, retries=2):
    body = (post.get("plaintext") or "").strip()
    if len(body) < 200:
        raise Abort(f"'{post['title']}' came back with {len(body)} characters of text.")
    prompt = "\n\n".join([STYLE, EXAMPLES, TASK.format(
        title=post["title"],
        dek=post.get("custom_excerpt") or post.get("excerpt") or "(none)",
        author=", ".join(a["name"] for a in post.get("authors") or []) or "(unattributed)",
        tags=", ".join(t["name"] for t in post.get("tags") or [] if not t["name"].startswith("#")) or "(none)",
        date=post["published_at"][:10],
        body=body[:24000])])

    last = None
    for attempt in range(retries + 1):
        msg = prompt if attempt == 0 else (
            prompt + "\n\nYour previous attempt broke these rules: " + last +
            "\nRewrite all three. Same JSON shape.")
        r = http_json(API_URL, timeout=120,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            data=json.dumps({"model": MODEL, "max_tokens": 4000,
                             "messages": [{"role": "user", "content": msg}]}).encode())
        text = "".join(b.get("text", "") for b in r.get("content", [])
                       if b.get("type") == "text").strip()
        if not text:
            kinds = [b.get("type") for b in r.get("content", [])]
            raise Abort(f"No text in the model reply (blocks: {kinds}, "
                        f"stop_reason: {r.get('stop_reason')})")
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
        try:
            posts = json.loads(text[text.find("{"):text.rfind("}") + 1])["posts"]
        except Exception as e:
            last = f"the response was not valid JSON ({e})"
            continue
        problems = {p["text"]: screen(p["text"]) for p in posts}
        flagged = {t: v for t, v in problems.items() if v}
        if not flagged:
            return posts, attempt
        last = "; ".join(f"{t[:40]}... -> {', '.join(v)}" for t, v in flagged.items())
        log(f"  retry {attempt+1}: {last[:160]}")
    # Hand back the last attempt with its problems attached rather than
    # dropping the piece; a flagged draft a human can fix beats silence.
    for p in posts:
        p["flags"] = screen(p["text"])
    return posts, retries + 1


# ------------------------------------------------------------------ delivery
def format_slack(results):
    L = [f"*Vital City - {len(results)} new "
         f"{'piece' if len(results)==1 else 'pieces'}, suggested posts*", ""]
    for r in results:
        L.append(f"*<{r['url']}|{r['title']}>*")
        by = r.get("author")
        L.append(f"_{by} · {r['date']}_" if by else f"_{r['date']}_")
        for p in r["posts"]:
            flag = "  :warning: " + ", ".join(p["flags"]) if p.get("flags") else ""
            L.append(f"> {p['text']}{flag}")
        L.append("")
    L.append("_Drafts, not decisions. Each one is meant to be postable as written "
             "or cut for parts. The link is appended when you post, so none of "
             "them include a URL._")
    return "\n".join(L)


def post_slack(text):
    tok, to = os.environ.get("SLACK_BOT_TOKEN"), os.environ.get("SLACK_DM_TO")
    if not tok or not to:
        raise Abort("SLACK_BOT_TOKEN / SLACK_DM_TO not set - the digest has nowhere to go.")
    r = http_json("https://slack.com/api/chat.postMessage", timeout=30,
        headers={"Authorization": f"Bearer {tok}",
                 "Content-Type": "application/json; charset=utf-8"},
        data=json.dumps({"channel": to, "text": text, "unfurl_links": False}).encode())
    if not r.get("ok"):
        raise Abort(f"Slack refused the message: {r.get('error')}")
    log("posted to Slack")


# ----------------------------------------------------------------- self-test
def self_test():
    ok = True
    cases = [
        ("It's not the budget. It's the politics.", True),
        ("The single most important number in the report is 4.2%.", True),
        ("A must-read deep dive on Rikers. A thread.", True),
        ("Ever wonder why NYC rents keep climbing? 🏠 #housing", True),
        ("Redlining ended in 1968. You can still find it on a map of New York City today.", False),
        ("Twenty things that bring crime down in cities. None of them replace policing.", False),
        ("x" * 300, True),
    ]
    for text, should_flag in cases:
        flagged = bool(screen(text))
        if flagged != should_flag:
            ok = False
            print(f"FAIL: {text[:50]!r} flagged={flagged} expected={should_flag}")
    print("self-test passed" if ok else "self-test FAILED")
    return 0 if ok else 1


# ---------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7, help="lookback window (default 7)")
    ap.add_argument("--slack", action="store_true", help="post the digest to Slack")
    ap.add_argument("--dry-run", action="store_true", help="list new pieces, spend nothing")
    ap.add_argument("--piece", metavar="SLUG", help="redraft one piece, ignoring the seen-list")
    ap.add_argument("--max", type=int, default=12, help="cap pieces per run (default 12)")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return self_test()

    posts = fetch_posts(max(a.days, 60) if a.piece else a.days)
    seen = load_seen()

    if a.piece:
        new = [p for p in posts if p["slug"] == a.piece]
        if not new:
            raise Abort(f"No piece with slug '{a.piece}' in the last 60 days.")
    else:
        new = [p for p in posts if p["slug"] not in seen]

    log(f"{len(posts)} published in window, {len(new)} not yet suggested")
    if not new:
        log("nothing new")
        return 0
    if len(new) > a.max:
        log(f"! capping at {a.max}; run again to pick up the rest")
        new = new[:a.max]

    if a.dry_run:
        for p in new:
            print(f"  {p['published_at'][:10]}  {p['title']}")
        print(f"\n{len(new)} pieces would cost roughly ${0.02*len(new):.2f}.")
        return 0

    key, results = anthropic_key(), []
    for i, p in enumerate(new, 1):
        log(f"[{i}/{len(new)}] {p['title'][:64]}")
        drafts, tries = draft(key, p)
        results.append({
            "slug": p["slug"], "title": p["title"], "url": p["url"],
            "date": p["published_at"][:10],
            "author": ", ".join(a_["name"] for a_ in p.get("authors") or []),
            "posts": drafts, "attempts": tries + 1})
        if i < len(new):
            time.sleep(1)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(
        {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "pieces": results}, indent=1))

    digest = format_slack(results)
    if a.slack:
        post_slack(digest)
    else:
        print("\n" + digest)

    if not a.piece:
        save_seen(seen | {p["slug"] for p in new})
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Abort as e:
        log(f"ABORT: {e}")
        sys.exit(2)
