"""Sales tax inside sales deposits.

Every card or cash deposit from sales carries the sales tax customers paid on those sales. That tax isn't the
business's income: it is owed to the state until it is remitted. So each sales deposit gets a split (like the card
tips split): the state sales tax goes to "Sales Tax Payable", the mixed beverage (alcohol) tax to "Alc Sales Tax",
and the rest stays as sales. Payments to the Oklahoma Tax Commission are categorized to the same two categories, so
each category's balance is the tax still owed. Both categories are liabilities ("Not in P&L").

Tax per business day = the POS day totals (Heartland reports, Linga) used by the Sales Tax report; the alcohol part is
the liquor tax for that day (actual from the Heartland Taxes report, else estimated). A deposit's tax:
- card deposit matched to business days: each day's card sales (incl. tax, excl. tips) × that day's tax ÷ gross receipts
- cash deposit: the cash days it covers (Cash page allocation) × each day's tax ÷ gross receipts
- card deposit not matched yet: its amount × the recent ratio of tax to card sales + tips (marked estimated)
"""
import collections
import datetime as dt
import json

STATE_CAT = "Sales Tax Payable"
ALC_CAT = "Alc Sales Tax"
TAX_CATS = (STATE_CAT, ALC_CAT)
SOURCE = "sales-tax"


def ensure(db, client):
    """Both tax categories exist and are liabilities (not P&L)."""
    for name, sort in ((STATE_CAT, 910), (ALC_CAT, 911)):
        r = db.execute("SELECT pl_group FROM category WHERE client=? AND name=?", (client, name)).fetchone()
        if not r:
            db.execute("INSERT INTO category(client,name,pl_group,sort) VALUES(?,?,?,?)", (client, name, "Not in P&L", sort))
        elif r[0] != "Not in P&L":
            db.execute("UPDATE category SET pl_group='Not in P&L' WHERE client=? AND name=?", (client, name))
    db.commit()


def _d(s):
    return dt.date.fromisoformat(s)


def day_rates(db, client, shifts_path, a, b):
    """{date: (tax ÷ gross receipts, alcohol tax ÷ gross receipts)} for business days with tax figures."""
    from . import salesum
    salesum.ensure(db)
    day, liquor, sh, lt, taxes, pos_liq, alcohol, alc_net = salesum._tax_sources(db, client, shifts_path, a, b)
    liquor = alcohol  # the alcohol bucket: both alcohol taxes while the hotel holds the license, the liquor tax after
    out = {}
    d = _d(a)
    while d.isoformat() <= b:
        k = d.isoformat()
        gross, tax, net, src = day(k)
        if tax is not None and gross and gross > 0:
            liq = min(liquor.get(k, 0.0), tax)
            out[k] = (tax / gross, liq / gross)
        d += dt.timedelta(days=1)
    return out


def _near_rate(rates, day, span=30):
    """Average rate of the business days within `span` days (for a day without tax figures)."""
    vals = [v for k, v in rates.items() if abs((_d(k) - _d(day)).days) <= span] or list(rates.values())
    if not vals:
        return None
    return (sum(v[0] for v in vals) / len(vals), sum(v[1] for v in vals) / len(vals))


