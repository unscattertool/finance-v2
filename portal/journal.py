"""Double-entry journal (books step 2).

Every transaction is a journal entry whose lines balance (debits = credits). For books clients like WWK the journal
is posted from the bank register: each register entry becomes one journal entry —

    bank account        +amount            (money in is a debit to the bank, money out a credit)
    each split          -split amount      (card tips, sales tax, …) to the split category's account
    the rest            -rest              to the entry's category account

Categories and bank-feed accounts reach the clean chart through account_map (portal/coa.py), so a merge or re-map
in the chart changes where history lands without editing any register entry.

The register stays the place people work in for now; the journal is re-posted from it whenever the register,
splits, categories or chart change (fingerprint), and reports can read the journal instead of the register
(meta reports_from_journal). `compare` runs every register report both ways and lists any difference.

Amounts are stored in cents (integers) so balancing is exact.
"""
import copy
import datetime as dt
import hashlib

from . import coa

SCHEMA = """
CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY, client TEXT NOT NULL, date TEXT NOT NULL, ref_type TEXT NOT NULL, ref_id INTEGER,
  description TEXT, payee TEXT, check_no TEXT, state TEXT, posted_at TEXT);
CREATE INDEX IF NOT EXISTS journal_client_date ON journal(client, date);
CREATE UNIQUE INDEX IF NOT EXISTS journal_ref ON journal(client, ref_type, ref_id);
CREATE TABLE IF NOT EXISTS journal_line (
  id INTEGER PRIMARY KEY, journal_id INTEGER NOT NULL, client TEXT NOT NULL, account_id INTEGER NOT NULL,
  cents INTEGER NOT NULL, memo TEXT, category TEXT);
CREATE INDEX IF NOT EXISTS journal_line_j ON journal_line(journal_id);
CREATE INDEX IF NOT EXISTS journal_line_acct ON journal_line(client, account_id);
"""
NEEDS = "Uncategorized"
BOOKS_BANK = "_books"  # register entries with no bank-feed account (hand-entered checks / manual entries)
CASH_TYPES = ("bank", "cash", "credit_card", "clearing")

# report group (the register's P&L groups) for an account with no Finance category behind it
TYPE_GROUP = {"income": "Revenue", "other_income": "Other Income", "cogs": "Cost of Goods Sold",
              "expense": "Operating Expenses", "other_expense": "Operating Expenses"}


def ensure(db):
    coa.ensure(db)
    db.executescript(SCHEMA)


def cents(x):
    return int(round((x or 0) * 100))


# ------------------------------------------------------------------ chart for a books client
def ensure_chart(db, client, user="system"):
    """Make sure every category in use and every bank account has a clean account (first run = set up the chart
    from the categories). Returns the number of clean accounts created."""
    ensure(db)
    from .tips import ensure as ensure_splits
    ensure_splits(db, client)
    cats, banks = coa.sources_from_books(db, client)
    known = {c["key"] for c in cats}
    groups = {r[0]: r[1] for r in db.execute("SELECT name, pl_group FROM category WHERE client=?", (client,))}
    used = {r[0] or NEEDS for r in db.execute("SELECT DISTINCT category FROM entry WHERE client=? AND status!='void'", (client,))}
    used |= {r[0] or NEEDS for r in db.execute("SELECT DISTINCT category FROM entry_split WHERE client=?", (client,))}
    for name in sorted(used - known):  # in use but not in the category list: same as the register (Needs Category)
        cats.append(dict(key=name, name=name, ztype=groups.get(name) or "Needs Category", type="other_current_asset",
                         code=None, active=1, parent=None, uses=0))
    banks.append(dict(key=BOOKS_BANK, name="Checking (entries not from the bank feed)", ztype="Bank feed", type="bank",
                      code=None, active=1, parent=None))
    have = {(r[0], r[1]) for r in db.execute("SELECT source, source_key FROM account_map WHERE client=? AND account_id IS NOT NULL", (client,))}
    made = 0
    for source, items in (("category", cats), ("bank", banks)):
        todo = [it for it in items if (source, it["key"]) not in have]
        if todo:
            coa.import_sources(db, client, source, todo, user)
            made += coa.propose(db, client, source, todo, user)
    return made


def _maps(db, client):
    m = {}
    for r in db.execute("SELECT source, source_key, account_id FROM account_map WHERE client=? AND account_id IS NOT NULL", (client,)):
        m[(r[0], r[1])] = r[2]
    return m


