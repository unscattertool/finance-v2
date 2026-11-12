"""Card deposits split into sales and staff card tips.

The card processor deposits each business day's card sales + card tips in one amount (fees are billed
separately each month), so a deposit matches one day, or a few days paid together, to the penny.
Daily card sales and card tips come from Linga when it is connected, otherwise from the Shifts closeouts.

A matched deposit gets a split: the card tips go to "Card Tips Payable" (owed to staff, a liability) and
the rest stays as card sales. Card tips are paid to staff through Gusto payroll, so each main Gusto run
moves that pay period's card tips out of wages and back off the liability. That part is applied
automatically only when every card deposit for the pay period matched (otherwise the tips aren't known
yet); an editor can always enter the amount from the Gusto payroll report instead.
"""
import datetime as dt
import itertools
import time
import json

from . import cash as C

TIPS_CAT = "Card Tips Payable"
CARD_CAT = "Sales - Card"
WAGES_CAT = "Payroll - Wages"
SCHEMA = """
CREATE TABLE IF NOT EXISTS entry_split (
  id INTEGER PRIMARY KEY, client TEXT NOT NULL, entry_id INTEGER NOT NULL, category TEXT NOT NULL,
  amount REAL NOT NULL, memo TEXT, source TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS entry_split_entry ON entry_split(client, entry_id);
CREATE TABLE IF NOT EXISTS sales_day (
  client TEXT NOT NULL, date TEXT NOT NULL, card_sales REAL, card_tips REAL, cash_sales REAL, cash_tips REAL,
  shifts INTEGER, source TEXT, fetched_at TEXT, PRIMARY KEY (client, date));
CREATE TABLE IF NOT EXISTS deposit_match (
  client TEXT NOT NULL, entry_id INTEGER NOT NULL, days TEXT NOT NULL, card_sales REAL, card_tips REAL,
  source TEXT, matched_at TEXT, fee REAL DEFAULT 0, PRIMARY KEY (client, entry_id));
CREATE TABLE IF NOT EXISTS card_batch (
  client TEXT NOT NULL, settled_at TEXT NOT NULL, business_date TEXT NOT NULL, amount REAL, tips REAL,
  payments INTEGER, first_payment TEXT, last_payment TEXT, kind TEXT, imported_at TEXT, imported_by TEXT,
  PRIMARY KEY (client, settled_at));
CREATE TABLE IF NOT EXISTS payroll_tip (
  client TEXT NOT NULL, entry_id INTEGER NOT NULL, period_start TEXT, period_end TEXT, suggested REAL,
  amount REAL, status TEXT, updated_by TEXT, updated_at TEXT, PRIMARY KEY (client, entry_id));
"""
TOL = 0.02          # dollars
MAX_LAG = 7         # a deposit lands 1-7 days after the business day(s) it pays
MAX_DAYS = 4        # at most this many business days in one deposit


INVESTOR_CAT = "Investor Contributions"


FEES_CAT = "Merchant Processing Fees"
FEE_MAX = 0.045     # a processor may keep up to 4.5% of a batch as fees and deposit the rest


def ensure(db, client=None):
    db.executescript(SCHEMA)
    if "fee" not in [r[1] for r in db.execute("PRAGMA table_info(deposit_match)")]:
        db.execute("ALTER TABLE deposit_match ADD COLUMN fee REAL DEFAULT 0")
    if client:
        for name, sort in ((TIPS_CAT, 900), (INVESTOR_CAT, 905)):
            if not db.execute("SELECT 1 FROM category WHERE client=? AND name=?", (client, name)).fetchone():
                db.execute("INSERT INTO category(client,name,pl_group,sort) VALUES(?,?,?,?)", (client, name, "Not in P&L", sort))
                db.commit()


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _d(s):
    return dt.date.fromisoformat(s)


