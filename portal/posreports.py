"""Standard daily reports exported from a POS back office (Heartland Restaurant first), recognised by their title line:

  TRANSACTIONS <Day> MM-DD-YYYY   → pos_payment  (one row per payment: ticket, staff, time, card/cash, brand, status, amount, tip)
  BATCH CREDIT CARD <Day> …       → pos_batch    (each card batch = one bank deposit: card sales + card tips)
  SALES <Day> … (Daily Summary)   → pos_daily    (net sales, tax, tips, card/cash by tender, expected cash, tickets, alcohol sales)
  TAXES <Day> …                   → pos_tax      (tax collected by type: Sales Tax, Liquor Tax) → day_tax (gross, net, tax)
  PAYROLL SUMMARY <Day> …         → pos_payroll  (per employee: regular / overtime hours and pay)
  NAME - JOB [<Day> …] (Time Clock) → pos_shift  (punches: clock in/out, hours, reported tips)
  CASH SUMMARY <Day> MM-DD-YYYY   → pos_cash     (per server: cash, cash voids, tips & payouts, pay in/out, expected cash)
  TIPS & PAYOUTS <Day> MM-DD-YYYY → pos_tips     (per server: tips paid in cash / through payroll, tip-outs, reported cash tips)

Several reports can be pasted together or uploaded as several files; each is stored for its business date and replaces an
earlier import of the same report, POS and date. Used by: card sales + tips per day (deposit matching, sales_day source
'pos'), card tips per server (tips check), sales per server (missing closeouts), cash per day (Cash page).
"""
import collections
import csv
import datetime as dt
import io
import re

SCHEMA = """
CREATE TABLE IF NOT EXISTS pos_payment (
  client TEXT NOT NULL, import_id INTEGER, source TEXT, business_date TEXT, employee TEXT, ticket TEXT, paid_at TEXT,
  tender TEXT, card_type TEXT, status TEXT, amount REAL, tip REAL, total REAL, card TEXT, reference TEXT);
CREATE INDEX IF NOT EXISTS pos_payment_d ON pos_payment(client, business_date);
CREATE TABLE IF NOT EXISTS pos_cash (
  client TEXT NOT NULL, import_id INTEGER, source TEXT, business_date TEXT, employee TEXT, cash REAL, cash_voids REAL,
  tips_payouts REAL, pay_in_out REAL, server_balance REAL, expected_cash REAL);
CREATE TABLE IF NOT EXISTS pos_batch (
  client TEXT NOT NULL, import_id INTEGER, source TEXT, business_date TEXT, device TEXT, batch_id TEXT, status TEXT, opened TEXT,
  closed TEXT, txns INTEGER, amount REAL);
CREATE TABLE IF NOT EXISTS pos_payroll (
  client TEXT NOT NULL, import_id INTEGER, source TEXT, business_date TEXT, employee TEXT, reg_hours REAL, reg_pay REAL,
  ot_hours REAL, ot_pay REAL, total_hours REAL, total_pay REAL);
CREATE TABLE IF NOT EXISTS pos_daily (
  client TEXT NOT NULL, import_id INTEGER, source TEXT, business_date TEXT, gross_sales REAL, discounts REAL, adjustments REAL,
  net_sales REAL, taxes REAL, tips REAL, gross_receipts REAL, card_amount REAL, card_tips REAL, cash_amount REAL, cash_transactions REAL,
  cash_voids REAL, pay_in REAL, pay_out REAL, expected_cash REAL, tickets INTEGER, guests INTEGER, alcohol_net REAL, liquor_net REAL,
  data TEXT);
CREATE TABLE IF NOT EXISTS pos_tax (
  client TEXT NOT NULL, import_id INTEGER, source TEXT, business_date TEXT, tax_name TEXT, collected REAL, inclusive REAL, total REAL);
CREATE TABLE IF NOT EXISTS pos_tips (
  client TEXT NOT NULL, import_id INTEGER, source TEXT, business_date TEXT, employee TEXT, server_cash REAL, server_payroll REAL,
  merchant REAL, tipout_cash REAL, tipout_payroll REAL, total REAL, reported REAL);
"""
TITLE = re.compile(r'^\W*(TRANSACTIONS|CASH SUMMARY|TIPS\s*&\s*PAYOUTS|BATCH CREDIT CARD|PAYROLL SUMMARY|TAXES|SALES)\s+[A-Za-z]{3}\w*\s+'
                   r'(\d{1,2})[-/](\d{1,2})[-/](\d{4})', re.I)
