"""Payroll cost: overtime (over 40 hours in a workweek) and where each payroll's money comes from.

Workweek = Monday–Sunday, the same weeks as the pay periods (each 14-day period is two workweeks). Hours over 40 in a
workweek are overtime, paid at 1.5 × the person's straight-time rate; the hours themselves are already in the Time
Clock wages, so overtime adds the 0.5 × rate premium. Salaried people are not counted (no hourly rate).

How a payroll is paid (Gusto):
  - out of the bank on pay day = net pay of everyone paid by DIRECT DEPOSIT (GUSTO/NET debit)
                                 + all payroll taxes: withheld from employees + the employer's share (GUSTO/TAX debit)
  - paper checks               = net pay of everyone paid by CHECK (written by hand, clear when they're cashed)
  total cost = gross pay + employer taxes = bank + checks.
Each person's method comes from their last Gusto payroll journal; Client settings can override it.
"""
import datetime as dt
import json
import sqlite3

from . import books as B

OT_LIMIT = 40.0
EE_RATE_DEFAULT = 0.10  # employee withholding share of gross until Gusto runs are imported


def week_start(d):
    """Monday of the workweek holding date d."""
    return d - dt.timedelta(days=d.weekday())


def _iso(d):
    return d.isoformat() if isinstance(d, dt.date) else d


# ------------------------------------------------------------------ how each person is paid
def get_overrides(db, client):
    try:
        v = json.loads(B.get_meta(db, client, "pay_methods", "{}") or "{}")
        return {str(k): ("check" if str(m).lower().startswith("c") else "dd") for k, m in v.items()}
    except (ValueError, TypeError, AttributeError, sqlite3.OperationalError):
        return {}


def set_overrides(db, client, items):
    """items: {display name: 'check'|'dd'}"""
    B.set_meta(db, client, "pay_methods", json.dumps(items))
    db.commit()


def gusto_methods(db, client):
    """{normalized name: (display name, 'check'|'dd', pay_day)} from each person's most recent Gusto run."""
    out = {}
    try:
        rows = db.execute("SELECT pay_day, employees FROM gusto_run WHERE client=? ORDER BY pay_day", (client,)).fetchall()
    except sqlite3.OperationalError:
        return out
    for pay_day, emps in rows:
        for e in json.loads(emps or "[]"):
            m = (e.get("payment") or "").strip().lower()
            if not m:
                continue
            out[B._norm_name(e.get("name"))] = (e.get("name"), "check" if m.startswith("check") else "dd", pay_day)
    return out


def _match(name, table):
    n = B._norm_name(name)
    if n in table:
        return table[n]
    first = n.split(" ")[0] if n else ""
    f = [v for k, v in table.items() if first and k.split(" ")[0] == first]
    return f[0] if len(f) == 1 else None


def pay_methods(db, client):
    """{normalized name: dict(name, method 'check'|'dd', source 'setting'|'gusto', since)} — setting wins over Gusto."""
    out = {n: dict(name=v[0], method=v[1], source="gusto", since=v[2]) for n, v in gusto_methods(db, client).items()}
    for name, m in get_overrides(db, client).items():
        n = B._norm_name(name)
        k = n if n in out else next((k for k in out if k.split(" ")[0] == n.split(" ")[0] and len(n.split(" ")) == 1), n)
        out[k] = dict(name=out.get(k, {}).get("name") or name, method=m, source="setting", since=None)
    return out


def method_of(name, methods):
    m = _match(name, methods)
    return m["method"] if m else "dd"


# ------------------------------------------------------------------ withholding / tips shares from Gusto
def gusto_rates(db, client, runs=4):
    """Employee withholding rate = employee taxes ÷ (gross − cash tips) of the last regular runs (cash tips are already
    in the employee's hand, but their withholding comes out of the paycheck). Plus each person's share of paycheck
    (card) tips."""
    try:
        rows = [dict(r) for r in db.execute("""SELECT gross, cash_tips, ee_tax, employees FROM gusto_run WHERE client=? AND kind='Regular'
                                               ORDER BY pay_day DESC LIMIT ?""", (client, runs))]
    except sqlite3.OperationalError:
        rows = []
    base = sum((r["gross"] or 0) - (r["cash_tips"] or 0) for r in rows)
    ee = sum(r["ee_tax"] or 0 for r in rows)
    tips = {}
    for r in rows:
        for e in json.loads(r["employees"] or "[]"):
            t = e.get("tips") or 0
            if t > 0:
                k = B._norm_name(e.get("name"))
                tips[k] = tips.get(k, 0.0) + t
    tt = sum(tips.values())
    return dict(ee_rate=(ee / base) if base > 0 else EE_RATE_DEFAULT, ee_from_gusto=base > 0,
                tip_share={k: v / tt for k, v in tips.items()} if tt else {})


