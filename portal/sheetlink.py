"""Live balance and pending items straight from SheetLink's API (needs a SheetLink Max API key).

The workbook still supplies everything else (Checkbook, bills, reports). This only replaces the
bank figures that go stale when nobody has synced in Excel.
"""
import datetime as dt
import json
import urllib.request

from .snapshot import _live_pending

API = "https://api.sheetlink.app"


def _call(key, method, path, body=None):
    req = urllib.request.Request(
        API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": "unscatter-finance-portal"})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


def _bal(acc, k):
    b = acc.get("balances") or {}
    v = b.get(k, acc.get(k + "_balance"))
    return None if v is None else float(v)


def live_bank(key, account_id=None, mask=None, days=21):
    """Return currentBalance, availableBalance, pending, pendingTotal for one bank account."""
    items = _call(key, "GET", "/api/items").get("items") or []
    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    accounts, txns = [], []

    def has_ours(it):
        for a in it.get("accounts") or []:
            if (account_id and account_id in (a.get("account_id"), a.get("persistent_account_id"))) or \
                    (mask and str(a.get("mask") or "") == str(mask)):
                return True
        return False

    # Only sync the bank that holds this client's account (other clients' banks share the key; SheetLink rate-limits).
    items = [it for it in items if has_ours(it)] or items
    for it in items:
        res = _call(key, "POST", "/api/sync", {"item_id": it["item_id"], "start_date": start})
        accounts += res.get("accounts") or []
        txns += res.get("transactions") or []

    def pick():
        ids = lambda a: {a.get("account_id"), a.get("persistent_account_id")}
        if account_id:
            for a in accounts:
                if account_id in ids(a):
                    return a
        if mask:
            for a in accounts:
                if str(a.get("mask") or "") == str(mask):
                    return a
        return None

    acc = pick()
    if not acc:
        raise LookupError("SheetLink account not found (check account_mask in clients.json)")

    cur, avail = _bal(acc, "current"), _bal(acc, "available")
    h = ["transaction_id", "pending", "pending_transaction_id", "date", "amount",
         "merchant_name", "description_raw", "synced_at"]
    ix = {k: i for i, k in enumerate(h)}
    rows = [[t.get("transaction_id"), bool(t.get("pending")), t.get("pending_transaction_id"),
             t.get("date"), t.get("amount"), t.get("merchant_name"),
             t.get("description_raw") or t.get("name") or "", ""]
            for t in txns if t.get("account_id") == acc.get("account_id")]
    holds = round(avail - cur, 2) if cur is not None and avail is not None else None
    pending = sorted(_live_pending(rows, ix, holds), key=lambda p: p["date"])
    out = {
        "bankSource": "SheetLink (live)",
        "bankSyncedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "pending": pending,
        "pendingTotal": round(sum(p["amount"] for p in pending), 2),
    }
    if cur is not None:
        out["currentBalance"] = round(cur, 2)
    if avail is not None:
        out["availableBalance"] = round(avail, 2)
    return out