KINDS = {"TRANSACTIONS": "payments", "CASH SUMMARY": "cash", "TIPS & PAYOUTS": "tips", "BATCH CREDIT CARD": "batch",
         "PAYROLL SUMMARY": "payroll", "TAXES": "taxes",
         "SALES": "summary"}  # SALES <Day> date = Heartland's Daily Summary (sales, transactions, cash, tickets, categories…)
# Time Clock report: one block per employee and job, titled "ASHLEY BOGLE - KITCHEN" (the first one also carries the date)
CLOCK_TITLE = re.compile(r"^([A-Z][A-Z.' -]*?[A-Z.])\s+-\s+([A-Z][A-Z0-9 /&+-]*?)(?:\s+[A-Z][a-z]{2}\s+(\d{1,2})-(\d{1,2})-(\d{4}))?\s*$")


def ensure(db):
    db.executescript(SCHEMA)


def _num(v):
    s = str(v or "").strip()
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-")
    s = re.sub(r"[^0-9.]", "", s)
    try:
        return -float(s) if neg and s else float(s) if s else 0.0
    except ValueError:
        return 0.0


def _name(*parts):
    return re.sub(r"\s+", " ", " ".join(str(p or "").strip() for p in parts)).strip()


RANGE_END = re.compile(r'^\s*-\s*[A-Za-z]{3}\w*\s+(\d{1,2})[-/](\d{1,2})[-/](\d{4})')


def split_sections(text, skipped=None):
    """→ ([dict(kind, date, header, rows)], remaining text without those sections)."""
    lines = text.replace("\r", "").split("\n")
    sections, rest, cur = [], [], None
    for line in lines:
        m = TITLE.match(line.strip())
        if m:
            kind = KINDS[re.sub(r"\s+", " ", re.sub(r"\s*&\s*", " & ", m.group(1).upper()))]
            cur = dict(kind=kind, date="%s-%02d-%02d" % (m.group(4), int(m.group(2)), int(m.group(3))), lines=[])
            e = RANGE_END.match(line.strip()[m.end():])
            if e:  # a report for a date range is totals for the whole range, not one day: not stored
                to = "%s-%02d-%02d" % (e.group(3), int(e.group(1)), int(e.group(2)))
                if to != cur["date"]:
                    cur["range"] = to
                    if skipped is not None:
                        skipped.append("%s %s – %s" % (NAMES[kind], cur["date"], to))
            sections.append(cur)
            continue
        m = CLOCK_TITLE.match(line.strip())
        if m:
            d = "%s-%02d-%02d" % (m.group(5), int(m.group(3)), int(m.group(4))) if m.group(5) else None
            cur = dict(kind="clock", date=d, lines=[], employee=m.group(1).title(), job=m.group(2).title())
            sections.append(cur)
            continue
        if cur is not None:
            if not line.strip():
                cur = None if cur["lines"] else cur
                continue
            cur["lines"].append(line)
        else:
            rest.append(line)
    out = []
    for s in sections:
        if s.get("range"):
            continue
        if s["kind"] == "summary":
            out.append(dict(kind="summary", date=s["date"], header={}, rows=[], lines=s["lines"]))
            continue
        rows = [r for r in csv.reader(io.StringIO("\n".join(s["lines"]))) if any(c.strip() for c in r)]
        if not rows:
            continue
        hdr = {re.sub(r"\s+", " ", h.strip().lower()): i for i, h in enumerate(rows[0])}
        data = [r for r in rows[1:] if r and r[0].strip().lower() != "total"]
        if s["kind"] == "clock":
            if "start date" not in hdr:
                continue
            d = s["date"] or (_clock_dt(data[0][hdr["start date"]], "") or "")[:10] if data else s["date"]
            if not d:
                continue
            out.append(dict(kind="clock", date=d, header=hdr, rows=data, employee=s["employee"], job=s["job"]))
            continue
        out.append(dict(kind=s["kind"], date=s["date"], header=hdr, rows=data))
    return out, "\n".join(rest)


