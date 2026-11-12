"""Linga POS (api.lingaros.com) — daily sales summary for a store.

Same endpoint the Shifts "Close Day" feature uses. Keys come from environment variables named in
clients.json ("linga": {"store_env": "LINGA_STORE_ID", "key_env": "LINGA_API_KEY"}).
Preferred: transactionReport (one request for a whole date range, dateOption=DR) — one row per payment with
tender ("IDTECH" = card, "Cash"), amount, paymentTip, paymentDate "DD-Mon-YYYY hh:mm AM", status. The key
allows only 252 requests a month, so this is what the daily pull uses (about one request a day).
Fallback / cross-check: saleSummaryReport (the day's checks) + GET sale/<id> per check (payments in cents):
card authorized + tip for a business day equals the processor's deposit (verified to the penny for Oct 2026).
"""
import datetime as dt
import json
import os
from concurrent.futures import ThreadPoolExecutor

import requests

ROOT = "https://api.lingaros.com/v1/lingapos/"
BASE = ROOT + "store/%s/saleSummaryReport"


def keys(c):
    lc = c.get("linga") or {}
    store = os.environ.get(lc.get("store_env", "LINGA_STORE_ID"), "").strip().strip("'\"")
    key = os.environ.get(lc.get("key_env", "LINGA_API_KEY"), "").strip().strip("'\"")
    return (store, key) if store and key else (None, None)


class QuotaExceeded(Exception):
    """Linga's monthly request allowance for this API key is used up; `until` is when it resets."""
    def __init__(self, until):
        super().__init__("Linga API monthly limit reached until %s" % until.isoformat(timespec="minutes"))
        self.until = until


def _get(url, key, params=None, session=None, tries=4):
    """GET with a short backoff for the per-second limit. The monthly limit (252 requests on this key)
    raises QuotaExceeded right away instead of waiting."""
    import time
    get = (session or requests).get
    for attempt in range(tries):
        r = get(url, params=params, headers={"apikey": key, "accept": "application/json"}, timeout=60)
        if r.status_code != 429:
            r.raise_for_status()
            return r.json()
        wait = r.headers.get("Retry-After") or ""
        secs = float(wait) if wait.replace(".", "").isdigit() else 5.0
        if secs > 30 or r.headers.get("X-RateLimit-Remaining-Month") == "0":
            raise QuotaExceeded(dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=secs))
        time.sleep(secs)
    r.raise_for_status()


def fetch_day(store, key, day):
    """Raw JSON of Linga's sale summary for one business day."""
    d = day if isinstance(day, dt.date) else dt.date.fromisoformat(day)
    return _get(BASE % store, key, {"dateOption": "SD", "specificDate": d.strftime("%d-%b-%Y"),
                                    "type": "SALESUMMARY", "isIncludedCategory": "true"})


def outline(node, depth=0, max_depth=6, path="$"):
    """Structure of a payload without personal details: keys, types and numbers only (strings are hidden)."""
    lines = []
    pad = "  " * depth
    if depth > max_depth:
        return lines
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                lines.append("%s%s: %s" % (pad, k, "{…}" if isinstance(v, dict) else "[%d]" % len(v)))
                lines += outline(v, depth + 1, max_depth, path + "." + k)
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                lines.append("%s%s = %s" % (pad, k, v))
            else:
                lines.append("%s%s : %s" % (pad, k, type(v).__name__))
    elif isinstance(node, list) and node:
        lines.append("%s[0] of %d" % (pad, len(node)))
        lines += outline(node[0], depth + 1, max_depth, path + "[0]")
    return lines


def probe(c, day):
    store, key = keys(c)
    if not store:
        return "Linga Store ID / API key are not set for this client."
    d = dt.date.fromisoformat(day)
    rows = transactions(store, key, d, d)
    sample = {k: (v if k not in ("employeeName", "customerName") else "…") for k, v in (rows[0].items() if rows and isinstance(rows[0], dict) else [])}
    return json.dumps(dict(rows=len(rows), sample=sample, by_tender=_totals_from(rows, d)), indent=1, default=str)


def _totals_from(rows, d):
    tender = {}
    for r in rows:
        if isinstance(r, dict) and not r.get("isTotal"):
            t = str(r.get("tender") or "?")
            a = tender.setdefault(t, [0.0, 0.0, 0])
            a[0] += _num(r.get("amount")); a[1] += _num(r.get("paymentTip")); a[2] += 1
    return {t: dict(amount=round(a, 2), tips=round(b, 2), n=n) for t, (a, b, n) in tender.items()}


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("$", "") or 0)
    except ValueError:
        return 0.0