# ------------------------------------------------------------------ posting
def fingerprint(db, client):
    h = hashlib.sha1()
    for sql in ("SELECT id, date, amount, category, status, description, payee, check_no, bank_txn_id FROM entry WHERE client=? ORDER BY id",
                "SELECT id, entry_id, category, amount FROM entry_split WHERE client=? ORDER BY id",
                "SELECT source, source_key, account_id FROM account_map WHERE client=? ORDER BY 1, 2",
                "SELECT id, name, parent_id, type FROM ledger_account WHERE client=? ORDER BY id",
                "SELECT name, pl_group FROM category WHERE client=? ORDER BY name",
                "SELECT txn_id, account_id, pending FROM bank_txn WHERE client=? AND removed=0 ORDER BY txn_id"):
        for r in db.execute(sql, (client,)):
            h.update(repr(tuple(r)).encode())
    return h.hexdigest()


def post_from_register(db, client, user="system", force=False):
    """(Re)post the journal from the register. Returns dict(posted, lines, skipped) — skipped=True when nothing changed."""
    ensure(db)
    ensure_chart(db, client, user)
    fp = fingerprint(db, client)
    old = db.execute("SELECT value FROM coa_meta WHERE client=? AND key='journal_fp'", (client,)).fetchone()
    if old and old[0] == fp and not force:
        n = db.execute("SELECT count(*) FROM journal WHERE client=? AND ref_type='entry'", (client,)).fetchone()[0]
        return dict(posted=n, lines=None, skipped=True)
    from .tips import splits_by_entry
    maps = _maps(db, client)
    splits = splits_by_entry(db, client)
    txn_acct = {r[0]: r[1] for r in db.execute("SELECT txn_id, account_id FROM bank_txn WHERE client=?", (client,))}
    pending = {r[0] for r in db.execute("SELECT txn_id FROM bank_txn WHERE client=? AND pending=1 AND removed=0", (client,))}
    default_bank = maps[("bank", BOOKS_BANK)]
    # entries with no bank-feed account go to the bank account most entries use (WWK: one checking account)
    counts = {}
    for r in db.execute("SELECT bank_txn_id FROM entry WHERE client=? AND status!='void' AND bank_txn_id IS NOT NULL", (client,)):
        a = maps.get(("bank", txn_acct.get(r[0])))
        if a:
            counts[a] = counts.get(a, 0) + 1
    if counts:
        default_bank = max(counts, key=counts.get)

    def cat_account(name):
        a = maps.get(("category", name or NEEDS))
        if not a:
            raise ValueError("Category %r has no account in the chart." % name)
        return a

    ts = coa.now()
    db.execute("DELETE FROM journal_line WHERE journal_id IN (SELECT id FROM journal WHERE client=? AND ref_type='entry')", (client,))
    db.execute("DELETE FROM journal WHERE client=? AND ref_type='entry'", (client,))
    n_j = n_l = 0
    for e in db.execute("SELECT * FROM entry WHERE client=? AND status!='void' ORDER BY date, id", (client,)).fetchall():
        state = "open" if e["status"] == "open" else "pending" if e["bank_txn_id"] in pending else ""
        cur = db.execute("""INSERT INTO journal (client, date, ref_type, ref_id, description, payee, check_no, state, posted_at)
                            VALUES (?,?,?,?,?,?,?,?,?)""",
                         (client, e["date"], "entry", e["id"], e["description"], e["payee"], e["check_no"], state, ts))
        jid = cur.lastrowid
        bank = maps.get(("bank", txn_acct.get(e["bank_txn_id"]))) or default_bank
        total = cents(e["amount"])
        lines = [(bank, total, None, None)]
        rest = total
        for cat, amt, memo in splits.get(e["id"], []):
            c = cents(amt)
            lines.append((cat_account(cat), -c, memo, cat))
            rest -= c
        if rest or e["id"] not in splits:
            lines.append((cat_account(e["category"]), -rest, None, e["category"] or NEEDS))
        if sum(l[1] for l in lines) != 0:
            raise ValueError("Entry %s doesn't balance." % e["id"])
        db.executemany("INSERT INTO journal_line (journal_id, client, account_id, cents, memo, category) VALUES (?,?,?,?,?,?)",
                       [(jid, client, a, c, m, cat) for a, c, m, cat in lines])
        n_j += 1
        n_l += len(lines)
    db.execute("INSERT OR REPLACE INTO coa_meta (client, key, value) VALUES (?, 'journal_fp', ?)", (client, fp))
    db.execute("INSERT OR REPLACE INTO coa_meta (client, key, value) VALUES (?, 'journal_posted_at', ?)", (client, ts))
    db.commit()
    return dict(posted=n_j, lines=n_l, skipped=False)