def _get(hdr, r, *names):
    for n in names:
        i = hdr.get(n)
        if i is not None and i < len(r):
            return r[i]
    return ""


def _paid_at(v, day):
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{2,4}),?\s*(\d{1,2}):(\d{2})\s*([ap]m)?", str(v or "").strip(), re.I)
    if not m:
        return None
    y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
    h = int(m.group(4)) % 12 + (12 if (m.group(6) or "").lower() == "pm" else 0) if m.group(6) else int(m.group(4))
    return "%04d-%02d-%02dT%02d:%s" % (y, int(m.group(1)), int(m.group(2)), h, m.group(5))


def _clock_dt(day, tm):
    """"Fri, 10-30-26" + "3:00 pm" → 2026-10-30T15:00 (date alone when no time)."""
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})", str(day or ""))
    if not m:
        return None
    y = int(m.group(3)) + (2000 if len(m.group(3)) == 2 else 0)
    d = "%04d-%02d-%02d" % (y, int(m.group(1)), int(m.group(2)))
    t = re.match(r"(\d{1,2}):(\d{2})\s*([ap]m)?", str(tm or "").strip(), re.I)
    if not t:
        return d + "T00:00" if tm == "" else None
    h = int(t.group(1))
    if t.group(3):
        h = h % 12 + (12 if t.group(3).lower() == "pm" else 0)
    return "%sT%02d:%s" % (d, h, t.group(2))


LIQUOR_RATE = 0.135  # Oklahoma mixed beverage gross receipts tax: same rate on all alcohol (liquor, beer, wine)


def parse_summary(lines):
    """Heartland Daily Summary → one dict: sales, tenders, cash, tickets/guests, alcohol sales, plus every table as JSON."""
    import json
    sec, kv, tables = "SALES", collections.defaultdict(dict), collections.defaultdict(list)
    for line in lines:
        t = line.strip()
        if not t:
            continue
        if not t.startswith('"'):
            sec = re.sub(r"\s+", " ", t.upper())
            continue
        row = next(csv.reader([t]))
        if sec in ("SALES", "CASH", "TAX CATEGORIES") and len(row) >= 2:
            kv[sec][row[0].strip().lower()] = row[1]
        else:
            tables[sec].append(row)

    def tab(name):
        rows = tables.get(name) or []
        if not rows:
            return []
        hdr = [h.strip().lower() for h in rows[0]]
        return [dict(zip(hdr, r)) for r in rows[1:]]
    S, C = kv["SALES"], kv["CASH"]
    v = lambda m, *ks: sum(_num(m.get(k)) for k in ks)
    tx = [r for r in tab("TRANSACTIONS") if not (r.get("type") or "").lower().startswith("total")]
    card = [r for r in tx if re.search(r"credit|debit|card", r.get("type") or "", re.I)]
    cash = [r for r in tx if re.search(r"^cash", r.get("type") or "", re.I)]
    tick = {(r.get("description") or "").lower(): r for r in tab("CLOSED TICKETS")}
    cats = [r for r in tab("REPORT CATEGORIES") if (r.get("type") or "").lower() != "total"]
    alcohol = sum(_num(r.get("net")) for r in cats if re.search(r"liquor|spirit|cocktail|beer|wine", r.get("type") or "", re.I))
    liquor = sum(_num(r.get("net")) for r in cats if re.search(r"liquor|spirit|cocktail", r.get("type") or "", re.I))
    return dict(gross_sales=v(S, "gross sales"), discounts=v(S, "discounts", "auto pricing discounts", "non-revenue discounts", "loyalty rewards"),
                adjustments=v(S, "adjustments", "non-revenue adjustments"), net_sales=v(S, "net sales"), taxes=v(S, "taxes", "taxes (inclusive)"),
                tips=v(S, "tips"), gross_receipts=v(S, "gross receipts"),
                card_amount=round(sum(_num(r.get("amount")) for r in card), 2), card_tips=round(sum(_num(r.get("tip")) for r in card), 2),
                cash_amount=round(sum(_num(r.get("amount")) for r in cash), 2), cash_transactions=v(C, "cash transactions"),
                cash_voids=v(C, "cash voids"), pay_in=v(C, "pay in"), pay_out=v(C, "pay out"), expected_cash=v(C, "expected cash"),
                tickets=int(_num((tick.get("tickets") or {}).get("qty"))), guests=int(_num((tick.get("guests") or {}).get("qty"))),
                alcohol_net=round(alcohol, 2), liquor_net=round(liquor, 2),
                data=json.dumps(dict(values={k: dict(m) for k, m in kv.items()}, tables={k: r for k, r in tables.items()})))


