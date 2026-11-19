"""The books: a private SQLite database (instance/books.db) holding every client's bank feed, register,
bills, recurring payments, categories and rules.

Signs: money in is positive, money out is negative (SheetLink/Plaid amounts are flipped on import).
Every bank transaction gets one register entry. A check or other payment typed in by hand is an entry
with no bank transaction until the bank shows it, then the two are linked (status "cleared").
"""
import datetime as dt
import os
import re
import sqlite3
import time

from .snapshot import PAYROLL_CATS, PERIOD_DAYS, _date, _num, _table

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS account (
  client TEXT NOT NULL, account_id TEXT NOT NULL, name TEXT, mask TEXT, institution TEXT,
  current REAL, available REAL, synced_at TEXT, PRIMARY KEY (client, account_id));
CREATE TABLE IF NOT EXISTS bank_txn (
  client TEXT NOT NULL, txn_id TEXT NOT NULL, account_id TEXT, date TEXT NOT NULL, amount REAL NOT NULL,
  description TEXT, merchant TEXT, check_number TEXT, pending INTEGER DEFAULT 0, pending_txn_id TEXT,
  removed INTEGER DEFAULT 0, synced_at TEXT, PRIMARY KEY (client, txn_id));
CREATE TABLE IF NOT EXISTS entry (
  id INTEGER PRIMARY KEY, client TEXT NOT NULL, date TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'bank',
  check_no TEXT, description TEXT, payee TEXT, category TEXT, amount REAL NOT NULL, memo TEXT,
  bank_txn_id TEXT, status TEXT NOT NULL DEFAULT 'cleared', bill_id INTEGER,
  created_by TEXT, created_at TEXT, updated_by TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS entry_client_date ON entry(client, date);
CREATE INDEX IF NOT EXISTS entry_txn ON entry(client, bank_txn_id);
CREATE TABLE IF NOT EXISTS bill (
  id INTEGER PRIMARY KEY, client TEXT NOT NULL, vendor TEXT NOT NULL, category TEXT, amount REAL NOT NULL,
  due_date TEXT, invoice_no TEXT, notes TEXT, status TEXT NOT NULL DEFAULT 'open', paid_date TEXT,
  entry_id INTEGER, created_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS recurring (
  id INTEGER PRIMARY KEY, client TEXT NOT NULL, name TEXT NOT NULL, payee TEXT, category TEXT,
  frequency TEXT, typical_amount REAL, autopay INTEGER DEFAULT 0, active INTEGER DEFAULT 1, notes TEXT);
CREATE TABLE IF NOT EXISTS category (
  client TEXT NOT NULL, name TEXT NOT NULL, pl_group TEXT, sort INTEGER, PRIMARY KEY (client, name));
CREATE TABLE IF NOT EXISTS rule (
  id INTEGER PRIMARY KEY, client TEXT NOT NULL, pattern TEXT NOT NULL, payee TEXT, category TEXT, sort INTEGER);
CREATE TABLE IF NOT EXISTS meta (client TEXT NOT NULL, k TEXT NOT NULL, v TEXT, PRIMARY KEY (client, k));
"""

FREQ_DAYS = {"weekly": 7, "biweekly": 14, "monthly": None, "quarterly": None, "yearly": None}
NEEDS = "Uncategorized"


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def connect(instance_path):
    path = os.path.join(instance_path, "books.db")
    new = not os.path.exists(path)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    if new:
        os.chmod(path, 0o600)
    return db


def get_meta(db, client, k, default=None):
    r = db.execute("SELECT v FROM meta WHERE client=? AND k=?", (client, k)).fetchone()
    return r["v"] if r else default


def set_meta(db, client, k, v):
    db.execute("INSERT INTO meta(client,k,v) VALUES(?,?,?) ON CONFLICT(client,k) DO UPDATE SET v=excluded.v", (client, k, v))


def has_books(db, client):
    return bool(db.execute("SELECT 1 FROM entry WHERE client=? LIMIT 1", (client,)).fetchone()
                or get_meta(db, client, "imported_at"))


# ------------------------------------------------------------------ categories & rules
def categories(db, client):
    return [dict(r) for r in db.execute("SELECT name, pl_group FROM category WHERE client=? ORDER BY sort, name", (client,))]


def apply_rules(db, client, text):
    """(payee, category) from the first rule whose pattern appears in the bank description."""
    t = (text or "").upper()
    for r in db.execute("SELECT pattern, payee, category FROM rule WHERE client=? ORDER BY sort, id", (client,)):
        if r["pattern"] and r["pattern"].upper() in t:
            return r["payee"], r["category"]
    return None, None


def add_rule(db, client, pattern, payee, category):
    pattern = pattern.strip()[:80]
    if not pattern:
        return
    db.execute("DELETE FROM rule WHERE client=? AND upper(pattern)=upper(?)", (client, pattern))
    db.execute("INSERT INTO rule(client,pattern,payee,category,sort) VALUES(?,?,?,?,0)", (client, pattern, payee, category))


def rule_pattern(description):
    """A reusable pattern from a bank description: the leading words, without dates/ids/numbers."""
    words = re.sub(r"[^A-Za-z&' /.-]", " ", description or "").upper().split()
    words = [w for w in words if len(w) > 1 and w not in ("POS", "PURCHASE", "DEBIT", "CARD", "ACH")]
    return " ".join(words[:3])


# ------------------------------------------------------------------ import from the old workbook
def import_workbook(db, client, xlsx_bytes, user="import"):
    """One-time move of a client's workbook into the database. Replaces that client's existing books."""
    import io
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True)
    ts = now()
    for t in ("entry", "bank_txn", "bill", "recurring", "category", "rule", "account"):
        db.execute("DELETE FROM %s WHERE client=?" % t, (client,))

    h, rows = _table(wb, "tblCategories")
    for i, r in enumerate(rows):
        if r[0]:
            db.execute("INSERT OR REPLACE INTO category VALUES(?,?,?,?)", (client, str(r[0]).strip(), str(r[1] or "Needs Category"), i))
    for name, grp in ((NEEDS, "Needs Category"),):
        db.execute("INSERT OR IGNORE INTO category VALUES(?,?,?,?)", (client, name, grp, 999))

    h, rows = _table(wb, "tblRules")
    ix = {k: i for i, k in enumerate(h)}
    for i, r in enumerate(rows):
        if r[ix["Pattern"]]:
            db.execute("INSERT INTO rule(client,pattern,payee,category,sort) VALUES(?,?,?,?,?)",
                       (client, str(r[ix["Pattern"]]).strip(), r[ix["Payee"]], r[ix["Category"]], 100 + i))

    # bank feed rows (posted and pending)
    h, rows = _table(wb, "tbltransactions")
    ix = {k: i for i, k in enumerate(h)}
    for r in rows:
        tid = r[ix["transaction_id"]]
        d = _date(r[ix["date"]])
        if not tid or not d:
            continue
        pend = r[ix["pending"]] in (True, "TRUE", "True", 1)
        chk = r[ix["check_number"]]
        db.execute("INSERT OR REPLACE INTO bank_txn VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
            client, tid, r[ix["account_id"]], d.isoformat(), round(-_num(r[ix["amount"]]), 2),
            r[ix["description_raw"]], r[ix["merchant_name"]], str(int(chk)) if isinstance(chk, (int, float)) and chk else (chk or None),
            int(pend), r[ix["pending_transaction_id"]], 0, str(r[ix["synced_at"]] or "")))

    # balances
    acc = wb["Accounts"]
    head = [c.value for c in acc[1]]
    for row in acc.iter_rows(min_row=2, values_only=True):
        a = dict(zip(head, row))
        if a.get("account_id"):
            db.execute("INSERT OR REPLACE INTO account VALUES(?,?,?,?,?,?,?,?)", (
                client, a["account_id"], a.get("name"), str(a.get("mask") or ""), a.get("institution"),
                _num(a.get("current_balance")), _num(a.get("available_balance")), str(a.get("last_synced_at") or "")))

    # register (Checkbook)
    h, rows = _table(wb, "tblRegister")
    ix = {k: i for i, k in enumerate(h)}
    for r in rows:
        d = _date(r[ix["Date"]])
        if not d:
            continue
        tid = r[ix["Transaction ID"]]
        if tid and not db.execute("SELECT 1 FROM bank_txn WHERE client=? AND txn_id=?", (client, tid)).fetchone():
            tid = None  # "Before feed" rows carry a made-up id
        chk = r[ix["Check #"]]
        chk = str(int(chk)) if isinstance(chk, (int, float)) and chk else (str(chk) if chk else None)
        notes = " ".join(str(x) for x in (r[ix["Memo"]], r[ix["Notes"]]) if x and "Pending from live bank feed" not in str(x))
        kind = "check" if chk else ("bank" if tid else "manual")
        status = "cleared" if tid or r[ix["Cleared"]] == "Yes" else "open"
        db.execute("""INSERT INTO entry(client,date,kind,check_no,description,payee,category,amount,memo,bank_txn_id,status,
                      created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (client, d.isoformat(), kind, chk, r[ix["Description"]], r[ix["Payee"]], r[ix["Category"]] or NEEDS,
                    round(_num(r[ix["Net Amount"]]), 2), notes or None, tid, status, user, ts))
    # bank rows the register didn't have yet
    for t in db.execute("""SELECT * FROM bank_txn b WHERE client=? AND NOT EXISTS
                           (SELECT 1 FROM entry e WHERE e.client=b.client AND e.bank_txn_id=b.txn_id)""", (client,)).fetchall():
        _new_entry_from_txn(db, client, t, user)

    h, rows = _table(wb, "tblBills")
    ix = {k: i for i, k in enumerate(h)}
    for r in rows:
        if not r[ix["Vendor"]] or r[ix["Amount"]] is None:
            continue
        paid = (r[ix["Status"]] or "") == "Paid"
        due, pd = _date(r[ix["Due Date"]]), _date(r[ix["Paid Date"]])
        db.execute("""INSERT INTO bill(client,vendor,category,amount,due_date,invoice_no,notes,status,paid_date,created_by,created_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                   (client, r[ix["Vendor"]], r[ix["Category"]], abs(_num(r[ix["Amount"]])), due and due.isoformat(),
                    r[ix["Invoice #"]], r[ix["Notes"]], "paid" if paid else "open", pd and pd.isoformat(), user, ts))

    h, rows = _table(wb, "tblRecurring")
    ix = {k: i for i, k in enumerate(h)}
    for r in rows:
        if not r[ix["Name"]]:
            continue
        db.execute("""INSERT INTO recurring(client,name,payee,category,frequency,typical_amount,autopay,active,notes)
                      VALUES(?,?,?,?,?,?,?,1,?)""",
                   (client, r[ix["Name"]], r[ix["Payee"]], r[ix["Category"]], str(r[ix["Frequency"]] or "Monthly").lower(),
                    _num(r[ix["Typical Amount"]]) or None, int(r[ix["Autopay"]] == "Yes"), r[ix["Notes"]]))
    set_meta(db, client, "imported_at", ts)
    db.commit()
    return {k: db.execute("SELECT count(*) FROM %s WHERE client=?" % k, (client,)).fetchone()[0]
            for k in ("entry", "bank_txn", "bill", "recurring", "category", "rule")}


# ------------------------------------------------------------------ bank sync
PROCESSOR_PATTERNS = ("HRTLAND PMT SYS", "HEARTLAND PAYMENT", "HEARTLAND PMT")


def processor_category(description, amount):
    """Card processor ACH items: money in = card sales deposit (batch), money out = processing fees."""
    d = (description or "").upper()
    if any(p in d for p in PROCESSOR_PATTERNS):
        return ("Heartland", "Sales - Card") if amount > 0 else ("Heartland", "Merchant Processing Fees")
    return None, None


def _new_entry_from_txn(db, client, t, user="bank"):
    payee, cat = apply_rules(db, client, t["description"])
    if not cat or cat == NEEDS:
        p2, c2 = processor_category(t["description"], t["amount"])
        if c2:
            payee, cat = payee or p2, c2
    db.execute("""INSERT INTO entry(client,date,kind,check_no,description,payee,category,amount,bank_txn_id,status,created_by,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,'cleared',?,?)""",
               (client, t["date"], "check" if t["check_number"] else "bank", t["check_number"], t["description"],
                payee or t["merchant"] or None, cat or NEEDS, t["amount"], t["txn_id"], user, now()))
    eid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    _match_bill(db, client, eid)
    return eid


def _match_open_entry(db, client, t):
    """A hand-entered check/payment waiting for the bank. Check number wins; else same amount, written before."""
    open_ = db.execute("""SELECT * FROM entry WHERE client=? AND bank_txn_id IS NULL AND status='open'
                          AND abs(amount - ?) < 0.005 ORDER BY date""", (client, t["amount"])).fetchall()
    if t["check_number"]:
        for e in open_:
            if e["check_no"] and e["check_no"].lstrip("0") == str(t["check_number"]).lstrip("0"):
                return e
    td = dt.date.fromisoformat(t["date"])
    for e in open_:
        age = (td - dt.date.fromisoformat(e["date"])).days
        if 0 <= age <= 120 and (not e["check_no"] or not t["check_number"]):
            return e
    return None


def _match_bill(db, client, eid):
    e = db.execute("SELECT * FROM entry WHERE id=?", (eid,)).fetchone()
    if e["amount"] >= 0 or e["bill_id"]:
        return
    d = dt.date.fromisoformat(e["date"])
    text = " ".join(str(x or "") for x in (e["payee"], e["description"])).upper()
    for b in db.execute("SELECT * FROM bill WHERE client=? AND status='open' AND abs(amount - ?) < 0.005", (client, -e["amount"])):
        due = _date(b["due_date"]) or d
        word = (b["vendor"] or "").upper().split()
        if -30 <= (d - due).days <= 20 and (not word or word[0] in text):
            db.execute("UPDATE bill SET status='paid', paid_date=?, entry_id=? WHERE id=?", (e["date"], eid, b["id"]))
            db.execute("UPDATE entry SET bill_id=? WHERE id=?", (b["id"], eid))
            return


def _ensure_prev_col(db):
    if "prev_bank_txn_id" not in [r[1] for r in db.execute("PRAGMA table_info(entry)")]:
        db.execute("ALTER TABLE entry ADD COLUMN prev_bank_txn_id TEXT")


def save_bank_data(db, client, accounts, txns, window_start, account_ids):
    """Store a SheetLink sync result for the client's chosen accounts and update the register."""
    _ensure_prev_col(db)
    ts = now()
    keep = set(account_ids)
    for a in accounts:
        if a.get("account_id") not in keep:
            continue
        b = a.get("balances") or {}
        db.execute("INSERT OR REPLACE INTO account VALUES(?,?,?,?,?,?,?,?)", (
            client, a["account_id"], a.get("name"), str(a.get("mask") or ""), a.get("institution") or a.get("institution_name"),
            b.get("current", a.get("current_balance")), b.get("available", a.get("available_balance")), ts))
    seen, new = set(), []
    for t in txns:
        if t.get("account_id") not in keep or not t.get("transaction_id"):
            continue
        tid = t["transaction_id"]
        seen.add(tid)
        old = db.execute("SELECT * FROM bank_txn WHERE client=? AND txn_id=?", (client, tid)).fetchone()
        chk = t.get("check_number")
        row = (client, tid, t["account_id"], str(t.get("date"))[:10], round(-float(t.get("amount") or 0), 2),
               t.get("description_raw") or t.get("name"), t.get("merchant_name"), str(chk) if chk else None,
               int(bool(t.get("pending"))), t.get("pending_transaction_id"), 0, ts)
        db.execute("INSERT OR REPLACE INTO bank_txn VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", row)
        if not old:
            new.append(tid)
        elif old["amount"] != row[4] or old["date"] != row[3]:
            db.execute("UPDATE entry SET amount=?, date=CASE WHEN kind='bank' THEN ? ELSE date END WHERE client=? AND bank_txn_id=?",
                       (row[4], row[3], client, tid))
    # pending items that disappeared from the bank (posted under a new id, or dropped)
    gone = [r["txn_id"] for r in db.execute(
        "SELECT txn_id FROM bank_txn WHERE client=? AND pending=1 AND removed=0 AND date>=? AND account_id IN (%s)"
        % ",".join("?" * len(keep)), (client, window_start, *keep)) if r["txn_id"] not in seen]
    for tid in gone:
        db.execute("UPDATE bank_txn SET removed=1 WHERE client=? AND txn_id=?", (client, tid))

    order = {t.get("transaction_id"): str(t.get("date")) for t in txns}
    for tid in sorted(new, key=lambda x: order.get(x, "")):
        t = db.execute("SELECT * FROM bank_txn WHERE client=? AND txn_id=?", (client, tid)).fetchone()
        # posted version of a pending item we already have: move its entry (keeps any category edits)
        prev = None
        if t["pending_txn_id"]:
            prev = db.execute("SELECT * FROM entry WHERE client=? AND bank_txn_id=?", (client, t["pending_txn_id"])).fetchone()
            if not prev:  # the pending item left the feed a sync earlier and its (edited) entry was set aside
                prev = db.execute("SELECT * FROM entry WHERE client=? AND prev_bank_txn_id=? AND bank_txn_id IS NULL AND status='open'",
                                  (client, t["pending_txn_id"])).fetchone()
                if prev:
                    db.execute("UPDATE entry SET status='cleared' WHERE id=?", (prev["id"],))
        if not prev and not t["pending"]:
            for g in gone:
                gt = db.execute("SELECT * FROM bank_txn WHERE client=? AND txn_id=?", (client, g)).fetchone()
                if gt and abs(gt["amount"] - t["amount"]) < 0.005 and 0 <= (dt.date.fromisoformat(t["date"]) - dt.date.fromisoformat(gt["date"])).days <= 7:
                    prev = db.execute("SELECT * FROM entry WHERE client=? AND bank_txn_id=?", (client, g)).fetchone()
                    if prev:
                        gone.remove(g)
                        break
        if not prev and not t["pending"]:
            # same posted item stored earlier under another id (e.g. imported from the workbook)
            for o in db.execute("""SELECT txn_id FROM bank_txn WHERE client=? AND txn_id!=? AND pending=0 AND removed=0
                                   AND abs(amount-?)<0.005 AND date=? AND coalesce(description,'')=coalesce(?,'')""",
                                (client, tid, t["amount"], t["date"], t["description"])).fetchall():
                if o["txn_id"] in seen:
                    continue
                prev = db.execute("SELECT * FROM entry WHERE client=? AND bank_txn_id=?", (client, o["txn_id"])).fetchone()
                if prev:
                    db.execute("UPDATE bank_txn SET removed=1 WHERE client=? AND txn_id=?", (client, o["txn_id"]))
                    seen.add(o["txn_id"])
                    break
        if prev:
            db.execute("UPDATE entry SET bank_txn_id=?, date=CASE WHEN kind='bank' THEN ? ELSE date END, amount=?, description=? WHERE id=?",
                       (tid, t["date"], t["amount"], t["description"], prev["id"]))
            continue
        e = _match_open_entry(db, client, t)
        if e:
            db.execute("UPDATE entry SET bank_txn_id=?, status='cleared', updated_at=? WHERE id=?", (tid, ts, e["id"]))
            continue
        _new_entry_from_txn(db, client, t)
    # entries still pointing at a pending item that vanished: the charge was dropped, so drop the entry
    for tid in gone:
        db.execute("DELETE FROM entry WHERE client=? AND bank_txn_id=? AND kind='bank' AND updated_by IS NULL", (client, tid))
        db.execute("UPDATE entry SET prev_bank_txn_id=?, bank_txn_id=NULL, status='open' WHERE client=? AND bank_txn_id=?", (tid, client, tid))
    merged = merge_posted_pending(db, client)
    set_meta(db, client, "bank_synced_at", ts)
    db.commit()
    return {"new": len(new), "removed": len(gone), "merged": len(merged)}


_DESC_STOP = {"POS", "PURCHASE", "DEBIT", "CARD", "ACH", "TRAN", "DATE", "TIME", "VIEW", "THE", "WITHDRAWAL", "PAYMENT", "ONLINE"}


def _desc_tokens(text):
    return {w for w in re.findall(r"[A-Z][A-Z0-9_*]{3,}", (text or "").upper().replace("*", " ")) if w not in _DESC_STOP}


def _untouched(db, e):
    if e["updated_by"] or e["bill_id"]:
        return False
    for q in ("SELECT 1 FROM entry_split WHERE entry_id=? AND coalesce(source,'') NOT IN ('sales-tax','tax-payment')", "SELECT 1 FROM deposit_match WHERE entry_id=?"):
        try:
            if db.execute(q, (e["id"],)).fetchone():
                return False
        except sqlite3.OperationalError:
            pass
    return True


def merge_posted_pending(db, client, dry_run=False):
    """A pending bank item that later posted under a new id without the bank linking the two shows up twice (e.g. a
    Google Workspace charge pending as "WORKSPACE_WAYW…" and posted as "POS PURCHASE GOOGLE *Workspace…"). Pair each
    pending item with a posted one in the same account: same amount, posted 0–10 days after, and the same payee or a
    shared word in the description. The entry with your edits (category, tip split, deposit match) is kept on the posted
    item; the other copy is removed and the pending item retired."""
    out = []
    used = set()
    pend = db.execute("""SELECT * FROM bank_txn WHERE client=? AND pending=1 AND removed=0 ORDER BY date""", (client,)).fetchall()
    for p in pend:
        ep = db.execute("SELECT * FROM entry WHERE client=? AND bank_txn_id=?", (client, p["txn_id"])).fetchone()
        cands = db.execute("""SELECT b.* FROM bank_txn b WHERE b.client=? AND b.account_id=? AND b.pending=0 AND b.removed=0
                              AND abs(b.amount-?)<0.005 AND b.date>=date(?, '-2 day') AND b.date<=date(?, '+10 day') ORDER BY b.date""",
                           (client, p["account_id"], p["amount"], p["date"], p["date"])).fetchall()
        for q in cands:
            if q["txn_id"] in used:
                continue
            # already the pending item's own posted version (linked by the bank) → handled at import
            eq = db.execute("SELECT * FROM entry WHERE client=? AND bank_txn_id=?", (client, q["txn_id"])).fetchone()
            same_payee = ep is not None and eq is not None and (ep["payee"] or "").strip() and (ep["payee"] or "").upper() == (eq["payee"] or "").upper()
            shared = _desc_tokens((p["description"] or "") + " " + (p["merchant"] or "")) & _desc_tokens((q["description"] or "") + " " + (q["merchant"] or ""))
            if not (same_payee or shared):
                continue
            used.add(q["txn_id"])
            action = "retire pending"
            if ep is not None and eq is not None:
                if _untouched(db, eq):
                    action = "keep pending entry, move it to posted item"
                    if not dry_run:
                        db.execute("DELETE FROM entry WHERE id=?", (eq["id"],))
                        db.execute("UPDATE entry SET bank_txn_id=?, date=CASE WHEN kind='bank' THEN ? ELSE date END, amount=?, description=? WHERE id=?",
                                   (q["txn_id"], q["date"], q["amount"], q["description"], ep["id"]))
                elif _untouched(db, ep):
                    action = "drop pending copy"
                    if not dry_run:
                        db.execute("DELETE FROM entry WHERE id=?", (ep["id"],))
                else:
                    out.append(dict(pending=dict(p), posted=dict(q), action="both edited: left for review"))
                    break
            elif ep is not None and eq is None:
                action = "move pending entry to posted item"
                if not dry_run:
                    db.execute("UPDATE entry SET bank_txn_id=?, date=CASE WHEN kind='bank' THEN ? ELSE date END, amount=?, description=? WHERE id=?",
                               (q["txn_id"], q["date"], q["amount"], q["description"], ep["id"]))
            if not dry_run:
                db.execute("UPDATE bank_txn SET removed=1 WHERE client=? AND txn_id=?", (client, p["txn_id"]))
            out.append(dict(pending=dict(p), posted=dict(q), action=action))
            break
    # entries set aside when their pending item left the feed, whose posted copy came in as a new entry
    _ensure_prev_col(db)
    for ep in db.execute("""SELECT * FROM entry WHERE client=? AND kind='bank' AND status='open' AND bank_txn_id IS NULL""", (client,)).fetchall():
        for eq in db.execute("""SELECT e.* FROM entry e JOIN bank_txn b ON b.client=e.client AND b.txn_id=e.bank_txn_id
                                WHERE e.client=? AND b.pending=0 AND b.removed=0 AND abs(e.amount-?)<0.005
                                AND e.date>=date(?, '-2 day') AND e.date<=date(?, '+10 day') ORDER BY e.date""",
                             (client, ep["amount"], ep["date"], ep["date"])).fetchall():
            if eq["bank_txn_id"] in used:
                continue
            q = db.execute("SELECT * FROM bank_txn WHERE client=? AND txn_id=?", (client, eq["bank_txn_id"])).fetchone()
            linked = ep["prev_bank_txn_id"] and q["pending_txn_id"] == ep["prev_bank_txn_id"]
            same_payee = (ep["payee"] or "").strip() and (ep["payee"] or "").upper() == (eq["payee"] or "").upper()
            shared = _desc_tokens(ep["description"]) & _desc_tokens((q["description"] or "") + " " + (q["merchant"] or ""))
            if not (linked or same_payee or shared):
                continue
            if not _untouched(db, eq):
                if _untouched(db, ep):
                    if not dry_run:
                        db.execute("DELETE FROM entry WHERE id=?", (ep["id"],))
                    used.add(eq["bank_txn_id"])
                    out.append(dict(pending=dict(ep), posted=dict(q), action="drop set-aside copy"))
                else:
                    out.append(dict(pending=dict(ep), posted=dict(q), action="both edited: left for review"))
                break
            used.add(eq["bank_txn_id"])
            if not dry_run:
                db.execute("DELETE FROM entry WHERE id=?", (eq["id"],))
                db.execute("""UPDATE entry SET bank_txn_id=?, status='cleared', date=?, amount=?, description=? WHERE id=?""",
                           (q["txn_id"], q["date"], q["amount"], q["description"], ep["id"]))
            out.append(dict(pending=dict(ep), posted=dict(q), action="keep your edited entry, attach it to the posted item"))
            break
    if not dry_run:
        db.commit()
    return out


def sync_sheetlink(db, client, key, account_ids, days=30):
    """Pull the client's accounts from SheetLink (only the bank connections that hold them)."""
    from .sheetlink import _call
    items = _call(key, "GET", "/api/items").get("items") or []
    keep = set(account_ids)
    mine = [it for it in items if any(a.get("account_id") in keep or a.get("persistent_account_id") in keep
                                      for a in it.get("accounts") or [])]
    if not mine:
        raise LookupError("None of this client's bank accounts are in SheetLink (check the client's bank settings).")
    last = db.execute("SELECT max(date) d FROM bank_txn WHERE client=? AND pending=0", (client,)).fetchone()["d"]
    full = not last or not get_meta(db, client, "full_synced")  # first sync after an import re-reads everything
    start = (dt.date.today() - dt.timedelta(days=730) if full
             else min(dt.date.fromisoformat(last), dt.date.today()) - dt.timedelta(days=days)).isoformat()
    accounts, txns = [], []
    for i, it in enumerate(mine):
        if i:
            time.sleep(2)
        res = _call(key, "POST", "/api/sync", {"item_id": it["item_id"], "start_date": start})
        for a in res.get("accounts") or []:
            a.setdefault("institution", it.get("institution_name"))
        accounts += res.get("accounts") or []
        txns += res.get("transactions") or []
    # map persistent ids to the ids the feed uses
    ids = {a["account_id"] for a in accounts if a.get("account_id") in keep or a.get("persistent_account_id") in keep}
    res = save_bank_data(db, client, accounts, txns, start, ids)
    if full:
        set_meta(db, client, "full_synced", now())
        db.commit()
    return res


def sheetlink_accounts(key):
    """All accounts visible to a SheetLink key (for the client settings page). No bank sync."""
    from .sheetlink import _call
    out = []
    for it in _call(key, "GET", "/api/items").get("items") or []:
        for a in it.get("accounts") or []:
            out.append({"account_id": a.get("account_id"), "name": a.get("name"), "mask": a.get("mask"),
                        "type": a.get("subtype") or a.get("type"), "institution": it.get("institution_name")})
    return out


# ------------------------------------------------------------------ manual entries
def add_check(db, client, user, date, amount, payee, category, check_no=None, memo=None, bill_id=None, kind="check"):
    ts = now()
    db.execute("""INSERT INTO entry(client,date,kind,check_no,description,payee,category,amount,memo,status,bill_id,created_by,created_at)
                  VALUES(?,?,?,?,?,?,?,?,?,'open',?,?,?)""",
               (client, date, kind, check_no or None, ("Check %s" % check_no) if check_no else (memo or payee),
                payee, category or NEEDS, -abs(amount), memo, bill_id, user, ts))
    eid = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    # the bank may already show it (entered after it cleared)
    for t in db.execute("""SELECT b.*, e.id eid FROM bank_txn b JOIN entry e ON e.client=b.client AND e.bank_txn_id=b.txn_id
                           WHERE b.client=? AND b.removed=0 AND abs(b.amount + ?) < 0.005 AND b.date>=? AND e.updated_by IS NULL
                           AND e.id!=? ORDER BY b.date""", (client, abs(amount), date, eid)):
        same_no = check_no and t["check_number"] and t["check_number"].lstrip("0") == str(check_no).lstrip("0")
        bare_check = not t["check_number"] and (t["description"] or "").upper().startswith("CHECK")
        if not (same_no or (bare_check and kind == "check")):
            continue
        # replace the bare bank row with the hand-entered details
        db.execute("DELETE FROM entry WHERE id=?", (t["eid"],))
        db.execute("UPDATE entry SET bank_txn_id=?, status='cleared' WHERE id=?", (t["txn_id"], eid))
        break
    db.commit()
    return eid


def add_bill(db, client, user, vendor, amount, due_date, category=None, invoice_no=None, notes=None):
    db.execute("""INSERT INTO bill(client,vendor,category,amount,due_date,invoice_no,notes,status,created_by,created_at)
                  VALUES(?,?,?,?,?,?,?,'open',?,?)""", (client, vendor, category, abs(amount), due_date, invoice_no, notes, user, now()))
    db.commit()
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


def pay_bill(db, client, user, bill_id, paid_date, method, check_no=None):
    b = db.execute("SELECT * FROM bill WHERE client=? AND id=?", (client, bill_id)).fetchone()
    if not b or b["status"] != "open":
        return None
    eid = None
    if method in ("check", "other"):
        eid = add_check(db, client, user, paid_date, b["amount"], b["vendor"], b["category"], check_no if method == "check" else None,
                        memo=("Inv %s" % b["invoice_no"]) if b["invoice_no"] else None, bill_id=bill_id,
                        kind="check" if method == "check" else "manual")
    db.execute("UPDATE bill SET status='paid', paid_date=?, entry_id=? WHERE id=?", (paid_date, eid, bill_id))
    db.commit()
    return eid


def update_entry(db, client, user, eid, **f):
    allowed = {k: v for k, v in f.items() if k in ("payee", "category", "memo", "check_no", "date", "amount")}
    e = db.execute("SELECT * FROM entry WHERE client=? AND id=?", (client, eid)).fetchone()
    if not e:
        return False
    if e["bank_txn_id"]:  # the bank decides date and amount once it has the item
        allowed.pop("date", None)
        allowed.pop("amount", None)
    if allowed:
        sets = ", ".join("%s=?" % k for k in allowed)
        db.execute("UPDATE entry SET %s, updated_by=?, updated_at=? WHERE id=?" % sets, (*allowed.values(), user, now(), eid))
    db.commit()
    return True


def update_bill(db, client, bid, vendor, amount, due_date, category, invoice_no, notes):
    db.execute("""UPDATE bill SET vendor=?, amount=?, due_date=?, category=?, invoice_no=?, notes=? WHERE client=? AND id=? AND status='open'""",
               (vendor, amount, due_date, category or None, invoice_no or None, notes or None, client, bid))
    db.commit()


def update_recurring(db, client, rid, **f):
    allowed = {k: v for k, v in f.items() if k in ("name", "payee", "category", "frequency", "typical_amount", "autopay", "active", "notes")}
    if allowed:
        db.execute("UPDATE recurring SET %s WHERE client=? AND id=?" % ", ".join("%s=?" % k for k in allowed), (*allowed.values(), client, rid))
        db.commit()


def void_entry(db, client, user, eid):
    """Only hand-entered items that the bank hasn't matched can be voided."""
    e = db.execute("SELECT * FROM entry WHERE client=? AND id=?", (client, eid)).fetchone()
    if not e or e["bank_txn_id"]:
        return False
    db.execute("UPDATE entry SET status='void', updated_by=?, updated_at=? WHERE id=?", (user, now(), eid))
    if e["bill_id"]:
        db.execute("UPDATE bill SET status='open', paid_date=NULL, entry_id=NULL WHERE id=?", (e["bill_id"],))
    db.commit()
    return True


# ------------------------------------------------------------------ dashboard snapshot (same shape as the workbook one)
def _add_months(d, n):
    import calendar
    y, m = divmod(d.month - 1 + n, 12)
    y += d.year
    return d.replace(year=y, month=m + 1, day=min(d.day, calendar.monthrange(y, m + 1)[1]))


def _next_due(last, freq):
    f = (freq or "monthly").lower()
    if f in ("weekly", "biweekly"):
        return last + dt.timedelta(days=FREQ_DAYS[f])
    return _add_months(last, {"quarterly": 3, "yearly": 12, "annual": 12}.get(f, 1))


# ------------------------------------------------------------------ recurring payments: which register items are one
_STOP = {"THE", "INC", "LLC", "CO", "CORP", "COMPANY", "SERVICE", "SERVICES", "FEE", "FEES", "PAYMENT", "PAYMENTS", "PMT", "BILL",
         "MONTHLY", "PAYROLL", "NET", "PAY", "TAXES", "TAX", "ACH", "DEBIT", "ONLINE", "WWW", "COM", "AUTOPAY", "RECURRING"}


def _tokens(text):
    return {w for w in re.findall(r"[A-Z0-9]{3,}", (text or "").upper()) if w not in _STOP and not w.isdigit()}


def recurring_amount(r, hits):
    """What the next payment will probably be: the set-up amount unless the last payment moved away from it (more than 2% and $1)."""
    last = -hits[-1]["amount"] if hits else 0.0
    typ = r["typical_amount"]
    if typ and (not last or abs(last - typ) <= max(1.0, 0.02 * typ)):
        return typ
    return last or typ or 0.0


PERIOD_DAYS_FREQ = {"weekly": 7, "biweekly": 14, "monthly": 30.4, "quarterly": 91, "annual": 365}


def recurring_forecast(r, hits, today=None):
    """(amount per period, averaged?) for the forecast. Vendors billed several times a period or by very different amounts
    (US Foods: several drafts a week, $75–$3,700 each) are projected at their average spend per period over the last
    8 weeks / 6 pay periods / 3 months (payments only, refunds left out); steady bills use the usual amount."""
    today = today or dt.date.today()
    freq = r["frequency"] or "monthly"
    per = PERIOD_DAYS_FREQ.get(freq, 30.4)
    n = 8 if freq == "weekly" else 6 if freq == "biweekly" else 3 if freq == "monthly" else 2
    window = per * n
    since = (today - dt.timedelta(days=window)).isoformat()
    pays = [-h["amount"] for h in hits if h["amount"] < 0 and since <= h["date"] <= today.isoformat()]
    if len(pays) >= 2:
        first = dt.date.fromisoformat(min(h["date"] for h in hits if h["amount"] < 0))
        covered = max(1.0, min(n, ((today - first).days + 1) / per))
        mean = sum(pays) / len(pays)
        sd = (sum((x - mean) ** 2 for x in pays) / len(pays)) ** 0.5
        if len(pays) > covered * 1.25 or (len(pays) >= 3 and mean and sd / mean > 0.35):
            return round(sum(pays) / covered, 2), True
    return recurring_amount(r, hits), False


def recurring_hits(r, entries):
    """Register payments that belong to recurring item `r`, oldest first. Not just an exact payee match:
      - same payee (any case), and the same category when one is set; or
      - the item's key words (from its payee and name, e.g. GUSTO) appear in the payee or bank description, and either the
        category matches or the amount is close to what it usually costs (within 40%)."""
    payee = (r["payee"] or "").strip().upper()
    cat = r["category"]
    keys = _tokens(r["payee"]) or _tokens(r["name"])
    out = []
    strong = []
    # a payment not categorized yet (e.g. still pending at the bank) still counts for its payee — otherwise a recurring
    # bill shows "late" while its payment is already pending
    uncat = lambda e: (e.get("category") or NEEDS) in (NEEDS, "Uncategorized", "Needs Category")
    for e in entries:
        if e["amount"] >= 0:
            continue
        if payee and (e["payee"] or "").strip().upper() == payee and (not cat or e["category"] == cat or uncat(e)):
            strong.append(e)
    ref = r["typical_amount"] or (sorted(-e["amount"] for e in strong[-6:])[len(strong[-6:]) // 2] if strong else None)
    strong_ids = {id(e) for e in strong}
    for e in entries:
        if e["amount"] >= 0:
            continue
        if id(e) in strong_ids:
            out.append(e)
            continue
        if not keys:
            continue
        text = _tokens((e.get("payee") or "") + " " + (e.get("description") or ""))
        if not keys & text:
            continue
        if cat and e["category"] == cat:
            out.append(e)
        elif (not cat or uncat(e)) and ref and abs(-e["amount"] - ref) <= 0.4 * ref:
            out.append(e)
    out.sort(key=lambda e: (e["date"], e.get("id") or 0))
    return out


def snapshot(db, client, account_ids=None, days_ahead=14, today=None):
    today = today or dt.date.today()
    horizon = today + dt.timedelta(days=days_ahead)
    accs = [dict(r) for r in db.execute("SELECT * FROM account WHERE client=?", (client,))]
    if account_ids:
        accs = [a for a in accs if a["account_id"] in set(account_ids)] or accs
    acc_ids = [a["account_id"] for a in accs]
    cur = sum(a["current"] or 0 for a in accs)
    avail = sum((a["available"] if a["available"] is not None else a["current"]) or 0 for a in accs)
    pending = [{"date": r["date"], "description": r["merchant"] or (r["description"] or "").split(" / ")[0], "amount": r["amount"]}
               for r in db.execute("SELECT * FROM bank_txn WHERE client=? AND pending=1 AND removed=0 ORDER BY date", (client,))
               if not acc_ids or r["account_id"] in acc_ids]

    entries = [dict(r) for r in db.execute("SELECT * FROM entry WHERE client=? AND status!='void' ORDER BY date, id", (client,))]

    # bills: open bills (overdue or due within the horizon) + recurring payments expected
    bills = []
    for b in db.execute("SELECT * FROM bill WHERE client=? AND status='open' ORDER BY due_date", (client,)):
        due = _date(b["due_date"])
        if due and due > horizon:
            continue
        days = (due - today).days if due else None
        status = "Overdue" if days is not None and days < 0 else "Due ≤7 days" if days is not None and days <= 7 else "Upcoming"
        bills.append({"id": b["id"], "due": due.isoformat() if due else "", "name": b["vendor"], "amount": b["amount"],
                      "status": status, "source": "Bill" + (" #%s" % b["invoice_no"] if b["invoice_no"] else ""), "kind": "bill",
                      "category": b["category"] or "", "invoice_no": b["invoice_no"] or "", "notes": b["notes"] or ""})
    for r in db.execute("SELECT * FROM recurring WHERE client=? AND active=1", (client,)):
        hits = recurring_hits(r, entries)
        if not hits:
            continue
        last = dt.date.fromisoformat(hits[-1]["date"])
        nxt = _next_due(last, r["frequency"])
        if nxt > horizon:
            continue
        amt, averaged = recurring_forecast(r, hits, today)
        late = (today - nxt).days > 3 and not averaged
        status = "Late / check" if late else "Due ≤7 days" if (nxt - today).days <= 7 else "Upcoming"
        name = r["name"] + (" (average per %s)" % {"weekly": "week", "biweekly": "2 weeks", "monthly": "month"}.get(r["frequency"], r["frequency"] or "period") if averaged else "")
        bills.append({"id": r["id"], "due": nxt.isoformat(), "name": name, "amount": round(amt, 2), "status": status,
                      "source": "Recurring (autopay)" if r["autopay"] else "Recurring", "kind": "recurring",
                      "payee": r["payee"] or "", "category": r["category"] or "", "frequency": r["frequency"] or "monthly",
                      "typical_amount": r["typical_amount"], "autopay": bool(r["autopay"]), "last_paid": hits[-1]["date"],
                      "last_amount": -hits[-1]["amount"]})
    bills.sort(key=lambda b: b["due"] or "9999")

    # payroll (same rules as the workbook version)
    month_start = today.replace(day=1)
    prev_start = (month_start - dt.timedelta(days=1)).replace(day=1)
    mtd = prev = 0.0
    items = []
    for e in entries:
        if e["category"] not in PAYROLL_CATS:
            continue
        d = dt.date.fromisoformat(e["date"])
        out = -e["amount"]
        if d >= month_start:
            mtd += out
        elif d >= prev_start:
            prev += out
        if e["category"] == "Payroll Processing Fees":
            continue
        desc = (e["description"] or "").upper()
        kind = "gusto_tax" if "GUSTO/TAX" in desc else "gusto_net" if "GUSTO/NET" in desc else "tax" if e["category"] == "Payroll Taxes" else "check"
        items.append((d, kind, out))
    last_run = None
    gusto = {}
    for d, kind, out in items:
        if kind in ("gusto_net", "gusto_tax"):
            gusto[d] = gusto.get(d, 0.0) + out
    if gusto:
        latest = max(gusto)
        recent = {d: v for d, v in gusto.items() if (latest - d).days <= 2 * PERIOD_DAYS}
        big = max(recent.values())
        anchor = max(d for d, v in recent.items() if v >= 0.5 * big)
        end = anchor + dt.timedelta(days=PERIOD_DAYS - 1)
        run = [x for x in items if anchor <= x[0] <= end]
        tot = lambda kinds: round(sum(x[2] for x in run if x[1] in kinds), 2)
        last_run = {"start": anchor.isoformat(), "end": end.isoformat(), "date": max(x[0] for x in run).isoformat(),
                    "gusto": tot({"gusto_net", "gusto_tax"}), "gustoRuns": sum(1 for d in gusto if anchor <= d <= end),
                    "checks": tot({"check"}), "checkCount": sum(1 for x in run if x[1] == "check"),
                    "netPay": tot({"gusto_net", "check"}), "taxes": tot({"gusto_tax", "tax"}),
                    "total": round(sum(x[2] for x in run), 2)}

    outstanding = [{"id": e["id"], "date": e["date"], "check": e["check_no"] or "", "payee": e["payee"] or "",
                    "category": e["category"], "amount": e["amount"]} for e in entries if e["status"] == "open"]
    from .tips import splits_by_entry, ensure as _ensure_splits
    _ensure_splits(db, client)
    splits = splits_by_entry(db, client)
    ledger = []
    pending_txn = {r[0] for r in db.execute("SELECT txn_id FROM bank_txn WHERE client=? AND pending=1 AND removed=0", (client,))}
    for e in entries:  # an entry with splits (card tips) becomes one ledger row per part
        base = [e["date"], e["description"] or "", e["payee"] or ""]
        # not cleared: "open" = entered by hand, not at the bank yet; "pending" = the bank shows it but hasn't posted it
        state = "open" if e["status"] == "open" else "pending" if e["bank_txn_id"] in pending_txn else ""
        rest = round(e["amount"], 2)
        for cat, amt, memo in splits.get(e["id"], []):
            ledger.append(base + [cat, round(amt, 2), e["check_no"] or "", e["id"], state])
            rest = round(rest - amt, 2)
        if abs(rest) >= 0.005 or e["id"] not in splits:
            ledger.append(base + [e["category"] or NEEDS, rest, e["check_no"] or "", e["id"], state])
    groups = {c["name"]: c["pl_group"] or "Needs Category" for c in categories(db, client)}
    open_bills = round(sum(r["amount"] for r in db.execute("SELECT amount FROM bill WHERE client=? AND status='open'", (client,))), 2)
    needs = sum(1 for e in entries if (e["category"] or NEEDS) in (NEEDS, "Missing Check") and e["date"] >= (today - dt.timedelta(days=120)).isoformat())
    synced = get_meta(db, client, "bank_synced_at") or (max((a["synced_at"] or "" for a in accs), default=""))
    return {
        "source": "books",
        "bankSource": "SheetLink (live)",
        "bankSyncedAt": synced,
        "receivedAt": synced or now(),
        "syncedBy": get_meta(db, client, "synced_by", ""),
        "accounts": [{"name": a["name"], "mask": a["mask"], "institution": a["institution"]} for a in accs],
        "currentBalance": round(cur, 2),
        "availableBalance": round(avail, 2),
        "pending": pending,
        "pendingTotal": round(sum(p["amount"] for p in pending), 2),
        "billsDue": bills,
        "billsDueTotal": round(sum(b["amount"] for b in bills), 2),
        "payroll": {"lastRun": last_run, "monthToDate": round(mtd, 2), "previousMonth": round(prev, 2)},
        "outstanding": outstanding,
        "outstandingTotal": round(sum(o["amount"] for o in outstanding), 2),
        "bookBalance": round(sum(e["amount"] for e in entries), 2),
        "needsCategory": needs,
        "ledger": ledger, "groups": groups, "cash": [], "openBills": open_bills,
    }


# ------------------------------------------------------------------ payroll from the POS Payroll Summary
PAYROLL_PAY_CATS = ("Payroll - Wages", "Payroll Taxes")


def _norm_name(n):
    return " ".join(re.sub(r"[^a-z ]", " ", (n or "").lower()).split())


def pay_rates(db, client, shifts_path=None):
    """Hourly (straight-time) rate per employee (normalized name) from the POS Payroll Summary: regular pay / regular
    hours, so an overtime premium never inflates the base rate (total pay / total hours when a row has no regular
    split). Finance's imports + Shifts'. People with no paid hours (owners, salaried) have no rate."""
    tot = {}
    rows = []
    q = "SELECT employee, reg_hours, reg_pay, total_hours, total_pay FROM pos_payroll"
    try:
        rows += db.execute(q + " WHERE client=?", (client,)).fetchall()
    except sqlite3.OperationalError:
        pass
    if shifts_path and os.path.exists(shifts_path):
        try:
            s = sqlite3.connect("file:%s?mode=ro" % shifts_path, uri=True)
            try:
                rows += s.execute(q).fetchall()
            except sqlite3.OperationalError:  # older Shifts table without the regular/OT split
                rows += [(e, None, None, h, p) for e, h, p in s.execute("SELECT employee, total_hours, total_pay FROM pos_payroll")]
            s.close()
        except sqlite3.Error:
            pass
    for emp, rh, rp, h, pay in rows:
        if rh and rp and rh > 0 and rp > 0:
            h, pay = rh, rp
        k = _norm_name(emp)
        t = tot.setdefault(k, [0.0, 0.0])
        t[0] += h or 0
        t[1] += pay or 0
    return {k: round(p / h, 4) for k, (h, p) in tot.items() if h > 0 and p > 0}


def time_clock(db, client, shifts_path=None):
    """Time Clock punches [(date, employee, job, clock_in, hours)] from Finance's imports and Shifts' (deduplicated)."""
    seen, out = set(), []
    srcs = []
    try:
        srcs.append(db.execute("SELECT business_date, employee, job, clock_in, hours FROM pos_shift WHERE client=? AND hours > 0",
                               (client,)).fetchall())
    except sqlite3.OperationalError:
        pass
    if shifts_path and os.path.exists(shifts_path):
        try:
            s = sqlite3.connect("file:%s?mode=ro" % shifts_path, uri=True)
            srcs.append(s.execute("SELECT business_date, employee, job, clock_in, hours FROM pos_shift WHERE hours > 0").fetchall())
            s.close()
        except sqlite3.Error:
            pass
    for rows in srcs:
        for d, emp, job, cin, h in rows:
            key = (_norm_name(emp), cin or "%s|%s" % (d, h))
            if not d or key in seen:
                continue
            seen.add(key)
            out.append((d, emp, job, cin, float(h or 0)))
    return out


def get_salaries(db, client):
    """Salaried pay not on the POS clock: [{name, annual}] (Client settings)."""
    import json
    try:
        v = json.loads(get_meta(db, client, "salaries", "[]") or "[]")
        return [dict(name=str(x.get("name") or "").strip(), annual=float(x.get("annual") or 0)) for x in v if float(x.get("annual") or 0) > 0]
    except (ValueError, TypeError, sqlite3.OperationalError):
        return []


def set_salaries(db, client, items):
    import json
    set_meta(db, client, "salaries", json.dumps([dict(name=x["name"], annual=round(float(x["annual"]), 2)) for x in items]))
    db.commit()


def _salaried_names(db, client):
    return [_norm_name(x["name"]) for x in get_salaries(db, client) if _norm_name(x["name"])]


def _is_salaried(name, salaried):
    n = _norm_name(name)
    return any(n == k or n.startswith(k + " ") for k in salaried)


def _pos_wages(db, client, shifts_path=None, detail=None):
    """Wages per business day: Time Clock hours × each person's hourly rate (rates from the Payroll Summary;
    someone without a rate yet gets the median rate). Days with a Payroll Summary but no Time Clock use the summary."""
    rates = pay_rates(db, client, shifts_path)
    known = sorted(rates.values())
    median = known[len(known) // 2] if known else 0.0
    out = {}
    salaried = _salaried_names(db, client)
    for d, emp, job, cin, h in time_clock(db, client, shifts_path):
        if _is_salaried(emp, salaried):
            continue  # paid a salary (added per pay period), not by the hour
        k = _norm_name(emp)
        r = rates.get(k)
        out[d] = out.get(d, 0.0) + h * (r if r is not None else median)
        if detail is not None:
            x = detail.setdefault(d, [])
            x.append(dict(employee=emp, job=job, hours=round(h, 2), rate=r if r is not None else median, guessed=r is None))
    summary = {}
    try:
        for d, v in db.execute("SELECT business_date, sum(total_pay) FROM pos_payroll WHERE client=? GROUP BY business_date", (client,)):
            summary[d] = v or 0.0
    except sqlite3.OperationalError:
        pass
    for d, v in summary.items():
        if d not in out:
            out[d] = v
    return {d: round(v, 2) for d, v in out.items()}


def payroll_estimate(db, client, today=None, until=None, shifts_path=None, anchor="2026-10-11", lag_days=4):
    """Each pay day from today to `until` (at least the next one), estimated from the POS Time Clock
    (hours × each person's rate from the Payroll Summary): wages for the pay period (days not imported yet use the average of the same weekday) + card tips paid through payroll
    (card tips of the period) + the employer's share of payroll taxes (rate from recent Gusto runs).
    Pay periods are 14 days aligned to `anchor` (a period end); pay day = period end + the usual lag from Gusto."""
    today = today or dt.date.today()
    until = until or today
    wages = _pos_wages(db, client, shifts_path)
    tips = {}
    for d, v in db.execute("SELECT date, card_tips FROM sales_day WHERE client=?", (client,)):
        tips[d] = v or 0.0
    rate, lag, runs = 0.094, lag_days, []
    try:
        runs = [dict(r) for r in db.execute("""SELECT pay_day, period_end, gross, paycheck_tips, er_tax FROM gusto_run
                                               WHERE client=? AND kind='Regular' ORDER BY pay_day DESC LIMIT 4""", (client,))]
        g = sum(r["gross"] or 0 for r in runs)
        if g > 0:
            rate = sum(r["er_tax"] or 0 for r in runs) / g
        lags = sorted((dt.date.fromisoformat(r["pay_day"]) - dt.date.fromisoformat(r["period_end"])).days for r in runs if r["period_end"])
        if lags:
            lag = lags[len(lags) // 2]
    except sqlite3.OperationalError:
        pass

    def wd_avg(series, wd, before):
        vals = [v for d, v in sorted(series.items(), reverse=True)
                if d < before and dt.date.fromisoformat(d).weekday() == wd][:4]
        return sum(vals) / len(vals) if vals else 0.0

    salaries = get_salaries(db, client)
    sal = sum(x["annual"] for x in salaries) / 26.0  # biweekly
    a = dt.date.fromisoformat(anchor)
    k = (today - a).days // 14
    end = a + dt.timedelta(days=14 * k)
    while end + dt.timedelta(days=lag) < today:
        end += dt.timedelta(days=14)
    # a pay day that has passed stays on the list until the payroll actually comes out of the bank (Gusto debits)
    # or a Gusto run for that period is imported — a payroll not run yet is still money going out
    first = end
    for back in (1, 2):
        e0 = end - dt.timedelta(days=14 * back)
        if (today - (e0 + dt.timedelta(days=lag))).days <= 21 and not payroll_paid(db, client, e0, e0 + dt.timedelta(days=lag)):
            first = e0
    end = first
    out = []
    while True:
        pay = end + dt.timedelta(days=lag)
        if out and pay > until and pay >= today:
            break
        if pay < today and payroll_paid(db, client, end, pay):
            end += dt.timedelta(days=14)
            continue
        start = end - dt.timedelta(days=13)
        w = t = 0.0
        known = est_days = 0
        d = start
        while d <= end:
            iso = d.isoformat()
            if iso in wages:
                w += wages[iso]
                known += 1
            else:
                x = wd_avg(wages, d.weekday(), iso)
                w += x
                est_days += 1 if x else 0
            t += tips[iso] if iso in tips else wd_avg(tips, d.weekday(), iso)
            d += dt.timedelta(days=1)
        gross = w + t + sal
        late = pay < today
        out.append(dict(pay_day=pay.isoformat(), start=start.isoformat(), end=end.isoformat(), wages=round(w, 2), tips=round(t, 2),
                        salaries=round(sal, 2), er_tax=round(gross * rate, 2), total=round(gross * (1 + rate), 2), days_known=known,
                        days_estimated=est_days, late=late, days_late=(today - pay).days if late else 0))
        end += dt.timedelta(days=14)
        if len(out) > 30:
            break
    # a Gusto payroll preview pasted for a pay day replaces the Time Clock estimate for that pay day (gross + company taxes
    # + benefits + reimbursements = what Gusto debits); it stops applying once the pay day has passed
    try:
        prev = gusto_previews(db, client)
    except sqlite3.OperationalError:
        prev = {}
    for p in out:
        g_ = prev.get(p["pay_day"]) or next((v for v in prev.values() if v.get("period_end") == p["end"]), None)
        if g_:
            p["estimate_total"] = p["total"]
            p["gusto"] = g_
            p["total"] = round(g_["total"], 2)
    last = runs[0] if runs else None
    est = dict(pay_days=out, rate=round(rate, 4), lag=lag, has_data=bool(wages) or bool(salaries), salaries=salaries,
               last_run=dict(pay_day=last["pay_day"], total=round((last["gross"] or 0) + (last["er_tax"] or 0), 2)) if last else None)
    # overtime (over 40 hours in a Mon–Sun workweek) and the bank / paper-check split of each pay day
    from . import payrollcost
    try:
        payrollcost.enrich(db, client, est, shifts_path, today=today)
    except Exception as e:  # noqa: BLE001 — never lose the estimate itself over the extra detail
        import logging
        logging.getLogger(__name__).warning("payroll cost detail failed for %s: %s", client, e)
    return est


def payroll_paid(db, client, period_end, pay_day):
    """Has the payroll for the period ending `period_end` come out of the bank? Yes when a Gusto run for that period is
    imported, or Gusto net-pay / tax debits (or a payroll entry paid through Gusto) show up from the day after the period ends to 10 days after the pay day."""
    try:
        if db.execute("SELECT 1 FROM gusto_run WHERE client=? AND period_end=? AND kind='Regular'", (client, period_end.isoformat())).fetchone():
            return True
    except sqlite3.OperationalError:
        pass
    a, b = (period_end + dt.timedelta(days=1)).isoformat(), (pay_day + dt.timedelta(days=10)).isoformat()
    for r in db.execute("SELECT description, payee, category, amount FROM entry WHERE client=? AND status!='void' AND amount<0 AND date BETWEEN ? AND ?", (client, a, b)):
        desc = (r["description"] or "").upper()
        # Gusto's own debits, or a payroll paid another way through Gusto (e.g. "Gusto payment plan … payroll paid with
        # Gusto financing", entered by hand as Payroll - Wages)
        if "GUSTO" in desc + " " + (r["payee"] or "").upper() and ("NET" in desc or "/TAX" in desc or " TAX" in desc
                                                                   or r["category"] in ("Payroll - Wages", "Payroll Taxes")):
            return True
    return False


# ------------------------------------------------------------------ Gusto payroll preview (pasted before a payroll runs)
GUSTO_PREVIEW_SCHEMA = """CREATE TABLE IF NOT EXISTS gusto_preview (
  client TEXT NOT NULL, pay_day TEXT NOT NULL, period_start TEXT, period_end TEXT, gross REAL, reimb REAL, er_tax REAL,
  benefits REAL, total REAL, rows TEXT, created_by TEXT, created_at TEXT, PRIMARY KEY (client, pay_day))"""
_MONEY = r"\(?-?\$?\s*-?[\d,]*\.?\d+\)?"


def _amt(v):
    v = v.strip()
    neg = v.startswith("(") or "-" in v
    n = float(re.sub(r"[^\d.]", "", v) or 0)
    return -n if neg else n


def parse_gusto_preview(text):
    """Gusto "Review payroll" table pasted as text: Team member, Gross pay, Reimbursements, Company taxes, Company benefits,
    Subtotal. Returns dict(rows=[{name, gross, reimb, er_tax, benefits, total}], gross, reimb, er_tax, benefits, total,
    stated_total) or raises ValueError."""
    rows, stated = [], None
    pat = re.compile(r"^(.*?)[\t ]+(%s)[\t ]+(%s)[\t ]+(%s)[\t ]+(%s)[\t ]+(%s)\s*$" % ((_MONEY,) * 5))
    for line in (text or "").splitlines():
        line = line.replace(" ", " ").strip()
        if not line:
            continue
        m = pat.match(line)
        if not m:
            continue
        name = m.group(1).strip().strip(",")
        vals = [_amt(m.group(i)) for i in range(2, 7)]
        if re.match(r"(grand )?totals?$", name, re.I):
            stated = vals
            continue
        if "," in name:  # "Berkowitz, Veronica" -> "Veronica Berkowitz"
            last, first = [x.strip() for x in name.split(",", 1)]
            name = (first + " " + last).strip()
        rows.append(dict(name=name, gross=vals[0], reimb=vals[1], er_tax=vals[2], benefits=vals[3], total=vals[4]))
    if not rows:
        raise ValueError("No payroll rows found. Copy the table from Gusto's payroll review (Team member, Gross pay, "
                         "Reimbursements, Company taxes, Company benefits, Subtotal).")
    tot = {k: round(sum(r[k] for r in rows), 2) for k in ("gross", "reimb", "er_tax", "benefits", "total")}
    if stated and abs(stated[4] - tot["total"]) > 0.05:
        raise ValueError("The rows add up to %.2f but the Grand totals line says %.2f — part of the table is missing."
                         % (tot["total"], stated[4]))
    return dict(rows=[r for r in rows if any(abs(r[k]) > 0.004 for k in ("gross", "reimb", "er_tax", "benefits", "total"))],
                stated_total=stated[4] if stated else None, **tot)


def save_gusto_preview(db, client, pay_day, start, end, parsed, user):
    import json
    db.execute(GUSTO_PREVIEW_SCHEMA)
    db.execute("""INSERT OR REPLACE INTO gusto_preview (client, pay_day, period_start, period_end, gross, reimb, er_tax, benefits, total,
                  rows, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
               (client, pay_day, start, end, parsed["gross"], parsed["reimb"], parsed["er_tax"], parsed["benefits"], parsed["total"],
                json.dumps(parsed["rows"]), user, dt.datetime.now().isoformat(timespec="seconds")))
    db.commit()


def delete_gusto_preview(db, client, pay_day):
    db.execute(GUSTO_PREVIEW_SCHEMA)
    db.execute("DELETE FROM gusto_preview WHERE client=? AND pay_day=?", (client, pay_day))
    db.commit()


def gusto_previews(db, client):
    import json
    db.execute(GUSTO_PREVIEW_SCHEMA)
    out = {}
    for r in db.execute("SELECT * FROM gusto_preview WHERE client=?", (client,)):
        d = dict(r)
        d["rows"] = json.loads(d["rows"] or "[]")
        out[d["pay_day"]] = d
    return out


INCOME_CATS = ("Sales - Card", "Sales - Cash")


def projection(db, client, snap, target, today=None, card_on_the_way=0.0, lookback_days=28, deposit_lag=2, payroll=None, taxes=None):
    """Projected bank balance on `target`: available balance now − checks written but not cleared + card batches not yet
    deposited + estimated sales deposits until then − bills and recurring payments due by then."""
    today = today or dt.date.today()
    target = max(target, today)
    days_ahead = (target - today).days
    start = snap.get("availableBalance", snap.get("currentBalance", 0.0)) or 0.0
    not_cleared = snap.get("outstandingTotal", 0.0) or 0.0  # negative: checks written, not at the bank yet
    items = []
    for b in db.execute("SELECT * FROM bill WHERE client=? AND status='open' ORDER BY due_date", (client,)):
        due = _date(b["due_date"])
        if due is None or due <= target:
            items.append(dict(date=(due or today).isoformat(), name=b["vendor"], amount=round(b["amount"], 2), kind="Bill",
                              overdue=bool(due and due < today)))
    entries = [dict(r) for r in db.execute("SELECT id, date, amount, payee, category, description FROM entry WHERE client=? AND status!='void' ORDER BY date, id", (client,))]
    use_pr = bool(payroll and payroll.get("has_data"))
    if use_pr:  # payroll comes from the POS Payroll Summary, not from the last Gusto debits
        for p in payroll["pay_days"]:
            if p.get("late") or today.isoformat() <= p["pay_day"] <= target.isoformat():
                items.append(dict(date=today.isoformat() if p.get("late") else p["pay_day"], name=("NOT RUN YET (was due %s) · " % dt.date.fromisoformat(p["pay_day"]).strftime("%-m/%-d") if p.get("late") else "") + ("Payroll %s–%s (Gusto preview)" if p.get("gusto") else "Payroll %s–%s (est.)") % (dt.date.fromisoformat(p["start"]).strftime("%-m/%-d"),
                                                                                    dt.date.fromisoformat(p["end"]).strftime("%-m/%-d")),
                                  amount=p["total"], kind="Payroll", overdue=bool(p.get("late"))))
    for t in taxes or []:  # sales / alcohol tax collected, due by the 25th of the next month (overdue: counted today)
        if t["overdue"] or today.isoformat() <= t["due"] <= target.isoformat():
            items.append(dict(date=today.isoformat() if t["overdue"] else t["due"], amount=t["amount"], kind="Tax",
                              name=t["label"] + (" (was due %s)" % dt.date.fromisoformat(t["due"]).strftime("%-m/%-d") if t["overdue"] else ""),
                              overdue=bool(t["overdue"])))
    tax_cats = {"Sales Tax Payable", "Alc Sales Tax"} if taxes is not None else set()
    for r in db.execute("SELECT * FROM recurring WHERE client=? AND active=1", (client,)):
        if use_pr and (r["category"] in PAYROLL_PAY_CATS):
            continue
        if r["category"] in tax_cats:  # projected from the tax collected instead
            continue
        hits = recurring_hits(r, entries)
        if not hits:
            continue
        amt, averaged = recurring_forecast(r, hits, today)
        label = r["name"] + (" (average per %s)" % {"weekly": "week", "biweekly": "2 weeks", "monthly": "month"}.get(r["frequency"], r["frequency"] or "period") if averaged else "")
        nxt = _next_due(dt.date.fromisoformat(hits[-1]["date"]), r["frequency"])
        n, late = 0, None
        while nxt <= target and n < 60:
            if nxt < today:
                late = nxt  # missed payments count once (paid late, or paid under another name — see Upcoming bills)
            else:
                items.append(dict(date=nxt.isoformat(), name=label, amount=round(amt, 2), kind="Recurring", overdue=False))
            nxt = _next_due(nxt, r["frequency"])
            n += 1
        if late:
            items.append(dict(date=today.isoformat(), name=label, amount=round(amt, 2), kind="Recurring (was due %s)" % late.strftime("%-m/%-d"),
                              overdue=True))
    items.sort(key=lambda x: x["date"])
    bills = round(sum(i["amount"] for i in items), 2)
    # card deposits still to come: one per open business day between now and the target. Each open day is estimated at the
    # average card sales + card tips of its weekday over its last 4 days with sales (from the batch reports); weekdays the
    # restaurant is closed (no sales in the last 8 weeks) count nothing. Cash sales aren't counted until they're deposited.
    since = (today - dt.timedelta(days=lookback_days)).isoformat()
    hist = [dict(r) for r in db.execute("SELECT date, card_sales, card_tips FROM sales_day WHERE client=? AND date>=? AND date<? ORDER BY date DESC",
                                        (client, (today - dt.timedelta(days=56)).isoformat(), today.isoformat()))]
    by_wd = {}
    for r in hist:
        t = (r["card_sales"] or 0) + (r["card_tips"] or 0)
        if t > 0:
            by_wd.setdefault(dt.date.fromisoformat(r["date"]).weekday(), []).append(t)
    avg_wd = {wd: sum(v[:4]) / len(v[:4]) for wd, v in by_wd.items() if len(v) >= 2}
    # owner (11/6): no average sales in the forecast — only money already earned (card batches not deposited yet) counts
    sales_days = []
    income = round(sum(x["amount"] for x in sales_days), 2)
    income_days = len(sales_days)
    per_day = round(income / income_days, 2) if income_days else 0.0
    open_days = ", ".join(dt.date(2026, 1, 5 + wd).strftime("%a") for wd in sorted(avg_wd))  # 2026-01-05 is a Monday
    # other day-to-day spending (supplies, groceries, liquor, …): average daily outflows over the same window, leaving out
    # payees already projected as recurring payments and bills, so nothing is counted twice
    rec_payees = {(r[0] or "") for r in db.execute("SELECT payee FROM recurring WHERE client=? AND active=1", (client,))}
    bill_payees = {(r[0] or "") for r in db.execute("SELECT vendor FROM bill WHERE client=?", (client,))}
    out = sum(-e["amount"] for e in entries if e["amount"] < 0 and e["date"] >= since and e["date"] <= today.isoformat()
              and (e["payee"] or "") not in rec_payees and (e["payee"] or "") not in bill_payees
              and not (use_pr and e["category"] in PAYROLL_PAY_CATS) and e["category"] not in tax_cats)
    other_per_day = out / lookback_days
    other = round(other_per_day * days_ahead, 2)
    projected = round(start + not_cleared + card_on_the_way + income - bills - other, 2)
    return dict(target=target.isoformat(), days=days_ahead, start=round(start, 2), not_cleared=round(not_cleared, 2),
                card_on_the_way=round(card_on_the_way, 2), income=income, per_day=round(per_day, 2), income_days=income_days,
                lookback_days=lookback_days, bills=bills, payments=items, projected=projected, other=other,
                sales_days=sales_days, open_days=open_days,
                other_per_day=round(other_per_day, 2),
                overdue=round(sum(i["amount"] for i in items if i["overdue"]), 2))


# ------------------------------------------------------------------ recurring payments check
CADENCES = [("weekly", 7, 5, 9), ("biweekly", 14, 11, 17), ("monthly", 30.4, 25, 37), ("quarterly", 91, 80, 102)]


def _cadence(dates):
    """(frequency, typical gap, share of gaps that fit) for a sorted list of distinct dates, or None."""
    if len(dates) < 3:
        return None
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    med = sorted(gaps)[len(gaps) // 2]
    for name, days, lo, hi in CADENCES:
        if lo <= med <= hi:
            fit = sum(1 for x in gaps if lo <= x <= hi) / len(gaps)
            need = 4 if name == "weekly" else 3
            if len(dates) >= need and fit >= 0.6:
                return name, days, fit
    return None


def _ignored(db, client):
    import json
    r = db.execute("SELECT v FROM meta WHERE client=? AND k='recurring_ignore'", (client,)).fetchone()
    try:
        return set(json.loads(r[0])) if r and r[0] else set()
    except ValueError:
        return set()


def ignore_recurring(db, client, payee, undo=False):
    import json
    s = _ignored(db, client)
    (s.discard if undo else s.add)(payee)
    db.execute("INSERT OR REPLACE INTO meta(client,k,v) VALUES(?,?,?)", (client, "recurring_ignore", json.dumps(sorted(s))))
    db.commit()


def add_recurring(db, client, name, payee, category, frequency, typical_amount, autopay):
    db.execute("""INSERT INTO recurring(client,name,payee,category,frequency,typical_amount,autopay,active)
                  VALUES(?,?,?,?,?,?,?,1)""", (client, name, payee, category, frequency, typical_amount, 1 if autopay else 0))
    db.commit()


def recurring_check(db, client, today=None, months=6):
    """Look through the register for payments that repeat on a schedule and compare them with the recurring list.
    Returns new candidates, tracked ones whose schedule or amount changed, and tracked ones not seen lately."""
    today = today or dt.date.today()
    since = (today - dt.timedelta(days=int(months * 30.4))).isoformat()
    rows = [dict(r) for r in db.execute("""SELECT id, date, amount, payee, category, description FROM entry WHERE client=? AND status!='void'
                                           AND amount<0 AND date>=? AND date<=? ORDER BY date, id""", (client, since, today.isoformat()))]

    def summarize(hits):
        by_day = {}
        for e in hits:
            by_day[e["date"]] = by_day.get(e["date"], 0.0) - e["amount"]
        dates = sorted(dt.date.fromisoformat(d) for d in by_day)
        amts = [by_day[d.isoformat()] for d in dates]
        cad = _cadence(dates)
        recent = sorted(amts[-3:])
        typical = recent[len(recent) // 2] if recent else 0.0
        cats = {}
        for e in hits:
            cats[e["category"] or NEEDS] = cats.get(e["category"] or NEEDS, 0) - e["amount"]
        return dict(count=len(dates), first=dates[0].isoformat() if dates else "", last=dates[-1].isoformat() if dates else "",
                    last_amount=round(amts[-1], 2) if amts else 0.0, typical=round(typical, 2),
                    varies=bool(recent) and typical > 0 and (recent[-1] - recent[0]) / typical > 0.15,
                    low=round(min(amts[-6:]), 2) if amts else 0.0, high=round(max(amts[-6:]), 2) if amts else 0.0,
                    frequency=cad[0] if cad else None, gap=cad[1] if cad else None, fit=round(cad[2], 2) if cad else None,
                    category=max(cats.items(), key=lambda x: x[1])[0] if cats else "",
                    per_month=round(sum(amts) / max(1.0, (today - dates[0]).days / 30.4), 2) if dates else 0.0)

    def gap_days(freq):
        return {"weekly": 7, "biweekly": 14, "quarterly": 91, "yearly": 365, "annual": 365}.get((freq or "monthly").lower(), 30.4)

    # the bank feed only shows payments up to its last sync, so "late" is judged against that date
    seen = db.execute("SELECT max(date) FROM bank_txn WHERE client=? AND removed=0", (client,)).fetchone()[0]
    through = min(today, dt.date.fromisoformat(seen)) if seen else today
    tracked = [dict(r) for r in db.execute("SELECT * FROM recurring WHERE client=? ORDER BY name", (client,))]
    active_payees = {(r["payee"] or "") for r in tracked if r["active"]}
    removed_payees = {(r["payee"] or "") for r in tracked if not r["active"]}
    ignored = _ignored(db, client)

    out_tracked = []
    for r in tracked:
        if not r["active"]:
            continue
        hits = recurring_hits(r, rows)
        s = summarize(hits)
        notes, status = [], "ok"
        g = gap_days(r["frequency"])
        if not hits:
            status = "missing"
            notes.append("No payments in the last %d months" % months)
        else:
            since_last = (through - dt.date.fromisoformat(s["last"])).days
            if since_last > 2 * g + 5:
                status = "missing"
                notes.append("Last paid %s (%d days ago)" % (dt.date.fromisoformat(s["last"]).strftime("%-m/%-d"), since_last))
            elif since_last > g + 5:
                status = "late"
                notes.append("Next was due about %s" % (dt.date.fromisoformat(s["last"]) + dt.timedelta(days=round(g))).strftime("%-m/%-d"))
            if s["frequency"] and s["frequency"] != (r["frequency"] or "monthly").replace("annual", "yearly"):
                status = "changed" if status == "ok" else status
                notes.append("Paid %s, set up as %s" % (s["frequency"], r["frequency"] or "monthly"))
            if r["typical_amount"] and s["typical"] and abs(s["typical"] - r["typical_amount"]) > max(5.0, 0.1 * r["typical_amount"]):
                status = "changed" if status == "ok" else status
                notes.append("Recent payments about $%s, set up as $%s" % ("{:,.2f}".format(s["typical"]), "{:,.2f}".format(r["typical_amount"])))
            elif r["typical_amount"] and s["last_amount"] and abs(s["last_amount"] - r["typical_amount"]) > max(1.0, 0.02 * r["typical_amount"]):
                status = "changed" if status == "ok" else status
                notes.append("Last payment $%s, set up as $%s (upcoming bills follow the last payment)" % (
                    "{:,.2f}".format(s["last_amount"]), "{:,.2f}".format(r["typical_amount"])))
        out_tracked.append(dict(r, check=s, status=status, notes=notes,
                                suggest_frequency=s["frequency"] or r["frequency"],
                                suggest_amount=(s["last_amount"] if r["typical_amount"] and s["last_amount"] and abs(s["last_amount"] - r["typical_amount"]) > max(1.0, 0.02 * r["typical_amount"])
                                                else s["typical"] if (r["typical_amount"] and not s["varies"]) else r["typical_amount"])))

    by_payee = {}
    for e in rows:
        # paychecks and payroll taxes belong to payroll, which has its own totals; they aren't bills
        if e["payee"] and e["category"] not in ("Payroll - Wages", "Payroll Taxes"):
            by_payee.setdefault(e["payee"], []).append(e)
    new, skipped = [], 0
    for payee, hits in by_payee.items():
        if payee in active_payees:
            continue
        s = summarize(hits)
        if not s["frequency"]:
            continue
        if (through - dt.date.fromisoformat(s["last"])).days > 2 * s["gap"] + 5:
            continue  # stopped
        if payee in ignored or payee in removed_payees:
            skipped += 1
            continue
        new.append(dict(payee=payee, **s))
    new.sort(key=lambda x: -x["per_month"])
    order = {"missing": 0, "late": 1, "changed": 2, "ok": 3}
    out_tracked.sort(key=lambda x: (order[x["status"]], x["name"]))
    return dict(new=new, tracked=out_tracked, skipped=skipped, ignored=sorted(ignored), months=months, since=since,
                through=through.isoformat())


# ------------------------------------------------------------------ starting chart of categories for a new client
STARTER = {
    "consulting": [
        ("Revenue", ["Consulting Income", "Bookkeeping Income", "Software & Setup Income", "Reimbursed Expenses"]),
        ("Cost of Goods Sold", ["Contract Labor", "Software Resold to Clients"]),
        ("Labor", ["Payroll - Wages", "Payroll Taxes", "Payroll Processing Fees"]),
        ("Operating Expenses", ["Advertising & Marketing", "Bank Fees", "Business Licenses & Taxes", "Car & Truck", "Education & Training",
                                "Home Office", "Insurance", "Legal & Professional Fees", "Meals (50%)", "Merchant Processing Fees",
                                "Office Supplies", "Postage & Shipping", "Rent & Occupancy", "Repairs & Maintenance",
                                "Software & Subscriptions", "Telephone & Internet", "Travel", "Utilities", "Web Hosting & Domains",
                                "Other Expense"]),
        ("Other Income", ["Interest Income", "Other Income"]),
        ("Not in P&L", ["Owner Contribution", "Owner Draw / Personal", "Transfer", "Credit Card Payment", "Loan Payables",
                        "Estimated Tax Payments"]),
        ("Needs Category", [NEEDS, "Missing Check"]),
    ],
}
STARTER_RULES = {
    "consulting": [("NAMECHEAP", "Namecheap", "Web Hosting & Domains"), ("GODADDY", "GoDaddy", "Web Hosting & Domains"),
                   ("ANTHROPIC", "Anthropic", "Software & Subscriptions"), ("CLAUDE.AI", "Anthropic", "Software & Subscriptions"),
                   ("OPENAI", "OpenAI", "Software & Subscriptions"), ("CHATGPT", "OpenAI", "Software & Subscriptions"),
                   ("MICROSOFT", "Microsoft", "Software & Subscriptions"), ("MSFT", "Microsoft", "Software & Subscriptions"),
                   ("GOOGLE *GSUITE", "Google Workspace", "Software & Subscriptions"), ("GOOGLE WORKSPACE", "Google Workspace", "Software & Subscriptions"),
                   ("ADOBE", "Adobe", "Software & Subscriptions"), ("ZOHO", "Zoho", "Software & Subscriptions"),
                   ("INTUIT", "Intuit", "Software & Subscriptions"), ("SHEETLINK", "SheetLink", "Software & Subscriptions"),
                   ("INTEREST PAID", "Bank", "Interest Income"), ("INTEREST CREDIT", "Bank", "Interest Income"),
                   ("SERVICE CHARGE", "Bank", "Bank Fees"), ("MAINTENANCE FEE", "Bank", "Bank Fees"),
                   ("IRS USATAXPYMT", "IRS", "Estimated Tax Payments"), ("OKLAHOMA TAX", "Oklahoma Tax Commission", "Estimated Tax Payments")],
}


def seed_client(db, client, kind="consulting"):
    """Give a brand-new client a starting chart of categories and a few bank rules (skips anything already there)."""
    n = 0
    for i, (grp, names) in enumerate(STARTER[kind]):
        for j, name in enumerate(names):
            if not db.execute("SELECT 1 FROM category WHERE client=? AND name=?", (client, name)).fetchone():
                db.execute("INSERT INTO category(client,name,pl_group,sort) VALUES(?,?,?,?)", (client, name, grp, i * 100 + j))
                n += 1
    for k, (pat, payee, cat) in enumerate(STARTER_RULES.get(kind, [])):
        if not db.execute("SELECT 1 FROM rule WHERE client=? AND upper(pattern)=upper(?)", (client, pat)).fetchone():
            db.execute("INSERT INTO rule(client,pattern,payee,category,sort) VALUES(?,?,?,?,?)", (client, pat, payee, cat, 100 + k))
    db.commit()
    return n


def payees(db, client):
    return [r[0] for r in db.execute("""SELECT payee FROM entry WHERE client=? AND payee IS NOT NULL AND payee!=''
                                        GROUP BY payee ORDER BY count(*) DESC LIMIT 400""", (client,))]


def backup(db, instance_path, keep=30):
    """Daily copy of the database in instance/backups (newest `keep` kept). Returns the file path."""
    folder = os.path.join(instance_path, "backups")
    os.makedirs(folder, exist_ok=True)
    os.chmod(folder, 0o700)
    path = os.path.join(folder, "books-%s.db" % dt.date.today().isoformat())
    dst = sqlite3.connect(path)
    db.backup(dst)
    dst.close()
    os.chmod(path, 0o600)
    files = sorted(f for f in os.listdir(folder) if f.startswith("books-"))
    for f in files[:-keep]:
        os.remove(os.path.join(folder, f))
    return path