def check_balanced(db, client):
    """Journal entries whose lines don't add to zero (should always be empty)."""
    ensure(db)
    return [dict(r) for r in db.execute(
        """SELECT j.id, j.date, j.description, sum(l.cents) AS off FROM journal j JOIN journal_line l ON l.journal_id=j.id
           WHERE j.client=? GROUP BY j.id HAVING sum(l.cents) != 0""", (client,))]


# ------------------------------------------------------------------ reading
def _accounts(db, client):
    acc = {r["id"]: dict(r) for r in db.execute("SELECT * FROM ledger_account WHERE client=?", (client,))}
    for a in acc.values():
        a["full"] = coa.full_name(acc, a)
    return acc


def report_groups(db, client, acc=None):
    """Account id -> register P&L group. Comes from the Finance categories mapped to the account (so changing a
    category's P&L group still works); accounts with no category get one from their type."""
    acc = acc or _accounts(db, client)
    cat_group = {r[0]: r[1] or "Needs Category" for r in db.execute("SELECT name, pl_group FROM category WHERE client=?", (client,))}
    out, conflicts = {}, {}
    for r in db.execute("SELECT source_key, account_id FROM account_map WHERE client=? AND source='category' AND account_id IS NOT NULL", (client,)):
        g = cat_group.get(r[0], "Needs Category")
        if r[1] in out and out[r[1]] != g:
            conflicts.setdefault(r[1], {out[r[1]]}).add(g)
        out.setdefault(r[1], g)
    for i, a in acc.items():
        out.setdefault(i, TYPE_GROUP.get(a["type"], "Not in P&L"))
    return out, conflicts


def ledger_rows(db, client):
    """The journal in the register report's row shape: one row per line that isn't the bank side, cash effect sign
    (money in +). [date, description, payee, account, amount, check_no, entry id, state]"""
    ensure(db)
    acc = _accounts(db, client)
    bank_ids = {r[0] for r in db.execute("SELECT DISTINCT account_id FROM account_map WHERE client=? AND source='bank' AND account_id IS NOT NULL", (client,))}
    rows = []
    q = """SELECT j.date, j.description, j.payee, j.check_no, j.ref_id, j.state, l.account_id, l.cents
           FROM journal j JOIN journal_line l ON l.journal_id=j.id WHERE j.client=? ORDER BY j.date, j.ref_id, l.id"""
    for r in db.execute(q, (client,)):
        if r["account_id"] in bank_ids:
            continue
        rows.append([r["date"], r["description"] or "", r["payee"] or "", acc[r["account_id"]]["full"],
                     round(-r["cents"] / 100, 2), r["check_no"] or "", r["ref_id"], r["state"] or ""])
    return rows


def journal_snapshot(db, client, snap):
    """The register snapshot with its ledger and P&L groups read from the journal instead."""
    acc = _accounts(db, client)
    groups, _ = report_groups(db, client, acc)
    s = dict(snap)
    s["ledger"] = ledger_rows(db, client)
    s["groups"] = {acc[i]["full"]: g for i, g in groups.items() if i in acc}
    s["fromJournal"] = True
    return s


def trial_balance(db, client, as_of=None):
    ensure(db)
    acc = _accounts(db, client)
    q = "SELECT l.account_id, sum(l.cents), count(*) FROM journal j JOIN journal_line l ON l.journal_id=j.id WHERE j.client=?"
    args = [client]
    if as_of:
        q += " AND j.date<=?"
        args.append(as_of)
    out = []
    for aid, c, n in db.execute(q + " GROUP BY l.account_id", args):
        a = acc.get(aid, {"full": "?", "type": "?"})
        out.append(dict(id=aid, account=a["full"], type=a["type"], cls=coa.TYPES.get(a["type"], ("", "?"))[1],
                        debit=round(c / 100, 2) if c > 0 else 0.0, credit=round(-c / 100, 2) if c < 0 else 0.0, lines=n))
    order = {k: i for i, k in enumerate(coa.CLASS_ORDER)}
    out.sort(key=lambda r: (order.get(r["cls"], 9), coa.TYPE_ORDER.index(r["type"]) if r["type"] in coa.TYPE_ORDER else 99, r["account"].lower()))
    return out