def parse(section):
    k, h, d = section["kind"], section["header"], section["date"]
    out = []
    if k == "summary":
        return [parse_summary(section.get("lines") or [])]
    if k == "clock":
        for r in section["rows"]:
            cin = _clock_dt(_get(h, r, "start date"), _get(h, r, "start time"))
            cout = _clock_dt(_get(h, r, "end date"), _get(h, r, "end time"))
            if not cin:
                continue
            hrs = _get(h, r, "duration (hours)", "duration", "regular hours")
            out.append(dict(employee=section["employee"], job=section["job"], business_date=cin[:10], clock_in=cin, clock_out=cout,
                            hours=_num(hrs) if str(hrs).strip() else None, declared_tips=_num(_get(h, r, "reported tips"))))
        return out
    if k == "batch":
        for r in section["rows"]:
            out.append(dict(device=_get(h, r, "device id").strip(), batch_id=_get(h, r, "batch id").strip(), status=_get(h, r, "status").strip(),
                            opened=_paid_at(_get(h, r, "open date (merchant)", "open date"), d),
                            closed=_paid_at(_get(h, r, "close date (merchant)", "close date"), d),
                            txns=int(_num(_get(h, r, "batch transaction count"))), amount=_num(_get(h, r, "batch transaction amount"))))
        return out
    if k == "taxes":
        for r in section["rows"]:
            n = _get(h, r, "tax", "tax name", "name").strip()
            if n:
                out.append(dict(tax_name=n, collected=_num(_get(h, r, "collected")), inclusive=_num(_get(h, r, "inclusive")),
                                total=_num(_get(h, r, "total"))))
        return out
    if k == "payroll":
        for r in section["rows"]:
            emp = _name(_get(h, r, "first name"), _get(h, r, "last name"))
            if emp:
                out.append(dict(employee=emp, reg_hours=_num(_get(h, r, "reg hours")), reg_pay=_num(_get(h, r, "reg pay")),
                                ot_hours=_num(_get(h, r, "ot hours")), ot_pay=_num(_get(h, r, "ot pay")),
                                total_hours=_num(_get(h, r, "total hours")), total_pay=_num(_get(h, r, "total pay"))))
        return out
    for r in section["rows"]:
        if k == "payments":
            emp = _name(_get(h, r, "staff", "server", "employee"))
            if not emp:
                continue
            typ = _get(h, r, "type", "tender").strip()
            out.append(dict(employee=emp, ticket=_get(h, r, "ticket #", "ticket", "check #").strip(), paid_at=_paid_at(_get(h, r, "date"), d),
                            tender="card" if re.search(r"card|credit|debit", typ, re.I) else "cash" if re.search(r"cash", typ, re.I) else typ.lower(),
                            card_type=_get(h, r, "card type").strip(), status=_get(h, r, "status").strip(),
                            amount=_num(_get(h, r, "amount")), tip=_num(_get(h, r, "tip")), total=_num(_get(h, r, "total")),
                            card=_get(h, r, "card number").strip()[-4:], reference=_get(h, r, "reference").strip()))
        else:
            emp = _name(_get(h, r, "first name"), _get(h, r, "last name")) or _name(_get(h, r, "employee", "name"))
            if not emp:
                continue
            if k == "cash":
                out.append(dict(employee=emp, cash=_num(_get(h, r, "cash")), cash_voids=_num(_get(h, r, "cash voids")),
                                tips_payouts=_num(_get(h, r, "tips & payouts")), pay_in_out=_num(_get(h, r, "pay in/out")),
                                server_balance=_num(_get(h, r, "server balance")), expected_cash=_num(_get(h, r, "expected cash"))))
            else:
                out.append(dict(employee=emp, server_cash=_num(_get(h, r, "server - cash")), server_payroll=_num(_get(h, r, "server - payroll")),
                                merchant=_num(_get(h, r, "merchant")), tipout_cash=_num(_get(h, r, "tip outs - cash")),
                                tipout_payroll=_num(_get(h, r, "tip outs - payroll")), total=_num(_get(h, r, "total")),
                                reported=_num(_get(h, r, "server reported tips"))))
    return out