# ------------------------------------------------------------------ hours per person per workweek
def daily_hours(db, client, shifts_path=None):
    """({date: {normalized name: hours}}, {normalized name: display name}) from the Time Clock, salaried people left out."""
    sal = B._salaried_names(db, client)
    days, names = {}, {}
    for d, emp, job, cin, h in B.time_clock(db, client, shifts_path):
        if B._is_salaried(emp, sal):
            continue
        k = B._norm_name(emp)
        names.setdefault(k, emp)
        x = days.setdefault(d, {})
        x[k] = x.get(k, 0.0) + (h or 0.0)
    return days, names


def weeks(db, client, a, b, shifts_path=None, today=None, project=True, hours=None):
    """Workweeks (Mon–Sun) overlapping a..b: [{start, end, people:[{key, name, hours, actual, projected, ot_hours, rate,
    guessed, premium}], ot_hours, premium, days_known, days_projected}]. Days not imported yet (up to the week's end) are
    projected from the person's average hours on the same weekday over the last 4 imported weeks when `project`."""
    today = today or dt.date.today()
    a, b = dt.date.fromisoformat(_iso(a)), dt.date.fromisoformat(_iso(b))
    days, names = hours if hours else daily_hours(db, client, shifts_path)
    rates = B.pay_rates(db, client, shifts_path)
    known_rates = sorted(rates.values())
    median = known_rates[len(known_rates) // 2] if known_rates else 0.0
    known_days = sorted(days)

    def wd_avg(k, d):
        past = [x for x in reversed(known_days) if x < d.isoformat() and dt.date.fromisoformat(x).weekday() == d.weekday()][:4]
        return sum(days[x].get(k, 0.0) for x in past) / len(past) if past else 0.0

    out = []
    ws = week_start(a)
    while ws <= b:
        we = ws + dt.timedelta(days=6)
        act, proj = {}, {}
        dk = dp = 0
        d = ws
        while d <= we:
            iso = d.isoformat()
            if iso in days:
                dk += 1
                for k, h in days[iso].items():
                    act[k] = act.get(k, 0.0) + h
            elif project and iso >= min(known_days or [iso]):
                dp += 1
                for k in names:
                    h = wd_avg(k, d)
                    if h > 0.01:
                        proj[k] = proj.get(k, 0.0) + h
            d += dt.timedelta(days=1)
        people = []
        for k in set(act) | set(proj):
            h = act.get(k, 0.0) + proj.get(k, 0.0)
            r = rates.get(k)
            ot = max(0.0, h - OT_LIMIT)
            people.append(dict(key=k, name=names.get(k, k.title()), hours=round(h, 2), actual=round(act.get(k, 0.0), 2),
                               projected=round(proj.get(k, 0.0), 2), ot_hours=round(ot, 2), rate=r if r is not None else median,
                               guessed=r is None, premium=round(ot * (r if r is not None else median) * 0.5, 2)))
        people.sort(key=lambda p: (-p["ot_hours"], -p["hours"]))
        out.append(dict(start=ws.isoformat(), end=we.isoformat(), people=people, days_known=dk, days_projected=dp,
                        ot_hours=round(sum(p["ot_hours"] for p in people), 2), premium=round(sum(p["premium"] for p in people), 2),
                        ot_people=sum(1 for p in people if p["ot_hours"] > 0), complete=we < today and dp == 0))
        ws += dt.timedelta(days=7)
    return out


# ------------------------------------------------------------------ enrich the payroll estimate
def enrich(db, client, est, shifts_path=None, today=None):
    """Add overtime and the bank / paper-check split to each pay day of books.payroll_estimate()'s result (in place).
    Per pay day: overtime (premium $), ot (people with overtime per week), gross, ee_tax, er_tax, bank, checks,
    check_people, dd_people, and for Gusto previews the same split from Gusto's per-person rows."""
    today = today or dt.date.today()
    methods = pay_methods(db, client)
    gr = gusto_rates(db, client)
    ee_rate = gr["ee_rate"]
    hours = daily_hours(db, client, shifts_path)
    sal = {B._norm_name(x["name"]): (x["name"], x["annual"] / 26.0) for x in est.get("salaries") or []}
    for p in est["pay_days"]:
        wk = weeks(db, client, p["start"], p["end"], shifts_path, today=today, hours=hours)
        ot_people = [dict(week=w["start"], week_end=w["end"], name=x["name"], hours=x["hours"], ot_hours=x["ot_hours"],
                          premium=x["premium"], projected=x["projected"] > 0, guessed=x["guessed"])
                     for w in wk for x in w["people"] if x["ot_hours"] > 0]
        ot = round(sum(x["premium"] for x in ot_people), 2)
        p["overtime"], p["ot"], p["ot_hours"] = ot, ot_people, round(sum(x["ot_hours"] for x in ot_people), 2)
        p["weeks"] = [dict(start=w["start"], end=w["end"], ot_hours=w["ot_hours"], premium=w["premium"], ot_people=w["ot_people"],
                           days_projected=w["days_projected"]) for w in wk]
        # the employer tax follows the gross, so overtime raises both
        rate = est.get("rate") or 0.0
        gross = round(p["wages"] + p["tips"] + p["salaries"] + ot, 2)
        p["gross"] = gross
        p["er_tax"] = round(gross * rate, 2)
        if p.get("gusto"):  # Gusto's numbers stay the amount due; keep the Time Clock estimate (now with overtime) beside it
            p["estimate_total"] = round(gross + p["er_tax"], 2)
        else:
            p["total"] = round(gross + p["er_tax"], 2)
        # each person's gross for the period → share paid by check
        per = {}
        for w in wk:
            for x in w["people"]:
                per[x["key"]] = per.get(x["key"], 0.0) + x["hours"] * x["rate"] + x["premium"]
        for k, (nm, v) in sal.items():
            per[k] = per.get(k, 0.0) + v
        for k, s in gr["tip_share"].items():
            per[k] = per.get(k, 0.0) + s * p["tips"]
        tot = sum(per.values())
        name_of = dict(hours[1])
        name_of.update({k: v[0] for k, v in sal.items()})
        chk = {k: v for k, v in per.items() if v > 0.005 and method_of(name_of.get(k, k), methods) == "check"}
        share = (sum(chk.values()) / tot) if tot > 0 else 0.0
        _split(p, gross, round(gross * share, 2), p["er_tax"], ee_rate)
        p["check_people"] = sorted(name_of.get(k, k.title()) for k in chk)
        p["split_source"] = "estimate"
        if p.get("gusto"):  # Gusto preview: per-person gross and company taxes are known
            g = p["gusto"]
            cg = round(sum(r["gross"] for r in g["rows"] if method_of(r["name"], methods) == "check"), 2)
            extra = round((g.get("reimb") or 0) + (g.get("benefits") or 0), 2)
            _split(p, g["gross"], cg, g["er_tax"], ee_rate, extra=extra)
            p["check_people"] = sorted(r["name"] for r in g["rows"] if method_of(r["name"], methods) == "check" and r["gross"])
            p["split_source"] = "gusto"
    est["ee_rate"] = round(ee_rate, 4)
    est["ee_from_gusto"] = gr["ee_from_gusto"]
    est["methods"] = methods
    return est


def _split(p, gross, check_gross, er_tax, ee_rate, extra=0.0):
    ee = round(gross * ee_rate, 2)
    net_check = round(check_gross * (1 - ee_rate), 2)
    net_dd = round((gross - check_gross) * (1 - ee_rate), 2)
    p["cost"] = dict(gross=round(gross, 2), er_tax=round(er_tax, 2), ee_tax=ee, net_dd=net_dd, net_check=net_check, extra=extra,
                     taxes=round(ee + er_tax, 2), bank=round(net_dd + ee + er_tax + extra, 2), checks=net_check,
                     total=round(gross + er_tax + extra, 2), check_gross=round(check_gross, 2))
