"""Build the at-a-glance snapshot from the WWK accounting workbook (.xlsx).

Reads the values Excel last calculated and saved, so the workbook must have been opened and saved in
Excel (OneDrive autosave does this). Nothing here writes to the workbook.
"""
import datetime as dt
import io

import openpyxl

PAYROLL_CATS = {"Payroll - Wages", "Payroll Taxes", "Payroll Processing Fees"}
PERIOD_DAYS = 14  # biweekly pay period


def _table(wb, name):
    """Return (headers, rows) for an Excel table by name."""
    for ws in wb.worksheets:
        if name in ws.tables:
            ref = ws.tables[name].ref
            cells = [[c.value for c in row] for row in ws[ref]]
            return [str(h) for h in cells[0]], cells[1:]
    return [], []


def _date(v):
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, (int, float)) and v > 20000:
        return (dt.datetime(1899, 12, 30) + dt.timedelta(days=v)).date()
    if isinstance(v, str) and v[:10].count("-") == 2:
        try:
            return dt.date.fromisoformat(v[:10])
        except ValueError:
            return None
    return None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _key(r, ix):
    """Rough merchant key so a pending row can be matched to its posted version."""
    s = str(r[ix["merchant_name"]] or r[ix["description_raw"]] or "").lower()
    s = s.replace("pos purchase", "").split(" / ")[0]
    return "".join(ch for ch in s if ch.isalnum())[:12]


def _live_pending(rows, ix, holds=None):
    """Pending items still on hold at the bank.

    The bank-sync add-in keeps a pending row after the purchase posts (often under a new id), so drop any
    pending row that has a posted twin: linked by pending_transaction_id, or same merchant and amount
    posted within a week. Each posted row can cancel only one pending row.
    """
    is_p = lambda r: r[ix["pending"]] in (True, "TRUE", "True", 1)
    posted = [r for r in rows if not is_p(r)]
    linked = {r[ix["pending_transaction_id"]] for r in posted if r[ix["pending_transaction_id"]]}
    used = set()
    out = []
    for r in sorted((r for r in rows if is_p(r)), key=lambda r: str(r[ix["date"]])):
        if r[ix["transaction_id"]] in linked:
            continue
        d, amt, k = _date(r[ix["date"]]), round(_num(r[ix["amount"]]), 2), _key(r, ix)
        twin = None
        for i, p in enumerate(posted):
            if i in used or round(_num(p[ix["amount"]]), 2) != amt or _key(p, ix) != k:
                continue
            pd = _date(p[ix["date"]])
            newer = str(p[ix["synced_at"]] or "") >= str(r[ix["synced_at"]] or "") if "synced_at" in ix else True
            if d and pd and 0 <= (pd - d).days <= 7 and newer:
                twin = i
                break
        if twin is not None:
            used.add(twin)
            continue
        out.append(_item(r, ix))
    # Cross-check with the bank: pending items must add up to available minus current balance.
    # If they don't, the feed still holds stale pending rows with no posted twin; pick the most recent
    # combination of pending rows that matches the bank's figure exactly.
    if holds is not None and abs(sum(x["amount"] for x in out) - holds) > 0.005:
        allp = [_item(r, ix) for r in rows if is_p(r)]
        if len(allp) <= 16:
            best = None
            for mask in range(1, 1 << len(allp)):
                pick = [allp[i] for i in range(len(allp)) if mask >> i & 1]
                if abs(sum(x["amount"] for x in pick) - holds) <= 0.005:
                    score = (min(x["date"] for x in pick), -len(pick))
                    if best is None or score > best[0]:
                        best = (score, pick)
            if best:
                return best[1]
        if abs(holds) < 0.005:
            return []
    return out


def _item(r, ix):
    d = _date(r[ix["date"]])
    desc = r[ix["merchant_name"]] or str(r[ix["description_raw"]] or "").split(" / ")[0]
    return {"date": d.isoformat() if d else "", "description": desc, "amount": round(-_num(r[ix["amount"]]), 2)}