TABLE = {"payments": "pos_payment", "cash": "pos_cash", "tips": "pos_tips", "batch": "pos_batch", "payroll": "pos_payroll", "taxes": "pos_tax", "summary": "pos_daily"}
COLS = {"payments": ["employee", "ticket", "paid_at", "tender", "card_type", "status", "amount", "tip", "total", "card", "reference"],
        "cash": ["employee", "cash", "cash_voids", "tips_payouts", "pay_in_out", "server_balance", "expected_cash"],
        "tips": ["employee", "server_cash", "server_payroll", "merchant", "tipout_cash", "tipout_payroll", "total", "reported"],
        "batch": ["device", "batch_id", "status", "opened", "closed", "txns", "amount"],
        "taxes": ["tax_name", "collected", "inclusive", "total"],
        "summary": ["gross_sales", "discounts", "adjustments", "net_sales", "taxes", "tips", "gross_receipts", "card_amount", "card_tips",
                    "cash_amount", "cash_transactions", "cash_voids", "pay_in", "pay_out", "expected_cash", "tickets", "guests", "alcohol_net",
                    "liquor_net", "data"],
        "payroll": ["employee", "reg_hours", "reg_pay", "ot_hours", "ot_pay", "total_hours", "total_pay"]}


def store(db, client, source, sections, import_id):
    """Replace each (report, date) for this POS; returns a summary per report."""
    ensure(db)
    summary = collections.defaultdict(lambda: dict(days=set(), rows=0))
    for s in sections:
        recs = parse(s)
        if s["kind"] == "clock":
            # time clock punches are shifts: replace this person's punches from this POS for the day
            db.execute("DELETE FROM pos_shift WHERE client=? AND source=? AND employee=? AND business_date=? AND clock_in IS NOT NULL",
                       (client, source, s["employee"], s["date"]))
            for r in recs:
                db.execute("INSERT INTO pos_shift(client,import_id,source,employee,job,business_date,clock_in,clock_out,hours,declared_tips,"
                           "gross,net,tips,tax,check_no) VALUES(?,?,?,?,?,?,?,?,?,?,0,0,0,0,'')",
                           (client, import_id, source, r["employee"], r["job"], r["business_date"], r["clock_in"], r["clock_out"], r["hours"],
                            r["declared_tips"]))
            summary["clock"]["days"].add(s["date"])
            summary["clock"]["rows"] += len(recs)
            continue
        t = TABLE[s["kind"]]
        db.execute("DELETE FROM %s WHERE client=? AND source=? AND business_date=?" % t, (client, source, s["date"]))
        cols = COLS[s["kind"]]
        for r in recs:
            db.execute("INSERT INTO %s(client,import_id,source,business_date,%s) VALUES(?,?,?,?,%s)" % (t, ",".join(cols), ",".join("?" * len(cols))),
                       (client, import_id, source, s["date"], *[r[c] for c in cols]))
        summary[s["kind"]]["days"].add(s["date"])
        summary[s["kind"]]["rows"] += len(recs)
    return {k: dict(days=sorted(v["days"]), rows=v["rows"]) for k, v in summary.items()}


