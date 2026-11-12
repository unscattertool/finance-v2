"""Card tips: Linga batches vs Shifts vs Gusto, and which employees are still owed.

Per pay period:
  - Linga batch report  = card tips customers left (by the server who ran the card)   ← the money that came in
  - Shifts closeouts    = card tips recorded by staff at closeout (+ tip-outs between staff)
  - Gusto journal       = Paycheck Tips (+ Cash Tips) actually paid
They should all agree. Per employee, tips earned = batch tips on cards they ran − tip-outs they gave
+ tip-outs and tip credits they received (Shifts). Owed = earned − paid by Gusto.

Names differ between systems (e.g. "Tammy Henson" in Linga, "Tammy H" in Shifts), so every name is mapped
to a Gusto employee; the owner can correct the mapping. A register whose tips belong to the bar (e.g. the
owner's) can be mapped to "Bar", which splits that day's tips evenly among the bartenders on shift in Shifts.
"""
import collections
import datetime as dt
import json
import re
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS card_batch_server (
  client TEXT NOT NULL, business_date TEXT NOT NULL, server TEXT NOT NULL, amount REAL, tips REAL, payments INTEGER,
  PRIMARY KEY (client, business_date, server));
CREATE TABLE IF NOT EXISTS tip_name_map (
  client TEXT NOT NULL, source TEXT NOT NULL, name TEXT NOT NULL, employee TEXT, auto INTEGER DEFAULT 1,
  updated_by TEXT, updated_at TEXT, PRIMARY KEY (client, source, name));
CREATE TABLE IF NOT EXISTS tip_extra_employee (
  client TEXT NOT NULL, name TEXT NOT NULL, added_by TEXT, added_at TEXT, PRIMARY KEY (client, name));
