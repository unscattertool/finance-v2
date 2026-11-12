"""Hand-written checks cross-referenced with payees, Gusto payroll (paper paychecks) and the tips in them.

Every check that cleared the bank (or was entered by hand and is still open) is matched to a Gusto employee
paid by "Check": same amount within 5 days before to 75 days after the pay day, preferring the payee's name.
Then near amounts (within $1) for the same person, then checks to staff with no Gusto paycheck behind them.
"""
import collections
import datetime as dt
import json
import sqlite3

from . import tipcheck as TC

WINDOW = (-5, 75)
NEAR = 1.00


def _names_match(payee, name):
    if not payee or not name:
        return False
    p, n = payee.lower().strip(), name.lower().strip()
    if p == n:
        return True
    f1, f2 = TC._first(payee), TC._first(name)
    l1, l2 = TC._last(payee), TC._last(name)
    if l1 and l2:
        return f1 == f2 and (l1 == l2 or l1[:1] == l2[:1])
    return bool(f1) and f1 == f2


def _staff_names(shifts_path):
    out = set()
    if not shifts_path:
        return out
    try:
        s = sqlite3.connect("file:%s?mode=ro" % shifts_path, uri=True, timeout=5)
        out = {r[0] for r in s.execute("SELECT name FROM staff") if r[0]}
        s.close()
    except sqlite3.Error:
        pass
    return out


