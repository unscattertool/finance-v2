"""Card money watch (Heartland): catch card sales that never reach the bank.

A batch that never closes in the terminal never shows up on a batch report, so nothing "expects" its deposit.
This works from the other side — what was sold — and raises an alert for:

  * no_report   a day the restaurant was open (closeouts in Shifts, shifts on the schedule, or the usual open weekday)
                with no Heartland report imported: card sales that day are invisible until it is imported
  * unbatched   a day whose card payments (Transactions / Daily Summary report) are more than its closed batches
                (Batch report): part of the day may still be in an open batch in the terminal
  * no_deposit  a day's card sales + tips not in the bank after LATE_DAYS days (from tips.pending_batches)

Days can be marked "closed" (no sales that day) or "checked" so they stop alerting.
"""
import datetime as dt
import json
import sqlite3

HEARTLAND_FROM = "2026-10-29"   # first business day on Heartland
LATE_DAYS = 3                   # Heartland deposits the next business day or two
TOL = 0.02


def _d(s):
    return dt.date.fromisoformat(s)


def _dismissed(db, client):
    r = db.execute("SELECT v FROM meta WHERE client=? AND k='cardwatch_ok'", (client,)).fetchone()
    try:
        return json.loads(r[0]) if r and r[0] else {}
    except ValueError:
        return {}


