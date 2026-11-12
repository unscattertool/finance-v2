"""Import shift and sales data from any POS (Heartland/Genius, …) — separate from the Linga importer.

Accepts a CSV/TSV/Excel file or text pasted from a POS report or its AI assistant, including the "one value per
line" layout you get when copying a table out of a web page (empty cells are simply missing there). Columns are
recognised by name (First Name / Last Name / Employee, Job, Punch In / Clock In, Punch Out, Gross Sales, Net Sales,
Tips, Taxes, Clock-out tips, Check #, Date). One row = one shift, or one sales total linked to a shift; a shift
that appears on several rows (one per linked sales total) counts its hours once.

Stored in pos_shift, tagged with the POS name and an import id; re-importing the same POS and dates replaces them.
The views attendance_all / sales_by_server_all combine this with the Linga data so the hours check, missing
closeouts, name matching, tip check and sales-tax pages use both.
"""
import collections
import csv
import datetime as dt
import io
import re

SCHEMA = """
CREATE TABLE IF NOT EXISTS pos_import (
  id INTEGER PRIMARY KEY AUTOINCREMENT, client TEXT NOT NULL, source TEXT, label TEXT, imported_at TEXT, imported_by TEXT,
  rows INTEGER, shifts INTEGER, first TEXT, last TEXT, columns TEXT);
CREATE TABLE IF NOT EXISTS pos_shift (
  client TEXT NOT NULL, import_id INTEGER, source TEXT, employee TEXT, job TEXT, business_date TEXT, clock_in TEXT,
  clock_out TEXT, hours REAL, declared_tips REAL, gross REAL, net REAL, tips REAL, tax REAL, check_no TEXT);
CREATE INDEX IF NOT EXISTS pos_shift_d ON pos_shift(client, business_date);
"""
VIEWS = """
DROP VIEW IF EXISTS attendance_all;
CREATE VIEW attendance_all AS
  SELECT client, date, name, clock_in, clock_out, hours, job, flag, 'Linga' AS source FROM linga_attendance
  UNION ALL
  SELECT client, business_date, employee, clock_in, clock_out, max(hours), job,
         CASE WHEN max(hours) IS NULL THEN 'still clocked in' WHEN max(hours) > 14 THEN 'over 14 h: probably didn''t clock out'
              ELSE '' END, source
  FROM pos_shift WHERE clock_in IS NOT NULL GROUP BY client, source, employee, job, clock_in;
DROP VIEW IF EXISTS pos_server_day;
CREATE VIEW pos_server_day AS
  SELECT client, business_date, employee, sum(amt) gross, NULL net, NULL tax, count(DISTINCT ticket) checks,
         sum(CASE WHEN tender='card' THEN amt ELSE 0 END) card, sum(CASE WHEN tender='card' THEN tp ELSE 0 END) tips, source
  FROM (SELECT *, CASE WHEN lower(status) LIKE '%refund%' OR lower(status) LIKE '%return%' THEN -abs(amount) ELSE amount END amt,
               CASE WHEN lower(status) LIKE '%refund%' OR lower(status) LIKE '%return%' THEN -abs(tip) ELSE tip END tp
        FROM pos_payment WHERE lower(status) NOT LIKE '%void%')
  GROUP BY client, business_date, employee, source
  UNION ALL
  SELECT p.client, p.business_date, p.employee, sum(CASE WHEN p.net != 0 THEN p.net ELSE p.gross END + p.tax), sum(p.net), sum(p.tax),
         count(*), sum(CASE WHEN p.net != 0 THEN p.net ELSE p.gross END + p.tax), sum(p.tips), p.source FROM pos_shift p
  WHERE NOT EXISTS (SELECT 1 FROM pos_payment x WHERE x.client = p.client AND x.business_date = p.business_date)
  GROUP BY p.client, p.business_date, p.employee, p.source;
DROP VIEW IF EXISTS sales_by_server_all;
CREATE VIEW sales_by_server_all AS
  SELECT client, date, server, gross, net, tax, checks, 'Linga' AS source FROM sale_server
  UNION ALL
  SELECT p.client, p.business_date, p.employee, p.gross, p.net, p.tax, p.checks, p.source FROM pos_server_day p
  WHERE p.gross != 0 AND NOT EXISTS (SELECT 1 FROM sale_server s WHERE s.client = p.client AND s.date = p.business_date);
"""