def report(db, client, shifts_path=None, since="2026-01-01"):
    from . import gusto as G
    G.ensure(db)
    checks = [dict(r) for r in db.execute(
        """SELECT id, date, amount, payee, check_no, category, description, status, kind, memo FROM entry
           WHERE client=? AND status!='void' AND amount<0 AND date>=?
             AND (kind='check' OR (description LIKE 'CHECK%' AND description NOT LIKE 'CHECK PRINTING%'
                  AND description NOT LIKE 'CHECK ORDER%'))
           ORDER BY date""", (client, since))]
    paper = []   # what Gusto says was paid outside direct deposit, per person per run
    first_pay = None
    for r in db.execute("SELECT pay_day, kind, period_start, period_end, employees FROM gusto_run WHERE client=? ORDER BY pay_day",
                        (client,)):
        first_pay = first_pay or r["pay_day"]
        for e in json.loads(r["employees"] or "[]"):
            by_check = (e.get("payment") or "").lower().startswith("check") and (e.get("net") or 0) > 0
            cash_tips = round(e.get("cash_tips") or 0, 2)
            if by_check or cash_tips > 0:
                paper.append(dict(name=e["name"], net=round(e["net"], 2) if by_check else 0.0, tips=round(e.get("tips") or 0, 2),
                                  cash_tips=cash_tips, pay_day=r["pay_day"], run=r["kind"], by_check=by_check,
                                  period=(r["period_start"], r["period_end"]), check=None, tips_check=None))
    employees = sorted({p["name"] for p in paper} | set(TC._gusto_employees(db, client)))
    staff = _staff_names(shifts_path)
    d = lambda s: dt.date.fromisoformat(s)

    def in_window(c, p):
        lag = (d(c["date"]) - d(p["pay_day"])).days
        return WINDOW[0] <= lag <= WINDOW[1]

    def targets(p):
        """(what, amount) this Gusto line could have been paid with, in the order to try."""
        t = []
        if p["by_check"] and not p["check"] and p["cash_tips"] and not p["tips_check"]:
            t.append(("net+cash", round(p["net"] + p["cash_tips"], 2)))
        if p["by_check"] and not p["check"]:
            t.append(("net", p["net"]))
        if p["cash_tips"] and not p["tips_check"]:
            t.append(("cash", p["cash_tips"]))
        return t

    used = set()

    def take(p, what, c, diff):
        used.add(c["id"])
        if what in ("net", "net+cash"):
            p["check"] = c
        if what in ("cash", "net+cash"):
            p["tips_check"] = c
        c["paycheck"] = dict({k: v for k, v in p.items() if k not in ("check", "tips_check")}, diff=diff, what=what,
                             expected=round((p["net"] if what != "cash" else 0) + (p["cash_tips"] if what != "net" else 0), 2))

    # pass 1: exact amount + name; 2: exact amount, any name; 3: within $1, same person; 4: same person, any amount (possible)
    def combos():
        """One check covering two paychecks of the same person (e.g. a new-hire run and the next regular run)."""
        for c in checks:
            if c["id"] in used:
                continue
            open_ = [p for p in paper if p["by_check"] and not p["check"] and _names_match(c["payee"], p["name"]) and in_window(c, p)]
            for i in range(len(open_)):
                for j in range(i + 1, len(open_)):
                    a, b = open_[i], open_[j]
                    if abs(-c["amount"] - a["net"] - b["net"]) < 0.005:
                        take(a, "net", c, 0.0)
                        b["check"] = c
                        c["paycheck"]["also"] = dict(pay_day=b["pay_day"], run=b["run"], net=b["net"], tips=b["tips"])
                        c["paycheck"]["expected"] = round(a["net"] + b["net"], 2)
                        break
                if c["id"] in used:
                    break

    for mode in ("exact_named", "exact", "near", "combo", "loose"):
        if mode == "combo":
            combos()
            continue
        for p in paper:
            for what, amt in targets(p):
                best = None
                for c in checks:
                    if c["id"] in used or not in_window(c, p):
                        continue
                    diff = round(-c["amount"] - amt, 2)
                    named = _names_match(c["payee"], p["name"])
                    if mode in ("exact_named", "exact") and abs(diff) >= 0.005:
                        continue
                    if mode == "exact_named" and not named:
                        continue
                    if mode == "near" and not (named and 0.005 <= abs(diff) <= NEAR):
                        continue
                    if mode == "loose" and not (named and what != "cash" and abs(diff) > NEAR):
                        continue
                    key = (abs(diff), abs((d(c["date"]) - d(p["pay_day"])).days))
                    if best is None or key < best[0]:
                        best = (key, c, diff)
                if best:
                    take(p, what, best[1], best[2])
                    if mode == "loose":
                        best[1]["paycheck"]["possible"] = True
                    break
    rows = []
    for c in checks:
        pc = c.get("paycheck")
        who = next((e for e in employees if _names_match(c["payee"], e)), None)
        staff_hit = who or next((s for s in staff if _names_match(c["payee"], s)), None)
        issues = []
        if pc:
            status = ("possible" if pc.get("possible") else "payroll" if abs(pc["diff"]) < 0.005 else "payroll_diff")
            label = {"net": "Gusto net pay", "cash": "Gusto cash tips", "net+cash": "Gusto net pay + cash tips"}[pc["what"]]
            if abs(pc["diff"]) >= 0.005:
                issues.append("bank %s vs %s %s (%s%s)" % (_m(-c["amount"]), label, _m(pc["expected"]),
                                                          "+" if pc["diff"] > 0 else "−", _m(abs(pc["diff"]))))
            if not _names_match(c["payee"], pc["name"]):
                issues.append("payee “%s” — Gusto paycheck is %s" % (c["payee"] or "blank", pc["name"]))
            if c["category"] != "Payroll - Wages":
                issues.append("categorized %s (should be Payroll - Wages)" % c["category"])
        elif staff_hit and first_pay and c["date"] < first_pay:
            status = "before_payroll"
        elif staff_hit:
            status = "staff_no_gusto"
            issues.append("paid to staff outside Gusto payroll — categorized %s" % c["category"])
        else:
            status = "other"
            if not c["payee"] or c["payee"].lower() in ("unknown", "deposit"):
                issues.append("no payee")
            if c["category"] in ("Uncategorized", "Missing Check"):
                issues.append("needs a category")
            if c["category"] == "Payroll - Wages":
                issues.append("categorized Payroll - Wages but no Gusto paycheck matches")
        rows.append(dict(c, status=status, issues=issues, employee=(pc or {}).get("name") or staff_hit))
    rows.reverse()
    outstanding = [p for p in paper if p["by_check"] and not p["check"]]
    for p in outstanding:
        p["age"] = (dt.date.today() - d(p["pay_day"])).days
    # per person
    people = collections.defaultdict(lambda: dict(gusto_checks=0, gusto_net=0.0, tips=0.0, cash_tips=0.0, cleared=0, cleared_net=0.0,
                                                  outside=0, outside_total=0.0, outstanding=0, outstanding_net=0.0, diffs=0))
    for p in paper:
        x = people[p["name"]]
        x["tips"] += p["tips"]; x["cash_tips"] += p["cash_tips"]
        if p["by_check"]:
            x["gusto_checks"] += 1; x["gusto_net"] += p["net"]
            if not p["check"]:
                x["outstanding"] += 1; x["outstanding_net"] += p["net"]
    for r in rows:
        pc = r.get("paycheck")
        if pc:
            x = people[pc["name"]]
            x["cleared"] += 1; x["cleared_net"] += -r["amount"]
            if abs(pc["diff"]) >= 0.005:
                x["diffs"] += 1
        elif r["status"] in ("staff_no_gusto", "before_payroll"):
            x = people[r["employee"]]
            if r["status"] == "before_payroll":
                x["before"] = x.get("before", 0.0) + -r["amount"]
            else:
                x["outside"] += 1; x["outside_total"] += -r["amount"]
    people = sorted(({**{k: (round(v, 2) if isinstance(v, float) else v) for k, v in x.items()}, "name": k}
                     for k, x in people.items()), key=lambda x: x["name"])
    cnt = collections.Counter(r["status"] for r in rows)
    return dict(rows=rows, outstanding=sorted(outstanding, key=lambda p: p["pay_day"], reverse=True), people=people,
                counts=cnt, total=round(-sum(r["amount"] for r in rows), 2),
                payroll_total=round(-sum(r["amount"] for r in rows if r.get("paycheck")), 2),
                before_total=round(-sum(r["amount"] for r in rows if r["status"] == "before_payroll"), 2), first_pay=first_pay,
                unpaid_cash_tips=[p for p in paper if p["cash_tips"] and not p["tips_check"]],
                outside_total=round(-sum(r["amount"] for r in rows if r["status"] == "staff_no_gusto"), 2),
                outstanding_total=round(sum(p["net"] for p in outstanding), 2),
                tips_by_check=round(sum(p["tips"] + p["cash_tips"] for p in paper), 2),
                issues=sum(1 for r in rows if r["issues"]))


def _m(v):
    return "${:,.2f}".format(v)