def dismiss(db, client, day, why, user):
    ok = _dismissed(db, client)
    ok[day] = dict(why=why, by=user, at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
    db.execute("INSERT INTO meta(client,k,v) VALUES(?,?,?) ON CONFLICT(client,k) DO UPDATE SET v=excluded.v",
               (client, "cardwatch_ok", json.dumps(ok)))
    db.commit()


def undismiss(db, client, day):
    ok = _dismissed(db, client)
    ok.pop(day, None)
    db.execute("INSERT INTO meta(client,k,v) VALUES(?,?,?) ON CONFLICT(client,k) DO UPDATE SET v=excluded.v",
               (client, "cardwatch_ok", json.dumps(ok)))
    db.commit()


def _shifts_days(path, a, b):
    """{date: (closeouts, scheduled shifts)} from Shifts (read-only)."""
    out = {}
    if not path:
        return out
    try:
        s = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        for d, n in s.execute("SELECT shift_date, count(*) FROM shifts WHERE shift_date BETWEEN ? AND ? GROUP BY 1", (a, b)):
            out.setdefault(d, [0, 0])[0] = n
        cols = [c[1] for c in s.execute("PRAGMA table_info(schedule_shifts)")]
        dc = next((c for c in ("shift_date", "date", "day") if c in cols), None) or next((c for c in cols if "date" in c), None)
        if dc:
            for d, n in s.execute("SELECT substr(%s,1,10), count(*) FROM schedule_shifts WHERE substr(%s,1,10) BETWEEN ? AND ? GROUP BY 1" % (dc, dc), (a, b)):
                out.setdefault(d, [0, 0])[1] = n
        s.close()
    except sqlite3.Error:
        pass
    return out


def check(db, client, shifts_path=None, today=None, since=HEARTLAND_FROM, days_back=60):
    """{'alerts': [...], 'days': per-day detail}. Alerts are for days up to yesterday (today's batch hasn't closed yet)."""
    from .tips import pending_batches
    today = today or dt.date.today()
    start = max(_d(since), today - dt.timedelta(days=days_back))
    end = today - dt.timedelta(days=1)
    if end < start:
        return dict(alerts=[], days=[])
    a, b = start.isoformat(), end.isoformat()
    q = lambda sql: db.execute(sql, (client, a, b)).fetchall()
    pay = {r[0]: (r[1] or 0.0) for r in q("""SELECT business_date, sum(CASE WHEN tender='card' THEN (amount + tip) *
                                                 (CASE WHEN lower(status) LIKE '%refund%' OR lower(status) LIKE '%return%' THEN -1 ELSE 1 END) ELSE 0 END)
                                          FROM pos_payment WHERE client=? AND business_date BETWEEN ? AND ? AND lower(status) NOT LIKE '%void%'
                                          GROUP BY 1""")}
    daily = {r[0]: (r[1] or 0.0) for r in q("""SELECT business_date, sum(coalesce(card_amount,0) + coalesce(card_tips,0)) FROM pos_daily
                                            WHERE client=? AND business_date BETWEEN ? AND ? GROUP BY 1""")}
    batch = {r[0]: (r[1] or 0.0, r[2]) for r in q("""SELECT business_date, sum(CASE WHEN lower(coalesce(status,''))!='open' THEN amount ELSE 0 END),
                                                   sum(lower(coalesce(status,''))='open') FROM pos_batch
                                                   WHERE client=? AND business_date BETWEEN ? AND ? GROUP BY 1""")}
    reported = set(pay) | set(daily) | set(batch)
    sh = _shifts_days(shifts_path, a, b)
    # usual open weekdays: weekdays with a report on at least half of the last 6 weeks' same weekday
    open_wd = {}
    for d in reported:
        open_wd[_d(d).weekday()] = open_wd.get(_d(d).weekday(), 0) + 1
    weeks = max(1, min(6, ((end - start).days + 1) // 7))
    usual = {wd for wd, n in open_wd.items() if n >= max(2, weeks // 2)}
    ok = _dismissed(db, client)
    alerts, days = [], []
    d = start
    while d <= end:
        k = d.isoformat()
        clos, sched = sh.get(k, (0, 0))
        card = pay.get(k, daily.get(k))
        bt = batch.get(k)
        row = dict(date=k, dow=d.strftime("%a"), reported=k in reported, card=None if card is None else round(card, 2),
                   batch=None if not bt else round(bt[0], 2), open_batches=bt[1] if bt else 0, closeouts=clos, scheduled=sched,
                   usual=d.weekday() in usual, ok=ok.get(k))
        days.append(row)
        if not ok.get(k):
            if k not in reported and (clos or sched or d.weekday() in usual):
                why = ("%d closeout%s in Shifts" % (clos, "" if clos == 1 else "s")) if clos else \
                      ("%d shift%s on the schedule" % (sched, "" if sched == 1 else "s")) if sched else "usually open on %ss" % d.strftime("%A")
                alerts.append(dict(kind="no_report", date=k, amount=None,
                                   text="No Heartland report imported for %s (%s). Card sales that day can't be checked against the bank — import the day's reports, or mark the day closed."
                                        % (d.strftime("%a %b %-d"), why)))
            elif bt and card is not None and card - bt[0] > TOL:
                alerts.append(dict(kind="unbatched", date=k, amount=round(card - bt[0], 2),
                                   text="%s: card payments %s but closed batches only %s — %s may still be in an open batch. Check the terminal and settle it."
                                        % (d.strftime("%a %b %-d"), _m(card), _m(bt[0]), _m(card - bt[0]))))
            elif bt and bt[1]:
                alerts.append(dict(kind="unbatched", date=k, amount=None,
                                   text="%s: the Batch report shows a batch still open. Settle it in the terminal." % d.strftime("%a %b %-d")))
        d += dt.timedelta(days=1)
    pend = pending_batches(db, client, days_back=days_back, today=today)
    for p in pend["days"]:
        if (today - _d(p["date"])).days > LATE_DAYS and not ok.get(p["date"]) and p["date"] >= since:
            alerts.append(dict(kind="no_deposit", date=p["date"], amount=p["total"],
                               text="%s: %s in card sales + tips is not in the bank after %d days. Call Heartland about the deposit."
                                    % (_d(p["date"]).strftime("%a %b %-d"), _m(p["total"]), (today - _d(p["date"])).days)))
    order = {"no_deposit": 0, "unbatched": 1, "no_report": 2}
    alerts.sort(key=lambda x: (order[x["kind"]], x["date"]))
    return dict(alerts=alerts, days=list(reversed(days)), since=since, start=a, end=b)


def _m(v):
    return "${:,.2f}".format(v)