# ------------------------------------------------------------------ daily sales and tips
def refresh_days(db, client, shifts_path, start, end):
    """Copy each business day's card sales/tips from Shifts. Days already loaded from Linga are left alone."""
    days = C.shifts_by_day(shifts_path, start, end)
    linga = {r[0] for r in db.execute("SELECT date FROM sales_day WHERE client=? AND source IN ('linga','batch','processor','pos')", (client,))}
    ts = _now()
    for d, x in days.items():
        if d in linga:
            continue
        card_sales = round(max(x["sales"] - x["cash_expected"], 0), 2)
        db.execute("""INSERT INTO sales_day(client,date,card_sales,card_tips,cash_sales,cash_tips,shifts,source,fetched_at)
                      VALUES(?,?,?,?,?,?,?,'shifts',?) ON CONFLICT(client,date) DO UPDATE SET card_sales=excluded.card_sales,
                      card_tips=excluded.card_tips, cash_sales=excluded.cash_sales, cash_tips=excluded.cash_tips,
                      shifts=excluded.shifts, fetched_at=excluded.fetched_at""",
                   (client, d, card_sales, round(x["card_tips"], 2), round(x["cash_expected"], 2), round(x["cash_tips"], 2), x["shifts"], ts))
    db.commit()


LINGA_PULLS_PER_DAY = 3  # owner's rule: Linga is only pulled by hand, at most 3 times a day


def _pull_key():
    return "linga_pulls_" + dt.date.today().isoformat()


def linga_pulls_today(db, client):
    r = db.execute("SELECT v FROM meta WHERE client=? AND k=?", (client, _pull_key())).fetchone()
    return int(r[0]) if r else 0


def _count_linga_pull(db, client):
    db.execute("INSERT INTO meta(client,k,v) VALUES(?,?,?) ON CONFLICT(client,k) DO UPDATE SET v=excluded.v",
               (client, _pull_key(), str(linga_pulls_today(db, client) + 1)))
    db.commit()