def apply(db, client, shifts_path, since):
    """Recompute the sales tax split on every sales deposit since `since` (and all matched / allocated ones)."""
    ensure(db, client)
    first = db.execute("SELECT min(date) FROM day_tax WHERE client=?", (client,)).fetchone()
    first = (first[0] if first and first[0] else since)
    today = dt.date.today().isoformat()
    rates = day_rates(db, client, shifts_path, first, today)
    if not rates:
        return 0
    from .tips import set_splits, _label
    db.execute("DELETE FROM entry_split WHERE client=? AND source=?", (client, SOURCE))
    done = 0

    def split(eid, tax, alc, memo):
        nonlocal done
        tax, alc = round(tax, 2), round(alc, 2)
        if tax < 0.005:
            return
        set_splits(db, client, eid, [(STATE_CAT, round(tax - alc, 2), "Sales tax in " + memo), (ALC_CAT, alc, "Alcohol tax in " + memo)], SOURCE)
        done += 1

    # card deposits matched to business days
    days = {r["date"]: dict(r) for r in db.execute("SELECT date, card_sales, card_tips FROM sales_day WHERE client=?", (client,))}
    matched = set()
    for m in db.execute("""SELECT m.entry_id, m.days FROM deposit_match m JOIN entry e ON e.id=m.entry_id
                           WHERE m.client=? AND e.status!='void'""", (client,)):
        dd = json.loads(m["days"])
        matched.add(m["entry_id"])
        tax = alc = 0.0
        est = False
        for k in dd:
            card = (days.get(k) or {}).get("card_sales") or 0.0
            r = rates.get(k)
            if r is None:
                r, est = _near_rate(rates, k), True
            if r:
                tax += card * r[0]
                alc += card * r[1]
        split(m["entry_id"], tax, alc, "card sales " + _label(dd) + (" (estimated)" if est else ""))
    # card deposits not matched yet: amount × recent tax ÷ (card sales + tips)
    for e in db.execute("""SELECT id, date, amount FROM entry WHERE client=? AND status!='void' AND amount>0 AND category='Sales - Card'
                           AND date>?""", (client, first)).fetchall():
        if e["id"] in matched:
            continue
        lo = (_d(e["date"]) - dt.timedelta(days=8)).isoformat()
        win = [k for k in days if lo <= k < e["date"] and k in rates]
        base = sum((days[k]["card_sales"] or 0) + (days[k]["card_tips"] or 0) for k in win)
        if base <= 0:
            continue
        t = sum((days[k]["card_sales"] or 0) * rates[k][0] for k in win) / base
        l_ = sum((days[k]["card_sales"] or 0) * rates[k][1] for k in win) / base
        split(e["id"], e["amount"] * t, e["amount"] * l_, "card deposit (estimated — not matched to business days yet)")
    # cash deposits: the cash days each one covers (Cash page allocation)
    try:
        from . import lingaarchive as LA
        alloc = LA.deposit_allocation(db, client, shifts_path)
    except Exception:  # noqa: BLE001 — no cash data: cash deposits keep no split
        alloc = None
    if alloc:
        for eid, o in alloc["by_entry"].items():
            tax = alc = 0.0
            est = False
            for c in o.get("covers") or []:
                r = rates.get(c["date"])
                if r is None:
                    r, est = _near_rate(rates, c["date"]), True
                if r:
                    tax += c["amount"] * r[0]
                    alc += c["amount"] * r[1]
            if o.get("excess", 0) > 0.005:
                r = _near_rate(rates, o["date"])
                if r:
                    tax += o["excess"] * r[0]
                    alc += o["excess"] * r[1]
                    est = True
            span = _label([c["date"] for c in o.get("covers") or []][:6]) if o.get("covers") else "cash deposit"
            split(eid, tax, alc, "cash " + span + (" (part estimated)" if est else ""))
    allocate_payments(db, client)
    db.commit()
    return done


PAY_SOURCE = "tax-payment"


RETURN_SCHEMA = """CREATE TABLE IF NOT EXISTS tax_return (
  client TEXT NOT NULL, kind TEXT NOT NULL, period_end TEXT NOT NULL, account TEXT, due_date TEXT, received TEXT,
  total_sales REAL, removed REAL, exemptions REAL, net_taxable REAL, state_tax REAL, local_tax REAL, tax_due REAL,
  interest REAL, penalty REAL, total_due REAL, amended INTEGER, filename TEXT, imported_by TEXT, imported_at TEXT,
  PRIMARY KEY (client, kind, period_end))"""
PENALTY_CAT = "Tax Penalties & Interest"
_MONTHS = r"(January|February|March|April|May|June|July|August|September|October|November|December) \d{1,2}, \d{4}"


def _money(v):
    return float(v.replace(",", "").replace("$", "")) if v else 0.0


