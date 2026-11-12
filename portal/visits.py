"""Visitor counter and page tracker for the Unscatter apps.

Where each app records what people open:
  Finance   instance/visits.db  (visit table, written here on every page request)
  Shifts    shifts.db page_view (written by shifts-server.cjs: page loads + tab changes)
  Sign-in   portal-gateway/instance/access.log (JSON lines: view, login, handoff, no_access, login_denied)
The Visitors page (admins only) reads all three. A "visit" is one person's run of activity in one app with no gap
longer than 30 minutes.
"""
import datetime as dt
import json
import os
import re
import sqlite3
from collections import Counter, defaultdict

try:
    from zoneinfo import ZoneInfo
    LOCAL = ZoneInfo("America/Chicago")
except Exception:  # noqa: BLE001
    LOCAL = dt.timezone(dt.timedelta(hours=-6))

GAP = dt.timedelta(minutes=30)
APPS = {"finance": "Finance", "shifts": "Shifts", "signin": "Sign-in"}
GATEWAY_LOG = "/home/gibstoli/pythonapps/portal-gateway/instance/access.log"
# people whose own use isn't counted (the owner/bookkeeper): not recorded by Finance, and left out of every count
EXCLUDE = {"tammy@unscatterllc.com", "toolshed@unscatterllc.com"}


def _attribute(ev):
    """Sign-in page hits carry no email: give each to whoever signs in from that address that day (next, else previous),
    then drop everything belonging to EXCLUDE."""
    for seq in (list(reversed(ev)), ev):
        ip_owner = {}
        for e in seq:
            k = (e["ip"], e["t"].date())
            if e["email"]:
                ip_owner[k] = e["email"]
            elif k in ip_owner:
                e["email"] = ip_owner[k]
                e["guessed"] = True
    return [e for e in ev if (e["email"] or "").lower() not in EXCLUDE]