# column name → field (lower-case, punctuation removed)
SYN = {
    "first": ["first name", "first", "firstname", "employee first name"],
    "last": ["last name", "last", "lastname", "employee last name"],
    "employee": ["employee", "employee name", "name", "server", "server name", "staff", "staff member", "team member"],
    "job": ["job type name", "job", "job type", "job code", "role", "position", "job title"],
    "clock_in": ["punch in date ltz", "punch in date", "punch in", "clock in", "check in", "time in", "in", "shift start", "start"],
    "clock_out": ["punch out date ltz", "punch out date", "punch out", "clock out", "check out", "time out", "out", "shift end", "end"],
    "date": ["business date", "date", "shift date", "day"],
    "hours": ["hours", "total hours", "hours worked", "paid hours"],
    "declared_tips": ["clock in out tip", "declared tips", "cash tips declared", "clock out tips", "cash tips"],
    "gross": ["gross sales", "gross", "gross sale", "grand sales"],
    "net": ["net sales", "net", "net sale"],
    "tips": ["sales tips", "tips", "tip", "card tips", "credit tips", "charged tips"],
    "tax": ["taxes", "tax", "sales tax", "total tax"],
    "check_no": ["check number", "check", "check #", "ticket", "ticket number", "order", "order number"],
}
TYPES = {"first": "text", "last": "text", "employee": "text", "job": "text", "clock_in": "datetime", "clock_out": "datetime",
         "date": "datetime", "hours": "number", "declared_tips": "number", "gross": "number", "net": "number",
         "tips": "number", "tax": "number", "check_no": "any"}


def ensure(db):
    from . import lingaarchive, salesum
    lingaarchive.ensure(db)
    salesum.ensure(db)
    from . import posreports
    posreports.ensure(db)
    db.executescript(SCHEMA)
    db.executescript(VIEWS)


def _key(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9#]+", " ", str(s or "").lower())).strip()


def field_of(name):
    k = _key(name)
    for f, names in SYN.items():
        if k in names:
            return f
    # looser: "Staff First Name", "Employee Last Name", "Clock Out Tip", "Punch In Date (Local)", "Total Taxes"…
    words = set(k.split())
    t = re.sub(r"\s+", " ", re.sub(r"\b(staff|employee|server|team|member|ltz|local|utc|date|time|total|amount|amt)\b", " ", k)).strip()
    if "first" in words and ("name" in words or t == "first"):
        return "first"
    if "last" in words and ("name" in words or t == "last"):
        return "last"
    if re.search(r"\btips?\b", k) and re.search(r"\b(clock|declared|cash)\b", k):
        return "declared_tips"
    if re.search(r"\b(punch|clock|check|time|shift)\b.*\b(in|start)\b", k):
        return "clock_in"
    if re.search(r"\b(punch|clock|check|time|shift)\b.*\b(out|end)\b", k):
        return "clock_out"
    for f, names in SYN.items():
        if t and t in names:
            return f
    for pat, f in ((r"\btax(es)?\b", "tax"), (r"\btips?\b|\bgratuit", "tips"), (r"\bgross\b", "gross"), (r"\bnet\b", "net"),
                   (r"\b(job|role|position)\b", "job")):
        if re.search(pat, k):
            return f
    return None


_DT = [("%Y-%m-%dT%H:%M:%S", True), ("%Y-%m-%dT%H:%M", True), ("%Y-%m-%d %H:%M:%S", True), ("%Y-%m-%d %H:%M", True),
       ("%m/%d/%Y %I:%M %p", True), ("%m/%d/%Y %I:%M:%S %p", True), ("%m-%d-%Y %I:%M %p", True), ("%m/%d/%Y %H:%M", True),
       ("%Y-%m-%d", False), ("%m/%d/%Y", False), ("%m-%d-%Y", False), ("%m/%d/%y", False)]


def parse_dt(v):
    if isinstance(v, dt.datetime):
        return v
    if isinstance(v, dt.date):
        return dt.datetime(v.year, v.month, v.day)
    s = str(v or "").strip().replace("Z", "")
    s = re.sub(r"\.\d+$", "", s)
    for fmt, _ in _DT:
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def parse_num(v):
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or "").strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.replace("$", "").replace(",", "").strip("()").strip()
    if not re.match(r"^-?\d+(\.\d+)?$", s):
        return None
    return -float(s) if neg else float(s)