def _sale(key, sale_id, session):
    return _get(ROOT + "sale/" + sale_id, key, session=session)


def day_totals(store, key, day, workers=3):
    """One business day from Linga: card sales, card tips, cash sales, other payments, checks.
    Card amounts are what the processor deposits (authorized amount + tip); cash is net of change given."""
    rows = fetch_day(store, key, day)
    rows = rows if isinstance(rows, list) else []
    checks = [r for r in rows if not r.get("isTotal") and r.get("id")]
    total = next((r for r in rows if r.get("isTotal")), {})
    out = dict(card_sales=0.0, card_tips=0.0, cash_sales=0.0, cash_tips=0.0, other=0.0, checks=len(checks),
               gross=_num(total.get("grandSales")), methods={})
    s = requests.Session()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        sales = list(ex.map(lambda r: _sale(key, r["id"], s), checks))
    for d in sales:
        if d.get("isVoidCheck"):
            continue
        for p in d.get("payments") or []:
            if (p.get("transactionStatus") or "Success") not in ("Success", "SUCCESS", "Approved", ""):
                continue
            method = ("%s %s" % (p.get("paymentMethod") or "", p.get("paymentType") or "")).strip()
            amt = (p.get("authorizedAmount") or 0) / 100.0
            tip = (p.get("paymentTip") or 0) / 100.0
            change = (p.get("changeDue") or 0) / 100.0
            out["methods"][method] = round(out["methods"].get(method, 0) + amt, 2)
            m = method.lower()
            if "cash" in m:
                out["cash_sales"] += amt  # applied amount; change given back is on top of it
                out["cash_tips"] += tip
            elif any(x in m for x in ("cc", "card", "credit", "debit", "visa", "master", "amex", "discover", "idtech")):
                out["card_sales"] += amt
                out["card_tips"] += tip
            else:
                out["other"] += amt + tip
    for k in ("card_sales", "card_tips", "cash_sales", "cash_tips", "other"):
        out[k] = round(out[k], 2)
    return out


def _parse_payment_date(v):
    try:
        return dt.datetime.strptime(str(v).strip(), "%d-%b-%Y %I:%M %p")
    except ValueError:
        try:
            return dt.datetime.strptime(str(v).strip()[:11], "%d-%b-%Y")
        except ValueError:
            return None


def transactions(store, key, start, end):
    """Raw payment rows from transactionReport for start..end (dates)."""
    rows = _get(ROOT + "store/%s/transactionReport" % store, key,
                {"dateOption": "DR", "fromDate": start.strftime("%d-%b-%Y"), "toDate": end.strftime("%d-%b-%Y")})
    if isinstance(rows, dict):  # some reports wrap the list
        rows = next((v for v in rows.values() if isinstance(v, list)), [])
    return rows if isinstance(rows, list) else []


def range_totals(store, key, start, end):
    """{date: {card_sales, card_tips, cash_sales, cash_tips, other, payments}} for every day start..end."""
    out = {}
    d = start
    while d <= end:
        out[d.isoformat()] = dict(card_sales=0.0, card_tips=0.0, cash_sales=0.0, cash_tips=0.0, other=0.0, checks=0)
        d += dt.timedelta(days=1)
    seen = set()
    for r in transactions(store, key, start, end):
        if not isinstance(r, dict) or r.get("isTotal") is True or r.get("istotal") is True:
            continue
        if str(r.get("status") or "success").strip().lower() != "success":
            continue
        rid = str(r.get("id") or "")
        if rid and rid in seen:
            continue
        seen.add(rid)
        when = _parse_payment_date(r.get("paymentDate"))
        if not when or when.date().isoformat() not in out:
            continue
        x = out[when.date().isoformat()]
        amt, tip = _num(r.get("amount")), _num(r.get("paymentTip"))
        tender = str(r.get("tender") or r.get("paymentMethod") or "").strip().lower()
        if "cash" in tender:
            x["cash_sales"] += amt
            x["cash_tips"] += tip
        elif any(k in tender for k in ("idtech", "cc", "card", "credit", "debit", "visa", "master", "amex", "discover")):
            x["card_sales"] += amt
            x["card_tips"] += tip
        else:
            x["other"] += amt + tip
        x["checks"] += 1
    for x in out.values():
        for k in ("card_sales", "card_tips", "cash_sales", "cash_tips", "other"):
            x[k] = round(x[k], 2)
    return out
