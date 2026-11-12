"""Admin commands. Run from the app folder inside the app's virtual environment.

  python manage.py add-user EMAIL --name "Full Name" --clients wwk[,other] [--admin] [--microsoft-only]
  python manage.py set-password EMAIL
  python manage.py disable-user EMAIL / enable-user EMAIL
  python manage.py remove-user EMAIL
  python manage.py list
  python manage.py new-secret            # random value for PORTAL_SECRET_KEY or a push token
  python manage.py test-client SLUG      # pull one client's workbook now and show the numbers
  python manage.py linga-probe SLUG YYYY-MM-DD   # show the shape of Linga's daily sales summary (no names)
  python manage.py linga-load SLUG FROM:TO       # load business days from Linga (sales, card tips, cash) and re-match
  python manage.py sync [SLUG]           # pull fresh numbers now (all clients if no SLUG); used by the daily cron job
  python manage.py import-books SLUG     # one-time: copy a client's workbook (Graph) into the database and switch it to the books
  python manage.py set-editor EMAIL on|off   # let a client user add checks/bills and categorize
  python manage.py reset-signin EMAIL    # forget which Microsoft/Google account this email first signed in with
"""
import argparse
import getpass
import json
import os
import secrets
import sys

from werkzeug.security import generate_password_hash

HERE = os.path.dirname(os.path.abspath(__file__))
INST = os.path.join(HERE, "instance")
USERS = os.path.join(INST, "users.json")


def load():
    try:
        with open(USERS, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"users": {}}


def save(d):
    os.makedirs(INST, exist_ok=True)
    tmp = USERS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, USERS)
    os.chmod(USERS, 0o600)