"""
BAR, NONE, OWNER = "__bar__", "__none__", "__owner__"
# Until this date tips were tracked on paper (gross sales, tip-outs, recipients, net tips), totalled by hand and
# entered in Gusto, so Gusto is the record for those pay periods: nobody is "owed" from Linga/Shifts there.
PAPER_UNTIL = "2026-08-31"
# Until this date the owner's Linga login was the bar register and the bar had one bartender, who gets its tips.
SOLO_BAR = {"until": "2026-09-30", "bartender": "Dani Woodland"}
# Managers ring catering / event tickets under their own login; working as manager (not the sole bartender) they don't
# keep the tip: it is split among the people who worked the event, picked individually on a Shifts event closeout.
MANAGERS = ["Veronica Berkowitz"]
# From the Heartland switch (10/29) managers keep their own card tips, and the owner's (Mandy's) login is her own, not
# the bar register (owner 11/8). The manager/event-crew rule above applies only before this date.
KEEP_FROM = "2026-10-29"
SHIFTS_TOLERANCE = 25.0  # a period counts as fully recorded in Shifts when it is within this of the Linga batches
ALIASES = {"jessi": "jessica", "kristy": "kristi", "stephen": "steven", "jd": "jonathan", "j.d.": "jonathan", "zoey": "zoe", "dane": "dane"}


def ensure(db):
    db.executescript(SCHEMA)


def _first(name):
    f = re.sub(r"[^a-z.]", "", (name or "").strip().split(" ")[0].lower())
    return ALIASES.get(f, f)


def _last(name):
    parts = (name or "").strip().split()
    return re.sub(r"[^a-z]", "", parts[-1].lower()) if len(parts) > 1 else ""


# ------------------------------------------------------------------ inputs
def save_batch_servers(db, client, per_day_server):
    """per_day_server: {(business_date, server): [amount, tips, n]} from the batch report import."""
    ensure(db)
    dates = {d for d, _ in per_day_server}
    for d in dates:
        db.execute("DELETE FROM card_batch_server WHERE client=? AND business_date=?", (client, d))
    for (d, s), (amt, tips, n) in per_day_server.items():
        db.execute("INSERT INTO card_batch_server VALUES(?,?,?,?,?,?)", (client, d, s, round(amt, 2), round(tips, 2), n))
    db.commit()


def _shifts(path, a, b):
    """Shifts data for a..b: per-shift owner card tips, tip-outs, credits, bartenders per day."""
    out = dict(owner_tips=collections.defaultdict(float), outs_given=collections.defaultdict(float),
               outs_recv=collections.defaultdict(float), credit_in=collections.defaultdict(float),
               credit_out=collections.defaultdict(float), credit_unsourced=0.0, bar_staff=collections.defaultdict(set),
               day_tips=collections.defaultdict(float), names=set(), moves=[], events=[])
    if not path:
        return out
    try:
        s = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
        s.row_factory = sqlite3.Row
        staff = {r["id"]: r for r in s.execute("SELECT id, name, team_group FROM staff")}
        shifts = {r["id"]: dict(r) for r in s.execute(
            "SELECT id, staff_id, shift_date, shift_type, cc_tips_cents FROM shifts WHERE shift_date BETWEEN ? AND ?", (a, b))}
        members = collections.defaultdict(list)
        for r in s.execute("SELECT shift_id, staff_id FROM shift_members"):
            members[r["shift_id"]].append(r["staff_id"])
        outs = [dict(r) for r in s.execute("SELECT shift_id, recipient, amount_cents FROM tip_outs")]
        try:
            credits = [dict(r) for r in s.execute(
                "SELECT staff_id, tip_date, amount_cents, source_shift_id FROM payroll_tip_credits WHERE tip_date BETWEEN ? AND ?", (a, b))]
        except sqlite3.Error:
            credits = []
        s.close()
    except sqlite3.Error:
        return out
    nm = lambda sid: staff[sid]["name"] if sid in staff else sid
    for sh in shifts.values():
        who = nm(sh["staff_id"])
        out["names"].add(who)
        out["owner_tips"][who] += (sh["cc_tips_cents"] or 0) / 100
        out["day_tips"][sh["shift_date"]] += (sh["cc_tips_cents"] or 0) / 100
        if sh["shift_type"] == "bar":
            for m in members.get(sh["id"], []) or [sh["staff_id"]]:
                if m in staff and staff[m]["team_group"] == "bar":
                    out["bar_staff"][sh["shift_date"]].add(nm(m))
    for o in outs:
        sh = shifts.get(o["shift_id"])
        if not sh:
            continue
        out["outs_given"][nm(sh["staff_id"])] += (o["amount_cents"] or 0) / 100
        out["outs_recv"][o["recipient"]] += (o["amount_cents"] or 0) / 100
        out["names"].add(o["recipient"])
        out["moves"].append((sh["shift_date"], nm(sh["staff_id"]), o["recipient"], (o["amount_cents"] or 0) / 100, "tip-out"))
    for c in credits:
        amt = (c["amount_cents"] or 0) / 100
        out["credit_in"][nm(c["staff_id"])] += amt
        out["names"].add(nm(c["staff_id"]))
        src = shifts.get(c["source_shift_id"]) if c["source_shift_id"] else None
        if src:
            out["credit_out"][nm(src["staff_id"])] += amt
        else:
            out["credit_unsourced"] += amt
        out["moves"].append((c["tip_date"], nm(src["staff_id"]) if src else None, nm(c["staff_id"]), amt, "credit"))
    # event closeouts: the card tips belong to everyone who worked the event, split evenly unless the closeout
    # already records tip-outs (Linga has them under the one login that rang the event, e.g. Veronica's)
    with_outs = {o["shift_id"] for o in outs}
    for sh in shifts.values():
        if sh["shift_type"] == "event" and sh["id"] not in with_outs and (sh["cc_tips_cents"] or 0) > 0:
            crew = members.get(sh["id"]) or []
            if crew:
                for m in crew:
                    out["names"].add(nm(m))
                out["events"].append((sh["shift_date"], nm(sh["staff_id"]), [nm(m) for m in crew], (sh["cc_tips_cents"] or 0) / 100))
    return out


def _gusto_employees(db, client):
    names = set()
    for r in db.execute("SELECT employees FROM gusto_run WHERE client=?", (client,)):
        for e in json.loads(r["employees"] or "[]"):
            names.add(e["name"])
    return sorted(names)


# ------------------------------------------------------------------ name mapping
def _guess(name, employees):
    f, l = _first(name), _last(name)
    exact = [e for e in employees if e.lower() == (name or "").lower()]
    if exact:
        return exact[0]
    both = [e for e in employees if _first(e) == f and l and _last(e) == l]
    if len(both) == 1:
        return both[0]
    firsts = [e for e in employees if _first(e) == f]
    if len(firsts) == 1:
        return firsts[0]
    initial = [e for e in firsts if l and _last(e).startswith(l[:1])]
    return initial[0] if len(initial) == 1 else None


def extra_employees(db, client):
    """Employees added by hand because they aren't in the imported Gusto journal yet (new hires, unpaid)."""
    return [r[0] for r in db.execute("SELECT name FROM tip_extra_employee WHERE client=? ORDER BY name", (client,))]