def active_payments(db, client, where="", args=()):
    """Payments that count: voids left out, refunds negative."""
    return db.execute("""SELECT business_date d, employee, tender, card_type, status, amount, tip FROM pos_payment
                         WHERE client=? AND lower(status) NOT LIKE '%%void%%' %s""" % where, (client, *args)).fetchall()


def day_totals(db, client):
    """Card sales / card tips / cash per business day → sales_day source 'pos' for days with no Linga batch or processor data."""
    ensure(db)
    days = collections.defaultdict(lambda: [0.0, 0.0, 0.0])
    have_pay = {r[0] for r in db.execute("SELECT DISTINCT business_date FROM pos_payment WHERE client=?", (client,))}
    for r in active_payments(db, client):
        sign = -1 if re.search(r"refund|return", r["status"] or "", re.I) else 1
        x = days[r["d"]]
        if r["tender"] == "card":
            x[0] += sign * (r["amount"] or 0)
            x[1] += sign * (r["tip"] or 0)
        elif r["tender"] == "cash":
            x[2] += sign * (r["amount"] or 0)
    # a day with only the Batch Credit Card report: batch = card sales + card tips; tips from Tips & Payouts when there
    have_batch = {}
    for r in db.execute("SELECT business_date d, sum(amount) a FROM pos_batch WHERE client=? AND lower(coalesce(status,'')) != 'open' "
                        "GROUP BY business_date", (client,)):
        have_batch[r["d"]] = r["a"] or 0
    tip_rep = {r[0]: r[1] or 0 for r in db.execute("SELECT business_date, sum(server_cash + server_payroll + merchant) FROM pos_tips "
                                                    "WHERE client=? GROUP BY business_date", (client,))}
    cash_day = cash_by_day(db, client)
    daily = {r["business_date"]: r for r in db.execute("SELECT * FROM pos_daily WHERE client=?", (client,))}
    for d, amt in have_batch.items():
        if d not in have_pay:
            tips = tip_rep[d] if d in tip_rep else (daily[d]["card_tips"] or 0) if d in daily else 0.0
            days[d] = [amt - tips, tips, cash_day.get(d, 0.0)]
    for d, r in daily.items():  # Daily Summary alone: card amount, card tips, expected cash
        if d not in have_pay and d not in have_batch:
            days[d] = [r["card_amount"] or 0, r["card_tips"] or 0, cash_day.get(d, 0.0)]
    db.execute("DELETE FROM sales_day WHERE client=? AND source='pos' AND date NOT IN (SELECT business_date FROM pos_payment WHERE client=? "
               "UNION SELECT business_date FROM pos_batch WHERE client=? UNION SELECT business_date FROM pos_daily WHERE client=?)",
               (client, client, client, client))
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for d, (card, tips, cash) in days.items():
        have = db.execute("SELECT source FROM sales_day WHERE client=? AND date=?", (client, d)).fetchone()
        if have and have["source"] in ("batch", "processor"):
            continue
        db.execute("""INSERT INTO sales_day(client,date,card_sales,card_tips,cash_sales,cash_tips,shifts,source,fetched_at)
                      VALUES(?,?,?,?,?,0,0,'pos',?) ON CONFLICT(client,date) DO UPDATE SET card_sales=excluded.card_sales,
                      card_tips=excluded.card_tips, cash_sales=excluded.cash_sales, source='pos', fetched_at=excluded.fetched_at""",
                   (client, d, round(card, 2), round(tips, 2), round(cash, 2), ts))
    tax_days(db, client)
    return len(days)


def tax_by_day(db, client):
    """{date: {tax name: total}} from the Taxes report."""
    ensure(db)
    out = collections.defaultdict(dict)
    for r in db.execute("SELECT business_date, tax_name, sum(total) t FROM pos_tax WHERE client=? GROUP BY business_date, tax_name", (client,)):
        out[r[0]][r[1]] = round(r[2] or 0, 2)
    return dict(out)