def entries(db, client, account_id=None, limit=200):
    """Recent journal entries with their lines (for the ledger page)."""
    ensure(db)
    acc = _accounts(db, client)
    q = "SELECT * FROM journal WHERE client=?"
    args = [client]
    if account_id:
        q += " AND id IN (SELECT journal_id FROM journal_line WHERE client=? AND account_id=?)"
        args += [client, account_id]
    out = []
    for j in db.execute(q + " ORDER BY date DESC, id DESC LIMIT ?", args + [limit]).fetchall():
        lines = [dict(account=acc.get(l["account_id"], {}).get("full", "?"), account_id=l["account_id"],
                      debit=round(l["cents"] / 100, 2) if l["cents"] > 0 else None,
                      credit=round(-l["cents"] / 100, 2) if l["cents"] < 0 else None, memo=l["memo"])
                 for l in db.execute("SELECT * FROM journal_line WHERE journal_id=? ORDER BY id", (j["id"],))]
        out.append(dict(j, lines=lines))
    return out


# ------------------------------------------------------------------ the check: register reports vs journal reports
def compare(db, client, snap, today=None):
    """Run the P&L (by month), balance sheet (each month end + today), cash flow (by month), vendors and the
    transaction list from the register and from the journal. Returns (checks run, [differences])."""
    from . import reports as R
    today = today or dt.date.today()
    js = journal_snapshot(db, client, snap)
    led = snap.get("ledger") or []
    if not led:
        return 0, []
    first = dt.date.fromisoformat(min(r[0] for r in led))
    last = max(today, dt.date.fromisoformat(max(r[0] for r in led)))
    diffs, n = [], 0

    def same(label, a, b):
        nonlocal n
        n += 1
        if a != b:
            where, x, y = first_difference(a, b)
            diffs.append(dict(check=label, where=where, register=_short(x), journal=_short(y)))

    m = first.replace(day=1)
    while m <= last:
        end = R.month_end(m)
        same("P&L %s" % m.strftime("%b %Y"), R.profit_and_loss(snap, m, end), R.profit_and_loss(js, m, end))
        same("Balance sheet %s" % end.isoformat(), R.balance_sheet(snap, end), R.balance_sheet(js, end))
        same("Cash flow %s" % m.strftime("%b %Y"), R.cash_flow(snap, m, end, by_month=False), R.cash_flow(js, m, end, by_month=False))
        m = R.add_months(m, 1)
    same("P&L all time by month", R.profit_and_loss(snap, first, last, by_month=True), R.profit_and_loss(js, first, last, by_month=True))
    same("Balance sheet today", R.balance_sheet(snap, today), R.balance_sheet(js, today))
    same("Vendors all time", R.vendors(snap, first, last, limit=1000), R.vendors(js, first, last, limit=1000))
    same("Vendors by category", R.vendors(snap, first, last, limit=1000, by="category"), R.vendors(js, first, last, limit=1000, by="category"))
    same("Transactions all time", R.transactions(snap, first, last), R.transactions(js, first, last))
    same("Category list", R.categories(snap), R.categories(js))
    return n, diffs


def _short(x, n=400):
    s = repr(x)
    return s if len(s) <= n else s[:n] + "…"


def first_difference(a, b, path=""):
    """Where two report results first differ (for the check page)."""
    if type(a) != type(b):
        return path or "/", a, b
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b), key=str):
            if a.get(k) != b.get(k):
                return first_difference(a.get(k), b.get(k), "%s.%s" % (path, k))
    elif isinstance(a, (list, tuple)):
        for i in range(max(len(a), len(b))):
            x = a[i] if i < len(a) else None
            y = b[i] if i < len(b) else None
            if x != y:
                return first_difference(x, y, "%s[%d]" % (path, i))
    return path or "/", a, b


def enabled(db, client):
    ensure(db)
    r = db.execute("SELECT value FROM coa_meta WHERE client=? AND key='reports_from_journal'", (client,)).fetchone()
    return bool(r and r[0] == "1")


def set_enabled(db, client, on):
    ensure(db)
    db.execute("INSERT OR REPLACE INTO coa_meta (client, key, value) VALUES (?, 'reports_from_journal', ?)", (client, "1" if on else "0"))
    db.commit()