def parse_return(data):
    """Oklahoma Tax Commission return PDF (OkTAP "for your records" copy) → dict, or None if it isn't one.
    Sales tax return STS 20002-A: lines 1 Total Sales … 8 Tax Due, 9 Interest, 10 Penalty, 11 Total Due."""
    import io
    import re
    try:
        import pypdf
        text = "\n".join(pg.extract_text() or "" for pg in pypdf.PdfReader(io.BytesIO(data)).pages[:1])
    except Exception:  # noqa: BLE001
        return None
    if "Oklahoma" not in text:
        return None
    kind = "sales" if re.search(r"Sales Tax Return", text) else "mixed" if re.search(r"Mixed Beverage", text, re.I) else None
    if not kind:
        return None
    dates = [dt.datetime.strptime(m.group(0), "%B %d, %Y").date().isoformat() for m in re.finditer(_MONTHS, text)]
    if not dates:
        return None

    def line(n):
        m = re.search(r"(?m)^\s*%s\.\s[^$]*?\$\s*(-?[\d,]+\.\d\d)" % re.escape(n), text)
        return _money(m.group(1)) if m else 0.0
    acct = re.search(r"ACCOUNT ID:\s*([A-Z]+-[\d-]+)", text)
    r = dict(kind=kind, period_end=dates[0], due_date=dates[1] if len(dates) > 1 else None, received=dates[2] if len(dates) > 2 else None,
             account=acct.group(1) if acct else None, total_sales=line("1"), removed=line("2"), exemptions=line("3"), net_taxable=line("5"),
             state_tax=line("6"), local_tax=line("7"), tax_due=line("8"), interest=line("9"), penalty=line("10"), total_due=line("11"),
             amended=0)
    if not r["tax_due"] and (r["state_tax"] or r["local_tax"]):
        r["tax_due"] = round(r["state_tax"] + r["local_tax"], 2)
    if not r["total_due"]:
        r["total_due"] = round(r["tax_due"] + r["interest"] + r["penalty"], 2)
    return r