def ask_password(visible=False):
    read = input if visible else getpass.getpass
    while True:
        p1 = read("New password (12+ characters): ").strip()
        if len(p1) < 12:
            print("Too short (%d characters)." % len(p1))
            continue
        p2 = read("Repeat password: ").strip()
        if p1 != p2:
            print("Didn't match. Tip: add --visible to see what you type.")
            continue
        if visible:
            print("\033[2J\033[H", end="")  # clear the screen so the password isn't left showing
        return generate_password_hash(p1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("email", nargs="?")
    ap.add_argument("value", nargs="?")
    ap.add_argument("--name", default="")
    ap.add_argument("--clients", default="")
    ap.add_argument("--admin", action="store_true")
    ap.add_argument("--microsoft-only", action="store_true", help="no portal password; sign in with Microsoft only")
    ap.add_argument("--visible", action="store_true", help="show the password while typing")
    a = ap.parse_args()
    d = load()
    u = d["users"]
    email = (a.email or "").strip().lower()

    if a.cmd == "new-secret":
        print(secrets.token_urlsafe(48))
    elif a.cmd == "list":
        for e, x in sorted(u.items()):
            print("%-35s %-22s clients=%s%s" % (e, x.get("name", ""), ",".join(x.get("clients", [])),
                                                  " DISABLED" if x.get("disabled") else ""))
    elif a.cmd == "add-user":
        if not email or email in u:
            sys.exit("Give a new email address.")
        clients = [c.strip() for c in a.clients.split(",") if c.strip()] or []
        u[email] = {"name": a.name, "clients": ["*"] if a.admin else clients, "hash": "" if a.microsoft_only else ask_password(a.visible)}
        save(d)
        print("Added", email)
    elif a.cmd == "set-password":
        if email not in u:
            sys.exit("No such user.")
        u[email]["hash"] = ask_password(a.visible)  # also signs out their existing sessions
        save(d)
        print("Password changed.")
    elif a.cmd in ("disable-user", "enable-user"):
        if email not in u:
            sys.exit("No such user.")
        u[email]["disabled"] = a.cmd == "disable-user"
        save(d)
        print("Done.")
    elif a.cmd == "remove-user":
        u.pop(email, None)
        save(d)
        print("Removed.")
    elif a.cmd == "test-client":
        sys.path.insert(0, HERE)
        from portal.graph import download_workbook
        from portal.snapshot import build_snapshot
        with open(os.path.join(INST, "clients.json"), encoding="utf-8") as f:
            c = json.load(f)["clients"][email]
        g = c["graph"]
        data = download_workbook(g["tenant_id"], g["client_id"], os.environ.get(g["secret_env"], ""), g["drive_user"], g["file_path"])
        s = build_snapshot(data, days_ahead=int(c.get("days_ahead", 14)))
        print(json.dumps({k: s[k] for k in ("bankSyncedAt", "currentBalance", "pendingTotal", "billsDueTotal", "payroll")}, indent=1))
    elif a.cmd == "import-books":
        _load_env()
        sys.path.insert(0, HERE)
        from portal.graph import download_workbook
        from portal import books as B
        path = os.path.join(INST, "clients.json")
        with open(path, encoding="utf-8") as f:
            allc = json.load(f)
        c = allc["clients"][email]
        g = c["graph"]
        data = download_workbook(g["tenant_id"], g["client_id"], os.environ.get(g["secret_env"], ""), g["drive_user"], g["file_path"])
        db = B.connect(INST)
        print(json.dumps(B.import_workbook(db, email, data), indent=1))
        acc = [r["account_id"] for r in db.execute("SELECT account_id FROM account WHERE client=?", (email,))]
        sl = c.pop("sheetlink", None) or {}
        c.setdefault("bank", {"provider": "sheetlink", "key_env": sl.get("key_env", "SHEETLINK_KEY_" + email.upper()), "accounts": acc})
        c["books"] = True
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(allc, f, indent=1)
        os.replace(tmp, path)
        print("Switched", email, "to the books; bank accounts:", len(acc))
    elif a.cmd == "reset-signin":
        if email not in u:
            sys.exit("No such user.")
        u[email].pop("ids", None)
        save(d)
        print("Next Microsoft/Google sign-in for", email, "will be linked fresh.")
    elif a.cmd == "set-editor":
        if email not in u:
            sys.exit("No such user.")
        u[email]["editor"] = (a.value or "").lower() in ("on", "yes", "true", "1")
        save(d)
        print(email, "editor" if u[email]["editor"] else "view only")
    elif a.cmd == "linga-probe":
        _load_env()
        sys.path.insert(0, HERE)
        from portal import linga
        with open(os.path.join(INST, "clients.json"), encoding="utf-8") as f:
            c = json.load(f)["clients"][email]
        print(linga.probe(c, a.value))
    elif a.cmd == "linga-load":
        # manage.py linga-load SLUG FROM:TO  (dates YYYY-MM-DD) — backfill business days from Linga, then re-match
        _load_env()
        sys.path.insert(0, HERE)
        import datetime as dt
        from portal import books as B, cash as C, tips as T
        with open(os.path.join(INST, "clients.json"), encoding="utf-8") as f:
            c = dict(json.load(f)["clients"][email], slug=email)
        a_, b_ = (a.value or "").split(":")
        db = B.connect(INST)
        C.ensure(db)
        T.ensure(db, email)
        n = T.refresh_linga(db, email, c, dt.date.fromisoformat(a_), dt.date.fromisoformat(b_), recent=0)
        T.run(db, email, c.get("shifts_db"), c.get("tips_since", "2026-04-27"), anchor=c.get("pay_period_end", T.PERIOD_END))
        m = db.execute("SELECT count(*) FROM deposit_match WHERE client=?", (email,)).fetchone()[0]
        print("loaded", n, "days from Linga; card deposits matched:", m)
    elif a.cmd == "processor-test":
        # read-only check of a Heartland/Portico key: manage.py processor-test cert|prod [KEY or ENV NAME]
        _load_env()
        sys.path.insert(0, HERE)
        import datetime as _dt
        from portal import processor as PR
        env = email or "cert"
        raw = a.value or "HEARTLAND_SECRET_KEY"
        key = os.environ.get(raw, raw if raw.startswith("skapi_") else "")
        if not key:
            sys.exit("No key: pass it, or the name of the env var holding it.")
        cfg = dict(key=key, url=PR.URLS[env])
        end = _dt.datetime.now(_dt.timezone.utc).replace(tzinfo=None)
        try:
            acts = PR.activity(cfg, end - _dt.timedelta(days=7), end)
            print("ReportActivity ok: %d transactions in the last 7 days" % len(acts))
            if acts:
                print("fields:", sorted(acts[0].keys()))
                print("services:", sorted({x.get("ServiceName") for x in acts}))
            bts = PR.batches(cfg, end - _dt.timedelta(days=30), end)
            print("ReportBatchHistory ok: %d batches in 30 days" % len(bts))
            if bts:
                print("batch fields:", sorted(bts[0].keys()))
            if acts:
                d = PR.detail(cfg, acts[0].get("GatewayTxnId"))
                print("ReportTxnDetail fields:", sorted(d.keys()))
        except Exception as e:
            sys.exit("Failed: %s" % e)
    elif a.cmd == "set-processor":
        # manage.py set-processor SLUG cert|prod|off   (key goes in env var HEARTLAND_SECRET_KEY)
        path = os.path.join(INST, "clients.json")
        with open(path, encoding="utf-8") as f:
            allc = json.load(f)
        c = allc["clients"][email]
        if a.value == "off":
            c.pop("processor", None)
        elif a.value in ("cert", "prod"):
            c["processor"] = {"provider": "portico", "env": a.value, "key_env": "HEARTLAND_SECRET_KEY"}
        else:
            sys.exit("use cert, prod or off")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(allc, f, indent=1)
        os.replace(tmp, path)
        print("processor for %s: %s" % (email, c.get("processor")))
    elif a.cmd == "processor-pull":
        # manage.py processor-pull SLUG FROM:TO  (dates YYYY-MM-DD)
        _load_env()
        sys.path.insert(0, HERE)
        import datetime as _dt
        from portal import books as B, processor as PR
        with open(os.path.join(INST, "clients.json"), encoding="utf-8") as f:
            c = json.load(f)["clients"][email]
        a_, b_ = (a.value or "").split(":")
        db = B.connect(INST)
        print(json.dumps(PR.pull(db, email, c, _dt.date.fromisoformat(a_), _dt.date.fromisoformat(b_))))
    elif a.cmd == "setup-google":
        # Google sign-in: client ID + secret entered as app env vars (any of these names); wires Finance, the portal
        # gateway and Shifts. Prints no secrets.
        _load_env()
        import subprocess
        names_id = ("GOOGLE_CLIENT_ID", "Google-Client-ID", "GOOGLE_CLIENT-ID")
        names_sec = ("GOOGLE_SECRET_UNSCATTER", "Google-Secret", "GOOGLE_CLIENT_SECRET")
        import re as _re
        valid = lambda n: _re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", n) and os.environ.get(n, "").strip()
        cid = next((os.environ[n].strip() for n in list(names_id) + sorted(os.environ)
                    if valid(n) and _re.search(r"google.*client.*id", n, _re.I)), "")
        sec_env = next((n for n in list(names_sec) + sorted(os.environ)
                        if valid(n) and _re.search(r"google.*secret", n, _re.I)), None)
        if not cid or not sec_env:
            sys.exit("Google client ID or secret not found in the Finance app's environment variables.")
        if not cid.endswith(".apps.googleusercontent.com"):
            sys.exit("The Google client ID doesn't look right (should end in .apps.googleusercontent.com).")
        apath = os.path.join(INST, "auth.json")
        with open(apath, encoding="utf-8") as f:
            auth = json.load(f)
        auth["google"] = {"client_id": cid, "secret_env": sec_env,
                          "redirect_uri": "https://finance.unscatterllc.com/auth/google/callback"}
        tmp = apath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(auth, f, indent=1)
        os.replace(tmp, apath)
        os.chmod(apath, 0o600)
        print("finance: google configured (secret from %s)" % sec_env)
        shifts_cfg = "/home/gibstoli/nodeapps/wayward-shifts/data/config.json"
        try:
            with open(shifts_cfg, encoding="utf-8") as f:
                sc = json.load(f)
            sc["GOOGLE_CLIENT_ID"] = cid
            sc["GOOGLE_CLIENT_SECRET"] = os.environ[sec_env].strip()
            fd = os.open(shifts_cfg + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(sc, f, indent=1)
            os.replace(shifts_cfg + ".tmp", shifts_cfg)
            print("shifts: google configured")
        except OSError as e:
            print("shifts: not updated (%s)" % e)
        gw_setup = "/home/gibstoli/pythonapps/portal-gateway/setup_sso.py"
        if os.path.exists(gw_setup):
            r = subprocess.run([sys.executable, gw_setup, "--activate"], capture_output=True, text=True, timeout=60)
            print("gateway:", (r.stdout or r.stderr).strip()[-300:])
        else:
            print("gateway: setup_sso.py not found")
        for d in ("/home/gibstoli/pythonapps/finance-portal", "/home/gibstoli/pythonapps/portal-gateway",
                  "/home/gibstoli/nodeapps/wayward-shifts"):
            try:
                os.makedirs(os.path.join(d, "tmp"), exist_ok=True)
                open(os.path.join(d, "tmp", "restart.txt"), "a").close()
                os.utime(os.path.join(d, "tmp", "restart.txt"))
            except OSError as e:
                print("restart %s failed: %s" % (d, e))
        print("restarted finance, gateway, shifts")
    elif a.cmd == "sync":
        _load_env()
        sys.path.insert(0, HERE)
        from portal import create_app
        app = create_app(INST)
        import datetime as dt
        bad = 0
        for slug in ([email] if email else app.list_clients()):
            with app.test_request_context():
                s = app.sync_client(slug)
            ok = bool(s) and not s.get("refreshError")
            bad += not ok
            print(dt.datetime.now().isoformat(timespec="seconds"), slug, "ok" if ok else "FAILED",
                  (s or {}).get("bankSource", ""), (s or {}).get("bankNote", ""))
        sys.exit(1 if bad else 0)
    else:
        sys.exit(__doc__)


def _load_env():
    """Cron jobs don't get the app's environment variables; read them from the site's .htaccess (SetEnv lines)."""
    import re
    path = os.environ.get("PORTAL_ENV_FILE") or os.path.join(os.path.expanduser("~"), "finance.unscatterllc.com", ".htaccess")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r'\s*SetEnv\s+(\S+)\s+"?([^"\r\n]*)"?', line)
                if m:
                    os.environ.setdefault(m.group(1), m.group(2))
    except FileNotFoundError:
        pass


if __name__ == "__main__":
    main()