def liquor_by_day(db, client):
    """Liquor (mixed beverage) tax per day: the Taxes report when imported, else 13.5% of alcohol sales in the Daily Summary."""
    out = {d: round(sum(v for k, v in x.items() if re.search(r"liquor|mixed bev", k, re.I)), 2) for d, x in tax_by_day(db, client).items()}
    for r in db.execute("SELECT business_date, alcohol_net, taxes FROM pos_daily WHERE client=?", (client,)):
        if r[0] not in out:
            out[r[0]] = round(min(r[2] or 0, LIQUOR_RATE * (r[1] or 0)), 2)
    return out


def liquor_check(db, client):
    """Days with both reports: liquor tax from the Taxes report vs 13.5% of Daily Summary alcohol sales (liquor, beer, wine)."""
    ensure(db)
    tx = tax_by_day(db, client)
    out = []
    for r in db.execute("SELECT business_date, alcohol_net, liquor_net FROM pos_daily WHERE client=? ORDER BY business_date DESC", (client,)):
        if r[0] in tx:
            actual = round(sum(v for k, v in tx[r[0]].items() if re.search(r"liquor|mixed bev", k, re.I)), 2)
            out.append(dict(date=r[0], actual=actual, alcohol=round(LIQUOR_RATE * (r[1] or 0), 2), liquor_only=round(LIQUOR_RATE * (r[2] or 0), 2)))
    return out


def tax_days(db, client):
    """Gross receipts / product sales / tax per day for the sales-tax table, from the Taxes report plus what customers paid
    (card sales + cash, tips excluded). Linga's Sale Summary and API days are left alone; replaces estimates from pasted shifts."""
    taxes = tax_by_day(db, client)
    daily = {r["business_date"]: r for r in db.execute("SELECT * FROM pos_daily WHERE client=?", (client,))}
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    db.execute("DELETE FROM day_tax WHERE client=? AND source='pos-report' AND date NOT IN (SELECT business_date FROM pos_tax WHERE client=? "
               "UNION SELECT business_date FROM pos_daily WHERE client=?)", (client, client, client))
    for d, r in daily.items():  # the Daily Summary has the exact net sales and tax
        have = db.execute("SELECT source FROM day_tax WHERE client=? AND date=?", (client, d)).fetchone()
        if have and have["source"] in ("report", "linga"):
            continue
        tax = round(sum(taxes[d].values()), 2) if d in taxes else round(r["taxes"] or 0, 2)
        net = round(r["net_sales"] or 0, 2)
        if not net and not tax:
            continue
        db.execute("INSERT OR REPLACE INTO day_tax VALUES(?,?,?,?,?,?,?,?)", (client, d, round(net + tax, 2), net, tax, r["tickets"] or 0, "pos-report", ts))
    for d, x in taxes.items():
        if d in daily:
            continue
        sd = db.execute("SELECT card_sales, cash_sales, source FROM sales_day WHERE client=? AND date=?", (client, d)).fetchone()
        if not sd or sd["source"] != "pos":
            continue
        have = db.execute("SELECT source FROM day_tax WHERE client=? AND date=?", (client, d)).fetchone()
        if have and have["source"] in ("report", "linga"):
            continue
        tax = round(sum(x.values()), 2)
        gross = round((sd["card_sales"] or 0) + (sd["cash_sales"] or 0), 2)
        if not gross:
            continue
        checks = db.execute("SELECT count(DISTINCT ticket) FROM pos_payment WHERE client=? AND business_date=? AND lower(status) NOT LIKE '%void%'",
                            (client, d)).fetchone()[0]
        db.execute("INSERT OR REPLACE INTO day_tax VALUES(?,?,?,?,?,?,?,?)", (client, d, gross, round(gross - tax, 2), tax, checks, "pos-report", ts))