def save_return(db, client, r, filename, user):
    db.execute(RETURN_SCHEMA)
    db.execute("""INSERT OR REPLACE INTO tax_return (client, kind, period_end, account, due_date, received, total_sales, removed, exemptions,
                  net_taxable, state_tax, local_tax, tax_due, interest, penalty, total_due, amended, filename, imported_by, imported_at)
                  VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (client, r["kind"], r["period_end"], r["account"], r["due_date"], r["received"], r["total_sales"], r["removed"], r["exemptions"],
                r["net_taxable"], r["state_tax"], r["local_tax"], r["tax_due"], r["interest"], r["penalty"], r["total_due"], r["amended"],
                filename, user, dt.datetime.now().isoformat(timespec="seconds")))
    db.commit()


def returns(db, client):
    db.execute(RETURN_SCHEMA)
    return [dict(r) for r in db.execute("SELECT * FROM tax_return WHERE client=? ORDER BY period_end", (client,))]


def match_payments(db, client):
    """Tie each payment to the Tax Commission to a filed return. Returns ({entry_id: return}, payments)."""
    rets = returns(db, client)
    pays = [dict(r) for r in db.execute("""SELECT id, date, amount, category, description, payee, check_no FROM entry WHERE client=?
                                           AND status!='void' AND amount<0 AND category IN (?,?,?) ORDER BY date, id""",
                                        (client,) + TAX_CATS + (PENALTY_CAT,))]
    used, hit = set(), {}
    for p in pays:
        amt = -p["amount"]
        best = None
        for k, r in enumerate(rets):
            if k in used or r["period_end"] >= p["date"] or r["total_due"] <= 0:
                continue
            exact = abs(amt - r["total_due"]) <= 0.01
            near = r["tax_due"] - 0.01 <= amt <= r["total_due"] + 25
            if not (exact or near):
                continue
            score = (0 if exact else 1, abs(amt - r["total_due"]), r["period_end"])
            if best is None or score < best[0]:
                best = (score, k)
        if best:
            used.add(best[1])
            hit[p["id"]] = rets[best[1]]
    return hit, pays


def allocate_payments(db, client, shifts_path=None, first=None, today=None):
    """Split each payment to the Tax Commission by the filed return it pays (the OkTAP copies in the Sales Tax cabinet):
    a sales tax return's Tax Due → Sales Tax Payable (state + county sales tax), a mixed beverage return's → Alc Sales
    Tax, and the return's interest/penalty (and anything paid above it) → Tax Penalties & Interest (an expense).
    A payment that matches no return keeps its own category."""
    from .tips import set_splits
    db.execute(RETURN_SCHEMA)
    if not db.execute("SELECT 1 FROM category WHERE client=? AND name=?", (client, PENALTY_CAT)).fetchone():
        db.execute("INSERT INTO category(client,name,pl_group,sort) VALUES(?,?,?,?)", (client, PENALTY_CAT, "Operating Expenses", 912))
    db.execute("DELETE FROM entry_split WHERE client=? AND source=?", (client, PAY_SOURCE))
    hit, pays = match_payments(db, client)
    for p in pays:
        r = hit.get(p["id"])
        if not r:
            continue
        amt = -p["amount"]
        tax = min(amt, r["tax_due"])
        cat = STATE_CAT if r["kind"] == "sales" else ALC_CAT
        per = dt.date.fromisoformat(r["period_end"]).strftime("%b %Y")
        what = "sales tax" if r["kind"] == "sales" else "mixed beverage tax"
        set_splits(db, client, p["id"], [(cat, -round(tax, 2), "%s return %s: tax due" % (what.capitalize(), per)),
                                         (PENALTY_CAT, -round(amt - round(tax, 2), 2), "%s return %s: interest, penalty" % (what.capitalize(), per))],
                   PAY_SOURCE)


def returns_report(db, client, shifts_path):
    """Filed returns vs what the POS shows for the same month, and which payment paid each."""
    from . import salesum
    rets = returns(db, client)
    if not rets:
        return []
    rep = salesum.tax_report(db, client, shifts_path, rets[0]["period_end"][:7] + "-01", rets[-1]["period_end"])
    pos = {m["month"]: m for m in rep["months"]}
    hit, pays = match_payments(db, client)
    by_ret = {}
    for p in pays:
        r = hit.get(p["id"])
        if r:
            by_ret[(r["kind"], r["period_end"])] = p
    out = []
    for r in rets:
        m = pos.get(r["period_end"][:7]) or {}
        p = by_ret.get((r["kind"], r["period_end"]))
        out.append(dict(r, pos_net=m.get("taxable_net"), pos_tax=m.get("sales_tax") if r["kind"] == "sales" else m.get("mixed"),
                        sales_diff=round(r["total_sales"] - (m.get("taxable_net") or 0), 2) if m else None,
                        tax_diff=round(r["tax_due"] - ((m.get("sales_tax") if r["kind"] == "sales" else m.get("mixed")) or 0), 2) if m else None,
                        paid_date=p["date"] if p else None, paid=round(-p["amount"], 2) if p else None))
    unmatched = [p for p in pays if p["id"] not in hit]
    return dict(rows=out, unmatched=unmatched)


def by_entry(db, client):
    """{entry_id: {state, alc, total, estimated}} for the deposits' tax splits."""
    out = collections.defaultdict(lambda: dict(state=0.0, alc=0.0, total=0.0, estimated=False))
    try:
        rows = db.execute("SELECT entry_id, category, amount, memo FROM entry_split WHERE client=? AND source=?", (client, SOURCE)).fetchall()
    except Exception:  # noqa: BLE001
        return {}
    for r in rows:
        x = out[r["entry_id"]]
        if r["category"] == ALC_CAT:
            x["alc"] = round(x["alc"] + r["amount"], 2)
        else:
            x["state"] = round(x["state"] + r["amount"], 2)
        x["total"] = round(x["state"] + x["alc"], 2)
        if "estimated" in (r["memo"] or ""):
            x["estimated"] = True
    return dict(out)


def balances(db, client):
    """Tax collected in deposits, paid to the state, and still owed, per category."""
    out = {}
    for cat in TAX_CATS:
        coll = db.execute("""SELECT round(coalesce(sum(s.amount),0),2) FROM entry_split s JOIN entry e ON e.id=s.entry_id
                             WHERE s.client=? AND s.category=? AND s.source=? AND e.status!='void'""", (client, cat, SOURCE)).fetchone()[0]
        paid = db.execute("""SELECT round(coalesce(-sum(s.amount),0),2) FROM entry_split s JOIN entry e ON e.id=s.entry_id
                             WHERE s.client=? AND s.category=? AND s.source=? AND e.status!='void'""", (client, cat, PAY_SOURCE)).fetchone()[0]
        paid += db.execute("""SELECT coalesce(-sum(amount),0) FROM entry e WHERE client=? AND category=? AND status!='void' AND amount<0
                              AND NOT EXISTS (SELECT 1 FROM entry_split s WHERE s.entry_id=e.id AND s.source=?)""", (client, cat, PAY_SOURCE)).fetchone()[0]
        paid = round(paid, 2)
        out[cat] = dict(collected=coll, paid=paid, owed=round(coll - paid, 2))
    return out


# ------------------------------------------------------------------ what each sales deposit has to cover (display only)
COGS_CATS = {"food": "Food COGS", "bev": "Beverage COGS"}
COGS_DAYS = 14


def _shift_minutes(shifts_path, a, b):
    """{date: [(staff name, minutes)]} from the Shifts closeouts (read-only)."""
    out = collections.defaultdict(list)
    if not shifts_path:
        return out
    import sqlite3
    try:
        s = sqlite3.connect("file:%s?mode=ro" % shifts_path, uri=True, timeout=5)
        for d, name, m in s.execute("""SELECT sh.shift_date, st.name, sh.minutes_worked FROM shifts sh JOIN staff st ON st.id = sh.staff_id
                                       WHERE sh.shift_date BETWEEN ? AND ? AND coalesce(sh.minutes_worked, 0) > 0""", (a, b)):
            out[d].append((name, m or 0))
        s.close()
    except sqlite3.Error:
        pass
    return out


def daily_labor(db, client, shifts_path, a, b):
    """{date: hourly wages} for business days: POS Time Clock hours × rates (Payroll Summary) when the Time Clock has the
    day, else hours worked on the Shifts closeouts × the same rates (median rate for someone without one). Salaried
    people are left out (their salary doesn't depend on the day)."""
    from . import books as B
    wages = {d: v for d, v in B._pos_wages(db, client, shifts_path).items() if a <= d <= b}
    rates = B.pay_rates(db, client, shifts_path)
    known = sorted(rates.values())
    median = known[len(known) // 2] if known else 0.0
    firsts = collections.defaultdict(list)
    for k, v in rates.items():
        firsts[k.split(" ")[0]].append(v)
    salaried = B._salaried_names(db, client)
    for d, rows in _shift_minutes(shifts_path, a, b).items():
        if d in wages:
            continue
        tot = 0.0
        for name, m in rows:
            if B._is_salaried(name, salaried):
                continue
            n = B._norm_name(name)
            r = rates.get(n)
            if r is None and len(firsts.get(n.split(" ")[0] if n else "", [])) == 1:
                r = firsts[n.split(" ")[0]][0]
            tot += m / 60.0 * (r if r is not None else median)
        if tot:
            wages[d] = round(tot, 2)
    return wages


def employer_tax_rate(db, client):
    """Employer payroll tax ÷ gross wages from the last 4 regular Gusto runs (9.4% if none)."""
    try:
        runs = db.execute("""SELECT gross, er_tax FROM gusto_run WHERE client=? AND kind='Regular' ORDER BY pay_day DESC LIMIT 4""", (client,)).fetchall()
        g = sum(r["gross"] or 0 for r in runs)
        if g > 0:
            return sum(r["er_tax"] or 0 for r in runs) / g
    except Exception:  # noqa: BLE001
        pass
    return 0.094


def deposit_breakdown(db, client, shifts_path):
    """{entry_id: {...}} for card and cash sales deposits: of the money in the deposit, the sales tax and card tips
    (already split), the net sales, the labor for the business days it covers (hourly wages from the hours worked that
    day × the deposit's share of the day's sales, + employer payroll tax), and food / beverage cost at the share of
    net sales that Food COGS / Beverage COGS purchases were over the 2 weeks before the deposit. Display only."""
    from . import salesum
    first = db.execute("SELECT min(date) FROM day_tax WHERE client=?", (client,)).fetchone()
    first = first[0] if first and first[0] else None
    if not first:
        return {}
    today = dt.date.today().isoformat()
    day, *_ = salesum._tax_sources(db, client, shifts_path, first, today)
    labor = daily_labor(db, client, shifts_path, first, today)
    er = employer_tax_rate(db, client)
    tax = by_entry(db, client)
    gross = {}
    d = _d(first)
    while d.isoformat() <= today:
        k = d.isoformat()
        g_, t_, n_, src = day(k)
        if g_ and t_ is not None:
            gross[k] = (g_, n_)
        d += dt.timedelta(days=1)
    # what customers paid per business day (incl. tax): POS gross; for days without tax figures, card + cash
    sd = {r["date"]: dict(r) for r in db.execute("SELECT date, card_sales, card_tips, cash_sales FROM sales_day WHERE client=?", (client,))}
    cogs = collections.defaultdict(float)
    for r in db.execute("""SELECT date, category, amount FROM entry WHERE client=? AND status!='void' AND category IN (?,?)""",
                        (client, COGS_CATS["food"], COGS_CATS["bev"])):
        cogs[(r["date"], r["category"])] += -r["amount"]

    def cogs_rates(dep_date):
        lo = (_d(dep_date) - dt.timedelta(days=COGS_DAYS)).isoformat()
        net = sum(v[1] for k, v in gross.items() if lo <= k < dep_date)
        if net <= 0:
            return None
        f = sum(v for (k, c), v in cogs.items() if lo <= k < dep_date and c == COGS_CATS["food"])
        b = sum(v for (k, c), v in cogs.items() if lo <= k < dep_date and c == COGS_CATS["bev"])
        return f / net, b / net, f, b, net

    def build(eid, dep_date, amount, parts):
        """parts = [(business day, money from that day in this deposit, incl. tax, excl. tips)]."""
        net = lab = 0.0
        days = []
        for k, money in parts:
            g_ = gross.get(k, (None, None))[0] or ((sd.get(k) or {}).get("card_sales") or 0) + ((sd.get(k) or {}).get("cash_sales") or 0)
            n_ = gross.get(k, (None, None))[1]
            if not g_:
                continue
            share = min(1.0, money / g_)
            net += (n_ if n_ is not None else g_) * share
            lab += labor.get(k, 0.0) * share
            days.append(k)
        cr = cogs_rates(dep_date)
        x = tax.get(eid) or dict(state=0.0, alc=0.0, total=0.0)
        tips = round(amount - sum(m for _, m in parts), 2) if parts else 0.0
        out = dict(net=round(net, 2), wages=round(lab, 2), payroll_tax=round(lab * er, 2), labor=round(lab * (1 + er), 2),
                   food=round(net * cr[0], 2) if cr else None, bev=round(net * cr[1], 2) if cr else None,
                   cogs_basis=dict(food=round(cr[2], 2), bev=round(cr[3], 2), sales=round(cr[4], 2)) if cr else None,
                   tax=x["total"], tips=max(0.0, tips), labor_days=sum(1 for k in days if k in labor), days=len(days))
        out["cogs"] = round((out["food"] or 0) + (out["bev"] or 0), 2)
        out["left"] = round(amount - out["tips"] - out["tax"] - out["labor"] - out["cogs"], 2)
        return out

    res = {}
    for m in db.execute("""SELECT m.entry_id, m.days, m.fee, e.date, e.amount FROM deposit_match m JOIN entry e ON e.id=m.entry_id
                           WHERE m.client=? AND e.status!='void' AND e.date>=?""", (client, first)):
        dd = json.loads(m["days"])
        parts = [(k, (sd.get(k) or {}).get("card_sales") or 0.0) for k in dd]
        res[m["entry_id"]] = build(m["entry_id"], m["date"], m["amount"] + (m["fee"] or 0), parts)
    try:
        from . import lingaarchive as LA
        alloc = LA.deposit_allocation(db, client, shifts_path)
    except Exception:  # noqa: BLE001
        alloc = None
    if alloc:
        for eid, o in alloc["by_entry"].items():
            if o["date"] < first:
                continue
            res[eid] = build(eid, o["date"], o["amount"], [(c["date"], c["amount"]) for c in o.get("covers") or []])
    return res
