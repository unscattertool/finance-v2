"""Platform data package: push and OneDrive pull, per-dataset source choice, Directory, Data sources, chart import.
tests/sample_platform_data.json is a real package built by the platform's own code (platform_package.build_package),
so this test also checks the two apps agree on the format. Run: python tests/test_platformdata.py"""
import copy
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TOKEN = "platform-data-token-0123456789abcdef"
os.environ.update(PORTAL_SECRET_KEY="x" * 40, PORTAL_INSECURE_DEV="1", PUSH_TOKEN_CRS=TOKEN, PUSH_TOKEN_WWK_PLATFORM=TOKEN,
                  GRAPH_SECRET_TEST="s")
SAMPLE = json.load(open(os.path.join(ROOT, "tests", "sample_platform_data.json"), encoding="utf-8"))


def push(c, slug, pkg, token=TOKEN, raw=None):
    body = raw if raw is not None else gzip.compress(json.dumps(pkg).encode())
    return c.post("/api/%s/platform-data" % slug, data=body,
                  headers={"Authorization": "Bearer " + token, "Content-Encoding": "gzip", "Content-Type": "application/json"})


def main():
    from werkzeug.security import generate_password_hash
    inst = tempfile.mkdtemp(prefix="finance-v2-pd-")
    try:
        json.dump({"users": {"admin@example.com": {"name": "Admin", "clients": ["*"], "admin": True,
                                                   "hash": generate_password_hash("correct horse battery")},
                             "viewer@example.com": {"name": "V", "clients": ["crs"],
                                                    "hash": generate_password_hash("correct horse battery")}}},
                  open(os.path.join(inst, "users.json"), "w"))
        graph = {"tenant_id": "t", "client_id": "c", "secret_env": "GRAPH_SECRET_TEST", "drive_user": "me@example.com"}
        json.dump({"clients": {
            "wwk": {"name": "Books client", "books": True,
                    "platform": {"push_token_env": "PUSH_TOKEN_WWK_PLATFORM", "ignore": ["vendors"]}},
            "crs": {"name": "CRS", "source": "platform", "push_token_env": "PUSH_TOKEN_CRS",
                    "platform": {"onedrive_folder": "Clients/CRS/Data/Platform", "graph": graph}},
            "plain": {"name": "No platform", "books": True}}},
            open(os.path.join(inst, "clients.json"), "w"))
        from portal import create_app
        from portal import platformdata as PD
        app = create_app(inst)
        app.testing = True
        c = app.test_client()

        # ---- push: refusals
        r = push(c, "plain", SAMPLE)
        assert r.status_code == 404 and r.get_json()["error"] == "unknown client", r.status_code
        assert push(c, "nobody", SAMPLE).status_code == 404
        assert push(c, "crs", SAMPLE, token="wrong" * 6).status_code == 401
        assert push(c, "crs", dict(SAMPLE, kind="other")).status_code == 400
        assert push(c, "crs", dict(SAMPLE, version=99)).status_code == 400
        assert push(c, "crs", None, raw=b"\x1f\x8bnope").status_code == 400
        assert not os.path.exists(os.path.join(inst, PD.file_name("crs")))

        # ---- push: accepted; summary pages come from the same package
        r = push(c, "crs", SAMPLE)
        assert r.status_code == 200 and r.get_json()["ok"], r.get_data(as_text=True)
        assert "entities" in r.get_json()["datasets"]
        m = PD.meta(inst, "crs")
        assert m["via"] == "push" and m["datasets"]["invoices"]["count"] == 4 and m["asOf"] == SAMPLE["asOf"]
        assert json.load(open(os.path.join(inst, "platform_crs.json")))["sales"]["last12"] == 1700.0
        assert push(c, "crs", SAMPLE).status_code == 200
        assert os.path.exists(os.path.join(inst, "platform_data_crs.prev.json.gz"))
        # the old summary-only address still works for platform-enabled clients
        r = c.post("/api/wwk/platform", data=gzip.compress(json.dumps(SAMPLE["reports"]).encode()),
                   headers={"Authorization": "Bearer " + TOKEN, "Content-Encoding": "gzip"})
        assert r.status_code == 200, r.status_code
        # books client takes platform data too (not either/or)
        assert push(c, "wwk", SAMPLE).status_code == 200

        tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        c.post("/login", data={"email": "admin@example.com", "password": "correct horse battery", "csrf_token": tok})

        bad = []
        for u in ["/c/crs/", "/c/crs/p/sales", "/c/crs/p/sales-tax", "/c/crs/directory", "/c/crs/directory?show=vendors",
                  "/c/crs/data", "/c/wwk/data", "/c/plain/data", "/c/wwk/", "/c/wwk/p/sales", "/c/wwk/directory",
                  "/c/wwk/accounts", "/c/crs/accounts"]:
            code = c.get(u).status_code
            print(code, u)
            if code >= 400:
                bad.append(u)
        assert not bad, bad
        assert c.get("/c/plain/directory").status_code == 404 and c.get("/c/plain/p/sales").status_code == 404

        page = c.get("/c/crs/directory").get_data(as_text=True)
        assert "Acme Foods" in page and "Grocery Group" in page and "Pat Buyer" in page
        assert "Paper Co" in c.get("/c/crs/directory?show=vendors").get_data(as_text=True)
        assert "Acme Foods" not in c.get("/c/crs/directory?q=zzz").get_data(as_text=True)
        csv_ = c.get("/c/crs/directory?format=csv").get_data(as_text=True)
        assert csv_.startswith("Group,Customer") and "Acme Foods" in csv_
        # wwk ignores the platform's vendors: Finance keeps its own payees
        assert "Paper Co" not in c.get("/c/wwk/directory?show=vendors").get_data(as_text=True)
        data = c.get("/c/wwk/data").get_data(as_text=True)
        assert "platform copy turned off" in data and "Platform" in data
        assert "doesn't take data from the platform" in c.get("/c/plain/data").get_data(as_text=True)
        assert "Customers &amp; vendors (platform)" in c.get("/c/wwk/").get_data(as_text=True)
        assert "Customers &amp; vendors (platform)" not in c.get("/c/plain/").get_data(as_text=True)

        # ---- chart of accounts from the platform; Zoho-sourced accounts keep the Zoho key
        pkg = copy.deepcopy(SAMPLE)
        for i, a in enumerate(pkg["datasets"]["accounts"]["rows"]):
            if a["name"] == "Travel":
                a["source"] = {"system": "zoho_books", "id": "Z-TRAVEL"}
        assert push(c, "crs", pkg).status_code == 200
        page = c.get("/c/crs/accounts").get_data(as_text=True)
        assert "Bring in the platform" in page
        tok = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        r = c.post("/c/crs/accounts", data={"csrf_token": tok, "action": "import-platform"}, follow_redirects=True)
        body = r.get_data(as_text=True)
        assert "Read 5 accounts from the platform" in body, body[:400]
        from portal import books as B
        db = B.connect(inst)
        got = {(x["source"], x["source_key"]) for x in db.execute("SELECT source, source_key FROM account_map WHERE client='crs'")}
        assert ("zoho", "Z-TRAVEL") in got and sum(1 for s_, _ in got if s_ == "platform") == 4, got
        names = {x[0] for x in db.execute("SELECT name FROM ledger_account WHERE client='crs'")}
        assert {"Office Supplies", "Office Supply", "Travel", "Sales"} <= names, names
        r = c.post("/c/crs/accounts", data={"csrf_token": tok, "action": "import-platform"}, follow_redirects=True)
        assert "(0 new)" in r.get_data(as_text=True)

        # ---- OneDrive pull through Microsoft Graph (faked)
        from portal import graph as G
        gz = gzip.compress(json.dumps(dict(SAMPLE, asOf="2026-11-01")).encode(), mtime=0)
        files = {"Clients/CRS/Data/Platform/platform-data.json.gz": gz,
                 "Clients/CRS/Data/Platform/manifest.json": json.dumps(
                     {"kind": PD.MANIFEST_KIND, "version": 1, "file": PD.PACKAGE_FILE, "sha256": hashlib.sha256(gz).hexdigest(),
                      "asOf": "2026-11-01", "generatedAt": "2026-11-01T10:00:00+00:00"}).encode()}
        calls = []

        def fake(tenant, client_id, secret, drive_user, path):
            calls.append(path)
            assert (tenant, drive_user, secret) == ("t", "me@example.com", "s")
            return files[path]
        G.download_workbook = fake
        page = c.get("/c/crs/data").get_data(as_text=True)
        assert "Clients/CRS/Data/Platform" in page and "Check OneDrive now" in page
        tok = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        r = c.post("/c/crs/data", data={"csrf_token": tok}, follow_redirects=True)
        assert "Loaded the platform data from OneDrive" in r.get_data(as_text=True)
        m = PD.meta(inst, "crs")
        assert m["via"] == "onedrive" and m["asOf"] == "2026-11-01"
        n = len(calls)
        r = c.post("/c/crs/data", data={"csrf_token": tok}, follow_redirects=True)
        assert "Already up to date" in r.get_data(as_text=True) and len(calls) == n + 1  # manifest only
        # half-synced file: checksum doesn't match, the stored copy stays
        files["Clients/CRS/Data/Platform/platform-data.json.gz"] = gz[:-10]
        r = c.post("/c/crs/data", data={"csrf_token": tok, "force": "1"}, follow_redirects=True)
        assert "still syncing" in r.get_data(as_text=True) and PD.meta(inst, "crs")["asOf"] == "2026-11-01"
        # daily job pulls too
        files["Clients/CRS/Data/Platform/platform-data.json.gz"] = gz
        with app.test_request_context("/"):
            out = app.sync_client("crs")
        assert "platformData" in out, out

        # a viewer (not admin) sees the directory but not the data sources page
        c2 = app.test_client()
        tok = re.search(r'name="csrf_token" value="([^"]+)"', c2.get("/login").get_data(as_text=True)).group(1)
        c2.post("/login", data={"email": "viewer@example.com", "password": "correct horse battery", "csrf_token": tok})
        assert c2.get("/c/crs/directory").status_code == 200 and c2.get("/c/crs/data").status_code == 404
        assert c2.get("/c/wwk/directory").status_code == 404
        print("OK")
    finally:
        shutil.rmtree(inst, ignore_errors=True)


if __name__ == "__main__":
    main()
