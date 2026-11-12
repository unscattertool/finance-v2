"""Which Shifts closeouts are missing, for whom: Linga sales per server per day (Sale Summary Report "Close By",
or card payments from the Batch Report) against the Shifts closeouts per staff member per day.
People are joined through the Name matching table (Linga / Shifts name → Gusto employee), else by first name."""
import collections
import sqlite3

from . import tipcheck as TC

TOLERANCE = 1.00  # dollars
BAR_KEY = "Bar register (shared login)"


def _key(m, source, name, shift_type=None):
    """Who a Linga server / Shifts closeout belongs to. Linga logins mapped to Owner or Bar are the shared bar
    register (several bartenders ring under the owner's name), and so are Shifts bar closeouts."""
    e = m.get((source, name))
    if e in (TC.BAR, TC.OWNER) or shift_type == "bar":
        return BAR_KEY
    if e and e != TC.NONE:
        return e
    return "~" + TC._first(name)


def check(db, client, shifts_path, since=None, paper_until=TC.PAPER_UNTIL):
    from . import salesum
    salesum.ensure(db)
    TC.ensure(db)
    m, _ = TC.mapping(db, client, shifts_path)
    ignore = {n for (src, n), e in m.items() if src == "batch" and e == TC.NONE}
    # bartenders on each day's Bar shift: their own-login sales belong with that Bar shift (the shared bar register key),
    # so they aren't "missing" when the Bar closeout carries them (e.g. Kristi 10/2, Dani 11/7)
    bar_members = collections.defaultdict(set)
    st_team = {}
    if shifts_path:
        try:
            s1 = sqlite3.connect("file:%s?mode=ro" % shifts_path, uri=True, timeout=5)
            st_team = {n: t for n, t in s1.execute("SELECT name, team_group FROM staff")}
            s1.close()
        except sqlite3.Error:
            pass
    if shifts_path:
        try:
            s0 = sqlite3.connect("file:%s?mode=ro" % shifts_path, uri=True, timeout=5)
            for d0, n0 in s0.execute("""SELECT sh.shift_date, st.name FROM shifts sh JOIN shift_members sm ON sm.shift_id = sh.id
                                        JOIN staff st ON st.id = sm.staff_id WHERE sh.shift_type = 'bar'
                                        UNION SELECT sh.shift_date, st.name FROM shifts sh JOIN staff st ON st.id = sh.staff_id
                                        WHERE sh.shift_type = 'bar'"""):
                bar_members[d0].add(TC._first(n0))  # anyone on that day's Bar shift (bartender or a manager covering the bar)
            for d0, n0, k0 in s0.execute("""SELECT sh.shift_date, st.name, sh.shift_type FROM shifts sh JOIN staff st ON st.id = sh.staff_id"""):
                if _key(m, "shifts", n0, k0) == BAR_KEY:  # a closeout that counts as the bar (e.g. the solo bartender's)
                    bar_members[d0].add(TC._first(n0))
            s0.close()
        except sqlite3.Error:
            pass

    def lkey(date, server):
        k = _key(m, "batch", server)
        return BAR_KEY if k != BAR_KEY and TC._first(server) in bar_members.get(date, ()) else k
    # Linga side
    linga = collections.defaultdict(lambda: dict(gross=0.0, checks=0, card=0.0, tips=0.0, names=set()))
    have_sales = False
    from . import posimport
    posimport.ensure(db)
    for r in db.execute("SELECT date, server, gross, checks FROM sales_by_server_all WHERE client=?", (client,)):
        if r["server"] in ignore:
            continue
        have_sales = True
        x = linga[(r["date"], lkey(r["date"], r["server"]))]
        x["gross"] += r["gross"] or 0; x["checks"] += r["checks"] or 0; x["names"].add(r["server"])
    for r in db.execute("""SELECT business_date, server, amount, tips FROM card_batch_server WHERE client=?
                           UNION ALL
                           SELECT business_date, employee, sum(card), sum(tips)
                           FROM pos_server_day p WHERE client=? AND NOT EXISTS (SELECT 1 FROM card_batch_server c
                             WHERE c.client=p.client AND c.business_date=p.business_date)
                           GROUP BY business_date, employee""", (client, client)):
        if r["server"] in ignore:
            continue
        x = linga[(r["business_date"], lkey(r["business_date"], r["server"]))]
        x["card"] += r["amount"] or 0; x["tips"] += r["tips"] or 0; x["names"].add(r["server"])
    # Shifts side
    shifts = collections.defaultdict(lambda: dict(gross=0.0, tips=0.0, rows=0, names=set()))
    first = None
    if shifts_path:
        try:
            s = sqlite3.connect("file:%s?mode=ro" % shifts_path, uri=True, timeout=5)
            s.row_factory = sqlite3.Row
            for r in s.execute("""SELECT sh.shift_date d, st.name n, sh.gross_sales_cents g, sh.cc_tips_cents t, sh.shift_type k
                                  FROM shifts sh LEFT JOIN staff st ON st.id = sh.staff_id"""):
                x = shifts[(r["d"], _key(m, "shifts", r["n"] or "?", r["k"]))]
                x["gross"] += (r["g"] or 0) / 100; x["tips"] += (r["t"] or 0) / 100; x["rows"] += 1; x["names"].add(r["n"] or "?")
                first = r["d"] if first is None or r["d"] < first else first
            s.close()
        except sqlite3.Error:
            pass
    import datetime as dt
    after_paper = (dt.date.fromisoformat(paper_until) + dt.timedelta(days=1)).isoformat() if paper_until else "0000"
    start = max(since or "0000", first or "9999", after_paper)
    clock = collections.defaultdict(float)
    try:
        for r in db.execute("SELECT date, name, hours FROM attendance_all WHERE client=? AND date>=? AND flag=''", (client, start)):
            clock[(r["date"], _key(m, "batch", r["name"]))] += r["hours"] or 0
    except sqlite3.Error:
        pass
    # compare
    people = collections.defaultdict(lambda: dict(missing=0, missing_sales=0.0, missing_tips=0.0, different=0, diff=0.0, dates=[]))
    days = collections.defaultdict(list)
    for k in set(linga) | set(shifts):
        d, who = k
        if d < start:
            continue
        L, S = linga.get(k), shifts.get(k)
        lg = (L["gross"] if have_sales else L["card"]) if L else 0.0
        sg = S["gross"] if S else 0.0
        if lg < 0.005 and sg < 0.005:
            continue
        name = sorted((L or {}).get("names") or (S or {}).get("names") or {who.lstrip("~")})[0]
        if who.startswith("~"):
            name = name
        else:
            name = who
        if lg >= 0.005 and sg < 0.005:
            status = "missing"
        elif sg >= 0.005 and lg < 0.005:
            status = "shifts_only"
        elif have_sales and abs(lg - sg) > TOLERANCE:
            status = "different"
        else:
            status = "ok"
        row = dict(date=d, name=name, linga=round(lg, 2), checks=(L or {}).get("checks", 0), tips=round((L or {}).get("tips", 0.0), 2),
                   shifts=round(sg, 2), shifts_tips=round((S or {}).get("tips", 0.0), 2), status=status,
                   linga_names=", ".join(sorted((L or {}).get("names", []))), shifts_names=", ".join(sorted((S or {}).get("names", []))),
                   worked=bool(S and S["rows"]), clock=round(clock.get(k, 0.0), 2))
        days[d].append(row)
        p = people[name]
        if status == "missing":
            p["missing"] += 1; p["missing_sales"] += lg; p["missing_tips"] += row["tips"]; p["dates"].append(d)
        elif status == "different":
            p["different"] += 1; p["diff"] += lg - sg
    out_days = []
    for d in sorted(days, reverse=True):
        rows = sorted(days[d], key=lambda r: (r["status"] == "ok", r["name"]))
        bad = [r for r in rows if r["status"] != "ok"]
        out_days.append(dict(date=d, rows=rows, bad=len(bad), linga=round(sum(r["linga"] for r in rows), 2),
                             shifts=round(sum(r["shifts"] for r in rows), 2)))
    out_people = sorted(({**v, "name": k, "missing_sales": round(v["missing_sales"], 2), "missing_tips": round(v["missing_tips"], 2),
                          "diff": round(v["diff"], 2), "dates": sorted(v["dates"], reverse=True)}
                         for k, v in people.items() if v["missing"] or v["different"]), key=lambda x: -x["missing_sales"])
    return dict(start=start, paper_until=paper_until, have_sales=have_sales, people=out_people, days=out_days,
                missing_days=sum(1 for d in out_days if d["bad"]),
                missing_total=round(sum(p["missing_sales"] for p in out_people), 2))
