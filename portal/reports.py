"""Cash-basis reports built from the snapshot ledger (Checkbook rows + P&L groups).

Signs: in the ledger, money in is positive and money out is negative.
On reports, income is shown positive and expenses are shown positive (as costs).
"""
import calendar
import datetime as dt
from collections import OrderedDict, defaultdict

PL_ORDER = [  # (group, title, sign on report, subtotal label)
    ("Revenue", "Revenue", 1, "Gross sales"),
    ("Sales Tax Remitted", "Less: sales tax remitted", -1, "Sales tax remitted"),
    ("Cost of Goods Sold", "Cost of goods sold", -1, "Total cost of goods sold"),
    ("Labor", "Labor", -1, "Total labor"),
    ("Operating Expenses", "Operating expenses", -1, "Total operating expenses"),
    ("Other Income", "Other income", 1, "Total other income"),
]
FINANCING = ("Not in P&L",)
SUSPENSE = ("Needs Category",)


# ------------------------------------------------------------------ periods
def month_start(d):
    return d.replace(day=1)


def month_end(d):
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


PRESETS = OrderedDict([
    ("this_month", "This month"), ("last_month", "Last month"), ("last_3", "Last 3 months"), ("this_quarter", "This quarter"),
    ("last_quarter", "Last quarter"), ("ytd", "Year to date"), ("last_12", "Last 12 months"),
    ("all", "All time"), ("custom", "Custom"),
])


