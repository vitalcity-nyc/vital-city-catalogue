#!/usr/bin/env python3
"""Pull every signal that feeds the growth dashboard, write private/growth.json.

Signups-first. Sections that need credentials we don't have yet (GA4, Search
Console, social) are emitted as `{available:false, reason:"..."}` stubs so the
dashboard can render a "Connect this source" card without crashing.

Sources wired now (using existing keys):
  - Mailchimp: daily signups+unsubs (180d), recent campaigns (12) w/ stats,
               rating distribution, top email domains, total subscribers
  - Ghost:     posts (last 90d), publish cadence
  - Google News RSS: items mentioning "Vital City" + NYC disambiguator
  - Reddit RSS:      threads mentioning "Vital City" NYC

Stubbed for next iteration (need creds):
  - GA4 (service-account JSON + property id)
  - Google Search Console (service-account or OAuth refresh token)
  - X (@vitalcitynyc) — needs paid API or paid mention service
  - Instagram (@vitalcitynyc) — needs FB business token

Output: private/growth.json (consumed by encrypt_growth.py).
"""
from __future__ import annotations
import base64, hashlib, hmac, html as html_mod, json, os, re, sys, time, urllib.parse, urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
PRIV = ROOT / "private"
OUT  = PRIV / "growth.json"

UA = "VitalCityGrowthDashboard/1.0 (+https://www.vitalcitynyc.org)"


def log(msg): print(msg, file=sys.stderr)