def _kind(tok):
    if parse_dt(tok) is not None and re.search(r"\d{4}|\d+/\d+", str(tok)):
        return "datetime"
    if parse_num(tok) is not None:
        return "number"
    return "text"


# ------------------------------------------------------------------ reading
def _table_from_rows(rows):
    """Find the header row (≥3 recognised columns) in a list of rows and return (fields, data rows)."""
    for i, r in enumerate(rows[:30]):
        f = [field_of(c) for c in r]
        if sum(1 for x in f if x) >= 3:
            return f, [x for x in rows[i + 1:] if any(str(c or "").strip() for c in x)]
    return None, []


def _vertical(lines):
    """One value per line, header first; empty cells are missing. Rows are rebuilt by the expected column types."""
    start = None
    for i in range(len(lines)):
        run = 0
        while i + run < len(lines) and field_of(lines[i + run]):
            run += 1
        if run >= 3:
            start, n = i, run
            break
    if start is None:
        return None, []
    fields = [field_of(x) for x in lines[start:start + n]]
    toks, rows, cur, ci = lines[start + n:], [], None, 0

    def fits(f, tok):
        k, t = _kind(tok), TYPES.get(f, "any")
        return t == "any" or k == t or (t == "number" and k == "number")

    i = 0
    while i < len(toks):
        tok = toks[i]
        if cur is None:
            if _kind(tok) != "text" and TYPES.get(fields[0]) == "text":
                i += 1  # stray value before a row starts
                continue
            cur, ci = [None] * len(fields), 0
        if ci >= len(fields):
            rows.append(cur)
            cur = None
            continue
        f = fields[ci]
        if fits(f, tok):
            # a text token where a later row could start: only take it if this row still needs text
            cur[ci] = tok
            ci += 1
            i += 1
        else:
            # this column is empty; if every remaining column is numeric/datetime and we see text, the row ended
            rest = [TYPES.get(x) for x in fields[ci:]]
            if _kind(tok) == "text" and all(t in ("number", "datetime") for t in rest):
                rows.append(cur)
                cur = None
            else:
                ci += 1
    if cur is not None and any(x is not None for x in cur):
        rows.append(cur)
    return fields, rows


def read_any(name, data=None, text=None):
    """(fields, rows) from a file (csv/tsv/xlsx) or pasted text."""
    if data is not None and (name or "").lower().endswith(".xlsx"):
        from . import xlsxlite
        return _table_from_rows(xlsxlite.rows(data))
    if text is None:
        text = data.decode("utf-8-sig", errors="replace") if data is not None else ""
    lines = [l.rstrip("\r") for l in text.split("\n")]
    nonempty = [l.strip() for l in lines if l.strip()]
    tabbed = sum(1 for l in nonempty if "\t" in l)
    commas = sum(1 for l in nonempty if l.count(",") >= 2)
    if tabbed >= 2 or commas >= 2:
        delim = "\t" if tabbed >= commas else ","
        rows = list(csv.reader(io.StringIO("\n".join(l for l in lines if l.strip())), delimiter=delim))
        f, r = _table_from_rows(rows)
        if f:
            return f, r
    return _vertical(nonempty)


# ------------------------------------------------------------------ importing
def records(fields, rows):
    out = []
    for r in rows:
        g = {}
        for f, v in zip(fields, r):
            if f and v not in (None, ""):
                g.setdefault(f, v)
        name = str(g.get("employee") or " ".join(x for x in (g.get("first"), g.get("last")) if x) or "").strip()
        name = re.sub(r"\s+", " ", name)
        cin, cout = parse_dt(g.get("clock_in")), parse_dt(g.get("clock_out"))
        if cout and cout.year < 2000:
            cout = None  # "1969-12-31…" = no punch-out yet (still clocked in)
        day = parse_dt(g.get("date"))
        bdate = (day or cin)
        if not name or not bdate:
            continue
        hours = parse_num(g.get("hours"))
        if hours is None and cin and cout:
            hours = round((cout - cin).total_seconds() / 3600, 2)
        num = lambda k: parse_num(g.get(k)) or 0.0
        out.append(dict(employee=name, job=str(g.get("job") or "").strip(), business_date=bdate.date().isoformat(),
                        clock_in=cin.isoformat(timespec="minutes") if cin else None,
                        clock_out=cout.isoformat(timespec="minutes") if cout else None, hours=hours,
                        declared_tips=num("declared_tips"), gross=num("gross"), net=num("net"), tips=num("tips"), tax=num("tax"),
                        check_no=str(g.get("check_no") or "").strip()))
    return out