def resolve_period(args, today, first_date):
    p = args.get("period") or "this_month"
    if p == "custom":
        try:
            a = dt.date.fromisoformat(args.get("from", ""))
            b = dt.date.fromisoformat(args.get("to", ""))
            if a > b:
                a, b = b, a
            return p, a, b
        except ValueError:
            p = "this_month"
    q0 = dt.date(today.year, 3 * ((today.month - 1) // 3) + 1, 1)
    rng = {
        "this_month": (month_start(today), month_end(today)),
        "last_month": (month_start(add_months(today, -1)), month_end(add_months(today, -1))),
        "last_3": (month_start(add_months(today, -3)), month_end(add_months(today, -1))),
        "this_quarter": (q0, month_end(add_months(q0, 2))),
        "last_quarter": (add_months(q0, -3), month_end(add_months(q0, -1))),
        "ytd": (dt.date(today.year, 1, 1), today),
        "last_12": (month_start(add_months(today, -11)), month_end(today)),
        "all": (first_date or dt.date(today.year, 1, 1), today),
    }
    if p not in rng:
        p = "this_month"
    a, b = rng[p]
    try:  # dates typed over a preset turn it into a custom range
        fa, fb = dt.date.fromisoformat(args.get("from", "")), dt.date.fromisoformat(args.get("to", ""))
        if (fa, fb) != (a, b) and args.get("edited") != "0":
            return ("custom",) + ((fa, fb) if fa <= fb else (fb, fa))
    except ValueError:
        pass
    return p, a, b


def months_between(a, b):
    out, m = [], month_start(a)
    while m <= b:
        out.append((max(m, a), min(month_end(m), b)))
        m = add_months(m, 1)
    return out


# ------------------------------------------------------------------ helpers
def _rows(snap):
    for r in snap.get("ledger", []):
        d, desc, payee, cat, net, chk = r[:6]
        yield dt.date.fromisoformat(d), desc, payee, cat, net, chk


def _group(snap, cat):
    return snap.get("groups", {}).get(cat, "Needs Category")


def _sum_by_cat(snap, a, b):
    t = defaultdict(float)
    for d, _, _, cat, net, _ in _rows(snap):
        if a <= d <= b:
            t[cat] += net
    return t


# ------------------------------------------------------------------ profit & loss
def profit_and_loss(snap, a, b, by_month=False):
    cols = months_between(a, b) if by_month else [(a, b)]
    if by_month and len(cols) > 12:
        cols = cols[-12:]
    sums = [_sum_by_cat(snap, x, y) for x, y in cols]
    cats = sorted({c for s in sums for c in s})
    sections, totals = [], {}
    for grp, title, sign, sub in PL_ORDER:
        lines = []
        for c in cats:
            if _group(snap, c) == grp:
                vals = [round(sign * s.get(c, 0.0), 2) for s in sums]
                if any(abs(v) >= 0.005 for v in vals):
                    lines.append({"label": c, "values": vals, "total": round(sum(vals), 2)})
        tot = [round(sum(l["values"][i] for l in lines), 2) for i in range(len(cols))]
        totals[grp] = tot
        sections.append({"title": title, "group": grp, "lines": lines, "subtotal": sub, "values": tot,
                         "total": round(sum(tot), 2)})
    n = len(cols)
    z = [0.0] * n
    g = lambda k: totals.get(k, z)
    net_sales = [g("Revenue")[i] - g("Sales Tax Remitted")[i] for i in range(n)]
    gross = [net_sales[i] - g("Cost of Goods Sold")[i] for i in range(n)]
    op = [gross[i] - g("Labor")[i] - g("Operating Expenses")[i] for i in range(n)]
    ni = [op[i] + g("Other Income")[i] for i in range(n)]
    pct = lambda v, i: (v / net_sales[i]) if net_sales[i] > 0 else None
    kpi = [
        ("Food & beverage cost %", [pct(sum(sums[i].get(c, 0) * -1 for c in ("Food COGS", "Beverage COGS")), i) for i in range(n)]),
        ("Labor %", [pct(g("Labor")[i], i) for i in range(n)]),
        ("Prime cost %", [pct(g("Cost of Goods Sold")[i] + g("Labor")[i], i) for i in range(n)]),
        ("Net margin %", [pct(ni[i], i) for i in range(n)]),
    ]
    kpi = [(label, vals) for label, vals in kpi if any(v for v in vals)]  # e.g. no food cost % for a non-restaurant
    other = []  # cash movements that are not profit or loss
    for c in cats:
        grp = _group(snap, c)
        if grp in FINANCING + SUSPENSE:
            vals = [round(s.get(c, 0.0), 2) for s in sums]
            if any(abs(v) >= 0.005 for v in vals):
                other.append({"label": c, "values": vals, "total": round(sum(vals), 2), "suspense": grp in SUSPENSE})
    r2 = lambda xs: [round(x, 2) for x in xs]
    return {
        "columns": [{"from": x.isoformat(), "to": y.isoformat(), "label": x.strftime("%b %Y") if by_month else
                     "%s – %s" % (x.strftime("%b %d, %Y"), y.strftime("%b %d, %Y"))} for x, y in cols],
        "sections": sections,
        "net_sales": r2(net_sales), "gross_profit": r2(gross), "operating_income": r2(op), "net_income": r2(ni),
        "kpis": kpi, "other": other,
    }


# ------------------------------------------------------------------ balance sheet
def balance_sheet(snap, as_of):
    cash_books = 0.0
    by_group = defaultdict(float)
    by_cat = defaultdict(float)
    for d, _, _, cat, net, _ in _rows(snap):
        if d <= as_of:
            cash_books += net
            by_group[_group(snap, cat)] += net
            by_cat[cat] += net
    pl_net = sum(v for k, v in by_group.items() if k not in FINANCING + SUSPENSE)
    loans = by_cat.get("Loan Payables", 0.0)
    contrib = by_cat.get("Owner Contribution", 0.0)
    investors = by_cat.get("Investor Contributions", 0.0)
    draws = by_cat.get("Owner Draw / Personal", 0.0)
    tips = by_cat.get("Card Tips Payable", 0.0)
    stax = by_cat.get("Sales Tax Payable", 0.0) if _group(snap, "Sales Tax Payable") == "Not in P&L" else 0.0
    atax = by_cat.get("Alc Sales Tax", 0.0) if _group(snap, "Alc Sales Tax") == "Not in P&L" else 0.0
    transfers = sum(v for c, v in by_cat.items() if _group(snap, c) == "Not in P&L"
                    and c not in ("Loan Payables", "Owner Contribution", "Investor Contributions", "Owner Draw / Personal", "Card Tips Payable",
                                  "Sales Tax Payable", "Alc Sales Tax"))
    suspense = by_group.get("Needs Category", 0.0)
    cash_on_hand = None
    for d, bal in snap.get("cash", []):
        if dt.date.fromisoformat(d) <= as_of:
            cash_on_hand = bal
    liab = round(loans + tips + stax + atax, 2)
    liabilities = [(k, round(v, 2)) for k, v in (("Loans payable", loans), ("Card tips owed to staff", tips),
                                                  ("Sales tax owed — state", stax), ("Sales tax owed — alcohol (mixed beverage)", atax)) if abs(v) >= 0.005]
    equity = [("Owner contributions", contrib), ("Investor contributions", investors), ("Owner draws", draws), ("Transfers", transfers),
              ("Retained earnings (profit since books began)", pl_net),
              ("Uncategorized items (needs review)", suspense)]
    equity = [(k, round(v, 2)) for k, v in equity if abs(v) >= 0.005]
    return {
        "as_of": as_of.isoformat(),
        "assets": [("Cash in bank (per books)", round(cash_books, 2))],
        "total_assets": round(cash_books, 2),
        "liabilities": liabilities,
        "total_liabilities": liab,
        "equity": equity,
        "total_equity": round(sum(v for _, v in equity), 2),
        "memo": {
            "bank_balance": snap.get("currentBalance"),
            "cash_on_hand": cash_on_hand,
            "open_bills": snap.get("openBills"),
        },
    }


# ------------------------------------------------------------------ cash flow
def cash_flow(snap, a, b, by_month=True):
    cols = months_between(a, b) if by_month else [(a, b)]
    if len(cols) > 12:
        cols = cols[-12:]
        a = cols[0][0]
    begin = sum(net for d, _, _, _, net, _ in _rows(snap) if d < a)
    lines_def = [
        ("Operating activities", [
            ("Cash from customers", lambda c, g: g == "Revenue"),
            ("Other income received", lambda c, g: g == "Other Income"),
            ("Paid for food, beverage & goods", lambda c, g: g == "Cost of Goods Sold"),
            ("Paid for labor & payroll taxes", lambda c, g: g == "Labor"),
            ("Paid for operating expenses", lambda c, g: g == "Operating Expenses"),
            ("Sales tax paid", lambda c, g: g == "Sales Tax Remitted"),
            ("Card tips held for staff (collected − paid)", lambda c, g: c == "Card Tips Payable"),
            ("Sales tax held for the state (collected − paid)", lambda c, g: g == "Not in P&L" and c in ("Sales Tax Payable", "Alc Sales Tax")),
        ], "Net cash from operations"),
        ("Financing activities", [
            ("Owner contributions", lambda c, g: c == "Owner Contribution"),
            ("Investor contributions", lambda c, g: c == "Investor Contributions"),
            ("Owner draws", lambda c, g: c == "Owner Draw / Personal"),
            ("Loans (net)", lambda c, g: c == "Loan Payables"),
            ("Transfers", lambda c, g: g == "Not in P&L" and c not in ("Owner Contribution", "Investor Contributions", "Owner Draw / Personal", "Loan Payables", "Card Tips Payable", "Sales Tax Payable", "Alc Sales Tax")),
        ], "Net cash from financing"),
        ("Not yet categorized", [
            ("Uncategorized / missing checks", lambda c, g: g == "Needs Category"),
        ], "Net uncategorized"),
    ]
    sums = [_sum_by_cat(snap, x, y) for x, y in cols]
    sections = []
    net_change = [0.0] * len(cols)
    for title, defs, subtotal in lines_def:
        lines = []
        for label, f in defs:
            vals = [round(sum(v for c, v in s.items() if f(c, _group(snap, c))), 2) for s in sums]
            if any(abs(v) >= 0.005 for v in vals):
                lines.append({"label": label, "values": vals, "total": round(sum(vals), 2)})
        tot = [round(sum(l["values"][i] for l in lines), 2) for i in range(len(cols))]
        net_change = [net_change[i] + tot[i] for i in range(len(cols))]
        sections.append({"title": title, "lines": lines, "subtotal": subtotal, "values": tot, "total": round(sum(tot), 2)})
    beginning, ending, run = [], [], begin
    for i in range(len(cols)):
        beginning.append(round(run, 2))
        run += net_change[i]
        ending.append(round(run, 2))
    return {
        "columns": [{"from": x.isoformat(), "to": y.isoformat(), "label": x.strftime("%b %Y") if by_month else
                     "%s – %s" % (x.strftime("%b %d"), y.strftime("%b %d, %Y"))} for x, y in cols],
        "beginning": beginning, "sections": sections,
        "net_change": [round(v, 2) for v in net_change], "ending": ending,
        "total_change": round(sum(net_change), 2),
    }


# ------------------------------------------------------------------ vendors & transactions
def vendors(snap, a, b, limit=40, by="payee", today=None):
    """Spending by payee (or by category) with averages per week, per 2-week pay period and per month. The averages use
    the days the period actually covers so far (a period that ends in the future counts only up to today)."""
    today = today or dt.date.today()
    out = defaultdict(lambda: [0.0, 0, defaultdict(float)])
    total = 0.0
    first = last = None
    for d, desc, payee, cat, net, _ in _rows(snap):
        first = d if first is None or d < first else first
        last = d if last is None or d > last else last
        if a <= d <= b and net < 0 and _group(snap, cat) not in FINANCING:
            name = (cat or "Uncategorized") if by == "category" else (payee or desc or "(no payee)")
            other = (payee or desc or "(no payee)") if by == "category" else cat
            out[name][0] += -net
            out[name][1] += 1
            out[name][2][other] += -net
            total += -net
    lo = max(a, first) if first else a
    hi = min(b, today, last) if last else min(b, today)  # nothing is known past the last bank sync
    days = max(1, (hi - lo).days + 1)
    weeks, periods, months = days / 7.0, days / 14.0, days / 30.4375
    rows = sorted(out.items(), key=lambda x: -x[1][0])
    if limit:
        rows = rows[:limit]
    res = []
    for k, v in rows:
        top = sorted(v[2].items(), key=lambda x: -x[1])
        r = {"name": k, "payee": k, "amount": round(v[0], 2), "count": v[1], "share": (v[0] / total) if total else 0,
             "per_week": round(v[0] / weeks, 2), "per_period": round(v[0] / periods, 2), "per_month": round(v[0] / months, 2)}
        if by == "category":
            r["category"] = k
            r["vendors"] = len(top)
            r["top"] = ", ".join(n for n, _ in top[:3])
        else:
            r["category"] = top[0][0] if top else ""
        res.append(r)
    return {"total": round(total, 2), "rows": res, "by": by, "days": days, "from": lo, "to": hi,
            "per_week": round(total / weeks, 2), "per_period": round(total / periods, 2), "per_month": round(total / months, 2),
            "more": max(0, len(out) - len(res))}


def transactions(snap, a, b, category=None, payee=None, q=None, group=None):
    """One row per bank transaction at its full bank amount. A transaction split across categories
    (e.g. a card deposit split into Sales - Card and Card Tips Payable) shows its parts under it; when
    filtering by category or group, only the matching part is shown, marked as part of the bank amount."""
    q = (q or "").strip().lower()
    by_entry, order = {}, []
    for r in snap.get("ledger", []):
        d, desc, py, cat, net, chk = r[:6]
        eid = r[6] if len(r) > 6 else None
        state = r[7] if len(r) > 7 else ""
        key = eid if eid is not None else ("row", len(order))
        if key not in by_entry:
            by_entry[key] = dict(date=d, description=desc, payee=py, check=chk, id=eid, state=state, parts=[])
            order.append(key)
        by_entry[key]["parts"].append((cat, net))
    out = []
    for key in order:
        t = by_entry[key]
        d = dt.date.fromisoformat(t["date"])
        if not (a <= d <= b):
            continue
        if payee and t["payee"] != payee:
            continue
        cats = " ".join(c for c, _ in t["parts"])
        if q and q not in (t["description"] + " " + t["payee"] + " " + cats + " " + t["check"]).lower():
            continue
        total = round(sum(v for _, v in t["parts"]), 2)
        parts = [dict(category=c, amount=round(v, 2)) for c, v in t["parts"]]
        split = len(parts) > 1
        base = dict(date=t["date"], description=t["description"], payee=t["payee"], check=t["check"], id=t["id"],
                    state=t["state"], bank_amount=total, parts=parts if split else [])
        if category or group:
            for c, v in t["parts"]:
                if (category and c != category) or (group and _group(snap, c) != group):
                    continue
                out.append(dict(base, category=c, amount=round(v, 2), partial=split))
        else:
            out.append(dict(base, category="Split" if split else parts[0]["category"], amount=total, partial=False))
    out.sort(key=lambda x: x["date"], reverse=True)
    nc = [x for x in out if x["state"]]
    return {"rows": out, "in": round(sum(x["amount"] for x in out if x["amount"] > 0), 2),
            "out": round(sum(x["amount"] for x in out if x["amount"] < 0), 2),
            "not_cleared": len(nc), "not_cleared_total": round(sum(x["amount"] for x in nc), 2)}


def categories(snap):
    return sorted({r[3] for r in snap.get("ledger", [])})


# ------------------------------------------------------------------ chart
def trend_svg(snap, end, months=6):
    """Grouped bars: money in (sales + other income) vs money out (P&L costs), last N months."""
    cols = months_between(month_start(add_months(end, -(months - 1))), month_end(end))
    data = []
    for a, b in cols:
        s = _sum_by_cat(snap, a, b)
        inc = sum(v for c, v in s.items() if _group(snap, c) in ("Revenue", "Other Income"))
        cost = -sum(v for c, v in s.items() if _group(snap, c) in ("Cost of Goods Sold", "Labor", "Operating Expenses", "Sales Tax Remitted"))
        data.append((a.strftime("%b"), a.strftime("%B %Y"), max(inc, 0), max(cost, 0)))
    top = max([max(x[2], x[3]) for x in data] + [1])
    step = 10 ** max(0, len(str(int(top))) - 1)
    top = ((int(top) // step) + 1) * step
    W, H, L, B, T = 640, 220, 56, 28, 12
    pw, ph = W - L - 8, H - B - T
    gw = pw / len(data)
    bw = min(22, gw / 3)

    def y(v):
        return T + ph - ph * v / top

    def fmt(v):
        return "${:,.0f}".format(v)
    parts = ['<svg viewBox="0 0 %d %d" role="img" aria-label="Money in vs money out by month" class="chart">' % (W, H)]
    for k in range(5):
        v = top * k / 4
        parts.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="grid"/>' % (L, W - 8, y(v), y(v)))
        parts.append('<text x="%d" y="%.1f" class="ax" text-anchor="end">%s</text>' % (L - 6, y(v) + 4, "$%dk" % (v / 1000) if top >= 10000 else fmt(v)))
    for i, (lab, full, inc, cost) in enumerate(data):
        cx = L + gw * i + gw / 2
        for j, (v, cls, name) in enumerate(((inc, "s1", "Money in"), (cost, "s2", "Money out"))):
            x = cx - bw - 1 + j * (bw + 2)
            hgt = max(ph * v / top, 0)
            r = min(4, hgt / 2, bw / 2)
            yy = y(v)
            # bar rounded at the data end only, square on the baseline
            if hgt > 0.5:
                base = T + ph
                parts.append('<path class="%s" d="M%.1f,%.1f V%.1f Q%.1f,%.1f %.1f,%.1f H%.1f Q%.1f,%.1f %.1f,%.1f V%.1f Z"><title>%s · %s: %s</title></path>'
                             % (cls, x, base, yy + r, x, yy, x + r, yy, x + bw - r, x + bw, yy, x + bw, yy + r, base, full, name, fmt(v)))
        parts.append('<text x="%.1f" y="%d" class="ax" text-anchor="middle">%s</text>' % (cx, H - 8, lab))
    parts.append("</svg>")
    return "".join(parts), data