def add_extra(db, client, name, user):
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not name or len(name) > 80:
        raise ValueError("Enter the employee's name (as it will appear in Gusto).")
    if name.lower() in {e.lower() for e in _gusto_employees(db, client)}:
        raise ValueError("%s is already in the Gusto journal." % name)
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    db.execute("INSERT OR IGNORE INTO tip_extra_employee VALUES(?,?,?,?)", (client, name, user, ts))
    # unmatched names that were guessed automatically get another try with the new employee
    for r in db.execute("SELECT source, name FROM tip_name_map WHERE client=? AND auto=1 AND employee IS NULL", (client,)).fetchall():
        g = _guess(r["name"], [name])
        if g:
            db.execute("UPDATE tip_name_map SET employee=?, updated_at=? WHERE client=? AND source=? AND name=?",
                       (g, ts, client, r["source"], r["name"]))
    db.commit()
    return name


def remove_extra(db, client, name):
    db.execute("DELETE FROM tip_extra_employee WHERE client=? AND name=?", (client, name))
    db.execute("UPDATE tip_name_map SET employee=NULL WHERE client=? AND employee=?", (client, name))
    db.commit()


def mapping(db, client, shifts_path=None):
    """{(source, name): employee} for every name seen, guessing and saving new ones."""
    ensure(db)
    employees = sorted(set(_gusto_employees(db, client)) | set(extra_employees(db, client)))
    have = {(r["source"], r["name"]): r["employee"] for r in db.execute("SELECT * FROM tip_name_map WHERE client=?", (client,))}
    seen = [("batch", r[0]) for r in db.execute("SELECT DISTINCT server FROM card_batch_server WHERE client=?", (client,))]
    for q in ("SELECT DISTINCT name FROM attendance_all WHERE client=?", "SELECT DISTINCT server FROM sales_by_server_all WHERE client=?"):
        try:  # every Linga login, also those who only clocked in or rang cash sales
            seen += [("batch", r[0]) for r in db.execute(q, (client,)) if r[0] and ("batch", r[0]) not in seen]
        except sqlite3.Error:
            pass
    sh = _shifts(shifts_path, "2000-01-01", "2100-01-01") if shifts_path else None
    if sh:
        seen += [("shifts", n) for n in sorted(sh["names"])]
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for key in seen:
        if key not in have:
            g = _guess(key[1], employees)
            have[key] = g
            db.execute("INSERT OR IGNORE INTO tip_name_map(client,source,name,employee,auto,updated_at) VALUES(?,?,?,?,1,?)",
                       (client, key[0], key[1], g, ts))
    db.commit()
    return have, employees