def dedupe_sales(recs):
    """The same sales total linked to several punches of one employee that day (duplicate or still-open punch) is kept
    once, on the punch that best covers it (clocked out, longest); the other punches stay as shifts with no sales."""
    groups = collections.defaultdict(list)
    for r in recs:
        if r["gross"] or r["net"] or r["tips"] or r["tax"]:
            groups[(r["employee"], r["business_date"], r["gross"], r["net"], r["tips"], r["tax"])].append(r)
    for rows in groups.values():
        if len(rows) < 2 or len({r["clock_in"] for r in rows}) < 2:
            continue
        best = sorted(rows, key=lambda r: (bool(r["clock_out"]), r["hours"] or 0), reverse=True)[0]
        for r in rows:
            if r is not best:
                r.update(gross=0.0, net=0.0, tips=0.0, tax=0.0)
    return recs


def import_data(db, client, source, name=None, data=None, text=None, user=""):
    ensure(db)
    source = re.sub(r"\s+", " ", (source or "").strip())[:40] or "POS"
    from . import posreports as PR
    if text is None and data is not None and not (name or "").lower().endswith(".xlsx"):
        text = data.decode("utf-8-sig", errors="replace")
    reports = None
    if text is not None:
        skipped = []
        sections, rest = PR.split_sections(text, skipped)
        if skipped and not sections and len([l for l in rest.split("\n") if l.strip()]) < 2:
            raise ValueError("%s: these are totals for a date range, so they can't be filed by day. Export the report one day at a time."
                             % ", ".join(skipped))
        if sections:
            reports = _import_reports(db, client, source, name, sections, user, skipped)
            if len([l for l in rest.split("\n") if l.strip()]) < 2:
                return reports
            text = rest
    try:
        fields, rows = read_any(name, data if text is None else None, text)
    except Exception:
        if reports:
            return reports
        raise
    if not fields and reports:
        return reports
    if not fields:
        raise ValueError("Couldn't find the column names (e.g. First Name, Punch In, Gross Sales). Paste the table with its "
                         "header, or upload the CSV/Excel file.")
    recs = dedupe_sales(records(fields, rows))
    if not recs:
        raise ValueError("Found the columns (%s) but no rows with an employee and a date." %
                         ", ".join(sorted({f for f in fields if f})))
    first, last = min(r["business_date"] for r in recs), max(r["business_date"] for r in recs)
    # same POS, same dates → replace
    db.execute("DELETE FROM pos_shift WHERE client=? AND source=? AND business_date BETWEEN ? AND ?", (client, source, first, last))
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    shifts = len({(r["employee"], r["job"], r["clock_in"]) for r in recs if r["clock_in"]})
    cur = db.execute("INSERT INTO pos_import(client,source,label,imported_at,imported_by,rows,shifts,first,last,columns) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)", (client, source, name or "pasted", ts, user, len(recs), shifts, first, last,
                                                      ",".join(f for f in fields if f)))
    iid = cur.lastrowid
    for r in recs:
        db.execute("INSERT INTO pos_shift VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (client, iid, source, r["employee"], r["job"], r["business_date"], r["clock_in"], r["clock_out"], r["hours"],
                    r["declared_tips"], r["gross"], r["net"], r["tips"], r["tax"], r["check_no"]))
    _day_totals(db, client, source, first, last)
    db.commit()
    tot = lambda k: round(sum(r[k] for r in recs), 2)
    msg = "%s: %d rows, %d shifts, %d employees, %s to %s — %s sales, %s tips, %s tax%s." % (
        source, len(recs), shifts, len({r["employee"] for r in recs}), _mdy(first), _mdy(last), _money(tot("gross")), _money(tot("tips")),
        _money(tot("tax")), (" · %d still clocked in" % sum(1 for r in recs if r["clock_in"] and not r["clock_out"]))
        if any(r["clock_in"] and not r["clock_out"] for r in recs) else "")
    if reports:
        msg = reports["message"] + " " + msg
    return dict(message=msg, id=iid, source=source, rows=len(recs), shifts=shifts, first=first, last=last, gross=tot("gross"),
                tips=tot("tips"), tax=tot("tax"), open=sum(1 for r in recs if r["clock_in"] and not r["clock_out"]),
                employees=len({r["employee"] for r in recs}), columns=sorted({f for f in fields if f}))