def _db(instance):
    db = sqlite3.connect(os.path.join(instance, "visits.db"), timeout=5)
    db.execute("""CREATE TABLE IF NOT EXISTS visit (id INTEGER PRIMARY KEY, ts TEXT NOT NULL, email TEXT, path TEXT, query TEXT,
                  status INTEGER, ip TEXT, ua TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS visit_ts ON visit(ts)")
    return db


def record(instance, email, path, query, status, ip, ua):
    if (email or "").lower() in EXCLUDE:
        return
    try:
        db = _db(instance)
        db.execute("INSERT INTO visit(ts,email,path,query,status,ip,ua) VALUES(?,?,?,?,?,?,?)",
                   (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), (email or "").lower(), path[:200],
                    (query or "")[:200], status, (ip or "")[:64], (ua or "")[:200]))
        db.commit()
        db.close()
    except sqlite3.Error:
        pass


# ------------------------------------------------------------------ page names
REPORTS = {"pl": "Profit & Loss", "balance-sheet": "Balance Sheet", "cash-flow": "Cash Flow", "vendors": "Spending by Vendor", "sales-tax": "Sales Tax",
           "transactions": "Transactions"}
FIN_PAGES = [
    (r"^/c/[^/]+/?$", "Dashboard"), (r"^/c/[^/]+/cash$", "Cash"), (r"^/c/[^/]+/tips$", "Card deposits & tips"),
    (r"^/c/[^/]+/closeouts$", "Missing closeouts"), (r"^/c/[^/]+/checks$", "Checks vs payroll"),
    (r"^/c/[^/]+/recurring-check$", "Recurring payments check"), (r"^/c/[^/]+/review", "Needs category"),
    (r"^/c/[^/]+/settings", "Client settings"), (r"^/c/[^/]+/(entry|new)", "Check / bill entry"),
    (r"^/c/[^/]+/export", "Excel download"), (r"^/login", "Sign-in page"), (r"^/sso", "Signed in (from portal)"),
    (r"^/logout", "Signed out"), (r"^/visitors", "Visitors"), (r"^/$", "Home"),
]


def finance_page(path, query=""):
    m = re.match(r"^/c/[^/]+/reports/([\w-]+)", path)
    if m:
        name = REPORTS.get(m.group(1), m.group(1))
        return name + (" (CSV)" if "format=csv" in (query or "") else "")
    for pat, name in FIN_PAGES:
        if re.match(pat, path):
            return name
    return path


def signin_page(e):
    ev, path = e.get("event"), e.get("path", "")
    if ev == "view":
        if path in ("/", ""):
            return "Sign-in page"
        m = re.match(r"^/go/(\w+)", path)
        if m:
            return "Open %s" % APPS.get(m.group(1), m.group(1).title())
        if path in ("/microsoft", "/google"):
            return "Start sign-in (%s)" % path[1:].title()
        if path == "/logout":
            return "Sign out"
        return path
    return {"login": "Signed in (%s)" % (e.get("provider") or "").title(), "handoff": "Sent to %s" % APPS.get(e.get("app"), e.get("app")),
            "no_access": "No access", "login_denied": "Sign-in refused"}.get(ev, ev)


def device(ua):
    ua = ua or ""
    if "iPhone" in ua:
        return "iPhone"
    if "iPad" in ua:
        return "iPad"
    if "Android" in ua:
        return "Android"
    if "Windows" in ua:
        return "Windows"
    if "Macintosh" in ua or "Mac OS" in ua:
        return "Mac"
    if "CrOS" in ua:
        return "Chromebook"
    return "Other" if ua else ""


def _ts(s):
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except (ValueError, AttributeError):
        return None


# ------------------------------------------------------------------ gather
def events(instance, since, shifts_db=None, gateway_log=GATEWAY_LOG, names=None):
    """Every recorded event since `since` (aware datetime) from all three apps, oldest first."""
    names = names or {}
    out = []
    s_iso = since.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        db = _db(instance)
        for ts, email, path, query, status, ip, ua in db.execute(
                "SELECT ts,email,path,query,status,ip,ua FROM visit WHERE ts>=? ORDER BY ts", (s_iso,)):
            if (status and status >= 400 and status != 403) or path.endswith((".ico", ".png", ".txt", ".xml", ".php")):
                continue
            out.append(dict(t=_ts(ts), app="finance", email=email or "", page=finance_page(path, query), ip=ip, ua=ua, view=True))
        db.close()
    except sqlite3.Error:
        pass
    if shifts_db and os.path.exists(shifts_db):
        try:
            s = sqlite3.connect("file:%s?mode=ro" % shifts_db, uri=True, timeout=5)
            for ts, email, name, path, view, kind, ip, ua in s.execute(
                    "SELECT ts,email,name,path,view,kind,ip,ua FROM page_view WHERE ts>=? ORDER BY ts", (s_iso.replace("+00:00", "Z"),)):
                if name and email:
                    names.setdefault(email, name)
                out.append(dict(t=_ts(ts), app="shifts", email=email or "", page=("Open Shifts" if kind == "load" else view or path),
                                ip=ip, ua=ua, view=True))
            s.close()
        except sqlite3.Error:
            pass
    if gateway_log and os.path.exists(gateway_log):
        try:
            with open(gateway_log, encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    t = _ts(e.get("t", ""))
                    if not t or t < since:
                        continue
                    out.append(dict(t=t, app="signin", email=(e.get("email") or "").lower(), page=signin_page(e), ip=e.get("ip", ""),
                                    ua=e.get("ua", ""), view=e.get("event") == "view"))
        except OSError:
            pass
    out = [e for e in out if e["t"]]
    out.sort(key=lambda e: e["t"])
    return out


# ------------------------------------------------------------------ where visitors come from (approximate, by internet address)
BOT_UA = ("bot", "crawl", "spider", "headless", "python-requests", "curl/", "wget", "go-http", "scan", "preview", "monitor")


def ip_info(instance, ips, lookup=True, max_new=100):
    """{ip: {city, region, country, isp, org, hosting, mobile}} from a small cache in visits.db; addresses not seen before are
    looked up at ip-api.com (approximate city and network owner, no key, one batch call). Lookup problems are ignored."""
    ips = sorted({i for i in ips if i and not i.startswith(("127.", "10.", "192.168."))})
    out = {}
    try:
        db = _db(instance)
        db.execute("""CREATE TABLE IF NOT EXISTS ip_geo (ip TEXT PRIMARY KEY, country TEXT, region TEXT, city TEXT, isp TEXT, org TEXT,
                      hosting INTEGER, mobile INTEGER, fetched_at TEXT)""")
        for r in db.execute("SELECT * FROM ip_geo WHERE ip IN (%s)" % ",".join("?" * len(ips)), ips) if ips else []:
            out[r[0]] = dict(country=r[1], region=r[2], city=r[3], isp=r[4], org=r[5], hosting=bool(r[6]), mobile=bool(r[7]))
        todo = [i for i in ips if i not in out][:max_new]
        if lookup and todo:
            import urllib.request
            req = urllib.request.Request("http://ip-api.com/batch?fields=status,query,country,regionName,city,isp,org,hosting,mobile",
                                         data=json.dumps(todo).encode(), headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=4) as resp:
                    rows = json.loads(resp.read().decode())
            except Exception:  # noqa: BLE001
                rows = []
            ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            for x in rows:
                if x.get("status") != "success":
                    continue
                d = dict(country=x.get("country"), region=x.get("regionName"), city=x.get("city"), isp=x.get("isp"), org=x.get("org"),
                         hosting=bool(x.get("hosting")), mobile=bool(x.get("mobile")))
                out[x["query"]] = d
                db.execute("INSERT OR REPLACE INTO ip_geo VALUES(?,?,?,?,?,?,?,?,?)", (x["query"], d["country"], d["region"], d["city"],
                           d["isp"], d["org"], int(d["hosting"]), int(d["mobile"]), ts))
            db.commit()
        db.close()
    except sqlite3.Error:
        pass
    return out


def _place(g):
    if not g:
        return ""
    bits = [g.get("city"), g.get("region") if g.get("country") == "United States" else g.get("country")]
    return ", ".join(b for b in bits if b)


def journeys(ev, geo, names, gap_signed=GAP, gap_anon=dt.timedelta(minutes=3)):
    """Each visit across apps, start to finish: who, from where, every page in order, and how it ended. Signed-in people are
    followed by email; visitors who never sign in by browser (addresses can change mid-visit behind a proxy)."""
    out, open_ = [], {}
    for e in ev:
        key = ("e", e["email"]) if e["email"] else ("a", e["ua"] or e["ip"])
        gap = gap_signed if e["email"] else gap_anon
        j = open_.get(key)
        if not j or e["t"] - j["end"] > gap:
            j = dict(email=e["email"], name=names.get(e["email"], ""), start=e["t"], end=e["t"], steps=[], ips=[], apps=[], ua=e["ua"],
                     device=device(e["ua"]), signed_in=False)
            out.append(j)
            open_[key] = j
        j["end"] = e["t"]
        step = "%s: %s" % (APPS[e["app"]], e["page"])
        if not j["steps"] or j["steps"][-1] != step:
            j["steps"].append(step)
        if e["ip"] and e["ip"] not in j["ips"]:
            j["ips"].append(e["ip"])
        if APPS[e["app"]] not in j["apps"]:
            j["apps"].append(APPS[e["app"]])
        if e["email"] and not e.get("guessed"):
            j["signed_in"] = True
    for j in out:
        g = next((geo.get(i) for i in j["ips"] if geo.get(i)), None)
        j["place"] = _place(g)
        j["network"] = (g or {}).get("org") or (g or {}).get("isp") or ""
        j["hosting"] = bool(g and g.get("hosting"))
        j["local"] = j["start"].astimezone(LOCAL)
        j["minutes"] = max(0, round((j["end"] - j["start"]).total_seconds() / 60))
        ua = (j["ua"] or "").lower()
        # automated: a browser that never signs in and either names itself, comes from a data center, switches internet
        # address mid-visit (a rotating proxy), or is a bare Linux desktop browser (typical of scanners)
        j["bot"] = not j["signed_in"] and (j["hosting"] or any(b in ua for b in BOT_UA) or len(j["ips"]) > 1
                                           or ("x11; linux" in ua and "android" not in ua))
        if j["signed_in"]:
            j["result"] = "Signed in"
        elif any("Signed in" in s or "Sent to" in s for s in j["steps"]):
            j["result"] = "Signed in"
        elif any("Start sign-in" in s for s in j["steps"]):
            j["result"] = "Started sign-in, didn't finish"
        elif any("No access" in s or "refused" in s for s in j["steps"]):
            j["result"] = "Turned away"
        else:
            j["result"] = "Left at the sign-in page"
    return list(reversed(out))


def report(instance, days=7, app_filter="", person="", shifts_db=None, gateway_log=GATEWAY_LOG, finance_users=None, now=None,
           hide_bots=False, lookup=False):
    now = now or dt.datetime.now(dt.timezone.utc)
    local_now = now.astimezone(LOCAL)
    start_local = (local_now - dt.timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    names = {k: (v.get("name") or "") for k, v in (finance_users or {}).items()}
    ev = _attribute(events(instance, start_local, shifts_db, gateway_log, names))
    all_people = sorted({e["email"] for e in ev if e["email"]})
    if app_filter in APPS:
        ev = [e for e in ev if e["app"] == app_filter]
    if person:
        ev = [e for e in ev if e["email"] == person]
    for e in ev:
        e["who"] = e["email"] or ("visitor %s" % (e["ip"] or "?"))
        e["local"] = e["t"].astimezone(LOCAL)
        e["device"] = device(e["ua"])
    # visits: one person in one app, no gap over 30 minutes
    visits, last = [], {}
    for e in ev:
        k = (e["who"], e["app"])
        if k in last and e["t"] - last[k]["end"] <= GAP:
            v = last[k]
            v["end"] = e["t"]
            v["events"] += 1
            if e["view"]:
                v["pages"].append(e["page"])
        else:
            v = dict(who=e["who"], email=e["email"], app=e["app"], start=e["t"], end=e["t"], events=1, pages=[e["page"]] if e["view"] else [],
                     device=e["device"])
            visits.append(v)
            last[k] = v
        e["visit"] = v
    views = [e for e in ev if e["view"]]
    today = local_now.date()
    day_list = [(start_local + dt.timedelta(days=i)).date() for i in range(days)]
    per_day = {d: dict(visits=0, views=0, people=set()) for d in day_list}
    for v in visits:
        d = v["start"].astimezone(LOCAL).date()
        if d in per_day:
            per_day[d]["visits"] += 1
            per_day[d]["people"].add(v["who"])
    for e in views:
        d = e["local"].date()
        if d in per_day:
            per_day[d]["views"] += 1
    peak = max([x["views"] for x in per_day.values()] + [1])
    daily = [dict(date=d, visits=x["visits"], views=x["views"], people=len(x["people"]), pct=round(100 * x["views"] / peak, 1)) for d, x in per_day.items()]
    by_app = []
    for k, label in APPS.items():
        a_views = [e for e in views if e["app"] == k]
        a_visits = [v for v in visits if v["app"] == k]
        by_app.append(dict(app=k, label=label, views=len(a_views), visits=len(a_visits), people=len({v["who"] for v in a_visits})))
    pages = Counter((e["app"], e["page"]) for e in views)
    page_people = defaultdict(set)
    for e in views:
        page_people[(e["app"], e["page"])].add(e["who"])
    top_pages = [dict(app=APPS[a], page=p, views=n, people=len(page_people[(a, p)])) for (a, p), n in pages.most_common(40)]
    people = {}
    for v in visits:
        p = people.setdefault(v["who"], dict(who=v["who"], email=v["email"], name=names.get(v["email"], ""), visits=0, views=0,
                                             apps=set(), last=v["end"], devices=set()))
        p["visits"] += 1
        p["views"] += len(v["pages"])
        p["apps"].add(APPS[v["app"]])
        p["last"] = max(p["last"], v["end"])
        if v["device"]:
            p["devices"].add(v["device"])
    people = sorted(people.values(), key=lambda p: p["last"], reverse=True)
    for p in people:
        p["last_local"] = p["last"].astimezone(LOCAL)
        p["apps"] = ", ".join(sorted(p["apps"]))
        p["devices"] = ", ".join(sorted(p["devices"]))
    recent = list(reversed(ev))[:150]
    geo = ip_info(instance, [e["ip"] for e in ev], lookup=lookup)
    trips = journeys(ev, geo, names)
    bots = sum(1 for j in trips if j["bot"])
    if hide_bots:
        trips = [j for j in trips if not j["bot"]]
    for e in recent:
        g = geo.get(e["ip"])
        e["place"] = _place(g)
    for p in people:
        if not p["email"]:
            g = geo.get(p["who"].replace("visitor ", ""))
            p["place"], p["network"] = _place(g), (g or {}).get("org") or (g or {}).get("isp") or ""
    return dict(days=days, start=start_local, today=today, visits=len(visits), views=len(views), people=len({v["who"] for v in visits}),
                today_visits=sum(1 for v in visits if v["start"].astimezone(LOCAL).date() == today),
                today_people=len({v["who"] for v in visits if v["start"].astimezone(LOCAL).date() == today}),
                daily=daily, by_app=by_app, top_pages=top_pages, people_rows=people, recent=recent,
                all_people=all_people, names=names, trips=trips[:150], bots=bots,
                signed_trips=sum(1 for j in trips if j["signed_in"]),
                total=totals(instance, shifts_db, gateway_log))


def totals(instance, shifts_db=None, gateway_log=GATEWAY_LOG):
    """All-time page views per app (the visitor counter), without EXCLUDE's own use."""
    ev = _attribute(events(instance, dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc), shifts_db, gateway_log))
    t = dict(finance=0, shifts=0, signin=0)
    for e in ev:
        if e["view"]:
            t[e["app"]] += 1
    t["since"] = min(e["t"] for e in ev).astimezone(LOCAL) if ev else None
    t["all"] = t["finance"] + t["shifts"] + t["signin"]
    return t
