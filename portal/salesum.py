"""Sales, sales tax and tips by pay period / month — product sales vs tax collected vs tax paid out.

Gross sales and sales tax per business day come from the Shifts closeouts (Sales tax field, typed from the
Linga server checkout). Days Shifts doesn't have can be filled from Linga by hand ("Pull from Linga", same
3-a-day rule): saleSummaryReport's total row gives grand sales, net sales and tax for one day per request.
Tax paid out = bank payments in the "Sales Tax Remitted" group (Oklahoma Tax Commission).
"""
import collections
import datetime as dt
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS day_tax (
  client TEXT NOT NULL, date TEXT NOT NULL, gross REAL, net REAL, tax REAL, checks INTEGER, source TEXT, fetched_at TEXT,
  PRIMARY KEY (client, date));
CREATE TABLE IF NOT EXISTS sale_server (
  client TEXT NOT NULL, date TEXT NOT NULL, server TEXT NOT NULL, gross REAL, net REAL, tax REAL, checks INTEGER,
  PRIMARY KEY (client, date, server));
"""
LINGA_TAX_DAYS_PER_PULL = 10


def ensure(db):
    db.executescript(SCHEMA)


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("$", "") or 0)
    except ValueError:
        return 0.0


def shifts_by_day(path, a, b):
    """{date: {gross, tax, closeouts, taxed}} from Shifts (read-only). Gross includes tax."""
    out = {}
    if not path:
        return out
    try:
        s = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        cols = {r[1] for r in s.execute("PRAGMA table_info(shifts)")}
        tax = "sales_tax_cents" if "sales_tax_cents" in cols else "0"
        rows = s.execute("SELECT shift_date, gross_sales_cents, %s FROM shifts WHERE shift_date BETWEEN ? AND ?" % tax,
                         (a.isoformat() if hasattr(a, "isoformat") else a, b.isoformat() if hasattr(b, "isoformat") else b)).fetchall()
        s.close()
    except sqlite3.Error:
        return out
    for d, g, t in rows:
        x = out.setdefault(d, dict(gross=0.0, tax=0.0, closeouts=0, taxed=0))
        x["gross"] += (g or 0) / 100
        x["tax"] += (t or 0) / 100
        if (g or 0) > 0:  # closeouts with no sales (tip-out / support rows) don't need tax
            x["closeouts"] += 1
            x["taxed"] += 1 if (t or 0) > 0 else 0
    return out


def pull_linga_tax(db, client, c, shifts_path, start, end, limit=LINGA_TAX_DAYS_PER_PULL):
    """Fill day_tax from Linga for the most recent days that have sales but no tax yet. Part of a manual pull."""
    from . import linga
    ensure(db)
    store, key = linga.keys(c)
    if not store:
        return 0
    from .tips import _now
    blocked = db.execute("SELECT v FROM meta WHERE client=? AND k='linga_blocked_until'", (client,)).fetchone()
    if blocked and blocked[0] > _now():
        return 0
    have = {r[0] for r in db.execute("SELECT date FROM day_tax WHERE client=?", (client,))}
    sh = shifts_by_day(shifts_path, start, end)
    sales_days = {r[0] for r in db.execute("SELECT date FROM sales_day WHERE client=? AND date BETWEEN ? AND ? AND (card_sales>0 OR cash_sales>0)",
                                           (client, start.isoformat(), end.isoformat()))} | {d for d, x in sh.items() if x["gross"] > 0}
    todo = sorted((d for d in sales_days if d not in have and not (sh.get(d) and sh[d]["taxed"] == sh[d]["closeouts"])), reverse=True)[:limit]
    n, ts = 0, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for d in todo:
        try:
            rows = linga.fetch_day(store, key, d)
        except linga.QuotaExceeded as e:
            db.execute("INSERT INTO meta(client,k,v) VALUES(?,?,?) ON CONFLICT(client,k) DO UPDATE SET v=excluded.v",
                       (client, "linga_blocked_until", e.until.isoformat(timespec="seconds")))
            db.commit()
            break
        total = next((r for r in rows if isinstance(r, dict) and r.get("isTotal")), None) if isinstance(rows, list) else None
        checks = sum(1 for r in rows if isinstance(r, dict) and not r.get("isTotal")) if isinstance(rows, list) else 0
        db.execute("INSERT OR REPLACE INTO day_tax VALUES(?,?,?,?,?,?,?,?)",
                   (client, d, _num((total or {}).get("grandSales")), _num((total or {}).get("netSales")),
                    _num((total or {}).get("totalTaxAmount")), checks, "linga", ts))
        db.commit()
        n += 1
    return n


def import_sale_summary(db, client, data, user=""):
    """Linga back office → Reports → Sale Summary Report (.xlsx, one row per check). Stores per business day
    gross receipts, net sales, tax and check count in day_tax (source 'report'); re-importing replaces those days."""
    from . import xlsxlite
    ensure(db)
    if isinstance(data, list):
        rows = data
    else:
        try:
            rows = xlsxlite.rows(data)
        except Exception:
            raise ValueError("That file isn't an Excel workbook. Export the Sale Summary Report from Linga as Excel.")
    hi = next((i for i, r in enumerate(rows[:20]) if r and "Net sales" in [str(x or "").strip() for x in r]), None)
    if hi is None:
        raise ValueError("No 'Net sales' column found — is this Linga's Sale Summary Report?")
    col = {str(x or "").strip().lower(): j for j, x in enumerate(rows[hi])}
    need = ["transaction date", "net sales", "total tax", "gross receipt"]
    if any(n not in col for n in need):
        raise ValueError("Missing columns: %s." % ", ".join(n for n in need if n not in col))
    days = collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0])
    per = collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0])
    who_col = col.get("close by")
    for r in rows[hi + 1:]:
        when = r[col["transaction date"]] if len(r) > col["transaction date"] else None
        if not when:
            continue
        if isinstance(when, dt.datetime):
            d = when.date()
        else:
            try:
                d = dt.datetime.strptime(str(when).strip(), "%m-%d-%Y %I:%M %p").date()
            except ValueError:
                continue
        x = days[d.isoformat()]
        x[0] += _num(r[col["gross receipt"]]); x[1] += _num(r[col["net sales"]]); x[2] += _num(r[col["total tax"]]); x[3] += 1
        if who_col is not None:
            y = per[(d.isoformat(), str(r[who_col] or "?").strip() or "?")]
            y[0] += _num(r[col["gross receipt"]]); y[1] += _num(r[col["net sales"]]); y[2] += _num(r[col["total tax"]]); y[3] += 1
    if not days:
        raise ValueError("No checks found in that report.")
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for d, (g, n, t, k) in days.items():
        db.execute("INSERT OR REPLACE INTO day_tax VALUES(?,?,?,?,?,?,?,?)",
                   (client, d, round(g, 2), round(n, 2), round(t, 2), k, "report", ts))
    if per:
        for d in days:
            db.execute("DELETE FROM sale_server WHERE client=? AND date=?", (client, d))
        for (d, w), (g, n, t, k) in per.items():
            db.execute("INSERT OR REPLACE INTO sale_server VALUES(?,?,?,?,?,?,?)", (client, d, w, round(g, 2), round(n, 2), round(t, 2), k))
    db.commit()
    v = list(days.values())
    return dict(days=len(days), first=min(days), last=max(days), checks=sum(x[3] for x in v),
                gross=round(sum(x[0] for x in v), 2), net=round(sum(x[1] for x in v), 2), tax=round(sum(x[2] for x in v), 2))


def _month(d):
    return d[:7]


def cash_by_day(db, client, shifts_path, a, b):
    """Cash (and customer checks) customers paid per business day: Linga's payment archive while Linga was the POS, else
    the POS day totals (Linga API / Heartland), else the Heartland cash reports, else cash turned in on Shifts closeouts."""
    out = {}
    for r in db.execute("SELECT date, card_sales, cash_sales, source FROM sales_day WHERE client=? AND source IN ('linga','pos')", (client,)):
        if (r["cash_sales"] or 0) > 0.005:
            out[r["date"]] = round(r["cash_sales"], 2)
    try:
        from . import posreports
        for d, v in posreports.cash_by_day(db, client).items():
            if d not in out and v > 0.005:
                out[d] = v
    except Exception:  # noqa: BLE001
        pass
    try:
        for r in db.execute("""SELECT date, sum(CASE WHEN lower(tender) NOT IN ('linga cc','linga reader') THEN amount ELSE 0 END) c
                               FROM linga_payment WHERE client=? AND lower(status)='success' GROUP BY date""", (client,)):
            if (r["c"] or 0) > 0.005:
                out[r["date"]] = round(r["c"], 2)  # every check Linga took: cash + customer checks
    except sqlite3.OperationalError:
        pass
    if shifts_path:
        try:
            from . import cash as C
            for d, x in C.shifts_by_day(shifts_path, dt.date.fromisoformat(a), dt.date.fromisoformat(b)).items():
                if d not in out and x["turned_in"] > 0.005:
                    out[d] = round(x["turned_in"], 2)
        except Exception:  # noqa: BLE001
            pass
    return out


def _tax_sources(db, client, shifts_path, a, b):
    """(day(d) -> (gross, tax, net, source), liquor {date: liquor tax}, sh, lt, taxes) shared by summary() and tax_report()."""
    sh = shifts_by_day(shifts_path, a, b)
    lt = {r["date"]: dict(r) for r in db.execute("SELECT * FROM day_tax WHERE client=?", (client,))}
    from . import lingaarchive as LA
    taxes = LA.tax_totals(db, client)
    liquor_total = taxes.get("State Liquor Tax", 0.0)
    weight = {d: max(0.0, (v["tax"] or 0) - LA.SALES_TAX_RATE * (v["net"] or 0)) for d, v in lt.items() if v["source"] == "report"}
    wsum = sum(weight.values())
    liquor = {d: liquor_total * w / wsum for d, w in weight.items()} if liquor_total and wsum else {}
    from . import posreports
    pos_liq = {d: v for d, v in posreports.liquor_by_day(db, client).items() if (lt.get(d) or {}).get("source") == "pos-report"}
    liquor.update(pos_liq)
    # sales tax charged on alcohol, and alcohol net sales, per day: Linga days by Linga's own Alc Sales Tax ÷ State Liquor
    # Tax ratio; Heartland days at 6.05% of alcohol sales (Daily Summary alcohol, else liquor tax ÷ 13.5%)
    ratio = (taxes.get("Alc Sales Tax", 0.0) / liquor_total) if liquor_total else LA.SALES_TAX_RATE / 0.135
    alc_net_pos = {}
    try:
        alc_net_pos = {r[0]: r[1] or 0.0 for r in db.execute("SELECT business_date, alcohol_net FROM pos_daily WHERE client=?", (client,))}
    except sqlite3.OperationalError:
        pass
    alc_sales, alc_net = {}, {}
    for d, v in liquor.items():
        if d in pos_liq or (lt.get(d) or {}).get("source") == "pos-report":
            alc_net[d] = alc_net_pos.get(d) or v / 0.135
            alc_sales[d] = alc_net[d] * LA.SALES_TAX_RATE
        else:
            alc_net[d] = v / 0.135
            alc_sales[d] = v * ratio
    # who owes the alcohol taxes: until the restaurant has its own liquor license (meta own_liquor_license_from), the hotel
    # holding the license reports the alcohol sales and pays both alcohol taxes, so the sales tax on alcohol + the liquor
    # tax collected are owed to the hotel; from the license date the sales tax on alcohol goes on the restaurant's own
    # sales tax return and only the liquor (mixed beverage) tax is separate.
    lic = license_from(db, client)

    def day(d):
        s, l = sh.get(d), lt.get(d)
        if l and l["gross"]:
            return l["gross"], l["tax"], (l["net"] or l["gross"] - l["tax"]), l["source"]
        if s and s["closeouts"] and s["taxed"] == s["closeouts"]:
            return s["gross"], s["tax"], s["gross"] - s["tax"], "shifts"
        return (s["gross"] if s else 0.0), None, None, None
    # alcohol bucket per day. Hotel's license: the restaurant owes 6.05% of its own (non-alcohol) sales; everything else
    # the POS charged that day was charged on alcohol (Linga's tax-type labels don't line up with the items — alcohol was
    # charged ≈21.4%), so it goes with the alcohol, owed to the hotel. Own license: only the liquor tax is separate.
    alcohol = {}
    for d, v in liquor.items():
        if lic and d >= lic:
            alcohol[d] = v
            continue
        g_, t_, n_, src_ = day(d)
        if t_ is None:
            alcohol[d] = v + alc_sales.get(d, 0.0)
            continue
        own = LA.SALES_TAX_RATE * max(0.0, (n_ or 0.0) - alc_net.get(d, 0.0))
        alcohol[d] = max(0.0, min(t_, t_ - own))
    return day, liquor, sh, lt, taxes, pos_liq, alcohol, alc_net


def license_from(db, client):
    """First day the restaurant sells alcohol under its own liquor license ('' / None = still the hotel's license)."""
    from .books import get_meta
    try:
        return (get_meta(db, client, "own_liquor_license_from", "") or "").strip() or None
    except sqlite3.OperationalError:
        return None


SOURCE_LABEL = {"report": "Linga Sale Summary", "linga": "Linga", "pos-report": "Heartland reports", "shifts": "Shifts closeouts"}


def _paid_by_month(db, client, since):
    paid = collections.defaultdict(float)
    for r in db.execute("""SELECT e.date, e.amount FROM entry e JOIN category c ON c.client=e.client AND c.name=e.category
                           WHERE e.client=? AND e.status!='void' AND e.amount<0 AND (c.pl_group='Sales Tax Remitted'
                           OR c.name IN ('Sales Tax Payable','Alc Sales Tax')) AND e.date>=?""", (client, since)):
        paid[_month(r["date"])] += -r["amount"]
    return paid


def tax_report(db, client, shifts_path, a, b):
    """Sales tax report for a filing period [a, b] (ISO dates): per day and per month — gross receipts, net sales, sales
    tax and mixed beverage (liquor) tax collected, which source each day came from, days with sales but no tax figures,
    and Sales Tax Remitted payments in the month after each month (due by the 25th of the month after)."""
    ensure(db)
    day, liquor, sh, lt, taxes, pos_liq, alcohol, alc_net = _tax_sources(db, client, shifts_path, a, b)
    lic = license_from(db, client)
    batch = {r["date"]: (r["card_sales"] or 0) for r in db.execute(
        "SELECT date, card_sales FROM sales_day WHERE client=? AND source IN ('batch','processor','pos','linga')", (client,))}
    days, missing = [], []
    d = dt.date.fromisoformat(a)
    while d.isoformat() <= b:
        k = d.isoformat()
        gross, tax, net, src = day(k)
        if gross or batch.get(k):
            if tax is None:
                missing.append(dict(date=k, gross=round(gross or 0, 2), card=round(batch.get(k, 0), 2)))
            else:
                liq = round(min(alcohol.get(k, 0.0), tax), 2)
                hotel = not lic or k < lic
                days.append(dict(date=k, source=SOURCE_LABEL.get(src, src), gross=round(gross, 2), net=round(net, 2), tax=round(tax, 2),
                                 liquor=liq, mixed=round(liquor.get(k, 0.0), 2), sales_tax=round(tax - liq, 2), liquor_actual=k in pos_liq,
                                 hotel=hotel, taxable_net=round(net - (alc_net.get(k, 0.0) if hotel else 0.0), 2)))
        d += dt.timedelta(days=1)
    paid = _paid_by_month(db, client, a[:7] + "-01")
    months = collections.OrderedDict()
    for x in days:
        m = months.setdefault(x["date"][:7], dict(month=x["date"][:7], days=0, gross=0.0, net=0.0, tax=0.0, liquor=0.0, mixed=0.0, sales_tax=0.0, taxable_net=0.0, missing=0))
        m["days"] += 1
        for f in ("gross", "net", "tax", "liquor", "mixed", "sales_tax", "taxable_net"):
            m[f] += x[f]
    for x in missing:
        months.setdefault(x["date"][:7], dict(month=x["date"][:7], days=0, gross=0.0, net=0.0, tax=0.0, liquor=0.0, mixed=0.0, sales_tax=0.0, taxable_net=0.0, missing=0))["missing"] += 1
    out_m = []
    for k in sorted(months):
        m = months[k]
        first = dt.date.fromisoformat(k + "-01")
        nxt = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        for f in ("gross", "net", "tax", "liquor", "mixed", "sales_tax", "taxable_net"):
            m[f] = round(m[f], 2)
        m.update(label=first.strftime("%B %Y"), due=nxt.replace(day=25).isoformat(), paid_next=round(paid.get(nxt.isoformat()[:7], 0.0), 2),
                 rate=round(m["sales_tax"] / m["taxable_net"] * 100, 2) if m["taxable_net"] > 0 else None)
        m["owed"] = round(m["tax"] - m["paid_next"], 2)
        out_m.append(m)
    tot = {f: round(sum(x[f] for x in days), 2) for f in ("gross", "net", "tax", "liquor", "mixed", "sales_tax", "taxable_net")}
    tot.update(days=len(days), missing=len(missing), paid=round(sum(m["paid_next"] for m in out_m), 2),
               rate=round(tot["sales_tax"] / tot["taxable_net"] * 100, 2) if tot["taxable_net"] > 0 else None)
    return dict(days=days, missing=missing, months=out_m, total=tot, license_from=lic, hotel_any=any(x["hotel"] for x in days), liquor_estimated=any(not x["liquor_actual"] and x["liquor"] for x in days))


def summary(db, client, shifts_path, periods, since):
    """Per pay period and per month: gross sales, sales tax, product sales, card payments, card tips, tax paid."""
    ensure(db)
    if not periods:
        return None
    a = min(p[0] for p in periods)
    b = max(p[1] for p in periods)
    today = dt.date.today().isoformat()
    sh = shifts_by_day(shifts_path, a, today)
    lt = {r["date"]: dict(r) for r in db.execute("SELECT * FROM day_tax WHERE client=?", (client,))}
    batch = {r["date"]: dict(r) for r in db.execute("SELECT date, card_sales, card_tips FROM sales_day WHERE client=? AND source IN ('batch','processor','pos','linga')", (client,))}
    cash = cash_by_day(db, client, shifts_path, a, today)
    # Linga's tax report gives totals by tax type for the whole period; spread the 13.5% liquor tax over the days by
    # each day's tax above the 6.05% sales tax rate (the only part liquor can explain)
    from . import lingaarchive as LA
    taxes = LA.tax_totals(db, client)
    liquor_total = taxes.get("State Liquor Tax", 0.0)
    weight = {d: max(0.0, (v["tax"] or 0) - LA.SALES_TAX_RATE * (v["net"] or 0)) for d, v in lt.items() if v["source"] == "report"}
    wsum = sum(weight.values())
    liquor = {d: liquor_total * w / wsum for d, w in weight.items()} if liquor_total and wsum else {}
    from . import posreports
    pos_liq = {d: v for d, v in posreports.liquor_by_day(db, client).items() if (lt.get(d) or {}).get("source") == "pos-report"}
    liquor.update(pos_liq)  # POS Taxes report: the actual liquor tax per day, not an estimate

    def day(d):
        """(gross, tax, net, source) for one business day: Linga (Sale Summary Report or API) first — it has every
        check — else Shifts when every closeout with sales has tax."""
        s, l = sh.get(d), lt.get(d)
        if l and l["gross"]:
            return l["gross"], l["tax"], (l["net"] or l["gross"] - l["tax"]), l["source"]  # report / linga / pos-report / pos:<name>
        if s and s["closeouts"] and s["taxed"] == s["closeouts"]:
            return s["gross"], s["tax"], s["gross"] - s["tax"], "shifts"
        return (s["gross"] if s else 0.0), None, None, None

    def span(x, y):
        g = t = net = card = tips = short = liq = csh = g_all = 0.0
        days = missing = 0
        d = dt.date.fromisoformat(x)
        while d.isoformat() <= y:
            k = d.isoformat()
            gross, tax, n, src = day(k)
            bt = batch.get(k) or {}
            if gross or bt.get("card_sales"):
                days += 1
                if tax is None:
                    missing += 1
                else:
                    g += gross
                    t += tax
                    net += n
                    liq += liquor.get(k, 0.0)
                    if src in ("report", "linga"):  # sales Linga has that the Shifts closeouts don't
                        short += max(0.0, gross - (sh.get(k) or {}).get("gross", 0.0))
            card += bt.get("card_sales") or 0
            tips += bt.get("card_tips") or 0
            csh += cash.get(k, 0.0)
            g_all += gross or 0.0
            d += dt.timedelta(days=1)
        return dict(cash=round(csh, 2), gap=round(g_all - card - csh, 2), gross_all=round(g_all, 2),
                    gross=round(g, 2), tax=round(t, 2), liquor=round(liq, 2), sales_tax=round(t - liq, 2), product=round(net, 2), card=round(card, 2), tips=round(tips, 2),
                    days=days, missing=missing, shifts_short=round(short, 2), rate=round(t / net * 100, 2) if net > 0 else None)

    rows = [dict(span(x, y), start=x, end=y) for x, y in sorted(periods, reverse=True)]
    paid = collections.defaultdict(float)
    for r in db.execute("""SELECT e.date, e.amount, e.category FROM entry e JOIN category c ON c.client=e.client AND c.name=e.category
                           WHERE e.client=? AND e.status!='void' AND e.amount<0 AND (c.pl_group='Sales Tax Remitted'
                           OR c.name IN ('Sales Tax Payable','Alc Sales Tax')) AND e.date>=?""", (client, since)):
        paid[_month(r["date"])] += -r["amount"]
    months = []
    m = dt.date.fromisoformat(since).replace(day=1)
    while m.isoformat() <= today:
        nxt = (m.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        x = span(m.isoformat(), (nxt - dt.timedelta(days=1)).isoformat())
        months.append(dict(x, month=m.isoformat()[:7], label=m.strftime("%b %Y"), paid_next=round(paid.get(nxt.isoformat()[:7], 0), 2),
                           paid_in=round(paid.get(m.isoformat()[:7], 0), 2)))
        m = nxt
    return dict(periods=rows, months=list(reversed(months)),
                tax_days=sum(1 for v in sh.values() if v["taxed"]),
                linga_days=len(lt), taxes=taxes, liquor_split=bool(liquor), pos_tax_last=max(pos_liq, default=None), report_last=max((d for d, v in lt.items() if v["source"] == "report"), default=None))