def _mdy(d):
    return "%s/%s/%s" % (int(d[5:7]), int(d[8:10]), d[:4]) if d else ""


def _money(v):
    return ("-$%s" if v < 0 else "$%s") % format(abs(v or 0), ",.2f")




def _import_reports(db, client, source, name, sections, user, skipped=()):
    """Daily POS reports (Transactions / Cash Summary / Tips & Payouts) → pos_payment / pos_cash / pos_tips."""
    from . import posreports as PR
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    dates = [s["date"] for s in sections]
    rows = sum(len(s["rows"]) for s in sections)
    kinds = sorted({s["kind"] for s in sections})
    cur = db.execute("INSERT INTO pos_import(client,source,label,imported_at,imported_by,rows,shifts,first,last,columns) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)", (client, source, name or "pasted", ts, user, rows, 0, min(dates), max(dates),
                                                      "reports:" + ",".join(kinds)))
    iid = cur.lastrowid
    summ = PR.store(db, client, source, sections, iid)
    PR.day_totals(db, client)
    db.commit()
    card = db.execute("SELECT round(sum(card),2), round(sum(tips),2) FROM pos_server_day WHERE client=? AND business_date BETWEEN ? AND ?",
                      (client, min(dates), max(dates))).fetchone()
    parts = ["%s %s" % (PR.NAMES[k], ", ".join(_mdy(d) for d in v["days"])) for k, v in sorted(summ.items())]
    msg = "%s reports: %s." % (source, "; ".join(parts))
    if skipped:
        msg += " Not imported (totals for a date range, not one day): %s." % ", ".join(skipped)
    if "payments" in summ:
        msg += " Card sales %s, card tips %s." % (_money(card[0] or 0), _money(card[1] or 0))
    if "clock" in summ:
        msg += " %d punches." % summ["clock"]["rows"]
    if "summary" in summ:
        lc = [x for x in PR.liquor_check(db, client) if x["date"] in set(dates)]
        if lc:
            ok_all = all(abs(x["actual"] - x["alcohol"]) < 0.02 for x in lc)
            msg += (" Liquor tax: 13.5%% of alcohol sales matches the Taxes report on %d day(s)." % len(lc) if ok_all else
                    " Liquor tax estimate (13.5%% of alcohol sales) differs from the Taxes report: " + ", ".join(
                        "%s %s vs %s" % (_mdy(x["date"]), _money(x["actual"]), _money(x["alcohol"])) for x in lc) + ".")
    off = [b for b in PR.batch_check(db, client) if b["date"] in set(dates) and b["diff"] is not None and abs(b["diff"]) >= 0.01]
    if off:
        msg += " Batch ≠ card sales + tips on " + ", ".join("%s (%s)" % (_mdy(b["date"]), _money(b["diff"])) for b in off) + "."
    return dict(message=msg, id=iid, source=source, rows=rows, shifts=0, first=min(dates), last=max(dates), reports=summ)


def _day_totals(db, client, source, a, b):
    """Daily gross/net/tax for the sales-tax page, for days Linga's report doesn't cover."""
    # POS "gross" is usually before tax; Finance's gross sales include tax (what customers paid), so gross = net + tax
    for r in db.execute("""SELECT business_date d, sum(CASE WHEN net != 0 THEN net ELSE gross END + tax) g,
                                  sum(CASE WHEN net != 0 THEN net ELSE gross END) n, sum(tax) t, sum(CASE WHEN gross!=0 THEN 1 ELSE 0 END) k
                           FROM pos_shift WHERE client=? AND source=? AND business_date BETWEEN ? AND ? GROUP BY business_date""",
                        (client, source, a, b)).fetchall():
        have = db.execute("SELECT source FROM day_tax WHERE client=? AND date=?", (client, r["d"])).fetchone()
        if have and have["source"] in ("report", "linga", "pos-report"):
            continue
        if (r["g"] or 0) == 0 and (r["t"] or 0) == 0:
            continue
        db.execute("INSERT OR REPLACE INTO day_tax VALUES(?,?,?,?,?,?,?,?)",
                   (client, r["d"], round(r["g"] or 0, 2), round(r["n"] or 0, 2), round(r["t"] or 0, 2), r["k"],
                    "pos:" + source, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))