def set_mapping(db, client, source, name, employee, user):
    db.execute("""INSERT INTO tip_name_map(client,source,name,employee,auto,updated_by,updated_at) VALUES(?,?,?,?,0,?,?)
                  ON CONFLICT(client,source,name) DO UPDATE SET employee=excluded.employee, auto=0,
                  updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
               (client, source, name, employee or None, user, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
    db.commit()


# ------------------------------------------------------------------ the check
def _periods(db, client, since):
    """Two-week pay periods from the Gusto journal, plus the one after the last imported."""
    per = sorted({(r["period_start"], r["period_end"]) for r in db.execute(
        "SELECT period_start, period_end FROM gusto_run WHERE client=? AND period_start IS NOT NULL", (client,))
        if (dt.date.fromisoformat(r["period_end"]) - dt.date.fromisoformat(r["period_start"])).days == 13
        and r["period_end"] >= since})
    return per


def _paper_gaps(periods):
    g = collections.defaultdict(float)
    for p in periods:
        if p["paper"]:
            for e in p["employees"]:
                g[e["name"]] += e["gap"]
    return g


def check(db, client, shifts_path, since="2026-04-27", paper_until=PAPER_UNTIL, solo_bar=SOLO_BAR, managers=None, extra_periods=()):
    managers = set(MANAGERS if managers is None else managers)
    from . import gusto as G
    ensure(db)
    G.ensure(db)
    if not G.has_runs(db, client):
        return None
    m, employees = mapping(db, client, shifts_path)
    emp = lambda source, name: m.get((source, name))
    days_batch = {r["date"]: r["card_tips"] or 0 for r in db.execute(
        "SELECT date, card_tips FROM sales_day WHERE client=? AND source IN ('batch','processor','pos')", (client,))}
    from . import posimport
    posimport.ensure(db)
    has_servers = db.execute("SELECT 1 FROM card_batch_server WHERE client=? UNION ALL SELECT 1 FROM pos_server_day WHERE client=? LIMIT 1",
                             (client, client)).fetchone() is not None
    periods, owed_total, owed_sure = [], collections.defaultdict(float), collections.defaultdict(float)
    last_batch = max(days_batch) if days_batch else None
    for a, b in sorted(set(_periods(db, client, since)) | {tuple(x) for x in extra_periods}):
        sh = _shifts(shifts_path, a, b)
        # what came in (batches) and what Shifts recorded, by day
        days = []
        d = dt.date.fromisoformat(a)
        while d.isoformat() <= b:
            k = d.isoformat()
            bt, st = round(days_batch.get(k, 0), 2), round(sh["day_tips"].get(k, 0), 2)
            if bt or st:
                days.append(dict(date=k, batch=bt, shifts=st, diff=round(bt - st, 2)))
            d += dt.timedelta(days=1)
        # paid (Gusto runs for this period, regular and off-cycle)
        paid = collections.defaultdict(float)
        for r in db.execute("SELECT employees FROM gusto_run WHERE client=? AND period_start=? AND period_end=?", (client, a, b)):
            for e in json.loads(r["employees"] or "[]"):
                paid[e["name"]] += (e.get("tips") or 0) + (e.get("cash_tips") or 0)
        # earned per employee
        earned = collections.defaultdict(float)
        unassigned = collections.defaultdict(float)
        owner = collections.defaultdict(float)  # tips the owner keeps (not payroll)
        bar_register = collections.defaultdict(float)  # owner-login tips given to the solo bartender
        mgr_pool = collections.defaultdict(float)  # (date, manager) → tips on a manager's tickets, for the event crew
        if has_servers:
            for r in db.execute("""SELECT business_date, server, tips FROM card_batch_server WHERE client=? AND business_date BETWEEN ? AND ?
                                   UNION ALL
                                   SELECT business_date, employee, sum(tips) FROM pos_server_day p WHERE client=? AND business_date BETWEEN ? AND ?
                                     AND tips != 0 AND NOT EXISTS (SELECT 1 FROM card_batch_server c WHERE c.client=p.client
                                     AND c.business_date=p.business_date) GROUP BY business_date, employee""",
                                (client, a, b, client, a, b)):
                who = emp("batch", r["server"])
                if who == BAR:
                    bar = [emp("shifts", n) for n in sorted(sh["bar_staff"].get(r["business_date"], []))]
                    bar = [x for x in bar if x and x not in (BAR, NONE)]
                    if bar:
                        for x in bar:
                            earned[x] += r["tips"] / len(bar)
                    else:
                        unassigned["Bar register, no bartender in Shifts on %s" % r["business_date"]] += r["tips"]
                elif who == OWNER and solo_bar and r["business_date"] <= solo_bar["until"]:
                    earned[solo_bar["bartender"]] += r["tips"]
                    bar_register[r["business_date"]] += r["tips"]
                elif who == OWNER:
                    owner[r["server"]] += r["tips"]
                elif who in managers and r["business_date"] < KEEP_FROM:
                    mgr_pool[(r["business_date"], who)] += r["tips"]
                elif who and who != NONE:
                    earned[who] += r["tips"]
                elif who != NONE:
                    unassigned[r["server"]] += r["tips"]
        adj = collections.defaultdict(float)
        events = 0.0

        def move(n, v):
            who = emp("shifts", n)
            if who == OWNER:
                owner[n] += v
            elif who and who not in (BAR, NONE):
                adj[who] += v
            else:
                unassigned["Shifts: %s" % n] += v

        for date, giver, recv, amt, kind in sh["moves"]:
            if giver and emp("shifts", giver) == OWNER and solo_bar and date <= solo_bar["until"]:
                continue  # the whole bar register already went to the bartender above
            if kind == "event":
                events += amt
            if giver:
                move(giver, -amt)
            move(recv, amt)
        # event closeouts: the tip goes to the crew picked on the closeout; managers on the crew don't share unless they
        # were the only ones working it
        for date, owner_name, crew, amt in sh["events"]:
            if not (a <= date <= b):
                continue
            giver = emp("shifts", owner_name)
            crew_e = [(n, emp("shifts", n)) for n in crew]
            share_with = [(n, e) for n, e in crew_e if e not in managers and e != OWNER] or crew_e
            if giver in managers and date < KEEP_FROM:
                mgr_pool[(date, giver)] -= amt
            elif giver == OWNER and solo_bar and date <= solo_bar["until"]:
                earned[solo_bar["bartender"]] -= amt
            else:
                move(owner_name, -amt)
            for n, e in share_with:
                move(n, amt / len(share_with))
            events += amt
        event_left = {}
        for (date, mgr), v in sorted(mgr_pool.items()):
            if v > 0.005:
                unassigned["Catering/event tips on %s's tickets %s — add a Shifts event closeout with who worked" % (mgr, date)] += v
                event_left[date] = round(event_left.get(date, 0) + v, 2)
        tb, ts_, tp = round(sum(x["batch"] for x in days), 2), round(sum(x["shifts"] for x in days), 2), round(sum(paid.values()), 2)
        complete = bool(last_batch and last_batch >= b)
        paper = bool(paper_until and b <= paper_until)
        shifts_ok = not paper and complete and abs(tb - ts_) <= SHIFTS_TOLERANCE  # Shifts has (nearly) every closeout and tip-out
        rows = []
        for who in sorted(set(earned) | set(adj) | set(paid)):
            e = round(earned.get(who, 0) + adj.get(who, 0), 2)
            p = round(paid.get(who, 0), 2)
            if abs(e) < 0.005 and abs(p) < 0.005:
                continue
            rows.append(dict(name=who, batch=round(earned.get(who, 0), 2), adj=round(adj.get(who, 0), 2), earned=e, paid=p,
                             owed=0.0 if paper else round(e - p, 2), gap=round(e - p, 2)))
            if paper:  # paper records went into Gusto: what Gusto paid is what they earned
                continue
            owed_total[who] += e - p
            if shifts_ok:
                owed_sure[who] += e - p
        owner_kept = round(sum(owner.values()), 2)
        periods.append(dict(start=a, end=b, paper=paper, bar_register=round(sum(bar_register.values()), 2), events=round(events, 2), event_left=event_left, shifts_ok=shifts_ok, owner_kept=owner_kept, payroll_due=round(tb - owner_kept, 2), batch=tb, shifts=ts_, gusto=tp, batch_vs_shifts=round(tb - ts_, 2),
                            batch_vs_gusto=round(tb - owner_kept - tp, 2), days=days, employees=rows, complete=complete,
                            unassigned={k: round(v, 2) for k, v in unassigned.items() if abs(v) >= 0.005},
                            unsourced_credits=round(sh["credit_unsourced"], 2),
                            match=complete and abs(tb - ts_) < 0.01 and abs(tb - owner_kept - tp) < 0.01))
    owed = sorted(({"name": k, "owed": round(v, 2), "sure": round(owed_sure.get(k, 0), 2)} for k, v in owed_total.items()
                   if abs(v) >= 0.01 or abs(owed_sure.get(k, 0)) >= 0.01), key=lambda x: (-x["sure"], -x["owed"]))
    names = [dict(source=s, name=n, employee=e) for (s, n), e in sorted(m.items())]
    return dict(periods=list(reversed(periods)), owed=[x for x in owed if x["sure"] > 0 or (x["owed"] > 0 and x["sure"] >= 0)],
                overpaid=[x for x in owed if x["owed"] < 0 and x["sure"] <= 0],
                owed_total=round(sum(x["sure"] for x in owed if x["sure"] > 0), 2),
                owed_total_all=round(sum(x["owed"] for x in owed if x["owed"] > 0), 2),
                sure_periods=[(p["start"], p["end"]) for p in periods if p["shifts_ok"]], names=names, employees=employees,
                extra=extra_employees(db, client), paper_until=paper_until, solo_bar=solo_bar, managers=sorted(managers),
                paper_leads=sorted(({"name": k, "gap": round(v, 2)} for k, v in _paper_gaps(periods).items() if abs(v) >= 0.01),
                                   key=lambda x: -x["gap"]),
                paper_gap=round(sum(p["batch_vs_gusto"] for p in periods if p["paper"]), 2),
                has_servers=has_servers, bar=BAR, none=NONE, owner=OWNER)


def event_tickets(db, client, shifts_path, managers=None, min_sale=400.0, min_tip=100.0):
    """Big tickets on manager / owner logins (likely catering or events) and whether Shifts has an event closeout
    naming who worked them."""
    managers = set(MANAGERS if managers is None else managers)
    try:
        pays = db.execute("""SELECT date, check_no, employee, sum(amount) amount, sum(tip) tip FROM linga_payment
                             WHERE client=? AND lower(status)='success' GROUP BY date, check_no, employee ORDER BY date DESC""",
                          (client,)).fetchall()
    except sqlite3.Error:
        return []
    m, _ = mapping(db, client, shifts_path)
    events = collections.defaultdict(list)
    outs = collections.defaultdict(list)
    if shifts_path:
        try:
            s = sqlite3.connect("file:%s?mode=ro" % shifts_path, uri=True, timeout=5)
            s.row_factory = sqlite3.Row
            names = {r["id"]: r["name"] for r in s.execute("SELECT id, name FROM staff")}
            crews = collections.defaultdict(list)
            for r in s.execute("SELECT shift_id, staff_id FROM shift_members"):
                crews[r["shift_id"]].append(names.get(r["staff_id"], r["staff_id"]))
            for r in s.execute("""SELECT sh.shift_date, sh.staff_id, t.recipient, t.amount_cents FROM tip_outs t
                                  JOIN shifts sh ON sh.id = t.shift_id"""):
                outs[r["shift_date"]].append((names.get(r["staff_id"]), r["recipient"], (r["amount_cents"] or 0) / 100))
            for r in s.execute("SELECT id, shift_date, staff_id, cc_tips_cents FROM shifts WHERE shift_type='event'"):
                events[r["shift_date"]].append(dict(owner=names.get(r["staff_id"]), crew=crews.get(r["id"], []),
                                                    tips=(r["cc_tips_cents"] or 0) / 100))
            s.close()
        except sqlite3.Error:
            pass
    out = []
    for r in pays:
        who = m.get(("batch", r["employee"]))
        if not (who in managers or who == OWNER) or r["date"] >= KEEP_FROM:
            continue
        if (r["amount"] or 0) < min_sale and (r["tip"] or 0) < min_tip:
            continue
        out.append(dict(date=r["date"], check_no=r["check_no"], login=r["employee"], role="manager" if who in managers else "owner",
                        amount=round(r["amount"] or 0, 2), tip=round(r["tip"] or 0, 2), events=events.get(r["date"], []),
                        tipouts=[dict(to=t, amount=a) for g, t, a in outs.get(r["date"], [])
                                 if g and m.get(("shifts", g)) in {OWNER, who} | managers]))
    return out
