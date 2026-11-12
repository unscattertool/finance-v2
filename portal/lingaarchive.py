"""The final Linga export (Linga was removed 10/28/2026): reports saved as Excel in OneDrive …/Data/LingaReportData.

Each file is recognised by its title, cleaned and stored:
  Transaction Report  → linga_payment   (every payment: check, tender, employee, amount, tip, batch)
  Paid In/Out         → linga_paid      (cash taken out of / put into the drawer)
  Attendance Report   → linga_attendance (clock in/out per employee per day)
  Sale Summary Report → day_tax + sale_server (salesum)
  Tax, Gratuity, Refund, Discount, Department, Void, Labour reports → linga_report (totals, as JSON)
The Batch Report CSV and Gusto Payroll Journal CSV in the Data folder are re-imported with their own importers.
"""
import collections
import datetime as dt
import json
import re

from . import xlsxlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS cash_day_alloc (
  client TEXT NOT NULL, date TEXT NOT NULL, cash REAL, deposited REAL, paid REAL, left REAL, deposits TEXT, updated_at TEXT,
  PRIMARY KEY (client, date));
CREATE TABLE IF NOT EXISTS cash_payout (
  id INTEGER PRIMARY KEY, client TEXT NOT NULL, date TEXT NOT NULL, amount REAL NOT NULL, party TEXT, reason TEXT,
  created_by TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS cash_lookback (
  client TEXT NOT NULL, entry_id INTEGER NOT NULL, days INTEGER NOT NULL, set_by TEXT, set_at TEXT, PRIMARY KEY (client, entry_id));
CREATE TABLE IF NOT EXISTS linga_payment (
  client TEXT NOT NULL, seq INTEGER, check_no TEXT, service TEXT, closed_at TEXT, paid_at TEXT, date TEXT, employee TEXT,
  tender TEXT, card TEXT, status TEXT, amount REAL, tip REAL, settled_at TEXT);
CREATE INDEX IF NOT EXISTS linga_payment_d ON linga_payment(client, date);
CREATE TABLE IF NOT EXISTS linga_paid (
  client TEXT NOT NULL, date TEXT, at TEXT, by_name TEXT, amount REAL, party TEXT, reason TEXT, node TEXT);
CREATE TABLE IF NOT EXISTS linga_attendance (
  client TEXT NOT NULL, date TEXT, name TEXT, clock_in TEXT, clock_out TEXT, hours REAL, job TEXT, flag TEXT);
CREATE INDEX IF NOT EXISTS linga_attendance_d ON linga_attendance(client, date);
CREATE TABLE IF NOT EXISTS linga_report (
  client TEXT NOT NULL, kind TEXT NOT NULL, data TEXT, source TEXT, imported_at TEXT, PRIMARY KEY (client, kind));
"""
CARD_TENDERS = ("linga cc", "linga reader")
LOOKBACK = 21  # a cash deposit carries cash from at most this many days before it
STRETCH = 60  # "look further back" on one deposit (cash held in the safe for a while)
SALES_TAX_RATE = 0.0605


def paid_kind(party, reason):
    """'bank' = the cash went to the bank (the deposit is the outflow), 'safe' = drawer cash pulled at end of day
    (still the business's cash, deposited later), 'spent' = cash used for purchases or given out."""
    t = "%s %s" % (party or "", reason or "")
    if re.search(r"deposit|bank", t, re.I):
        return "bank"
    if re.search(r"drawer|end of day|eod|checks|safe", t, re.I):
        return "safe"
    return "spent"


def ensure(db):
    db.executescript(SCHEMA)


def _num(v):
    if v is None or v == "" or v == "-":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except ValueError:
        return 0.0


def _date(v):
    """'10-25-2026' or '10-25-2026 03:18 PM' → ('2026-10-25', '2026-10-25T15:18')."""
    s = str(v or "").strip()
    for fmt in ("%m-%d-%Y %I:%M %p", "%m-%d-%Y"):
        try:
            t = dt.datetime.strptime(s, fmt)
            return t.date().isoformat(), t.isoformat(timespec="minutes")
        except ValueError:
            pass
    return None, None


def _hours(v):
    m = re.match(r"^(\d+):(\d\d)$", str(v or "").strip())
    return round(int(m.group(1)) + int(m.group(2)) / 60, 2) if m else _num(v)


def _name(v):
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _table(rows, must):
    """(header index map, data rows) for the first row containing every column in `must`."""
    for i, r in enumerate(rows[:12]):
        cells = [str(x or "").strip() for x in r]
        if all(m in cells for m in must):
            hdr = {}
            for j, k in enumerate(cells):
                hdr.setdefault(k, j)
            return hdr, rows[i + 1:]
    return None, []


def kind_of(rows):
    first = " ".join(str(x) for x in (rows[0] if rows else []) if x)
    titles = [("Transaction Report", "payments"), ("Paid Report", "paid"), ("Attendance Report", "attendance"),
              ("Sale Summary Report", "sale_summary"), ("Tax Report", "tax"), ("Gratuity Report", "gratuity"),
              ("Refund Report", "refunds"), ("Discount Summary Report", "discounts"), ("Department Sale Report", "departments"),
              ("Void Transaction Report", "void_payments"), ("Void Employee Report", "void_items"), ("Labour Report", "labor"),
              ("Category Sale Report", "categories"), ("Daily Sale Report", "daily_sales")]
    for t, k in titles:
        if first.startswith(t):
            return k
    if rows and str(rows[0][0] or "").strip() == "Date" and "Cash" in [str(x or "") for x in rows[0]]:
        return "daily_tender"
    return None


def _save_report(db, client, kind, data, source):
    db.execute("INSERT OR REPLACE INTO linga_report VALUES(?,?,?,?,?)",
               (client, kind, json.dumps(data), source, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))


def import_rows(db, client, rows, source=""):
    ensure(db)
    kind = kind_of(rows)
    if kind == "payments":
        h, data = _table(rows, ["Check Number", "Tender Name", "Amount"])
        db.execute("DELETE FROM linga_payment WHERE client=?", (client,))
        n = 0
        tot = collections.defaultdict(float)
        for r in data:
            if not r[h["Check Number"]] or str(r[h["Check Number"]]).strip() in ("Total", ""):
                continue
            d, at = _date(r[h["Transaction Date"]])
            if not d:
                continue
            tender = _name(r[h["Tender Name"]])
            amt, tip = _num(r[h["Amount"]]), _num(r[h.get("Tip")]) if "Tip" in h else 0.0
            db.execute("INSERT INTO linga_payment VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (client, int(_num(r[h["Seq.No"]])) if "Seq.No" in h else n, str(r[h["Check Number"]]).strip(),
                        _name(r[h.get("Service Type", 0)]) if "Service Type" in h else "",
                        _date(r[h["Sale Close Date"]])[1] if "Sale Close Date" in h else None, at, d,
                        _name(r[h["Employee Name"]]), tender, str(r[h.get("Card Number")] or "") if "Card Number" in h else "",
                        _name(r[h["Status"]]) if "Status" in h else "", amt, tip,
                        _date(r[h["Batch Settled Date"]])[1] if "Batch Settled Date" in h else None))
            tot[tender] += amt
            n += 1
        return kind, dict(payments=n, by_tender={k: round(v, 2) for k, v in tot.items()})
    if kind == "paid":
        h, data = _table(rows, ["Business Date", "Amount", "Reason"])
        db.execute("DELETE FROM linga_paid WHERE client=?", (client,))
        n, total = 0, 0.0
        for r in data:
            d, _ = _date(r[h["Business Date"]])
            if not d:
                continue
            db.execute("INSERT INTO linga_paid VALUES(?,?,?,?,?,?,?,?)",
                       (client, d, _date(r[h["Transaction Date"]])[1], _name(r[h["Paid In/Out By"]]), _num(r[h["Amount"]]),
                        _name(r[h["Paid From/TO"]]), _name(r[h["Reason"]]), _name(r[h.get("Node")]) if "Node" in h else ""))
            n += 1
            total += _num(r[h["Amount"]])
        return kind, dict(entries=n, total=round(total, 2))
    if kind == "attendance":
        h, data = _table(rows, ["Date", "Name", "Check In", "Check Out", "Hours"])
        db.execute("DELETE FROM linga_attendance WHERE client=?", (client,))
        n, hrs = 0, 0.0
        for r in data:
            d, _ = _date(r[h["Date"]])
            if not d:
                continue
            hours = _hours(r[h["Hours"]])
            flag = "over 14 h: probably didn't clock out" if hours > 14 else ""
            db.execute("INSERT INTO linga_attendance VALUES(?,?,?,?,?,?,?,?)",
                       (client, d, _name(r[h["Name"]]), r[h["Check In"]], r[h["Check Out"]], hours,
                        _name(r[h["Job Code"]]) if "Job Code" in h else "", flag))
            n += 1
            hrs += hours
        return kind, dict(shifts=n, hours=round(hrs, 2))
    if kind == "sale_summary":
        from . import salesum
        r = salesum.import_sale_summary(db, client, rows)
        return kind, r
    # summary-type reports: keep their rows as clean dicts
    musts = {"tax": ["Tax Name"], "gratuity": ["Name", "Gratuity"], "refunds": ["Check Number", "Total Amount"],
             "discounts": ["Discount Name", "Discount Total"], "departments": ["Name", "Amount", "Tax"],
             "void_payments": ["Check Number", "Tender Name", "Amount"], "void_items": ["Check Number", "Item Name", "Amount"],
             "labor": ["Name", "Hours"], "categories": ["Name", "Amount", "Tax"], "daily_sales": ["Date", "Net Sale", "Tax"],
             "daily_tender": ["Date", "Cash"]}
    if kind in musts:
        h, data = _table(rows, musts[kind])
        if h is None:
            return kind, dict(error="columns not found")
        cols = [k for k in h if k]
        items = []
        for r in data:
            item = {k: r[h[k]] for k in cols if h[k] < len(r)}
            first = next((v for v in item.values() if v not in (None, "")), None)
            if first is None:
                continue
            items.append(item)
        if kind == "void_items":  # 770 lines: keep totals by reason and by person
            by_reason, by_person = collections.defaultdict(float), collections.defaultdict(float)
            for it in items:
                if str(it.get("Check Number") or "").strip() in ("Total", ""):
                    continue
                by_reason[_name(it.get("Void Reason")) or "?"] += _num(it.get("Amount"))
                by_person[_name(it.get("Void By")) or "?"] += _num(it.get("Amount"))
            items = dict(by_reason={k: round(v, 2) for k, v in by_reason.items()},
                         by_person={k: round(v, 2) for k, v in by_person.items()},
                         lines=sum(1 for it in items if str(it.get("Check Number") or "").strip() not in ("Total", "")))
        _save_report(db, client, kind, items, source)
        return kind, dict(rows=len(items) if isinstance(items, list) else items.get("lines"))
    return None, None


def import_files(db, client, files):
    """files: [(name, bytes)]. Returns [(name, kind, summary or error)]."""
    from . import tips as T, gusto as G
    ensure(db)
    out = []
    for name, data in sorted(files, key=lambda f: f[0]):
        low = name.lower()
        try:
            if low.endswith(".xlsx"):
                rows = xlsxlite.rows(data)
                kind, summ = import_rows(db, client, rows, source=name)
                out.append((name, kind or "skipped", summ))
            elif low.endswith(".csv"):
                text = data.decode("utf-8-sig", errors="replace")
                if text.lstrip().startswith("Batch Report") or "Batch settled date" in text[:2000]:
                    out.append((name, "batch", T.import_batch_report(db, client, text, "linga-archive")))
                elif "Payroll Journal" in text[:300]:
                    out.append((name, "gusto", G.import_journal(db, client, text, "linga-archive")))
                else:
                    out.append((name, "skipped", None))
            else:
                out.append((name, "skipped", None))
        except Exception as e:  # keep going: one bad file shouldn't stop the archive
            out.append((name, "error", str(e)))
    db.commit()
    return out


def report(db, client, kind):
    ensure(db)
    r = db.execute("SELECT data FROM linga_report WHERE client=? AND kind=?", (client, kind)).fetchone()
    return json.loads(r[0]) if r else None


def tax_totals(db, client):
    """{'NA Sales Tax': x, 'Alc Sales Tax': y, 'State Liquor Tax': z} from the Linga tax report."""
    rows = report(db, client, "tax") or []
    out = {}
    for it in rows:
        name = _name(it.get("Tax Name"))
        if name and name not in ("Total", "No tax"):
            out[name] = round(sum(_num(v) for k, v in it.items() if k != "Tax Name"), 2)
    return out


def status(db, client):
    """What the archive holds, for the tips page."""
    ensure(db)
    p = db.execute("SELECT count(*), min(date), max(date) FROM linga_payment WHERE client=?", (client,)).fetchone()
    tend = {r[0]: round(r[1], 2) for r in db.execute(
        "SELECT tender, sum(amount) FROM linga_payment WHERE client=? AND lower(status)='success' GROUP BY tender", (client,))}
    a = db.execute("SELECT count(*), min(date), max(date), round(sum(hours),1) FROM linga_attendance WHERE client=?", (client,)).fetchone()
    paid = db.execute("SELECT count(*), round(sum(amount),2), min(date), max(date) FROM linga_paid WHERE client=?", (client,)).fetchone()
    kinds = {r[0]: r[1] for r in db.execute("SELECT kind, imported_at FROM linga_report WHERE client=?", (client,))}
    grat = report(db, client, "gratuity") or []
    refunds = [x for x in (report(db, client, "refunds") or []) if str(x.get("Check Number") or "").strip() not in ("", "Total")
               and _date(x.get("Date"))[0]]
    return dict(payments=p[0], first=p[1], last=p[2], tenders=tend, attendance=a[0], att_first=a[1], att_last=a[2], hours=a[3],
                paid=paid[0], paid_total=paid[1], paid_first=paid[2], paid_last=paid[3], reports=sorted(kinds),
                taxes=tax_totals(db, client),
                gratuity=[(x.get("Name"), _num(x.get("Gratuity"))) for x in grat if x.get("Name") and _num(x.get("Gratuity"))],
                refunds=refunds, voids=report(db, client, "void_items"),
                discounts=[x for x in (report(db, client, "discounts") or []) if isinstance(x.get("Discount Name"), str)
                           and not x["Discount Name"].startswith("Total")])


def cash_vs_deposits(db, client):
    """Cash and customer checks taken in (Linga), cash taken out of the drawer (Paid In/Out), and bank cash deposits,
    by month with a running balance of what was taken in but not deposited."""
    ensure(db)
    pays = db.execute("""SELECT date, tender, amount, check_no, employee FROM linga_payment WHERE client=? AND lower(status)='success'
                         AND lower(tender) NOT IN ('linga cc','linga reader')""", (client,)).fetchall()
    if not pays:
        return None
    m = collections.defaultdict(lambda: dict(cash=0.0, checks=0.0, paid_out=0.0, deposits=0.0, n_dep=0))
    checks = []
    for r in pays:
        k = r["date"][:7]
        if r["tender"].lower() == "cash":
            m[k]["cash"] += r["amount"]
        else:
            m[k]["checks"] += r["amount"]
            checks.append(dict(date=r["date"], amount=r["amount"], check_no=r["check_no"], employee=r["employee"], tender=r["tender"]))
    paid = [dict(r) for r in db.execute("SELECT * FROM linga_paid WHERE client=? ORDER BY at", (client,))]
    for r in paid:
        r["kind"] = paid_kind(r["party"], r["reason"])
        r["to_bank"] = r["kind"] == "bank"
        if r["kind"] == "spent":
            m[r["date"][:7]]["paid_out"] += r["amount"]
    first = min(m)
    deps = [dict(r) for r in db.execute("""SELECT id, date, amount, payee, memo FROM entry WHERE client=? AND status!='void' AND amount>0
                                           AND category='Sales - Cash' AND date>=? ORDER BY date""", (client, first + "-01"))]
    for r in deps:
        m[r["date"][:7]]["deposits"] += r["amount"]
        m[r["date"][:7]]["n_dep"] += 1
    run, months = 0.0, []
    for k in sorted(m):
        x = m[k]
        took = x["cash"] + x["checks"]
        run += took - x["deposits"]
        months.append(dict(x, month=k, label=dt.date.fromisoformat(k + "-01").strftime("%b %Y"), took=round(took, 2),
                           running=round(run, 2), **{f: round(x[f], 2) for f in ("cash", "checks", "paid_out", "deposits")}))
    tot = lambda f: round(sum(x[f] for x in months), 2)
    return dict(months=months, checks=checks, paid=paid, deposits=deps, cash=tot("cash"), checks_total=tot("checks"),
                paid_to_bank=round(sum(r["amount"] for r in paid if r["kind"] == "bank"), 2),
                paid_to_safe=round(sum(r["amount"] for r in paid if r["kind"] == "safe"), 2),
                paid_out=tot("paid_out"), deposited=tot("deposits"), not_deposited=round(run, 2))


def hours_by_name(db, client, a, b):
    ensure(db)
    out = collections.defaultdict(float)
    for r in db.execute("SELECT name, sum(hours) FROM attendance_all WHERE client=? AND date BETWEEN ? AND ? AND flag='' GROUP BY name",
                        (client, a, b)):
        out[r[0]] += r[1] or 0
    return out


def hours_check(db, client, since="2026-04-27"):
    """Per Gusto pay period and employee: hours clocked in Linga vs hours paid in Gusto."""
    from . import tipcheck as TC
    ensure(db)
    from . import posimport
    posimport.ensure(db)
    if not db.execute("SELECT 1 FROM attendance_all WHERE client=? LIMIT 1", (client,)).fetchone():
        return None
    m, employees = TC.mapping(db, client)
    who = lambda n: (m.get(("batch", n)) if m.get(("batch", n)) not in (None, TC.BAR, TC.NONE, TC.OWNER) else None)
    periods = collections.OrderedDict()
    for r in db.execute("""SELECT period_start, period_end, employees FROM gusto_run WHERE client=? AND period_start IS NOT NULL
                           AND period_end>=? ORDER BY period_start""", (client, since)):
        key = (r["period_start"], r["period_end"])
        p = periods.setdefault(key, collections.defaultdict(lambda: dict(gusto=0.0, linga=0.0, raw=0.0, names=set(), flags=0)))
        for e in json.loads(r["employees"] or "[]"):
            p[e["name"]]["gusto"] += e.get("hours") or 0
    out, unmatched = [], collections.defaultdict(float)
    for (a, b), p in periods.items():
        if (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days != 13:
            continue  # off-cycle and new-hire runs share the regular period's hours
        for r in db.execute("SELECT name, hours, flag FROM attendance_all WHERE client=? AND date BETWEEN ? AND ?", (client, a, b)):
            e = who(r["name"])
            if not e:
                unmatched[r["name"]] += r["hours"] or 0
                continue
            p[e]["raw"] += r["hours"] or 0
            if r["flag"]:  # missed clock-out: Gusto has the corrected hours
                p[e]["flags"] += 1
            else:
                p[e]["linga"] += r["hours"] or 0
            p[e]["names"].add(r["name"])
        rows = []
        for name, x in sorted(p.items()):
            if x["gusto"] < 0.01 and x["raw"] < 0.01:
                continue
            diff = round(x["linga"] - x["gusto"], 2)
            rows.append(dict(name=name, gusto=round(x["gusto"], 2), linga=round(x["linga"], 2), raw=round(x["raw"], 2), diff=diff,
                             flags=x["flags"], no_clock=x["raw"] < 0.01, off=abs(diff) > 2 and x["raw"] >= 0.01 and not x["flags"]))
        out.append(dict(start=a, end=b, rows=rows, gusto=round(sum(r["gusto"] for r in rows), 2),
                        linga=round(sum(r["linga"] for r in rows), 2), off=sum(1 for r in rows if r["off"])))
    return dict(periods=list(reversed(out)), unmatched={k: round(v, 1) for k, v in unmatched.items() if v},
                long_shifts=[dict(r) for r in db.execute("SELECT date, name, clock_in, clock_out, hours FROM attendance_all "
                                                          "WHERE client=? AND flag!='' ORDER BY date DESC", (client,))])


def add_payout(db, client, date, amount, party, reason, user):
    """Cash taken out of the till/safe that never goes to the bank (owner draw, cash paid out). Covers that date's and
    earlier days' cash, like a deposit."""
    ensure(db)
    db.execute("INSERT INTO cash_payout(client,date,amount,party,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
               (client, date, round(amount, 2), party, reason, user, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
    db.commit()


def delete_payout(db, client, pid):
    db.execute("DELETE FROM cash_payout WHERE client=? AND id=?", (client, pid))
    db.commit()


def set_lookback(db, client, entry_id, days, user):
    """Let one bank deposit carry older cash (days > the usual 21), or back to normal with days=None."""
    ensure(db)
    if days:
        db.execute("INSERT OR REPLACE INTO cash_lookback VALUES(?,?,?,?,?)", (client, entry_id, int(days), user,
                                                                            dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
    else:
        db.execute("DELETE FROM cash_lookback WHERE client=? AND entry_id=?", (client, entry_id))
    db.commit()


def _save_day_alloc(db, client, days):
    """Each business day's cash: how much went to the bank and to paid-outs (read by Shifts' cash card)."""
    import json
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        db.execute("DELETE FROM cash_day_alloc WHERE client=?", (client,))
        for d in days:
            dep = [w for w in d["went"] if w["kind"] == "deposit"]
            db.execute("INSERT INTO cash_day_alloc VALUES(?,?,?,?,?,?,?,?)",
                       (client, d["date"], round(d["cash"] + d["checks"], 2), round(sum(w["amount"] for w in dep), 2),
                        round(sum(w["amount"] for w in d["went"] if w["kind"] == "paid"), 2), d["left"],
                        json.dumps([dict(label=w["label"], amount=w["amount"], kind=w["kind"]) for w in d["went"]]), ts))
        db.commit()
    except Exception:  # never break the page over the cache
        db.rollback()


def deposit_allocation(db, client, shifts_path=None):
    """Which business days' cash each cash deposit carried, oldest cash first.

    Cash in by day = Linga cash + customer checks (Transaction Report); after Linga ended, cash turned in on the Shifts
    closeouts. Cash out = bank deposits from the till (recorded on the Cash page with "It's from the till", plus
    any other bank deposit categorized Sales - Cash) and drawer paid-outs Linga recorded. Each outflow takes the oldest
    cash still on hand from days before it; a deposit bigger than the cash on hand shows the excess."""
    ensure(db)
    days = collections.OrderedDict()
    for r in db.execute("""SELECT date, sum(CASE WHEN lower(tender)='cash' THEN amount ELSE 0 END) cash,
                                  sum(CASE WHEN lower(tender) NOT IN ('cash','linga cc','linga reader') THEN amount ELSE 0 END) chk
                           FROM linga_payment WHERE client=? AND lower(status)='success' GROUP BY date ORDER BY date""", (client,)):
        if (r["cash"] or 0) + (r["chk"] or 0) > 0.005:
            days[r["date"]] = dict(date=r["date"], cash=round(r["cash"] or 0, 2), checks=round(r["chk"] or 0, 2), source="Linga")
    if not days:
        return None
    last_linga = max(days)
    if shifts_path:
        from . import cash as C
        a = dt.date.fromisoformat(last_linga) + dt.timedelta(days=1)
        for d, x in sorted(C.shifts_by_day(shifts_path, a, dt.date.today()).items()):
            if x["turned_in"] > 0.005:
                days[d] = dict(date=d, cash=round(x["turned_in"], 2), checks=0.0, source="Shifts")
    from . import posreports
    for d, v in sorted(posreports.cash_by_day(db, client).items()):  # the POS's own cash report wins over closeouts
        if d > last_linga and v > 0.005:
            days[d] = dict(date=d, cash=v, checks=0.0, source="POS")
    days = collections.OrderedDict(sorted(days.items()))
    first = min(days)
    for d in days.values():
        d.update(taken=round(d["cash"] + d["checks"], 2), left=round(d["cash"] + d["checks"], 2), went=[])
    # outflows. Each takes the most recent cash still on hand up to its cut-off day (what was in the drawer/safe when it
    # left); older cash nobody deposited stays on its day as "not deposited".
    to_bank = []
    for r in db.execute("SELECT * FROM linga_paid WHERE client=? ORDER BY at", (client,)):
        r = dict(r)
        hour = int((r["at"] or "T00")[11:13] or 0) if r["at"] and len(r["at"]) >= 13 else 0
        r["cutoff"] = r["date"] if hour >= 14 else (dt.date.fromisoformat(r["date"]) - dt.timedelta(days=1)).isoformat()
        r["kind"] = paid_kind(r["party"], r["reason"])
        r["to_bank"] = r["kind"] == "bank"
        to_bank.append(r)
    outs = []
    stretch = {r[0]: r[1] for r in db.execute("SELECT entry_id, days FROM cash_lookback WHERE client=?", (client,))}
    rec = {r["entry_id"]: dict(r) for r in db.execute("SELECT * FROM cash_deposit WHERE client=? AND entry_id IS NOT NULL", (client,))}
    used_hint = set()
    for r in db.execute("""SELECT id, date, amount, category, description FROM entry WHERE client=? AND status!='void' AND amount>0
                           AND date>=? AND (category='Sales - Cash' OR id IN (SELECT entry_id FROM cash_deposit WHERE client=?
                           AND entry_id IS NOT NULL)) ORDER BY date, id""", (client, first, client)):
        cutoff = (dt.date.fromisoformat(r["date"]) - dt.timedelta(days=1)).isoformat()
        hint = next((p for i, p in enumerate(to_bank) if p["to_bank"] and i not in used_hint and abs(p["amount"] - r["amount"]) < 0.01
                     and 0 <= (dt.date.fromisoformat(r["date"]) - dt.date.fromisoformat(p["date"])).days <= 7), None)
        if hint:
            used_hint.add(to_bank.index(hint))
            cutoff = hint["cutoff"]
        if r["id"] in rec and rec[r["id"]]["date"] < r["date"]:
            cutoff = min(cutoff, rec[r["id"]]["date"])  # recorded on the Cash page as taken to the bank that day
        outs.append(dict(kind="deposit", key="dep-%d" % r["id"], entry_id=r["id"], date=r["date"], cutoff=cutoff,
                         amount=round(r["amount"], 2), recorded=r["id"] in rec, lookback=stretch.get(r["id"], LOOKBACK),
                         stretched=r["id"] in stretch, label="Bank deposit %s" % dt.date.fromisoformat(r["date"]).strftime("%-m/%-d")))
    for r in db.execute("SELECT * FROM cash_deposit WHERE client=? AND entry_id IS NULL AND date>=?", (client, first)):
        outs.append(dict(kind="deposit", key="rec-%d" % r["id"], entry_id=None, date=r["date"], cutoff=r["date"],
                         amount=round(r["amount"], 2), recorded=True,
                         label="Deposit %s (not at bank yet)" % dt.date.fromisoformat(r["date"]).strftime("%-m/%-d")))
    for r in to_bank:
        if r["kind"] != "spent":
            continue  # bank: the deposit itself is the outflow; safe: still on hand until deposited
        outs.append(dict(kind="paid", key="paid-%s-%s" % (r["date"], int(r["amount"] * 100)), entry_id=None, date=r["date"],
                         cutoff=r["date"], amount=round(r["amount"], 2), recorded=True,
                         label="Paid out %s: %s (%s)" % (dt.date.fromisoformat(r["date"]).strftime("%-m/%-d"), r["party"], r["reason"])))
    for r in db.execute("SELECT * FROM cash_payout WHERE client=? ORDER BY date", (client,)):  # recorded on the Cash page
        outs.append(dict(kind="paid", key="payout-%d" % r["id"], payout_id=r["id"], entry_id=None, date=r["date"], cutoff=r["date"],
                         amount=round(r["amount"], 2), recorded=True,
                         label="%s %s: %s" % (r["reason"] or "Paid out", dt.date.fromisoformat(r["date"]).strftime("%-m/%-d"), r["party"] or "")))
    outs.sort(key=lambda o: (o["cutoff"], o["date"]))
    order = list(days.values())
    for o in outs:
        need, cov = o["amount"], []
        for d in reversed(order):  # newest cash first
            if need < 0.005:
                break
            if d["date"] > o["cutoff"] or d["left"] < 0.005:
                continue
            if (dt.date.fromisoformat(o["cutoff"]) - dt.date.fromisoformat(d["date"])).days > o.get("lookback", LOOKBACK):
                break
            take = round(min(d["left"], need), 2)
            d["left"] = round(d["left"] - take, 2)
            need = round(need - take, 2)
            cov.append(dict(date=d["date"], amount=take, partial=take < d["taken"] - 0.005))
            d["went"].append(dict(key=o["key"], label=o["label"], amount=take, kind=o["kind"]))
        cov.sort(key=lambda c: c["date"])
        o["covers"] = cov
        o["excess"] = round(need, 2)  # more than the recorded cash on hand
        o["span"] = (cov[0]["date"], cov[-1]["date"]) if cov else None
    by_entry = {o["entry_id"]: o for o in outs if o["entry_id"]}
    _save_day_alloc(db, client, order)
    return dict(days=list(reversed(order)), outs=list(reversed(outs)), by_entry=by_entry,
                on_hand=round(sum(d["left"] for d in order), 2), last_linga=last_linga,
                excess=round(sum(o["excess"] for o in outs), 2))
