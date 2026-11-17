"""Client Finance Portal: password-protected, read-only "at a glance" pages, one per client.

All settings live in the instance folder (outside the web root):
  instance/clients.json   which clients exist and where their numbers come from (no secrets)
  instance/users.json     who can sign in and which clients they can see (hashed passwords)
Secrets (session key, Microsoft app secrets, push tokens) come from environment variables.
"""
import datetime as dt
import hmac
import re
import json
import zlib
import os
import secrets
import tempfile
import threading
import time
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

_lock = threading.Lock()
_DUMMY_HASH = generate_password_hash(secrets.token_hex(16))  # keeps timing equal for unknown users


# ------------------------------------------------------------------ storage helpers
def _read_json(app, name, default):
    try:
        with open(os.path.join(app.instance_path, name), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _write_json(app, name, data):
    os.makedirs(app.instance_path, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=app.instance_path, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, os.path.join(app.instance_path, name))


def create_app(instance_path=None):
    app = Flask(__name__, instance_path=instance_path or os.environ.get("PORTAL_INSTANCE"),
                instance_relative_config=True)
    key = os.environ.get("PORTAL_SECRET_KEY")
    if not key or len(key) < 32:
        raise RuntimeError("Set PORTAL_SECRET_KEY (32+ random characters) in the app's environment variables.")
    app.config.update(
        SECRET_KEY=key,
        SESSION_COOKIE_NAME="portal_session",
        SESSION_COOKIE_SECURE=os.environ.get("PORTAL_INSECURE_DEV") != "1",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=dt.timedelta(hours=int(os.environ.get("PORTAL_SESSION_HOURS", "8"))),
        PORTAL_TITLE=os.environ.get("PORTAL_TITLE", "Client Finance Portal"),
        PORTAL_TZ=os.environ.get("PORTAL_TZ", "America/Chicago"),
        # sites allowed to show the portal inside an <iframe>, space separated (e.g. https://shifts.unscatterllc.com)
        FRAME_ANCESTORS=os.environ.get("PORTAL_FRAME_ANCESTORS", ""),
    )
    os.makedirs(app.instance_path, exist_ok=True)

    def clients():
        return _read_json(app, "clients.json", {"clients": {}})["clients"]

    def users():
        return _read_json(app, "users.json", {"users": {}})["users"]

    def log(event, **kw):
        line = json.dumps(dict(t=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                               event=event, ip=request.remote_addr, **kw))
        with _lock, open(os.path.join(app.instance_path, "access.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")

    # -------------------------------------------------------------- security plumbing
    @app.before_request
    def _security():
        if request.path.startswith("/.well-known/acme-challenge/"):
            return None  # certificate validation must work over plain http, before any redirect or sign-in
        if app.config["SESSION_COOKIE_SECURE"]:
            proto = request.headers.get("X-Forwarded-Proto", request.scheme)
            if proto != "https" and request.endpoint != "static":
                return redirect(request.url.replace("http://", "https://", 1), code=301)
        g.user = None
        email = session.get("user")
        if email:
            u = users().get(email)
            if u and not u.get("disabled") and session.get("pwv") == u.get("hash", "")[-12:]:  # password change logs out old sessions
                g.user = dict(u, email=email)
            else:
                session.clear()
        if request.method == "POST" and not request.path.startswith("/api/"):
            sent = request.form.get("csrf_token", "")
            if not sent or not hmac.compare_digest(sent, session.get("csrf", "")):
                abort(400)

    @app.after_request
    def _headers(resp):
        fa = ("'self' " + app.config["FRAME_ANCESTORS"]).strip()
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "frame-ancestors %s; form-action 'self'; base-uri 'none'" % fa)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "same-origin"
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
        if not app.config["FRAME_ANCESTORS"]:
            resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
        try:  # page tracker (Visitors page): pages and downloads people open; not static files or scripted checks
            ct = resp.headers.get("Content-Type", "")
            if (request.method == "GET" and not request.path.startswith(("/static/", "/.well-known/", "/visitors")) and not request.headers.get("X-Diag")
                    and (ct.startswith("text/html") or ct.startswith("text/csv") or "spreadsheet" in ct or resp.status_code in (301, 302))):
                from . import visits as V
                V.record(app.instance_path, (g.get("user") or {}).get("email", ""), request.path, request.query_string.decode("utf-8", "replace"),
                         resp.status_code, request.remote_addr, request.headers.get("User-Agent", ""))
        except Exception:  # noqa: BLE001 — never break a page over this
            pass
        return resp

    @app.context_processor
    def _ctx():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return dict(csrf_token=session["csrf"], user=g.get("user"), title=app.config["PORTAL_TITLE"])

    def login_required(view):
        @wraps(view)
        def wrapper(*a, **kw):
            if not g.user:
                return redirect(url_for("login", next=request.path))
            return view(*a, **kw)
        return wrapper

    def client_or_404(slug):
        c = clients().get(slug)
        allowed = g.user.get("clients", [])
        if not c or not ("*" in allowed or slug in allowed):
            abort(404)  # don't reveal which clients exist
        return dict(c, slug=slug)

    # -------------------------------------------------------------- login throttling
    def _throttle(keys, failed=None):
        """Return seconds locked out (0 if fine). failed=True records a failure, False clears."""
        now = time.time()
        with _lock:
            data = _read_json(app, "throttle.json", {})
            data = {k: [t for t in v if now - t < 900] for k, v in data.items()}
            if failed is True:
                for k in keys:
                    data.setdefault(k, []).append(now)
            elif failed is False:
                for k in keys:
                    data.pop(k, None)
            _write_json(app, "throttle.json", data)
        worst = max((data.get(k, []) for k in keys), key=len)
        return int(900 - (now - worst[-5])) if len(worst) >= 5 else 0

    # -------------------------------------------------------------- sign-in
    def auth_cfg():
        """instance/auth.json: {"password_login": true, "microsoft": {tenant_id, client_id, secret_env, redirect_uri}}"""
        return _read_json(app, "auth.json", {"password_login": True})

    def _start_session(email, u):
        session.clear()
        session.permanent = True
        session.update(user=email, pwv=u.get("hash", "")[-12:], csrf=secrets.token_urlsafe(32))

    def _next_url(nxt):
        return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else url_for("home")

    def render_login(code=200):
        return render_template("login.html", ms=bool(ms_cfg()), google=bool(prov_cfg("google")),
                               pw=auth_cfg().get("password_login", True)), code

    @app.route("/.well-known/acme-challenge/<token>")
    def acme_challenge(token):
        """Let's Encrypt HTTP-01 check: serve the key authorization the renewal script left for this token."""
        from flask import Response
        if not token or len(token) > 128 or any(not (ch.isalnum() or ch in "-_") for ch in token):
            abort(404)
        folder = os.environ.get("ACME_CHALLENGE_DIR", "/home/gibstoli/private/acme/challenges")
        try:
            with open(os.path.join(folder, token), encoding="ascii") as f:
                return Response(f.read().strip(), mimetype="text/plain")
        except OSError:
            abort(404)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            if not auth_cfg().get("password_login", True):
                abort(404)
            email = request.form.get("email", "").strip().lower()
            keys = ["ip:" + (request.remote_addr or "?"), "user:" + email]
            wait = _throttle(keys)
            if wait:
                flash("Too many attempts. Try again in %d minutes." % (wait // 60 + 1))
                return render_login(429)
            u = users().get(email)
            h = (u or {}).get("hash") or _DUMMY_HASH  # users without a password can only use Microsoft
            ok = check_password_hash(h, request.form.get("password", "")) and bool(u and u.get("hash"))
            if u and ok and not u.get("disabled"):
                _throttle(keys, failed=False)
                _start_session(email, u)
                log("login", user=email, method="password")
                return redirect(_next_url(request.args.get("next")))
            _throttle(keys, failed=True)
            log("login_failed", user=email, method="password")
            flash("Email or password is incorrect.")
            return render_login(401)
        c = sso_cfg()
        if c and not request.args.get("local"):
            from urllib.parse import urlencode
            nxt = request.args.get("next", "")
            return redirect(c["portal"] + "/go/finance" + (("?" + urlencode({"next": nxt})) if nxt.startswith("/") else ""))
        return render_login()

    # Sign in with Microsoft or Google (OpenID Connect, authorization code + PKCE).
    # The ID token comes straight from the provider's token endpoint over TLS using our client secret,
    # so it is trusted without a separate signature check; we still check issuer, audience, nonce and expiry.
    MS_CONSUMERS = "9188040d-6c67-4c5b-b112-36a304b66dad"  # personal Microsoft accounts

    def prov_cfg(name):
        a = auth_cfg()
        m = a.get(name) or {}
        need = ("client_id", "secret_env", "redirect_uri") + (("tenant_id",) if name == "microsoft" else ())
        return m if all(m.get(k) for k in need) and os.environ.get(m["secret_env"]) else None

    def ms_cfg():
        return prov_cfg("microsoft")

    def _endpoints(name, m):
        if name == "google":
            return "https://accounts.google.com/o/oauth2/v2/auth", "https://oauth2.googleapis.com/token"
        authority = "common" if m.get("multi_tenant") else m["tenant_id"]
        base = "https://login.microsoftonline.com/%s/oauth2/v2.0/" % authority
        return base + "authorize", base + "token"

    @app.route("/auth/<name>")
    def oidc_start(name):
        m = prov_cfg(name) if name in ("microsoft", "google") else None
        if not m:
            abort(404)
        import base64, hashlib
        from urllib.parse import urlencode
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        session["oidc"] = dict(p=name, state=secrets.token_urlsafe(24), nonce=secrets.token_urlsafe(24), verifier=verifier,
                               next=request.args.get("next", ""))
        q = dict(client_id=m["client_id"], response_type="code", redirect_uri=m["redirect_uri"],
                 scope="openid profile email", state=session["oidc"]["state"], nonce=session["oidc"]["nonce"],
                 code_challenge=challenge, code_challenge_method="S256", prompt="select_account")
        if name == "microsoft":
            q["response_mode"] = "query"
        return redirect("%s?%s" % (_endpoints(name, m)[0], urlencode(q)))

    @app.route("/auth/microsoft/start")
    def ms_start():  # old links
        return redirect(url_for("oidc_start", name="microsoft", next=request.args.get("next", "")))

    def _identity(name, m, claims):
        """(emails we may trust, stable account id) or (set(), None) if the email can't be trusted."""
        if name == "google":
            if claims.get("iss") not in ("https://accounts.google.com", "accounts.google.com") or not claims.get("email_verified"):
                return set(), None
            return {str(claims.get("email", "")).lower()}, "google:" + str(claims.get("sub"))
        tid = str(claims.get("tid", ""))
        if claims.get("iss") != "https://login.microsoftonline.com/%s/v2.0" % tid:
            return set(), None
        if not m.get("multi_tenant") and tid != m["tenant_id"]:
            return set(), None
        ems = set()
        if tid == m["tenant_id"]:  # our own directory, including invited guests
            ems = {str(claims.get(k) or "").lower() for k in ("email", "preferred_username", "upn")}
        elif tid == MS_CONSUMERS or claims.get("xms_edov") in (True, 1, "1", "true", "True"):
            ems = {str(claims.get("email") or "").lower()}  # email Microsoft has verified
        return ems - {""}, "microsoft:%s:%s" % (tid, claims.get("oid"))

    @app.route("/auth/<name>/callback")
    def oidc_callback(name):
        import base64
        import requests as rq
        m, st = (prov_cfg(name) if name in ("microsoft", "google") else None), session.pop("oidc", None)
        if not m or not st or st.get("p") != name or not hmac.compare_digest(request.args.get("state", ""), st["state"]):
            flash("Sign-in expired. Please try again.")
            return redirect(url_for("login"))
        if request.args.get("error"):
            flash("Sign-in was cancelled or failed.")
            return redirect(url_for("login"))
        try:
            r = rq.post(_endpoints(name, m)[1], timeout=20, data=dict(
                client_id=m["client_id"], client_secret=os.environ[m["secret_env"]], grant_type="authorization_code",
                code=request.args.get("code", ""), redirect_uri=m["redirect_uri"], code_verifier=st["verifier"]))
            r.raise_for_status()
            part = r.json()["id_token"].split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        except Exception as e:
            app.logger.warning("%s sign-in failed: %s", name, e)
            flash("Sign-in failed. Please try again.")
            return redirect(url_for("login"))
        good = claims.get("aud") == m["client_id"] and claims.get("nonce") == st["nonce"] and int(claims.get("exp", 0)) > time.time()
        emails, ident = _identity(name, m, claims) if good else (set(), None)
        shown = claims.get("email") or claims.get("preferred_username") or "this account"
        with _lock:
            allu = _read_json(app, "users.json", {"users": {}})
            email = next((e for e in emails if e in allu["users"]), None)
            u = allu["users"].get(email) if email else None
            ok = bool(good and u and not u.get("disabled") and ident)
            if ok:
                ids = u.setdefault("ids", {})
                bound = ids.get(name)
                if bound and bound != ident:  # same email, different account than the one first used
                    ok = False
                elif not bound:
                    ids[name] = ident
                    _write_json(app, "users.json", allu)
        if not ok:
            log("login_denied", user=",".join(sorted(emails)) or str(shown), method=name)
            if good and not emails:
                flash("We couldn't confirm the email on that %s account. Try the other sign-in option, or ask the administrator." % name.title())
            else:
                flash("%s isn't set up for this portal yet. Ask the administrator to add it." % shown)
            return redirect(url_for("login"))
        _start_session(email, u)
        log("login", user=email, method=name)
        return redirect(_next_url(st.get("next")))

    # -------------------------------------------------------------- single sign-in through portal.unscatterllc.com
    def sso_cfg():
        """instance/sso.json {portal, key}: when present, sign-in happens at the portal gateway."""
        c = _read_json(app, "sso.json", {})
        return c if c.get("portal") and len(c.get("key", "")) >= 32 else None

    def _ticket(t, key):
        import base64, hashlib
        try:
            p, sig = t.split(".")
            want = base64.urlsafe_b64encode(hmac.new(key.encode(), p.encode(), hashlib.sha256).digest()).decode().rstrip("=")
            if not hmac.compare_digest(sig, want):
                return None
            d = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
        except (ValueError, TypeError):
            return None
        if d.get("aud") != "finance" or not (time.time() - 5 < d.get("exp", 0) < time.time() + 120):
            return None
        # one use only: claim the ticket id atomically (works across worker processes)
        folder = os.path.join(app.instance_path, "sso_used")
        os.makedirs(folder, exist_ok=True)
        for f in os.listdir(folder):
            fp = os.path.join(folder, f)
            if time.time() - os.path.getmtime(fp) > 300:
                os.remove(fp)
        try:
            os.close(os.open(os.path.join(folder, "".join(ch for ch in str(d.get("jti")) if ch.isalnum() or ch in "-_")[:40] or "x"),
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            return None
        return d

    def _redeem(c, code):
        """Swap the one-time code from the URL for the signed ticket the gateway left on the server."""
        if not code or len(code) > 64 or any(not (ch.isalnum() or ch in "-_") for ch in code):
            return ""
        src = os.path.join(c.get("codes_dir", "/home/gibstoli/private/sso_codes"), code)
        mine = src + ".finance-%d" % os.getpid()
        try:
            os.rename(src, mine)  # only one request can claim it
        except OSError:
            return ""
        try:
            if time.time() - os.path.getmtime(mine) > 120:
                return ""
            with open(mine, encoding="utf-8") as f:
                return f.read().strip()
        finally:
            os.remove(mine)

    @app.route("/sso")
    def sso():
        c = sso_cfg()
        d = _ticket(_redeem(c, request.args.get("code", "")), c["key"]) if c else None
        if not d:
            flash("That sign-in link expired. Please sign in again.")
            return redirect(url_for("login", local=1) if not c else c["portal"] + "/go/finance")
        email, uid = str(d.get("email", "")).lower(), str(d.get("uid", ""))
        prov = uid.split(":", 1)[0]
        with _lock:
            allu = _read_json(app, "users.json", {"users": {}})
            u = allu["users"].get(email)
            ok = bool(u and not u.get("disabled") and prov in ("microsoft", "google"))
            if ok:
                ids = u.setdefault("ids", {})
                if ids.get(prov) and ids[prov] != uid:
                    ok = False
                elif not ids.get(prov):
                    ids[prov] = uid
                    _write_json(app, "users.json", allu)
        if not ok:
            log("login_denied", user=email, method="portal")
            return render_template("message.html", msg="%s doesn't have access to Finance." % email), 403
        _start_session(email, u)
        log("login", user=email, method="portal:" + prov)
        return redirect(_next_url(request.args.get("next")))

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        c = sso_cfg()
        return redirect(c["portal"] + "/logout" if c else url_for("login"))

    # -------------------------------------------------------------- pages
    @app.route("/")
    @login_required
    def home():
        allowed = g.user.get("clients", [])
        mine = [dict(c, slug=s) for s, c in clients().items() if "*" in allowed or s in allowed]
        if len(mine) == 1:
            return redirect(url_for("client_page", slug=mine[0]["slug"]))
        return render_template("home.html", clients=mine)

    def _snap_name(slug):
        return "snapshot_%s.json" % "".join(ch for ch in slug if ch.isalnum() or ch in "-_")

    def get_snapshot(c, force=False, by=""):
        """Saved numbers. Pulls fresh data only when forced (daily job / Sync now), when nothing is saved yet,
        or when the client sets "refresh_minutes" in clients.json."""
        if c.get("books"):
            return books_snapshot(c, force=force, by=by)
        snap = _read_json(app, _snap_name(c["slug"]), None)
        gcfg = c.get("graph") or {}
        if c.get("source") != "graph" or not gcfg:
            return snap
        if snap and not force:
            mins = int(c.get("refresh_minutes", 0) or 0)
            age = time.time() - dt.datetime.fromisoformat(snap["receivedAt"]).timestamp()
            if not mins or age < 60 * mins:
                return snap
        try:
            from .graph import download_workbook
            from .snapshot import build_snapshot
            secret = os.environ.get(gcfg.get("secret_env", ""), "")
            data = download_workbook(gcfg["tenant_id"], gcfg["client_id"], secret, gcfg["drive_user"], gcfg["file_path"])
            new = build_snapshot(data, days_ahead=int(c.get("days_ahead", 14)))
            sl = c.get("sheetlink") or {}
            sl_key = os.environ.get(sl.get("key_env", ""), "")
            if sl_key:
                try:
                    from .sheetlink import live_bank
                    new.update(live_bank(sl_key, account_id=sl.get("account_id") or new.get("bankAccountId"),
                                         mask=sl.get("account_mask") or new.get("bankMask")))
                except Exception as e:
                    app.logger.warning("sheetlink live pull failed for %s: %s", c["slug"], e)
                    new["bankNote"] = "Live bank pull didn't work just now; balance and pending are from the workbook."
            new["syncedBy"] = by or "auto"
            return _save_snapshot(c["slug"], new)
        except Exception as e:
            app.logger.warning("refresh failed for %s: %s", c["slug"], e)
            if snap:
                snap["refreshError"] = "Couldn't refresh just now. Showing the last saved numbers."
            return snap

    # -------------------------------------------------------------- the books (database)
    from . import books as B
    from . import platformdata as PD
    from . import cash as C
    from . import tips as T

    def db():
        if "db" not in g:
            g.db = B.connect(app.instance_path)
            C.ensure(g.db)
            T.ensure(g.db)
            from . import salesum, lingaarchive, posimport
            salesum.ensure(g.db)
            lingaarchive.ensure(g.db)
            posimport.ensure(g.db)
            from . import processor
            processor.ensure(g.db)
        return g.db

    def tips_run(c, linga=False):
        """Refresh daily sales/tips and re-match card deposits (cheap; runs on sync and on the pages that show it).
        With linga=True (daily sync / Sync now) it first loads recent business days from Linga."""
        if c.get("books") and c.get("shifts_db"):
            if linga:  # only from the "Pull from Linga" button (owner's rule: by hand, at most 3 times a day)
                try:
                    end = dt.date.today() - dt.timedelta(days=1)
                    T.refresh_linga(db(), c["slug"], c, dt.date.fromisoformat(c.get("tips_since", "2026-04-27")), end)
                except Exception as e:
                    app.logger.warning("linga pull failed for %s: %s", c["slug"], e)
                try:  # sales tax for recent days Shifts doesn't have it for (same pull, same 3-a-day rule)
                    from . import salesum
                    salesum.pull_linga_tax(db(), c["slug"], c, c["shifts_db"], dt.date.fromisoformat(c.get("tips_since", "2026-04-27")), end)
                except Exception as e:
                    app.logger.warning("linga tax pull failed for %s: %s", c["slug"], e)
            try:
                from . import posimport
                posimport.sync_from_shifts(db(), c["slug"], c["shifts_db"])
            except Exception as e:
                app.logger.warning("POS sync from Shifts failed for %s: %s", c["slug"], e)
            try:  # keep the per-day cash → deposit matching current for Shifts' cash card
                from . import lingaarchive as LA
                LA.deposit_allocation(db(), c["slug"], c["shifts_db"])
            except Exception as e:
                app.logger.warning("cash allocation failed for %s: %s", c["slug"], e)
            try:
                T.run(db(), c["slug"], c["shifts_db"], c.get("tips_since", "2026-04-27"), anchor=c.get("pay_period_end", T.PERIOD_END))
            except Exception as e:  # never block the page over this
                app.logger.warning("card tips matching failed for %s: %s", c["slug"], e)

    @app.teardown_appcontext
    def _close_db(e):
        d = g.pop("db", None)
        if d is not None:
            d.close()

    def bank_cfg(c):
        b = c.get("bank") or {}
        return b if b.get("provider") == "sheetlink" and b.get("accounts") else None

    def bank_sync(c, by):
        """Pull the client's bank accounts from SheetLink into the books. Returns an error message or None."""
        b = bank_cfg(c)
        if not b:
            return "No bank feed is set up for this client yet (Client settings)."
        key = os.environ.get(b.get("key_env") or "SHEETLINK_KEY", "")
        if not key:
            return "The SheetLink key for this client is missing on the server."
        try:
            B.sync_sheetlink(db(), c["slug"], key, b["accounts"])
            B.set_meta(db(), c["slug"], "synced_by", by)
            db().commit()
        except Exception as e:
            app.logger.warning("bank sync failed for %s: %s", c["slug"], e)
            return "429" in str(e) and "The bank feed is busy; try again in a few minutes." or "Bank sync didn't finish; try again in a few minutes."
        return None

    def books_snapshot(c, force=False, by=""):
        err = bank_sync(c, by or "manual") if force else None
        if force:
            tips_run(c)
        s = B.snapshot(db(), c["slug"], (bank_cfg(c) or {}).get("accounts"), int(c.get("days_ahead", 14)))
        if err:
            s["refreshError"] = err
        return s

    def can_edit(slug):
        u = g.get("user") or {}
        allowed = u.get("clients", [])
        return "*" in allowed or (u.get("editor") and slug in allowed)

    def is_admin():
        return "*" in (g.get("user") or {}).get("clients", [])

    def edit_required(slug):
        c = client_or_404(slug)
        if not c.get("books") or not can_edit(slug):
            abort(404)
        return c

    def _money_in(v):
        v = (v or "").replace("$", "").replace(",", "").strip()
        try:
            x = round(float(v), 2)
        except ValueError:
            return None
        return x if 0 < abs(x) < 10_000_000 else None

    def _day(v):
        try:
            return dt.date.fromisoformat((v or "").strip()).isoformat()
        except ValueError:
            return None

    def _txt(v, n=120):
        return (v or "").strip()[:n] or None

    @app.route("/c/<slug>/new/<kind>", methods=["GET", "POST"])
    @login_required
    def entry_new(slug, kind):
        c = edit_required(slug)
        if kind not in ("check", "bill"):
            abort(404)
        f = request.form
        cats = [x["name"] for x in B.categories(db(), slug)]
        if request.method == "POST":
            amt, cat = _money_in(f.get("amount")), f.get("category") or None
            if cat and cat not in cats:
                cat = None
            payee = _txt(f.get("payee"), 100)
            if kind == "check":
                d = _day(f.get("date"))
                if not (amt and d and payee):
                    flash("Fill in date, payee and amount.")
                else:
                    eid = B.add_check(db(), slug, g.user["email"], d, abs(amt), payee, cat, _txt(f.get("check_no"), 12), _txt(f.get("memo"), 200),
                                      kind="check" if f.get("check_no") else "manual")
                    log("entry_add", user=g.user["email"], client=slug, id=eid, kind="check")
                    flash("Saved: %s %s" % (payee, money(-abs(amt))), "ok")
                    return redirect(url_for("client_page", slug=slug) if not f.get("again") else url_for("entry_new", slug=slug, kind=kind))
            else:
                d = _day(f.get("due_date"))
                if not (amt and d and payee):
                    flash("Fill in vendor, amount and due date.")
                else:
                    bid = B.add_bill(db(), slug, g.user["email"], payee, abs(amt), d, cat, _txt(f.get("invoice_no"), 40), _txt(f.get("notes"), 200))
                    log("bill_add", user=g.user["email"], client=slug, id=bid)
                    flash("Saved bill: %s %s due %s" % (payee, money(abs(amt)), mdy(d)), "ok")
                    return redirect(url_for("client_page", slug=slug) if not f.get("again") else url_for("entry_new", slug=slug, kind=kind))
        return render_template("entry_form.html", c=c, kind=kind, cats=cats, payees=B.payees(db(), slug), f=f,
                               today=dt.date.today().isoformat())

    @app.route("/c/<slug>/entry/<int:eid>", methods=["GET", "POST"])
    @login_required
    def entry_edit(slug, eid):
        c = edit_required(slug)
        e = db().execute("SELECT * FROM entry WHERE client=? AND id=?", (slug, eid)).fetchone()
        if not e:
            abort(404)
        cats = [x["name"] for x in B.categories(db(), slug)]
        f = request.form
        if request.method == "POST":
            if f.get("action") == "void":
                ok = B.void_entry(db(), slug, g.user["email"], eid)
                log("entry_void", user=g.user["email"], client=slug, id=eid, ok=ok)
                flash("Voided." if ok else "Only hand-entered items the bank hasn't matched can be voided.")
                return redirect(url_for("client_page", slug=slug))
            upd = dict(payee=_txt(f.get("payee"), 100), memo=_txt(f.get("memo"), 200), check_no=_txt(f.get("check_no"), 12))
            if f.get("category") in cats:
                upd["category"] = f.get("category")
            if not e["bank_txn_id"]:
                if _day(f.get("date")):
                    upd["date"] = _day(f.get("date"))
                amt = _money_in(f.get("amount"))
                if amt:
                    upd["amount"] = -abs(amt) if e["amount"] < 0 else abs(amt)
            B.update_entry(db(), slug, g.user["email"], eid, **upd)
            if f.get("rule") and upd.get("category") and e["description"]:
                B.add_rule(db(), slug, f.get("pattern") or B.rule_pattern(e["description"]), upd.get("payee"), upd["category"])
                n = 0
                for x in db().execute("SELECT id, description FROM entry WHERE client=? AND category IN ('Uncategorized','Missing Check')", (slug,)).fetchall():
                    p_, c_ = B.apply_rules(db(), slug, x["description"])
                    if c_:
                        db().execute("UPDATE entry SET category=?, payee=coalesce(?, payee), updated_by=?, updated_at=? WHERE id=?",
                                     (c_, p_, g.user["email"], B.now(), x["id"]))
                        n += 1
                db().commit()
                if n:
                    flash("Rule saved and applied to %d other item%s." % (n, "" if n == 1 else "s"), "ok")
            log("entry_edit", user=g.user["email"], client=slug, id=eid)
            nxt = f.get("next", "")
            return redirect(nxt if nxt.startswith("/c/%s/" % slug) else url_for("client_page", slug=slug))
        bt = db().execute("SELECT * FROM bank_txn WHERE client=? AND txn_id=?", (slug, e["bank_txn_id"])).fetchone() if e["bank_txn_id"] else None
        splits = [dict(r) for r in db().execute("SELECT category, amount, memo, source FROM entry_split WHERE client=? AND entry_id=? ORDER BY id", (slug, eid))]
        from . import receipts as R
        R.ensure(db())
        recs = R.for_entries(db(), slug, [eid]).get(eid, [])
        rfolder = R.target(c, clients())[1]
        return render_template("entry_edit.html", c=c, e=e, bt=bt, cats=cats, payees=B.payees(db(), slug), splits=splits, recs=recs, rfolder=rfolder,
                               pattern=B.rule_pattern(e["description"]), nxt=request.args.get("next", ""))

    @app.route("/c/<slug>/review")
    @login_required
    def review(slug):
        c = edit_required(slug)
        rows = db().execute("""SELECT * FROM entry WHERE client=? AND status!='void' AND category IN ('Uncategorized','Missing Check')
                               ORDER BY date DESC LIMIT 200""", (slug,)).fetchall()
        return render_template("review.html", c=c, rows=rows, cats=[x["name"] for x in B.categories(db(), slug)])

    @app.route("/c/<slug>/bills/<int:bid>", methods=["POST"])
    @login_required
    def bill_action(slug, bid):
        edit_required(slug)
        f = request.form
        if f.get("action") == "pay":
            d = _day(f.get("paid_date")) or dt.date.today().isoformat()
            B.pay_bill(db(), slug, g.user["email"], bid, d, f.get("method", "other"), _txt(f.get("check_no"), 12))
            log("bill_paid", user=g.user["email"], client=slug, id=bid)
            flash("Marked paid.", "ok")
        elif f.get("action") == "save":
            amt, due = _money_in(f.get("amount")), _day(f.get("due_date"))
            name = _txt(f.get("vendor"), 120)
            if not name or amt is None or amt <= 0:
                flash("Enter the payee and an amount.")
            else:
                B.update_bill(db(), slug, bid, name, amt, due, _txt(f.get("category"), 80), _txt(f.get("invoice_no"), 40), _txt(f.get("notes"), 300))
                log("bill_edit", user=g.user["email"], client=slug, id=bid)
                flash("Bill updated.", "ok")
        elif f.get("action") == "delete":
            db().execute("UPDATE bill SET status='void' WHERE client=? AND id=? AND status='open'", (slug, bid))
            db().commit()
            log("bill_void", user=g.user["email"], client=slug, id=bid)
            flash("Bill removed.", "ok")
        return redirect(url_for("client_page", slug=slug) + "#bills")

    def _payroll_people(c, payroll):
        """Per person for the next pay day(s): hourly pay, salary and the customer card tips in the check, so it's clear
        how much of payroll the tips fund. Tips = what each person earned in card tips for the pay period (Card deposits &
        tips: tips on the cards they ran ± tip-outs, same rules as the tips check). With a Gusto preview, hourly/salary pay
        = Gusto gross − those tips; otherwise from the Time Clock (hours × rate) and the salaries."""
        import re as _re
        from . import tipcheck
        slug = c["slug"]
        norm = lambda n: B._norm_name(_re.sub(r"\(.*?\)", " ", n or ""))
        days = [p for p in payroll["pay_days"][:3] if p.get("gusto")] or payroll["pay_days"][:1]
        if not days:
            return
        tc = tipcheck.check(db(), slug, c.get("shifts_db"), c.get("tips_since", "2026-04-27"), c.get("paper_until", tipcheck.PAPER_UNTIL),
                            c.get("solo_bar", tipcheck.SOLO_BAR), c.get("tip_managers"), extra_periods=[(p["start"], p["end"]) for p in days]) or {}
        by_period = {(x["start"], x["end"]): {norm(e["name"]): e["earned"] for e in x["employees"]} for x in tc.get("periods", [])}
        short_by_period = {(x["start"], x["end"]): {norm(e["name"]): (e.get("cash_short") or 0.0, e.get("cash_short_days") or [])
                                                    for e in x["employees"] if e.get("cash_short")} for x in tc.get("periods", [])}
        detail = {}
        B._pos_wages(db(), slug, c.get("shifts_db"), detail=detail)
        sal = {norm(x["name"]): round(x["annual"] / 26.0, 2) for x in payroll.get("salaries") or []}

        def tip_for(tips, n):
            if n in tips:
                return tips[n]
            f = [v for k, v in tips.items() if k.split(" ")[0] == n.split(" ")[0]]
            return f[0] if len(f) == 1 else 0.0
        for p in days:
            tips = by_period.get((p["start"], p["end"]), {})
            rows = {}
            if p.get("gusto"):
                for r in p["gusto"]["rows"]:
                    n = norm(r["name"])
                    t = round(min(max(tip_for(tips, n), 0.0), r["gross"]), 2)
                    rows[n] = dict(name=r["name"], tips=t, pay=round(r["gross"] - t, 2), gross=r["gross"], er_tax=r["er_tax"], total=r["total"],
                                   salaried=n in sal or any(n.startswith(k.split(" ")[0] + " ") for k in sal))
            else:
                for d, lst in detail.items():
                    if p["start"] <= d <= p["end"]:
                        for x in lst:
                            n = norm(x["employee"])
                            r = rows.setdefault(n, dict(name=x["employee"], tips=0.0, pay=0.0, salaried=False))
                            r["pay"] += x["hours"] * x["rate"]
                for n, v in sal.items():
                    r = rows.setdefault(n, dict(name=n.title(), tips=0.0, pay=0.0, salaried=True))
                    r["pay"] += v
                    r["salaried"] = True
                for n, v in tips.items():
                    if v > 0.005:
                        k = n if n in rows else next((k for k in rows if k.split(" ")[0] == n.split(" ")[0]), n)
                        r = rows.setdefault(k, dict(name=n.title(), tips=0.0, pay=0.0, salaried=False))
                        r["tips"] += v
                for r in rows.values():
                    r["pay"], r["tips"] = round(r["pay"], 2), round(r["tips"], 2)
                    r["gross"] = round(r["pay"] + r["tips"], 2)
            # cash expected on their closeouts but not turned in comes off the tips to be paid
            for n, (cs, cdays) in short_by_period.get((p["start"], p["end"]), {}).items():
                k = n if n in rows else next((k for k in rows if k.split(" ")[0] == n.split(" ")[0]), n)
                r = rows.setdefault(k, dict(name=n.title(), tips=0.0, pay=0.0, gross=0.0, salaried=False))
                r["cash_short"] = round(r.get("cash_short", 0.0) + cs, 2)
                r["cash_short_days"] = r.get("cash_short_days", []) + cdays
            for r in rows.values():
                r.setdefault("cash_short", 0.0)
                r["tips_to_pay"] = round(r["tips"] - r["cash_short"], 2)
            people = sorted((r for r in rows.values() if abs(r["gross"]) >= 0.005 or r["cash_short"]), key=lambda r: -r["gross"])
            g_ = round(sum(r["gross"] for r in people), 2)
            t_ = round(sum(r["tips"] for r in people), 2)
            cs_ = round(sum(r["cash_short"] for r in people), 2)
            p["people"] = people
            p["people_totals"] = dict(gross=g_, tips=t_, pay=round(g_ - t_, 2), share=(t_ / g_) if g_ else None, tips_known=bool(tips),
                                      cash_short=cs_, tips_to_pay=round(t_ - cs_, 2))

    def _stx(slug):
        """Sales tax split per deposit entry: {entry_id: {state, alc, total, estimated}}."""
        try:
            from . import salestax
            return salestax.by_entry(db(), slug)
        except Exception:  # noqa: BLE001
            return {}

    def _bd(c):
        """What each sales deposit has to cover: tips, sales tax, labor, food and beverage cost (display only)."""
        try:
            from . import salestax
            return salestax.deposit_breakdown(db(), c["slug"], c.get("shifts_db"))
        except Exception as e:  # noqa: BLE001
            app.logger.warning("deposit breakdown failed for %s: %s", c.get("slug"), e)
            return {}

    @app.route("/c/<slug>/liquor-license", methods=["POST"])
    @login_required
    def liquor_license(slug):
        """Date the restaurant starts selling alcohol under its own license (before it: the hotel's license)."""
        edit_required(slug)
        c = client_or_404(slug)
        d = _day(request.form.get("date")) or ""
        B.set_meta(db(), slug, "own_liquor_license_from", d)
        db().commit()
        from . import salestax
        salestax.apply(db(), slug, c.get("shifts_db"), c.get("tips_since", "2026-04-27"))
        log("liquor_license", user=g.user["email"], client=slug, date=d)
        flash("Saved: %s." % (("restaurant's own liquor license from " + _mdy_s(d)) if d else "the hotel's liquor license (no date)"), "ok")
        return redirect(url_for("report", slug=slug, kind="sales-tax", period=request.form.get("period") or "last_month") + "#returns")

    @app.route("/c/<slug>/tax-returns", methods=["POST"])
    @login_required
    def tax_returns_upload(slug):
        """Filed Oklahoma tax returns (OkTAP PDF copies): stored per period; payments are split by the return they pay."""
        edit_required(slug)
        from . import salestax
        saved, bad = [], []
        for f in request.files.getlist("files")[:24]:
            data = f.read(5 * 1024 * 1024 + 1)
            r = salestax.parse_return(data) if data and len(data) <= 5 * 1024 * 1024 else None
            if not r:
                bad.append(f.filename or "file")
                continue
            salestax.save_return(db(), slug, r, (f.filename or "")[:120], g.user["email"])
            saved.append("%s %s" % ("Sales tax" if r["kind"] == "sales" else "Mixed beverage", _mdy_s(r["period_end"])))
        if saved:
            salestax.allocate_payments(db(), slug)
            db().commit()
            log("tax_returns", user=g.user["email"], client=slug, n=len(saved))
            flash("Imported %d return%s: %s." % (len(saved), "" if len(saved) == 1 else "s", ", ".join(saved)), "ok")
        if bad:
            flash("Not an Oklahoma tax return PDF: %s." % ", ".join(bad))
        return redirect(url_for("report", slug=slug, kind="sales-tax", period=request.form.get("period") or "last_12") + "#returns")

    @app.route("/c/<slug>/payroll-preview", methods=["POST"])
    @login_required
    def payroll_preview(slug):
        """Paste of Gusto's payroll review table: replaces the Time Clock estimate for that pay day in the forecast."""
        edit_required(slug)
        f = request.form
        pay_day = _day(f.get("pay_day"))
        if f.get("action") == "delete" and pay_day:
            B.delete_gusto_preview(db(), slug, pay_day)
            log("gusto_preview_delete", user=g.user["email"], client=slug, pay_day=pay_day)
            flash("Gusto preview removed — the forecast is back to the Time Clock estimate.", "ok")
        elif not pay_day:
            flash("Choose the pay date.")
        else:
            try:
                parsed = B.parse_gusto_preview((f.get("text") or "")[:60000])
            except ValueError as e:
                flash(str(e))
            else:
                B.save_gusto_preview(db(), slug, pay_day, _day(f.get("start")), _day(f.get("end")), parsed, g.user["email"])
                log("gusto_preview", user=g.user["email"], client=slug, pay_day=pay_day, total=parsed["total"])
                flash("Gusto preview saved for %s: %s (gross %s + company taxes %s) from %d people." % (
                    _mdy_s(pay_day), "${:,.2f}".format(parsed["total"]), "${:,.2f}".format(parsed["gross"]),
                    "${:,.2f}".format(parsed["er_tax"]), len(parsed["rows"])), "ok")
        return redirect(url_for("client_page", slug=slug) + "#bills")

    @app.route("/c/<slug>/recurring/<int:rid>", methods=["POST"])
    @login_required
    def recurring_action(slug, rid):
        edit_required(slug)
        f = request.form
        if f.get("action") == "save":
            name = _txt(f.get("name"), 120)
            typ = (f.get("typical_amount") or "").strip()
            amt = _money_in(typ) if typ else None
            freq = f.get("frequency") if f.get("frequency") in ("weekly", "biweekly", "monthly", "quarterly", "yearly") else "monthly"
            if not name:
                flash("Enter a name.")
            else:
                B.update_recurring(db(), slug, rid, name=name, payee=_txt(f.get("payee"), 120), category=_txt(f.get("category"), 80) or None,
                                   frequency=freq, typical_amount=amt if amt and amt > 0 else None, autopay=1 if f.get("autopay") else 0)
                log("recurring_edit", user=g.user["email"], client=slug, id=rid)
                flash("Recurring payment updated.", "ok")
        elif f.get("action") == "delete":
            B.update_recurring(db(), slug, rid, active=0)
            log("recurring_remove", user=g.user["email"], client=slug, id=rid)
            flash("Recurring payment removed from upcoming bills.", "ok")
        if f.get("next") == "check":
            return redirect(url_for("recurring_check", slug=slug))
        return redirect(url_for("client_page", slug=slug) + "#bills")

    # ------------------------------------------------------------------ 1099s
    @app.route("/c/<slug>/1099", methods=["GET", "POST"])
    @login_required
    def ten99_page(slug):
        c = client_or_404(slug)
        if not c.get("books"):
            abort(404)
        from . import ten99 as N
        N.ensure(db())
        try:
            year = int(request.args.get("year") or request.form.get("year") or (dt.date.today().year))
        except ValueError:
            year = dt.date.today().year
        if request.method == "POST":
            edit_required(slug)
            f = request.form
            payee = _txt(f.get("payee"), 200)
            if not payee:
                abort(400)
            ent = f.get("entity") if f.get("entity") in dict(N.ENTITIES) else None
            st = f.get("status") if f.get("status") in ("", "filed", "not_needed") else None
            form = f.get("form") if f.get("form") in ("1099-NEC", "1099-MISC") else None
            N.save(db(), slug, payee, g.user["email"], entity=ent, w9=f.get("w9") == "1", status=st, notes=_txt(f.get("notes"), 300), form=form)
            log("1099_save", user=g.user["email"], client=slug, payee=payee)
            flash("Saved %s." % payee, "ok")
            return redirect(url_for("ten99_page", slug=slug, year=year) + "#p-" + str(zlib.crc32(payee.encode())))
        r = N.report(db(), slug, year)
        if request.args.get("format") == "csv":
            rows = [[p["payee"], p["form"], p["reportable"], p["nec"], p["misc"], dict(N.ENTITIES).get(p["entity"], p["entity"]),
                     "yes" if p["w9"] else "no", p["status"] or "", p["notes"]] for p in r["needs"]]
            return _csv("%s-1099-%d" % (slug, year), ["Payee", "Form", "Reportable", "Services (NEC)", "Rent (MISC)", "Business type",
                                                   "W-9 on file", "Status", "Notes"], rows)
        detail = None
        if request.args.get("payee"):
            detail = dict(payee=request.args["payee"], rows=N.payments(db(), slug, request.args["payee"], year))
        years = [y for (y,) in db().execute("SELECT DISTINCT CAST(substr(date,1,4) AS INTEGER) FROM entry WHERE client=? ORDER BY 1 DESC", (slug,))]
        return render_template("ten99.html", c=c, r=r, entities=N.ENTITIES, detail=detail, years=years or [year],
                               editor=bool(c.get("books")) and can_edit(slug), pid=lambda n: zlib.crc32(n.encode()))

    @app.route("/c/<slug>/recurring-check", methods=["GET", "POST"])
    @login_required
    def recurring_check(slug):
        c = client_or_404(slug)
        if not c.get("books"):
            abort(404)
        if request.method == "POST":
            edit_required(slug)
            f = request.form
            payee = _txt(f.get("payee"), 120)
            if f.get("action") == "add" and payee:
                typ = (f.get("typical_amount") or "").strip()
                amt = _money_in(typ) if typ else None
                freq = f.get("frequency") if f.get("frequency") in ("weekly", "biweekly", "monthly", "quarterly", "yearly") else "monthly"
                B.add_recurring(db(), slug, _txt(f.get("name"), 120) or payee, payee, _txt(f.get("category"), 80) or None, freq,
                                amt if amt and amt > 0 else None, bool(f.get("autopay")))
                log("recurring_add", user=g.user["email"], client=slug, payee=payee)
                flash("Added %s to recurring payments." % payee, "ok")
            elif f.get("action") in ("ignore", "unignore") and payee:
                B.ignore_recurring(db(), slug, payee, undo=f.get("action") == "unignore")
                flash(("%s is back on the list." if f.get("action") == "unignore" else "%s won't be suggested again.") % payee, "ok")
            return redirect(url_for("recurring_check", slug=slug))
        r = B.recurring_check(db(), slug)
        cats = [x["name"] for x in B.categories(db(), slug)]
        return render_template("recurring.html", c=c, r=r, cats=cats, editor=can_edit(slug))

    @app.route("/robots.txt")
    def robots_txt():
        from flask import Response
        return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")

    @app.route("/visitors")
    @login_required
    def visitors():
        if not is_admin():
            abort(404)
        from . import visits as V
        try:
            days = int(request.args.get("days", 7))
        except ValueError:
            days = 7
        days = days if days in (1, 7, 30, 90) else 7
        shifts_db = next((c.get("shifts_db") for c in clients().values() if c.get("shifts_db")), None)
        r = V.report(app.instance_path, days=days, app_filter=request.args.get("app", ""), person=request.args.get("person", ""),
                     shifts_db=shifts_db, gateway_log=V.GATEWAY_LOG, finance_users=users(), hide_bots=request.args.get("bots") == "hide",
                     lookup=os.environ.get("VISITOR_IP_LOOKUP") == "1")  # sends visitor addresses to ip-api.com only when switched on
        return render_template("visitors.html", r=r, days=days, apps=V.APPS, args=request.args)

    @app.route("/c/<slug>/settings", methods=["GET", "POST"])
    @login_required
    def client_settings(slug):
        c = client_or_404(slug)
        if not is_admin():
            abort(404)
        b = c.get("bank") or {}
        key_env = b.get("key_env") or "SHEETLINK_KEY_%s" % slug.upper().replace("-", "_")
        if request.method == "POST":
            key_env = re.sub(r"[^A-Z0-9_]", "", request.form.get("key_env", key_env).upper())[:60] or key_env
            chosen = request.form.getlist("accounts")
            allc = _read_json(app, "clients.json", {"clients": {}})
            cc = allc["clients"][slug]
            cc["bank"] = {"provider": "sheetlink", "key_env": key_env, "accounts": chosen[:10]}
            try:
                cc["days_ahead"] = max(1, min(60, int(request.form.get("days_ahead", 14))))
            except ValueError:
                pass
            if cc.get("source") == "platform":
                try:
                    cc["cleanup_since"] = dt.date.fromisoformat(request.form.get("cleanup_since", "")).isoformat()
                except ValueError:
                    pass
            _write_json(app, "clients.json", allc)
            if "salaries" in request.form:  # "Name, 50000" per line
                items = []
                for line in request.form.get("salaries", "").splitlines()[:20]:
                    m = re.match(r"^\s*(.*?)[,\s]+\$?\s*([\d,]+(?:\.\d+)?)\s*(?:/\s*(?:yr|year))?\s*$", line)
                    name, v = (m.group(1).strip(" ,"), _money_in(m.group(2).replace(",", ""))) if m else ("", None)
                    if name.strip() and v and v > 0:
                        items.append(dict(name=_txt(name, 60), annual=v))
                B.set_salaries(db(), slug, items)
            log("client_settings", user=g.user["email"], client=slug)
            flash("Settings saved.", "ok")
            return redirect(url_for("client_settings", slug=slug))
        accts, err = [], None
        if os.environ.get(key_env):
            try:
                accts = B.sheetlink_accounts(os.environ[key_env])
            except Exception as e:
                err = "Couldn't reach SheetLink (%s)." % type(e).__name__
        else:
            err = "No SheetLink key on the server named %s. Add it in cPanel → Setup Python App → Environment variables." % key_env
        salaries = "\n".join("%s, %s" % (x["name"], ("%.2f" % x["annual"]).rstrip("0").rstrip(".")) for x in B.get_salaries(db(), slug)) if c.get("books") else None
        return render_template("settings.html", c=c, key_env=key_env, accts=accts, chosen=set(b.get("accounts") or []), err=err, salaries=salaries)

    @app.route("/c/<slug>/export.xlsx")
    @login_required
    def export_xlsx(slug):
        c = client_or_404(slug)
        if not c.get("books"):
            abort(404)
        import io
        import openpyxl
        from flask import send_file
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Register"
        ws.append(["Date", "Check #", "Description", "Payee", "Category", "Amount", "Status", "Memo", "Bank ID"])
        for e in db().execute("SELECT * FROM entry WHERE client=? ORDER BY date, id", (slug,)):
            ws.append([dt.date.fromisoformat(e["date"]), e["check_no"], e["description"], e["payee"], e["category"], e["amount"],
                       e["status"], e["memo"], e["bank_txn_id"]])
        for name, q, cols in (
                ("Bills", "SELECT vendor, invoice_no, category, amount, due_date, status, paid_date, notes FROM bill WHERE client=? ORDER BY due_date",
                 ["Vendor", "Invoice #", "Category", "Amount", "Due", "Status", "Paid", "Notes"]),
                ("Recurring", "SELECT name, payee, category, frequency, typical_amount, autopay, active FROM recurring WHERE client=?",
                 ["Name", "Payee", "Category", "Frequency", "Typical amount", "Autopay", "Active"]),
                ("Categories", "SELECT name, pl_group FROM category WHERE client=? ORDER BY sort", ["Category", "P&L Group"]),
                ("Rules", "SELECT pattern, payee, category FROM rule WHERE client=? ORDER BY sort, id", ["Pattern", "Payee", "Category"]),
                ("Bank feed", "SELECT date, description, merchant, amount, check_number, pending, removed, txn_id FROM bank_txn WHERE client=? ORDER BY date",
                 ["Date", "Description", "Merchant", "Amount", "Check #", "Pending", "Dropped", "Bank ID"])):
            w = wb.create_sheet(name)
            w.append(cols)
            for r in db().execute(q, (slug,)):
                w.append(list(r))
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        log("export", user=g.user["email"], client=slug)
        return send_file(buf, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", as_attachment=True,
                         download_name="%s-books-%s.xlsx" % (slug, dt.date.today().isoformat()))

    @app.context_processor
    def _ctx_books():
        c = sso_cfg()
        return dict(can_edit_fn=can_edit, is_admin=is_admin(), today_iso=dt.date.today().isoformat(),
                    shifts_url=(c["portal"] + "/go/shifts") if c else None)

    def _save_snapshot(slug, snap):
        snap["receivedAt"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        _write_json(app, _snap_name(slug), snap)
        return snap

    @app.route("/c/<slug>/")
    @login_required
    def client_page(slug):
        c = client_or_404(slug)
        if c.get("source") == "platform":
            return platform_page(c, None)
        inc = cash_till = None
        if c.get("books") and c.get("shifts_db"):
            tips_run(c)
        s = get_snapshot(c)
        if c.get("books") and c.get("shifts_db"):
            today = dt.date.today()
            which = request.args.get("inc", "month")
            if which == "last":
                b_ = today.replace(day=1) - dt.timedelta(days=1)
                a_ = b_.replace(day=1)
            elif which == "week":
                a_, b_ = today - dt.timedelta(days=today.weekday()), today
            else:
                which, a_, b_ = "month", today.replace(day=1), today
            inc = dict(C.income_split(c["shifts_db"], db(), slug, a_, b_), which=which, tie=T.period_status(db(), slug, a_, b_))
            cash_till = C.till(db(), slug, c["shifts_db"], today)
        cats = [x["name"] for x in B.categories(db(), slug)] if c.get("books") else []
        card_pending = card_watch = None
        if c.get("books") and c.get("shifts_db"):
            try:
                card_pending = T.pending_batches(db(), slug)
            except Exception as e:
                app.logger.warning("pending batches failed for %s: %s", slug, e)
            try:
                from . import cardwatch as CW
                card_watch = CW.check(db(), slug, c.get("shifts_db"))
            except Exception as e:
                app.logger.warning("card watch failed for %s: %s", slug, e)
        proj = payroll = None
        if c.get("books") and s:
            target = _day(request.args.get("proj")) or (dt.date.today() + dt.timedelta(days=1)).isoformat()
            horizon = dt.date.today() + dt.timedelta(days=c.get("days_ahead", 14))
            try:  # payroll from the POS Time Clock (hours × rates)
                payroll = B.payroll_estimate(db(), slug, until=max(dt.date.fromisoformat(target), horizon), shifts_path=c.get("shifts_db"),
                                             anchor=c.get("pay_period_end", T.PERIOD_END))
                if not payroll["has_data"]:
                    payroll = None
            except Exception as e:
                app.logger.warning("payroll estimate failed for %s: %s", slug, e)
            if payroll:
                try:
                    _payroll_people(c, payroll)
                except Exception as e:  # noqa: BLE001
                    app.logger.warning("payroll people failed for %s: %s", slug, e)
            if payroll and s.get("billsDue") is not None:  # upcoming bills: the estimate replaces the Gusto recurring rows
                s = dict(s)
                bills = [b for b in s["billsDue"] if not (b.get("kind") == "recurring" and b.get("category") in B.PAYROLL_PAY_CATS)]
                today = dt.date.today()
                for p in payroll["pay_days"]:
                    if p["pay_day"] <= horizon.isoformat():
                        days = (dt.date.fromisoformat(p["pay_day"]) - today).days
                        bills.append({"id": None, "due": p["pay_day"], "name": "Payroll %s – %s" % (_mdy_s(p["start"]), _mdy_s(p["end"])),
                                      "amount": p["total"], "status": ("Late · not run yet (%d day%s)" % (p["days_late"], "" if p["days_late"] == 1 else "s")) if p.get("late") else "Due ≤7 days" if days <= 7 else "Upcoming",
                                      "source": "Gusto preview" if p.get("gusto") else "Estimate · Time Clock", "kind": "payroll", "payroll": p})
                bills.sort(key=lambda b: b["due"] or "9999")
                s["billsDue"] = bills
                s["billsDueTotal"] = round(sum(b["amount"] for b in bills), 2)
            taxes = []
            try:  # sales tax + alcohol tax collected last month, due by the 25th
                from . import salestax as STX
                taxes = STX.tax_due(db(), slug)
            except Exception as e:  # noqa: BLE001
                app.logger.warning("tax due failed for %s: %s", slug, e)
            if taxes and s.get("billsDue") is not None:
                s = dict(s)
                today = dt.date.today()
                bills = list(s["billsDue"])
                for t in taxes:
                    if t["due"] <= horizon.isoformat() or t["overdue"]:
                        days = (dt.date.fromisoformat(t["due"]) - today).days
                        bills.append({"id": None, "due": t["due"], "name": t["label"] + (" (rest)" if t["partial"] else ""), "amount": t["amount"],
                                      "status": "Overdue" if t["overdue"] else "Due ≤7 days" if days <= 7 else "Upcoming",
                                      "source": "Tax collected in sales deposits", "kind": "tax"})
                bills.sort(key=lambda b: b["due"] or "9999")
                s["billsDue"] = bills
                s["billsDueTotal"] = round(sum(b["amount"] for b in bills), 2)
            try:
                proj = B.projection(db(), slug, s, dt.date.fromisoformat(target), card_on_the_way=(card_pending or {}).get("total", 0.0),
                                    payroll=payroll, taxes=taxes)
            except Exception as e:
                app.logger.warning("projection failed for %s: %s", slug, e)
        billdocs = {}
        if c.get("books"):
            from . import receipts as RC
            RC.ensure(db())
            billdocs = RC.for_bills(db(), slug)
        return render_template("client.html", platform_on=bool(PD.rows(app.instance_path, c, "entities") or PD.rows(app.instance_path, c, "vendors")), c=c, s=s, show=c.get("show", {}), billdocs=billdocs, inc=inc, till=cash_till, cats=cats, card_pending=card_pending,
                               proj=proj, payroll=payroll, card_watch=card_watch)

    def _mdy_s(iso):
        d = dt.date.fromisoformat(iso)
        return "%d/%d" % (d.month, d.day)

    @app.route("/c/<slug>/cash", methods=["GET", "POST"])
    @login_required
    def cash_page(slug):
        c = client_or_404(slug)
        if not (c.get("books") and c.get("shifts_db")):
            abort(404)
        if request.method == "POST":
            if not can_edit(slug):
                abort(403)
            f = request.form
            act = f.get("action")
            if act == "start":
                d, amt = _day(f.get("date")), _money_in(f.get("amount"))
                if not d or amt is None or amt < 0:
                    flash("Enter a date and an amount.")
                else:
                    C.add_start(db(), slug, g.user["email"], d, amt, _txt(f.get("note"), 200))
                    log("cash_start", user=g.user["email"], client=slug)
                    flash("Starting till saved: %s on %s." % (money(amt), mdy(d)), "ok")
                return redirect(url_for("cash_page", slug=slug) + "#till-days")
            if act in ("count", "deposit"):
                d, amt = _day(f.get("date")), _money_in(f.get("amount"))
                if not d or amt is None or amt < 0:
                    flash("Enter a date and an amount.")
                elif act == "count":
                    C.add_count(db(), slug, g.user["email"], d, amt, _txt(f.get("note"), 200))
                    flash("Till count saved: %s on %s." % (money(amt), mdy(d)), "ok")
                else:
                    eid = f.get("entry_id")
                    C.add_deposit(db(), slug, g.user["email"], d, amt, _txt(f.get("note"), 200), int(eid) if eid and eid.isdigit() else None)
                    flash("Cash deposit saved: %s on %s." % (money(amt), mdy(d)), "ok")
                log("cash_" + act, user=g.user["email"], client=slug)
            elif act == "payout":
                from . import lingaarchive as LA
                d, amt = _day(f.get("date")), _money_in(f.get("amount"))
                if not d or amt is None or amt <= 0:
                    flash("Enter a date and an amount.")
                else:
                    LA.add_payout(db(), slug, d, amt, _txt(f.get("party"), 80), _txt(f.get("reason"), 80) or "Paid out", g.user["email"])
                    log("cash_payout", user=g.user["email"], client=slug)
                    flash("Cash taken out saved: %s on %s." % (money(amt), mdy(d)), "ok")
                return redirect(url_for("cash_page", slug=slug) + "#cash-outs")
            elif act == "delete-payout" and (f.get("id") or "").isdigit():
                from . import lingaarchive as LA
                LA.delete_payout(db(), slug, int(f["id"]))
                log("cash_payout_delete", user=g.user["email"], client=slug, id=f["id"])
                flash("Removed.", "ok")
                return redirect(url_for("cash_page", slug=slug) + "#cash-outs")
            elif act in ("stretch", "unstretch") and (f.get("entry_id") or "").isdigit():
                from . import lingaarchive as LA
                LA.set_lookback(db(), slug, int(f["entry_id"]), LA.STRETCH if act == "stretch" else None, g.user["email"])
                log("cash_" + act, user=g.user["email"], client=slug, entry_id=f["entry_id"])
                flash("That deposit now looks back %d days for cash." % LA.STRETCH if act == "stretch" else "Back to the usual 3 weeks.", "ok")
                return redirect(url_for("cash_page", slug=slug) + "#cash-outs")
            elif act in ("delete-count", "delete-deposit", "delete-start") and (f.get("id") or "").isdigit():
                C.delete_item(db(), slug, act.split("-")[1], int(f["id"]))
                log("cash_" + act, user=g.user["email"], client=slug, id=f["id"])
                flash("Removed.", "ok")
            return redirect(url_for("cash_page", slug=slug))
        from . import lingaarchive as LA
        return render_template("cash.html", c=c, t=C.till(db(), slug, c["shifts_db"]), editor=can_edit(slug),
                               today=dt.date.today().isoformat(), lc=LA.cash_vs_deposits(db(), slug),
                               da=LA.deposit_allocation(db(), slug, c["shifts_db"]), stx=_stx(slug), bd=_bd(c))

    # ------------------------------------------------------------------ receipts (holding area → OneDrive via the computer sync)
    def _receipts():
        from . import receipts as R
        R.SYNC_FILE = os.path.join(app.instance_path, "receipts_sync.json")
        R.ensure(db())
        return R

    def _graph_read(c):
        from . import receipts as R
        gcfg, folder = R.target(c, clients())
        secret = os.environ.get(gcfg["secret_env"], "") if gcfg else ""
        return gcfg, secret, folder

    def _receipt_bytes(c, r, R):
        """The file: from OneDrive once it's there (then the holding copy goes), else the holding copy."""
        from . import graph
        if r["status"] == "filed" and r["drive_path"]:
            gcfg, secret, _ = _graph_read(c)
            if gcfg and secret:
                try:
                    data = graph.download_workbook(gcfg["tenant_id"], gcfg["client_id"], secret, gcfg["drive_user"], r["drive_path"])
                    if r["local_path"]:
                        R.drop_local(db(), r["id"])
                    return data
                except Exception:  # noqa: BLE001 — OneDrive may not have synced it up yet
                    pass
        if r["local_path"] and os.path.exists(r["local_path"]):
            with open(r["local_path"], "rb") as fh:
                return fh.read()
        return None

    @app.route("/c/<slug>/receipts", methods=["GET", "POST"])
    @login_required
    def receipts_page(slug):
        c = client_or_404(slug)
        if not c.get("books"):
            abort(404)
        R = _receipts()
        _, _, folder = _graph_read(c)
        pending_dir = os.path.join(app.instance_path, "receipts_pending", slug)
        new_key = None
        if request.method == "POST":
            if not can_edit(slug):
                abort(403)
            f = request.form
            act = f.get("action")
            eid = int(f["entry_id"]) if (f.get("entry_id") or "").isdigit() else None
            if eid and not db().execute("SELECT 1 FROM entry WHERE client=? AND id=?", (slug, eid)).fetchone():
                eid = None
            fields = dict(entry_id=eid, date=_day(f.get("date")), vendor=_txt(f.get("vendor"), 80), note=_txt(f.get("note"), 200),
                          ref=_txt(f.get("ref"), 40))
            amt = _money_in(f.get("amount"))
            fields["amount"] = round(abs(amt), 2) if amt else None
            nxt = f.get("next", "")
            back = nxt if nxt.startswith("/c/%s/" % slug) else url_for("receipts_page", slug=slug)
            if act == "upload":
                ups = [u for u in request.files.getlist("file") if u and u.filename][:10]
                if not ups:
                    flash("Choose a photo or PDF of the receipt.")
                    return redirect(back)
                saved = 0
                for u in ups:
                    data = u.read(R.MAX_BYTES + 1)
                    if len(data) > R.MAX_BYTES:
                        flash("%s is over 25 MB." % _txt(u.filename, 60))
                        continue
                    try:
                        rid = R.add_file(db(), slug, g.user["email"], data, fields, folder, pending_dir,
                                         upload_name=u.filename, keep_name=f.get("naming") == "keep")
                        R.refile(db(), slug, rid, folder)
                        if f.get("as_bill") and not eid:
                            how, b = R.link_bill(db(), slug, rid, _day(f.get("due")), g.user["email"])
                            if how == "added":
                                flash("Added to upcoming bills: %s %s due %s." % (b["vendor"], money(b["amount"]), mdy(b["due_date"])), "ok")
                            elif how == "matched":
                                flash("Matched to the open bill: %s %s due %s." % (b["vendor"], money(b["amount"]), mdy(b["due_date"])), "ok")
                            else:
                                flash("Not added to bills: %s." % b)
                        x = db().execute("SELECT ref, ref_found, filename FROM receipt WHERE id=?", (rid,)).fetchone()
                        if x and x["ref_found"]:
                            flash("Reference # %s found in %s → %s" % (x["ref"], x["ref_found"], x["filename"]), "ok")
                    except ValueError as e:
                        flash("%s: %s" % (_txt(u.filename, 60), e))
                        continue
                    saved += 1
                log("receipt_upload", user=g.user["email"], client=slug, n=saved)
                if saved:
                    flash("Receipt%s saved. %s go%s into OneDrive (%s) the next time the receipts sync runs on your computer."
                          % ("" if saved == 1 else "s", "It" if saved == 1 else "They", "es" if saved == 1 else "", folder), "ok")
            elif act == "link":
                link = _txt(f.get("link"), 500)
                if not link:
                    flash("Paste a link or a folder path.")
                else:
                    R.add_link(db(), slug, g.user["email"], link, fields)
                    log("receipt_link", user=g.user["email"], client=slug)
                    flash("Receipt location saved.", "ok")
            elif act == "attach" and (f.get("id") or "").isdigit():
                m = re.search(r"#(\d+)", f.get("entry") or "")  # typed/picked in the search box: "#1033 · 11/5 · US Foods · -$75.83"
                if m and not eid and db().execute("SELECT 1 FROM entry WHERE client=? AND id=?", (slug, int(m.group(1)))).fetchone():
                    eid = int(m.group(1))
                if (f.get("entry") or "").strip() and not eid:
                    flash("Pick a transaction from the list (type a vendor, date or amount to search).")
                    return redirect(back)
                R.attach(db(), slug, int(f["id"]), eid)
                R.refile(db(), slug, int(f["id"]), folder)
                flash("Receipt attached." if eid else "Receipt detached.", "ok")
            elif act == "edit" and (f.get("id") or "").isdigit():
                res = R.update(db(), slug, int(f["id"]), dict(fields, ref=_txt(f.get("ref"), 40) or None, note=_txt(f.get("note"), 200) or None,
                                                              force_rename=f.get("rename") == "1"),
                               folder, rename=f.get("rename") == "1")
                if f.get("as_bill"):
                    how, b = R.link_bill(db(), slug, int(f["id"]), _day(f.get("due")), g.user["email"])
                    flash(("Added to upcoming bills: %s %s due %s." if how == "added" else "Matched to the open bill: %s %s due %s.")
                          % (b["vendor"], money(b["amount"]), mdy(b["due_date"])) if how else "Not added to bills: %s." % b, "ok" if how else None)
                log("receipt_edit", user=g.user["email"], client=slug, id=f["id"])
                if res:
                    msg = "Saved."
                    if res["status"] == "filed" and res["renamed"]:
                        msg += " The file in OneDrive becomes %s the next time the sync runs on your computer." % res["filename"]
                    elif res["renamed"]:
                        msg += " File name: %s." % res["filename"]
                    flash(msg, "ok")
                return redirect(back)
            elif act == "delete" and (f.get("id") or "").isdigit():
                R.delete(db(), slug, int(f["id"]))
                log("receipt_delete", user=g.user["email"], client=slug, id=f["id"])
                flash("Receipt removed from Finance (a copy already in OneDrive stays there).", "ok")
            elif act in ("skip", "unskip") and eid:
                R.skip(db(), slug, eid, g.user["email"], act == "skip")
                flash("Marked as not needing a receipt." if act == "skip" else "Back on the missing list.", "ok")
            elif act == "set-folder" and is_admin():
                new = R.safe_folder(f.get("folder"))
                if not new:
                    flash("The folder has to be inside a client folder, like Clients/Wayward/Receipts.")
                else:
                    allc = _read_json(app, "clients.json", {"clients": {}})
                    allc["clients"][slug]["receipts_folder"] = new
                    _write_json(app, "clients.json", allc)
                    moved = R.set_folder(db(), slug, new, move_waiting=bool(f.get("move_waiting")), move_filed=bool(f.get("move_filed")))
                    log("receipt_folder", user=g.user["email"], client=slug, folder=new)
                    flash("Documents now go to OneDrive › %s › <month>.%s" % (new.replace("/", " › "),
                          (" %d waiting document%s will go there too." % (moved, "" if moved == 1 else "s")) if moved else ""), "ok")
                return redirect(url_for("receipts_page", slug=slug) + "#folder")
            elif act == "sync-key" and is_admin():
                new_key = R.new_sync_key(g.user["email"])
                log("receipt_sync_key", user=g.user["email"])
            if not new_key:
                return redirect(back)
        data = R.listing(db(), slug)
        pre = None
        if (request.args.get("entry") or "").isdigit():
            e = db().execute("SELECT id, date, payee, description, amount, category, check_no FROM entry WHERE client=? AND id=?", (slug, int(request.args["entry"]))).fetchone()
            pre = dict(e) if e else None
        return render_template("receipts.html", c=c, r=data, folder=folder, editor=can_edit(slug), pre=pre, payees=B.payees(db(), slug),
                               today=dt.date.today().isoformat(), admin=is_admin(), new_key=new_key, sync=R.sync_info(),
                               base_url=request.url_root.rstrip("/").replace("http://", "https://"))

    @app.route("/c/<slug>/receipts/<int:rid>/file")
    @login_required
    def receipt_file(slug, rid):
        c = client_or_404(slug)
        if not c.get("books"):
            abort(404)
        R = _receipts()
        r = R.get(db(), slug, rid)
        if not r or r["status"] == "link":
            abort(404)
        data = _receipt_bytes(c, r, R)
        if data is None:
            abort(404)
        log("receipt_view", user=g.user["email"], client=slug, id=rid)
        resp = app.response_class(data, mimetype=r["content_type"] or "application/octet-stream")
        inline = (r["content_type"] or "").startswith(("image/", "application/pdf")) and r["content_type"] != "image/heic"
        resp.headers["Content-Disposition"] = '%s; filename="%s"' % ("inline" if inline else "attachment", re.sub(r'[^\w .,()&-]', "", r["filename"] or "receipt"))
        resp.headers["Cache-Control"] = "private, no-store"
        return resp

    @app.route("/receipts-sync.ps1")
    @login_required
    def receipts_sync_script():
        if not is_admin():
            abort(404)
        path = os.path.join(os.path.dirname(__file__), "receipts-sync.ps1")
        with open(path, "rb") as fh:
            resp = app.response_class(fh.read(), mimetype="text/plain")
        resp.headers["Content-Disposition"] = 'attachment; filename="receipts-sync.ps1"'
        return resp

    @app.route("/receipts-sync.cmd")
    @login_required
    def receipts_sync_launcher():
        if not is_admin():
            abort(404)
        with open(os.path.join(os.path.dirname(__file__), "receipts-sync.cmd"), "rb") as fh:
            resp = app.response_class(fh.read(), mimetype="application/octet-stream")
        resp.headers["Content-Disposition"] = 'attachment; filename="Receipts sync.cmd"'
        return resp

    # the computer sync (tools/receipts-sync.ps1): Authorization: Bearer <receipts sync key>
    def _sync_auth():
        R = _receipts()
        sent = request.headers.get("Authorization", "")
        sent = (sent[7:] if sent.startswith("Bearer ") else sent).strip()
        if not R.check_sync_key(sent):
            log("receipt_sync_denied")
            if sent:
                R.note_denied()
            abort(401)
        return R

    @app.route("/api/receipts/pending")
    def receipts_sync_pending():
        R = _sync_auth()
        items = R.for_sync(db())
        R.touch_sync(len(items))
        return jsonify(receipts=items)

    @app.route("/api/receipts/<int:rid>/file")
    def receipts_sync_file(rid):
        R = _sync_auth()
        r = db().execute("SELECT * FROM receipt WHERE id=? AND status='pending'", (rid,)).fetchone()
        if not r or not r["local_path"] or not os.path.exists(r["local_path"]):
            abort(404)
        with open(r["local_path"], "rb") as fh:
            return app.response_class(fh.read(), mimetype="application/octet-stream")

    @app.route("/api/receipts/moves")
    def receipts_sync_moves():
        R = _sync_auth()
        return jsonify(moves=R.for_moves(db()))

    @app.route("/api/receipts/<int:rid>/moved", methods=["POST"])
    def receipts_sync_moved(rid):
        R = _sync_auth()
        body = request.get_json(silent=True) or {}
        ok = R.mark_moved(db(), rid, str(body.get("path") or ""))
        log("receipt_moved", id=rid, ok=ok)
        return jsonify(ok=ok), (200 if ok else 400)

    @app.route("/api/receipts/<int:rid>/filed", methods=["POST"])
    def receipts_sync_filed(rid):
        R = _sync_auth()
        body = request.get_json(silent=True) or {}
        ok = R.mark_filed(db(), rid, str(body.get("path") or ""))
        log("receipt_filed", id=rid, ok=ok)
        return jsonify(ok=ok), (200 if ok else 400)

    # ------------------------------------------------------------------ journal (books step 2)
    @app.route("/c/<slug>/ledger", methods=["GET", "POST"])
    @login_required
    def ledger_page(slug):
        c = client_or_404(slug)
        if not c.get("books"):
            abort(404)
        from . import journal as J
        editor = can_edit(slug)
        if request.method == "POST":
            if not editor:
                abort(403)
            f = request.form
            act = f.get("action")
            if act == "use-journal":
                J.set_enabled(db(), slug, f.get("on") == "1")
                log("journal_reports", user=g.user["email"], client=slug, on=f.get("on"))
                flash("Reports now read the ledger." if f.get("on") == "1" else "Reports read the register again.", "ok")
            elif act == "repost":
                r = J.post_from_register(db(), slug, g.user["email"], force=True)
                flash("Re-posted %d entries." % r["posted"], "ok")
            else:
                abort(400)
            return redirect(url_for("ledger_page", slug=slug))
        try:
            posted = J.post_from_register(db(), slug, g.user["email"])
        except ValueError as e:
            return render_template("message.html", msg="The ledger couldn't be posted: %s" % e), 200
        s = get_snapshot(c)
        today = dt.date.today()
        checks, diffs = J.compare(db(), slug, s, today) if s and s.get("ledger") is not None else (0, [])
        a_ = request.args
        as_of = _day(a_.get("as_of")) or today.isoformat()
        acct = int(a_["account"]) if (a_.get("account") or "").isdigit() else None
        tb = J.trial_balance(db(), slug, as_of)
        _, conflicts = J.report_groups(db(), slug)
        return render_template("ledger.html", c=c, posted=posted, checks=checks, diffs=diffs, tb=tb, as_of=as_of,
                               acct=acct, acct_name=next((r["account"] for r in tb if r["id"] == acct), None),
                               entries=J.entries(db(), slug, acct, limit=300 if acct else 60),
                               unbalanced=J.check_balanced(db(), slug), conflicts=conflicts,
                               on=J.enabled(db(), slug), editor=editor)

    # ------------------------------------------------------------------ chart of accounts (books step 1)
    @app.route("/c/<slug>/accounts", methods=["GET", "POST"])
    @login_required
    def accounts_page(slug):
        c = client_or_404(slug)
        if not (c.get("books") or c.get("source") == "platform"):
            abort(404)
        from . import coa
        coa.ensure(db())
        editor = can_edit(slug) and (bool(c.get("books")) or is_admin())  # platform clients: Unscatter admins only
        if request.method == "POST":
            if not editor:
                abort(403)
            f = request.form
            act = f.get("action")
            user = g.user["email"]
            try:
                if act == "import-zoho":
                    up = request.files.get("file")
                    if not up or not up.filename:
                        raise ValueError("Choose the Chart_of_Accounts.csv from a Zoho Books export.")
                    raw = up.read(60_000_000)
                    usage = None
                    if up.filename.lower().endswith(".zip") or raw[:2] == b"PK":
                        try:
                            text, usage, year = coa.read_zoho_export(raw)
                        except Exception:
                            raise ValueError("Couldn't read that .zip; use the export .zip from Zoho Books (or its Chart_of_Accounts.csv).")
                        if not text:
                            raise ValueError("That .zip has no Chart_of_Accounts.csv.")
                    else:
                        text = raw.decode("utf-8-sig", errors="replace")
                    items = coa.read_zoho_csv(text)
                    new = coa.import_sources(db(), slug, "zoho", items, user)
                    made = coa.propose(db(), slug, "zoho", items, user)
                    msg = "Read %d Zoho accounts (%d new); %d clean accounts proposed." % (len(items), new, made)
                    if usage is not None:
                        n_used = coa.set_usage(db(), slug, usage, year)
                        msg += " %d of them were used in %s." % (n_used, year)
                    log("coa_import", user=user, client=slug, rows=len(items), new=new)
                    flash(msg + " Review the type changes and duplicates below.", "ok")
                elif act == "import-platform":
                    by_source, total = PD.coa_items(app.instance_path, c)
                    if not by_source:
                        raise ValueError("No chart of accounts from the platform for this client yet.")
                    new = made = 0
                    for src, items in by_source.items():
                        if items:
                            new += coa.import_sources(db(), slug, src, items, user)
                            made += coa.propose(db(), slug, src, items, user)
                    log("coa_import_platform", user=user, client=slug, rows=total, new=new)
                    flash("Read %d accounts from the platform (%d new); %d clean accounts proposed. Review the type changes "
                          "and duplicates below." % (total, new, made), "ok")
                elif act == "from-books":
                    cats, banks = coa.sources_from_books(db(), slug)
                    n1 = coa.import_sources(db(), slug, "category", cats, user)
                    n2 = coa.import_sources(db(), slug, "bank", banks, user)
                    made = coa.propose(db(), slug, "category", cats, user) + coa.propose(db(), slug, "bank", banks, user)
                    log("coa_from_books", user=user, client=slug, made=made)
                    flash("Set up from %d categories and %d bank accounts (%d new); %d clean accounts proposed." %
                          (len(cats), len(banks), n1 + n2, made), "ok")
                elif act == "map":
                    aid = int(f["account_id"]) if (f.get("account_id") or "").isdigit() else None
                    coa.set_mapping(db(), slug, f.get("source", ""), f.get("key", ""), aid, user, _txt(f.get("note"), 200) or None)
                    log("coa_map", user=user, client=slug, source=f.get("source"), key=f.get("key"), account=aid)
                    flash("Mapping saved.", "ok")
                elif act == "map-name":
                    names = coa.by_full_name(db(), slug)
                    want = (f.get("account_name") or "").strip().lower()
                    if want not in names:
                        raise ValueError("Pick an account from the list (type to search).")
                    coa.set_mapping(db(), slug, f.get("source", ""), f.get("key", ""), names[want], user)
                    log("coa_map", user=user, client=slug, source=f.get("source"), key=f.get("key"), account=names[want])
                    flash("Mapping saved.", "ok")
                elif act == "confirm":
                    coa.confirm(db(), slug, f.get("source", ""), f.get("key", ""), user)
                    flash("Confirmed.", "ok")
                elif act == "merge":
                    names = coa.by_full_name(db(), slug)
                    src = int(f["from_id"]) if (f.get("from_id") or "").isdigit() else None
                    want = (f.get("into") or "").strip().lower()
                    if not src or want not in names:
                        raise ValueError("Pick the account to merge into from the list.")
                    coa.merge(db(), slug, src, names[want], user)
                    log("coa_merge", user=user, client=slug, src=src, into=names[want])
                    flash("Merged.", "ok")
                elif act == "not-dup":
                    ids = [x for x in (f.get("ids") or "").split(",") if x.strip().isdigit()]
                    if len(ids) < 2:
                        raise ValueError("Nothing to mark.")
                    coa.not_duplicates(db(), slug, ids, user)
                    log("coa_not_dup", user=user, client=slug, ids=",".join(ids))
                    flash("Marked as different accounts; they won't be suggested again.", "ok")
                elif act == "save-account":
                    aid = int(f["id"]) if (f.get("id") or "").isdigit() else None
                    pname = (f.get("parent_name") or "").strip().lower()
                    pid = None
                    if pname:
                        names = coa.by_full_name(db(), slug)
                        if pname not in names:
                            raise ValueError("Pick the parent account from the list (or leave it empty).")
                        pid = names[pname]
                    coa.save_account(db(), slug, user, aid, _txt(f.get("name"), 120), _txt(f.get("code"), 20), f.get("type"), pid,
                                     f.get("active") == "1", _txt(f.get("description"), 300))
                    log("coa_account", user=user, client=slug, id=aid)
                    flash("Account saved.", "ok")
                else:
                    abort(400)
            except ValueError as e:
                flash(str(e))
            return redirect(url_for("accounts_page", slug=slug, show=request.args.get("show")) + (f.get("anchor") or ""))
        show = request.args.get("show", "issues")
        chart = coa.accounts(db(), slug)
        return render_template("accounts.html", c=c, show=show, summ=coa.summary(db(), slug), chart=chart,
                               platform_accounts=(PD.meta(app.instance_path, slug) or {}).get("datasets", {}).get("accounts")
                               if PD.enabled(c) and PD.allowed(c, "accounts") else None,
                               info={a["full"]: a for a in chart}, usage_year=coa.usage_year(db(), slug),
                               maps=coa.mappings(db(), slug), iss=coa.issues(db(), slug), types=coa.TYPES,
                               editor=editor)

    @app.route("/c/<slug>/checks")
    @login_required
    def checks_page(slug):
        c = client_or_404(slug)
        if not c.get("books"):
            abort(404)
        from . import checks as CK
        r = CK.report(db(), slug, c.get("shifts_db"))
        from . import lingaarchive as LA
        hc = LA.hours_check(db(), slug)
        return render_template("checks.html", c=c, r=r, hc=hc, show=request.args.get("show", "issues"), editor=can_edit(slug))

    @app.route("/c/<slug>/closeouts")
    @login_required
    def closeouts_page(slug):
        c = client_or_404(slug)
        if not (c.get("books") and c.get("shifts_db")):
            abort(404)
        from . import shiftcheck, posimport
        from . import tipcheck
        posimport.sync_from_shifts(db(), slug, c["shifts_db"])
        r = shiftcheck.check(db(), slug, c["shifts_db"], c.get("tips_since", "2026-04-27"), c.get("paper_until", tipcheck.PAPER_UNTIL))
        show = request.args.get("show", "problems")
        return render_template("closeouts.html", c=c, r=r, show=show, editor=can_edit(slug))

    @app.route("/c/<slug>/tips", methods=["GET", "POST"])
    @login_required
    def tips_page(slug):
        return _tips_page(slug)

    @app.route("/c/<slug>/card-watch", methods=["POST"])
    @login_required
    def card_watch_mark(slug):
        c = client_or_404(slug)
        if not (c.get("books") and c.get("shifts_db")) or not can_edit(slug):
            abort(403)
        from . import cardwatch as CW
        f = request.form
        day = _day(f.get("date"))
        if not day:
            abort(400)
        if f.get("action") == "undo":
            CW.undismiss(db(), slug, day)
            flash("%s is checked again." % day, "ok")
        else:
            why = {"closed": "Closed (no sales)", "checked": "Checked"}.get(f.get("why"), "Checked")
            CW.dismiss(db(), slug, day, why + ((": " + _txt(f.get("note"), 200)) if f.get("note") else ""), g.user["email"])
            log("card_watch_ok", user=g.user["email"], client=slug, day=day, why=why)
            flash("%s marked: %s." % (day, why.lower()), "ok")
        return redirect(request.referrer or url_for("tips_page", slug=slug))

    def _tips_page(slug):
        c = client_or_404(slug)
        if not (c.get("books") and c.get("shifts_db")):
            abort(404)
        if request.method == "POST":
            if not can_edit(slug):
                abort(403)
            f = request.form
            eid = f.get("entry_id", "")
            if f.get("action") == "batch":
                up = request.files.get("file")
                try:
                    if not up or not up.filename:
                        raise ValueError("Choose the Batch Report CSV exported from Linga.")
                    text = up.read(5_000_000).decode("utf-8-sig", errors="replace")
                    r = T.import_batch_report(db(), slug, text, g.user["email"])
                    tips_run(c)
                    flash("Imported %d card batches (%s to %s): %s in card payments + %s in card tips." %
                          (r["batches"], mdy(r["first"]), mdy(r["last"]), money(r["amount"]), money(r["tips"])), "ok")
                    log("batch_import", user=g.user["email"], client=slug, batches=r["batches"])
                except ValueError as e:
                    flash(str(e))
                return redirect(url_for("tips_page", slug=slug))
            if f.get("action") == "gusto":
                up = request.files.get("file")
                try:
                    if not up or not up.filename:
                        raise ValueError("Choose the Payroll Journal CSV exported from Gusto.")
                    from . import gusto as G
                    r = G.import_journal(db(), slug, up.read(5_000_000).decode("utf-8-sig", errors="replace"), g.user["email"])
                    tips_run(c)
                    flash("Imported %d Gusto pay runs (pay days %s to %s): %s paycheck tips%s." % (
                        r["runs"], mdy(r["first"]), mdy(r["last"]), money(r["tips"]),
                        (", %s cash tips" % money(r["cash_tips"])) if r["cash_tips"] else ""), "ok")
                    log("gusto_import", user=g.user["email"], client=slug, runs=r["runs"])
                except ValueError as e:
                    flash(str(e))
                return redirect(url_for("tips_page", slug=slug))
            if f.get("action") in ("archive", "archive_onedrive"):
                from . import lingaarchive as LA
                try:
                    if f.get("action") == "archive_onedrive":
                        if not is_admin():
                            abort(403)
                        from .graph import fetch_linga_archive
                        files = fetch_linga_archive(c, os.environ)
                    else:
                        files = [(u.filename, u.read(25_000_000)) for u in request.files.getlist("file") if u and u.filename]
                    if not files:
                        raise ValueError("Choose the Linga report files (you can select them all at once).")
                    res = LA.import_files(db(), slug, files)
                    tips_run(c)
                    done = [k for _, k, _ in res if k not in ("skipped", "error")]
                    errs = ["%s: %s" % (n, e) for n, k, e in res if k == "error"]
                    msg = "Linga archive: loaded %d of %d files (%s).%s" % (len(done), len(res), ", ".join(sorted(set(done))),
                                                                          (" Problems: " + "; ".join(errs)) if errs else "")
                    flash(msg) if errs else flash(msg, "ok")
                    log("linga_archive", user=g.user["email"], client=slug, files=len(res))
                except ValueError as e:
                    flash(str(e))
                except Exception as e:
                    app.logger.warning("linga archive failed for %s: %s", slug, e)
                    flash("Couldn't load the Linga archive: %s" % e)
                return redirect(url_for("tips_page", slug=slug) + "#archive")
            if f.get("action") == "processor":
                from . import processor as PR
                try:
                    days = max(1, min(62, int(f.get("days") or 7)))
                    end = dt.date.today() - dt.timedelta(days=1)
                    r = PR.pull(db(), slug, c, end - dt.timedelta(days=days - 1), end)
                    tips_run(c)
                    flash("Card processor: %d transactions, %d batches, %d business days updated." % (r["transactions"], r["batches"], r["days"]), "ok")
                    log("processor_pull", user=g.user["email"], client=slug, **r)
                except Exception as e:
                    flash("Card processor: %s" % e)
                return redirect(url_for("tips_page", slug=slug) + "#processor")
            if f.get("action") in ("pos", "pos_delete"):
                from . import posimport as PI
                try:
                    if f.get("action") == "pos_delete":
                        ok = PI.delete_import(db(), slug, int(f.get("id") or 0))
                        flash("Import removed." if ok else "That import wasn't found.", "ok" if ok else "message")
                    else:
                        ups = [u for u in request.files.getlist("file") if u and u.filename][:60]
                        text = (f.get("text") or "").strip()
                        if not ups and not text:
                            raise ValueError("Choose a file or paste the table.")
                        done = []
                        for u in ups:
                            try:
                                done.append(PI.import_data(db(), slug, f.get("source"), name=u.filename, data=u.read(20_000_000), user=g.user["email"]))
                            except ValueError as e:
                                flash("%s: %s" % (u.filename, e))
                        if text:
                            done.append(PI.import_data(db(), slug, f.get("source"), text=text[:3_000_000], user=g.user["email"]))
                        if done:
                            tips_run(c)
                        for r in done:
                            flash(r["message"], "ok")
                            log("pos_import", user=g.user["email"], client=slug, source=r["source"], rows=r["rows"])
                except ValueError as e:
                    flash(str(e))
                return redirect(url_for("tips_page", slug=slug) + "#pos")
            if f.get("action") == "salesum":
                up = request.files.get("file")
                try:
                    if not up or not up.filename:
                        raise ValueError("Choose the Sale Summary Report (.xlsx) exported from Linga.")
                    from . import salesum
                    r = salesum.import_sale_summary(db(), slug, up.read(20_000_000), g.user["email"])
                    flash("Imported %d checks over %d business days (%s to %s): %s gross, %s sales tax, %s net sales." % (
                        r["checks"], r["days"], mdy(r["first"]), mdy(r["last"]), money(r["gross"]), money(r["tax"]), money(r["net"])), "ok")
                    log("salesum_import", user=g.user["email"], client=slug, days=r["days"])
                except ValueError as e:
                    flash(str(e))
                return redirect(url_for("tips_page", slug=slug) + "#salestax")
            if f.get("action") in ("addemp", "delemp"):
                from . import tipcheck
                if f.get("action") == "addemp":
                    try:
                        n = tipcheck.add_extra(db(), slug, f.get("name", ""), g.user["email"])
                        flash("Added %s. Match their Linga/Shifts names below if that didn't happen automatically." % n, "ok")
                    except ValueError as e:
                        flash(str(e))
                else:
                    tipcheck.remove_extra(db(), slug, f.get("name", ""))
                    flash("Removed %s." % f.get("name", ""), "ok")
                return redirect(url_for("tips_page", slug=slug) + "#names")
            if f.get("action") == "tipname":
                from . import tipcheck
                emp = f.get("employee") or None
                tipcheck.set_mapping(db(), slug, f.get("source", ""), f.get("name", ""), emp, g.user["email"])
                flash("Saved: %s → %s." % (f.get("name"), {"__bar__": "Bar (split among bartenders)", "__none__": "not an employee", "__owner__": "owner (keeps own tips)"}.get(emp, emp or "unmatched")), "ok")
                return redirect(url_for("tips_page", slug=slug) + "#names")
            if f.get("action") == "linga":
                if T.linga_pulls_today(db(), slug) >= T.LINGA_PULLS_PER_DAY:
                    flash("Linga has already been pulled %d times today (the limit). Try again tomorrow." % T.LINGA_PULLS_PER_DAY)
                else:
                    tips_run(c, linga=True)
                    flash("Pulled from Linga (%d of %d today)." % (T.linga_pulls_today(db(), slug), T.LINGA_PULLS_PER_DAY), "ok")
                    log("linga_pull", user=g.user["email"], client=slug)
                return redirect(url_for("tips_page", slug=slug))
            if eid.isdigit():
                if f.get("action") == "reset":
                    T.confirm_payroll(db(), slug, int(eid), None, g.user["email"])
                    flash("Back to the automatic amount.", "ok")
                else:
                    v = (f.get("amount") or "").replace("$", "").replace(",", "").strip()
                    try:
                        amt = round(float(v), 2)
                    except ValueError:
                        amt = None
                    if amt is None or amt < 0:
                        flash("Enter the card tips paid in that payroll (0 or more).")
                    else:
                        T.confirm_payroll(db(), slug, int(eid), amt, g.user["email"])
                        flash("Saved: %s of card tips paid in that payroll." % money(amt), "ok")
                log("payroll_tips", user=g.user["email"], client=slug, entry=eid)
            return redirect(url_for("tips_page", slug=slug))
        from . import tipcheck
        t = T.report(db(), slug, c["shifts_db"], c.get("tips_since", "2026-04-27"), anchor=c.get("pay_period_end", T.PERIOD_END))
        tc = tipcheck.check(db(), slug, c["shifts_db"], c.get("tips_since", "2026-04-27"), c.get("paper_until", tipcheck.PAPER_UNTIL),
                             c.get("solo_bar", tipcheck.SOLO_BAR), c.get("tip_managers"))
        if tc:
            tc["event_tickets"] = tipcheck.event_tickets(db(), slug, c["shifts_db"], c.get("tip_managers"))
        from . import salesum
        ss = None
        try:
            per = sorted((p["start"], p["end"]) for p in (tc or {}).get("periods", []))
            if per:  # add the open pay period(s) up to today
                while per[-1][1] < dt.date.today().isoformat():
                    e = dt.date.fromisoformat(per[-1][1])
                    per.append(((e + dt.timedelta(days=1)).isoformat(), (e + dt.timedelta(days=14)).isoformat()))
            ss = salesum.summary(db(), slug, c["shifts_db"], per, c.get("tips_since", "2026-04-27"))
        except Exception as e:
            app.logger.warning("sales summary failed for %s: %s", slug, e)
        from . import lingaarchive as LA
        la = LA.status(db(), slug)
        from . import posimport as PI
        pi = PI.status(db(), slug)
        from . import processor as PR
        pr = PR.status(db(), slug, c)
        from . import cardwatch as CW
        cw = CW.check(db(), slug, c.get("shifts_db"))
        return render_template("tips.html", c=c, tc=tc, t=t, ss=ss, la=la, pi=pi, pr=pr, editor=can_edit(slug), admin=is_admin(), stx=_stx(slug), bd=_bd(c), cw=cw)

    @app.route("/c/<slug>/refresh", methods=["POST"])
    @login_required
    def client_refresh(slug):
        c = client_or_404(slug)
        if c.get("source") == "platform":
            if not bank_cfg(c):
                flash("These numbers are published by Unscatter. They update when a new summary is published.")
                return redirect(url_for("client_page", slug=slug))
            last = B.get_meta(db(), slug, "bank_synced_at")
            if last and time.time() - dt.datetime.fromisoformat(last).timestamp() < 60:
                flash("Bank synced less than a minute ago.")
            else:
                err = bank_sync(c, "manual")
                log("sync", user=g.user["email"], client=slug, ok=not err)
                flash(err or "Bank synced.", None if err else "ok")
            return redirect(request.form.get("next") or url_for("client_page", slug=slug))
        snap = _read_json(app, _snap_name(slug), None)
        if c.get("books"):
            last = B.get_meta(db(), slug, "bank_synced_at")
            snap = {"receivedAt": last} if last else None
        if snap and time.time() - dt.datetime.fromisoformat(snap["receivedAt"]).timestamp() < 60:
            flash("Synced less than a minute ago. Showing those numbers.")
        else:
            new = get_snapshot(c, force=True, by="manual")
            log("sync", user=g.user["email"], client=slug, ok=bool(new and not new.get("refreshError")))
            if new and new.get("refreshError"):
                flash(new["refreshError"])
        return redirect(url_for("client_page", slug=slug))

    @app.route("/c/<slug>/data.json")
    @login_required
    def client_data(slug):
        return jsonify(get_snapshot(client_or_404(slug)) or {})

    @app.route("/api/<slug>/snapshot", methods=["POST"])
    def push(slug):
        c = clients().get(slug)
        token = os.environ.get((c or {}).get("push_token_env", ""), "")
        sent = request.headers.get("Authorization", "")
        sent = (sent[7:] if sent.startswith("Bearer ") else sent).strip()
        if not c or len(token) < 24 or not hmac.compare_digest(sent, token):
            abort(401)
        snap = request.get_json(silent=True)
        if isinstance(snap, dict) and isinstance(snap.get("result"), dict):
            snap = snap["result"]
        if not isinstance(snap, dict) or "currentBalance" not in snap:
            abort(400)
        snap["source"] = "push"
        _save_snapshot(slug, snap)
        return jsonify(ok=True)

    # -------------------------------------------------------------- reports
    from . import reports as R

    REPORTS = [("pl", "Profit & Loss"), ("balance-sheet", "Balance Sheet"), ("cash-flow", "Cash Flow"),
               ("vendors", "Spending by Vendor"), ("sales-tax", "Sales Tax"), ("transactions", "Transactions")]

    def _csv(name, header, rows):
        import csv, io
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        w.writerows(rows)
        from flask import Response
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="%s.csv"' % name})

    @app.route("/c/<slug>/reports/")
    @login_required
    def reports_home(slug):
        return redirect(url_for("report", slug=slug, kind="pl"))

    @app.route("/c/<slug>/reports/<kind>")
    @login_required
    def report(slug, kind):
        c = client_or_404(slug)
        if not c.get("show", {}).get("reports", True) or kind not in dict(REPORTS):
            abort(404)
        s = get_snapshot(c)
        if not s or "ledger" not in s:
            return render_template("message.html", msg="Reports will appear after the next data refresh."), 200
        if c.get("books"):
            from . import journal as J
            if J.enabled(db(), slug):  # books step 2: reports read the double-entry ledger
                J.post_from_register(db(), slug, (g.get("user") or {}).get("email", "system"))
                s = J.journal_snapshot(db(), slug, s)
        today = dt.date.today()
        first = dt.date.fromisoformat(s["ledger"][0][0]) if s["ledger"] else today
        a_ = request.args
        default = {"pl": "this_month", "cash-flow": "last_12", "vendors": "last_3", "transactions": "this_month", "sales-tax": "last_month"}
        args = dict(a_.items())
        args.setdefault("period", default.get(kind, "this_month"))
        # preset changed in the dropdown: ignore the old from/to so the preset wins
        if a_.get("period") and a_.get("period") != "custom" and a_.get("prev") and a_.get("prev") != a_.get("period"):
            args.pop("from", None); args.pop("to", None)
        period, start, end = R.resolve_period(args, today, first)
        if kind in ("pl", "cash-flow"):
            start = max(start, first.replace(day=1)) if start < first else start
        ctx = dict(c=c, kind=kind, reports=REPORTS, presets=R.PRESETS, period=period, start=start, end=end,
                   args=a_, s=s, today=today, can_edit=bool(c.get("books")) and can_edit(slug))
        fmt = a_.get("format")
        name = "%s-%s-%s" % (slug, kind, end.isoformat())
        if kind == "pl":
            view = a_.get("view", "total")
            data = R.profit_and_loss(s, start, end, by_month=(view == "month"))
            if fmt == "csv":
                rows = []
                for sec in data["sections"]:
                    rows += [[sec["title"], l["label"]] + l["values"] + [l["total"]] for l in sec["lines"]]
                    rows.append([sec["title"], sec["subtotal"]] + sec["values"] + [sec["total"]])
                rows.append(["", "NET INCOME"] + data["net_income"] + [round(sum(data["net_income"]), 2)])
                return _csv(name, ["Section", "Line"] + [x["label"] for x in data["columns"]] + ["Total"], rows)
            chart, _ = R.trend_svg(s, min(end, today))
            return render_template("reports.html", data=data, view=view, chart=chart, **ctx)
        if kind == "balance-sheet":
            try:
                as_of = dt.date.fromisoformat(a_.get("as_of", ""))
            except ValueError:
                as_of = today
            data = R.balance_sheet(s, as_of)
            if fmt == "csv":
                rows = [["Assets", k, v] for k, v in data["assets"]] + [["Liabilities", k, v] for k, v in data["liabilities"]]
                rows += [["Equity", k, v] for k, v in data["equity"]]
                return _csv("%s-balance-sheet-%s" % (slug, as_of), ["Section", "Line", "Amount"], rows)
            return render_template("reports.html", data=data, as_of=as_of, **ctx)
        if kind == "cash-flow":
            data = R.cash_flow(s, start, end, by_month=a_.get("view", "month") == "month")
            if fmt == "csv":
                rows = [["", "Beginning cash"] + data["beginning"]]
                for sec in data["sections"]:
                    rows += [[sec["title"], l["label"]] + l["values"] for l in sec["lines"]]
                    rows.append([sec["title"], sec["subtotal"]] + sec["values"])
                rows += [["", "Net change"] + data["net_change"], ["", "Ending cash"] + data["ending"]]
                return _csv(name, ["Section", "Line"] + [x["label"] for x in data["columns"]], rows)
            return render_template("reports.html", data=data, view=a_.get("view", "month"), **ctx)
        if kind == "vendors":
            by = "category" if a_.get("by") == "category" else "payee"
            data = R.vendors(s, start, end, by=by, limit=0, today=today)
            if fmt == "csv":
                if by == "category":
                    return _csv(name + "-by-category", ["Category", "Payees", "Top payees", "Transactions", "Spent", "Share",
                                                         "Per week", "Per pay period (2 wks)", "Per month"],
                                [[r["name"], r["vendors"], r["top"], r["count"], r["amount"], round(r["share"], 4),
                                  r["per_week"], r["per_period"], r["per_month"]] for r in data["rows"]])
                return _csv(name, ["Payee", "Main category", "Transactions", "Spent", "Share", "Per week", "Per pay period (2 wks)", "Per month"],
                            [[r["payee"], r["category"], r["count"], r["amount"], round(r["share"], 4),
                              r["per_week"], r["per_period"], r["per_month"]] for r in data["rows"]])
            return render_template("reports.html", data=data, by=by, **ctx)
        if kind == "sales-tax":
            if not c.get("books"):
                abort(404)
            from . import salesum
            data = salesum.tax_report(db(), slug, c.get("shifts_db"), start.isoformat(), end.isoformat())
            from . import salestax
            try:
                data["owed"] = salestax.balances(db(), slug)
            except Exception:  # noqa: BLE001
                data["owed"] = None
            try:
                data["returns"] = salestax.returns_report(db(), slug, c.get("shifts_db"))
            except Exception as e:  # noqa: BLE001
                app.logger.warning("tax returns report failed for %s: %s", slug, e)
                data["returns"] = None
            if fmt == "csv":
                rows = [[x["date"], x["source"], x["gross"], x["net"], x["sales_tax"], x["liquor"], x["tax"]] for x in data["days"]]
                rows += [[x["date"], "NO TAX FIGURES (sales %.2f)" % x["gross"], "", "", "", "", ""] for x in data["missing"]]
                rows.sort(key=lambda r: r[0])
                t = data["total"]
                rows.append(["TOTAL", "%d days" % t["days"], t["gross"], t["net"], t["sales_tax"], t["liquor"], t["tax"]])
                return _csv(name, ["Date", "Source", "Gross receipts (incl. tax)", "Net sales", "Sales tax", "Mixed beverage tax", "Total tax collected"], rows)
            return render_template("reports.html", data=data, **ctx)
        data = R.transactions(s, start, end, category=a_.get("category") or None, payee=a_.get("payee") or None,
                              q=a_.get("q"), group=a_.get("group") or None)
        if fmt == "csv":
            return _csv(name, ["Date", "Check #", "Description", "Payee", "Category", "Amount", "Bank amount", "Split", "Status"],
                        [[r["date"], r["check"], r["description"], r["payee"], r["category"], r["amount"], r["bank_amount"],
                          "; ".join("%s %.2f" % (p["category"], p["amount"]) for p in r["parts"]),
                          {"open": "Not cleared", "pending": "Pending at bank"}.get(r.get("state"), "Cleared")] for r in data["rows"]])
        recs = {}
        if c.get("books"):
            from . import receipts as RC
            RC.ensure(db())
            recs = RC.for_entries(db(), slug, [r.get("id") for r in data["rows"][:500]])
        return render_template("reports.html", data=data, cats=R.categories(s), recs=recs, **ctx)

    @app.template_global()
    def drill_qs(args, extra):
        """Link into Transactions for the same period, filtered by `extra`."""
        from urllib.parse import urlencode
        d = {k: v for k, v in args.items() if k in ("period", "from", "to")}
        d.update(extra)
        return "?" + urlencode(d)

    @app.template_filter("pct")
    def pct(v):
        return "" if v is None else "{:.1f}%".format(v * 100)

    @app.template_global()
    def qs(args, **kw):
        """Current query string with some keys replaced (None removes)."""
        from urllib.parse import urlencode
        d = {k: v for k, v in args.items() if k != "format"}
        for k, v in kw.items():
            if v is None:
                d.pop(k, None)
            else:
                d[k] = v
        return "?" + urlencode(d)

    # -------------------------------------------------------------- template filters
    @app.template_filter("money")
    def money(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return ""
        return ("-$" if v < 0 else "$") + "{:,.2f}".format(abs(v))

    @app.template_filter("mdy")
    def mdy(v):
        try:
            d = dt.date.fromisoformat(str(v)[:10])
            return "%s %d" % (d.strftime("%b"), d.day)
        except ValueError:
            return v or ""

    @app.template_filter("when")
    def when(v):
        try:
            t = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            try:
                from zoneinfo import ZoneInfo
                t = t.astimezone(ZoneInfo(app.config["PORTAL_TZ"]))
            except Exception:
                pass
            return "%s %d, %d:%02d %s" % (t.strftime("%b"), t.day, (t.hour % 12) or 12, t.minute, "AM" if t.hour < 12 else "PM")
        except ValueError:
            return v or ""

    @app.errorhandler(404)
    def _404(e):
        return render_template("message.html", msg="Page not found."), 404

    @app.errorhandler(400)
    def _400(e):
        return render_template("message.html", msg="That form expired. Go back, reload the page and try again."), 400

    # -------------------------------------------------------------- clients published from the Unscatter platform
    from . import published as PUB
    from . import bankview as BV

    def platform_ingest(slug, pkg, gz, via):
        """Store a platform data package; its summary (if any) also feeds the platform pages."""
        with _lock:
            meta = PD.store(app.instance_path, slug, pkg, gz, via)
            rep = pkg.get("reports")
            if rep and rep.get("kind") == PUB.KIND:
                PUB.keep_previous(app.instance_path, slug)
                rep = dict(rep, receivedAt=meta["receivedAt"])
                _write_json(app, PUB.file_name(slug), rep)
        return meta

    def _push_auth(c):
        token = os.environ.get(PD.token_env(c) or "", "") if c else ""
        sent = request.headers.get("Authorization", "")
        sent = (sent[7:] if sent.startswith("Bearer ") else sent).strip()
        return bool(c) and PD.enabled(c) and len(token) >= 24 and hmac.compare_digest(sent, token)

    def platform_snapshot(slug):
        return _read_json(app, PUB.file_name(slug), None)

    def platform_bank(c):
        """Bank figures from SheetLink (books.db) for a platform client, or None if no bank feed is set up."""
        b = bank_cfg(c)
        if not b:
            return None
        BV.ensure(db())
        return BV.view(db(), c["slug"], b["accounts"], BV.cleanup_since(c))

    def platform_page(c, page):
        snap = platform_snapshot(c["slug"])
        bank = platform_bank(c)
        pages = PUB.visible_pages(c, snap, has_bank=bank is not None)
        if page is not None and page not in dict(pages):
            abort(404)
        a_ = request.args
        if page and a_.get("format") == "csv":
            got = PUB.csv_rows(snap, page, a_.get("table", ""), a_.get("period"), bank=bank)
            if got is None:
                abort(404)
            log("platform_csv", user=g.user["email"], client=c["slug"], page=page, table=a_.get("table"))
            return _csv("%s-%s-%s" % (c["slug"], a_.get("table") or page, (snap or {}).get("asOf", "")), *got)
        ctx = dict(c=c, s=snap, pages=pages, page=page, page_title=dict(pages).get(page), args=a_, bank=bank,
                   admin=is_admin(), has_directory=bool(PD.rows(app.instance_path, c, "entities")
                                                        or PD.rows(app.instance_path, c, "vendors")))
        if snap and page in ("sales", None) and snap.get("sales"):
            ctx["chart"] = PUB.bars_svg(snap["sales"].get("months") or [])
        if snap and page == "sales-tax" and snap.get("salesTax"):
            periods = snap["salesTax"].get("periods") or []
            detail = snap["salesTax"].get("detail") or {}
            sel = a_.get("period") if a_.get("period") in detail else (periods[0]["period"] if periods else None)
            ctx.update(period=sel, period_rows=detail.get(sel) or [],
                       detail_periods=[p for p in periods if p["period"] in detail])
        return render_template("platform.html", **ctx)

    @app.template_filter("ym")
    def ym(v):
        try:
            return dt.date(int(str(v)[:4]), int(str(v)[5:7]), 1).strftime("%b %Y")
        except (TypeError, ValueError):
            return v or ""

    @app.template_filter("longdate")
    def longdate(v):
        try:
            d = dt.date.fromisoformat(str(v)[:10])
            return "%s %d, %d" % (d.strftime("%b"), d.day, d.year)
        except ValueError:
            return v or ""

    @app.route("/c/<slug>/p/<page>")
    @login_required
    def platform_module(slug, page):
        c = client_or_404(slug)
        if not PD.enabled(c):
            abort(404)
        return platform_page(c, page)

    @app.route("/c/<slug>/p/cleanup/txn", methods=["POST"])
    @login_required
    def platform_bank_note(slug):
        """Anyone with access to the client can note what a bank transaction was; only Unscatter marks it done."""
        c = client_or_404(slug)
        if c.get("source") != "platform" or not bank_cfg(c):
            abort(404)
        BV.ensure(db())
        tid, action = request.form.get("txn_id", ""), request.form.get("action", "note")
        user = g.user["email"]
        if action == "note":
            ok = BV.save_note(db(), slug, tid, request.form.get("note", ""), user)
        elif action in ("done", "undo") and is_admin():
            ok = BV.set_done(db(), slug, tid, action == "done", user)
        else:
            abort(404)
        log("bank_note", user=user, client=slug, txn=tid, action=action, ok=ok)
        flash("Saved." if ok else "That transaction wasn't found.", "ok" if ok else None)
        return redirect(url_for("platform_module", slug=slug, page="cleanup") + "#t-" + tid)

    @app.route("/api/<slug>/platform", methods=["POST"])
    def platform_push(slug):
        """The Unscatter platform's "Publish to portal" button sends the client's summary here."""
        c = clients().get(slug)
        if not _push_auth(c):
            abort(401)
        if (request.content_length or 0) > PUB.MAX_COMPRESSED:
            return jsonify(ok=False, error="too large"), 413
        try:
            snap = PUB.parse_upload(request.get_data(cache=False), request.headers.get("Content-Encoding"))
        except PUB.Rejected as e:
            return jsonify(ok=False, error=str(e)), e.code
        with _lock:
            PUB.keep_previous(app.instance_path, slug)
            snap["receivedAt"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
            _write_json(app, PUB.file_name(slug), snap)
        log("platform_publish", client=slug, modules=snap.get("modules"), asOf=snap.get("asOf"))
        return jsonify(ok=True, receivedAt=snap["receivedAt"])

    @app.route("/api/<slug>/platform-data", methods=["POST"])
    def platform_data_push(slug):
        """The platform's "Save and send to Finance" button sends the client's clean data package here."""
        c = clients().get(slug)
        if not c or not PD.enabled(c):
            return jsonify(ok=False, error="unknown client"), 404  # JSON, so the platform doesn't fall back
        if not _push_auth(c):
            return jsonify(ok=False, error="bad token"), 401
        if (request.content_length or 0) > PD.MAX_COMPRESSED:
            return jsonify(ok=False, error="too large"), 413
        try:
            pkg, gz = PD.parse(request.get_data(cache=False), request.headers.get("Content-Encoding"))
        except PD.Rejected as e:
            return jsonify(ok=False, error=str(e)), e.code
        meta = platform_ingest(slug, pkg, gz, "push")
        log("platform_data", client=slug, asOf=meta.get("asOf"), datasets=",".join(sorted(meta["datasets"])), bytes=meta["bytes"])
        return jsonify(ok=True, receivedAt=meta["receivedAt"], datasets=sorted(meta["datasets"]))

    def platform_pull(c, by, force=False):
        """Load the package from the client's OneDrive Platform folder (if one is set). Returns a message or None."""
        if not PD.graph_cfg(c):
            return None
        m, msg = PD.pull_onedrive(app.instance_path, c, force=force)
        if m and m.get("via") == "onedrive" and m.get("receivedAt"):
            pkg = PD.load(app.instance_path, c["slug"])
            rep = (pkg or {}).get("reports")
            if rep and rep.get("kind") == PUB.KIND and (platform_snapshot(c["slug"]) or {}).get("generatedAt") != rep.get("generatedAt"):
                with _lock:
                    PUB.keep_previous(app.instance_path, c["slug"])
                    _write_json(app, PUB.file_name(c["slug"]), dict(rep, receivedAt=m["receivedAt"]))
        log("platform_pull", user=by, client=c["slug"], ok=bool(m), msg=msg)
        return msg

    @app.route("/c/<slug>/data", methods=["GET", "POST"])
    @login_required
    def data_sources(slug):
        """Admins: where each kind of data comes from for this client (the platform's clean copy or Finance's own)."""
        c = client_or_404(slug)
        if not is_admin():
            abort(404)
        if request.method == "POST":
            if not PD.enabled(c):
                abort(404)
            msg = platform_pull(c, g.user["email"], force=request.form.get("force") == "1")
            flash(msg or "No OneDrive folder is set for this client's platform data (clients.json → platform → onedrive_folder).",
                  "ok" if msg and msg.startswith(("Loaded", "Already")) else None)
            return redirect(url_for("data_sources", slug=slug))
        m = PD.meta(app.instance_path, slug) if PD.enabled(c) else None
        g_ = PD.graph_cfg(c)
        return render_template("data.html", c=c, m=m, on=PD.enabled(c), rows=PD.sources(app.instance_path, c),
                               folder=g_["folder"] if g_ else None, token_env=PD.token_env(c))

    @app.route("/c/<slug>/directory")
    @login_required
    def directory_page(slug):
        """Customers (with locations and contacts) and vendors as cleaned in the platform."""
        c = client_or_404(slug)
        if not PD.enabled(c):
            abort(404)
        customers, vendors = PD.directory(app.instance_path, c)
        if not customers and not vendors:
            abort(404)
        a_ = request.args
        q = (a_.get("q") or "").strip().lower()
        if q:
            def hit(*vals):
                return any(q in (v or "").lower() for v in vals)
            customers = [e for e in customers if hit(e.get("name"), e.get("group"), e.get("accountNumber"),
                                                      *[x.get("name") for x in e["locations"]],
                                                      *[x.get("city") for x in e["locations"]],
                                                      *[x.get("name") for x in e["contacts"]])]
            vendors = [v for v in vendors if hit(v.get("name"), v.get("company"), v.get("email"))]
        show = a_.get("show") if a_.get("show") in ("customers", "vendors") else ("customers" if customers or not vendors else "vendors")
        if a_.get("format") == "csv":
            log("directory_csv", user=g.user["email"], client=slug, show=show)
            if show == "vendors":
                return _csv("%s-vendors" % slug, ["Vendor", "Company", "Email", "Phone", "Terms", "Active"],
                            [[v.get("name"), v.get("company"), v.get("email"), v.get("phone"), v.get("terms"),
                              "yes" if v.get("active", True) else "no"] for v in vendors])
            out = []
            for e in customers:
                for loc in e["locations"] or [{}]:
                    out.append([e.get("group"), e.get("name"), e.get("accountNumber"), loc.get("name"), loc.get("address1"),
                                loc.get("city"), loc.get("state"), loc.get("zip"), loc.get("taxJurisdiction"),
                                "; ".join("%s%s" % (x.get("name") or "", " (%s)" % x["role"] if x.get("role") else "") for x in e["contacts"]),
                                "yes" if e.get("active", True) else "no"])
            return _csv("%s-customers" % slug, ["Group", "Customer", "Account #", "Location", "Address", "City", "State", "ZIP",
                                                "Tax jurisdiction", "Contacts", "Active"], out)
        m = PD.meta(app.instance_path, slug)
        return render_template("directory.html", c=c, customers=customers[:1000], vendors=vendors[:1000], show=show,
                               n_customers=len(customers), n_vendors=len(vendors), q=a_.get("q", ""), m=m)

    # used by `manage.py sync` (daily job)
    def _daily(slug):
        cc = dict(clients()[slug], slug=slug)
        pull_msg = None
        if PD.enabled(cc):
            try:
                pull_msg = platform_pull(cc, "daily")
            except Exception as e:
                app.logger.warning("platform pull failed for %s: %s", slug, e)
                pull_msg = "Platform data pull failed (%s)." % type(e).__name__
        out = _daily_rest(slug)
        if pull_msg and isinstance(out, dict):
            out = dict(out, platformData=pull_msg)
        return out

    def _daily_rest(slug):
        if clients()[slug].get("source") == "platform":  # summary is published by hand; only the bank is pulled
            c = dict(clients()[slug], slug=slug)
            err = bank_sync(c, "daily") if bank_cfg(c) else None
            out = dict(platform_snapshot(slug) or {"note": "nothing published yet"})
            if err:
                out["refreshError"] = err
            elif bank_cfg(c):
                out["bankSource"] = "SheetLink"
                B.backup(db(), app.instance_path)
            return out
        s = get_snapshot(dict(clients()[slug], slug=slug), force=True, by="daily")
        if clients()[slug].get("books"):
            B.backup(db(), app.instance_path)
        return s
    app.sync_client = _daily
    app.list_clients = lambda: list(clients())
    return app
