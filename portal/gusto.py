"""Gusto payroll journal (Reports → Payroll Journal, CSV) — what each payroll actually paid.

Each pay run lists every employee's pay types (Regular, Paycheck Tips, Cash Tips, Distributed service charges),
employee/employer taxes, net pay and how it was paid (Direct Deposit or Check). Reconciled with the bank:
  - GUSTO/TAX debit  = employee + employer taxes of the run
  - GUSTO/NET debit  = net pay of the Direct Deposit employees
  - paper checks     = net pay of each "Check" employee (written by hand, cleared later)
The run's Paycheck Tips are the card tips paid out through payroll: they move from Payroll - Wages to
Card Tips Payable (owed to staff), as a split on the run's tax debit (net zero, so cash is unchanged).
"""
import csv
import datetime as dt
import json

TIPS_CAT = "Card Tips Payable"
WAGES_CAT = "Payroll - Wages"
SCHEMA = """
CREATE TABLE IF NOT EXISTS gusto_run (
  client TEXT NOT NULL, run_key TEXT NOT NULL, pay_day TEXT NOT NULL, kind TEXT, period_start TEXT, period_end TEXT,
  gross REAL, paycheck_tips REAL, cash_tips REAL, service REAL, ee_tax REAL, er_tax REAL, net_check REAL, net_dd REAL,
  employees TEXT, imported_at TEXT, imported_by TEXT, PRIMARY KEY (client, run_key));
"""


def ensure(db):
    db.executescript(SCHEMA)


def _num(v):
    v = (v or "").replace(",", "").replace("$", "").strip()
    try:
        return float(v)
    except ValueError:
        return 0.0


def _mdy(v):
    return dt.datetime.strptime(v.strip(), "%m/%d/%Y").date().isoformat()


def parse_journal(text):
    rows = list(csv.reader(text.lstrip("﻿").splitlines()))
    if not any(r[:1] == ["Payroll Journal Report"] for r in rows[:3]) and not any(r[:1] == ["Pay day"] for r in rows):
        raise ValueError("This doesn't look like a Gusto Payroll Journal export (Reports → Payroll Journal → CSV).")
    runs, cur, emp, hdr = [], None, None, None
    for i, r in enumerate(rows):
        if not r:
            continue
        nxt = rows[i + 1][:1] if i + 1 < len(rows) else []
        if len(r) == 1 and r[0].strip() and nxt in (["Pay period"], ["Pay day"]):
            cur = dict(kind=r[0].strip(), period=(None, None), pay_day=None, emps=[], done=False)
            runs.append(cur)
            emp, hdr = None, None
            continue
        if cur is None:
            continue
        if r[0] == "Pay period":
            a, b = [x.strip() for x in r[1].split("-")]
            cur["period"] = (_mdy(a), _mdy(b))
            continue
        if r[0] == "Pay day":
            cur["pay_day"] = _mdy(r[1])
            continue
        if r[0] == "Last Name":
            hdr = {k: j for j, k in enumerate(r)}
            continue
        if hdr is None or cur["done"] or len(r) < len(hdr):
            continue
        if r[0] == "Payroll Totals":
            cur["done"] = True
            continue
        g = lambda k: r[hdr[k]] if k in hdr else ""
        if r[0]:
            emp = dict(name=("%s %s" % (g("First Name"), g("Last Name"))).strip(), dept=g("Department"),
                       ee_tax=_num(g("Employee Taxes")), er_tax=_num(g("Employer Taxes")), net=_num(g("Net Pay")),
                       payment=g("Payment"), pay={}, hours=0.0)
            cur["emps"].append(emp)
        if emp is not None and g("Pay Type") and g("Job") != "Totals" and g("Pay Type") not in ("Gross", "Paycheck Tips", "Cash Tips"):
            emp["hours"] = round(emp["hours"] + _num(g("Hours")), 2)
        if emp is not None and g("Pay Type"):
            emp["pay"][g("Pay Type")] = round(emp["pay"].get(g("Pay Type"), 0) + _num(g("Amount")), 2)
    out = []
    for run in runs:
        e = [x for x in run["emps"] if x["net"] or x["pay"].get("Gross")]
        if not run["pay_day"] or not e:
            continue
        P = lambda k: round(sum(x["pay"].get(k, 0) for x in e), 2)
        out.append(dict(
            kind=run["kind"], pay_day=run["pay_day"], period_start=run["period"][0], period_end=run["period"][1],
            gross=P("Gross"), paycheck_tips=P("Paycheck Tips"), cash_tips=P("Cash Tips"), service=P("Distributed service charges"),
            ee_tax=round(sum(x["ee_tax"] for x in e), 2), er_tax=round(sum(x["er_tax"] for x in e), 2),
            net_check=round(sum(x["net"] for x in e if x["payment"].lower().startswith("check")), 2),
            net_dd=round(sum(x["net"] for x in e if not x["payment"].lower().startswith("check")), 2),
            employees=[dict(name=x["name"], net=x["net"], payment=x["payment"], tips=x["pay"].get("Paycheck Tips", 0),
                            cash_tips=x["pay"].get("Cash Tips", 0), hours=x["hours"], gross=x["pay"].get("Gross", 0),
                            service=x["pay"].get("Distributed service charges", 0)) for x in e]))
    if not out:
        raise ValueError("No pay runs found in that file.")
    return out


