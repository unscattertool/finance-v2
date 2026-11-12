"""Bank view and cleanup list for clients whose books are elsewhere (e.g. CRS in Zoho).

The bank comes from SheetLink into the same books.db tables as WWK (account, bank_txn), but these clients
don't categorize here. Instead every posted transaction since "cleanup_since" is on a cleanup list until
Unscatter marks it done (after categorizing it in the client's own books). Anyone with access to the client
can add a note saying what a transaction was.
"""
import datetime as dt

SCHEMA = """
CREATE TABLE IF NOT EXISTS bank_note (
  client TEXT NOT NULL, txn_id TEXT NOT NULL, note TEXT, noted_by TEXT, noted_at TEXT,
  done INTEGER NOT NULL DEFAULT 0, done_by TEXT, done_at TEXT, PRIMARY KEY (client, txn_id));
"""
RECENT_DAYS = 45
DEFAULT_CLEANUP_DAYS = 90


def ensure(db):
    db.executescript(SCHEMA)


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def cleanup_since(c, today=None):
    today = today or dt.date.today()
    try:
        return dt.date.fromisoformat(c.get("cleanup_since") or "")
    except ValueError:
        return today - dt.timedelta(days=DEFAULT_CLEANUP_DAYS)


def _desc(r):
    return r["merchant"] or (r["description"] or "").split(" / ")[0] or ""


def view(db, client, account_ids, since, today=None):
    today = today or dt.date.today()
    ids = list(account_ids or [])
    marks = ",".join("?" * len(ids)) or "''"
    accts = [dict(r) for r in db.execute(
        "SELECT * FROM account WHERE client=? AND account_id IN (%s) ORDER BY name" % marks, (client, *ids))]
    names = {a["account_id"]: "%s ••%s" % (a["name"] or "Account", a["mask"] or "") for a in accts}
    rows = db.execute(
        """SELECT b.*, n.note, n.noted_by, n.noted_at, n.done, n.done_by, n.done_at
           FROM bank_txn b LEFT JOIN bank_note n ON n.client=b.client AND n.txn_id=b.txn_id
           WHERE b.client=? AND b.removed=0 AND b.account_id IN (%s) AND b.date>=?
           ORDER BY b.date DESC, b.txn_id""" % marks,
        (client, *ids, min(since, today - dt.timedelta(days=RECENT_DAYS)).isoformat())).fetchall()

    def item(r):
        return {"id": r["txn_id"], "date": r["date"], "account": names.get(r["account_id"], ""),
                "description": _desc(r), "full": r["description"] or "", "check": r["check_number"] or "",
                "amount": r["amount"], "note": r["note"] or "", "notedBy": r["noted_by"] or "",
                "notedAt": r["noted_at"] or "", "done": bool(r["done"]), "doneBy": r["done_by"] or "",
                "doneAt": r["done_at"] or ""}
    pending = [item(r) for r in rows if r["pending"]]
    recent_from = (today - dt.timedelta(days=RECENT_DAYS)).isoformat()
    recent = [item(r) for r in rows if not r["pending"] and r["date"] >= recent_from]
    todo = [item(r) for r in rows if not r["pending"] and r["date"] >= since.isoformat() and not r["done"]]
    done = sorted((item(r) for r in rows if not r["pending"] and r["done"]),
                  key=lambda x: x["doneAt"], reverse=True)[:30]
    cur = sum(a["current"] or 0 for a in accts)
    avail = sum((a["available"] if a["available"] is not None else a["current"]) or 0 for a in accts)
    synced = max((a["synced_at"] or "" for a in accts), default="")
    return {
        "accounts": accts, "current": round(cur, 2), "available": round(avail, 2), "syncedAt": synced,
        "pending": pending, "pendingTotal": round(sum(p["amount"] for p in pending), 2),
        "recent": recent, "recentDays": RECENT_DAYS,
        "todo": todo, "todoNoted": sum(1 for t in todo if t["note"]), "done": done, "since": since.isoformat(),
    }


def save_note(db, client, txn_id, note, user):
    if not db.execute("SELECT 1 FROM bank_txn WHERE client=? AND txn_id=?", (client, txn_id)).fetchone():
        return False
    note = (note or "").strip()[:500]
    db.execute("INSERT OR IGNORE INTO bank_note(client, txn_id) VALUES(?,?)", (client, txn_id))
    db.execute("UPDATE bank_note SET note=?, noted_by=?, noted_at=? WHERE client=? AND txn_id=?",
               (note, user, _now(), client, txn_id))
    db.commit()
    return True


def set_done(db, client, txn_id, done, user):
    if not db.execute("SELECT 1 FROM bank_txn WHERE client=? AND txn_id=?", (client, txn_id)).fetchone():
        return False
    db.execute("INSERT OR IGNORE INTO bank_note(client, txn_id) VALUES(?,?)", (client, txn_id))
    db.execute("UPDATE bank_note SET done=?, done_by=?, done_at=? WHERE client=? AND txn_id=?",
               (1 if done else 0, user if done else None, _now() if done else None, client, txn_id))
    db.commit()
    return True