def delete_import(db, client, iid):
    ensure(db)
    row = db.execute("SELECT * FROM pos_import WHERE client=? AND id=?", (client, iid)).fetchone()
    if not row:
        return False
    db.execute("DELETE FROM pos_shift WHERE client=? AND import_id=?", (client, iid))
    from . import posreports as PR
    for t in PR.TABLE.values():
        db.execute("DELETE FROM %s WHERE client=? AND import_id=?" % t, (client, iid))
    PR.day_totals(db, client)
    db.execute("DELETE FROM pos_import WHERE id=?", (iid,))
    db.execute("DELETE FROM day_tax WHERE client=? AND source=? AND date BETWEEN ? AND ?", (client, "pos:" + row["source"], row["first"], row["last"]))
    _day_totals(db, client, row["source"], row["first"], row["last"])
    db.commit()
    return True


def status(db, client):
    ensure(db)
    imps = [dict(r) for r in db.execute("SELECT * FROM pos_import WHERE client=? ORDER BY id DESC", (client,))]
    live = {r[0] for r in db.execute("SELECT DISTINCT import_id FROM pos_shift WHERE client=? UNION SELECT DISTINCT import_id FROM pos_payment "
                                     "WHERE client=? UNION SELECT DISTINCT import_id FROM pos_cash WHERE client=? UNION SELECT DISTINCT "
                                     "import_id FROM pos_tips WHERE client=?", (client,) * 4)}
    for x in imps:
        x["replaced"] = x["id"] not in live
    by = [dict(r) for r in db.execute("""SELECT source, count(*) n, min(business_date) a, max(business_date) b, round(sum(gross),2) g,
                                          round(sum(tips),2) t, round(sum(tax),2) x FROM pos_shift WHERE client=? GROUP BY source""", (client,))]
    from . import posreports as PR
    reps = PR.status(db, client)
    if not by and reps:
        src = db.execute("SELECT source, min(business_date), max(business_date) FROM pos_payment WHERE client=? GROUP BY source", (client,)).fetchall()
        by = [dict(source=r[0], a=r[1], b=r[2]) for r in src]
    return dict(imports=imps[:20], sources=by, reports=reps, batches=PR.batch_check(db, client)[:14])


def sync_from_shifts(db, client, shifts_path):
    """Shifts and sales pasted on Shifts' POS import tab (shifts.db pos_shift) → Finance pos_shift (import_id < 0)."""
    import sqlite3
    if not shifts_path:
        return 0
    ensure(db)
    try:
        s = sqlite3.connect("file:%s?mode=ro" % shifts_path, uri=True, timeout=5)
        s.row_factory = sqlite3.Row
        has = s.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pos_shift'").fetchone()
        rows = [dict(r) for r in s.execute("SELECT * FROM pos_shift")] if has else []
        from . import posreports as PR
        nrep = PR.sync_rows(db, client, s)
        s.close()
    except sqlite3.Error:
        return 0
    old = db.execute("SELECT source, min(business_date), max(business_date) FROM pos_shift WHERE client=? AND import_id<0 GROUP BY source",
                     (client,)).fetchall()
    db.execute("DELETE FROM pos_shift WHERE client=? AND import_id<0", (client,))
    # the same POS day pasted in Shifts wins over a copy imported here, so nothing is counted twice
    for src, d in {(r["source"], r["business_date"]) for r in rows}:
        db.execute("DELETE FROM pos_shift WHERE client=? AND import_id>0 AND source=? AND business_date=?", (client, src, d))
    for r in rows:
        db.execute("INSERT INTO pos_shift VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (client, -int(r["import_id"] or 0) or -1, r["source"], r["employee"], r["job"], r["business_date"], r["clock_in"],
                    r["clock_out"], r["hours"], r["declared_tips"] or 0, r["gross"] or 0, r["net"] or 0, r["tips"] or 0, r["tax"] or 0,
                    r["check_no"] or ""))
    spans = {(src, a, b) for src, a, b in old}
    for src in {r["source"] for r in rows}:
        ds = [r["business_date"] for r in rows if r["source"] == src]
        spans.add((src, min(ds), max(ds)))
    for src, a, b in spans:
        db.execute("DELETE FROM day_tax WHERE client=? AND source=? AND date BETWEEN ? AND ?", (client, "pos:" + src, a, b))
        _day_totals(db, client, src, a, b)
    PR.day_totals(db, client)
    db.commit()
    return len(rows) + nrep