def batch_check(db, client):
    """Per day: batch total vs card sales + tips from the Transactions report (they should be equal)."""
    ensure(db)
    out = []
    for r in db.execute("""SELECT b.business_date d, sum(b.amount) batch,
                                  (SELECT sum(CASE WHEN tender='card' THEN amount + tip ELSE 0 END) FROM pos_payment p WHERE p.client=b.client
                                    AND p.business_date=b.business_date AND lower(status) NOT LIKE '%void%') pay,
                                  (SELECT sum(card_amount + card_tips) FROM pos_daily x WHERE x.client=b.client AND x.business_date=b.business_date) dly
                           FROM pos_batch b WHERE client=? GROUP BY b.business_date ORDER BY b.business_date DESC""", (client,)):
        pay = r["pay"] if r["pay"] is not None else r["dly"]
        out.append(dict(date=r["d"], batch=round(r["batch"] or 0, 2), payments=None if pay is None else round(pay, 2),
                        diff=None if pay is None else round((r["batch"] or 0) - pay, 2)))
    return out


def cash_by_day(db, client):
    """Cash taken in per business day: Expected Cash from the Cash Summary, else cash payments in the Transactions report."""
    ensure(db)
    out = {r[0]: r[1] for r in db.execute("SELECT business_date, sum(amount) FROM pos_payment p WHERE client=? AND tender='cash' "
                                          "AND lower(status) NOT LIKE '%void%' GROUP BY business_date", (client,))}
    for r in db.execute("SELECT business_date, sum(expected_cash) FROM pos_daily WHERE client=? GROUP BY business_date", (client,)):
        out[r[0]] = r[1]
    for r in db.execute("SELECT business_date, sum(expected_cash) FROM pos_cash WHERE client=? GROUP BY business_date", (client,)):
        out[r[0]] = r[1]
    return {d: round(v or 0, 2) for d, v in out.items()}


NAMES = {"payments": "Transactions", "cash": "Cash Summary", "tips": "Tips & Payouts", "batch": "Batch Credit Card",
         "payroll": "Payroll Summary", "clock": "Time Clock", "taxes": "Taxes", "summary": "Daily Summary"}


def status(db, client):
    ensure(db)
    out = []
    for k, t in TABLE.items():
        r = db.execute("SELECT count(*), count(DISTINCT business_date), min(business_date), max(business_date) FROM %s WHERE client=?" % t,
                       (client,)).fetchone()
        if r[0]:
            out.append(dict(kind=NAMES[k], rows=r[0], days=r[1],
                            a=r[2], b=r[3]))
    r = db.execute("SELECT count(*), count(DISTINCT business_date), min(business_date), max(business_date) FROM pos_shift WHERE client=? "
                   "AND clock_in IS NOT NULL AND gross=0 AND net=0 AND tips=0", (client,)).fetchone()
    if r[0]:
        out.append(dict(kind="Time Clock / punches", rows=r[0], days=r[1], a=r[2], b=r[3]))
    return out


def sync_rows(db, client, s):
    """Copy the report tables from Shifts' database (connection s) into Finance with negative import ids; a report day
    pasted in Shifts replaces the same report, POS and day here."""
    ensure(db)
    n = 0
    for k, t in TABLE.items():
        db.execute("DELETE FROM %s WHERE client=? AND import_id<0" % t, (client,))
        if not s.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone():
            continue
        rows = [dict(r) for r in s.execute("SELECT * FROM %s" % t)]
        for src, d in {(r["source"], r["business_date"]) for r in rows}:
            db.execute("DELETE FROM %s WHERE client=? AND source=? AND business_date=?" % t, (client, src, d))
        cols = [c for c in COLS[k] if c in (rows[0] if rows else {})]
        for r in rows:
            db.execute("INSERT INTO %s(client,import_id,source,business_date,%s) VALUES(?,?,?,?,%s)" % (t, ",".join(cols), ",".join("?" * len(cols))),
                       (client, -int(r.get("import_id") or 0) or -1, r["source"], r["business_date"], *[r.get(c) for c in cols]))
        n += len(rows)
    return n