def import_journal(db, client, text, user):
    ensure(db)
    runs = parse_journal(text)
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for r in runs:
        key = "%s|%s|%s" % (r["pay_day"], r["kind"], r["period_start"])
        db.execute("""INSERT OR REPLACE INTO gusto_run(client,run_key,pay_day,kind,period_start,period_end,gross,paycheck_tips,cash_tips,
                      service,ee_tax,er_tax,net_check,net_dd,employees,imported_at,imported_by) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (client, key, r["pay_day"], r["kind"], r["period_start"], r["period_end"], r["gross"], r["paycheck_tips"], r["cash_tips"],
                    r["service"], r["ee_tax"], r["er_tax"], r["net_check"], r["net_dd"], json.dumps(r["employees"]), ts, user))
    db.commit()
    return dict(runs=len(runs), first=min(r["pay_day"] for r in runs), last=max(r["pay_day"] for r in runs),
                tips=round(sum(r["paycheck_tips"] for r in runs), 2), cash_tips=round(sum(r["cash_tips"] for r in runs), 2))


def has_runs(db, client):
    ensure(db)
    return db.execute("SELECT 1 FROM gusto_run WHERE client=? LIMIT 1", (client,)).fetchone() is not None


def covered_until(db, client):
    r = db.execute("SELECT max(pay_day) FROM gusto_run WHERE client=?", (client,)).fetchone()
    return r[0] if r else None


def _find(db, client, like, amount, day, before=7, after=5, used=()):
    d = dt.date.fromisoformat(day)
    for e in db.execute("""SELECT id, date, amount FROM entry WHERE client=? AND status!='void' AND amount<0
                           AND upper(coalesce(description,'')) LIKE ? AND abs(amount + ?) < 0.005
                           AND date BETWEEN ? AND ? ORDER BY abs(julianday(date) - julianday(?))""",
                        (client, like, amount, (d - dt.timedelta(days=before)).isoformat(),
                         (d + dt.timedelta(days=after)).isoformat(), day)):
        if e["id"] not in used:
            return dict(e)
    return None


def reconcile(db, client, sales_days=None):
    """Every pay run with what the bank shows for it, and card tips collected vs paid for its pay period."""
    from .tips import set_splits
    ensure(db)
    runs = [dict(r) for r in db.execute("SELECT * FROM gusto_run WHERE client=? ORDER BY pay_day, run_key", (client,))]
    days = sales_days or {r["date"]: dict(r) for r in db.execute("SELECT * FROM sales_day WHERE client=?", (client,))}
    used, out = set(), []
    checks = [dict(r) for r in db.execute(
        """SELECT id, date, amount, payee, check_no, status FROM entry WHERE client=? AND status!='void' AND amount<0
           AND category=? AND upper(coalesce(description,'')) NOT LIKE '%GUSTO%' ORDER BY date""", (client, WAGES_CAT))]
    seen_periods = set()
    for r in runs:
        tax = _find(db, client, "%GUSTO/TAX%", r["ee_tax"] + r["er_tax"], r["pay_day"], used=used) if r["ee_tax"] + r["er_tax"] else None
        dd = _find(db, client, "%GUSTO/NET%", r["net_dd"], r["pay_day"], used=used) if r["net_dd"] else None
        for x in (tax, dd):
            if x:
                used.add(x["id"])
        emps = json.loads(r["employees"] or "[]")
        paper = []
        pd = dt.date.fromisoformat(r["pay_day"])
        for e in emps:
            if not e["payment"].lower().startswith("check") or e["net"] <= 0:
                continue
            hit = next((c for c in checks if c["id"] not in used and abs(c["amount"] + e["net"]) < 0.005
                        and -5 <= (dt.date.fromisoformat(c["date"]) - pd).days <= 75), None)
            if hit:
                used.add(hit["id"])
            paper.append(dict(name=e["name"], net=e["net"], entry=hit))
        # card tips collected for the pay period (once per period, on its regular run)
        a, b = r["period_start"], r["period_end"]
        collected = None
        if a and b and (a, b) not in seen_periods and r["kind"].lower().startswith("regular"):
            seen_periods.add((a, b))
            collected = round(sum((v["card_tips"] or 0) for d, v in days.items() if a <= d <= b), 2)
        anchor = tax or dd or next((p["entry"] for p in paper if p["entry"]), None)
        tips = round(r["paycheck_tips"] or 0, 2)
        out.append(dict(r, tax_entry=tax, dd_entry=dd, paper=paper, anchor=anchor, collected=collected, tips=tips,
                        paper_cleared=sum(1 for p in paper if p["entry"]), paper_total=len(paper),
                        outstanding=[p for p in paper if not p["entry"]]))
    return out


def apply(db, client, since):
    """Move each run's Paycheck Tips off wages onto Card Tips Payable (split on the run's tax debit)."""
    from .tips import set_splits
    db.execute("DELETE FROM entry_split WHERE client=? AND source='gusto-tips'", (client,))
    rows = reconcile(db, client)
    for r in rows:
        if r["tips"] and r["anchor"] and (r["period_start"] or r["pay_day"]) >= since:
            a, b = r["period_start"] or r["pay_day"], r["period_end"] or r["pay_day"]
            memo = "Paycheck tips, %s payroll %s–%s" % (r["kind"], dt.date.fromisoformat(a).strftime("%-m/%-d"),
                                                         dt.date.fromisoformat(b).strftime("%-m/%-d"))
            set_splits(db, client, r["anchor"]["id"], [(TIPS_CAT, -r["tips"], memo), (WAGES_CAT, r["tips"], memo)], "gusto-tips")
    db.commit()
    return rows
