"""Journal posted from the register: balanced, same reports, follows chart merges. Run: python tests/test_journal.py
Optional: python tests/test_journal.py path/to/books.db  — runs the report check on a copy of a real database."""
import datetime as dt
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from portal import books as B, coa, journal as J, tips  # noqa: E402


def seed(db):
    B.seed_client(db, "w", "restaurant") if "restaurant" in B.STARTER else B.seed_client(db, "w")
    for name, grp in (("Sales - Card", "Revenue"), ("Food COGS", "Cost of Goods Sold"), ("Rent & Occupancy", "Operating Expenses"),
                      ("Card Tips Payable", "Not in P&L"), ("Sales Tax Payable", "Not in P&L"), ("Loan Payables", "Not in P&L"),
                      ("Payroll - Wages", "Labor")):
        db.execute("INSERT OR REPLACE INTO category(client,name,pl_group,sort) VALUES('w',?,?,0)", (name, grp))
    db.execute("INSERT INTO account(client,account_id,name,mask) VALUES('w','acc1','Checking','1234')")
    rows = [("t1", "2026-09-02", 1000.00, "Sales - Card"), ("t2", "2026-09-05", -250.10, "Food COGS"),
            ("t3", "2026-09-30", -1200.00, "Rent & Occupancy"), ("t4", "2026-10-03", 5000.00, "Loan Payables"),
            ("t5", "2026-10-09", 820.55, "Sales - Card"), ("t6", "2026-10-15", -99.99, None)]
    for tid, d, amt, cat in rows:
        db.execute("INSERT INTO bank_txn(client,txn_id,account_id,date,amount,description) VALUES('w',?,?,?,?,?)", (tid, "acc1", d, amt, "TXN " + tid))
        db.execute("INSERT INTO entry(client,date,kind,description,payee,category,amount,bank_txn_id,status) VALUES('w',?,'bank',?,?,?,?,?,'cleared')",
                   (d, "TXN " + tid, "Payee " + tid, cat or B.NEEDS, amt, tid))
    B.add_check(db, "w", "t", "2026-10-20", 300.00, "Staff", "Payroll - Wages", check_no="1001")
    tips.ensure(db, "w")
    eid = db.execute("SELECT id FROM entry WHERE bank_txn_id='t5'").fetchone()[0]
    tips.set_splits(db, "w", eid, [("Card Tips Payable", 120.25, "tips"), ("Sales Tax Payable", 45.10, "tax")], "test")
    db.commit()


def main():
    tmp = tempfile.mkdtemp()
    try:
        db = B.connect(tmp)
        seed(db)
        r = J.post_from_register(db, "w")
        assert r["posted"] == 7 and not r["skipped"], r
        assert J.check_balanced(db, "w") == []
        assert J.post_from_register(db, "w")["skipped"]
        tb = J.trial_balance(db, "w")
        assert abs(sum(x["debit"] - x["credit"] for x in tb)) < 0.005
        bank = next(x for x in tb if x["account"] == "Checking 1234")
        assert bank["debit"] == round(1000 - 250.10 - 1200 + 5000 + 820.55 - 99.99 - 300, 2), bank
        snap = B.snapshot(db, "w", today=dt.date(2026, 10, 31))
        n, diffs = J.compare(db, "w", snap, today=dt.date(2026, 10, 31))
        assert n > 5 and diffs == [], diffs
        # a register change re-posts
        db.execute("UPDATE entry SET category='Food COGS' WHERE bank_txn_id='t6'")
        db.commit()
        assert not J.post_from_register(db, "w")["skipped"]
        # merging two categories in the chart moves history and the report says so
        ids = {a["full"]: a["id"] for a in coa.accounts(db, "w")}
        coa.merge(db, "w", ids["Rent & Occupancy"], ids["Food COGS"], "t")
        J.post_from_register(db, "w")
        snap = B.snapshot(db, "w", today=dt.date(2026, 10, 31))
        n, diffs = J.compare(db, "w", snap, today=dt.date(2026, 10, 31))
        assert diffs and any(d["check"].startswith("P&L Sep 2026") for d in diffs), diffs
        js = J.journal_snapshot(db, "w", snap)
        food = sum(r[4] for r in js["ledger"] if r[3] == "Food COGS")
        assert round(food, 2) == round(-250.10 - 1200 - 99.99, 2), food
        assert "Rent & Occupancy" not in {r[3] for r in js["ledger"]}
        print("OK")
        if len(sys.argv) > 1:
            real(sys.argv[1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def real(path):
    tmp = tempfile.mkdtemp()
    try:
        shutil.copy(path, os.path.join(tmp, "books.db"))
        db = B.connect(tmp)
        for (client,) in db.execute("SELECT DISTINCT client FROM entry").fetchall():
            r = J.post_from_register(db, client, force=True)
            n, diffs = J.compare(db, client, B.snapshot(db, client))
            print(client, r, "unbalanced:", len(J.check_balanced(db, client)), "checks:", n, "differences:", len(diffs))
            for d in diffs[:10]:
                print("  ", d)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