# ------------------------------------------------------------------ Linga batch report (CSV export)
def import_batch_report(db, client, text, user):
    """Linga back office → Batch Report (CSV). Each batch is what the processor deposits as one amount.
    Batches are stored and summed into sales_day by business date (a batch settled before noon belongs to the
    day before). Re-importing the same or an overlapping report replaces those batches."""
    import csv
    import io
    P = lambda v: dt.datetime.strptime(v.strip(), "%m-%d-%Y %I:%M %p")
    num = lambda v: float(v.replace(",", "")) if v and v not in ("<nil>",) else 0.0
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    head = next((i for i, r in enumerate(rows) if r[:4] == ["Check Number", "Card Number", "Transaction", "Amount"]), None)
    if head is None:
        raise ValueError("This doesn't look like a Linga Batch Report (no 'Check Number, Card Number, Transaction, Amount' header).")
    ix = {k: i for i, k in enumerate(rows[head])}
    batches, per_server = {}, {}
    for r in rows[head + 1:]:
        if len(r) < len(ix) or not r[ix["Batch settled date"]].strip():
            continue
        kind = r[ix["Transaction"]].strip().lower()
        if kind not in ("payment", "refund"):
            continue
        sign = -1 if kind == "refund" else 1
        settled = P(r[ix["Batch settled date"]])
        paid = P(r[ix["Date"]])
        b = batches.setdefault(settled, dict(amount=0.0, tips=0.0, n=0, first=paid, last=paid, kind=r[ix["Type"]].strip()))
        b["amount"] += sign * num(r[ix["Amount"]])
        b["tips"] += sign * num(r[ix["Tip"]])
        b["n"] += 1
        b["first"], b["last"] = min(b["first"], paid), max(b["last"], paid)
        if "Server Name" in ix:
            bday = ((settled.date() - dt.timedelta(days=1)) if settled.hour < 12 else settled.date()).isoformat()
            x = per_server.setdefault((bday, r[ix["Server Name"]].strip() or "(no server)"), [0.0, 0.0, 0])
            x[0] += sign * num(r[ix["Amount"]]); x[1] += sign * num(r[ix["Tip"]]); x[2] += 1
    if not batches:
        raise ValueError("No settled card payments found in that file.")
    ts = _now()
    for settled, b in batches.items():
        bday = (settled.date() - dt.timedelta(days=1)) if settled.hour < 12 else settled.date()
        db.execute("""INSERT OR REPLACE INTO card_batch(client,settled_at,business_date,amount,tips,payments,first_payment,last_payment,
                      kind,imported_at,imported_by) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                   (client, settled.isoformat(timespec="minutes"), bday.isoformat(), round(b["amount"], 2), round(b["tips"], 2), b["n"],
                    b["first"].isoformat(timespec="minutes"), b["last"].isoformat(timespec="minutes"), b["kind"], ts, user))
    days = {}
    for r in db.execute("SELECT business_date, sum(amount), sum(tips), sum(payments) FROM card_batch WHERE client=? GROUP BY business_date", (client,)):
        days[r[0]] = (round(r[1], 2), round(r[2], 2), r[3])
    for d, (amt, tips, n) in days.items():
        db.execute("""INSERT INTO sales_day(client,date,card_sales,card_tips,cash_sales,cash_tips,shifts,source,fetched_at)
                      VALUES(?,?,?,?,0,0,?,'batch',?) ON CONFLICT(client,date) DO UPDATE SET card_sales=excluded.card_sales,
                      card_tips=excluded.card_tips, shifts=excluded.shifts, source='batch', fetched_at=excluded.fetched_at""",
                   (client, d, amt, tips, n, ts))
    db.commit()
    if per_server:
        from . import tipcheck
        tipcheck.save_batch_servers(db, client, per_server)
    first, last = min(batches), max(batches)
    return dict(batches=len(batches), days=len({(s.date() - dt.timedelta(days=1)) if s.hour < 12 else s.date() for s in batches}),
                first=first.date().isoformat(), last=last.date().isoformat(),
                amount=round(sum(b["amount"] for b in batches.values()), 2), tips=round(sum(b["tips"] for b in batches.values()), 2))


def refresh_linga(db, client, c, start, end, recent=3, limit=None):
    """Load business days from Linga into sales_day (source 'linga'). Days already loaded are skipped,
    except the last `recent` days, which are re-read in case checks were closed late. Returns days loaded."""
    from . import linga
    store, key = linga.keys(c)
    if not store:
        return 0
    blocked = db.execute("SELECT v FROM meta WHERE client=? AND k='linga_blocked_until'", (client,)).fetchone()
    if blocked and blocked[0] > _now():
        return 0
    if linga_pulls_today(db, client) >= LINGA_PULLS_PER_DAY:
        return 0
    _count_linga_pull(db, client)
    have = {r[0] for r in db.execute("SELECT date FROM sales_day WHERE client=? AND source IN ('linga','batch')", (client,))}
    todo, d = [], start
    while d <= end:
        if d.isoformat() not in have or (end - d).days < recent:
            todo.append(d)
        d += dt.timedelta(days=1)
    todo.sort(reverse=True)  # newest first, so a capped run covers the days that matter now
    if limit:
        todo = todo[:limit]
    ts = _now()
    done = 0
    # group the days to load into date ranges of up to 31 days: one Linga request per range
    todo.sort()
    ranges, cur = [], []
    for d in todo:
        if cur and ((d - cur[-1]).days > 1 or (d - cur[0]).days >= 31):
            ranges.append(cur)
            cur = []
        cur.append(d)
    if cur:
        ranges.append(cur)
    for rg in sorted(ranges, key=lambda r: r[-1], reverse=True):  # newest first
        try:
            days = linga.range_totals(store, key, rg[0], rg[-1])
        except linga.QuotaExceeded as e:
            db.execute("INSERT INTO meta(client,k,v) VALUES(?,?,?) ON CONFLICT(client,k) DO UPDATE SET v=excluded.v",
                       (client, "linga_blocked_until", e.until.isoformat(timespec="seconds")))
            db.commit()
            break
        if len(rg) >= 3 and not any(x["checks"] for x in days.values()):
            break  # an empty answer for several days is more likely a Linga problem than closed days: keep what we have
        for day, x in days.items():
            old = db.execute("SELECT shifts, source FROM sales_day WHERE client=? AND date=?", (client, day)).fetchone()
            if not x["checks"] and old and old["source"] == "linga" and (old["shifts"] or 0) > 0:
                continue  # never wipe a day Linga already gave us
            db.execute("""INSERT INTO sales_day(client,date,card_sales,card_tips,cash_sales,cash_tips,shifts,source,fetched_at)
                          VALUES(?,?,?,?,?,?,?,'linga',?) ON CONFLICT(client,date) DO UPDATE SET card_sales=excluded.card_sales,
                          card_tips=excluded.card_tips, cash_sales=excluded.cash_sales, cash_tips=excluded.cash_tips,
                          shifts=excluded.shifts, source='linga', fetched_at=excluded.fetched_at""",
                       (client, day, x["card_sales"], x["card_tips"], x["cash_sales"], x["cash_tips"], x["checks"], ts))
            done += 1
        db.commit()
        time.sleep(1)
    return done


def _day_total(r):
    return round((r["card_sales"] or 0) + (r["card_tips"] or 0), 2)


# ------------------------------------------------------------------ splits
def set_splits(db, client, entry_id, parts, source):
    """Replace this source's splits on an entry. parts = [(category, amount, memo)]."""
    db.execute("DELETE FROM entry_split WHERE client=? AND entry_id=? AND source=?", (client, entry_id, source))
    ts = _now()
    for cat, amt, memo in parts:
        if abs(amt) >= 0.005:
            db.execute("INSERT INTO entry_split(client,entry_id,category,amount,memo,source,created_at) VALUES(?,?,?,?,?,?,?)",
                       (client, entry_id, cat, round(amt, 2), memo, source, ts))


def splits_by_entry(db, client):
    out = {}
    for r in db.execute("SELECT entry_id, category, amount, memo FROM entry_split WHERE client=? ORDER BY id", (client,)):
        out.setdefault(r["entry_id"], []).append((r["category"], r["amount"], r["memo"]))
    return out


# ------------------------------------------------------------------ card deposits
def card_deposits(db, client, since):
    return [dict(r) for r in db.execute(
        """SELECT id, date, description, amount FROM entry WHERE client=? AND status!='void' AND amount>0
           AND category=? AND date>=? ORDER BY date, id""", (client, CARD_CAT, since))]


def _is_processor(dep):
    from .books import PROCESSOR_PATTERNS
    return any(p in (dep.get("description") or "").upper() for p in PROCESSOR_PATTERNS)


def _parts(tips, fee, days):
    out = [(TIPS_CAT, tips, "Card tips for " + _label(days))]
    if fee:
        out.append((FEES_CAT, -fee, "Processor fees kept from " + _label(days)))
    return out


def _label(days):
    return " + ".join(_d(x).strftime("%a %-m/%-d") for x in days)


def fix_processor_entries(db, client, since):
    """Heartland ACH items that arrived uncategorized (e.g. still pending at the bank): deposits → card sales, debits → fees."""
    from .books import processor_category, NEEDS
    for e in db.execute("""SELECT id, description, amount FROM entry WHERE client=? AND date>=? AND status!='void'
                           AND coalesce(category,?)=? AND updated_by IS NULL""", (client, since, NEEDS, NEEDS)).fetchall():
        payee, cat = processor_category(e["description"], e["amount"])
        if cat:
            db.execute("UPDATE entry SET category=?, payee=coalesce(payee,?) WHERE id=?", (cat, payee, e["id"]))
    db.commit()


def match_deposits(db, client, since):
    """Match card deposits to business days and split each into sales + card tips (and processor fees when the
    processor deposits the batch net of its fees)."""
    fix_processor_entries(db, client, since)
    days = {r["date"]: dict(r) for r in db.execute("SELECT * FROM sales_day WHERE client=?", (client,))}
    existing = {r["entry_id"]: dict(r) for r in db.execute("SELECT * FROM deposit_match WHERE client=?", (client,))}
    deps = card_deposits(db, client, since)
    by_id = {d["id"]: d for d in deps}
    used = set()

    def drop(eid):
        db.execute("DELETE FROM deposit_match WHERE client=? AND entry_id=?", (client, eid))
        set_splits(db, client, eid, [], "card-tips")
        existing.pop(eid)

    # keep matches that still add up (a closeout edited later, or a deposit recategorized, breaks one)
    for eid, m in list(existing.items()):
        dd = json.loads(m["days"])
        dep = by_id.get(eid)
        if dep is None:
            e = db.execute("SELECT * FROM entry WHERE id=? AND client=?", (eid, client)).fetchone()
            if not e or e["status"] == "void" or e["category"] != CARD_CAT or e["amount"] <= 0:
                drop(eid)
            else:
                used.update(dd)  # older than the window: leave as is
            continue
        fee = m.get("fee") or 0.0
        if not all(x in days for x in dd) or abs(sum(_day_total(days[x]) for x in dd) - fee - dep["amount"]) > TOL:
            drop(eid)
            continue
        used.update(dd)
        tips = round(sum(days[x]["card_tips"] or 0 for x in dd), 2)
        sales = round(dep["amount"] - tips + fee, 2)
        db.execute("UPDATE deposit_match SET card_sales=?, card_tips=?, source=? WHERE client=? AND entry_id=?",
                   (sales, tips, "+".join(sorted({days[x]["source"] for x in dd})), client, eid))
        set_splits(db, client, eid, _parts(tips, fee, dd), "card-tips")
    for dep in deps:
        if dep["id"] in existing:
            continue
        dd_ = _d(dep["date"])
        cand = sorted(d for d, r in days.items() if d not in used and _day_total(r) > 0
                      and 0 < (dd_ - _d(d)).days <= MAX_LAG)
        hit = None
        for n in range(1, MAX_DAYS + 1):
            for combo in itertools.combinations(cand, n):
                if abs(sum(_day_total(days[x]) for x in combo) - dep["amount"]) <= TOL:
                    hit = combo
                    break
            if hit:
                break
        fee = 0.0
        if not hit and _is_processor(dep):
            # Heartland may deposit a day's batch minus its fees: the earliest open day the deposit is just under
            for d in cand:
                tot = _day_total(days[d])
                if 0 < tot - dep["amount"] <= FEE_MAX * tot:
                    hit, fee = (d,), round(tot - dep["amount"], 2)
                    break
        if not hit:
            continue
        used.update(hit)
        tips = round(sum(days[x]["card_tips"] or 0 for x in hit), 2)
        db.execute("INSERT OR REPLACE INTO deposit_match(client,entry_id,days,card_sales,card_tips,source,matched_at,fee) VALUES(?,?,?,?,?,?,?,?)",
                   (client, dep["id"], json.dumps(list(hit)), round(dep["amount"] - tips + fee, 2), tips,
                    "+".join(sorted({days[x]["source"] for x in hit})), _now(), fee))
        set_splits(db, client, dep["id"], _parts(tips, fee, hit), "card-tips")
    db.commit()


# ------------------------------------------------------------------ tips paid through Gusto
PERIOD_END = "2026-10-11"  # any known pay-period end date; periods are 14 days, aligned to it


def pay_period(paid_on, anchor=PERIOD_END):
    """The biweekly pay period a Gusto debit pays: the latest period that ended at least 2 days before it."""
    a = dt.date.fromisoformat(anchor)
    latest = paid_on - dt.timedelta(days=2)
    end = a + dt.timedelta(days=14 * ((latest - a).days // 14))
    return end - dt.timedelta(days=13), end


def gusto_runs(db, client, since, anchor=PERIOD_END):
    rows = [dict(r) for r in db.execute(
        """SELECT id, date, description, amount FROM entry WHERE client=? AND status!='void' AND amount<0
           AND category=? AND upper(coalesce(description,'')) LIKE '%GUSTO/NET%' AND date>=? ORDER BY date""",
        (client, WAGES_CAT, since))]
    by_period = {}
    for r in rows:
        a, b = pay_period(_d(r["date"]), anchor)
        r["period"] = (a.isoformat(), b.isoformat())
        by_period.setdefault(r["period"], []).append(r)
    for runs in by_period.values():  # the biggest run in a period is the regular payroll; others are off-cycle
        main = max(runs, key=lambda x: -x["amount"])
        for r in runs:
            r["main"] = r is main
    return rows


def apply_payroll(db, client, since, anchor=PERIOD_END):
    days = {r["date"]: dict(r) for r in db.execute("SELECT * FROM sales_day WHERE client=?", (client,))}
    matched_days = set()
    for r in db.execute("SELECT days FROM deposit_match WHERE client=?", (client,)):
        matched_days.update(json.loads(r["days"]))
    saved = {r["entry_id"]: dict(r) for r in db.execute("SELECT * FROM payroll_tip WHERE client=?", (client,))}
    from . import gusto as G
    covered = G.covered_until(db, client) if G.has_runs(db, client) else None
    if covered:
        covered = (_d(covered) + dt.timedelta(days=10)).isoformat()
    out = []
    for run in gusto_runs(db, client, since, anchor):
        if covered and run["date"] <= covered:  # the Gusto journal gives the real tips for these payrolls
            set_splits(db, client, run["id"], [], "payroll-tips")
            continue
        a, b = run["period"]
        pdays = [d for d in days if a <= d <= b and _day_total(days[d]) > 0]
        tips = round(sum(days[d]["card_tips"] or 0 for d in pdays), 2) if run["main"] else 0.0
        missing = [d for d in pdays if d not in matched_days]
        ready = run["main"] and pdays and not missing
        s = saved.get(run["id"])
        if s and s["status"] == "confirmed":
            amount, status = s["amount"], "confirmed"
        elif ready:
            amount, status = tips, "auto"
        else:
            amount, status = 0.0, ("waiting" if run["main"] else "off-cycle")
        amount = min(amount, -run["amount"])
        db.execute("""INSERT INTO payroll_tip(client,entry_id,period_start,period_end,suggested,amount,status,updated_by,updated_at)
                      VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(client,entry_id) DO UPDATE SET period_start=excluded.period_start,
                      period_end=excluded.period_end, suggested=excluded.suggested, amount=excluded.amount, status=excluded.status""",
                   (client, run["id"], a, b, tips, amount, status, (s or {}).get("updated_by"), (s or {}).get("updated_at") or _now()))
        set_splits(db, client, run["id"], [(TIPS_CAT, -amount, "Card tips paid in payroll %s–%s" % (_d(a).strftime("%-m/%-d"), _d(b).strftime("%-m/%-d")))],
                   "payroll-tips")
        out.append(dict(run, net_pay=round(-run["amount"], 2), suggested=tips, tips=amount, status=status, missing=missing, start=a, end=b))
    db.commit()
    return out


def confirm_payroll(db, client, entry_id, amount, user):
    db.execute("UPDATE payroll_tip SET amount=?, status=?, updated_by=?, updated_at=? WHERE client=? AND entry_id=?",
               (amount, "confirmed" if amount is not None else "auto", user, _now(), client, entry_id))
    db.commit()


# ------------------------------------------------------------------ everything together
def run(db, client, shifts_path, since="2026-04-27", today=None, anchor=PERIOD_END):
    today = today or dt.date.today()
    ensure(db, client)
    if shifts_path:
        refresh_days(db, client, shifts_path, _d(since) - dt.timedelta(days=MAX_LAG), today)
    match_deposits(db, client, since)
    try:  # the sales tax inside each sales deposit → Sales Tax Payable / Alc Sales Tax
        from . import salestax
        salestax.apply(db, client, shifts_path, since)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("sales tax split failed for %s: %s", client, e)
    out = apply_payroll(db, client, since, anchor)
    from . import gusto as G
    if G.has_runs(db, client):
        G.apply(db, client, since)
    return out


BATCH_SOURCES = ("batch", "processor", "pos")


def pending_batches(db, client, days_back=30, today=None):
    """Card money the batch reports say is coming that hasn't reached the bank yet: business days whose card sales + tips
    come from a batch report (Linga batch, processor, Heartland POS) and aren't tied to a bank deposit."""
    today = today or dt.date.today()
    start = (today - dt.timedelta(days=days_back)).isoformat()
    used = set()
    for r in db.execute("SELECT days FROM deposit_match WHERE client=?", (client,)):
        used.update(json.loads(r[0]))
    rows = [dict(r, total=_day_total(r)) for r in db.execute(
        "SELECT * FROM sales_day WHERE client=? AND date>=? AND date<=? AND source IN (%s) ORDER BY date" % ",".join("?" * len(BATCH_SOURCES)),
        (client, start, today.isoformat(), *BATCH_SOURCES))]
    rows = [r for r in rows if r["date"] not in used and r["total"] > 0]
    oldest = rows[0]["date"] if rows else None
    late = [r for r in rows if (today - _d(r["date"])).days > MAX_LAG]
    return dict(days=rows, total=round(sum(r["total"] for r in rows), 2), sales=round(sum(r["card_sales"] or 0 for r in rows), 2),
                tips=round(sum(r["card_tips"] or 0 for r in rows), 2), oldest=oldest, late=late)


def report(db, client, shifts_path, since="2026-04-27", days_back=75, today=None, anchor=PERIOD_END):
    """Everything the Card deposits & tips page shows."""
    today = today or dt.date.today()
    runs = run(db, client, shifts_path, since, today, anchor)
    from . import gusto as G
    gusto = [r for r in G.reconcile(db, client) if (r["period_end"] or r["pay_day"]) >= since] if G.has_runs(db, client) else []
    start = (today - dt.timedelta(days=days_back)).isoformat()
    days = {r["date"]: dict(r) for r in db.execute("SELECT * FROM sales_day WHERE client=?", (client,))}
    matches = {r["entry_id"]: dict(r) for r in db.execute("SELECT * FROM deposit_match WHERE client=?", (client,))}
    used = set()
    for m in matches.values():
        used.update(json.loads(m["days"]))
    rows = []
    for dep in card_deposits(db, client, start):
        m = matches.get(dep["id"])
        if m:
            dd = json.loads(m["days"])
            rows.append(dict(dep, matched=True, covers=_label(dd), sales=m["card_sales"], tips=m["card_tips"], source=m["source"]))
        else:
            dd_ = _d(dep["date"])
            near = sorted(d for d in days if d not in used and _day_total(days[d]) > 0 and 0 < (dd_ - _d(d)).days <= MAX_LAG)
            rows.append(dict(dep, matched=False, near=[(d, _day_total(days[d])) for d in near]))
    rows.reverse()
    unmatched = [r for r in rows if not r["matched"]]
    liability = db.execute("""SELECT round(coalesce(sum(s.amount),0),2) FROM entry_split s JOIN entry e ON e.id=s.entry_id
                              WHERE s.client=? AND s.category=? AND e.status!='void'""", (client, TIPS_CAT)).fetchone()[0]
    collected = db.execute("""SELECT round(coalesce(sum(s.amount),0),2) FROM entry_split s JOIN entry e ON e.id=s.entry_id
                              WHERE s.client=? AND s.category=? AND s.amount>0 AND e.status!='void' AND e.date>=?""",
                           (client, TIPS_CAT, start)).fetchone()[0]
    # business days with no deposit matched yet: the closeouts most likely to be incomplete
    open_days = [dict(days[d], total=_day_total(days[d])) for d in sorted(days, reverse=True)
                 if start <= d <= today.isoformat() and d not in used and _day_total(days[d]) > 0]
    return dict(
        pending=pending_batches(db, client, today=today),
        deposits=rows, unmatched_count=len(unmatched), unmatched_total=round(sum(r["amount"] for r in unmatched), 2),
        matched_count=len(rows) - len(unmatched), liability=liability, collected=collected,
        payroll=[r for r in reversed(runs) if r["date"] >= start], open_days=open_days[:40],
        period_end=anchor, gusto=list(reversed(gusto)),
        gusto_tips=round(sum(r["tips"] for r in gusto), 2), gusto_cash_tips=round(sum(r["cash_tips"] or 0 for r in gusto), 2),
        gusto_collected=round(sum(r["collected"] or 0 for r in gusto), 2),
        source=("Linga batch report" if any(r["source"] == "batch" for r in days.values())
                else "POS reports" if any(r["source"] in ("pos", "processor") for r in days.values())
                else "Linga" if any(r["source"] == "linga" for r in days.values()) else "Shifts closeouts"),
        last_batch=db.execute("SELECT max(settled_at), max(imported_at) FROM card_batch WHERE client=?", (client,)).fetchone(),
        linga_pulls=linga_pulls_today(db, client), linga_max=LINGA_PULLS_PER_DAY,
    )


def period_status(db, client, a, b):
    """For the income widget: how many business days in a..b tie to a card deposit to the penny."""
    days = [r["date"] for r in db.execute("SELECT * FROM sales_day WHERE client=? AND date BETWEEN ? AND ?", (client, a.isoformat(), b.isoformat()))
            if _day_total(r) > 0]
    used = set()
    for r in db.execute("SELECT days FROM deposit_match WHERE client=?", (client,)):
        used.update(json.loads(r["days"]))
    matched_ids = {r[0] for r in db.execute("SELECT entry_id FROM deposit_match WHERE client=?", (client,))}
    deps = [d for d in card_deposits(db, client, a.isoformat()) if d["date"] <= (b + dt.timedelta(days=MAX_LAG)).isoformat()
            and d["id"] not in matched_ids]
    return dict(days=len(days), matched=sum(1 for d in days if d in used),
                unmatched_deposits=len(deps), unmatched_amount=round(sum(d["amount"] for d in deps), 2))
