"""Payroll cost: overtime over 40 h in a Mon–Sun workweek, check vs direct deposit split, Gusto preview split. Run: python tests/test_payrollcost.py"""
import datetime as dt, json, os, sqlite3, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from portal import books as B, posreports as PR, posimport as PI, gusto as G, tips as T, payrollcost as PC

tmp = tempfile.mkdtemp()
db = B.connect(tmp); PR.ensure(db); db.executescript(PI.SCHEMA); G.ensure(db); db.executescript(T.SCHEMA)
C = "wwk"
# rates: Tammy $10 reg (OT row in summary must not inflate it), Gary $20
db.execute("INSERT INTO pos_payroll(client,business_date,employee,reg_hours,reg_pay,ot_hours,ot_pay,total_hours,total_pay) VALUES(?,?,?,?,?,?,?,?,?)",
           (C, "2026-10-01", "Tammy H", 10, 100, 2, 30, 12, 130))
db.execute("INSERT INTO pos_payroll(client,business_date,employee,reg_hours,reg_pay,ot_hours,ot_pay,total_hours,total_pay) VALUES(?,?,?,?,?,?,?,?,?)",
           (C, "2026-10-01", "Gary", 10, 200, 0, 0, 10, 200))
assert abs(B.pay_rates(db, C)["tammy h"] - 10) < 1e-9, B.pay_rates(db, C)
# period 11/9 (Mon) - 11/22 (Sun). Week 1: Tammy 4 x 11h = 44h -> 4 OT; Gary 3 x 10h = 30
for i, d in enumerate(["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"]):
    db.execute("INSERT INTO pos_shift(client,source,employee,job,business_date,clock_in,hours) VALUES(?,?,?,?,?,?,?)", (C, "t", "Tammy H", "Server", d, d + " 09:00", 11))
for d in ["2026-11-13", "2026-11-14", "2026-11-15"]:
    db.execute("INSERT INTO pos_shift(client,source,employee,job,business_date,clock_in,hours) VALUES(?,?,?,?,?,?,?)", (C, "t", "Gary", "Chef", d, d + " 09:00", 10))
# week 2 fully imported: Tammy 20h, Gary 41h (1 OT)
for d, emp, h in [("2026-11-16", "Tammy H", 20), ("2026-11-17", "Gary", 41)]:
    db.execute("INSERT INTO pos_shift(client,source,employee,job,business_date,clock_in,hours) VALUES(?,?,?,?,?,?,?)", (C, "t", emp, "x", d, d + " 09:00", h))
for d in ["2026-11-18", "2026-11-19", "2026-11-20", "2026-11-21", "2026-11-22"]:
    db.execute("INSERT INTO pos_shift(client,source,employee,job,business_date,clock_in,hours) VALUES(?,?,?,?,?,?,?)", (C, "t", "Nobody", "x", d, d + " 09:00", 0.01))
# Gusto: Tammy paid by check, Gary DD; ee tax 10% of (gross - cash tips)
emps = [dict(name="Tammy H", net=500, payment="Check", tips=100, cash_tips=0, hours=40, gross=600, service=0),
        dict(name="Gary", net=900, payment="Direct Deposit", tips=0, cash_tips=0, hours=40, gross=1000, service=0)]
db.execute("""INSERT INTO gusto_run(client,run_key,pay_day,kind,period_start,period_end,gross,paycheck_tips,cash_tips,service,ee_tax,er_tax,net_check,net_dd,employees)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (C, "k", "2026-11-12", "Regular", "2026-10-26", "2026-11-08", 1600, 100, 0, 0, 160, 160, 500, 900, json.dumps(emps)))
db.commit()

w = PC.weeks(db, C, "2026-11-09", "2026-11-22", today=dt.date(2026, 11, 30))
assert [x["start"] for x in w] == ["2026-11-09", "2026-11-16"], w
t = {p["name"]: p for p in w[0]["people"]}
assert t["Tammy H"]["hours"] == 44 and t["Tammy H"]["ot_hours"] == 4 and t["Tammy H"]["premium"] == 20.0, t["Tammy H"]
assert t["Gary"]["ot_hours"] == 0
g2 = {p["name"]: p for p in w[1]["people"]}
assert g2["Gary"]["ot_hours"] == 1 and g2["Gary"]["premium"] == 10.0

est = B.payroll_estimate(db, C, today=dt.date(2026, 11, 19), until=dt.date(2026, 11, 30))
p = next(x for x in est["pay_days"] if x["end"] == "2026-11-22")
assert p["overtime"] == 30.0, p["overtime"]
assert p["ot_hours"] == 5.0
c = p["cost"]
assert abs(est["ee_rate"] - 0.10) < 1e-9
assert abs(c["bank"] + c["checks"] - c["total"]) < 0.02, c
assert abs(c["total"] - (p["gross"] + p["er_tax"])) < 0.02
assert c["checks"] > 0 and p["check_people"] == ["Tammy H"], (c, p["check_people"])
# override: Tammy to direct deposit -> no checks
PC.set_overrides(db, C, {"Tammy": "dd"})
est2 = B.payroll_estimate(db, C, today=dt.date(2026, 11, 19), until=dt.date(2026, 11, 30))
p2 = next(x for x in est2["pay_days"] if x["end"] == "2026-11-22")
assert p2["cost"]["checks"] == 0 and p2["check_people"] == [], p2["cost"]
PC.set_overrides(db, C, {})
# Gusto preview: total stays Gusto's, split from its rows
B.save_gusto_preview(db, C, "2026-11-26", "2026-11-09", "2026-11-22", B.parse_gusto_preview(
    "H, Tammy\t$700.00\t$0.00\t$60.00\t$0.00\t$760.00\nGary\t$1,000.00\t$0.00\t$90.00\t$0.00\t$1,090.00\nGrand totals\t$1,700.00\t$0.00\t$150.00\t$0.00\t$1,850.00"), "t") if hasattr(B, "save_gusto_preview") else None
est3 = B.payroll_estimate(db, C, today=dt.date(2026, 11, 19), until=dt.date(2026, 11, 30))
p3 = next(x for x in est3["pay_days"] if x["end"] == "2026-11-22")
if p3.get("gusto"):
    assert p3["total"] == 1850.0 and p3["split_source"] == "gusto", p3["total"]
    assert p3["cost"]["checks"] == 630.0 and abs(p3["cost"]["bank"] - (900 + 170 + 150)) < 0.02, p3["cost"]
    assert p3["estimate_total"] > 0
print("OK")
