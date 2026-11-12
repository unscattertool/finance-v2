"""Smoke test for finance-v2: a throwaway instance with one books client (wwk) and one platform client (crs).
Publishes a sample Platform snapshot to /api/crs/platform and opens every page. Run: python tests/smoke_test.py"""
import gzip
import json
import os
import re
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
TOKEN = "smoke-test-token-0123456789abcdef"
os.environ.update(PORTAL_SECRET_KEY="x" * 40, PORTAL_INSECURE_DEV="1", PUSH_TOKEN_CRS=TOKEN)

SNAPSHOT = {
    "kind": "unscatter-platform", "version": 1, "client": "CRS", "asOf": "2026-11-12", "generatedAt": "2026-11-12T20:00:00Z",
    "modules": ["sales_tax", "sales", "receivables", "cleanup"],
    "salesTax": {
        "periods": [{"period": "2026-10", "start": "2026-10-01", "end": "2026-10-31", "due": "2026-11-20", "states": ["CO"],
                     "liability": 100.0, "filed": 90.0, "paid": 90.0, "variance": 10.0, "jurisdictions": 2, "matched": 1,
                     "missingReturns": 1, "needsReview": 0, "status": "Needs review"}],
        "detail": {"2026-10": [{"period": "2026-10", "jurisdiction": "Denver", "code": "DEN", "state": "CO", "method": "home rule",
                                "frequency": "monthly", "liability": 60.0, "expectedFiled": 60.0, "filed": 50.0, "paid": 50.0,
                                "variance": 10.0, "taxableSales": 800.0, "status": "Variance", "due": "2026-11-20", "reviewed": False}]},
        "exceptions": [{"title": "Return missing", "severity": "High", "jurisdiction": "Aurora", "period": "2026-10", "amount": 40.0,
                        "action": "File"}],
        "exceptionsOpen": 1, "latestPeriod": "2026-10", "liabilityImportedAt": "2026-11-01", "returnsImportedAt": "2026-11-02"},
    "sales": {"months": [{"month": "2026-09", "sales": 1000.0, "invoices": 10}, {"month": "2026-10", "sales": 1200.0, "invoices": 12}],
              "last12": 2200.0, "byProduct": [{"product": "Register", "sku": "R1", "qty": 2, "sales": 900.0}],
              "byLocation": [{"group": "G", "company": "Acme", "location": "Denver", "sales": 500.0, "invoices": 3}],
              "openOrders": [{"number": "SO-1", "date": "2026-11-01", "company": "Acme", "location": "Denver", "po": "P1", "amount": 300.0,
                              "status": "Open", "shipment": None}],
              "openOrdersCount": 1, "openOrdersTotal": 300.0, "invoicesImportedAt": "2026-11-12", "ordersImportedAt": "2026-11-12"},
    "receivables": {"total": 500.0, "pastDueTotal": 200.0,
                    "buckets": [{"key": "current", "label": "Not due yet", "amount": 300.0}, {"key": "1_30", "label": "1–30 days late", "amount": 200.0}],
                    "byCompany": [{"group": "G", "company": "Acme", "total": 500.0, "invoices": 2, "current": 300.0, "1_30": 200.0,
                                   "31_60": 0, "61_90": 0, "90_plus": 0, "oldestDays": 23}],
                    "pastDue": [{"invoice": "INV-1", "date": "2026-10-01", "due": "2026-10-20", "daysLate": 23, "group": "G", "company": "Acme",
                                 "location": "Denver", "po": "", "total": 500.0, "balance": 200.0}],
                    "pastDueCount": 1, "invoicesImportedAt": "2026-11-12", "paymentsImportedAt": "2026-11-12"},
    "cleanup": {"coa": {"issues": [{"account": "T-Mobile Hot Spot Expense", "code": "", "type": "Other Current Liability",
                                    "issue": "Expense set up as a liability", "detail": ""}], "issueCounts": {"type": 1},
                        "importedAt": "2026-11-12"}, "bank": None},
}


def main():
    from werkzeug.security import generate_password_hash
    inst = tempfile.mkdtemp(prefix="finance-v2-smoke-")
    try:
        json.dump({"users": {"admin@example.com": {"name": "Admin", "clients": ["*"], "admin": True,
                                                   "hash": generate_password_hash("correct horse battery")}}},
                  open(os.path.join(inst, "users.json"), "w"))
        json.dump({"clients": {"wwk": {"name": "Books client", "books": True},
                               "crs": {"name": "CRS", "source": "platform", "push_token_env": "PUSH_TOKEN_CRS",
                                       "modules": ["sales_tax", "sales", "receivables", "cleanup"]}}},
                  open(os.path.join(inst, "clients.json"), "w"))
        from portal import create_app
        app = create_app(inst)
        app.testing = True
        c = app.test_client()
        r = c.post("/api/crs/platform", data=gzip.compress(json.dumps(SNAPSHOT).encode()),
                   headers={"Authorization": "Bearer " + TOKEN, "Content-Encoding": "gzip", "Content-Type": "application/json"})
        assert r.status_code == 200, r.get_data(as_text=True)
        tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        c.post("/login", data={"email": "admin@example.com", "password": "correct horse battery", "csrf_token": tok})
        bad = []
        for u in ["/", "/c/crs/", "/c/crs/p/sales", "/c/crs/p/sales-tax", "/c/crs/p/receivables", "/c/crs/p/cleanup", "/c/crs/settings",
                  "/c/wwk/", "/c/wwk/reports/pl", "/c/wwk/reports/sales-tax", "/c/wwk/settings", "/c/wwk/accounts", "/c/crs/accounts", "/c/crs/accounts?show=chart", "/c/crs/accounts?show=maps"]:
            code = c.get(u).status_code
            print(code, u)
            if code >= 400:
                bad.append(u)
        assert not bad, bad
        # chart: import a small Zoho chart, see the similar-name suggestion, mark it "not the same"
        import io as _io
        page = c.get("/c/crs/accounts").get_data(as_text=True)
        tok = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        csv_ = b"Account ID,Account Name,Account Type\n1,Rent,Expense\n2,Rent Expense,Expense\n"
        r = c.post("/c/crs/accounts", data={"csrf_token": tok, "action": "import-zoho", "file": (_io.BytesIO(csv_), "Chart_of_Accounts.csv")},
                   content_type="multipart/form-data", follow_redirects=True)
        page = r.get_data(as_text=True)
        assert r.status_code == 200 and "Same thing, different name" in page, page[-2000:]
        ids = re.search(r'name="action" value="not-dup"><input type="hidden" name="ids" value="([^"]+)"', page).group(1)
        r = c.post("/c/crs/accounts", data={"csrf_token": tok, "action": "not-dup", "ids": ids}, follow_redirects=True)
        assert "Same thing, different name" not in r.get_data(as_text=True)
        print("200 chart import + not-the-same")
        print("OK")
    finally:
        shutil.rmtree(inst, ignore_errors=True)


if __name__ == "__main__":
    main()