def http_get(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ---------------------------------------------------------------- Mailchimp
def mailchimp_key():
    k = os.environ.get("MAILCHIMP_KEY")
    if k: return k.strip()
    f = PRIV / ".mailchimp_key"
    return f.read_text().strip() if f.exists() else ""


def variate_children(cid, key, dc, on_error=None):
    """The report IDs that actually hold an A/B campaign's data.

    An A/B (variate) campaign is secretly four campaigns: the parent, two
    test-variant sends and a winner send. Every per-member and per-link report
    on the PARENT comes back empty -- open-details, click-details, sent-to
    (all open_counts zero), email-activity (activity arrays empty), even
    /unsubscribed. The same reports on the hidden children carry everything;
    verified live on the Aug 20 2026 send, where the parent reported zero
    members and the children held 4,301 open records, 42 clicked links and 7
    unsubscribes. Mailchimp has to know -- picking a winner requires it -- it
    just files the knowledge under IDs the UI never shows.

    Returns [] for a non-variate campaign or on failure, so callers can fall
    back to the parent id unchanged."""
    try:
        vs = mc_get(f"/campaigns/{cid}?fields=variate_settings,type", key, dc)
    except Exception as e:
        if on_error: on_error(f"variate-children {cid}: {e}")
        return []
    v = vs.get("variate_settings") or {}
    kids = [x.get("id") for x in (v.get("combinations") or []) if x.get("id")]
    if v.get("winning_campaign_id"):
        kids.append(v["winning_campaign_id"])
    return kids


def report_ids(camp_id, camp_type, key, dc, on_error=None):
    """The IDs to query reports on: the campaign itself, or -- for an A/B
    send -- its children, falling back to the parent if they can't be read."""
    if camp_type == "variate":
        kids = variate_children(camp_id, key, dc, on_error)
        return kids or [camp_id]
    return [camp_id]


def mc_get(path, key, dc):
    url = f"https://{dc}.api.mailchimp.com/3.0{path}"
    auth = base64.b64encode(f"anystring:{key}".encode()).decode()
    return json.loads(http_get(url, headers={"Authorization": "Basic " + auth}, timeout=120))


# Fundraising-appeal detection. Donation-language keywords, validated against the
# full Mailchimp export (June 2026): this set catches all 12 genuine appeals with
# no false positives. Avoid bare "contribut" (hits editorial "contributor") and
# "match"/"sustain" (hit "matchup"/"sustainability"). Everything else on the
# regular Thursday cadence is "newsletter"; the rest is "other".
_FUND_RE = re.compile(
    r"donat|\bgift\b|double your|generous|your support|support evidence|"
    r"building vital|year[- ]?end|fundrais|tax-deduct|chip in|"
    r"make a gift|personal request|membership drive|"
    # Seasonal drives use soft subject lines with NO donation keywords, so they
    # are matched explicitly. The Spring 2026 drive below was confirmed against
    # the Donorbox "Spring 2026" campaign gift dates (e.g. 18 gifts on 5/12 ==
    # "New mayor. New moment."). Add new drives here as they run.
    r"new mayor.{0,3}new moment|this is why we exist|"
    r"what people who shape cities need|best ideas need the best people", re.I)

def _campaign_kind(entry):
    txt = " ".join([entry.get("subject") or "", entry.get("winner_subject") or ""]
                   + (entry.get("variate_subjects") or []))
    if _FUND_RE.search(txt):
        return "appeal"
    try:
        from datetime import date as _d
        if _d.fromisoformat((entry.get("sent") or "")).weekday() == 3:  # Thursday
            return "newsletter"
    except Exception:
        pass
    return "other"


def pull_mailchimp():
    key = mailchimp_key()
    if not key:
        return {"available": False, "reason": "MAILCHIMP_KEY not set"}
    dc = key.split("-")[-1]
    list_id = os.environ.get("MAILCHIMP_LIST", "ec30bf0c4b")
    out: dict = {"available": True}

    # List summary (total subs + recent stats)
    try:
        lst = mc_get(f"/lists/{list_id}", key, dc)
        out["total_subscribers"] = lst.get("stats", {}).get("member_count", 0)
        out["unsubscribed_total"] = lst.get("stats", {}).get("unsubscribe_count", 0)
        out["avg_open_rate"]  = round((lst.get("stats", {}).get("open_rate")  or 0), 2)
        out["avg_click_rate"] = round((lst.get("stats", {}).get("click_rate") or 0), 2)
    except Exception as e:
        log(f"  mailchimp list summary failed: {e}")

    # Growth history — monthly *cumulative* sub + unsub counts going back ~58
    # months. From this we can derive REAL monthly new-signup counts (the
    # /activity feed is unreliable because it only sees direct-MC-form
    # signups, and the actual site cutover to Ghost was April 2026 — most
    # 2025 signups went through the prior Prismic-hosted form into Mailchimp
    # directly). Formula: new_signups[m] = (subs[m] - subs[m-1]) +
    # (unsubs[m] - unsubs[m-1]). Captures everyone added to the list,
    # whether through Ghost reconcile, MC form, or manual import.
    try:
        gh = mc_get(f"/lists/{list_id}/growth-history?count=72&sort_field=month&sort_dir=ASC",
                    key, dc).get("history", [])
        monthly_signups = []
        prev_subs = prev_unsubs = None
        for h in gh:
            month = h.get("month") or ""
            subs   = int(h.get("subscribed") or 0)
            unsubs = int(h.get("unsubscribed") or 0)
            if prev_subs is None:
                new = subs   # first month — total subs is the count of signups so far
            else:
                new = (subs - prev_subs) + (unsubs - prev_unsubs)
            monthly_signups.append({"month": month, "new_signups": max(0, new),
                                    "cum_subs": subs, "cum_unsubs": unsubs})
            prev_subs, prev_unsubs = subs, unsubs
        out["monthly_signups"] = monthly_signups
    except Exception as e:
        log(f"  mailchimp growth-history failed: {e}")
        out["monthly_signups"] = []

    # Daily activity (subs/unsubs by day) — last 180 days.
    # Subs: computed from people.json `since` dates (Mailchimp's /activity endpoint
    # only counts direct-MC-form signups and grossly understates the real number
    # because most VC signups arrive via the Ghost signup form). Unsubs/opens/clicks
    # are taken from Mailchimp /activity which is accurate for those.
    rows_by_day = {}
    try:
        # 730 days = 2 years, enough for year-over-year comparisons on
        # unsubscribes / opens / clicks (these signals are reliable in Mailchimp).
        act = mc_get(f"/lists/{list_id}/activity?count=730", key, dc).get("activity", [])
        for a in act:
            d = a.get("day")
            if not d: continue
            rows_by_day[d] = {
                "d": d, "subs": 0,
                "unsubs": int(a.get("unsubs") or 0),
                "opens":  int(a.get("unique_opens") or 0),
                "clicks": int(a.get("recipient_clicks") or 0),
            }
    except Exception as e:
        log(f"  mailchimp activity failed: {e}")

    # Overlay accurate signup counts from people.json (canonical merged dataset)
    pj = PRIV / "people.json"
    if pj.exists():
        try:
            people = json.loads(pj.read_text())
            today = datetime.now(timezone.utc).date()
            cutoff = (today - timedelta(days=180)).isoformat()
            for p in people:
                # Count a signup for anyone who joined the newsletter in-window,
                # dated by their real Ghost signup date (`since`) — INCLUDING people
                # who have since unsubscribed. Gross signups, not net-of-churn, so a
                # past window's count stays stable and agrees with the weekly report.
                # (Unsubscribes are tracked separately, on their Mailchimp date. The
                # Thursday Ghost→Mailchimp batch doesn't matter here: `since` is the
                # real Ghost signup date, never the Mailchimp add date.)
                if not (p.get("mem") or p.get("unsub")): continue
                s = (p.get("since") or "")[:10]
                if not s or s < cutoff: continue
                row = rows_by_day.setdefault(s, {"d": s, "subs": 0, "unsubs": 0, "opens": 0, "clicks": 0})
                row["subs"] += 1
        except Exception as e:
            log(f"  people.json overlay failed: {e}")

    out["daily_activity"] = sorted(rows_by_day.values(), key=lambda r: r["d"])

    # Signup + unsubscribe windows (YTD and YoY).
    # The Ghost subscription started Jan 2025 but the actual SITE cutover
    # (vitalcitynyc.org moving from Prismic to Ghost) was April 2026. So:
    #  - 2025 signups were captured via the prior site/Mailchimp form
    #  - 2026 signups (April onward) come via Ghost's form
    # Mailchimp's growth-history reflects all subscriber adds regardless of
    # which form they came through, so it's the YoY-fair source.
    from datetime import date as _date
    today = datetime.now(timezone.utc).date()
    y = today.year
    GHOST_CUTOVER = _date(2026, 4, 1)  # vitalcitynyc.org site moved to Ghost

    def _sum(rows, start, end, key):
        s, e = start.isoformat(), end.isoformat()
        return sum(int(r.get(key) or 0) for r in rows if s <= r["d"] <= e)

    rows = out["daily_activity"]
    # Gross signups by month, straight from the real-time Ghost `since` dates (the
    # daily_activity overlay). This is the "true signups" count that's current
    # through today; the Mailchimp new_signups above is net of churn and lags the
    # Thursday Ghost->Mailchimp batch. Attach both so the dashboard can show them.
    from collections import Counter as _Cg
    _gm = _Cg()
    for r in rows:
        _gm[(r.get("d") or "")[:7]] += int(r.get("subs") or 0)
    for m in (out.get("monthly_signups") or []):
        m["ghost_signups"] = _gm.get(m.get("month"), 0)
    ytd_start  = _date(y, 1, 1);   ytd_end = today
    py_start   = _date(y-1, 1, 1); py_end  = _date(y-1, today.month, today.day)

    # Derive YTD signup counts from Mailchimp's growth-history (cumulative
    # subscribers per month — the most complete record of net additions
    # regardless of which form the signup came through). For both current
    # and prior year, sum new_signups Jan→current_month.
    def _ytd_signups(monthly, year, through_month):
        return sum(int(m.get("new_signups") or 0) for m in monthly
                   if (m.get("month") or "").startswith(f"{year}-")
                   and (m.get("month") or "")[5:7] <= f"{through_month:02d}")
    monthly = out.get("monthly_signups") or []
    sig_ytd       = _ytd_signups(monthly, today.year,     today.month)
    sig_prior_ytd = _ytd_signups(monthly, today.year - 1, today.month)
    out["signup_windows"] = {
        "ytd":              sig_ytd,
        "prior_ytd":        sig_prior_ytd,
        "prior_ytd_ok":     sig_prior_ytd > 0,
        "prior_ytd_source": "Mailchimp growth-history (monthly subscriber counts) — counts every net addition to the list, whether the signup came in via Ghost form, the MC form, or a manual import.",
        "ghost_cutover":    GHOST_CUTOVER.isoformat(),
        # Gross, real-time signups straight from Ghost `since` dates. Current
        # through today (no Thursday-batch lag), not netted against churn.
        "ytd_ghost":        _sum(rows, ytd_start, ytd_end, "subs"),
        "last30_ghost":     _sum(rows, today - timedelta(days=30), today, "subs"),
        "last7_ghost":      _sum(rows, today - timedelta(days=7),  today, "subs"),
        "ghost_source":     "Ghost member signup dates (organic form signups), counted the day they happened. Current through today; not reduced by later unsubscribes and does not include manual bulk imports.",
        "unsub_ytd":        _sum(rows, ytd_start, ytd_end, "unsubs"),
        "unsub_prior_ytd":  _sum(rows, py_start,  py_end,  "unsubs"),
    }

    # ALL sent campaigns ever — we use the full history for monthly trend lines
    # (Mailchimp goes back to ~March 2022) and for YoY comparisons. The recent‑12
    # table on the dashboard just slices the newest ones.
    try:
        camp = mc_get(
            f"/campaigns?status=sent&list_id={list_id}&count=500"
            f"&sort_field=send_time&sort_dir=DESC&fields=campaigns.id,campaigns.type,"
            f"campaigns.settings.subject_line,campaigns.send_time,campaigns.emails_sent,"
            f"campaigns.report_summary,campaigns.variate_settings.subject_lines,"
            f"campaigns.variate_settings.winning_combination_id,"
            f"campaigns.variate_settings.combinations,campaigns.variate_settings.test_size",
            key, dc).get("campaigns", [])

        # report_summary does NOT carry unsubscribes; the /reports list does.
        # One paginated call maps campaign id -> total unsubscribes + DELIVERED
        # count (emails_sent minus hard/soft bounces). Unsubscribe rates use
        # delivered as the denominator (the people who actually got the email).
        unsub_by_id, deliv_by_id = {}, {}
        try:
            reps = mc_get(f"/reports?count=1000&fields=reports.id,reports.unsubscribed,"
                          f"reports.emails_sent,reports.bounces", key, dc).get("reports", [])
            for r in reps:
                rid = r.get("id")
                unsub_by_id[rid] = int(r.get("unsubscribed") or 0)
                es = int(r.get("emails_sent") or 0)
                bo = r.get("bounces") or {}
                bounced = int(bo.get("hard_bounces") or 0) + int(bo.get("soft_bounces") or 0)
                deliv_by_id[rid] = max(0, es - bounced)
        except Exception as e:
            log(f"  Mailchimp /reports (unsubscribes/bounces) fetch failed: {e}")

        camp_out = []
        for c in camp:
            rs = c.get("report_summary") or {}
            # Variate (A/B) campaigns leave settings.subject_line empty on the
            # parent — the variant subjects live in variate_settings.subject_lines.
            # Pick those up so the dashboard doesn't show "(no subject)".
            # IMPORTANT on A/B numbers: emails_sent and report_summary on the
            # parent already aggregate ALL waves — both test groups AND the
            # winner rollout (which Mailchimp sends as a hidden child campaign
            # that never appears in this list). Verified against campaign
            # 41736c4a01 (2026-06-04): parent 8,333 sends = 2×2,083 test +
            # 4,167 winner; parent opens 3,345 = 1,660 test + 1,685 winner.
            # So one row per A/B is complete — we just surface the winner.
            variate_subjects, winner_subject, test_n = [], "", 0
            ab_combos = []
            if c.get("type") == "variate":
                vs = c.get("variate_settings") or {}
                variate_subjects = vs.get("subject_lines") or []
                combos = vs.get("combinations") or []
                test_n = sum(int(cb.get("recipients") or 0) for cb in combos)
                win_id = vs.get("winning_combination_id")
                for cb in combos:
                    if cb.get("id") == win_id:
                        si = cb.get("subject_line")
                        if isinstance(si, int) and 0 <= si < len(variate_subjects):
                            winner_subject = variate_subjects[si]
                        break
                # Per-variant capture. The combination object's stat fields aren't
                # documented consistently for 'variate' campaigns, so grab the
                # subject + EVERY numeric field present — one rebuild then reveals
                # whether per-variant opens/clicks are available or only recipients.
                for cb in combos:
                    si = cb.get("subject_line")
                    subj = variate_subjects[si] if isinstance(si, int) and 0 <= si < len(variate_subjects) else str(si)
                    row = {"subject": subj, "winner": cb.get("id") == win_id}
                    for fk, fv in cb.items():
                        if fk not in ("id", "subject_line") and isinstance(fv, (int, float)):
                            row[fk] = fv
                    ab_combos.append(row)
            op = (rs.get("open_rate")  or 0) * 100
            cl = (rs.get("click_rate") or 0) * 100
            ctor = (cl / op * 100) if op > 0 else 0
            sent_to = c.get("emails_sent") or 0
            entry = {
                "id":       c.get("id"),
                "subject":  (c.get("settings") or {}).get("subject_line", ""),
                "variate_subjects": variate_subjects,
                "winner_subject": winner_subject,
                "test_n":   test_n,
                "winner_n": max(0, sent_to - test_n) if test_n else 0,
                "type":     c.get("type") or "regular",
                "sent":     (c.get("send_time") or "")[:10],
                "sent_to":  sent_to,
                "delivered": deliv_by_id.get(c.get("id"), sent_to),  # sent − bounces
                "open_pct": round(op, 1),
                "click_pct":round(cl, 1),
                "ctor_pct": round(ctor, 1),   # click-to-open ratio = honest engagement
                "unsubs":   unsub_by_id.get(c.get("id"), 0),  # exact, from /reports
                "ab_combos": ab_combos,  # per-variant subject + stats (variate only)
            }
            entry["kind"] = _campaign_kind(entry)  # appeal | newsletter | other
            camp_out.append(entry)
        # Sort by send date ourselves rather than trusting the API's ordering.
        # The request asks for sort_field=send_time&sort_dir=DESC, and Mailchimp
        # honours it only partly: the live payload came back newest-first for 68
        # campaigns (2026-08-27 down to 2025-12-17) and then jumped to 2022-03-02
        # and ran oldest-first for the remaining 200. Every consumer here assumes
        # newest-first -- the send-by-send chart takes the first 20 and reverses
        # them, the table slices the first 12 -- so the dashboard showed a
        # December 2025 send followed by three from 2022.
        camp_out.sort(key=lambda e: e.get("sent") or "", reverse=True)
        out["campaigns"] = camp_out

        # ---- Per-LINK clicks: which piece in each send actually got clicked.
        # Campaign-level click rate tells you a send worked; /click-details tells
        # you WHICH article did the work. Aggregated by article slug across sends,
        # so the piece look-up can show "newsletter clicks" alongside web traffic.
        # Capped to the most recent CLICK_SENDS campaigns — one API call each, and
        # older sends add little (the list was a fraction of today's size).
        try:
            CLICK_SENDS = 120
            recent = [e for e in sorted(camp_out, key=lambda e: e.get("sent") or "", reverse=True)
                      if e.get("sent")][:CLICK_SENDS]
            by_slug, n_ok = {}, 0
            for e in recent:
                # A/B parents report zero clicked links; the children hold them
                # (see variate_children). Merge across the child sends.
                det = []
                for rid in report_ids(e["id"], e.get("type"), key, dc):
                    try:
                        det += mc_get(f"/reports/{rid}/click-details?count=1000"
                                      f"&fields=urls_clicked.url,urls_clicked.total_clicks,"
                                      f"urls_clicked.unique_clicks", key, dc).get("urls_clicked", [])
                    except Exception:
                        continue
                if not det:
                    continue
                n_ok += 1
                for u in det:
                    url = (u.get("url") or "").split("?")[0]
                    if "vitalcitynyc.org" not in url:
                        continue                       # skip social/donate/footer links
                    sl = url.rstrip("/").rsplit("/", 1)[-1].lower()
                    if not sl or sl in ("vitalcitynyc.org", "www.vitalcitynyc.org"):
                        continue
                    r = by_slug.setdefault(sl, {"clicks": 0, "unique": 0, "sends": 0})
                    r["clicks"] += int(u.get("total_clicks") or 0)
                    r["unique"] += int(u.get("unique_clicks") or 0)
                    r["sends"] += 1
            out["link_clicks"] = {"available": True, "sends_scanned": n_ok,
                                  "cap": CLICK_SENDS, "n_slugs": len(by_slug),
                                  "by_slug": by_slug,
                                  "window_start": min((e.get("sent") or "") for e in recent) if recent else ""}
            log(f"  mailchimp link clicks: {len(by_slug)} article URLs across {n_ok} sends")
        except Exception as e:
            log(f"  mailchimp link-clicks pull failed: {e}")
            out["link_clicks"] = {"available": False, "reason": str(e)}

        # Unsubscribe reasons by campaign kind (appeal vs newsletter vs other).
        # /reports/{id}/unsubscribed carries each unsub's optional reason. We
        # aggregate to COUNTS ONLY (never store emails). Mailchimp's fixed radio
        # reasons recur hundreds of times identically; a member's free-text note is
        # essentially never repeated — so we keep any reason that recurs >=5 times
        # VERBATIM (those are the canonical options, whatever their exact wording)
        # and bucket the rest as "Other (free text)" so no personal content leaks.
        try:
            from collections import Counter as _Counter
            _cut = (datetime.now(timezone.utc).date() - timedelta(days=365)).isoformat()
            NONE = "No reason given"
            raw_by_kind = {"appeal": _Counter(), "newsletter": _Counter(), "other": _Counter()}
            for e in camp_out:
                if (e.get("sent") or "") < _cut or not e.get("unsubs"):
                    continue
                kind = e.get("kind", "other")
                # A/B parents report an unsub COUNT but an empty unsub LIST;
                # the reasons live on the child sends (see variate_children).
                us = []
                for rid in report_ids(e["id"], e.get("type"), key, dc):
                    try:
                        us += mc_get(f"/reports/{rid}/unsubscribed?count=200"
                                     f"&fields=unsubscribes.reason", key, dc).get("unsubscribes", [])
                    except Exception:
                        continue
                for u in us:
                    raw = (u.get("reason") or "").strip()
                    raw_by_kind[kind][raw or NONE] += 1
            glob = _Counter()
            for c in raw_by_kind.values():
                glob.update(c)
            canon = {s for s, n in glob.items() if s != NONE and n >= 5}
            def _label(s):
                return NONE if s == NONE else (s if s in canon else "Other (free text)")
            reasons = {}
            for kind, c in raw_by_kind.items():
                agg = _Counter()
                for s, n in c.items():
                    agg[_label(s)] += n
                reasons[kind] = dict(agg)
            out["unsub_reasons"] = reasons
        except Exception as e:
            log(f"  unsubscribe reasons pull failed: {e}")

        # Monthly aggregates (recipient‑weighted rates) for the trend chart
        from collections import defaultdict as _dd
        mo = _dd(lambda: {"sends": 0, "recipients": 0, "wt_open": 0.0, "wt_click": 0.0, "unsubs": 0})
        for c in camp:
            sent = c.get("emails_sent") or 0
            rs = c.get("report_summary") or {}
            m = (c.get("send_time") or "")[:7]
            if not m: continue
            mo[m]["sends"] += 1
            mo[m]["recipients"] += sent
            mo[m]["wt_open"]  += float(rs.get("open_rate")  or 0) * sent
            mo[m]["wt_click"] += float(rs.get("click_rate") or 0) * sent
            mo[m]["unsubs"]   += unsub_by_id.get(c.get("id"), 0)
        monthly = []
        for m in sorted(mo):
            r = mo[m]
            recs = r["recipients"] or 1
            op_pct = (r["wt_open"]  / recs) * 100
            cl_pct = (r["wt_click"] / recs) * 100
            monthly.append({
                "month": m,
                "sends": r["sends"],
                "recipients": r["recipients"],
                "open_pct":  round(op_pct, 1),
                "click_pct": round(cl_pct, 1),
                "ctor_pct":  round((cl_pct / op_pct) * 100, 1) if op_pct > 0 else 0,
                "unsubs":    r["unsubs"],
            })
        out["monthly_campaigns"] = monthly

        # Period buckets: window stats and YoY comparisons (Mailchimp send data
        # goes back to ~2022, so this is reliable for newsletter performance).
        # Signup YoY uses Mailchimp growth-history (canonical regardless of
        # which front-end form fed the list — Prismic pre-cutover, Ghost after).
        from datetime import date as _date
        today = datetime.now(timezone.utc).date()
        y, _ = today.year, today.month

        def _agg(items):
            recs = sum(i.get("emails_sent") or 0 for i in items)
            wt_open  = sum(float((i.get("report_summary") or {}).get("open_rate")  or 0) * (i.get("emails_sent") or 0) for i in items)
            wt_click = sum(float((i.get("report_summary") or {}).get("click_rate") or 0) * (i.get("emails_sent") or 0) for i in items)
            op_pct = (wt_open  / recs) * 100 if recs else 0
            cl_pct = (wt_click / recs) * 100 if recs else 0
            return {
                "sends": len(items),
                "recipients": recs,
                "open_pct":  round(op_pct, 1),
                "click_pct": round(cl_pct, 1),
                "ctor_pct":  round((cl_pct / op_pct) * 100, 1) if op_pct > 0 else 0,
                "unsubs":    sum(int((i.get("report_summary") or {}).get("unsubscribed") or 0) for i in items),
            }

        def _in(items, start, end):
            return [c for c in items if start.isoformat() <= (c.get("send_time") or "")[:10] <= end.isoformat()]

        ytd_start  = _date(y, 1, 1); ytd_end = today
        py_start   = _date(y-1, 1, 1); py_end = _date(y-1, today.month, today.day)
        last30_end = today; last30_start = today - timedelta(days=30)
        prev30_end = last30_start - timedelta(days=1); prev30_start = prev30_end - timedelta(days=30)
        yoy30_end  = _date(y-1, today.month, today.day); yoy30_start = yoy30_end - timedelta(days=30)

        out["windows"] = {
            "ytd":         _agg(_in(camp, ytd_start,  ytd_end)),
            "prior_ytd":   _agg(_in(camp, py_start,   py_end)),
            "last_30":     _agg(_in(camp, last30_start, last30_end)),
            "prev_30":     _agg(_in(camp, prev30_start, prev30_end)),
            "yoy_30":      _agg(_in(camp, yoy30_start,  yoy30_end)),
        }
    except Exception as e:
        log(f"  mailchimp campaigns failed: {e}")
        out["campaigns"] = []
        out["monthly_campaigns"] = []
        out["windows"] = {}

    # Rating distribution + top email domains — read from cached engagement CSV
    eng = PRIV / "engagement_source.csv"
    rating, domains = Counter(), Counter()
    open_buckets = Counter()    # 0-25-50-75-100 open-rate bands
    if eng.exists():
        import csv
        with open(eng) as f:
            r = csv.DictReader(f)
            for row in r:
                em = (row.get("Email") or "").lower().strip()
                if "@" in em: domains[em.split("@", 1)[1]] += 1
                try: rating[int(row.get("Rating") or 0)] += 1
                except: pass
                try:
                    op = int(row.get("Open Rate") or 0)
                    if op == 0: open_buckets["0%"] += 1
                    elif op <= 25: open_buckets["1-25%"] += 1
                    elif op <= 50: open_buckets["26-50%"] += 1
                    elif op <= 75: open_buckets["51-75%"] += 1
                    else:          open_buckets["76-100%"] += 1
                except: pass
    out["rating_dist"]  = {str(k): rating[k] for k in sorted(rating)}
    out["open_buckets"] = {k: open_buckets[k] for k in ["0%", "1-25%", "26-50%", "51-75%", "76-100%"]}
    out["top_domains"]  = [{"d": d, "n": n} for d, n in domains.most_common(12)]
    out["engaged_share"] = round(((rating.get(4, 0) + rating.get(5, 0)) / max(sum(rating.values()), 1)), 3)

    # ---- Monthly + annual active users ---------------------------------
    # Real number, not a proxy: the UNION of unique email addresses that
    # opened at least one regular (non-A/B) send in the window.
    # Cost: one extra API call per campaign + ~1 per 1000 openers (pagination).
    # ~30-90 calls for the year — under a minute.
    # (A count-only _union_openers predecessor lived here; it was never
    # called and still excluded A/B sends, so it was removed when the child-
    # send fix landed rather than left as a trap.)
    # Re-issue the pulls but capture the sets too (re-uses Mailchimp data we
    # already paid the API cost for; about doubles AAU pull time, but worth
    # it since lifecycle is the single highest-leverage analysis).
    def _union_openers_with_set(days_back):
        since_iso = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        url = (f"/campaigns?status=sent&list_id={list_id}"
               f"&since_send_time={urllib.parse.quote(since_iso)}"
               f"&count=500&sort_field=send_time&sort_dir=DESC"
               f"&fields=campaigns.id,campaigns.type,campaigns.send_time")
        try:
            cs = mc_get(url, key, dc).get("campaigns", [])
        except Exception as e:
            log(f"  lifecycle openers ({days_back}d) failed: {e}"); return (set(), {})
        regulars = [c for c in cs if c.get("type") == "regular"]
        variate  = [c for c in cs if c.get("type") == "variate"]
        # Count openers from BOTH regular and A/B (variate) sends. Excluding
        # variate zeroed out active-subscriber counts whenever a window happened
        # to contain only A/B sends — which is exactly what broke the 30-day
        # tile (all four recent newsletters were A/B tests). The open-details
        # report works on the variate parent campaign's id and returns the union
        # of openers across its variants, deduped by email here anyway.
        usable = regulars + variate
        openers = set()
        problems = []

        def _open_details(cid, diag=False):
            """Openers via /open-details. Returns None if the endpoint errors
            (so the caller can fall back), else a set (possibly empty)."""
            got, offset = set(), 0
            while True:
                try:
                    page = mc_get(
                        f"/reports/{cid}/open-details?count=1000&offset={offset}"
                        f"&fields=members.email_address,total_items", key, dc)
                except Exception as e:
                    # NEVER swallow this silently — a hidden failure here is what
                    # made the 30-day active count read 0 while looking healthy.
                    problems.append(f"open-details {cid}: {e}")
                    return None
                rows = page.get("members", [])
                if diag and offset == 0:
                    problems.append(f"DIAG open-details {cid}: total_items={page.get('total_items')} "
                                    f"rows={len(rows)} keys={sorted(page.keys())}")
                for m in rows:
                    em = (m.get("email_address") or "").lower().strip()
                    if em: got.add(em)
                total = int(page.get("total_items") or 0)
                offset += 1000
                if offset >= total: break
            return got

        def _sent_to(cid, diag=False):
            """Openers via /reports/{id}/sent-to, which lists every recipient
            with an `open_count`. This is what works for A/B (variate) sends:
            open-details returns total_items=0 for a variate parent, and
            email-activity returns the 8,500 recipient rows with their activity
            arrays empty — but sent-to carries open_count per member."""
            got, offset = set(), 0
            while True:
                try:
                    page = mc_get(
                        f"/reports/{cid}/sent-to?count=1000&offset={offset}"
                        f"&fields=sent_to.email_address,sent_to.open_count,total_items", key, dc)
                except Exception as e:
                    problems.append(f"sent-to {cid}: {e}")
                    return got
                rows = page.get("sent_to", [])
                if diag and offset == 0:
                    problems.append(f"DIAG sent-to {cid}: total_items={page.get('total_items')} "
                                    f"rows={len(rows)} row_keys={sorted(rows[0].keys()) if rows else 'NONE'} "
                                    f"opened_in_first_page={sum(1 for m in rows if (m.get('open_count') or 0) > 0)}")
                for m in rows:
                    if (m.get("open_count") or 0) > 0:
                        em = (m.get("email_address") or "").lower().strip()
                        if em: got.add(em)
                total = int(page.get("total_items") or 0)
                offset += 1000
                if offset >= total: break
            return got

        def _email_activity(cid, diag=False):
            """Fallback for A/B (variate) campaigns. NOTE: do not use a nested
            `fields=emails.activity.action` selector — Mailchimp returns the
            rows with `activity` empty, which silently yields zero openers.
            Request `emails.activity` whole instead."""
            got, offset = set(), 0
            while True:
                try:
                    page = mc_get(
                        f"/reports/{cid}/email-activity?count=1000&offset={offset}"
                        f"&fields=emails.email_address,emails.activity,total_items",
                        key, dc)
                except Exception as e:
                    problems.append(f"email-activity {cid}: {e}")
                    return got
                rows = page.get("emails", [])
                if diag and offset == 0:
                    # Structural only — never log email addresses.
                    acts = set()
                    for m in rows[:50]:
                        for a in (m.get("activity") or []): acts.add(str(a.get("action")))
                    problems.append(f"DIAG email-activity {cid}: total_items={page.get('total_items')} "
                                    f"rows={len(rows)} keys={sorted(page.keys())} actions_seen={sorted(acts) or 'NONE'}")
                for m in rows:
                    if any((a.get("action") or "") == "open" for a in (m.get("activity") or [])):
                        em = (m.get("email_address") or "").lower().strip()
                        if em: got.add(em)
                total = int(page.get("total_items") or 0)
                offset += 1000
                if offset >= total: break
            return got

        diag_done = False
        for c in usable:
            cid, ctype = c["id"], c.get("type")
            want_diag = (ctype == "variate" and not diag_done)
            if ctype == "variate":
                # Go straight to the children -- the parent's open-details is
                # always empty, and sent-to/email-activity on the parent return
                # recipients with all activity zeroed.
                got = set()
                kids = variate_children(cid, key, dc, problems.append)
                for kid in kids:
                    kg = _open_details(kid)
                    if kg: got |= kg
                if got:
                    problems.append(f"{cid} (variate): {len(got)} openers via {len(kids)} child sends")
                else:
                    got = _sent_to(cid, diag=want_diag) or _email_activity(cid, diag=want_diag)
                    if got: problems.append(f"{cid} (variate): recovered {len(got)} via parent fallback")
                if want_diag: diag_done = True
            else:
                got = _open_details(cid, diag=want_diag)
                if got is None or not got:
                    got = _sent_to(cid, diag=want_diag)
                    if got:
                        problems.append(f"{cid} ({ctype}): recovered {len(got)} via sent-to")
                    else:
                        got = _email_activity(cid, diag=want_diag)
                        if got: problems.append(f"{cid} ({ctype}): recovered {len(got)} via email-activity")
            if not got:
                problems.append(f"{cid} ({ctype}, sent {c.get('send_time','?')[:10]}): 0 openers")
            openers |= got
        for p in problems[:12]:
            log(f"    opener-pull: {p}")
        return (openers, {"regulars_counted": len(regulars), "variate_counted": len(variate),
                          "campaigns_in_window": len(cs), "issues": len(problems)})

    log("  computing MAU (30d) openers set…")
    mau_set, mau_meta = _union_openers_with_set(30)
    log("  computing AAU (365d) openers set…")
    aau_set, aau_meta = _union_openers_with_set(365)
    out["mau"] = {"active_users": len(mau_set), **mau_meta}
    out["aau"] = {"active_users": len(aau_set), **aau_meta}
    # Stash the sets internally so build_lifecycle() can use them
    out["_mau_set"] = mau_set
    out["_aau_set"] = aau_set
    return out


def _last_name_key(row):
    """Sort key for subset lists in the dashboard: alphabetical by last name,
    then first. Mirrors how the Contact tool sorts by last name — split on
    whitespace, the last token is the last name. Single-word names sort by
    the whole thing. Empty names sort last."""
    n = (row.get("name") or "").strip()
    if not n: return ("zzz", "")
    parts = n.split()
    if len(parts) == 1:
        return (parts[0].lower(), "")
    return (parts[-1].lower(), " ".join(parts[:-1]).lower())


def build_engagement_extras(mc, signup_attr, donorbox):
    """Four newer metrics that publishers (Atlantic, Pico-using sites, Stratechery)
    use to get past Apple Mail privacy noise:
      - mailbox_engagement: avg open rate by email provider (gmail vs Apple vs ...)
      - power_readers: top decile of Mailchimp rating + open rate (your reliable readers)
      - channel_ltv: subscriber→donor conversion by acquisition source
      - influence_weighted_reach: opens weighted by reader importance
                                  (rating + Wikipedia "notable" + government domain)
    All from data we already pull. Heuristic but defensible.
    """
    import csv
    out = {"available": True}
    eng_path = PRIV / "engagement_source.csv"
    pj_path  = PRIV / "people.json"

    # ---- 1. Mailbox-provider engagement -----------------------------------
    provider_buckets = {
        "Gmail":         ["gmail.com", "googlemail.com"],
        "Yahoo":         ["yahoo.com", "ymail.com", "rocketmail.com"],
        "Apple iCloud":  ["icloud.com", "me.com", "mac.com"],
        "Microsoft":     ["hotmail.com", "outlook.com", "live.com", "msn.com"],
        "AOL":           ["aol.com"],
        "Comcast":       ["comcast.net"],
        "Government":    [".gov"],    # any .gov subdomain
        "Academic":      [".edu"],
        "Other":         None,        # catch-all
    }
    def _provider(email):
        em = (email or "").lower()
        for label, doms in provider_buckets.items():
            if doms is None: continue
            for d in doms:
                if d.startswith("."):
                    if em.endswith(d) or f"{d}." in em: return label
                elif em.endswith("@"+d):
                    return label
        return "Other"

    by_prov = {label: {"label": label, "subs": 0, "wt_open_sum": 0.0} for label in provider_buckets}

    # ---- 2. Power readers ------------------------------------------------
    # Top-decile readers by composite engagement score: rating × 20 + open_rate
    rows = []     # (email, rating, open_rate)
    if eng_path.exists():
        with open(eng_path) as f:
            for r in csv.DictReader(f):
                em = (r.get("Email") or "").lower().strip()
                if not em: continue
                try:
                    rating = int(r.get("Rating") or 0)
                    op     = int(r.get("Open Rate") or 0)
                except: continue
                rows.append((em, rating, op))
                prov = _provider(em)
                by_prov[prov]["subs"] += 1
                by_prov[prov]["wt_open_sum"] += op   # avg of per-member rates within provider

    for d in by_prov.values():
        d["avg_open_pct"] = round(d["wt_open_sum"]/d["subs"], 1) if d["subs"] else 0
        d.pop("wt_open_sum")
    # Sort by sub count descending; drop empty
    out["mailbox_engagement"] = [d for d in sorted(by_prov.values(), key=lambda x: -x["subs"]) if d["subs"] >= 5]

    # Power-readers: composite score, take top 10%
    if rows:
        scored = sorted(rows, key=lambda r: -(r[1] * 20 + r[2]))
        decile = max(1, len(scored) // 10)
        power = scored[:decile]
        # Provider breakdown of power readers
        pp = {}
        for em, _, _ in power:
            p = _provider(em)
            pp[p] = pp.get(p, 0) + 1
        out["power_readers"] = {
            "count": len(power),
            "as_pct_of_list": round((len(power) / max(len(rows), 1)) * 100, 1),
            "by_provider": [{"label": p, "n": n} for p, n in sorted(pp.items(), key=lambda kv: -kv[1])],
            "avg_open_pct": round(sum(r[2] for r in power) / len(power), 1) if power else 0,
            "avg_rating":   round(sum(r[1] for r in power) / len(power), 2) if power else 0,
        }
    else:
        out["power_readers"] = {"count": 0, "by_provider": [], "as_pct_of_list": 0,
                                "avg_open_pct": 0, "avg_rating": 0}

    # ---- 3. Channel LTV (subscriber → donor by acquisition source) -------
    # For each acquisition source recorded in Ghost's signup attribution,
    # find which subscribers became donors (join via email) and sum their
    # donation totals. Caveat: limited to subscribers whose signup events
    # are in the Ghost feed (post April-2026 site cutover, ~800 signups
    # captured here). Pre-cutover donors won't have a source we can attribute.
    src_email = (signup_attr or {}).get("_by_email") or {}
    if src_email and pj_path.exists():
        try:
            people_for_ltv = json.loads(pj_path.read_text())
        except Exception: people_for_ltv = []
        # email → person; capture donor totals
        em_to_person = {}
        for p in people_for_ltv:
            for em in (p.get("emails") or []):
                em_to_person[em.lower().strip()] = p
        chan = {}     # source label → {signups, donors, total_amt, gifts}
        for em, info in src_email.items():
            src = info.get("source") or "(unknown)"
            chan.setdefault(src, {"signups": 0, "donors": 0, "total_amt": 0.0, "gifts": 0})
            chan[src]["signups"] += 1
            p = em_to_person.get(em)
            if p and p.get("don"):
                chan[src]["donors"]    += 1
                chan[src]["total_amt"] += float(p.get("damt") or 0)
                chan[src]["gifts"]     += int(p.get("dcnt") or 0)
        # Roll up tiny channels (<5 signups) into "Other" to keep the chart readable
        chan_rows = []   # renamed from `rows` to avoid shadowing the engagement tuples list above
        other = {"signups": 0, "donors": 0, "total_amt": 0.0, "gifts": 0}
        for src, d in chan.items():
            if d["signups"] < 5:
                other["signups"] += d["signups"]
                other["donors"]  += d["donors"]
                other["total_amt"] += d["total_amt"]
                other["gifts"]     += d["gifts"]
                continue
            chan_rows.append({
                "source":      src,
                "signups":     d["signups"],
                "donors":      d["donors"],
                "donor_rate":  round((d["donors"] / d["signups"]) * 100, 1) if d["signups"] else 0,
                "total_raised":round(d["total_amt"], 2),
                "ltv_per_signup": round(d["total_amt"] / d["signups"], 2) if d["signups"] else 0,
                "ltv_per_donor":  round(d["total_amt"] / d["donors"], 2)  if d["donors"]  else 0,
            })
        if other["signups"] > 0:
            chan_rows.append({
                "source":      "Other (small channels)",
                "signups":     other["signups"],
                "donors":      other["donors"],
                "donor_rate":  round((other["donors"] / other["signups"]) * 100, 1) if other["signups"] else 0,
                "total_raised":round(other["total_amt"], 2),
                "ltv_per_signup": round(other["total_amt"] / other["signups"], 2) if other["signups"] else 0,
                "ltv_per_donor":  round(other["total_amt"] / other["donors"], 2)  if other["donors"]  else 0,
            })
        chan_rows.sort(key=lambda r: -r["signups"])
        # Totals row for context
        tot = {
            "signups":     sum(r["signups"] for r in chan_rows),
            "donors":      sum(r["donors"]  for r in chan_rows),
            "total_raised":round(sum(r["total_raised"] for r in chan_rows), 2),
        }
        tot["donor_rate"] = round((tot["donors"] / tot["signups"]) * 100, 1) if tot["signups"] else 0
        tot["ltv_per_signup"] = round(tot["total_raised"] / tot["signups"], 2) if tot["signups"] else 0
        out["channel_ltv"] = {
            "available": True,
            "window_days": (signup_attr or {}).get("window_days", 180),
            "channels": chan_rows,
            "total": tot,
        }
    else:
        out["channel_ltv"] = {"available": False,
            "reason": "No per-email signup data available — needs Ghost member-events feed."}

    # ---- 4. Influence-weighted reach -------------------------------------
    # Score each subscriber by their likely influence in NYC policy circles:
    #   rating-based engagement + wiki "notable" bonus + gov/edu bonus
    # Sum across all subscribers in MAU → influence-weighted reach
    # Convert rows (list of tuples) into email→rating dict for fast lookup
    rating_by_email = {r[0]: r[1] for r in rows} if rows else {}
    if pj_path.exists() and rows:
        try:
            people = json.loads(pj_path.read_text())
        except Exception: people = []
        # Map email → person (for is_notable, types, etc.)
        em2p = {}
        for p in people:
            if not p.get("mem") or p.get("unsub"): continue
            for em in (p.get("emails") or []):
                em2p[em.lower().strip()] = p
        # MAU emails (need set passed in — fall back to high-rating subscribers as proxy)
        mau_set = mc.get("_mau_set") or set()
        def _score(p, rating):
            s = 1.0 + (rating or 0) * 0.4
            if p.get("wiki"): s += 2.0
            types = set(p.get("types") or [])
            if any(t in types for t in ("current nyc.gov", "city gov", "state gov", "fed gov", "judge")):
                s += 1.5
            return s

        weighted_total = 0.0
        unweighted_total = 0
        notable_list = []
        gov_list     = []
        GOV_TYPES = {"current nyc.gov", "city gov", "state gov", "fed gov", "judge"}
        for em, p in em2p.items():
            if mau_set and em not in mau_set: continue
            rating = rating_by_email.get(em, 0)
            weighted_total += _score(p, rating)
            unweighted_total += 1
            types = set(p.get("types") or [])
            row = {
                "name":  p.get("n") or "(no name)",
                "email": em,
                "inst":  p.get("inst") or "",
                "types": list(types)[:6],
                "rating": rating,
            }
            if p.get("wiki"): notable_list.append({**row, "wiki": True})
            if any(t in types for t in GOV_TYPES): gov_list.append(row)
        # Alphabetical by last name — matches how the Contact tool sorts and
        # makes it easy to scan / find specific people.
        notable_list.sort(key=_last_name_key)
        gov_list.sort(    key=_last_name_key)
        out["influence_weighted_reach"] = {
            "score":            round(weighted_total, 1),
            "raw_mau":          unweighted_total,
            "notable_in_mau":   len(notable_list),
            "gov_in_mau":       len(gov_list),
            "score_per_reader": round(weighted_total / unweighted_total, 2) if unweighted_total else 0,
            "notable_list":     notable_list,
            "gov_list":         gov_list,
        }
    else:
        out["influence_weighted_reach"] = {"available": False, "reason": "no people.json or engagement data"}

    # Also include a power-readers list (top 200 by composite engagement score)
    # so the count there is clickable too. Stored separately to keep the
    # power_readers summary block backward-compatible.
    if rows:
        scored2 = sorted(rows, key=lambda r: -(r[1] * 20 + r[2]))
        top = scored2[: min(200, max(1, len(scored2) // 10))]   # top 10% capped at 200
        # Lookup person info for each
        pj_path2 = PRIV / "people.json"
        em2p2 = {}
        if pj_path2.exists():
            try:
                _people = json.loads(pj_path2.read_text())
                for p in _people:
                    for em in (p.get("emails") or []):
                        em2p2[em.lower().strip()] = p
            except Exception: pass
        # Keep composite-engagement order (rating × 20 + open rate), most to
        # least — `top` is already sorted that way; do NOT re-sort by name.
        prl = [{
            "name":   (em2p2.get(em, {}).get("n") or "(no name)"),
            "email":  em,
            "inst":   em2p2.get(em, {}).get("inst") or "",
            "rating": rating,
            "open_pct": op,
            "score":  round(rating * 20 + op, 1),
        } for em, rating, op in top]
        out["power_readers_list"] = prl

    # Policy-circle reach history — one counts-only point per UTC day, persisted
    # in data/ (public-repo safe: aggregate counts, never names) so the "who
    # reads" metric can trend over time. No back-history exists, so the trend
    # accrues from the first run onward.
    iwr = out.get("influence_weighted_reach") or {}
    if iwr.get("raw_mau"):
        try:
            hist_path = ROOT / "data" / "policy_reach_history.json"
            hist = []
            if hist_path.exists():
                try: hist = json.loads(hist_path.read_text()) or []
                except Exception: hist = []
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            point = {
                "date":           today,
                "notable_in_mau": iwr.get("notable_in_mau", 0),
                "gov_in_mau":     iwr.get("gov_in_mau", 0),
                "score":          iwr.get("score", 0),
                "active_mau":     iwr.get("raw_mau", 0),
                "power_readers":  (out.get("power_readers") or {}).get("count", 0),
                "list_total":     mc.get("total_subscribers", 0),
            }
            hist = [h for h in hist if h.get("date") != today]   # upsert today's point
            hist.append(point)
            hist.sort(key=lambda h: h.get("date", ""))
            hist = hist[-400:]                                    # cap ~13 months
            hist_path.write_text(json.dumps(hist))
            out["policy_history"] = hist
        except Exception as e:
            log(f"  policy-reach history capture failed: {e}")
            out["policy_history"] = []

    return out


def build_lifecycle(mc):
    """Retention curves + sunset + at-risk analysis from the MAU/AAU sets
    + people.json signup dates + engagement-source ratings.

    Outputs the four core lifecycle metrics:
      - cohort_retention: % of each signup cohort that's in MAU today
      - activation_rate: % of new-30-day subscribers in MAU
      - sunset_candidates: low-engagement + tenured subscribers (count + sample)
      - at_risk: AAU minus MAU = lapsed-but-once-engaged
    """
    if not mc or not mc.get("_mau_set"):
        return {"available": False, "reason": "no MAU/AAU sets available"}
    mau_set = mc["_mau_set"]
    aau_set = mc["_aau_set"]
    today = datetime.now(timezone.utc).date()

    # Load people.json for subscriber list + signup dates
    pj = PRIV / "people.json"
    if not pj.exists():
        return {"available": False, "reason": "no people.json yet"}
    try:
        people = json.loads(pj.read_text())
    except Exception as e:
        return {"available": False, "reason": f"people.json read failed: {e}"}

    # Load Mailchimp member ratings (1-5 stars) from cached engagement CSV
    ratings = {}   # email -> rating int
    eng = PRIV / "engagement_source.csv"
    if eng.exists():
        import csv
        with open(eng) as f:
            for row in csv.DictReader(f):
                em = (row.get("Email") or "").lower().strip()
                if em:
                    try: ratings[em] = int(row.get("Rating") or 0)
                    except: pass

    # Cohort buckets (days since signup)
    BUCKETS = [
        ("0-7 days",      0,   7),
        ("8-14 days",     8,   14),
        ("15-30 days",    15,  30),
        ("31-60 days",    31,  60),
        ("61-90 days",    61,  90),
        ("91-180 days",   91,  180),
        ("181-365 days",  181, 365),
        ("366-730 days",  366, 730),
        ("731+ days",     731, 99999),
    ]
    cohort = {lab: {"label": lab, "subs": 0, "engaged": 0, "lo": lo, "hi": hi} for lab, lo, hi in BUCKETS}

    sunset = []
    at_risk = []
    new_30 = 0
    new_30_engaged = 0
    total_subs = 0

    for p in people:
        if not p.get("mem"): continue
        if p.get("unsub"): continue
        total_subs += 1
        # Subscriber's earliest signup date — use `since`
        since = (p.get("since") or "")[:10]
        if not since: continue
        try:
            y, m, d = (int(x) for x in since.split("-"))
            from datetime import date as _date
            signup = _date(y, m, d)
        except Exception: continue
        days_since = (today - signup).days
        # Which buckets does this person belong to (use the first matching)
        for lab, lo, hi in BUCKETS:
            if lo <= days_since <= hi:
                cohort[lab]["subs"] += 1
                # Is any of this person's emails in MAU?
                em_list = [e.lower().strip() for e in (p.get("emails") or [p.get("e","")]) if e]
                engaged = any(em in mau_set for em in em_list)
                if engaged: cohort[lab]["engaged"] += 1
                break

        # Activation: did new-30-day subscribers open anything?
        if days_since <= 30:
            new_30 += 1
            em_list = [e.lower().strip() for e in (p.get("emails") or [p.get("e","")]) if e]
            if any(em in mau_set for em in em_list):
                new_30_engaged += 1

        # Sunset candidates: rating ≤ 2 AND tenured > 180 days AND NOT in MAU
        em_list = [e.lower().strip() for e in (p.get("emails") or [p.get("e","")]) if e]
        worst_rating = min((ratings.get(em, 5) for em in em_list if em in ratings), default=None)
        in_mau = any(em in mau_set for em in em_list)
        in_aau = any(em in aau_set for em in em_list)
        if (worst_rating is not None and worst_rating <= 2
            and days_since > 180 and not in_mau):
            sunset.append({
                "name":  p.get("n") or "(no name)",
                "email": em_list[0] if em_list else "",
                "rating": worst_rating,
                "since":  since,
                "tenure_days": days_since,
            })
        # At-risk: was in AAU but not in MAU (opened in last year but not last 30d)
        if in_aau and not in_mau:
            at_risk.append({
                "name":  p.get("n") or "(no name)",
                "email": em_list[0] if em_list else "",
                "rating": worst_rating,
                "since":  since,
                "tenure_days": days_since,
            })

    # Compute retention rate per cohort. Cohorts under 20 people report None
    # rather than a percentage — 0-of-7 is noise, not signal. The newest
    # cohorts also structurally undercount: new signups live in Ghost until
    # the weekly Mailchimp reconcile, so their opens aren't measurable yet,
    # and they may not have received a send at all. A tiny young cohort once
    # produced "0% activation — broken welcome flow?" on the dashboard when
    # nothing was wrong.
    MIN_COHORT = 20
    for lab, d in cohort.items():
        d["retention_pct"] = round((d["engaged"] / d["subs"]) * 100, 1) if d["subs"] >= MIN_COHORT else None
        d["small_sample"]  = bool(d["subs"] < MIN_COHORT)

    activation_rate = round((new_30_engaged / new_30) * 100, 1) if new_30 else 0

    return {
        "available": True,
        "total_subscribers_counted": total_subs,
        "cohort_retention": [cohort[lab] for lab, lo, hi in BUCKETS],
        "activation_30d": {
            "cohort_size": new_30,
            "engaged":     new_30_engaged,
            "pct":         activation_rate,
        },
        "sunset_candidates": {
            "count": len(sunset),
            # Sample sticks with longest-tenure-first (the inline preview in
            # the lifecycle card surfaces the longest-lapsed first as a
            # nudge), but the modal list is alphabetical by last name.
            "sample": sorted(sunset, key=lambda x: -x["tenure_days"])[:20],
            "list":   sorted(sunset, key=_last_name_key)[:500],
            # `list` is capped at 500 purely to keep the dashboard modal light.
            # The Contact tool's Engagement filter derives its per-person flags
            # from this block, so it needs every match, not the first 500 —
            # otherwise the filter silently returns a 500-row subset. Emails
            # only: no names, and the growth payload is encrypted anyway.
            "emails_all": [x["email"] for x in sunset if x.get("email")],
        },
        "at_risk": {
            "count": len(at_risk),
            # Sample stays sorted by star rating (highest-engagement first) so
            # the inline preview leads with your highest-value reachable
            # subscribers. The modal list is alphabetical by last name.
            "sample": sorted(at_risk, key=lambda x: -(x.get("rating") or 0))[:20],
            "list":   sorted(at_risk, key=_last_name_key)[:500],
            "emails_all": [x["email"] for x in at_risk if x.get("email")],   # see note above
        },
    }


# -------------------------------------------------------------------- Ghost
GHOST_CONTENT_KEY = "dd8e178e9ddfc883537e71dd07"   # public, same as scrape.py
GHOST_API = "https://vital-city.ghost.io/ghost/api/content"
GHOST_ADMIN_API = "https://vital-city.ghost.io/ghost/api/admin"

def _ghost_jwt_from(key):
    """Sign a short-lived JWT for the Ghost Admin API from an id:secret pair.
    Works for both integration admin keys and staff access tokens (same
    format; different permission levels)."""
    import hashlib, hmac
    if not key or ":" not in key:
        return None
    kid, secret = key.split(":", 1)
    def b64u(b): return base64.urlsafe_b64encode(b).rstrip(b"=")
    iat = int(time.time())
    h = b64u(json.dumps({"alg":"HS256","typ":"JWT","kid":kid}).encode())
    p = b64u(json.dumps({"iat":iat,"exp":iat+300,"aud":"/admin/"}).encode())
    sig = hmac.new(bytes.fromhex(secret), h+b"."+p, hashlib.sha256).digest()
    return (h + b"." + p + b"." + b64u(sig)).decode()


def _ghost_admin_token():
    """Integration admin key from private/.ghost_admin_key or env GHOST_ADMIN_KEY."""
    key = os.environ.get("GHOST_ADMIN_KEY") or ""
    if not key:
        f = PRIV / ".ghost_admin_key"
        if f.exists(): key = f.read_text().strip()
    return _ghost_jwt_from(key)


def _ghost_staff_token():
    """STAFF access token (user-level permissions) from private/.ghost_staff_key
    or env GHOST_STAFF_KEY. Ghost's /stats/* analytics endpoints — visitor
    counts, top content, sources; the Tinybird-backed data behind the admin
    Analytics tab — return 403 for integration keys (verified live on Ghost
    6.45) but are allowed for staff credentials. Find yours in Ghost Admin →
    Settings → Staff → your profile → "Staff access token"."""
    key = os.environ.get("GHOST_STAFF_KEY") or ""
    if not key:
        f = PRIV / ".ghost_staff_key"
        if f.exists(): key = f.read_text().strip()
    return _ghost_jwt_from(key)


# ---------------------------------------------------------------- GA4
GA4_SETUP = [
    "1) In Google Cloud (console.cloud.google.com), pick/create a project and enable the 'Google Analytics Data API'.",
    "2) Create a service account in that project; under its Keys tab, add a JSON key and download it.",
    "3) In GA4 -> Admin -> Property access management, add the service-account email with the Viewer role. IMPORTANT: UNCHECK 'Notify new users by email' (a service account has no inbox, and leaving it checked throws 'This email doesn't match a Google Account').",
    "4) Copy the GA4 property's numeric ID (Admin -> Property settings, e.g. 123456789).",
    "5) Set GitHub secrets GA4_PROPERTY_ID (the number) and GA4_CREDS_JSON (the whole JSON key, raw or base64).",
]


def _b64url(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=")


def _sa_access_token(creds, scope):
    """Mint a short-lived OAuth token from a service-account key for a given
    scope, by signing a JWT (RS256) with the cryptography lib — no google-auth
    dependency needed."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    claim = {
        "iss": creds["client_email"],
        "scope": scope,
        "aud": "https://oauth2.googleapis.com/token",
        "iat": now, "exp": now + 3600,
    }
    seg = _b64url(json.dumps(header).encode()) + b"." + _b64url(json.dumps(claim).encode())
    key = serialization.load_pem_private_key(creds["private_key"].encode(), password=None)
    sig = key.sign(seg, padding.PKCS1v15(), hashes.SHA256())
    assertion = (seg + b"." + _b64url(sig)).decode()
    data = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def _ga4_access_token(creds):
    return _sa_access_token(creds, "https://www.googleapis.com/auth/analytics.readonly")


def _ga4_totals(prop, token, days):
    """totalUsers / sessions / page views over the last `days` days."""
    body = {
        "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
        "metrics": [{"name": "totalUsers"}, {"name": "sessions"}, {"name": "screenPageViews"}],
    }
    req = urllib.request.Request(
        f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        rep = json.loads(r.read())
    rows = rep.get("rows") or []
    if not rows:
        return (0, 0, 0)
    v = rows[0]["metricValues"]
    return (int(v[0]["value"]), int(v[1]["value"]), int(v[2]["value"]))


def _ga4_weekly_traffic(prop, token, start_date="2025-01-01"):
    """Daily sessions + page views aggregated to Monday-start weeks. Lets the
    custom-report tool backfill weeks that predate Ghost's own analytics history.
    GA4 is one consistent counter across the whole timeline — it reads LOWER than
    Ghost (ad blockers / ITP suppress its third-party script), so where GA4 weeks
    meet Ghost weeks the report flags the source change."""
    body = {
        "dateRanges": [{"startDate": start_date, "endDate": "today"}],
        "dimensions": [{"name": "date"}],
        "metrics": [{"name": "sessions"}, {"name": "screenPageViews"}],
        "orderBys": [{"dimension": {"dimensionName": "date"}}],
        "limit": 100000,
    }
    req = urllib.request.Request(
        f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
        data=json.dumps(body).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        rep = json.loads(r.read())
    wk = {}
    for row in rep.get("rows") or []:
        d = row["dimensionValues"][0]["value"]            # YYYYMMDD
        dt = datetime(int(d[:4]), int(d[4:6]), int(d[6:]))
        monday = dt - timedelta(days=dt.weekday())
        e = wk.setdefault(monday.strftime("%Y-%m-%d"), [0, 0])
        v = row["metricValues"]
        e[0] += int(v[0]["value"]); e[1] += int(v[1]["value"])
    return [{"wk": k, "visits": wk[k][0], "pageviews": wk[k][1]} for k in sorted(wk)]


def _ga4_story_weekly(prop, token, days=300, top=120):
    """Per-article weekly page views + engaged seconds, so the custom report can
    rank top stories for the EXACT period the reader picks (week resolution)
    rather than a fixed GA4 window. pagePath x date, aggregated to Monday weeks,
    keeping the top `top` articles by total views over the window. Indexed
    (pages[] + compact rows) to keep the payload small."""
    body = {
        "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
        "dimensions": [{"name": "pagePath"}, {"name": "date"}],
        "metrics": [{"name": "screenPageViews"}, {"name": "userEngagementDuration"}],
        "limit": 200000,
    }
    rep = None
    for attempt in range(3):
        req = urllib.request.Request(
            f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                rep = json.loads(r.read()); break
        except Exception:
            if attempt == 2: raise
            time.sleep(2 * (attempt + 1))
    cat = {}
    try:
        for c in json.loads((ROOT / "data" / "catalogue.json").read_text()):
            u = (c.get("url") or "").rstrip("/")
            if u:
                cat["/" + u.split("/")[-1] + "/"] = c.get("title")
    except Exception:
        pass
    def is_article(p):
        return not (p in ("/", "") or "/job" in p or p in ("/about/", "/about")
                    or p.startswith("/tag/") or p.startswith("/author/"))
    pages = {}
    for row in (rep.get("rows") or []):
        path = (row["dimensionValues"][0]["value"] or "").split("?")[0]
        if not is_article(path):
            continue
        dt = row["dimensionValues"][1]["value"]   # YYYYMMDD
        try:
            d = datetime(int(dt[:4]), int(dt[4:6]), int(dt[6:]))
        except Exception:
            continue
        wk = (d - timedelta(days=d.weekday())).strftime("%Y-%m-%d")   # Monday
        v = int(row["metricValues"][0]["value"] or 0)
        s = float(row["metricValues"][1]["value"] or 0)
        key = "/" + path.strip("/").split("/")[-1] + "/"
        title = cat.get(key) or path.strip("/").replace("-", " ").title()
        e = pages.setdefault(path, {"title": title, "tot": 0, "weeks": {}})
        e["tot"] += v
        w = e["weeks"].setdefault(wk, [0, 0.0]); w[0] += v; w[1] += s
    kept = sorted(pages.items(), key=lambda kv: -kv[1]["tot"])[:top]
    plist = [{"p": path, "t": info["title"]} for path, info in kept]
    rows = []
    for i, (path, info) in enumerate(kept):
        for wk, (v, s) in sorted(info["weeks"].items()):
            rows.append([i, wk, v, round(s)])
    log(f"  ga4 story-weekly: {len(plist)} articles, {len(rows)} page-weeks")
    return {"pages": plist, "rows": rows}


def _ga4_daily_by_slug(prop, token, start_date, end_date="today"):
    """Per-slug, per-day views + engaged seconds over an arbitrary range.

    Pulled in YEARLY chunks with offset paging: the pagePath x date grain blows
    past the API's row cap over multi-year ranges, and a silent truncation would
    quietly zero out older pieces' opening numbers. Both URL shapes (the old
    Prismic /articles/<slug> and the Ghost /<slug>/) collapse to the bare slug."""
    def slug_of(p):
        # Last path segment = the piece slug, regardless of section prefix.
        # Ghost uses flat /<slug>; the old Prismic CMS used /articles/<slug> and
        # /vital_signs/<slug>, and GA4 still holds that pre-migration traffic.
        # Matching only the flat/articles shape silently dropped every
        # section-prefixed URL (e.g. the 15k-view gun-violence data page).
        s = (p or "").split("?")[0].split("#")[0].strip("/").lower()
        return s.split("/")[-1] if s else ""
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.now(timezone.utc).date() if end_date == "today" else datetime.strptime(end_date, "%Y-%m-%d").date()
    daily, total_rows = {}, 0
    yr = start.year
    while yr <= end.year:
        lo = max(start, datetime(yr, 1, 1).date()).isoformat()
        hi = min(end, datetime(yr, 12, 31).date()).isoformat()
        offset = 0
        while True:
            body = {"dateRanges": [{"startDate": lo, "endDate": hi}],
                    "dimensions": [{"name": "pagePath"}, {"name": "date"}],
                    "metrics": [{"name": "screenPageViews"}, {"name": "userEngagementDuration"}],
                    "limit": 100000, "offset": offset}
            rep = None
            for attempt in range(3):
                req = urllib.request.Request(
                    f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
                    data=json.dumps(body).encode(),
                    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
                try:
                    with urllib.request.urlopen(req, timeout=120) as r:
                        rep = json.loads(r.read()); break
                except Exception:
                    if attempt == 2: raise
                    time.sleep(2 * (attempt + 1))
            rows = rep.get("rows") or []
            for row in rows:
                sl = slug_of(row["dimensionValues"][0]["value"] or "")
                if not sl or "/" in sl or sl in ("about", "the-journal"):
                    continue
                dt = row["dimensionValues"][1]["value"]
                e = daily.setdefault(sl, {})
                cur = e.get(dt) or [0, 0.0]
                cur[0] += int(row["metricValues"][0]["value"] or 0)
                cur[1] += float(row["metricValues"][1]["value"] or 0)
                e[dt] = cur
            total_rows += len(rows)
            if len(rows) < body["limit"]:
                break
            offset += body["limit"]
        yr += 1
    log(f"  ga4 daily-by-slug: {len(daily)} slugs from {total_rows:,} rows ({start_date}..)")
    return daily


def _ga4_piece_benchmarks(prop, token, days=400, first_days=30, index_since="2023-01-01"):
    """Benchmark bands for judging ONE piece: the full distribution of
    first-30-days-after-publication page views across every piece published
    inside the window.

    Why first-30-days and not lifetime: a 2023 explainer has had years to
    accumulate views, so comparing lifetime totals punishes anything new. Each
    piece is measured over its own opening 30 days instead, which is the only
    apples-to-apples cut. Deliberately does NOT truncate to a top-N (unlike the
    story/engagement pulls, which are capped at 120/60 and would make the
    'average' the average of our best work).

    Two windows on purpose. The BANDS are computed on the recent `days` window
    so a piece is judged against the audience we have now. The per-piece rows
    reach back to `index_since` (as far as GA4 goes) so the look-up tool can
    show an opening number for older pieces too — judged against their OWN
    year's bands, since the site drew ~8x fewer visitors in 2023 and scoring a
    2023 piece against 2026 medians would be meaningless."""
    daily = _ga4_daily_by_slug(prop, token, index_since)
    # Catalogue: slug -> (published_date, type, title)
    cat = {}
    try:
        for c in json.loads((ROOT / "data" / "catalogue.json").read_text()):
            sl = (c.get("slug") or "").lower()
            if sl and c.get("published_date"):
                cat[sl] = (c["published_date"][:10], c.get("type") or "unknown", c.get("title") or sl)
    except Exception as e:
        log(f"  ga4 benchmarks: catalogue unavailable ({e})")
        return {"available": False, "reason": f"catalogue unavailable: {e}"}
    today = datetime.now(timezone.utc).date()
    win_start = today - timedelta(days=days)
    # Eligible: published inside the GA4 window AND old enough that its full
    # first_days have elapsed (otherwise its opening window is still filling).
    lo = win_start.isoformat(); hi = (today - timedelta(days=first_days)).isoformat()
    # Score every piece back to index_since (the look-up tool wants an opening
    # number for old pieces too); `bands` below still uses only the recent
    # window so today's pieces are judged against today's audience.
    all_pieces = []
    for sl, (pub, typ, title) in cat.items():
        if not (index_since <= pub <= hi):
            continue
        try:
            p0 = datetime.strptime(pub, "%Y-%m-%d").date()
        except Exception:
            continue
        end = p0 + timedelta(days=first_days)
        dd = daily.get(sl) or {}
        views = 0; secs = 0.0
        lifetime = 0; first_seen = None
        for dt, (v, s) in dd.items():
            try:
                dobj = datetime.strptime(dt, "%Y%m%d").date()
            except Exception:
                continue
            lifetime += v
            if v > 0 and (first_seen is None or dobj < first_seen):
                first_seen = dobj
            if p0 <= dobj < end:
                views += v; secs += s
        # A piece that GA4 clearly recorded traffic for, but which shows ZERO
        # views in its own stated opening window, has an unreliable published
        # date — several dates were mis-assigned in the Prismic->Ghost
        # migration. Scoring that as "under-performing" would be flatly wrong
        # (one such piece has 76k lifetime views), and feeding a spurious 0
        # into the bands would drag the distribution down. Mark it unknown.
        suspect = (views == 0 and lifetime > 0)
        all_pieces.append({"slug": sl, "title": title, "type": typ, "pub": pub,
                           "views": None if suspect else views,
                           "secs": None if suspect else secs,
                           "suspect_date": suspect,
                           "first_seen": first_seen.isoformat() if first_seen else None})
    # Bands are built from the recent window only (comparable-era judging), and
    # never from pieces whose opening window we can't trust.
    pieces = [p for p in all_pieces if lo <= p["pub"] <= hi and not p["suspect_date"]]
    if len(pieces) < 12:
        return {"available": False, "reason": f"only {len(pieces)} pieces old enough in the window"}
    def pct(vals, q):
        if not vals: return 0
        v = sorted(vals); i = (len(v) - 1) * q
        lo_i, hi_i = int(i), min(int(i) + 1, len(v) - 1)
        return round(v[lo_i] + (v[hi_i] - v[lo_i]) * (i - lo_i))
    def pctf(vals, q):           # same, but keeps decimals (for reader-hours)
        if not vals: return 0.0
        v = sorted(vals); i = (len(v) - 1) * q
        lo_i, hi_i = int(i), min(int(i) + 1, len(v) - 1)
        return v[lo_i] + (v[hi_i] - v[lo_i]) * (i - lo_i)
    QS = (("p10", .10), ("p25", .25), ("p50", .50), ("p75", .75), ("p90", .90), ("p95", .95))
    vv = [p["views"] for p in pieces]
    bands = {q: pct(vv, f) for q, f in QS}
    # Attention: total reader-hours in the same opening window. Views x depth —
    # rewards pieces that actually hold people, not just pieces that got clicked.
    hrs = [p["secs"] / 3600.0 for p in pieces]
    hours_bands = {q: round(pctf(hrs, f), 1) for q, f in QS}
    # Depth: seconds per view, across pieces with enough traffic to be stable.
    spv = [(p["secs"] / p["views"]) for p in pieces if p["views"] >= 30]
    depth_bands = {q: round(pctf(spv, f)) for q, f in QS}
    med_spv = depth_bands.get("p50", 0)
    by_type = {}
    for t in set(p["type"] for p in pieces):
        sub = [p["views"] for p in pieces if p["type"] == t]
        if len(sub) >= 8:
            by_type[t] = {"n": len(sub), "p50": pct(sub, .50), "p90": pct(sub, .90)}
    top = sorted(pieces, key=lambda p: -p["views"])[:8]
    log(f"  ga4 benchmarks: {len(all_pieces)} pieces scored back to {index_since}; "
        f"bands from {len(pieces)} in the recent window; "
        f"{len(pieces)} pieces scored on first {first_days}d "
        f"(median {bands['p50']} views, p90 {bands['p90']})")
    return {"available": True, "n_pieces": len(pieces), "first_days": first_days,
            "window_days": days, "window_start": lo, "window_end": hi,
            "bands": bands, "median_secs_per_view": med_spv,
            "hours_bands": hours_bands, "depth_bands": depth_bands,
            "n_depth": len(spv),
            # Per-piece first-30-day rows for EVERY piece back to index_since,
            # consumed by _ga4_piece_index and stripped before publishing.
            "_pieces": all_pieces,
            # Era-fair bands: judge a 2023 piece against 2023, not against a
            # site that now draws several times the traffic. Only years with
            # enough pieces to be meaningful.
            "year_bands": {
                y: {"n": len(sub), **{q: pct([p["views"] for p in sub], f) for q, f in QS}}
                for y, sub in
                {y: [p for p in all_pieces if p["pub"][:4] == y and not p["suspect_date"]]
                 for y in sorted({p["pub"][:4] for p in all_pieces})}.items()
                if len(sub) >= 20
            },
            "index_since": index_since,
            "by_type": by_type,
            "top": [{"title": p["title"], "views": p["views"], "pub": p["pub"]} for p in top],
            "as_of": today.isoformat()}


def _ga4_events(prop, token, days=365):
    """GA4 custom events — the only pull here that uses the eventName dimension
    (everything else is pagePath/date/user based, which is why a custom event
    like the 'make Vital City a preferred source' click was invisible).

    Auto-detects the preferred-source event by name rather than hardcoding it,
    so a rename in GA4 doesn't silently zero the panel, and returns the full
    event list so an unmatched name is diagnosable from the published data."""
    def run(body):
        for attempt in range(3):
            req = urllib.request.Request(
                f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
                data=json.dumps(body).encode(),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=60) as r:
                    return json.loads(r.read())
            except Exception:
                if attempt == 2: raise
                time.sleep(2 * (attempt + 1))
    rep = run({"dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
               "dimensions": [{"name": "eventName"}],
               "metrics": [{"name": "eventCount"}, {"name": "totalUsers"}],
               "limit": 500})
    events = [{"name": r["dimensionValues"][0]["value"],
               "count": int(r["metricValues"][0]["value"] or 0),
               "users": int(r["metricValues"][1]["value"] or 0)}
              for r in (rep.get("rows") or [])]
    events.sort(key=lambda e: -e["count"])
    names = [e["name"] for e in events]
    log(f"  ga4 events: {len(events)} event names — {', '.join(names[:12])}{'…' if len(names) > 12 else ''}")

    pref_names = [e["name"] for e in events if re.search(r"prefer", e["name"], re.I)]
    preferred = None
    if pref_names:
        tot = sum(e["count"] for e in events if e["name"] in pref_names)
        usr = max((e["users"] for e in events if e["name"] in pref_names), default=0)
        # Daily series so the panel can show adoption over time, plus a 30-day cut.
        daily = []
        try:
            drep = run({"dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
                        "dimensions": [{"name": "date"}],
                        "metrics": [{"name": "eventCount"}],
                        "dimensionFilter": {"filter": {"fieldName": "eventName",
                                                       "inListFilter": {"values": pref_names}}},
                        "limit": 1000})
            for r in (drep.get("rows") or []):
                dt = r["dimensionValues"][0]["value"]
                daily.append({"d": f"{dt[:4]}-{dt[4:6]}-{dt[6:]}",
                              "n": int(r["metricValues"][0]["value"] or 0)})
            daily.sort(key=lambda x: x["d"])
        except Exception as e:
            log(f"  ga4 events: preferred-source daily series failed ({e})")
        # Which placement is working: the ask sits in several spots on the site,
        # so break the clicks out by the page they happened on.
        by_page = []
        try:
            prep = run({"dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
                        "dimensions": [{"name": "pagePath"}],
                        "metrics": [{"name": "eventCount"}],
                        "dimensionFilter": {"filter": {"fieldName": "eventName",
                                                       "inListFilter": {"values": pref_names}}},
                        "limit": 200})
            for r in (prep.get("rows") or []):
                by_page.append({"path": r["dimensionValues"][0]["value"],
                                "n": int(r["metricValues"][0]["value"] or 0)})
            by_page.sort(key=lambda x: -x["n"])
        except Exception as e:
            log(f"  ga4 events: preferred-source by-page breakdown failed ({e})")
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=30)).isoformat()
        preferred = {"events": pref_names, "clicks": tot, "users": usr,
                     "clicks_30d": sum(x["n"] for x in daily if x["d"] >= cutoff),
                     "first_seen": daily[0]["d"] if daily else None,
                     "daily": daily, "by_page": by_page[:20], "window_days": days}
        log(f"  ga4 events: preferred-source '{'/'.join(pref_names)}' — {tot} clicks, {usr} users"
            + (f", first seen {daily[0]['d']}" if daily else ""))
    else:
        log("  ga4 events: no event name matching /prefer/ — preferred-source panel will show as not-yet-detected")
    return {"available": True, "window_days": days, "events_top": events[:30],
            "preferred_source": preferred}


def _ga4_piece_index(prop, token, bench=None):
    """Per-piece performance index for the look-up tool: EVERY catalogue piece
    GA4 has data for, with lifetime views/engaged-time and (where the piece is
    young enough for GA4 to cover its debut) its first-30-day numbers.

    Deliberately un-truncated — the other page pulls cap at top-15/60/120,
    which is fine for leaderboards but useless for "look up any piece". Reuses
    the benchmark pull's first-30-day figures rather than re-querying."""
    body = {
        "dateRanges": [{"startDate": "2020-01-01", "endDate": "today"}],
        "dimensions": [{"name": "pagePath"}],
        "metrics": [{"name": "screenPageViews"}, {"name": "userEngagementDuration"},
                    {"name": "totalUsers"}],
        "limit": 100000,
    }
    rep = None
    for attempt in range(3):
        req = urllib.request.Request(
            f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                rep = json.loads(r.read()); break
        except Exception:
            if attempt == 2: raise
            time.sleep(2 * (attempt + 1))
    def slug_of(p):
        # Last path segment = the piece slug, regardless of section prefix.
        # Ghost uses flat /<slug>; the old Prismic CMS used /articles/<slug> and
        # /vital_signs/<slug>, and GA4 still holds that pre-migration traffic.
        # Matching only the flat/articles shape silently dropped every
        # section-prefixed URL (e.g. the 15k-view gun-violence data page).
        s = (p or "").split("?")[0].split("#")[0].strip("/").lower()
        return s.split("/")[-1] if s else ""
    # Merge every URL a piece has ever lived at (Ghost flat + old Prismic
    # section prefixes + trailing-slash variants) onto its one slug, so lifetime
    # views/uniques are the true total, not one URL era's slice. `screenPageViews`
    # sums correctly across paths; totalUsers summed across a piece's variants
    # can slightly over-count a person who read it under two URLs — accepted as
    # far better than dropping the older URL's readers entirely (the undercount
    # we're fixing). No pagePath here carries a query string (GA4's `pagePath`
    # already excludes it), so there's no row explosion to truncate.
    life, recovered = {}, 0
    for row in (rep.get("rows") or []):
        raw = row["dimensionValues"][0]["value"] or ""
        sl = slug_of(raw)
        if not sl:
            continue
        if raw.strip("/").count("/") >= 1:           # was dropped before this fix
            recovered += int(row["metricValues"][0]["value"] or 0)
        e = life.setdefault(sl, [0, 0.0, 0])
        e[0] += int(row["metricValues"][0]["value"] or 0)
        e[1] += float(row["metricValues"][1]["value"] or 0)
        e[2] += int(row["metricValues"][2]["value"] or 0)
    if recovered:
        log(f"  ga4 piece index: recovered {recovered:,} lifetime views from section-prefixed/old URLs")
    # first-30-day numbers computed by the benchmark pull (same GA4 window)
    first30 = {p["slug"]: p for p in ((bench or {}).get("_pieces") or [])}
    out = []
    try:
        cat = json.loads((ROOT / "data" / "catalogue.json").read_text())
    except Exception as e:
        log(f"  ga4 piece index: catalogue unavailable ({e})")
        return {"available": False, "reason": f"catalogue unavailable: {e}"}
    for c in cat:
        sl = (c.get("slug") or "").lower()
        if not sl:
            continue
        lv = life.get(sl)
        f3 = first30.get(sl)
        if not lv and not f3:
            continue                      # no GA4 trace at all — skip
        rec = {"slug": sl, "title": c.get("title") or sl,
               "pub": (c.get("published_date") or "")[:10],
               "type": c.get("type") or "unknown",
               "author": c.get("primary_author") or "",
               # Full byline: the author look-up credits every co-author, not
               # just the first name on the piece.
               "authors": [a for a in (c.get("authors") or []) if a] or
                          ([c["primary_author"]] if c.get("primary_author") else []),
               "topics": (c.get("topics") or [])[:4],
               "words": c.get("word_count") or 0,
               "url": c.get("url") or "",
               "views": (lv or [0, 0, 0])[0],
               "secs": round((lv or [0, 0, 0])[1]),
               "users": (lv or [0, 0, 0])[2]}
        if f3:
            # A suspect date means we can't trust the opening window at all, so
            # leave views30 absent rather than publishing a misleading zero the
            # UI would then band as "under-performing".
            if f3.get("suspect_date"):
                rec["date_suspect"] = True
                if f3.get("first_seen"): rec["first_seen"] = f3["first_seen"]
            else:
                rec["views30"] = f3["views"]
                rec["secs30"] = round(f3["secs"])
        out.append(rec)
    out.sort(key=lambda r: -(r.get("views") or 0))
    # Total-impact bands: percentiles of LIFETIME reader-hours (views x depth
    # over the whole life) across every piece with a real read. This is the
    # headline band the look-up tools show — total time readers spent with a
    # piece, judged against the whole catalogue. One scale, so a bigger number
    # always ranks higher; no era adjustment (an old evergreen genuinely has
    # more lifetime impact, and that's the point of the metric).
    hrs = sorted((r["secs"] / 3600.0) for r in out if (r.get("secs") or 0) > 0)
    impact_bands = None
    if len(hrs) >= 12:
        def pctf(vals, q):
            i = (len(vals) - 1) * q; lo = int(i); hi = min(lo + 1, len(vals) - 1)
            return round(vals[lo] + (vals[hi] - vals[lo]) * (i - lo), 1)
        impact_bands = {q: pctf(hrs, f) for q, f in
                        (("p25", .25), ("p50", .50), ("p75", .75), ("p90", .90), ("p95", .95))}
    log(f"  ga4 piece index: {len(out)} pieces with GA4 data ({sum(1 for r in out if 'views30' in r)} with first-30d)"
        + (f"; impact bands (reader-hrs) p50={impact_bands['p50']} p90={impact_bands['p90']}" if impact_bands else ""))
    return {"available": True, "n": len(out), "pieces": out,
            "impact_bands": impact_bands,
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d")}


def _ga4_engagement(prop, token, days=90, min_views=50, limit=300, start_date=None, end_date="today"):
    """Per-article engagement time — a read-depth proxy GA4 measures (the
    seconds a reader's tab is actually focused on the page) and Ghost can't.
    Per piece: views, avg engaged seconds/view (userEngagementDuration /
    views), and total engaged seconds (audience-weighted). Filtered to
    articles with enough views to be non-noisy; enough rows for a scatter.
    Pass start_date (YYYY-MM-DD) for an absolute window, else last `days`.

    `limit` is 300 (the API request already asks for 300 rows) rather than the
    old 60. The leaderboards only ever display a dozen, but these rows are also
    joined to catalogue subjects to show which topics earn readers, and a top-60
    cut biases that badly toward whatever a year's few blockbusters were about.
    GA4 is the ONLY per-article history before Ghost analytics begin in March
    2026, so this is the sole way to see how the audience's interests moved."""
    body = {
        "dateRanges": [{"startDate": start_date or f"{days}daysAgo", "endDate": end_date}],
        "dimensions": [{"name": "pagePath"}],
        "metrics": [{"name": "screenPageViews"}, {"name": "userEngagementDuration"}],
        "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}],
        "limit": 300,
    }
    # Retry transient GA4 errors (timeouts / 429-503) so one flaky query
    # doesn't silently empty a leaderboard on an unattended daily run.
    rep = None
    for attempt in range(3):
        req = urllib.request.Request(
            f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                rep = json.loads(r.read())
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    cat = {}
    try:
        for c in json.loads((ROOT / "data" / "catalogue.json").read_text()):
            u = (c.get("url") or "").rstrip("/")
            if u:
                cat["/" + u.split("/")[-1] + "/"] = c.get("title")
    except Exception:
        pass
    def is_article(p):
        return not (p in ("/", "") or "/job" in p or p in ("/about/", "/about")
                    or p.startswith("/tag/") or p.startswith("/author/"))
    out = []
    for row in (rep.get("rows") or []):
        path = (row["dimensionValues"][0]["value"] or "").split("?")[0]
        if not is_article(path):
            continue
        views = int(row["metricValues"][0]["value"] or 0)
        eng = float(row["metricValues"][1]["value"] or 0)
        if views < min_views:
            continue
        key = "/" + path.strip("/").split("/")[-1] + "/"
        title = cat.get(key) or path.strip("/").replace("-", " ").title()
        out.append({"path": path, "title": title, "views": views,
                    "avg_secs": round(eng / views, 1) if views else 0,
                    "total_secs": round(eng)})
    out.sort(key=lambda x: -x["avg_secs"])
    return out[:limit]


def _ga4_pretty_slug(slug):
    t = slug.replace("-", " ").strip().title()
    for a, b in (("Nyc", "NYC"), ("Nypd", "NYPD"), ("Mta", "MTA"), ("Lic", "LIC"),
                 (" Ai ", " AI "), ("Us ", "US "), ("Nyc's", "NYC's")):
        t = t.replace(a, b)
    return t


def _ga4_top_pages_since(prop, token, start_date, limit=15, end_date="today"):
    """Most-read articles by unique visitors over an absolute date range, from
    GA4 — which covers the pre-Ghost-handoff period (back to 2023), so it can
    build per-year 'most read' lists Ghost's own analytics can't. Titles come
    from the Ghost catalogue where the slug still matches; older URLs fall back
    to a prettified slug."""
    body = {
        "dateRanges": [{"startDate": start_date, "endDate": end_date}],
        "dimensions": [{"name": "pagePath"}],
        "metrics": [{"name": "totalUsers"}, {"name": "screenPageViews"}],
        "orderBys": [{"metric": {"metricName": "totalUsers"}, "desc": True}],
        "limit": 250,
    }
    # Retry transient GA4 errors (timeouts / 429-503) so one flaky query
    # doesn't silently empty a leaderboard on an unattended daily run.
    rep = None
    for attempt in range(3):
        req = urllib.request.Request(
            f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                rep = json.loads(r.read())
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2 * (attempt + 1))
    cat = {}
    try:
        for c in json.loads((ROOT / "data" / "catalogue.json").read_text()):
            u = (c.get("url") or "").rstrip("/")
            if u:
                cat["/" + u.split("/")[-1] + "/"] = c.get("title")
    except Exception:
        pass
    # Hub / section landing pages (not individual pieces) — matched on the last
    # path segment so a piece whose slug merely contains one of these words
    # (e.g. ".../a-2125-map") is NOT excluded.
    HUB_SLUGS = {"data", "data_hub", "data-hub", "explorer", "explore",
                 "maps", "map", "vital_signs", "vital-signs"}
    def is_article(p):
        return not (p in ("/", "") or "/job" in p or "/career" in p or p in ("/about/", "/about")
                    or p.startswith("/tag/") or p.startswith("/author/") or p.startswith("/contributor")
                    or p.startswith("/issues") or p.startswith("/search")
                    or p.startswith("/privacy") or p.startswith("/terms")
                    or p.rstrip("/").rsplit("/", 1)[-1] in HUB_SLUGS)
    # Merge by title so a piece that lived at both an old-site /articles/ URL
    # and the new-site URL is counted once (its visitors summed), and prefer
    # the canonical (catalogue-matching) path for the link.
    agg = {}
    for row in (rep.get("rows") or []):
        path = (row["dimensionValues"][0]["value"] or "").split("?")[0]
        if not is_article(path):
            continue
        slug = path.strip("/").split("/")[-1]
        canon = cat.get("/" + slug + "/")
        title = canon or _ga4_pretty_slug(slug)
        e = agg.get(title)
        if not e:
            e = agg[title] = {"path": path, "title": title, "visitors": 0, "views": 0}
        e["visitors"] += int(row["metricValues"][0]["value"] or 0)
        e["views"]    += int(row["metricValues"][1]["value"] or 0)
        if canon:
            e["path"] = path   # link to the live URL when we recognize it
    return sorted(agg.values(), key=lambda x: -x["visitors"])[:limit]


def _ga4_by_year(prop, token):
    """The long view: visitors / page views / sessions per calendar year across
    all of GA4's history, plus a true (deduplicated) all-time total. Per-year
    users are deduped within each year, so they must NOT be summed for an
    all-time figure — the separate all-time query handles that."""
    def runrep(body):
        req = urllib.request.Request(
            f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read())
    METRICS = [{"name": "totalUsers"}, {"name": "screenPageViews"}, {"name": "sessions"}]
    RANGE = [{"startDate": "2020-01-01", "endDate": "today"}]
    yr = runrep({"dateRanges": RANGE, "dimensions": [{"name": "year"}], "metrics": METRICS,
                 "orderBys": [{"dimension": {"dimensionName": "year"}}]})
    years = []
    for row in (yr.get("rows") or []):
        v = row["metricValues"]
        years.append({"year": int(row["dimensionValues"][0]["value"]),
                      "users": int(v[0]["value"]), "pageviews": int(v[1]["value"]),
                      "sessions": int(v[2]["value"])})
    tot = runrep({"dateRanges": RANGE, "metrics": METRICS})
    tr = tot.get("rows") or []
    if tr:
        v = tr[0]["metricValues"]
        alltime = {"users": int(v[0]["value"]), "pageviews": int(v[1]["value"]), "sessions": int(v[2]["value"])}
    else:
        alltime = {"users": 0, "pageviews": 0, "sessions": 0}
    return {"years": years, "alltime": alltime}


def _ga4_returning(prop, token):
    """Returning vs new visitors — a loyalty signal (do people come back, or
    is it all one-and-done arrivals?). Share for the last 30 days and per
    calendar year. GA4's newVsReturning has a chunk of '(not set)' rows; the
    share is computed over classified users only."""
    def q(start, end="today"):
        body = {"dateRanges": [{"startDate": start, "endDate": end}],
                "dimensions": [{"name": "newVsReturning"}],
                "metrics": [{"name": "activeUsers"}]}
        rep = None
        for attempt in range(3):
            req = urllib.request.Request(
                f"https://analyticsdata.googleapis.com/v1beta/properties/{prop}:runReport",
                data=json.dumps(body).encode(),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=45) as r:
                    rep = json.loads(r.read())
                break
            except Exception:
                if attempt == 2: raise
                time.sleep(2 * (attempt + 1))
        m = {}
        for row in (rep.get("rows") or []):
            m[(row["dimensionValues"][0]["value"] or "").lower()] = int(row["metricValues"][0]["value"] or 0)
        nw, rt = m.get("new", 0), m.get("returning", 0)
        tot = nw + rt
        return {"new": nw, "returning": rt, "returning_pct": round(rt / tot * 100, 1) if tot else 0}
    cur = datetime.now(timezone.utc).year
    by_year = {}
    for y in range(2024, cur + 1):
        by_year[str(y)] = q(f"{y}-01-01", "today" if y == cur else f"{y}-12-31")
    return {"d30": q("30daysAgo"), "by_year": by_year}


def pull_ga4():
    """Website traffic from Google Analytics 4 (covers the pre-April-2026 site
    history Ghost's own analytics can't). Needs GA4_PROPERTY_ID + a
    service-account key in GA4_CREDS_JSON. Returns a stub with setup steps until
    both are present. Shape matches what the dashboard visitor tiles read
    (mau / sessions_30 / aau / sessions_365)."""
    prop = os.environ.get("GA4_PROPERTY_ID", "").strip()
    raw = os.environ.get("GA4_CREDS_JSON", "").strip()
    if not prop or not raw:
        return {"available": False, "reason": "GA4 service-account JSON + property ID not configured",
                "setup": GA4_SETUP}
    try:
        try:
            creds = json.loads(base64.b64decode(raw))
        except Exception:
            creds = json.loads(raw)
        token = _ga4_access_token(creds)
        u30, s30, p30 = _ga4_totals(prop, token, 30)
        u365, s365, p365 = _ga4_totals(prop, token, 365)
        ENG_SINCE = "2026-01-01"
        cur_year = datetime.now(timezone.utc).year
        try:
            # Reader attention since the start of 2026 (not a rolling 90d) — a
            # longer window, so bump the min-views floor to keep it meaningful.
            engagement = _ga4_engagement(prop, token, start_date=ENG_SINCE, min_views=100)
        except Exception as e:
            log(f"  ga4 engagement query failed: {e}"); engagement = []
        # Per-year + all-time engagement so the dashboard can offer the same
        # year/all-time toggle as the most-read leaderboards.
        eng_by_year, eng_alltime = {}, []
        try:
            eng_by_year[str(cur_year)] = engagement
            for y in range(2024, cur_year):
                eng_by_year[str(y)] = _ga4_engagement(
                    prop, token, start_date=f"{y}-01-01", end_date=f"{y}-12-31", min_views=100)
            eng_alltime = _ga4_engagement(prop, token, start_date="2020-01-01", min_views=100)
        except Exception as e:
            log(f"  ga4 per-year engagement failed: {e}")
        try:
            since_pages = _ga4_top_pages_since(prop, token, "2026-01-01")
        except Exception as e:
            log(f"  ga4 since-Jan top pages failed: {e}"); since_pages = []
        # Most-read pieces per calendar year (2024, 2025) + year-to-date 2026,
        # so the dashboard can offer a year toggle. 2026 reuses since_pages.
        top_by_year = {}
        try:
            top_by_year[str(cur_year)] = since_pages
            for y in range(2024, cur_year):
                top_by_year[str(y)] = _ga4_top_pages_since(
                    prop, token, f"{y}-01-01", end_date=f"{y}-12-31")
        except Exception as e:
            log(f"  ga4 per-year top pages failed: {e}")
        try:
            # All-time leaderboard: a single query over all of GA4's history
            # (can't sum the per-year top lists — a piece can lead all-time
            # without topping any single year).
            top_alltime = _ga4_top_pages_since(prop, token, "2020-01-01")
        except Exception as e:
            log(f"  ga4 all-time top pages failed: {e}"); top_alltime = []
        try:
            by_year = _ga4_by_year(prop, token)
        except Exception as e:
            log(f"  ga4 by-year failed: {e}"); by_year = {"years": [], "alltime": {}}
        try:
            returning = _ga4_returning(prop, token)
        except Exception as e:
            log(f"  ga4 returning-vs-new failed: {e}"); returning = {}
        try:
            traffic_weekly = _ga4_weekly_traffic(prop, token, "2025-01-01")
        except Exception as e:
            log(f"  ga4 weekly traffic failed: {e}"); traffic_weekly = []
        try:
            story_weekly = _ga4_story_weekly(prop, token)
        except Exception as e:
            log(f"  ga4 story weekly failed: {e}"); story_weekly = {"pages": [], "rows": []}
        try:
            piece_benchmarks = _ga4_piece_benchmarks(prop, token)
        except Exception as e:
            log(f"  ga4 piece benchmarks failed: {e}")
            piece_benchmarks = {"available": False, "reason": str(e)}
        try:
            piece_index = _ga4_piece_index(prop, token, piece_benchmarks)
        except Exception as e:
            log(f"  ga4 piece index failed: {e}")
            piece_index = {"available": False, "reason": str(e)}
        # Drop the working per-piece rows now that the index has consumed them
        # (they'd otherwise bloat the published payload with a duplicate copy).
        piece_benchmarks.pop("_pieces", None)
        try:
            ga4_events = _ga4_events(prop, token)
        except Exception as e:
            log(f"  ga4 events failed: {e}")
            ga4_events = {"available": False, "reason": str(e)}
        log(f"  ga4: {u30:,} users (30d); {u365:,} users (1y); {len(engagement)} eng; {len(since_pages)} since-Jan; {len(by_year.get('years',[]))} yrs")
        return {
            "available": True, "property_id": prop,
            "mau": u30, "sessions_30": s30, "pageviews_30": p30,
            "aau": u365, "sessions_365": s365, "pageviews_365": p365,
            "engagement": engagement, "engagement_since": ENG_SINCE,
            "engagement_by_year": eng_by_year, "engagement_alltime": eng_alltime,
            "top_pages_since": since_pages, "top_pages_since_date": "2026-01-01",
            "top_pages_by_year": top_by_year,
            "top_pages_alltime": top_alltime,
            "by_year": by_year, "returning": returning,
            "traffic_weekly": traffic_weekly,
            "story_weekly": story_weekly,
            "piece_benchmarks": piece_benchmarks,
            "piece_index": piece_index,
            "events": ga4_events,
            "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
    except Exception as e:
        log(f"  ga4: pull failed ({e})")
        return {"available": False, "reason": f"GA4 configured but the pull failed: {e}",
                "setup": GA4_SETUP}


GSC_SETUP = [
    "1) In Google Cloud (same project as GA4), enable the 'Google Search Console API'.",
    "2) In Search Console (search.google.com/search-console), open the vitalcitynyc.org property → Settings → Users and permissions → add the GA4 service-account email with Full or Restricted access.",
    "3) Nothing else — the pull reuses GA4_CREDS_JSON and auto-detects the property (or set GSC_SITE_URL, e.g. sc-domain:vitalcitynyc.org).",
]


def pull_search_console():
    """Google Search Console — how people find Vital City on Google: search
    queries, impressions, clicks, CTR and average position (last 28 days).
    Reuses the GA4 service account (added to the Search Console property);
    auto-detects the property unless GSC_SITE_URL is set. Stub until access."""
    raw = (os.environ.get("GSC_CREDS_JSON") or os.environ.get("GA4_CREDS_JSON") or "").strip()
    if not raw:
        return {"available": False, "reason": "no service-account creds", "setup": GSC_SETUP}
    try:
        try:
            creds = json.loads(base64.b64decode(raw))
        except Exception:
            creds = json.loads(raw)
        token = _sa_access_token(creds, "https://www.googleapis.com/auth/webmasters.readonly")
        site = os.environ.get("GSC_SITE_URL", "").strip()
        if not site:
            req = urllib.request.Request("https://searchconsole.googleapis.com/webmasters/v3/sites",
                                         headers={"Authorization": "Bearer " + token})
            with urllib.request.urlopen(req, timeout=30) as r:
                entries = json.loads(r.read()).get("siteEntry", [])
            usable = [e["siteUrl"] for e in entries if e.get("permissionLevel") != "siteUnverifiedUser"]
            site = (next((s for s in usable if "vitalcitynyc" in s and s.startswith("sc-domain:")), None)
                    or next((s for s in usable if "vitalcitynyc" in s), None)
                    or (usable[0] if usable else ""))
        if not site:
            return {"available": False, "reason": "service account can't see any Search Console property", "setup": GSC_SETUP}
        today = datetime.now(timezone.utc).date()
        end = today.isoformat()
        enc = urllib.parse.quote(site, safe="")

        # ---- Catalogue matcher: link each page-2 opportunity query to the
        # Vital City piece that already ranks for it (so the panel becomes an
        # action list, not just a diagnosis). Approximate, IDF-weighted keyword
        # overlap over unigrams AND adjacent-word phrases ("bigrams"), so
        # distinctive terms outweigh common ones ("tammany" beats "hall") and
        # proper nouns that split across the stopword filter still match
        # ("la guardia"->"laguardia", "co-op"->"coop", "Pelé"->"pele"). A hit is
        # accepted only when it's confident — either the query's weight is well
        # covered with a title/slug term, or a genuinely distinctive term
        # (idf>=5.3, i.e. in ~<1% of pieces) matches the title/slug. Otherwise
        # left blank: a possible content gap to eyeball, never a false link.
        import math as _math, unicodedata as _ud, re as _re
        _STOP = set("the a an of in on to for is are be and or vs why so how did what has does do "
                    "can will not new city nyc york at by with from your our who when where all more "
                    "most one make".split())
        def _fold(s): return _ud.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode()
        def _norm(t): return t[:-1] if len(t) > 3 and t.endswith("s") else t
        def _uni(s): return {_norm(w) for w in _re.findall(r"[a-z']+", _fold(s).lower())
                             if len(w) > 2 and w not in _STOP}
        def _bg(s):                                   # adjacent content-word phrases
            r = _re.findall(r"[a-z0-9']+", _fold(s).lower())
            return {r[i] + r[i+1] for i in range(len(r) - 1)
                    if r[i] not in _STOP and r[i+1] not in _STOP}
        def _strong(c): return _uni(c.get("title")) | _uni(c.get("slug")) | _bg(c.get("title")) | _bg(c.get("slug"))
        def _full(c): return _strong(c) | _uni(c.get("summary")) | _uni(c.get("excerpt")) \
                             | _bg(c.get("summary")) | {_norm(_fold(t).lower()) for t in (c.get("topics") or [])}
        _idx, _df = [], {}
        try:
            for c in json.loads((ROOT / "data" / "catalogue.json").read_text()):
                strong, full = _strong(c), _full(c)
                if full:
                    _idx.append((strong, full, c.get("title"), c.get("url")))
                    for t in full:
                        _df[t] = _df.get(t, 0) + 1
        except Exception as e:
            log(f"  search console: catalogue match unavailable ({e})")
        _N = len(_idx) or 1
        def _idf(t): return _math.log(1 + _N / _df.get(t, 0.5))
        _mmemo = {}
        # url -> title from the catalogue index built for match_piece. Lets the
        # Google-attributed pages carry a real headline instead of a slug.
        _by_url = {}
        for _s, _fl, _t, _u in _idx:
            if _u:
                _by_url[_u.rstrip("/")] = _t
        def _title_for_url(u):
            if not u: return ""
            key = u.split("?")[0].split("#")[0].rstrip("/")
            if key in _by_url: return _by_url[key]
            slug = key.split("/")[-1]
            for k, t in _by_url.items():
                if k.rstrip("/").endswith("/" + slug): return t
            return ""

        def match_piece(q):
            if q in _mmemo: return _mmemo[q]
            qt = _uni(q) | _bg(q); res = None
            if qt and _idx:
                W = sum(_idf(t) for t in qt) or 1.0
                best = None
                for strong, full, title, url in _idx:
                    inter = qt & full
                    if not inter: continue
                    cov = sum(_idf(t) for t in inter) / W
                    sint = qt & strong
                    tcov = sum(_idf(t) for t in sint) / W
                    smax = max((_idf(t) for t in sint), default=0.0)
                    k = (round(smax, 3), round(tcov, 3), round(cov, 3))
                    if best is None or k > best[0]: best = (k, title, url, cov, tcov, smax)
                if best and ((best[3] >= 0.55 and best[4] >= 0.30) or best[5] >= 5.3):
                    res = {"title": best[1], "url": best[2]}
            _mmemo[q] = res
            return res

        def query(body):
            req = urllib.request.Request(
                f"https://searchconsole.googleapis.com/webmasters/v3/sites/{enc}/searchAnalytics/query",
                data=json.dumps(body).encode(),
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read()).get("rows", []) or []
        def window(days):
            start = (today - timedelta(days=days)).isoformat()
            tr = query({"startDate": start, "endDate": end})
            t0 = tr[0] if tr else {}
            totals = {"clicks": int(t0.get("clicks", 0)), "impressions": int(t0.get("impressions", 0)),
                      "ctr": round((t0.get("ctr") or 0) * 100, 1), "position": round(t0.get("position") or 0, 1)}
            # Pull a wide slice (Search Console returns rows sorted by clicks
            # desc). The first 25 are the familiar "top queries by clicks"; the
            # long tail is where the page-2 opportunities hide — queries with
            # real impressions but too low a rank to earn clicks.
            qrows = query({"startDate": start, "endDate": end, "dimensions": ["query"], "rowLimit": 1000})
            allq = [{"query": r["keys"][0], "clicks": int(r.get("clicks", 0)),
                     "impressions": int(r.get("impressions", 0)),
                     "ctr": round((r.get("ctr") or 0) * 100, 1),
                     "position": round(r.get("position") or 0, 1)} for r in qrows]
            top_queries = allq[:25]
            # "Striking distance": ranked roughly on page two (avg position 8-20)
            # with enough impressions to matter — demand Google already ties to
            # Vital City, but where we sit too low for searchers to find us.
            # Impression floor scales gently with the window so longer windows
            # (more accumulated impressions) don't fill the list with noise.
            floor = max(10, days // 6)
            opps = [x for x in allq if 7.5 <= x["position"] <= 20.5 and x["impressions"] >= floor]
            # Rough upside: clicks we'd expect if the query reached the top of
            # page one. Assumes a ~10% click-through at a top-3 rank (a
            # deliberately conservative point on published organic CTR curves);
            # clearly an estimate, surfaced only to help prioritize.
            for x in opps:
                x["potential_clicks"] = max(0, round(x["impressions"] * 0.10) - x["clicks"])
            opps.sort(key=lambda x: -x["impressions"])
            top_opps = opps[:15]
            for x in top_opps:            # link each to the VC piece that ranks for it
                x["piece"] = match_piece(x["query"])
            return {"totals": totals, "top_queries": top_queries,
                    "opportunities": top_opps, "opp_impr_floor": floor}
        # Short / medium / long windows the dashboard can toggle between.
        WINDOWS = [28, 90, 365]
        windows = {str(d): window(d) for d in WINDOWS}
        default = windows["28"]
        log(f"  search console: {default['totals']['clicks']:,} clicks / {default['totals']['impressions']:,} impressions (28d), windows {WINDOWS} ({site})")

        # ---- Top New York politics/policy searches, year-to-date, regardless of
        # whether the searcher clicked through to Vital City. Search Console can
        # only report queries where a VC page *appeared* in results at least
        # once, so this is the broadest free demand signal we have — NOT the whole
        # search universe (that needs Keyword Planner or a paid SEO tool). Ranked
        # by impressions (how often searched-and-shown). Branded VC lookups are
        # dropped and a keyword filter keeps it to NYC politics/policy.
        ytd_start = today.replace(month=1, day=1).isoformat()
        _BRAND = re.compile(r"vital\s*-?\s*city", re.I)
        # NYC relevance: places, political figures, agencies and NYC-flavored
        # policy topics. A query needs at least one to count. Heuristic — errs
        # toward inclusion; the dashboard notes it's an automated filter.
        # Short/ambiguous tokens get word boundaries (so "doe" doesn't match
        # "does"); longer stems match open-ended (so "rent stabiliz" catches
        # "stabilized" and "migrant" catches "migrants").
        _NYC = re.compile(
            r"(?:\b(?:nyc|bronx|koch|doe|dob|hpd|tlc|mta|g and t|3-?k|pre-?k)\b)"
            r"|new york|manhattan|brooklyn|queens|staten island|harlem"
            r"|mamdani|cuomo|adams|hochul|bloomberg|de ?blasio|giuliani|dinkins|lindsay"
            r"|la ?guardia|sliwa|brad lander|\blander\b|mark levine|tish james|letitia"
            r"|nypd|fdny|nycha|rikers|tammany|city council|city hall|comptroller"
            r"|public advocate|borough president|albany"
            r"|congestion|rent stabiliz|rent control|rent freeze|right to shelter|broker fee"
            r"|co-?op city|nimby|upzon|migrant|asylum|outdoor dining|open streets"
            r"|specialized high school|gifted and talented|zoning|homeless|shelter"
            r"|precinct|stop and frisk|subway", re.I)
        # Google's own query->page attribution. Far better than guessing from the
        # title: match_piece() is a fuzzy text matcher and produced false
        # "nothing matched" rows for queries that plainly do rank (e.g. "mamdani
        # approval rating" -> /assessing-mamdani-six-months-in/). Used first;
        # the fuzzy matcher stays only as a fallback where Google attributes
        # nothing.
        qpage = {}
        try:
            for r in query({"startDate": ytd_start, "endDate": end,
                            "dimensions": ["query", "page"], "rowLimit": 25000}):
                q, url = r["keys"][0], r["keys"][1]
                c = int(r.get("clicks", 0)), int(r.get("impressions", 0))
                # keep the best-performing page per query
                if q not in qpage or c > qpage[q][1]:
                    qpage[q] = (url, c)
            log(f"  search console: query->page attribution for {len(qpage):,} queries")
        except Exception as e:
            log(f"  search console: query+page pull failed ({e}) — falling back to title matching")

        def attributed(q):
            """Google's attribution first, then the fuzzy title matcher."""
            hit = qpage.get(q)
            if hit:
                url = hit[0]
                t = _title_for_url(url)
                return {"title": t or url.rstrip("/").split("/")[-1].replace("-", " ").title(),
                        "url": url, "source": "google"}
            m = match_piece(q)
            if m:
                m = dict(m); m["source"] = "title-match"
            return m

        # Per-query direction: first half of the year to date vs second half.
        # Answers "is this search growing or fading", which a flat annual total
        # cannot. Sampled on the top queries only, to keep the request small.
        trend, trend_base = {}, {}
        try:
            drows = query({"startDate": ytd_start, "endDate": end,
                           "dimensions": ["query", "date"], "rowLimit": 25000})
            from collections import defaultdict as _dd
            byq = _dd(list)
            for r in drows:
                byq[r["keys"][0]].append((r["keys"][1], int(r.get("impressions", 0))))
            for q, pts in byq.items():
                if len(pts) < 6:
                    continue                      # too little history to call
                pts.sort()
                half = len(pts) // 2
                a = sum(v for _, v in pts[:half]) or 0
                b = sum(v for _, v in pts[half:]) or 0
                if a < 100:
                    continue                      # tiny base makes the ratio noise
                trend[q] = round((b - a) / a * 100)
                trend_base[q] = a                 # so the page can say what the % is measured from
            log(f"  search console: trend direction for {len(trend):,} queries")
        except Exception as e:
            log(f"  search console: query+date pull failed ({e})")

        try:
            yrows = query({"startDate": ytd_start, "endDate": end,
                           "dimensions": ["query"], "rowLimit": 5000})
            topic = []
            for r in yrows:
                q = r["keys"][0]
                if _BRAND.search(q) or not _NYC.search(q):
                    continue
                topic.append({"query": q, "clicks": int(r.get("clicks", 0)),
                              "impressions": int(r.get("impressions", 0)),
                              "ctr": round((r.get("ctr") or 0) * 100, 1),
                              "position": round(r.get("position") or 0, 1),
                              "trend": trend.get(q),
                              "trend_base": trend_base.get(q),
                              "piece": attributed(q)})
            topic.sort(key=lambda x: -x["impressions"])
            topic_searches = topic[:40]
            # Searches about AI itself, kept as their own rollup. The topic list
            # is capped at 40 by impressions and is dominated by the mayoralty,
            # so AI queries never surfaced in it even when they existed. Matched
            # on whole words: a substring test counts "aid", "said" and "chair".
            _AI = re.compile(r"\b(a\.?i\.?|artificial intelligence|chatgpt|openai|"
                             r"llm|large language model|machine learning|algorithmic|"
                             r"algorithm|automation|chatbot)\b", re.I)
            ai_rows = [{"query": r["keys"][0], "clicks": int(r.get("clicks", 0)),
                        "impressions": int(r.get("impressions", 0)),
                        "ctr": round((r.get("ctr") or 0) * 100, 1),
                        "position": round(r.get("position") or 0, 1)}
                       for r in yrows if _AI.search(r["keys"][0])]
            ai_rows.sort(key=lambda x: -x["impressions"])
            ai_clicks = sum(r["clicks"] for r in ai_rows)
            ai_impr   = sum(r["impressions"] for r in ai_rows)
            all_clicks = sum(int(r.get("clicks", 0)) for r in yrows) or 1
            all_impr   = sum(int(r.get("impressions", 0)) for r in yrows) or 1
            ai_searches = {"queries": ai_rows[:40], "n_queries": len(ai_rows),
                           "clicks": ai_clicks, "impressions": ai_impr,
                           "pct_clicks": round(ai_clicks / all_clicks * 100, 2),
                           "pct_impressions": round(ai_impr / all_impr * 100, 2),
                           "since": ytd_start}
            log(f"  search console: AI-related searches — {ai_clicks:,} clicks, "
                f"{ai_impr:,} impressions across {len(ai_rows)} queries "
                f"({ai_searches['pct_clicks']}% of clicks)")
            log(f"  search console: {len(topic_searches)} top NYC politics/policy searches YTD (from {len(yrows)} queries since {ytd_start})")
        except Exception as e:
            log(f"  search console: top-searches pull failed ({e})")
            ai_searches = {"queries": [], "n_queries": 0, "clicks": 0, "impressions": 0,
                           "pct_clicks": 0, "pct_impressions": 0, "since": ytd_start}
            topic_searches = []

        # Channels beyond web search. Discover has no query dimension by design
        # (nobody searched — Google pushed it), so it is reported by page.
        channels = {}
        d90 = (today - timedelta(days=93)).isoformat()
        for key, body, label in (
            ("discover", {"type": "discover"}, "Discover"),
            ("google_news", {"type": "googleNews"}, "Google News"),
            ("news_appearance", {"type": "news"}, "News search results"),
        ):
            try:
                tot = query({"startDate": d90, "endDate": end, **body})
                t0 = tot[0] if tot else {}
                entry = {"label": label,
                         "clicks": int(t0.get("clicks", 0)),
                         "impressions": int(t0.get("impressions", 0)),
                         "ctr": round((t0.get("ctr") or 0) * 100, 2)}
                if key == "discover":
                    entry["top_pages"] = [
                        {"url": r["keys"][0], "title": _title_for_url(r["keys"][0]),
                         "clicks": int(r.get("clicks", 0)),
                         "impressions": int(r.get("impressions", 0))}
                        for r in query({"startDate": d90, "endDate": end,
                                        "dimensions": ["page"], "rowLimit": 15, **body})]
                channels[key] = entry
            except Exception as e:
                log(f"  search console: {label} pull failed ({e})")
                channels[key] = {"label": label, "unavailable": str(e)[:120]}
        if channels:
            log("  search console: channels " + ", ".join(
                f"{v.get('label')}={v.get('clicks','n/a')}cl" for v in channels.values()))

        return {"available": True, "site": site, "window_days": 28,
                "windows": windows, "windows_avail": WINDOWS,
                "totals": default["totals"], "top_queries": default["top_queries"], "as_of": end,
                "topic_searches": topic_searches, "topic_search_start": ytd_start,
                "ai_searches": ai_searches,
                "channels": channels, "channels_window_start": d90}
    except Exception as e:
        log(f"  search console pull failed: {e}")
        return {"available": False, "reason": f"Search Console configured but the pull failed: {e}", "setup": GSC_SETUP}


def pull_ghost_traffic():
    """Site traffic (unique visitors, page views) from Ghost's own analytics.

    Path: staff JWT → /tinybird/token/ (the same token the admin UI uses) →
    query the Tinybird api_kpis pipe for daily visits/pageviews. Requires a
    staff access token; integration keys are forbidden from every stats
    endpoint. Returns {"available": False, reason} until that's configured.
    Field names from Tinybird are version-dependent, so raw rows are stored
    alongside the computed numbers for debuggability."""
    tok = _ghost_staff_token()
    if not tok:
        return {"available": False,
                "reason": "no Ghost staff token — free 5-minute setup, see the How it works page"}
    hdr = {"Authorization": "Ghost " + tok, "Accept-Version": "v5.0", "User-Agent": UA}
    def gj(path):
        return json.loads(http_get(GHOST_ADMIN_API + path, headers=hdr, timeout=30))
    out = {"available": False, "source": "ghost-analytics (Tinybird)"}
    try:
        cfg = (gj("/config/").get("config") or {})
        stats_cfg = cfg.get("stats") or {}
        endpoint, site = stats_cfg.get("endpoint") or "https://api.tinybird.co", stats_cfg.get("id")
        tb = gj("/tinybird/token/")
        token = (tb.get("tinybird") or {}).get("token") or tb.get("token")
        if not (token and site):
            out["reason"] = "staff token works but no Tinybird token/site id in config"
            return out
        today = datetime.now(timezone.utc).date()
        def tbq(date_from, date_to=None):
            q = urllib.parse.urlencode({"site_uuid": site, "date_from": str(date_from),
                                        "date_to": str(date_to or today)})
            return json.loads(http_get(f"{endpoint}/v0/pipes/api_kpis.json?{q}",
                headers={"Authorization": "Bearer " + token}, timeout=30)).get("data") or []
        def agg(rows):
            # Tolerate field-name drift across Ghost/Tinybird versions
            v = sum((r.get("visits") or r.get("visitors") or 0) for r in rows)
            pv = sum((r.get("pageviews") or r.get("page_views") or 0) for r in rows)
            return int(v), int(pv)
        rows30  = tbq(today - timedelta(days=30))
        rows365 = tbq(today - timedelta(days=365))
        out["visitors_30d"],  out["pageviews_30d"]  = agg(rows30)
        out["visitors_365d"], out["pageviews_365d"] = agg(rows365)
        # Prior 30 days (days 30-60 ago) so the dashboard can show % change.
        prev30 = tbq(today - timedelta(days=60), today - timedelta(days=30))
        out["visitors_prev_30d"], out["pageviews_prev_30d"] = agg(prev30)
        # First month with REAL traffic. Tinybird has a trickle back to
        # 2025-08 (6-36 visits/month — staging/preview hits during the Ghost
        # build-out) and then jumps to ~37K/month in 2026-03 when the site
        # actually went live. "First nonzero day" would mislabel the window
        # by seven months, so: first month with at least 1,000 visits, then
        # that month's first day with traffic. The dashboard labels the tile
        # "since YYYY-MM" with this so a partial history is never passed off
        # as a full year.
        monthly = {}
        for r in rows365:
            d = r.get("date") or ""
            if d: monthly[d[:7]] = monthly.get(d[:7], 0) + (r.get("visits") or r.get("visitors") or 0)
        real_months = sorted(m for m, v in monthly.items() if v >= 1000)
        if real_months:
            m0 = real_months[0]
            days = sorted(r.get("date") for r in rows365
                          if (r.get("date") or "").startswith(m0)
                          and (r.get("visits") or r.get("visitors") or 0) > 0)
            out["history_start"] = days[0] if days else m0 + "-01"
        else:
            out["history_start"] = None
        out["kpi_rows_30d"] = rows30[:40]   # raw sample for field-name debugging
        # Weekly visitors + page views over the real-traffic window, for the
        # overall-traffic TREND chart. Bucket daily api_kpis rows by ISO week
        # (Monday start) from history_start onward (skips the pre-launch trickle).
        wseries = {}
        wstart = out.get("history_start") or ""
        for r in rows365:
            d = (r.get("date") or "")[:10]
            if not d or (wstart and d < wstart):
                continue
            try:
                dd = datetime.strptime(d, "%Y-%m-%d").date()
            except Exception:
                continue
            wk = (dd - timedelta(days=dd.weekday())).isoformat()
            s = wseries.setdefault(wk, {"wk": wk, "visitors": 0, "pageviews": 0})
            s["visitors"]  += int(r.get("visits") or r.get("visitors") or 0)
            s["pageviews"] += int(r.get("pageviews") or r.get("page_views") or 0)
        ser = [wseries[k] for k in sorted(wseries)]
        # Trim partial bookend weeks so they don't distort the trend line:
        #  - leading: if Ghost recording began mid-week (history_start isn't a
        #    Monday), that first ISO-week bucket holds only a day or two of real
        #    data — drop it (e.g. the 2026-02-23 sliver from a 2026-03-01 start).
        #  - trailing: the current week, when only 1-2 days in, is too partial
        #    to plot honestly — drop it until at least 3 days have elapsed.
        if ser and wstart:
            try:
                hs = datetime.strptime(wstart[:10], "%Y-%m-%d").date()
                hs_monday = (hs - timedelta(days=hs.weekday())).isoformat()
                if hs.weekday() != 0 and ser and ser[0]["wk"] == hs_monday:
                    ser = ser[1:]
            except Exception:
                pass
        if ser:
            cur_monday = (today - timedelta(days=today.weekday())).isoformat()
            if ser[-1]["wk"] == cur_monday:
                # The current week is always incomplete (Mon-Sun isn't over yet).
                if today.weekday() < 2:      # Mon/Tue: barely 1-2 days — drop the stub
                    ser = ser[:-1]
                else:                        # mid-week: keep it but flag it partial so the
                    ser[-1]["partial"] = True  # chart doesn't read the dip as a real decline
        out["traffic_series"] = ser
        # Top pieces by visits (the "top performers" view) over 7 and 30 days.
        # Exclude the homepage, jobs board, tag/author index pages; map each
        # pathname to its article title from the catalogue.
        cat = {}
        try:
            for c in json.loads((ROOT / "data" / "catalogue.json").read_text()):
                u = (c.get("url") or "").rstrip("/")
                if u: cat["/" + u.split("/")[-1] + "/"] = c.get("title")
        except Exception:
            pass
        # path -> visits over an explicit [from, to] window (whole list, for deltas)
        def pages_map(d_from, d_to):
            try:
                q = urllib.parse.urlencode({"site_uuid": site, "date_from": str(d_from),
                                            "date_to": str(d_to), "limit": 300})
                tp = json.loads(http_get(f"{endpoint}/v0/pipes/api_top_pages.json?{q}",
                    headers={"Authorization": "Bearer " + token}, timeout=30)).get("data") or []
            except Exception as e:
                log(f"  ghost top pages ({d_from}..{d_to}) failed: {e}"); return {}
            m = {}
            for r in tp:
                path = (r.get("pathname") or "").strip()
                if path: m[path] = m.get(path, 0) + int(r.get("visits") or r.get("hits") or 0)
            return m
        def is_article(path):
            return not (path in ("/", "") or "/job" in path or path in ("/about/", "/about")
                        or path.startswith("/tag/") or path.startswith("/author/"))
        def build_pages(cur, prev, limit):
            pages = []
            for path, v in sorted(cur.items(), key=lambda kv: -kv[1]):
                if not is_article(path): continue
                title = cat.get(path) or path.strip("/").replace("-", " ").title()
                pages.append({"title": title, "path": path, "visits": v, "prev": prev.get(path, 0)})
                if len(pages) >= limit: break
            return pages
        cur30  = pages_map(today - timedelta(days=30), today)
        prev30p = pages_map(today - timedelta(days=60), today - timedelta(days=30))
        cur7   = pages_map(today - timedelta(days=7), today)
        # Topic rollups from the FULL page map, not the top-12 slice: the API
        # already returns 300 rows per window, so this costs no extra call.
        # Window note: Ghost analytics only begins 2026-03-01 -- everything
        # before that lived in Google Analytics, which is not connected, so
        # these shares describe 2026 and cannot be extended backwards.
        tmap = catalogue_topic_map()
        def slug_counts(pmap):
            out_ = {}
            for path, v in pmap.items():
                if not is_article(path): continue
                sl = path.strip("/").rsplit("/", 1)[-1].lower()
                if sl: out_[sl] = out_.get(sl, 0) + v
            return out_
        out["traffic_by_topic_30d"] = by_topic(slug_counts(cur30), tmap, "visits 30d")
        out["top_pages_30d"] = build_pages(cur30, prev30p, 12)
        out["top_pages_7d"]  = build_pages(cur7, {}, 8)   # 7d list (week pulse) — no delta
        # All-time leaders since the Ghost handoff (history_start onward).
        hs = out.get("history_start")
        if hs:
            since_map = pages_map(hs, today)
            out["top_pages_since_launch"] = build_pages(since_map, {}, 15)
            out["top_pages_since"] = hs
            out["traffic_by_topic_since"] = by_topic(slug_counts(since_map), tmap, "visits since launch")
        else:
            out["top_pages_since_launch"] = []
        # Where the site's traffic comes from (referrer sources): this 30d vs prior 30d.
        def sources_map(d_from, d_to):
            try:
                q = urllib.parse.urlencode({"site_uuid": site, "date_from": str(d_from),
                                            "date_to": str(d_to), "limit": 40})
                sd = json.loads(http_get(f"{endpoint}/v0/pipes/api_top_sources.json?{q}",
                    headers={"Authorization": "Bearer " + token}, timeout=30)).get("data") or []
            except Exception as e:
                log(f"  ghost top sources ({d_from}..{d_to}) failed: {e}"); return {}
            m = {}
            for r in sd:
                name = (r.get("source") or r.get("referrer") or r.get("referrer_source") or "").strip() or "Direct / none"
                m[name] = m.get(name, 0) + int(r.get("visits") or r.get("hits") or 0)
            return m
        try:
            cur_src  = sources_map(today - timedelta(days=30), today)
            prev_src = sources_map(today - timedelta(days=60), today - timedelta(days=30))
            srcs = [{"source": n, "visits": v, "prev": prev_src.get(n, 0)}
                    for n, v in sorted(cur_src.items(), key=lambda kv: -kv[1]) if v > 0]
            # 40, not 10: AI assistants (ChatGPT, Perplexity, Copilot, Gemini)
            # send small but fast-growing referral volumes that sat just below
            # a ten-row cut, so the question "how much traffic comes from AI
            # tools" could not be answered from the stored data at all.
            out["top_sources_30d"] = srcs[:40]
        except Exception as e:
            log(f"  ghost top sources failed: {e}")
            out["top_sources_30d"] = []
        out["available"] = bool(rows30 or rows365)
        if not out["available"]:
            out["reason"] = "Tinybird responded but returned no rows"
        return out
    except Exception as e:
        out["reason"] = f"stats path failed: {e}"
        log(f"  ghost traffic: {out['reason']}")
        return out


def catalogue_topic_map():
    """slug -> (topics, title) from data/catalogue.json.

    Vital City tags a piece with several topics, and the tag vocabulary mixes
    three different things: real subjects, issue-section rubrics ("Setting the
    Stage") and format labels ("Q&A"). Only subjects describe what a piece is
    ABOUT, so the rubric and format buckets computed by analyze_subjects.py are
    excluded here -- counting them would report that Vital City's biggest
    coverage area is opening an issue.
    """
    try:
        cat = json.loads((ROOT / "data" / "catalogue.json").read_text())
    except Exception as e:
        log(f"  catalogue topic map: unreadable ({e})")
        return {}
    items = cat if isinstance(cat, list) else (cat.get("items") or cat.get("posts") or [])
    skip = set()
    try:
        sa = json.loads((ROOT / "data" / "subject_analysis.json").read_text())
        # analyze_subjects.py names these buckets "rubrics_listed" (issue
        # section headers) and "meta_listed" (format labels like "Book Review").
        # Getting these key names wrong fails silently -- the skip set comes
        # back empty and formats show up as if they were subjects.
        for key in ("rubrics_listed", "meta_listed"):
            v = sa.get(key)
            if isinstance(v, list):
                skip |= {(x.get("tag") if isinstance(x, dict) else str(x)).lower().strip()
                         for x in v}
    except Exception:
        pass   # no subject analysis yet: fall back to every tag
    out = {}
    for c in items:
        sl = (c.get("slug") or "").strip().lower()
        if not sl: continue
        tops = [t for t in (c.get("topics") or [])
                if isinstance(t, str) and t.lower().strip() not in skip]
        out[sl] = (tops, c.get("title") or sl)
    return out


def by_topic(counts, topic_map, label):
    """Aggregate {slug: n} into per-topic totals and shares.

    A piece carries several subjects, so its readers are credited to each of
    them -- shares therefore sum above 100%, and the note says so rather than
    dividing a reader into fractions and implying a precision the tagging
    cannot support. `matched` is the honest coverage figure: how much of the
    measured total could be tied to a tagged piece at all.
    """
    tot = sum(counts.values())
    per, matched, untagged = {}, 0, 0
    for slug, n in counts.items():
        ent = topic_map.get(slug)
        if not ent:
            continue
        matched += n
        if not ent[0]:
            untagged += n
            continue
        for t in ent[0]:
            per[t] = per.get(t, 0) + n
    rows = [{"topic": t, "n": n, "pct": round(n / matched * 100, 1) if matched else 0}
            for t, n in sorted(per.items(), key=lambda kv: -kv[1])]
    log(f"  {label} by topic: {len(rows)} topics, {matched:,} of {tot:,} matched to a piece")
    return {"rows": rows, "total": tot, "matched": matched, "untagged": untagged,
            "unmatched": tot - matched}


def pull_ghost_signup_attribution(days_back=180):
    """REAL per-post signup attribution from Ghost's member-events feed.
    Each signup_event carries the exact page the person signed up on plus
    referrer_source/medium. This is the actual answer — not a 4-day
    post-publish window correlation. Replaces the older correlational
    proxy we used before this endpoint was wired in.
    """
    import time as _t
    tok = _ghost_admin_token()
    if not tok:
        return {"available": False, "reason": "no Ghost admin key"}
    since_iso = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()
    by_url = {}
    by_day = {}            # day -> count of signup events (canonical signup source-of-truth)
    by_source = {}         # referrer_source -> count (Direct, Google, newsletter, LinkedIn, etc.)
    by_medium = {}         # referrer_medium -> count (search, email, social, etc.)
    by_landing = {}        # attribution.type (post|page|url) -> count (where they signed up)
    signup_slugs = {}      # article slug -> signups it landed, for the topic rollup
    by_email  = {}         # email -> {source, medium, landing_url, ts} (for channel-LTV join)
    recent = []            # last-21d signups w/ name+email+date for the click-through list
    recent_cut = (datetime.now(timezone.utc) - timedelta(days=21)).isoformat()
    fetched = 0
    stop = False
    cursor_id = None       # last event id from previous page (cursor pagination)
    pages = 0
    # Ghost's /members/events/ endpoint ignores `page=N` (always returns page 1).
    # It also rejects `created_at` in the filter ("Cannot filter by created_at").
    # The supported workaround is cursor pagination via `id:<lastId` — Ghost
    # event ids are lexicographically time-sortable, so this walks the feed
    # newest-first reliably.
    while not stop:
        flt_str = "type:signup_event"
        if cursor_id:
            flt_str += f"+id:<{cursor_id}"
        url = f"{GHOST_ADMIN_API}/members/events/?filter={urllib.parse.quote(flt_str)}&limit=100"
        try:
            data = json.loads(http_get(url, headers={
                "Authorization": f"Ghost {tok}", "Accept-Version": "v5.0",
            }, timeout=60))
        except Exception as e:
            log(f"  ghost member-events page {pages+1} failed: {e}")
            break
        events = data.get("events", []) or []
        if not events:
            break
        for e in events:
            d = e.get("data") or {}
            ts = (d.get("created_at") or "")
            if ts and ts < since_iso:
                stop = True
                continue
            # Daily total — every signup, regardless of attribution
            if ts:
                day = ts[:10]
                by_day[day] = by_day.get(day, 0) + 1
            # Recent-signups list (names behind the 7d-box "New signups" tile)
            if ts and ts >= recent_cut:
                m = d.get("member") or {}
                em = (m.get("email") or "").strip()
                if em:
                    _att = d.get("attribution") or {}
                    recent.append({"email": em, "name": (m.get("name") or "").strip(),
                                   "date": ts[:10],
                                   "source": _att.get("referrer_source") or "",
                                   "landing_url": (_att.get("url") or "").rstrip("/"),
                                   "landing_title": _att.get("title") or "",
                                   "landing_type": _att.get("type") or ""})
            att = d.get("attribution") or {}
            # Flat aggregates — capture every signup's source, not just the
            # ones that attributed to a specific post. Homepage signups are
            # the bulk of volume; they'd be invisible if we only looked at
            # per-URL counts.
            src = (att.get("referrer_source") or "(unknown)").strip() or "(unknown)"
            by_source[src] = by_source.get(src, 0) + 1
            med = (att.get("referrer_medium") or "(none)").strip() or "(none)"
            by_medium[med] = by_medium.get(med, 0) + 1
            ltype = (att.get("type") or "unknown")
            by_landing[ltype] = by_landing.get(ltype, 0) + 1
            # Landing slug, for the by-topic rollup. Only post landings can be
            # tied to a piece; homepage and tag signups have no subject.
            if ltype == "post":
                _u = (att.get("url") or "").rstrip("/")
                if _u:
                    signup_slugs[_u.rsplit("/", 1)[-1].lower()] = \
                        signup_slugs.get(_u.rsplit("/", 1)[-1].lower(), 0) + 1
            # Per-email source map (for channel-LTV join with donor data).
            # First-touch attribution: if a user re-signs up later, keep the
            # earliest recorded source.
            mem = d.get("member") or {}
            mem_em = ((mem.get("email") or "")).lower().strip()
            if mem_em and mem_em not in by_email:
                # landing URL kept per person: it is the only way to ask "who
                # signed up on a piece about X" for anyone older than the
                # 21-day recent_signups window, which the event-invite work
                # needs. Stripped before the public JSON write like the rest.
                by_email[mem_em] = {"source": src, "medium": med, "type": ltype, "ts": ts,
                                    "landing_url": (att.get("url") or ""), "landing_title": (att.get("title") or "")}
            post_url = (att.get("url") or "").rstrip("/")
            if not post_url: continue
            r = by_url.setdefault(post_url, {
                "signups": 0, "title": att.get("title") or "", "type": att.get("type") or "",
                "first_seen": "", "last_seen": "", "sources": {},
            })
            r["signups"] += 1
            src = att.get("referrer_source") or "(none)"
            r["sources"][src] = r["sources"].get(src, 0) + 1
            if not r["first_seen"] or ts < r["first_seen"]: r["first_seen"] = ts
            if not r["last_seen"]  or ts > r["last_seen"]:  r["last_seen"]  = ts
        fetched += len(events)
        pages += 1
        # Advance cursor to the last (oldest) event on this page
        cursor_id = (events[-1].get("data") or {}).get("id")
        if not cursor_id:
            break
        if pages > 250:        # safety — ~25,000 events
            break
        _t.sleep(0.05)         # gentle pacing
    # Normalize: pick top 3 sources per url
    for url, r in by_url.items():
        srcs = sorted(r["sources"].items(), key=lambda kv: -kv[1])[:3]
        r["top_sources"] = [{"src": s, "n": n} for s, n in srcs]
        r["first_seen"] = r["first_seen"][:10]
        r["last_seen"]  = r["last_seen"][:10]
        del r["sources"]
    log(f"  ghost signup attribution: {fetched} signup events across {len(by_url)} URLs, {len(by_day)} days, {len(by_source)} sources, {len(by_email)} per-email entries")
    # The cursor walk should end by crossing since_iso. If it ended any other
    # way (empty page, missing id), coverage is silently shorter than the
    # requested window — say so loudly instead of letting the card imply
    # full-window data.
    if not stop and fetched:
        oldest = min(by_day) if by_day else "?"
        log(f"  WARNING: events feed ended early — coverage starts {oldest}, "
            f"short of the requested {days_back}-day window (feed returned an "
            f"empty page or an event without an id).")
    return {
        "available":      True,
        "events_counted": fetched,
        "by_url":         by_url,
        "by_day":         [{"d": d, "subs": n} for d, n in sorted(by_day.items())],
        "by_source":      [{"src": s, "n": n} for s, n in sorted(by_source.items(), key=lambda kv: -kv[1])],
        "by_medium":      [{"med": m, "n": n} for m, n in sorted(by_medium.items(), key=lambda kv: -kv[1])],
        "by_landing":     [{"type": t, "n": n} for t, n in sorted(by_landing.items(), key=lambda kv: -kv[1])],
        "by_topic":       by_topic(signup_slugs, catalogue_topic_map(), "signups"),
        "window_days":    days_back,
        "recent_signups": sorted(recent, key=lambda r: r["date"], reverse=True),
        "_by_email":      by_email,   # internal — used for channel-LTV join, stripped before JSON write
    }


def pull_recent_unsubs(days=21):
    """Recent unsubscribers with email + date, for the 7d-box click-through.
    Source: Mailchimp list members with status=unsubscribed, sorted by
    last_changed descending and filtered with since_last_changed. This captures
    EVERY unsubscribe — campaign-link, profile page, admin or API — current to
    today, unlike the per-campaign /reports/{id}/unsubscribed detail which only
    sees campaign-link unsubscribes and therefore lags (it was leaving the 7-day
    box's count unlinked once the newest campaign-attributed unsub aged out).
    Names come from merge fields when Mailchimp has them."""
    key = mailchimp_key()
    if not key: return []
    dc = key.split("-")[-1]
    list_id = os.environ.get("MAILCHIMP_LIST", "ec30bf0c4b")
    def mc(path):
        auth = base64.b64encode(f"anystring:{key}".encode()).decode()
        return json.loads(http_get(f"https://{dc}.api.mailchimp.com/3.0{path}",
            headers={"Authorization": "Basic " + auth}, timeout=60))
    cut = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    out, seen, offset = [], set(), 0
    try:
        while offset < 2000:
            mems = mc(f"/lists/{list_id}/members?status=unsubscribed"
                      f"&sort_field=last_changed&sort_dir=DESC&count=200&offset={offset}"
                      f"&since_last_changed={urllib.parse.quote(cut)}&fields="
                      "members.email_address,members.last_changed,"
                      "members.merge_fields.FNAME,members.merge_fields.LNAME").get("members", [])
            for m in mems:
                ts = (m.get("last_changed") or "")
                em = (m.get("email_address") or "").lower().strip()
                if not em or em in seen or ts < cut: continue
                seen.add(em)
                mf = m.get("merge_fields") or {}
                nm = f"{mf.get('FNAME','')} {mf.get('LNAME','')}".strip()
                out.append({"email": em, "name": nm, "date": ts[:10]})
            if len(mems) < 200: break
            offset += 200
    except Exception as e:
        log(f"  recent unsubs failed: {e}")
    log(f"  recent unsubs (last {days}d): {len(out)}")
    return sorted(out, key=lambda r: r["date"], reverse=True)


def pull_ghost():
    out = {"available": True, "posts": []}
    since = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ")
    flt = urllib.parse.quote(f"published_at:>={since}")
    page = 1
    while True:
        try:
            url = (f"{GHOST_API}/posts/?key={GHOST_CONTENT_KEY}&filter={flt}"
                   f"&include=authors,tags&limit=100&page={page}&fields=id,title,slug,url,published_at,reading_time")
            data = json.loads(http_get(url))
        except Exception as e:
            log(f"  ghost posts failed: {e}")
            break
        for p in data.get("posts", []):
            out["posts"].append({
                "title": p.get("title"),
                "url":   p.get("url"),
                "published": (p.get("published_at") or "")[:10],
                "reading_time": p.get("reading_time") or 0,
                "primary_author": ((p.get("primary_author") or {}).get("name")) or "",
                "tags": [t.get("name") for t in (p.get("tags") or []) if t.get("name")],
            })
        meta = (data.get("meta") or {}).get("pagination") or {}
        if not meta.get("next"): break
        page = meta["next"]
    out["posts"].sort(key=lambda p: p["published"], reverse=True)
    # Counts
    today = datetime.now(timezone.utc).date()
    def _cnt(days):
        cut = (today - timedelta(days=days)).isoformat()
        return sum(1 for p in out["posts"] if p["published"] >= cut)
    out["count_7"]  = _cnt(7)
    out["count_30"] = _cnt(30)
    out["count_90"] = len(out["posts"])
    # Every publication date, from the catalogue. The posts list above is a
    # 90-day feed, which is fine for the recent-posts card but useless as a
    # comparison base: a 90-day window compared against "the 90 days before"
    # found 2 posts there and reported +2950%. Dates alone are ~11 KB for the
    # whole archive, so the pulse tiles can count any window and build a real
    # trailing-year norm.
    try:
        cat = json.loads((ROOT / "data" / "catalogue.json").read_text())
        out["pub_dates"] = sorted(str(x.get("published_date") or "")[:10]
                                  for x in cat if x.get("published_date"))
    except Exception as e:
        log(f"  ghost: could not read catalogue for pub_dates ({e})")
    return out


# ----------------------------------------------- Media mentions (third-party press)
# Two distinct whitelists:
#   MEDIA_OUTLETS — actual news/policy publications that cover us. For these,
#     query "Vital City" + @ handle + URL-share. The brand-phrase search works
#     on press sites because they use the name intentionally.
#   SOCIAL_PLATFORMS — X, LinkedIn, Bluesky, Instagram. Here we DROP the
#     brand-phrase shape (people use "vital city" generically — "Karachi
#     remains Pakistan's most inclusive and economically vital city..." etc.)
#     and only search @vitalcitynyc + vitalcitynyc.org URL shares. Those are
#     unambiguous.
MEDIA_OUTLETS = [
    # NYC outlets
    ("gothamist.com",          "Gothamist"),
    ("nytimes.com",            "The New York Times"),
    ("ny1.com",                "NY1"),
    ("nyc.streetsblog.org",    "Streetsblog NYC"),
    ("streetsblog.org",        "Streetsblog"),
    ("wnyc.org",               "WNYC"),
    ("thecity.nyc",            "THE CITY"),
    ("nydailynews.com",        "New York Daily News"),
    ("nypost.com",             "New York Post"),
    ("nymag.com",              "New York Magazine"),
    ("city-journal.org",       "City Journal"),
    ("cityandstateny.com",     "City & State NY"),
    ("therealdeal.com",        "The Real Deal"),
    # Substacks frequently cited in the manual list
    ("johnkroman.substack.com",      "John Kroman (Substack)"),
    ("nyeditorialboard.substack.com","NY Editorial Board (Substack)"),
    ("probablecausation.substack.com","Probable Causation (Substack)"),
    # National outlets
    ("politico.com",           "Politico"),
    ("semafor.com",            "Semafor"),
    ("washingtonpost.com",     "Washington Post"),
    ("newyorker.com",          "The New Yorker"),
    ("bloomberg.com",          "Bloomberg"),
    ("theguardian.com",        "The Guardian"),
    ("newsweek.com",           "Newsweek"),
    # ---- added 21 Aug 2026, from the mentions & influence audit ------------
    # The audit found 60+ items the tracker structurally could not see. Its
    # first finding was that the whitelist itself was the bottleneck: the New
    # York Post alone had produced two items and was not on the list. These are
    # the outlets it named, with the reason each was missing.
    ("fastcompany.com",        "Fast Company"),        # ran a feature built on the subway-safety report
    ("citylimits.org",         "City Limits"),          # quoted Glazer directly in a voter guide
    ("amny.com",               "amNewYork"),
    ("abc7ny.com",             "ABC7 New York"),        # covered the mayoral forum VC co-hosted
    ("silive.com",             "SILive / Staten Island Advance"),
    ("gothamgazette.com",      "Gotham Gazette"),
    ("law.com",                "Law.com / NY Law Journal"),
    ("thecityreporter.nyc",    "The City Reporter"),    # republishes the State of Crime report in full
    ("niemanlab.org",          "Nieman Lab"),
]

# Government statements. The audit called the Public Advocate item "the single
# best influence artifact in this report" — a citywide elected official adopting
# a Vital City finding as the basis of an official statement — and the tracker
# could not see it, because .gov press pages were not on the whitelist.
GOV_DOMAINS = [
    ("advocate.nyc.gov",       "NYC Public Advocate"),
    ("council.nyc.gov",        "New York City Council"),
    ("comptroller.nyc.gov",    "NYC Comptroller"),
    ("nyc.gov",                "City of New York"),
    ("governor.ny.gov",        "Governor of New York"),
    ("osc.ny.gov",             "NY State Comptroller"),
    ("manhattanda.org",        "Manhattan District Attorney"),
]

# Institutions that publish IN Vital City and then republish under their own
# banner. Nine in twenty months, which the audit reads as organisations treating
# it as an authoritative venue rather than as incidental pickup.
REPUBLISHERS = [
    ("cbcny.org",              "Citizens Budget Commission"),
    ("niskanencenter.org",     "Niskanen Center"),
    ("nycfuture.org",          "Center for an Urban Future"),
    ("brennancenter.org",      "Brennan Center for Justice"),
    ("ipk.nyu.edu",            "NYU Institute for Public Knowledge"),
    ("steptwopolicy.org",      "Step Two Policy"),
    ("steptwopolicyproject.substack.com", "Step Two Policy (Substack)"),
    ("vera.org",               "Vera Institute of Justice"),
    ("manhattan-institute.org","Manhattan Institute"),
    ("urban.org",              "Urban Institute"),
    ("futureofpolicing.blog",  "Future of Policing"),
    ("gregberman.substack.com","Small Sanities (Berman)"),
    ("benjaminschneider.substack.com", "The Urban Condition"),
    ("nycpolitics101.substack.com",    "NYC Politics 101"),
    ("citythatworks.substack.com",     "A City That Works"),
    ("changinglanesnewsletter.com",    "Changing Lanes"),
    ("nycuriosity.substack.com",       "NYCuriosity"),
    ("normanoder.substack.com",        "Atlantic Yards Report"),
]

# The people, not the brand. Google News will never index audio, so the audit's
# advice for podcasts is to track WHO appears rather than what is said: twelve-
# plus appearances in the window went unrecorded because no one was searching
# for the guests.
VC_VOICES = [
    ("Elizabeth Glazer", "founder and co-editor"),
    ("Greg Berman",      "co-editor"),
    ("Josh Greenman",    "managing editor"),
    ("Paul Reeping",     "research director"),
    ("Jamie Rubin",      "host, After Hours"),
    ("Ted Alcorn",       "policy director"),
]
SOCIAL_PLATFORMS = [
    ("x.com",            "X"),
    ("twitter.com",      "X"),
    ("linkedin.com",     "LinkedIn"),
    ("bsky.app",         "Bluesky"),
    ("instagram.com",    "Instagram"),
    ("facebook.com",     "Facebook"),
    ("threads.net",      "Threads"),
]
# Junk-title patterns for MEDIA mentions (case-insensitive regex, editable).
# These match ephemeral aggregate pages — TV schedules, live blogs, homepage
# rotations — that Google indexes with a momentary "Vital City" promo that's
# gone by the time anyone clicks through (e.g. NY1's "NEWS ALL DAY" schedule
# page). A matching title drops the item from the media list.
JUNK_TITLE_PATTERNS = [
    r"^news all day\b",          # NY1 rolling schedule page
    r"^watch live\b",
    r"^live blog\b",
    r"^today'?s (top )?headlines?$",   # bare headline-roundup pages (no specifics)
]

# Link roundups are REAL distribution but not substantive engagement, and the
# 21 Aug 2026 audit found they were quietly inflating the media count: most of
# Streetsblog's 19 hits were daily "Headlines" posts, which made Streetsblog
# look like our biggest champion when Gothamist and Politico actually lead on
# substance. So these are tagged, not dropped — the dashboard shows the split
# and lets you judge, which is the honest handling of a link that is genuinely
# a mention and genuinely not a story about us.
ROUNDUP_TITLE_PATTERNS = [
    r"^(mon|tues|wednes|thurs|fri|satur|sun)day'?s headlines\b",  # Streetsblog daily
    r"\bheadlines\b.*\bedition\b",
    r"^(the )?(morning|afternoon|evening|daily|weekly) (links|roundup|digest|briefing)\b",
    r"^links?:",
    r"^what we'?re reading\b",
]

# Blog archive and pagination pages. "Month: July 2026" is not an article; it is
# an index that happens to contain every post from that month, so the phrase
# matches without anyone having written about us.
ARCHIVE_TITLE_PATTERNS = [
    r"^(month|year|day|category|tag|author|archives?)\s*[:|]",
    r"^page \d+\b",
    r"^archives?\b",
    r"^(posts|articles) (from|by)\b",
]


VC_BLUESKY_HANDLE = "vitalcitynyc.bsky.social"

def pull_bluesky_profile():
    """Account-level Bluesky stats — followers, posts. Open official API,
    no auth (same as the mention search)."""
    try:
        d = json.loads(http_get(
            f"https://api.bsky.app/xrpc/app.bsky.actor.getProfile?actor={VC_BLUESKY_HANDLE}",
            timeout=15))
        out = {"available": True, "handle": VC_BLUESKY_HANDLE,
               "followers": d.get("followersCount") or 0,
               "posts_total": d.get("postsCount") or 0}
        log(f"  bluesky profile: {out['followers']} followers, {out['posts_total']} posts")
        return out
    except Exception as e:
        log(f"  bluesky profile failed: {e}")
        return {"available": False, "reason": str(e)[:120]}

def pull_bluesky_mentions():
    """Bluesky's public search API — the one social platform with a real,
    free, official search endpoint (open AT Protocol; no key, no auth).
    Unlike the Google News site: queries, this is the platform's own index:
    complete, current and with engagement counts. NOTE: use api.bsky.app —
    public.api.bsky.app 403s from some networks."""
    items = []
    seen_uris = set()
    for q, is_share in (("vitalcitynyc.org", True), ("vitalcitynyc", False)):
        url = ("https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
               f"?q={urllib.parse.quote(q)}&limit=100&sort=latest")
        try:
            data = json.loads(http_get(url, timeout=20))
        except Exception as e:
            log(f"  bluesky search ({q}) failed: {e}"); continue
        for p in data.get("posts", []):
            uri = p.get("uri") or ""
            if not uri or uri in seen_uris: continue
            seen_uris.add(uri)
            handle = ((p.get("author") or {}).get("handle") or "").strip()
            rkey   = uri.rsplit("/", 1)[-1]
            text   = ((p.get("record") or {}).get("text") or "").strip()
            created = ((p.get("record") or {}).get("createdAt") or p.get("indexedAt") or "")
            created = re.sub(r"\.\d+", "", created)   # trim fractional seconds for strptime
            likes, reposts = p.get("likeCount") or 0, p.get("repostCount") or 0
            eng = f" · {likes} likes, {reposts} reposts" if (likes or reposts) else ""
            items.append({
                "title": re.sub(r"\s+", " ", text)[:140] or "(no text)",
                "url": f"https://bsky.app/profile/{handle}/post/{rkey}",
                "source": "@" + handle, "published": created,
                "snippet": (text[:200] + eng).strip(),
                "domain": "bsky.app", "match_shape": q, "kind": "social",
                "is_url_share": is_share,
                "likes": likes, "reposts": reposts,
                # Authorship is definitive here (it's the platform API, not a
                # Google index guess) — lets flag_own_url_shares skip matching.
                "own_account": handle.lower() == VC_BLUESKY_HANDLE,
                "native_search": True,
            })
    log(f"  bluesky mentions: {len(items)}")
    return items


def pull_reddit_mentions():
    """Reddit search via the RSS endpoint — every submission linking to
    vitalcitynyc.org. (The .json endpoint 403s for non-browser clients;
    search.rss serves the same results as Atom and stays open.)"""
    items = []
    seen = set()
    ns = "{http://www.w3.org/2005/Atom}"
    for q, is_share in (("site:vitalcitynyc.org", True), ('"vitalcitynyc.org"', False)):
        url = f"https://www.reddit.com/search.rss?q={urllib.parse.quote(q)}&sort=new&limit=100&t=all"
        try:
            xml = http_get(url, headers={"User-Agent": UA}, timeout=20)
            root = ET.fromstring(xml)
        except Exception as e:
            log(f"  reddit search ({q}) failed: {e}"); continue
        for it in root.findall(f".//{ns}entry"):
            link_el = it.find(f"{ns}link")
            link = link_el.get("href") if link_el is not None else ""
            if not link or link in seen: continue
            seen.add(link)
            cat = it.find(f"{ns}category")
            sub = (cat.get("label") if cat is not None else "") or "Reddit"
            pub = _xml_text(it, f"{ns}published") or _xml_text(it, f"{ns}updated")
            pub = re.sub(r"\.\d+", "", pub)
            items.append({
                "title": html_mod.unescape(_xml_text(it, f"{ns}title"))[:140],
                "url": link, "source": sub, "published": pub,
                "snippet": "", "domain": "reddit.com", "match_shape": q,
                "kind": "social", "is_url_share": is_share,
                "native_search": True,
            })
    log(f"  reddit mentions: {len(items)}")
    return items


LI_CACHE = ROOT / "data" / "li_followers.json"

def pull_linkedin_followers():
    """LinkedIn company-page follower count, scraped from the PUBLIC page's
    meta description ("Vital City | 3,046 followers on LinkedIn. ..."). No
    auth needed with a browser User-Agent — verified live. LinkedIn often
    authwalls datacenter IPs though, so the GitHub Actions runner may get
    blocked: on success we write data/li_followers.json (committed by the
    workflow — the count is public information); on failure we serve that
    cached value with its original as-of date."""
    UA_browser = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    try:
        page = http_get("https://www.linkedin.com/company/vitalcitynyc",
                        headers={"User-Agent": UA_browser}, timeout=20).decode("utf-8", "replace")
        m = re.search(r'content="[^"]*?\|\s*([\d,]+)\s+followers on LinkedIn', page)
        if m:
            n = int(m.group(1).replace(",", ""))
            today = datetime.now(timezone.utc).date().isoformat()
            out = {"available": True, "followers": n, "as_of": today,
                   "source": "public company-page meta (live)"}
            try:
                LI_CACHE.parent.mkdir(parents=True, exist_ok=True)
                LI_CACHE.write_text(json.dumps({"followers": n, "as_of": today}))
            except Exception:
                pass
            log(f"  linkedin followers: {n} (live)")
            return out
        raise ValueError("follower pattern not found in page (authwall?)")
    except Exception as e:
        if LI_CACHE.exists():
            try:
                c = json.loads(LI_CACHE.read_text())
                log(f"  linkedin followers: live fetch failed ({e}); using cached {c.get('followers')} from {c.get('as_of')}")
                return {"available": True, "followers": c.get("followers"),
                        "as_of": c.get("as_of"), "source": "cached (live fetch blocked)"}
            except Exception:
                pass
        log(f"  linkedin followers failed: {e}")
        return {"available": False, "reason": str(e)[:120]}


def load_mentions_ledger():
    """Hand-logged mentions from mentions_ledger.json.

    Google News does not index audio, so podcast and radio appearances are
    unreachable by every site:-scoped query in this file. The 21 Aug 2026
    audit found a dozen already on the record and uncounted. This reads the
    curated file so they show up; it is the one channel here that is not
    automated, and the dashboard labels it as such.
    """
    fp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mentions_ledger.json")
    if not os.path.exists(fp):
        log("  mentions ledger: file missing")
        return {"available": False, "items": []}
    try:
        with open(fp) as fh:
            d = json.load(fh)
    except Exception as e:
        log(f"  mentions ledger: unreadable ({e})")
        return {"available": False, "items": []}
    items = sorted(d.get("items", []), key=lambda x: x.get("date", ""), reverse=True)
    log(f"  mentions ledger: {len(items)} hand-logged "
        f"(audited {d.get('last_audit', 'unknown')})")
    return {"available": True, "items": items,
            "last_audit": d.get("last_audit", ""),
            "audit_window": d.get("audit_window", []),
            "total": len(items),
            "appearances": sum(1 for i in items if i.get("role") == "appearance"),
            "citations": sum(1 for i in items if i.get("role") == "citation"),
            "note": ("Logged by hand from the mentions audit. Audio and video are "
                     "invisible to Google News, so these cannot be collected "
                     "automatically -- edit mentions_ledger.json to add more.")}


def pull_scholar_citations():
    """Academic and law-review citations of Vital City.

    The audit's first recommendation, and the one with the longest tail: five
    citations were found — Yale Law Journal Forum, Emory, Fordham Urban Law
    Journal, Springer/Palgrave, GMU Translational Criminology — and every one
    arrived with no notification, because Google News does not index law reviews
    or journals at all. Academic citation also lags two to four years, so the
    2025-26 output will keep surfacing through 2029. This is the most durable
    form of influence the organisation has and it was completely invisible.

    Google Scholar answers scripted exact-phrase queries where the general
    search engines refuse them. It rate-limits after a handful, so this runs a
    small number of queries and treats a block as "come back tomorrow" rather
    than as an error worth failing the build over.
    """
    import time as _t
    UA_local = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    QUERIES = ['"vitalcitynyc.org"',
               '"Vital City" "New York" crime',
               '"Vital City" "New York" housing',
               '"Vital City" Glazer']
    out, seen = [], set()
    blocked = False
    for q in QUERIES:
        url = "https://scholar.google.com/scholar?q=" + urllib.parse.quote(q) + "&as_ylo=2025"
        try:
            page = http_get(url, headers={"User-Agent": UA_local,
                                          "Accept-Language": "en-US,en;q=0.9"}, timeout=30).decode("utf-8", "replace")
        except Exception as e:
            log(f"  scholar '{q}': {type(e).__name__}")
            if getattr(e, "code", None) == 429:
                blocked = True
                break
            continue
        if "not a robot" in page or "unusual traffic" in page:
            blocked = True
            log("  scholar: rate-limited; will retry on the next run")
            break
        for blk in re.findall(r'<div class="gs_ri">(.*?)</div>\s*</div>', page, re.S):
            tm = re.search(r'<h3 class="gs_rt".*?</h3>', blk, re.S)
            if not tm:
                continue
            def _clean(x):
                return re.sub(r"\s+", " ", html_mod.unescape(re.sub(r"<[^>]+>", "", x))).strip()
            # lstrip() with a string strips CHARACTERS, not a prefix — it ate the
            # T off every title beginning "The". Strip the tag properly.
            title = re.sub(r"^(\[(PDF|HTML|BOOK|CITATION|B)\]\s*)+", "", _clean(tm.group(0))).strip()
            am = re.search(r'<div class="gs_a">(.*?)</div>', blk, re.S)
            sm = re.search(r'<div class="gs_rs">(.*?)</div>', blk, re.S)
            lm = re.search(r'<h3 class="gs_rt".*?<a href="([^"]+)"', blk, re.S)
            if not title or title in seen:
                continue
            seen.add(title)
            # Only the domain query is self-verifying. "Vital City" as a phrase
            # also appears in papers using it generically ("a vital city
            # centre"), so phrase hits are surfaced as leads to check, never
            # counted as citations.
            snippet = _clean(sm.group(1))[:300] if sm else ""
            confirmed = ("vitalcitynyc" in q
                         or "vitalcitynyc.org" in (snippet + title).lower())
            out.append({"title": title[:220],
                        "authors": _clean(am.group(1))[:200] if am else "",
                        "snippet": snippet,
                        "url": html_mod.unescape(lm.group(1)) if lm else "",
                        "query": q,
                        "confidence": "confirmed" if confirmed else "unverified"})
        _t.sleep(6)
    conf = sum(1 for x in out if x["confidence"] == "confirmed")
    log(f"  scholar citations: {len(out)} ({conf} confirmed by URL, {len(out)-conf} to verify)"
        + (" (rate-limited partway)" if blocked else ""))
    # An empty result set is a failure, not a finding. Google Scholar answers a
    # blocked query with a 200 and a consent or CAPTCHA page, which parses to
    # zero rows without tripping the `blocked` flag -- so "no citations" and
    # "we were turned away" arrive looking identical. Vital City is cited; zero
    # means the pull broke. Reporting available:false here hands the block to
    # the carry-forward guard below, which restores the last good pull and
    # marks it stale, instead of publishing a confident zero.
    # Seen 2026-09-08: a CI run returned 0 where a laptop run 11 days earlier
    # found 10 confirmed and 15 unverified.
    return {"available": bool(out), "citations": out,
            "confirmed": conf, "unverified": len(out) - conf,
            "reason": ("Scholar returned no rows — rate-limited or served an "
                       "interstitial" if not blocked else "Scholar rate-limited the run"),
            "rate_limited": blocked, "since_year": 2025,
            "note": ("Law reviews and journals are not in Google News, so these never reach the "
                     "press tracker. Citation lags publication by 2-4 years: 2025-26 pieces will "
                     "keep appearing here through 2029.")}


def pull_voice_appearances():
    """Track the PEOPLE, not the brand.

    Google News will never index audio, so a podcast appearance by a Vital City
    editor leaves no trace a mention-tracker can find. The audit counted twelve-
    plus such appearances in twenty months, none of them captured. Searching for
    the staff member by name alongside the organisation is the workable proxy.
    """
    import time as _t
    out = []
    for name, role in VC_VOICES:
        q = f'"{name}" "Vital City"'
        url = (f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}"
               f"&hl=en-US&gl=US&ceid=US:en")
        try:
            root = ET.fromstring(http_get(url, timeout=25))
        except Exception as e:
            log(f"  voices '{name}': {type(e).__name__}")
            continue
        items = []
        for it in root.iter("item"):
            src = it.find("source")
            source = (src.text if src is not None else "") or ""
            link = (it.findtext("link") or "").strip()
            if "vitalcitynyc.org" in link or re.search(r"vital\s*city", source, re.I):
                continue            # our own site is not an appearance elsewhere
            items.append({"title": (it.findtext("title") or "").strip()[:200],
                          "url": link, "source": source,
                          "published": (it.findtext("pubDate") or "").strip()})
        out.append({"name": name, "role": role, "count": len(items), "items": items[:12]})
        log(f"  voices: {name} -> {len(items)}")
        _t.sleep(1.5)
    return {"available": True, "people": out,
            "total": sum(p["count"] for p in out),
            "note": ("Appearances by Vital City's own editors and researchers, found by searching "
                     "the person rather than the publication. Podcasts and broadcast leave no "
                     "trace a brand-mention tracker can see.")}


# Citations we know about that no search engine will hand us. Google News does
# not index advocate.nyc.gov at all -- every query against that domain returns
# zero -- so a Public Advocate statement responding to a Vital City report was
# undiscoverable by the automated channel no matter how the whitelist was
# tuned. (Worth keeping in proportion: a press office reacting to a published
# report is cheap evidence, and this channel is thinner than its rarity makes
# it look. It is tracked because it is real, not because it is impressive.)
# Seeding the URL fixes the blind spot without weakening anything: these are
# fetched and checked by verify_citations() exactly like a searched result, so
# a seeded item that stops naming us stops counting.
#
# Add a row when someone tells you about a citation the dashboard missed.
SEED_CITATIONS = [
    ("https://advocate.nyc.gov/press/nyc-public-advocate-responds-to-report-that-crime-rates-do-not-correlate-to-nypd-headcount",
     "advocate.nyc.gov", "NYC Public Advocate", "gov"),
]


def resolve_gnews_url(stub_url, timeout=25):
    """Turn a news.google.com/rss/articles/CBMi... stub into the real URL.

    Google News RSS no longer puts the destination in the link -- the base64
    payload used to contain it and no longer does, and the stub page is a JS
    shell. Without the real URL a citation cannot be fetched, and without
    fetching it cannot be checked, which is how 88 "government citations"
    turned out to lead with the Homeless Services landing page.

    Google's own resolver takes the per-article signature and timestamp printed
    into the stub page, so: GET the stub, lift the tokens, POST them back.
    Returns the original URL unchanged if anything goes wrong -- callers treat
    an unresolved item as unverified, never as confirmed.

    (DuckDuckGo returns real URLs directly and was tried first. It serves a
    CAPTCHA after a handful of queries, so it is not usable unattended.)
    """
    if "news.google.com" not in stub_url:
        return stub_url
    try:
        raw = http_get(stub_url, timeout=timeout)
        if isinstance(raw, bytes):
            raw = raw.decode("utf8", "replace")
        aid = re.search(r'data-n-a-id="([^"]+)"', raw)
        sg  = re.search(r'data-n-a-sg="([^"]+)"', raw)
        ts  = re.search(r'data-n-a-ts="([^"]+)"', raw)
        if not (aid and sg and ts):
            return stub_url
        inner = json.dumps(["garturlreq",
                            [["X", "X", ["X", "X"], None, None, 1, 1, "US:en",
                              None, 1, None, None, None, None, None, 0, 1],
                             "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                            aid.group(1), int(ts.group(1)), sg.group(1)])
        body = urllib.parse.urlencode(
            {"f.req": json.dumps([[["Fbv4je", inner, None, "generic"]]])}).encode()
        req = urllib.request.Request(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                     "User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = r.read().decode("utf8", "replace")
        hits = re.findall(r'https?://(?!news\.google)[^"\\\s]{12,300}', resp)
        return hits[0] if hits else stub_url
    except Exception:
        return stub_url


def verify_citations(items, workers=12):
    """Fetch each gov/republication page and check it actually names us.

    Google's site:-scoped phrase match is lenient, and "vital city" is ordinary
    English in exactly the two places we now search: government pages say "vital
    city services" and policy shops say "a vital city centre". The first run of
    this channel returned 88 government hits, of which the top of the list
    included the DHS landing page and "Partner with the Public Engagement Unit"
    -- no one had written about us at all.

    So every item is fetched and marked, never silently dropped:
      verified=True   the page contains vitalcitynyc.org, or "Vital City" used
                      as a proper noun (journal/report/analysis/according to)
      verified=False  the phrase is there but generically, or we could not read
                      the page -- surfaced separately, never counted

    Failing to fetch marks an item unverified rather than removing it: a page we
    could not read is not evidence of absence, and a 403 should not quietly
    delete a real citation.
    """
    if not items:
        return items

    # Capitalisation is the signal, so this is deliberately CASE-SENSITIVE.
    # An earlier version compiled these with re.I and threw that away, which
    # cost accuracy in both directions: it accepted the Comptroller writing
    # "ensure that vital City services can continue" (lowercase v -- the word
    # doing its job, capital C only because City means the municipality), and
    # it rejected Gothamist writing "an analysis by the civic group Vital City
    # found" because "group" was not in a hand-listed set of nouns.
    #
    # The rule that actually separates them: our name is "Vital City" with both
    # words capitalised. Generic usage is "a vital city", "our vital city",
    # "vital city services". So take capitalised occurrences and subtract the
    # generic shapes, rather than trying to enumerate every noun that can
    # precede a publication's name.
    PROPER   = re.compile(r"\bVital City\b")
    # The determiner must sit directly before the phrase (allowing only an
    # intensifier between). Letting any two words intervene wrongly swallowed
    # "the civic group Vital City" and "the policy journal Vital City", where
    # the determiner belongs to "group" and "journal", not to us.
    GENERIC_BEFORE = re.compile(
        r"\b(?:a|an|our|your|this|that|these|those|every|each|any|another|"
        r"such|the)\s+(?:truly\s+|really\s+|very\s+|so\s+|especially\s+)?$", re.I)
    GENERIC_AFTER  = re.compile(
        r"^\s+(?:services?|agenc(?:y|ies)|employees?|workers?|staff|"
        r"infrastructure|functions?|departments?|operations?|programs?|"
        r"budgets?|centres?|centers?|streets?|blocks?|neighou?rhoods?)\b")

    def names_us(text):
        """True when "Vital City" appears as our name rather than as English."""
        for m in PROPER.finditer(text):
            before = text[max(0, m.start() - 60):m.start()]
            after  = text[m.end():m.end() + 40]
            if GENERIC_BEFORE.search(before) or GENERIC_AFTER.match(after):
                continue
            return True
        return False

    def check(it):
        # Resolve first: fetching the Google News stub only ever reads Google's
        # own interstitial, which contains neither the article nor our name.
        it["resolved_url"] = resolve_gnews_url(it.get("url", ""))
        try:
            raw = http_get(it["resolved_url"], timeout=20)
        except Exception:
            it["verified"] = False
            it["verify_note"] = "page could not be fetched"
            return it
        if isinstance(raw, bytes):
            raw = raw.decode("utf8", "replace")
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html_mod.unescape(re.sub(r"\s+", " ", text))
        if not it.get("title"):
            tm = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
            if tm:
                it["title"] = re.sub(r"\s+", " ", html_mod.unescape(
                    re.sub(r"<[^>]+>", "", tm.group(1)))).strip()[:220]
        if "vitalcitynyc" in raw.lower() or names_us(text):
            it["verified"] = True
            it["verify_note"] = ""
        elif re.search(r"vital\s+city", text, re.I):
            it["verified"] = False
            it["verify_note"] = "phrase present but used generically"
        else:
            it["verified"] = False
            it["verify_note"] = "phrase not found on the page"
        return it

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        items = list(ex.map(check, items))
    ok = sum(1 for i in items if i.get("verified"))
    log(f"  citation verification: {ok}/{len(items)} confirmed by page fetch")
    return items


def pull_news_mentions():
    """Search Google News RSS for Vital City references, scoped per outlet.

    Two outlet groups with different query shapes:
      - MEDIA_OUTLETS (news publications): three shapes — "Vital City",
        @vitalcitynyc, vitalcitynyc.org — because brand-phrase matches on
        press sites are intentional references to the publication.
      - SOCIAL_PLATFORMS (X, LinkedIn, Bluesky, Instagram): only two shapes —
        @vitalcitynyc, vitalcitynyc.org — DROPPING the brand-phrase search
        because on social platforms "vital city" is used generically
        ("Karachi remains Pakistan's most inclusive and economically vital
        city...") and produces high-volume false positives.

    Each result is tagged with `kind: 'media' | 'social'` so the dashboard
    can route them into the right card without re-filtering by domain.
    """
    import time as _t
    out = []
    seen = set()
    UA_local = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    MEDIA_SHAPES  = ['"Vital City"', "@vitalcitynyc", "vitalcitynyc.org"]
    SOCIAL_SHAPES = ["@vitalcitynyc", "vitalcitynyc.org"]
    # Google News RSS hard-caps at 100 results per query. For high-volume
    # platforms we hit that ceiling and lose older items. Mitigation: also
    # query each social platform's vitalcitynyc.org share by year, which
    # multiplies our depth by 3-4x without changing the shape of the data.
    cur_year = datetime.now(timezone.utc).year
    targets = []
    for d, label in MEDIA_OUTLETS:
        for s in MEDIA_SHAPES:
            targets.append((d, label, "media", s, None))
    # Government statements and institutional republication get the same
    # site:-scoped treatment as the press whitelist — that scoping is what makes
    # any of these counts trustworthy, and it is why an unscoped phrase search
    # is never used here.
    for d, label in GOV_DOMAINS:
        for shape in ('"Vital City"', "vitalcitynyc.org"):
            targets.append((d, label, "gov", shape, None))
    for d, label in REPUBLISHERS:
        for shape in ('"Vital City"', "vitalcitynyc.org"):
            targets.append((d, label, "republication", shape, None))
    for d, label in SOCIAL_PLATFORMS:
        # Brand-tag shape (no year split)
        targets.append((d, label, "social", "@vitalcitynyc", None))
        # URL-share shape, split by year for depth (cur..cur-5 inclusive)
        for yr in range(cur_year, cur_year - 6, -1):
            targets.append((d, label, "social", "vitalcitynyc.org", yr))
    for domain, label, kind, shape, year in targets:
        q = f'{shape} site:{domain}'
        if year is not None:
            # Constrain to a single calendar year using Google's "after:" /
            # "before:" operators — works inside news.google.com queries.
            q += f' after:{year}-01-01 before:{year+1}-01-01'
        url = (f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}"
               f"&hl=en-US&gl=US&ceid=US:en")
        # Indent the rest of the loop body
        if True:
            xml = None
            for attempt in range(3):   # Google News RSS 503s transiently from CI IPs
                try:
                    xml = http_get(url, headers={"User-Agent": UA_local}, timeout=20); break
                except Exception as e:
                    if attempt == 2:
                        log(f"  news mentions {domain} ({shape}) failed: {e}")
                    else:
                        _t.sleep(1.5 * (attempt + 1))
            if xml is None: continue
            try:
                root = ET.fromstring(xml)
            except Exception as e:
                continue
            for it in root.findall(".//item"):
                # Google News RSS double-escapes HTML entities (&amp;nbsp;),
                # so XML parsing leaves literal "&nbsp;" / "&#39;" text in
                # titles and snippets — unescape to real characters.
                title = html_mod.unescape(_xml_text(it, "title"))
                link  = _xml_text(it, "link")
                src   = html_mod.unescape(_xml_text(it, "source") or label)
                pub   = _xml_text(it, "pubDate")
                snip  = html_mod.unescape(re.sub(r"<[^>]+>", "", _xml_text(it, "description")))[:240]
                # Google News appends " - Source Name" to every title; the
                # source is shown separately on the dashboard, so strip it.
                if src and title.endswith(" - " + src):
                    title = title[: -len(" - " + src)].rstrip()
                title = re.sub(r"\s+", " ", title).strip()
                if not title or not link: continue
                # Drop known ephemeral aggregate pages (TV schedules, live
                # blogs) — see JUNK_TITLE_PATTERNS above.
                if kind == "media" and any(re.search(p, title, re.I) for p in JUNK_TITLE_PATTERNS):
                    continue
                if kind in ("gov", "republication") and any(
                        re.search(p, title, re.I) for p in ARCHIVE_TITLE_PATTERNS):
                    continue
                key = (domain, title.lower())
                if key in seen: continue
                seen.add(key)
                out.append({
                    "title": title, "url": link, "source": src, "published": pub,
                    "snippet": snip, "domain": domain, "match_shape": shape,
                    "kind": kind,
                    "is_url_share": (shape == "vitalcitynyc.org"),
                    "roundup": bool(kind == "media" and any(
                        re.search(pt, title, re.I) for pt in ROUNDUP_TITLE_PATTERNS)),
                })
            _t.sleep(0.15)   # polite pacing across many outlets × shapes

    # Platform-native sources — far better than Google News for the
    # platforms that offer a real free search API. These flow through the
    # same date-parse / dedup / sort / cap stages below.
    out.extend(pull_bluesky_mentions())
    out.extend(pull_reddit_mentions())

    # Parse pub dates. For PRESS mentions we drop anything older than 24
    # months (old press isn't actionable). For URL SHARES on social we keep
    # everything Google indexed — those are organic distribution signals
    # that don't go stale the same way.
    cutoff_press = datetime.now(timezone.utc) - timedelta(days=730)
    def _parse(p):
        for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z",
                    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                return datetime.strptime(p, fmt).astimezone(timezone.utc)
            except Exception:
                pass
        return None
    for it in out:
        dt = _parse(it.get("published", ""))
        it["published_iso"] = dt.isoformat() if dt else ""
        it["_dt"] = dt
    # Government citations and institutional republications are exempt from the
    # 24-month press cutoff. Old press isn't actionable; a Public Advocate
    # report citing our research is still the best influence artifact we have
    # however long ago it was written, and there are few enough of them that
    # keeping all of them costs nothing.
    KEEP_ALL_AGES = ("social", "gov", "republication")
    out = [it for it in out
           if it.get("_dt") and (it.get("kind") in KEEP_ALL_AGES or it["_dt"] >= cutoff_press)]

    # Note on verification: Google's site-restricted exact-phrase query
    # ('"Vital City" site:DOMAIN') already requires the phrase to appear in
    # the article body. We trust that — fetching every page just to re-check
    # title/snippet (which is only the first 240 chars) would discard valid
    # hits where "Vital City" appears deeper in the body. Outlet whitelist
    # carries most of the precision; rare lexical false positives can sneak
    # through (Google's phrase matching has slight leniency) but they're
    # easy to spot in a reverse-chron list.

    # Dedup. Social stays PER-DOMAIN — the same article on Streetsblog vs an
    # X share of that same article are distinct signals; keep both. MEDIA
    # dedups on title alone across domains, because overlapping site: scopes
    # (streetsblog.org also matches nyc.streetsblog.org) return the same
    # article twice under two whitelist entries.
    titles_seen = set(); deduped = []
    for it in sorted(out, key=lambda x: x["_dt"], reverse=True):
        # Media dedups on title alone (overlapping site: scopes return the same
        # article twice); every other kind dedups per-domain, because the same
        # headline republished by two institutions is two real signals.
        # Media and gov dedup on title alone. For media, overlapping site: scopes
        # return the same article twice; for gov, comptroller.nyc.gov IS a
        # subdomain of nyc.gov, so every comptroller item arrived twice and was
        # being counted twice. Social and republication stay per-domain, because
        # the same headline republished by two institutions is two real signals.
        key = (it["title"].lower()[:80],) if it.get("kind") in ("media", "gov") \
              else (it["domain"], it["title"].lower()[:80])
        if key in titles_seen: continue
        titles_seen.add(key); deduped.append(it)
    for it in deduped: it.pop("_dt", None)

    # Per-kind caps. Press capped tight (recent 200), social much higher
    # since URL shares are the headline social signal and we want depth.
    media  = [it for it in deduped if it.get("kind") == "media"][:200]
    social = [it for it in deduped if it.get("kind") == "social"][:1500]
    # Uncapped on purpose: these two are rare and each one matters. If they ever
    # grow large enough to need a cap, that is itself the good news.
    gov    = [it for it in deduped if it.get("kind") == "gov"]
    repub  = [it for it in deduped if it.get("kind") == "republication"]
    # Carry-forward cache. Google News RSS periodically 503s for a whole run from
    # CI IP ranges, which would otherwise blank the third-party-press card. Cache
    # the last good media pull; if this run scraped none, reuse the cache so a
    # transient Google outage doesn't wipe the feed (items flagged `stale`).
    cache_path = ROOT / "data" / "media_mentions_cache.json"
    if media:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(
                {"as_of": datetime.now(timezone.utc).isoformat(), "items": media}, ensure_ascii=False))
        except Exception as e:
            log(f"  media cache write failed: {e}")
    elif cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            media = cached.get("items", [])
            for it in media: it["stale"] = True
            log(f"  news mentions: live media scrape empty (Google News down?) — carried "
                f"forward {len(media)} cached items from {str(cached.get('as_of',''))[:10]}")
        except Exception as e:
            log(f"  media cache read failed: {e}")
    # Seeded citations join the searched ones before verification, so they are
    # held to the same standard rather than trusted because we typed them in.
    seen_urls = {i.get("url") for i in gov + repub}
    for url, dom, label, kind in SEED_CITATIONS:
        if url in seen_urls:
            continue
        row = {"title": "", "url": url, "source": label, "domain": dom,
               "kind": kind, "match_shape": "seeded", "published": "",
               "snippet": "", "is_url_share": False, "seeded": True}
        (gov if kind == "gov" else repub).append(row)

    # Verify before assembling: Google's phrase match is too lenient to trust on
    # domains where "vital city" is ordinary English.
    gov   = verify_citations(gov)
    repub = verify_citations(repub)
    for it in gov + repub:
        if it.get("resolved_url"):
            it["url"] = it["resolved_url"]   # link to the source, not to Google
    out    = media + social + gov + repub
    log(f"  news mentions: {len(out)} items ({len(media)} media + {len(social)} social "
        f"+ {len(gov)} gov [{sum(1 for i in gov if i.get('verified'))} verified] "
        f"+ {len(repub)} republication [{sum(1 for i in repub if i.get('verified'))} verified]) "
        f"across {len(set(i['domain'] for i in out))} outlets")
    return out


# ------------------------------------------------------------- Press / Reddit (free RSS)
def _xml_text(el, tag):
    e = el.find(tag)
    return (e.text or "").strip() if e is not None and e.text else ""


def pull_press():
    # Google News RSS — quoted brand + NYC disambiguation; -site exclusions reduce false hits
    queries = [
        # The brand spelled out (with NYC disambiguator)
        ('"Vital City" (NYC OR "New York" OR Mamdani OR Adams OR Bragg OR NYCHA)', "google-news"),
        # Direct links to the site
        ('site:vitalcitynyc.org', "google-news-direct"),
    ]
    items = []
    for q, src in queries:
        url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=en-US&gl=US&ceid=US:en"
        try:
            xml = http_get(url, timeout=30)
            root = ET.fromstring(xml)
            for it in root.findall(".//item"):
                items.append({
                    "title":   html_mod.unescape(_xml_text(it, "title")),
                    "url":     _xml_text(it, "link"),
                    "source":  html_mod.unescape(_xml_text(it, "source") or "Google News"),
                    "published": _xml_text(it, "pubDate"),
                    "snippet": html_mod.unescape(re.sub(r"<[^>]+>", "", _xml_text(it, "description")))[:240],
                    "channel": src,
                })
        except Exception as e:
            log(f"  google news ({src}) failed: {e}")

    # Reddit search RSS — same brand, NYC disambiguator
    try:
        rq = urllib.parse.quote('"Vital City" NYC')
        rurl = f"https://www.reddit.com/search.rss?q={rq}&sort=new"
        xml = http_get(rurl, timeout=30, headers={"User-Agent": UA})
        root = ET.fromstring(xml)
        ns = "{http://www.w3.org/2005/Atom}"
        for it in root.findall(f".//{ns}entry"):
            link_el = it.find(f"{ns}link")
            items.append({
                "title":   _xml_text(it, f"{ns}title"),
                "url":     (link_el.get("href") if link_el is not None else ""),
                "source":  "Reddit",
                "published": _xml_text(it, f"{ns}updated"),
                "snippet": "",
                "channel": "reddit",
            })
    except Exception as e:
        log(f"  reddit rss failed: {e}")

    # Drop self-references — we want mentions OF Vital City IN other outlets,
    # not Vital City's own articles (Google News indexes vitalcitynyc.org too).
    def _is_self(it):
        blob = (it.get("source", "") + " " + it.get("url", "")).lower()
        return ("vitalcitynyc" in blob
                or blob.endswith("vital city")
                or "source>vital city<" in blob
                or it.get("source", "").strip().lower() == "vital city")
    # Require an external outlet to actually mention "vital city" by name.
    def _mentions_us(it):
        blob = (it.get("title", "") + " " + it.get("snippet", "")).lower()
        return "vital city" in blob
    items = [it for it in items if not _is_self(it) and _mentions_us(it)]

    # De-dupe by URL, keep newest
    seen, dedup = set(), []
    for it in items:
        u = it.get("url", "").split("?", 1)[0]
        if u in seen: continue
        seen.add(u); dedup.append(it)

    # Parse published into sortable ISO; keep both
    def _parse(p):
        for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z",
                    "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                dt = datetime.strptime(p, fmt)
                return dt.astimezone(timezone.utc).isoformat()
            except Exception:
                pass
        return ""
    for it in dedup:
        it["published_iso"] = _parse(it.get("published", ""))
    dedup.sort(key=lambda it: it.get("published_iso") or "", reverse=True)
    return dedup[:60]


# --------------------------------------------------------------- Donorbox
def donorbox_creds():
    key = os.environ.get("DONORBOX_KEY") or ""
    if not key:
        f = PRIV / ".donorbox_key"
        if f.exists(): key = f.read_text().strip()
    email = os.environ.get("DONORBOX_EMAIL", "info@vitalcitynyc.org").strip()
    return key.strip(), email


def pull_donorbox():
    key, email = donorbox_creds()
    if not key:
        return {"available": False, "reason": "DONORBOX_KEY not set"}
    auth = base64.b64encode(f"{email}:{key}".encode()).decode()
    headers = {
        "Authorization": "Basic " + auth,
        "User-Agent": "VitalCity-GrowthDashboard/1.0",
        "Accept": "application/json",
    }
    donations, page = [], 1
    while True:
        try:
            url = f"https://donorbox.org/api/v1/donations?page={page}&per_page=100"
            batch = json.loads(http_get(url, headers=headers, timeout=120))
        except Exception as e:
            log(f"  donorbox page {page} failed: {e}")
            break
        if not batch: break
        donations.extend(batch)
        if len(batch) < 100: break
        page += 1
        if page > 200: break   # safety cap

    paid = [d for d in donations if (d.get("status") or "").lower() == "paid"]
    if not paid:
        return {"available": True, "donations_paid": 0, "reason": "no paid donations in account"}

    # Normalize fields we'll use
    def _amt(d):
        try: return float(d.get("amount") or 0)
        except: return 0.0
    def _net(d):
        try: return float(d.get("net_amount") or 0)
        except: return 0.0
    def _day(d):  return (d.get("donation_date") or "")[:10]
    def _mon(d):  return (d.get("donation_date") or "")[:7]
    def _email(d): return ((d.get("donor") or {}).get("email") or "").strip().lower()
    def _recurring(d): return bool(d.get("recurring"))
    def _campaign(d): return ((d.get("campaign") or {}).get("name") or "(no campaign)").strip()

    from datetime import date as _date
    from collections import defaultdict as _dd, Counter as _C
    today = datetime.now(timezone.utc).date()
    y = today.year
    ytd_start = _date(y, 1, 1); py_start = _date(y-1, 1, 1)
    py_end = _date(y-1, today.month, today.day)
    d30 = today - timedelta(days=30); d90 = today - timedelta(days=90); d7 = today - timedelta(days=7)
    yoy30_end = _date(y-1, today.month, today.day); yoy30_start = yoy30_end - timedelta(days=30)

    def _agg(items):
        if not items: return {"count": 0, "amount": 0.0, "net": 0.0, "donors": 0,
                              "recurring_amount": 0.0, "onetime_amount": 0.0, "avg_gift": 0.0}
        emails = set()
        amt = net = rec_amt = one_amt = 0.0
        new_donors = 0
        for d in items:
            a = _amt(d); n = _net(d)
            amt += a; net += n
            if _recurring(d): rec_amt += a
            else: one_amt += a
            em = _email(d)
            if em: emails.add(em)
        return {
            "count": len(items),
            "amount": round(amt, 2),
            "net": round(net, 2),
            "donors": len(emails),
            "recurring_amount": round(rec_amt, 2),
            "onetime_amount":   round(one_amt, 2),
            "avg_gift": round(amt / len(items), 2) if items else 0.0,
        }

    def _in(items, start, end):
        s, e = start.isoformat(), end.isoformat()
        return [d for d in items if s <= _day(d) <= e]

    # Daily series (last 365 days for the trend chart)
    daily = _dd(lambda: {"d": "", "amt": 0.0, "n": 0, "donors": set()})
    cutoff = (today - timedelta(days=365)).isoformat()
    for d in paid:
        day = _day(d)
        if day < cutoff or day > today.isoformat(): continue
        r = daily[day]; r["d"] = day
        r["amt"] += _amt(d); r["n"] += 1
        em = _email(d)
        if em: r["donors"].add(em)
    daily_series = [{"d": r["d"], "amt": round(r["amt"], 2), "gifts": r["n"], "donors": len(r["donors"])}
                    for r in sorted(daily.values(), key=lambda x: x["d"])]

    # Monthly series (24 months for YoY trend)
    monthly = _dd(lambda: {"m": "", "amt": 0.0, "n": 0, "donors": set(), "recurring_amt": 0.0})
    for d in paid:
        m = _mon(d)
        if not m: continue
        r = monthly[m]; r["m"] = m
        a = _amt(d); r["amt"] += a; r["n"] += 1
        if _recurring(d): r["recurring_amt"] += a
        em = _email(d)
        if em: r["donors"].add(em)
    monthly_series = [{"m": r["m"], "amt": round(r["amt"], 2), "gifts": r["n"],
                       "donors": len(r["donors"]), "recurring_amt": round(r["recurring_amt"], 2)}
                      for r in sorted(monthly.values(), key=lambda x: x["m"])]

    # Top campaigns YTD + all-time
    camp_ytd = _C(); camp_all = _C()
    for d in paid:
        a = _amt(d); name = _campaign(d)
        camp_all[name] += a
        if _day(d) >= ytd_start.isoformat(): camp_ytd[name] += a
    top_campaigns = [{"name": n, "amount": round(a, 2)} for n, a in camp_ytd.most_common(6)]

    # Two gift lists for the dashboard: largest in the last 90 days, and the
    # most recent regardless of size.
    def _gift_row(d):
        return {
            "amount": _amt(d),
            "net": _net(d),
            "date": _day(d),
            "donor": ((d.get("donor") or {}).get("name") or "").strip() or "Anonymous",
            "recurring": _recurring(d),
            "campaign": _campaign(d),
            "comment": (d.get("comment") or "")[:240],
        }
    d90_start = d90
    largest = sorted(_in(paid, d90_start, today), key=_amt, reverse=True)[:8]
    top_recent  = [_gift_row(d) for d in largest]   # key name kept for dashboard compat; now last 90d
    latest = sorted(paid, key=_day, reverse=True)[:8]
    latest_gifts = [_gift_row(d) for d in latest]
    # Every gift in the last 21 days w/ donor + email + date, for the 7d-box
    # "Gifts" click-through (small counts, so a full list is fine).
    d21 = today - timedelta(days=21)
    recent_gifts = [{**_gift_row(d), "email": _email(d)}
                    for d in sorted(_in(paid, d21, today), key=_day, reverse=True)]

    # Active recurring donors + MRR estimate
    rec_donors = set(); mrr = 0.0
    last90 = _in(paid, d90, today)
    for d in last90:
        if _recurring(d):
            em = _email(d)
            if em: rec_donors.add(em)
    # MRR: sum recurring gifts in last 30d (rough proxy)
    for d in _in(paid, d30, today):
        if _recurring(d): mrr += _amt(d)

    # Earliest paid gift in this account — honest signal for YoY validity
    oldest = min((_day(d) for d in paid if _day(d)), default="")
    yoy_ok = bool(oldest and oldest < py_start.isoformat())

    # ---- Per-window gift + donor lists so the headline KPIs can drill in ----
    def _name(d): return ((d.get("donor") or {}).get("name") or "").strip() or "Anonymous"
    def _lean(d):
        return {"amount": _amt(d), "date": _day(d), "donor": _name(d),
                "recurring": _recurring(d), "campaign": _campaign(d)}
    def _donor_agg(rows):   # dedupe by email (matches the unique-donor counts)
        agg = {}
        for d in rows:
            key = _email(d) or ("name:" + _name(d).lower())
            a = agg.setdefault(key, {"donor": _name(d), "amount": 0.0, "gifts": 0, "last": "", "recurring": False})
            a["amount"] += _amt(d); a["gifts"] += 1
            if _day(d) > a["last"]: a["last"] = _day(d)
            if _recurring(d): a["recurring"] = True
        return sorted(({**v, "amount": round(v["amount"], 2)} for v in agg.values()), key=lambda x: -x["amount"])
    _ytd_rows = _in(paid, ytd_start, today)
    gifts_ytd = [_lean(d) for d in sorted(_ytd_rows, key=_day, reverse=True)]
    gifts_30  = [_lean(d) for d in sorted(_in(paid, d30, today), key=_day, reverse=True)]
    donors_ytd = _donor_agg(_ytd_rows)
    donors_all = _donor_agg(paid)
    rec_rows = [d for d in last90 if _recurring(d) and _email(d)]
    recurring_donors = _donor_agg(rec_rows)

    return {
        "available": True,
        "donations_paid": len(paid),
        "history_starts": oldest,
        "yoy_ok": yoy_ok,
        "windows": {
            "ytd":       _agg(_in(paid, ytd_start, today)),
            "prior_ytd": _agg(_in(paid, py_start,  py_end)),
            "last_30":   _agg(_in(paid, d30, today)),
            "yoy_30":    _agg(_in(paid, yoy30_start, yoy30_end)),
            "last_7":    _agg(_in(paid, d7, today)),
            "all_time":  _agg(paid),
        },
        "daily_series":   daily_series,
        "monthly_series": monthly_series,
        "top_campaigns":  top_campaigns,
        "top_recent":     top_recent,      # largest gifts, last 90 days
        "latest_gifts":   latest_gifts,    # most recent gifts, any size
        "recent_gifts":   recent_gifts,    # all gifts last 21d (for 7d-box click-through)
        "active_recurring_donors": len(rec_donors),
        "mrr_estimate":   round(mrr, 2),
        "gifts_ytd":         gifts_ytd,          # every YTD gift (drill-in for Raised/Gifts YTD)
        "gifts_30":          gifts_30,           # gifts in the last 30 days
        "donors_ytd":        donors_ytd,         # unique YTD donors, email-deduped
        "donors_all":        donors_all,         # unique all-time donors
        "recurring_donors":  recurring_donors,   # active recurring donors (last 90d)
    }


# ----------------------------------------------------- X (Twitter) — free path
# Uses Twitter's public syndication endpoint (the same one their embed widgets
# hit). Returns follower/following/tweet counts + the 100 most recent tweets
# with per-tweet likes/retweets/replies. NO auth required. Caveats: it's
# unofficial, so it can break at any time; we treat it as best-effort.
# Hand-maintained follower counts for platforms we can't read for free — X's
# API is paid and Instagram's public count isn't reliably scrapeable. Update
# these by hand (and bump as_of). Used only when the live fetch can't get a
# count, so the Social-followers total can still include all four platforms.
MANUAL_FOLLOWERS = {
    "x":         {"followers": 4515, "as_of": "2026-09-09"},
    "instagram": {"followers": 765, "as_of": "2026-08-14"},
    "facebook":  {"followers": 208, "as_of": "2026-08-14"},
}


def _manual_follower_stub(platform, handle, reason):
    mf = MANUAL_FOLLOWERS.get(platform) or {}
    if mf.get("followers"):
        return {"available": True, "manual": True, "followers": mf["followers"],
                "as_of": mf.get("as_of"), "handle": handle, "reason": reason}
    return {"available": False, "reason": reason}


def pull_x_followers():
    """Who follows Vital City on X — from a point-in-time follower export
    (private/x_followers_source.csv, dropped by hand; X's API is paid, so this
    is a manual snapshot, NOT live). Computes reach + a named list of the most
    influential followers, and cross-matches them to the contact database so
    the growth dashboard can show which high-profile followers we already have
    on the newsletter vs. who's reaching us only on social.
    Refresh: overwrite the CSV (and x_followers_asof.txt), re-pack the bundle."""
    import csv as _csv, re as _re, unicodedata as _ud, collections
    src = PRIV / "x_followers_source.csv"
    if not src.exists():
        return {"available": False, "reason": "no follower export bundled yet"}
    try:
        rows = list(_csv.DictReader(open(src, encoding="utf-8-sig")))
        asof_f = PRIV / "x_followers_asof.txt"
        as_of = asof_f.read_text().strip() if asof_f.exists() else ""
        def num(r, k):
            try: return int((r.get(k) or "0").replace(",", "") or 0)
            except Exception: return 0
        def norm(s):
            s = _ud.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
            return _re.sub(r"[^a-z ]+", " ", s).strip()
        total = len(rows)
        reach = sum(num(r, "Followers Count") for r in rows)
        loc = [(r.get("Location") or "").strip() for r in rows]
        have_loc = sum(1 for l in loc if l)
        nyc = sum(1 for l in loc if _re.search(r"new york|nyc|brooklyn|manhattan|queens|bronx|\bny\b", l, _re.I))
        def band(n): return ("100k+" if n >= 100_000 else "10k-100k" if n >= 10_000 else "1k-10k" if n >= 1000 else "<1k")
        bc = collections.Counter(band(num(r, "Followers Count")) for r in rows)
        bands = [[b, bc.get(b, 0)] for b in ["100k+", "10k-100k", "1k-10k", "<1k"]]

        # Cross-match the influential followers (>=5k own-audience) to the
        # contact DB by name (and press Twitter handle where we have it).
        by_name, by_tw = {}, {}
        try:
            for p in json.loads((PRIV / "people.json").read_text()):
                nm = norm(p.get("n"))
                if len(nm.split()) >= 2: by_name.setdefault(nm, p)
                tw = (p.get("ptw") or "").lower().lstrip("@").strip()
                if tw: by_tw[tw] = p
        except Exception as e:
            log(f"  x_followers: contact cross-match skipped ({e})")
        def match(f):
            return by_tw.get((f.get("Username") or "").lower().strip()) or by_name.get(norm(f.get("Name")))

        infl = sorted((r for r in rows if num(r, "Followers Count") >= 5000),
                      key=lambda r: -num(r, "Followers Count"))
        in_db = subs = notable = 0
        def entry(f, p):
            return {"name": (f.get("Name") or "").strip(), "handle": (f.get("Username") or "").strip(),
                    "followers": num(f, "Followers Count"), "bio": (f.get("Bio") or "").strip()[:120],
                    "in_db": bool(p), "sub": bool(p and p.get("mem") and not p.get("unsub")),
                    "notable": bool(p and p.get("wiki"))}
        top, net_new = [], []
        for f in infl:
            p = match(f)
            if p:
                in_db += 1
                if p.get("mem") and not p.get("unsub"): subs += 1
                if p.get("wiki"): notable += 1
            else:
                net_new.append(entry(f, None))
            if len(top) < 40: top.append(entry(f, p))
        log(f"  x_followers: {total:,} followers, {reach:,} combined reach ({as_of}); {len(infl)} with 5k+, {in_db} already in contacts")
        return {"available": True, "as_of": as_of, "total": total, "combined_reach": reach,
                "have_location": have_loc, "nyc_area": nyc, "bands": bands,
                "influential": len(infl), "top": top,
                "crossmatch": {"influential": len(infl), "in_db": in_db, "subscribers": subs,
                               "notable_db": notable, "net_new": len(net_new)},
                "net_new_top": net_new[:24]}
    except Exception as e:
        log(f"  x_followers pull failed: {e}")
        return {"available": False, "reason": f"follower export present but parse failed: {e}"}


def pull_x():
    import re as _re
    url = "https://syndication.twitter.com/srv/timeline-profile/screen-name/vitalcitynyc"
    ua  = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    try:
        html = http_get(url, headers={"User-Agent": ua}, timeout=20).decode("utf-8", "ignore")
        m = _re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', html, _re.S)
        if not m: raise RuntimeError("no embedded JSON")
        d = json.loads(m.group(1))
        entries = d["props"]["pageProps"]["timeline"]["entries"]
        tweets = []
        user = None
        for e in entries:
            if e.get("type") != "tweet": continue
            t = e.get("content", {}).get("tweet", {})
            if not t: continue
            if user is None: user = t.get("user", {}) or {}
            tweets.append({
                "id":         t.get("id_str") or str(t.get("id") or ""),
                "created_at": t.get("created_at"),
                "text":       (t.get("text") or t.get("full_text") or "")[:280],
                "likes":      int(t.get("favorite_count") or 0),
                "retweets":   int(t.get("retweet_count")  or 0),
                "replies":    int(t.get("reply_count")    or 0) if t.get("reply_count") is not None else None,
            })
        if user is None:
            return _manual_follower_stub("x", "@VitalCityNYC", "syndication endpoint returned no tweets")
        # ISO-normalize tweet timestamps for sort + UI
        def _iso(p):
            try:
                from email.utils import parsedate_to_datetime
                return parsedate_to_datetime(p).astimezone(timezone.utc).isoformat()
            except Exception: return ""
        for t in tweets: t["created_iso"] = _iso(t.get("created_at") or "")
        tweets.sort(key=lambda t: t["created_iso"] or "", reverse=True)
        avg_likes = round(sum(t["likes"] for t in tweets[:20]) / max(len(tweets[:20]), 1), 1)
        return {
            "available": True,
            "source": "syndication.twitter.com (unofficial, no API key)",
            "handle": user.get("screen_name"),
            "name":   user.get("name"),
            "followers": int(user.get("followers_count") or 0),
            "following": int(user.get("friends_count")   or 0),
            "tweets_total": int(user.get("statuses_count") or 0),
            "avg_likes_recent_20": avg_likes,
            # Keep all syndication tweets (up to 100) so flag_own_url_shares
            # has a deeper matching set. The dashboard's social card still
            # slices to the most recent ~10 for display.
            "recent_tweets": tweets[:100],
        }
    except Exception as e:
        return _manual_follower_stub("x", "@VitalCityNYC", f"X scrape failed: {e}")


# ------------------------------------------------------- Instagram — free path
# Uses the same web_profile_info endpoint Instagram's own web app calls.
# Needs the X-IG-App-ID header (a public constant) and a browser User-Agent.
# Same best-effort framing as X.
def pull_instagram():
    url = "https://www.instagram.com/api/v1/users/web_profile_info/?username=vitalcitynyc"
    headers = {
        "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
                       "AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1 "
                       "Instagram 285.0.0.16.119"),
        "X-IG-App-ID": "936619743392459",
        "Accept": "application/json",
    }
    try:
        d = json.loads(http_get(url, headers=headers, timeout=20))
        u = (d.get("data") or {}).get("user") or {}
        if not u: return _manual_follower_stub("instagram", "@vitalcitynyc", "empty user data")
        posts_out = []
        for edge in (u.get("edge_owner_to_timeline_media") or {}).get("edges", [])[:12]:
            n = edge.get("node") or {}
            cap_edges = (n.get("edge_media_to_caption") or {}).get("edges") or []
            cap = (cap_edges[0].get("node", {}).get("text", "") if cap_edges else "")[:240]
            posts_out.append({
                "id":        n.get("id"),
                "shortcode": n.get("shortcode"),
                "url":       f"https://www.instagram.com/p/{n.get('shortcode')}/" if n.get("shortcode") else None,
                "timestamp": n.get("taken_at_timestamp"),
                "iso":       (datetime.fromtimestamp(int(n["taken_at_timestamp"]), tz=timezone.utc).isoformat()
                              if n.get("taken_at_timestamp") else ""),
                "likes":     int((n.get("edge_liked_by") or {}).get("count") or 0),
                "comments":  int((n.get("edge_media_to_comment") or {}).get("count") or 0),
                "caption":   cap,
                "type":      n.get("__typename") or n.get("product_type") or "",
            })
        avg_likes = round(sum(p["likes"] for p in posts_out[:10]) / max(len(posts_out[:10]), 1), 1) if posts_out else 0
        return {
            "available": True,
            "source": "instagram.com web_profile_info (unofficial, no API key)",
            "handle": u.get("username"),
            "name":   u.get("full_name"),
            "bio":    (u.get("biography") or "")[:240],
            "followers": int((u.get("edge_followed_by") or {}).get("count") or 0),
            "following": int((u.get("edge_follow")      or {}).get("count") or 0),
            "posts_total": int((u.get("edge_owner_to_timeline_media") or {}).get("count") or 0),
            "avg_likes_recent_10": avg_likes,
            "recent_posts": posts_out,
        }
    except Exception as e:
        return _manual_follower_stub("instagram", "@vitalcitynyc", f"Instagram scrape failed: {e}")


# ------------------------------------------------------------------------ main
def pull_all_ghost_titles():
    """All-time Ghost article titles via the Content API. Used for own-post
    detection — matching a share's title against the full title catalog
    catches LinkedIn/Facebook/Instagram VC-account reposts that use the
    article title verbatim. Fast — only titles, ~10 KB total."""
    titles = set()
    page = 1
    while True:
        try:
            url = (f"{GHOST_API}/posts/?key={GHOST_CONTENT_KEY}"
                   f"&limit=100&page={page}&fields=title")
            data = json.loads(http_get(url, timeout=30))
        except Exception as e:
            log(f"  ghost title catalog page {page} failed: {e}"); break
        for p in data.get("posts", []):
            t = (p.get("title") or "").strip()
            if t and len(t) > 8: titles.add(t)
        meta = (data.get("meta") or {}).get("pagination") or {}
        if not meta.get("next"): break
        page = meta["next"]
        if page > 30: break   # safety
    log(f"  ghost title catalog: {len(titles)} titles")
    return titles


def pull_own_social_posts():
    """Query Google News restricted to VC's own social-account paths
    (x.com/VitalCityNYC, linkedin.com/company/vitalcitynyc). Returns
    per-domain sets of post titles that are KNOWN to be VC's own — used
    as a stronger own-vs-third-party signal than article-title matching,
    which only catches LinkedIn company-page reposts.
    """
    UA_local = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
    # Site paths that Google News will accept. We search each path with three
    # query shapes to gather as many VC-own titles as possible (the brand
    # phrase, the URL, and a wildcard).
    sources = [
        ("x.com",        ["site:x.com/VitalCityNYC",
                          "site:twitter.com/VitalCityNYC",
                          'site:x.com/VitalCityNYC "vitalcitynyc.org"']),
        ("linkedin.com", ["site:linkedin.com/company/vitalcitynyc",
                          'site:linkedin.com/company/vitalcitynyc "vitalcitynyc"']),
    ]
    out = {}
    for dom, qs in sources:
        titles = []
        for q in qs:
            try:
                url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=en-US&gl=US&ceid=US:en"
                xml = http_get(url, headers={"User-Agent": UA_local}, timeout=20)
                root = ET.fromstring(xml)
                for it in root.findall(".//item"):
                    t = _xml_text(it, "title").strip()
                    if t and len(t) > 8: titles.append(t)
            except Exception as e:
                log(f"  own-social ({dom}/{q[:40]}) failed: {e}")
        out[dom] = list(set(titles))   # dedup within domain
    log(f"  VC own social posts: {sum(len(v) for v in out.values())} titles ({', '.join(f'{k}={len(v)}' for k,v in out.items())})")
    return out


def flag_own_url_shares(news_mentions, all_titles, vc_tweets=None, own_social=None):
    """For each URL-share item, classify it and decide whether to keep it.

    Two-stage filter:
      1. KEEP-OR-DROP: the title or snippet must actually look like a VC
         reference (brand phrase or known article title substring). Filters
         out Facebook Page false positives like "Model Cities Initiative".
      2. OWN vs THIRD-PARTY: a share is flagged as VC's own social post if
         (a) its title matches a known VC article title verbatim or substring
         (catches LinkedIn company-page reposts), OR
         (b) its title matches text from a recent @VitalCityNYC tweet pulled
         via the syndication feed (catches X tweets where VC's promotional
         copy is original and doesn't repeat the article title — "Amid the
         seemingly endless news...", "Left: Vital City in January..." etc.).
    """
    if not news_mentions:
        return
    norm = lambda s: re.sub(r"[\W_]+", "", (s or "").lower())
    titles_n = {norm(t) for t in (all_titles or set())}
    substr_titles = [t for t in titles_n if len(t) > 20]
    # Normalize VC's own recent tweets for matching X shares. We strip URLs
    # and handles before normalizing because Google News titles often drop
    # the trailing t.co links that VC tweets include.
    vc_tweets_n = set()
    if vc_tweets:
        for t in vc_tweets:
            cleaned = re.sub(r"https?://\S+", "", t or "")
            cleaned = re.sub(r"@\w+", "", cleaned)
            n = norm(cleaned)
            if len(n) > 25:    # ignore very short tweets to avoid bad matches
                vc_tweets_n.add(n)
    # Per-domain own-social-post titles (from pull_own_social_posts —
    # Google News restricted to x.com/VitalCityNYC and
    # linkedin.com/company/vitalcitynyc). Stronger signal than vc_tweets
    # because it doesn't depend on the syndication feed's quirks.
    own_titles_by_dom = {}
    if own_social:
        for dom, titles in own_social.items():
            normd = set()
            PLATFORM_TAILS_pre = re.compile(r"\s*[-–—]\s*[\w\.]+\s*$")
            for t in titles:
                t2 = PLATFORM_TAILS_pre.sub("", t).strip()
                t2 = re.sub(r"https?://\S+", "", t2)
                t2 = re.sub(r"@\w+", "", t2)
                n = norm(t2)
                if len(n) > 20: normd.add(n)
            own_titles_by_dom[dom] = normd
    PLATFORM_TAILS = re.compile(
        r"\s*[-–—]\s*(LinkedIn|Facebook|Instagram|X|Twitter|Bluesky|Threads|"
        r"x\.com|twitter\.com|bsky\.app|facebook\.com|instagram\.com)\s*$", re.I)
    BRAND = re.compile(r"(?i)\bvital\s*city\b|vitalcitynyc")

    def looks_like_vc(it):
        # Brand phrase or domain in title or snippet → clearly about us
        if BRAND.search(it.get("title", "")) or BRAND.search(it.get("snippet", "")):
            return True
        # Otherwise require a long-enough match against a known VC article title
        t_norm = norm(PLATFORM_TAILS.sub("", it.get("title", "")).strip())
        if len(t_norm) < 15: return False
        for ti in substr_titles:
            if ti in t_norm or t_norm in ti: return True
        return False

    kept = []
    for it in news_mentions:
        # Platform-native results (Bluesky API, Reddit search) carry definitive
        # metadata: own_account means the platform says VC authored it, and
        # native_search means the platform's own index matched the query — no
        # Google-leniency false-positive risk, so skip the heuristics.
        if it.get("own_account"):
            it["own_post"] = True
            kept.append(it); continue
        if it.get("native_search"):
            it["own_post"] = False
            kept.append(it); continue
        if it.get("is_url_share"):
            if not looks_like_vc(it):
                continue  # drop the false positive
            t = it.get("title") or ""
            stripped = PLATFORM_TAILS.sub("", t).strip()
            s_norm = norm(stripped)
            # Strip URLs/handles from the title before matching against VC's
            # own X tweets — Google often drops the t.co link from the end
            # while VC's tweet text includes one.
            s_norm_x = norm(re.sub(r"@\w+", "", re.sub(r"https?://\S+", "", stripped)))
            domain_lc = (it.get("domain") or "").lower()
            is_x = ("x.com" in domain_lc or "twitter" in domain_lc)
            if s_norm in titles_n:
                it["own_post"] = True
            elif any(ti in s_norm or (len(s_norm) > 20 and s_norm in ti) for ti in substr_titles):
                it["own_post"] = True
            elif is_x and vc_tweets_n and len(s_norm_x) > 25 and any(
                tw in s_norm_x or s_norm_x in tw for tw in vc_tweets_n
            ):
                # Title matches one of @VitalCityNYC's recent tweets — own post
                it["own_post"] = True
            else:
                # Check against per-domain own-social-post titles from Google
                # News (site:x.com/VitalCityNYC etc.) — the strongest signal
                # for X & LinkedIn own posts that aren't article-title reposts.
                own = False
                for dom, normd in own_titles_by_dom.items():
                    if dom not in domain_lc: continue
                    if len(s_norm_x) > 20 and any(t in s_norm_x or s_norm_x in t for t in normd):
                        own = True; break
                it["own_post"] = own
        else:
            it["own_post"] = False
        kept.append(it)
    # Mutate the list in place so callers using the same reference see the result
    news_mentions[:] = kept


def attribute_signups_to_posts(mc, gh, signup_attr=None, window_days=4):
    """Per-post newsletter signup attribution.

    If we have Ghost's real per-event attribution feed (passed as `signup_attr`),
    we use the *exact* count of signups whose attribution.url matches the post —
    no correlation needed, no shared-day ambiguity. Otherwise we fall back to
    the old correlational approach: sum signups on publish_day + window_days,
    compare against the typical active X-day window.
    """
    # ---------- REAL attribution path (Ghost member-events) ------------
    if signup_attr and signup_attr.get("available") and signup_attr.get("by_url"):
        by_url = signup_attr["by_url"]
        # Collect raw counts so we can compute a meaningful baseline
        counts = sorted((r["signups"] for r in by_url.values()), reverse=True)
        if counts:
            # "Typical" post on the list (median of all attributed-to-a-post URLs)
            median_real = counts[len(counts)//2] if counts else 0
            for p in gh["posts"]:
                u = (p.get("url") or "").rstrip("/")
                r = by_url.get(u)
                if not r:
                    p["direct_signups"] = 0
                    continue
                p["direct_signups"] = r["signups"]
                p["direct_sources"] = r.get("top_sources", [])
                # A post is a "mover" if it directly attracted notably more
                # signups than a typical attributed-to-a-post URL (3× median),
                # and the absolute floor is at least 8 signups.
                # Threshold tuned for direct-attribution counts: a post is a
                # "mover" if it directly attracted at least 4 signups AND at
                # least 3× the median post. (Direct attribution is stricter
                # than the old correlational window — homepage-routed signups
                # don't land on the post URL, so post-level numbers are
                # naturally smaller. ~4 is the practical floor for "this
                # piece converted on its own page.")
                p["mover"] = p["direct_signups"] >= 4 and p["direct_signups"] >= median_real * 3
                # Keep these fields consistent with the old surface so the
                # dashboard rendering doesn't need to change much.
                p["signups_window"] = p["direct_signups"]
                p["signups_window_days"] = signup_attr.get("window_days") or window_days
                if median_real:
                    p["lift"] = round(p["direct_signups"] / median_real, 2)
            gh["signup_attribution_mode"] = "real"
            gh["signup_baseline_xd"] = median_real
            gh["signup_attribution_window_days"] = signup_attr.get("window_days") or window_days
            return
    # ---------- Correlational fallback ---------------------------------
    if not (mc and mc.get("daily_activity") and gh and gh.get("posts")):
        return
    daily = {r["d"]: int(r.get("subs") or 0) for r in mc["daily_activity"] if r.get("d")}
    if not daily: return

    # Compute a baseline: median X-day rolling sum across the whole window.
    days = sorted(daily)
    sums = []
    for i in range(len(days) - window_days + 1):
        sums.append(sum(daily[days[j]] for j in range(i, i + window_days)))
    if not sums: return
    # Use the median of *active* windows (sum > 0) — the dataset is zero-rich
    # because the daily signup data is only present where people.json has a
    # subscriber whose Ghost `since` date hits that day, so plain median is
    # dragged to zero and lift would explode meaninglessly.
    active = sorted(s for s in sums if s > 0)
    if not active: return
    median_xd = active[len(active) // 2]
    # A robust "spike" threshold: at least 1.5x median AND at least 12 absolute
    # signups in the window (so small post-day signup counts don't ping).
    LIFT_THRESHOLD = 1.35
    MIN_ABS = 12

    earliest = days[0]; latest = days[-1]

    for p in gh["posts"]:
        d0 = (p.get("published") or "")[:10]
        if not d0 or d0 < earliest or d0 > latest:
            p["signups_window"] = None
            continue
        # Sum the [d0, d0 + window_days) window — clamp to data range
        from datetime import date as _date
        try:
            y, m, dd = (int(x) for x in d0.split("-"))
            start = _date(y, m, dd)
        except Exception:
            p["signups_window"] = None
            continue
        s = 0; valid = False
        for k in range(window_days):
            day = (start + timedelta(days=k)).isoformat()
            if day in daily:
                s += daily[day]; valid = True
        if not valid:
            p["signups_window"] = None
            continue
        p["signups_window"] = s
        p["signups_window_days"] = window_days
        if median_xd > 0:
            p["lift"] = round(s / median_xd, 2)
        else:
            p["lift"] = None
        p["mover"] = bool(p.get("lift") and p["lift"] >= LIFT_THRESHOLD and s >= MIN_ABS)

    gh["signup_baseline_xd"] = median_xd
    gh["signup_window_days"] = window_days
    gh["signup_attribution_mode"] = "correlational"


def attribute_donations_to_posts(db, gh, window_days=14):
    """Donor attribution: for each post, sum donations + dollars received in
    the X-day window after publish, compare to the typical active X-day
    donation window. Correlational only — Donorbox doesn't track which page
    a donor was on when they gave. Same caveats as the (old) signup version:
    multiple posts in a window share the lift, outside drivers exist, last
    couple of days under-count.
    """
    if not (db and db.get("available") and gh and gh.get("posts")):
        return
    daily = {r["d"]: r for r in (db.get("daily_series") or [])}
    if not daily:
        return
    days = sorted(daily)
    sums_amt = []
    for i in range(len(days) - window_days + 1):
        sums_amt.append(sum(daily[days[j]]["amt"] for j in range(i, i + window_days)))
    if not sums_amt:
        return
    active = sorted(s for s in sums_amt if s > 0)
    if not active:
        return
    median_amt = active[len(active) // 2]
    LIFT_THRESHOLD = 1.5
    MIN_GIFTS = 3
    MIN_AMT = 100.0
    earliest = days[0]; latest = days[-1]

    from datetime import date as _date
    for p in gh["posts"]:
        d0 = (p.get("published") or "")[:10]
        if not d0 or d0 < earliest or d0 > latest:
            p["donations_window"] = None
            continue
        try:
            y, m, dd = (int(x) for x in d0.split("-"))
            start = _date(y, m, dd)
        except Exception:
            p["donations_window"] = None
            continue
        amt = 0.0; n = 0; valid = False
        for k in range(window_days):
            day = (start + timedelta(days=k)).isoformat()
            if day in daily:
                amt += daily[day]["amt"]; n += daily[day]["gifts"]; valid = True
        if not valid:
            p["donations_window"] = None
            continue
        p["donations_window_amt"]  = round(amt, 2)
        p["donations_window_n"]    = n
        p["donations_window_days"] = window_days
        if median_amt > 0:
            p["donor_lift"] = round(amt / median_amt, 2)
        p["donor_mover"] = bool(
            n >= MIN_GIFTS and amt >= MIN_AMT
            and p.get("donor_lift", 0) >= LIFT_THRESHOLD
        )
    gh["donation_baseline_xd"] = round(median_amt, 2)
    gh["donation_window_days"] = window_days


def main():
    PRIV.mkdir(parents=True, exist_ok=True)
    mc = pull_mailchimp()
    gh = pull_ghost()
    signup_attr = pull_ghost_signup_attribution(days_back=180)
    db = pull_donorbox()
    attribute_signups_to_posts(mc, gh, signup_attr=signup_attr)
    attribute_donations_to_posts(db, gh)
    # News mentions need to be pulled here (was at the bottom of out) so we
    # can post-process URL-share items for own-post flagging.
    news_mentions = pull_news_mentions()
    all_titles = pull_all_ghost_titles()
    # Pull VC's own X tweets to recognize URL shares that are reposts from
    # @VitalCityNYC's own account (where VC's tweet copy doesn't match an
    # article title verbatim — promotional text, contextual one-liners, etc.).
    xprof = pull_x()
    vc_tweets = [t.get("text","") for t in (xprof.get("recent_tweets") or [])] if xprof.get("available") else []
    own_social = pull_own_social_posts()
    flag_own_url_shares(news_mentions, all_titles, vc_tweets=vc_tweets, own_social=own_social)
    # Capture MAU/AAU sets BEFORE they're popped, for the people.json enrich pass
    mau_set_for_enrich = mc.get("_mau_set") or set()
    aau_set_for_enrich = mc.get("_aau_set") or set()
    lifecycle = build_lifecycle(mc)
    engagement_extras = build_engagement_extras(mc, signup_attr, db)
    # Strip internal-only fields from in-memory objects before JSON write
    mc.pop("_mau_set", None); mc.pop("_aau_set", None)
    signup_attr.pop("_by_email", None)

    # ---- Enrich people.json with engagement flags --------------------
    # Add mau / aau / power_reader / at_risk / sunset booleans to each
    # subscriber's record so the Contact tool can filter to these exact
    # subsets that the Growth dashboard surfaces.
    #
    # IMPORTANT — this function NEVER encrypts anything. It only writes the
    # flags into the plaintext private/people.json (which is gitignored and
    # never published). Encryption of network/data.enc is the SOLE
    # responsibility of encrypt_people.py, which the workflow runs as its
    # own step AFTER this one — so the flags are baked into the single
    # passphrase-correct encryption. A prior version called
    # encrypt_people.main() inline here; in the cloud that step lacked
    # VC_NETWORK_PASS and fell through to a freshly generated passphrase,
    # re-encrypting the contact DB with a throwaway key and locking Josh
    # out. That coupling is gone for good: enrich here, encrypt elsewhere.
    import os as _os
    pj_path = PRIV / "people.json"
    if pj_path.exists():
        try:
            people = json.loads(pj_path.read_text())
        except Exception as e:
            log(f"  enrich people.json: read failed: {e}")
            people = None
        if people is not None:
            # Pull the engagement subsets we computed
            power_emails = {r["email"] for r in (engagement_extras.get("power_readers_list") or [])}
            # Use the UNCAPPED email lists — `list` is truncated to 500 for the
            # dashboard modal, and reading the flags off it meant the Contact
            # tool's "At-risk" and "Sunset candidates" filters only ever matched
            # the first 500 people alphabetically.
            _ar, _sc = lifecycle.get("at_risk", {}), lifecycle.get("sunset_candidates", {})
            at_risk_emails = {e for e in (_ar.get("emails_all") or [r["email"] for r in (_ar.get("list") or []) if r.get("email")])}
            sunset_emails  = {e for e in (_sc.get("emails_all") or [r["email"] for r in (_sc.get("list") or []) if r.get("email")])}
            updated = 0
            for p in people:
                em_list = [e.lower().strip() for e in (p.get("emails") or [p.get("e","")]) if e]
                old_mau = p.get("mau"); old_aau = p.get("aau")
                old_pr = p.get("power_reader"); old_ar = p.get("at_risk"); old_sc = p.get("sunset_candidate")
                p["mau"] = bool(any(em in mau_set_for_enrich for em in em_list))
                p["aau"] = bool(any(em in aau_set_for_enrich for em in em_list))
                p["power_reader"]     = bool(any(em in power_emails   for em in em_list))
                p["at_risk"]          = bool(any(em in at_risk_emails for em in em_list))
                p["sunset_candidate"] = bool(any(em in sunset_emails  for em in em_list))
                if (old_mau, old_aau, old_pr, old_ar, old_sc) != (p["mau"], p["aau"], p["power_reader"], p["at_risk"], p["sunset_candidate"]):
                    updated += 1
            pj_path.write_text(json.dumps(people, indent=2))
            log(f"  enriched people.json with engagement flags ({updated} rows changed). "
                "encrypt_people.py (separate workflow step) will publish them.")

    # Ghost is the source of truth for signups (the public newsletter form
    # writes to Ghost first; Mailchimp is reconciled in weekly batches). Use
    # the Ghost signup_event stream to overwrite the `subs` field in the
    # Mailchimp daily activity series — that's why the dashboard's last-2-days
    # signup count was reading zero (people.json rebuilds daily and lags).
    if signup_attr.get("available") and signup_attr.get("by_day"):
        by_day_ghost = {r["d"]: r["subs"] for r in signup_attr["by_day"]}
        # Replace the subs field with Ghost's authoritative count
        for row in (mc.get("daily_activity") or []):
            if row.get("d") in by_day_ghost:
                row["subs"] = by_day_ghost[row["d"]]
        # Add any days Ghost has that Mailchimp activity doesn't (the last
        # day or two, typically)
        existing_days = {row["d"] for row in (mc.get("daily_activity") or [])}
        for d, n in by_day_ghost.items():
            if d not in existing_days:
                mc.setdefault("daily_activity", []).append({
                    "d": d, "subs": n, "unsubs": 0, "opens": 0, "clicks": 0,
                })
        mc["daily_activity"] = sorted(mc.get("daily_activity") or [], key=lambda r: r["d"])
        # Note this in the data so the dashboard can label it
        mc["signup_source"] = "ghost_events"
        # Also recompute the signup_windows ytd/prior_ytd totals based on the
        # corrected activity series.
        from datetime import date as _date
        today = datetime.now(timezone.utc).date()
        y = today.year
        rows = mc["daily_activity"]
        def _sum(start, end, key):
            s, e = start.isoformat(), end.isoformat()
            return sum(int(r.get(key) or 0) for r in rows if s <= r["d"] <= e)
        ytd_start = _date(y, 1, 1); ytd_end = today
        py_start  = _date(y-1, 1, 1); py_end = _date(y-1, today.month, today.day)
        mc.setdefault("signup_windows", {})
        # When Mailchimp's net change for a month is zero/negative (list
        # cleanup wiped out the gross signups), patch that month's signup
        # count from Ghost's per-event count instead — Ghost only sees real
        # form signups so it's not affected by cleanups.
        from collections import defaultdict as _dd2
        ghost_month = _dd2(int)
        for row in (mc.get("daily_activity") or []):
            if row.get("subs", 0) > 0:
                ghost_month[row["d"][:7]] += int(row["subs"])
        for m in (mc.get("monthly_signups") or []):
            mo = m.get("month") or ""
            if m.get("new_signups", 0) == 0 and ghost_month.get(mo, 0) > 0:
                m["new_signups"] = ghost_month[mo]
                m["source_note"] = "Ghost events (Mailchimp net was zero/negative this month from a list cleanup)"
        # Re-compute YTD totals after the patch
        from datetime import date as _date2
        _today = datetime.now(timezone.utc).date()
        def _ytd2(year, m_through, ms):
            return sum(int(x.get("new_signups") or 0) for x in ms
                       if (x.get("month") or "").startswith(f"{year}-")
                       and (x.get("month") or "")[5:7] <= f"{m_through:02d}")
        _ms = mc.get("monthly_signups") or []
        mc["signup_windows"]["ytd"]       = _ytd2(_today.year,     _today.month, _ms)
        mc["signup_windows"]["prior_ytd"] = _ytd2(_today.year - 1, _today.month, _ms)
        mc["signup_windows"]["prior_ytd_ok"] = mc["signup_windows"]["prior_ytd"] > 0
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mailchimp": mc,
        "ghost":     gh,
        "donorbox":  db,
        # The acquisition card renders these breakdowns — they're aggregates
        # (no emails; _by_email is popped above). A past slimming pass cut
        # the payload to bare metadata and silently blanked the card.
        "ghost_signup_attribution": {
            "available":      signup_attr.get("available", False),
            "events_counted": signup_attr.get("events_counted", 0),
            "window_days":    signup_attr.get("window_days", 0),
            "coverage_start": (signup_attr.get("by_day") or [{}])[0].get("d", ""),
            "by_source":      signup_attr.get("by_source") or [],
            "by_medium":      signup_attr.get("by_medium") or [],
            "by_landing":     signup_attr.get("by_landing") or [],
            # NOTE: this block is an explicit allowlist, so a field added to
            # pull_ghost_signup_attribution()'s return value does NOT appear
            # here until it is named. by_topic was computed correctly and
            # dropped silently on the first run for exactly that reason.
            "by_topic":       signup_attr.get("by_topic") or {},
            "recent_signups": signup_attr.get("recent_signups") or [],
        },
        # Recent unsubscribers (email + date, names where Mailchimp has them)
        # for the 7d-box "Unsubscribes" click-through.
        "recent_unsubs": pull_recent_unsubs(21),
        "press":     pull_press(),
        "news_mentions": news_mentions,
        # Two channels the audit showed a brand-mention tracker structurally
        # cannot reach: law reviews (not in Google News at all) and podcasts
        # (audio leaves no indexable trace).
        "scholar_citations": pull_scholar_citations(),
        "voice_appearances": pull_voice_appearances(),
        "mentions_ledger": load_mentions_ledger(),
        "lifecycle":     lifecycle,
        "engagement_extras": engagement_extras,
        # Ghost's own Tinybird-backed site analytics (visitors, page views).
        # Needs a STAFF access token; the integration key gets 403 on every
        # /stats/* endpoint (verified live). Free once the token is added.
        "ghost_traffic": pull_ghost_traffic(),
        # LinkedIn company-page follower count (public-page meta scrape with
        # repo-cached fallback for when LinkedIn authwalls the runner).
        "linkedin": pull_linkedin_followers(),
        # Bluesky account stats (open official API).
        "bluesky": pull_bluesky_profile(),
        # Sources blocked on credentials Josh hasn't set up yet — dashboard renders
        # a "Connect this source" placeholder card with the exact setup steps.
        # Google Analytics 4 — website traffic (covers pre-April-2026 history).
        # Real pull when GA4_PROPERTY_ID + GA4_CREDS_JSON are set; stub w/ steps otherwise.
        "ga4": pull_ga4(),
        # Google Search Console — search queries/impressions/clicks/position.
        # Reuses the GA4 service account; auto-detects the property.
        "search_console": pull_search_console(),
        "x_profile":  xprof,
        # Facebook page followers — login-walled to scrapers, so manual
        # snapshots via update_social.py, same as X and Instagram.
        "facebook": _manual_follower_stub("facebook", "facebook.com/vitalcitynyc",
                                          "Facebook login-walls scrapers — manual snapshot"),
        # Who follows VC on X — point-in-time follower export, cross-matched to
        # the contact DB. Manual snapshot (X's API is paid); refresh by dropping
        # a new CSV into the bundle.
        "x_followers": pull_x_followers(),
        "instagram":  pull_instagram(),
    }
    # ---- durable follower history -------------------------------------------
    # One row per platform per day, committed to data/ so the series survives
    # every run. This is what turns "screenshot the count occasionally" into an
    # automatic record: live platforms append themselves nightly; manual ones
    # append whenever update_social.py records a new snapshot.
    try:
        hp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "social_history.json")
        hist = json.load(open(hp)) if os.path.exists(hp) else {"rows": []}
        today_s = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        have = {(r["p"], r["d"]) for r in hist["rows"]}
        def _rec(p, n, src, d=None):
            d = d or today_s
            if n and (p, d) not in have:
                hist["rows"].append({"d": d, "p": p, "n": int(n), "src": src})
        _rec("linkedin", (out.get("linkedin") or {}).get("followers"), "live")
        _rec("bluesky", (out.get("bluesky") or {}).get("followers"), "live")
        for p in ("x_profile", "instagram", "facebook"):
            v = out.get(p) or {}
            _rec("x" if p == "x_profile" else p, v.get("followers"), "manual", v.get("as_of"))
        hist["rows"].sort(key=lambda r: (r["d"], r["p"]))
        json.dump(hist, open(hp, "w"), indent=1)
        log(f"  social history: {len(hist['rows'])} observations on file")
    except Exception as e:
        log(f"  social history append failed ({e}) — non-fatal")

    # ---- Enrich the per-piece index with the signals that live in OTHER pulls,
    # so the look-up tool can show one piece's full picture in one place:
    #   * search queries it ranks for (Search Console, matched to pieces by the
    #     same catalogue matcher the opportunities panel uses)
    #   * newsletter signups attributed to it as a landing page (Ghost)
    # Both are best-effort: a piece with neither simply shows nothing there.
    try:
        idx = ((out.get("ga4") or {}).get("piece_index") or {})
        pieces = idx.get("pieces") or []
        if pieces:
            by_slug = {p["slug"]: p for p in pieces}
            by_title = {(p["title"] or "").strip().lower(): p for p in pieces}
            # 1) Search queries per piece, from every GSC window we pulled
            sc = out.get("search_console") or {}
            seen_q = {}
            def _add_q(q, rec):
                pc = (q or {}).get("piece") or {}
                url = (pc.get("url") or "").rstrip("/").rsplit("/", 1)[-1].lower()
                tgt = by_slug.get(url) or by_title.get((pc.get("title") or "").strip().lower())
                if not tgt: return
                bucket = seen_q.setdefault(tgt["slug"], {})
                prev = bucket.get(q["query"])
                if not prev or (q.get("impressions") or 0) > (prev.get("impressions") or 0):
                    bucket[q["query"]] = rec
            for win in (sc.get("windows") or {}).values():
                for q in (win.get("opportunities") or []):
                    _add_q(q, {"q": q["query"], "impr": q.get("impressions"),
                               "pos": q.get("position"), "clicks": q.get("clicks")})
            for q in (sc.get("topic_searches") or []):
                _add_q(q, {"q": q["query"], "impr": q.get("impressions"),
                           "pos": q.get("position"), "clicks": q.get("clicks")})
            for slug, qs in seen_q.items():
                rows = sorted(qs.values(), key=lambda r: -(r.get("impr") or 0))[:6]
                by_slug[slug]["queries"] = rows
                by_slug[slug]["search_impr"] = sum(r.get("impr") or 0 for r in rows)
            # 2) Newsletter signups attributed to the piece as landing page
            att = out.get("ghost_signup_attribution") or {}
            sig = {}
            for s in (att.get("recent_signups") or []):
                t = (s.get("landing_title") or "").strip().lower()
                if t: sig[t] = sig.get(t, 0) + 1
            for t, n in sig.items():
                tgt = by_title.get(t)
                if tgt: tgt["signups"] = n
            idx["signup_window_days"] = att.get("window_days")
            # 3) Newsletter clicks per piece, from the per-link click details
            lc = (out.get("mailchimp") or {}).get("link_clicks") or {}
            n_lc = 0
            for slug, r in (lc.get("by_slug") or {}).items():
                tgt = by_slug.get(slug)
                if tgt and r.get("clicks"):
                    tgt["nl_clicks"] = r["clicks"]
                    tgt["nl_unique"] = r.get("unique") or 0
                    tgt["nl_sends"] = r.get("sends") or 0
                    n_lc += 1
            idx["link_click_sends"] = lc.get("sends_scanned")
            log(f"  piece index enriched: {len(seen_q)} pieces w/ search queries, "
                f"{sum(1 for p in pieces if p.get('signups'))} w/ attributed signups, "
                f"{n_lc} w/ newsletter clicks")
    except Exception as e:
        log(f"  piece index enrichment failed: {e}")

    # ---- Never let a missing credential erase good data -------------------
    # Every credential-gated section emits {"available": false, "reason": ...}
    # when its key is absent. Writing that over a previously good block turns a
    # LOCAL environment gap into published data loss: the GA4 keys live in
    # GitHub secrets, so a laptop run that skipped them silently blanked the
    # year-long traffic tiles for everyone until the next CI run repaired them.
    # A stub is "we could not look", never "there is nothing there", so the
    # last good block is carried forward and flagged stale instead.
    # The fallback source matters. Reading only the local plaintext file fails
    # the second time: once a run without credentials has written a stub there,
    # the next run finds a stub to carry forward and the good block is gone for
    # good. The PUBLISHED payload is the real last-known-good state, so fall
    # back to decrypting it whenever the local copy has nothing usable. That is
    # how search_console came to be blank on the live dashboard while this
    # guard was in place and doing exactly what it was written to do.
    def _published():
        enc = ROOT / "growth" / "data.enc"
        if not enc.exists():
            return {}
        try:
            import base64
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
            from cryptography.hazmat.primitives import hashes
            pw = os.environ.get("VC_NETWORK_PASS")
            if not pw:
                f = PRIV / ".netpass"
                pw = f.read_text().strip() if f.exists() else ""
            if not pw:
                return {}
            b = json.loads(enc.read_text())
            k = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                           salt=base64.b64decode(b["salt"]),
                           iterations=b["iters"]).derive(pw.encode())
            return json.loads(AESGCM(k).decrypt(base64.b64decode(b["iv"]),
                                                base64.b64decode(b["ct"]), None))
        except Exception as e:
            log(f"  carry-forward: could not read the published payload ({e})")
            return {}

    if OUT.exists():
        try:
            prev = json.loads(OUT.read_text())
        except Exception:
            prev = {}
        _pub = None
        for key, cur in list(out.items()):
            if isinstance(cur, dict) and cur.get("available") is False:
                good = prev.get(key)
                if not (isinstance(good, dict) and good.get("available")):
                    if _pub is None:
                        _pub = _published()
                    pub_block = _pub.get(key)
                    if isinstance(pub_block, dict) and pub_block.get("available"):
                        prev[key] = pub_block
                        log(f"  {key}: local copy was already blank — recovered from the published payload")
        for key, cur in list(out.items()):
            if not (isinstance(cur, dict) and cur.get("available") is False):
                continue
            old_block = prev.get(key)
            if isinstance(old_block, dict) and old_block.get("available"):
                carried = dict(old_block)
                carried["stale"] = True
                carried["stale_reason"] = cur.get("reason") or "credential unavailable this run"
                out[key] = carried
                log(f"  {key}: unavailable this run — carried forward the last good pull "
                    f"({carried['stale_reason']})")
    OUT.write_text(json.dumps(out, indent=2))
    size_kb = OUT.stat().st_size // 1024
    mc = out["mailchimp"]; gh = out["ghost"]
    log(f"wrote {OUT.name} ({size_kb} KB)")
    if mc.get("available"):
        log(f"  mailchimp: {mc.get('total_subscribers'):,} subs · {len(mc.get('daily_activity', []))} activity days · {len(mc.get('campaigns', []))} campaigns")
    if gh.get("available"):
        log(f"  ghost:     {gh.get('count_90')} posts in last 90d ({gh.get('count_7')} in 7d)")
    log(f"  press:     {len(out['press'])} items")


if __name__ == "__main__":
    main()