def build_snapshot(xlsx_bytes, days_ahead=14, today=None):
    wb = openpyxl.load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=False)
    today = today or dt.date.today()
    horizon = today + dt.timedelta(days=days_ahead)

    # balances
    acc = wb["Accounts"]
    head = [c.value for c in acc[1]]
    row2 = [c.value for c in acc[2]]
    a = dict(zip(head, row2))

    # pending bank items
    h, rows = _table(wb, "tbltransactions")
    ix = {k: i for i, k in enumerate(h)}
    cur, avail = a.get("current_balance"), a.get("available_balance")
    holds = round(_num(avail) - _num(cur), 2) if cur is not None and avail is not None else None
    pending = _live_pending(rows, ix, holds)
    pending.sort(key=lambda p: p["date"])

    # bills due: open bills + recurring expected within the horizon
    bills = []
    h, rows = _table(wb, "tblBills")
    ix = {k: i for i, k in enumerate(h)}
    for r in rows:
        status = r[ix["Status"]] or ""
        if not status or status == "Paid":
            continue
        due = _date(r[ix["Due Date"]])
        if due and due > horizon:
            continue
        bills.append({"due": due.isoformat() if due else "", "name": r[ix["Vendor"]],
                      "amount": round(_num(r[ix["Amount"]]), 2), "status": status, "source": "Bill"})
    h, rows = _table(wb, "tblRecurring")
    ix = {k: i for i, k in enumerate(h)}
    for r in rows:
        nxt = _date(r[ix["Next Expected"]])
        if not r[ix["Name"]] or not nxt or nxt > horizon:
            continue
        amt = _num(r[ix["Typical Amount"]]) or _num(r[ix["Last Amount"]]) or _num(r[ix["Avg (90 days)"]])
        bills.append({"due": nxt.isoformat(), "name": r[ix["Name"]], "amount": round(amt, 2),
                      "status": r[ix["Status"]] or "",
                      "source": "Recurring (autopay)" if r[ix["Autopay"]] == "Yes" else "Recurring"})
    bills.sort(key=lambda b: b["due"] or "9999")

    # payroll from Checkbook
    h, rows = _table(wb, "tblRegister")
    ix = {k: i for i, k in enumerate(h)}
    month_start = today.replace(day=1)
    prev_start = (month_start - dt.timedelta(days=1)).replace(day=1)
    mtd = prev = 0.0
    items = []  # wages + payroll taxes (Gusto withdrawals and hand-written payroll checks)
    for r in rows:
        cat = r[ix["Category"]]
        if cat not in PAYROLL_CATS:
            continue
        d = _date(r[ix["Date"]])
        if not d:
            continue
        out = -_num(r[ix["Net Amount"]])
        if d >= month_start:
            mtd += out
        elif d >= prev_start:
            prev += out
        if cat == "Payroll Processing Fees":
            continue
        desc = str(r[ix["Description"]] or "").upper()
        kind = "gusto_tax" if "GUSTO/TAX" in desc else "gusto_net" if "GUSTO/NET" in desc else "tax" if cat == "Payroll Taxes" else "check"
        items.append((d, kind, out))
    # Pay period = 14 days starting at the latest regular Gusto run. Regular runs are the big biweekly ones;
    # smaller off-cycle runs (e.g. an employee paid weekly) and hand-written checks inside the period count too.
    last_run = None
    gusto = {}
    for d, kind, out in items:
        if kind in ("gusto_net", "gusto_tax"):
            gusto[d] = gusto.get(d, 0.0) + out
    if gusto:
        latest = max(gusto)
        recent = {d: v for d, v in gusto.items() if (latest - d).days <= 2 * PERIOD_DAYS}
        big = max(recent.values())
        anchor = max(d for d, v in recent.items() if v >= 0.5 * big)  # latest regular (full-size) run
        end = anchor + dt.timedelta(days=PERIOD_DAYS - 1)
        run = [x for x in items if anchor <= x[0] <= end]
        tot = lambda kinds: round(sum(x[2] for x in run if x[1] in kinds), 2)
        last_run = {"start": anchor.isoformat(), "end": end.isoformat(), "date": max(x[0] for x in run).isoformat(),
                    "gusto": tot({"gusto_net", "gusto_tax"}), "gustoRuns": sum(1 for d in gusto if anchor <= d <= end),
                    "checks": tot({"check"}), "checkCount": sum(1 for x in run if x[1] == "check"),
                    "netPay": tot({"gusto_net", "check"}), "taxes": tot({"gusto_tax", "tax"}),
                    "total": round(sum(x[2] for x in run), 2)}

    # ledger for reports: every Checkbook row (date, description, payee, category, net, check #)
    ledger = []
    for r in rows:
        d = _date(r[ix["Date"]])
        if not d:
            continue
        chk = r[ix["Check #"]] if "Check #" in ix else None
        ledger.append([d.isoformat(), str(r[ix["Description"]] or ""), str(r[ix["Payee"]] or ""),
                       str(r[ix["Category"]] or "Uncategorized"), round(_num(r[ix["Net Amount"]]), 2),
                       str(int(chk)) if isinstance(chk, (int, float)) and chk else str(chk or "")])
    ledger.sort(key=lambda x: x[0])
    h, rows = _table(wb, "tblCategories")
    groups = {}
    if h:
        ci = {k: i for i, k in enumerate(h)}
        cat_col = 0
        grp_col = ci.get("P&L Group")
        for r in rows:
            if r[cat_col] and grp_col is not None:
                groups[str(r[cat_col])] = str(r[grp_col] or "Needs Category")
    cash = []
    h, rows = _table(wb, "CashRegister")
    if h and "Balance" in h:
        ci = {k: i for i, k in enumerate(h)}
        for r in rows:
            d = _date(r[ci["Date"]])
            if d and r[ci["Balance"]] is not None:
                cash.append([d.isoformat(), round(_num(r[ci["Balance"]]), 2)])
    h, rows = _table(wb, "tblBills")
    open_bills = 0.0
    if h:
        bi = {k: i for i, k in enumerate(h)}
        open_bills = round(sum(_num(r[bi["Amount"]]) for r in rows if (r[bi["Status"]] or "") not in ("", "Paid")), 2)

    return {
        "ledger": ledger, "groups": groups, "cash": cash, "openBills": open_bills,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "bankSyncedAt": str(a.get("last_synced_at") or ""),
        "bankSource": "Workbook",
        "bankAccountId": str(a.get("account_id") or ""),
        "bankMask": str(a.get("mask") or ""),
        "currentBalance": round(_num(a.get("current_balance")), 2),
        "availableBalance": round(_num(a.get("available_balance")), 2),
        "pending": pending,
        "pendingTotal": round(sum(p["amount"] for p in pending), 2),
        "billsDue": bills,
        "billsDueTotal": round(sum(b["amount"] for b in bills), 2),
        "payroll": {"lastRun": last_run, "monthToDate": round(mtd, 2), "previousMonth": round(prev, 2)},
        "source": "workbook",
    }
