"""Cash tracking and the sales/tips split.

Sales, tips and cash per shift come from WayWard Shifts (read-only copy of its database on this server).
Finance users record till counts and the cash deposits they take to the bank; each deposit is matched to
the bank's deposit, and each count is compared with what the till should hold.

Till math (11/5, owner): the till carries from day to day.
  start of a day   = the starting till entered for that day, else the Heartland cash drawer report's starting cash,
                     else estimated: the last close (count, or what the till should have held) minus cash taken out since
  close of a day   = start + the day's cash sales (POS) − receipts paid out of the drawer on Shifts closeouts
  a till count     = what was really there at close; the difference is unaccounted cash (over / short)
  after the count  = cash taken out (owner draw, weekend pull …) and deposits from the till dated that day come out,
                     leaving the next day's starting till (count $795, take out $700 → $95 left).
"""
import datetime as dt
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS cash_count (
  id INTEGER PRIMARY KEY, client TEXT NOT NULL, date TEXT NOT NULL, amount REAL NOT NULL, note TEXT,
  created_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS cash_start (
  id INTEGER PRIMARY KEY, client TEXT NOT NULL, date TEXT NOT NULL, amount REAL NOT NULL, note TEXT,
  created_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS cash_deposit (
  id INTEGER PRIMARY KEY, client TEXT NOT NULL, date TEXT NOT NULL, amount REAL NOT NULL, note TEXT,
  entry_id INTEGER, created_by TEXT, created_at TEXT);
"""
CASH_DEPOSIT_CATS = ("Sales - Cash", "Uncategorized")


def ensure(db):
    db.executescript(SCHEMA)


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ Shifts data
def shifts_by_day(path, a, b):
    """{date: {sales, cash_expected, turned_in, paid_out, due, card_tips, cash_tips, shifts}} from Shifts (read-only)."""
    out = {}
    if not path:
        return out
    try:
        s = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        rows = s.execute("""SELECT shift_date, gross_sales_cents, cash_expected_cents, cash_drop_cents, cash_due_cents,
                                   cc_tips_cents, cash_tips_cents,
                                   COALESCE((SELECT SUM(amount_cents) FROM cash_paid_outs p WHERE p.shift_id = shifts.id), 0)
                            FROM shifts WHERE shift_date BETWEEN ? AND ?""", (a.isoformat(), b.isoformat())).fetchall()
        s.close()
    except sqlite3.Error:
        return out
    for d, sales, exp, drop, due, cct, casht, paid in rows:
        x = out.setdefault(d, dict(sales=0.0, cash_expected=0.0, turned_in=0.0, paid_out=0.0, due=0.0, card_tips=0.0, cash_tips=0.0, shifts=0))
        x["sales"] += (sales or 0) / 100
        x["cash_expected"] += (exp or 0) / 100
        x["turned_in"] += (drop or 0) / 100
        x["paid_out"] += (paid or 0) / 100
        x["due"] += (due or 0) / 100
        x["card_tips"] += (cct or 0) / 100
        x["cash_tips"] += (casht or 0) / 100
        x["shifts"] += 1
    return out


def income_split(path, db, client, a, b):
    """Sales vs tips for a period, plus what the bank shows for the same period."""
    days = shifts_by_day(path, a, b)
    t = {k: round(sum(d[k] for d in days.values()), 2) for k in ("sales", "cash_expected", "card_tips", "cash_tips")}
    card_sales = max(t["sales"] - t["cash_expected"], 0)
    bank = {r[0]: r[1] for r in db.execute(
        """SELECT category, round(sum(amount), 2) FROM entry WHERE client=? AND status!='void' AND amount>0
           AND date BETWEEN ? AND ? AND category IN ('Sales - Card','Sales - Cash') GROUP BY category""",
        (client, a.isoformat(), b.isoformat()))}
    tips = t["card_tips"] + t["cash_tips"]
    total = t["sales"] + tips
    return dict(start=a.isoformat(), end=b.isoformat(), shifts=sum(d["shifts"] for d in days.values()), has_data=bool(days),
                sales=t["sales"], card_sales=round(card_sales, 2), cash_sales=t["cash_expected"],
                card_tips=t["card_tips"], cash_tips=t["cash_tips"], tips=round(tips, 2), total=round(total, 2),
                bank_card=bank.get("Sales - Card", 0.0), bank_cash=bank.get("Sales - Cash", 0.0),
                card_expected=round(card_sales + t["card_tips"], 2))


# ------------------------------------------------------------------ till and deposits
def add_count(db, client, user, date, amount, note):
    db.execute("INSERT INTO cash_count(client,date,amount,note,created_by,created_at) VALUES(?,?,?,?,?,?)",
               (client, date, round(amount, 2), note, user, _now()))
    db.commit()


def add_start(db, client, user, date, amount, note):
    db.execute("DELETE FROM cash_start WHERE client=? AND date=?", (client, date))
    db.execute("INSERT INTO cash_start(client,date,amount,note,created_by,created_at) VALUES(?,?,?,?,?,?)",
               (client, date, round(amount, 2), note, user, _now()))
    db.commit()


def add_deposit(db, client, user, date, amount, note, entry_id=None):
    db.execute("INSERT INTO cash_deposit(client,date,amount,note,entry_id,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
               (client, date, round(amount, 2), note, entry_id, user, _now()))
    db.commit()
    match_deposits(db, client)


def delete_item(db, client, kind, item_id):
    t = {"count": "cash_count", "deposit": "cash_deposit", "start": "cash_start"}.get(kind)
    if t:
        db.execute("DELETE FROM %s WHERE client=? AND id=?" % t, (client, item_id))
        db.commit()


def match_deposits(db, client):
    """Link each recorded deposit to a bank deposit of the same amount within 7 days (each bank item used once)."""
    used = {r[0] for r in db.execute("SELECT entry_id FROM cash_deposit WHERE client=? AND entry_id IS NOT NULL", (client,))}
    for dep in db.execute("SELECT * FROM cash_deposit WHERE client=? AND entry_id IS NULL ORDER BY date", (client,)).fetchall():
        for e in db.execute("""SELECT id, date FROM entry WHERE client=? AND status!='void' AND amount>0 AND abs(amount-?)<0.005
                               AND category IN (%s) AND upper(coalesce(description,'')) NOT LIKE '%%BANKCARD%%'
                               AND date BETWEEN date(?, '-2 day') AND date(?, '+7 day') ORDER BY date""" % ",".join("?" * len(CASH_DEPOSIT_CATS)),
                            (client, dep["amount"], *CASH_DEPOSIT_CATS, dep["date"], dep["date"])):
            if e["id"] not in used:
                db.execute("UPDATE cash_deposit SET entry_id=? WHERE id=?", (e["id"], dep["id"]))
                used.add(e["id"])
                break
    db.commit()


def drawer_starts(path):
    """{date: starting cash of the day's first Heartland cash drawer session} (Shifts' POS import, read-only)."""
    out = {}
    if not path:
        return out
    try:
        s = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        for d, opened, start in s.execute("SELECT business_date, opened, starting_cash FROM pos_drawer ORDER BY opened"):
            if d and d not in out and start is not None:
                out[d] = round(start, 2)
        s.close()
    except sqlite3.Error:
        pass
    return out


def till(db, client, path, today=None, days=60):
    """Everything the Cash page shows."""
    from . import salesum
    today = today or dt.date.today()
    ensure(db)
    match_deposits(db, client)
    counts = [dict(r) for r in db.execute("SELECT * FROM cash_count WHERE client=? ORDER BY date, id", (client,))]
    starts = {r["date"]: dict(r) for r in db.execute("SELECT * FROM cash_start WHERE client=? ORDER BY date, id", (client,))}
    deps = [dict(r) for r in db.execute(
        """SELECT d.*, e.date AS bank_date, e.description AS bank_desc FROM cash_deposit d
           LEFT JOIN entry e ON e.id = d.entry_id WHERE d.client=? ORDER BY d.date, d.id""", (client,))]
    try:
        payouts = [dict(r) for r in db.execute("SELECT * FROM cash_payout WHERE client=? ORDER BY date, id", (client,))]
    except sqlite3.OperationalError:
        payouts = []
    first = [dt.date.fromisoformat(c["date"]) for c in counts] + [dt.date.fromisoformat(d) for d in starts]
    start = min(first + [today - dt.timedelta(days=days)])
    by_day = shifts_by_day(path, start, today)
    cash = {d: v for d, v in salesum.cash_by_day(db, client, path, start, today).items() if start.isoformat() <= d <= today.isoformat()}
    drawer = drawer_starts(path)
    count_on = {}
    for cnt in counts:
        count_on[cnt["date"]] = cnt  # the last count of a day wins
    outs_on = {}
    for x in payouts:
        outs_on.setdefault(x["date"], []).append(dict(kind="payout", amount=x["amount"], label="%s%s" % (x.get("reason") or "Taken out", " · " + x["party"] if x.get("party") else ""), id=x["id"]))
    for x in deps:
        outs_on.setdefault(x["date"], []).append(dict(kind="deposit", amount=x["amount"], label="Deposit to the bank", id=x["id"]))

    dates = sorted({d for d in list(cash) + list(by_day) + list(count_on) + list(starts) + list(outs_on) + list(drawer)
                    if start.isoformat() <= d <= today.isoformat()})
    running, rows, flags = None, [], []
    for d in dates:
        x = by_day.get(d, {})
        est = round(running, 2) if running is not None else None
        st, src = None, None
        if d in starts:
            st, src = starts[d]["amount"], "entered"
        elif d in drawer:
            st, src = drawer[d], "drawer report"
        sales = round(cash.get(d, 0.0), 2)
        receipts = round(x.get("paid_out", 0.0), 2)
        if st is None and est is None and d not in count_on:
            continue  # nothing known yet
        open_day = st if st is not None else est
        start_var = round(st - est, 2) if st is not None and est is not None else None
        if start_var is not None and abs(start_var) > 0.005:
            flags.append(dict(date=d, kind="start", amount=start_var, expected=est, actual=st,
                              text="Starting till %s (%s) vs %s left after the last close" % (_m(st), src, _m(est))))
        close_exp = round((open_day or 0) + sales - receipts, 2) if open_day is not None else None
        cnt = count_on.get(d)
        var = round(cnt["amount"] - close_exp, 2) if cnt and close_exp is not None else None
        if var is not None and abs(var) > 0.005:
            flags.append(dict(date=d, kind="count", amount=var, expected=close_exp, actual=cnt["amount"],
                              text="Counted %s at close, the till should have held %s" % (_m(cnt["amount"]), _m(close_exp))))
        after = cnt["amount"] if cnt else close_exp
        outs = outs_on.get(d, [])
        out_total = round(sum(o["amount"] for o in outs), 2)
        left = round(after - out_total, 2) if after is not None else None
        rows.append(dict(date=d, start=open_day, start_src=src or ("estimated" if est is not None else None), start_id=starts[d]["id"] if d in starts else None,
                         start_var=start_var, sales=sales, receipts=receipts, close=close_exp, counted=cnt["amount"] if cnt else None,
                         count_id=cnt["id"] if cnt else None, variance=var, outs=outs, out_total=out_total, left=left,
                         note=(cnt or {}).get("note") or (starts.get(d) or {}).get("note")))
        running = left

    last = counts[-1] if counts else None
    since = [r for r in rows if last and r["date"] > last["date"]]
    last_row = next((r for r in rows if last and r["date"] == last["date"]), None)
    in_since = round(sum(r["sales"] for r in since), 2)
    paid_since = round(sum(r["receipts"] for r in since), 2)
    out_since = round(sum(r["out_total"] for r in since) + (last_row["out_total"] if last_row else 0), 2)
    cutoff = (today - dt.timedelta(days=days)).isoformat()
    unaccounted = [f for f in flags if f["date"] >= cutoff]
    prev = counts[-2]["date"] if len(counts) > 1 else None
    recent = [f for f in flags if prev is None or f["date"] > prev]  # since the count before the last one

    # bank cash deposits nobody has recorded yet (last 60 days)
    linked = {x["entry_id"] for x in deps if x["entry_id"]}
    unrecorded = [dict(r) for r in db.execute(
        """SELECT id, date, description, amount, category FROM entry WHERE client=? AND status!='void' AND amount>0
           AND category IN (%s) AND upper(coalesce(description,'')) LIKE '%%DEPOSIT%%' AND upper(description) NOT LIKE '%%BANKCARD%%'
           AND date >= ? ORDER BY date DESC""" % ",".join("?" * len(CASH_DEPOSIT_CATS)),
        (client, *CASH_DEPOSIT_CATS, cutoff))
        if r["id"] not in linked]

    daily = [dict(date=d, **{k: round(v, 2) if isinstance(v, float) else v for k, v in x.items()},
                  short=round(x["cash_expected"] - x["turned_in"] - x["paid_out"], 2))
             for d, x in sorted(by_day.items(), reverse=True) if x["cash_expected"] or x["turned_in"] or x["paid_out"]][:45]
    history = [dict(date=r["date"], counted=r["counted"], expected=r["close"], variance=r["variance"], note=r["note"], id=r["count_id"])
               for r in rows if r["count_id"]]
    return dict(
        last_count=last, expected_now=round(running, 2) if running is not None else None,
        in_since=in_since, paid_since=paid_since, out_since=out_since, since=list(reversed(since)), last_row=last_row,
        rows=list(reversed(rows))[:45], unaccounted=list(reversed(unaccounted)),
        unaccounted_total=round(sum(f["amount"] for f in unaccounted), 2),
        recent=list(reversed(recent)), recent_total=round(sum(f["amount"] for f in recent), 2), prev_count=prev,
        history=list(reversed(history))[:30], starts=starts,
        deposits=list(reversed(deps))[:40], unrecorded=unrecorded, daily=daily,
        open_due=round(sum(x["due"] for x in by_day.values()), 2),
        unmatched=sum(1 for x in deps if not x["entry_id"] and (today - dt.date.fromisoformat(x["date"])).days > 3),
    )


def _m(v):
    return "${:,.2f}".format(v) if v is not None else "—"
