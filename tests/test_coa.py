"""Chart of accounts: Zoho import, proposals, type fixes, duplicates, merge, mapping by name. Run: python tests/test_coa.py"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from portal import coa  # noqa: E402

ZOHO = """Account ID,Account Name,Account Code,Description,Account Type,Account Status,Currency,Parent Account
1,Bank of Albuquerque Checking (-3660),,,Bank,Active,USD,
2,Line of Credit,,,Bank,Inactive,USD,
3,Clearent Processing,,,Bank,Active,USD,
4,Undeposited Funds,,,Cash,Active,USD,
5,Tax Payable,,,Other Current Liability,Active,USD,
6,Tax Payable - Liability,,,Other Current Liability,Active,USD,
7,Payroll Liabilities,,,Other Current Liability,Active,USD,
8,Payroll Liabilities:NM Income Tax,,,Other Current Liability,Active,USD,Payroll Liabilities
9,T-Mobile Hot Spot Expense,,,Other Current Liability,Active,USD,
10,Inventory,,,Other Current Asset,Active,USD,
11,Inventory Asset,,,Stock,Active,USD,
12,BOCA Capital Line of Credit #1,,,Other Liability,Active,USD,
13,Sales,,,Income,Active,USD,
14,Rent,6000,,Expense,Active,USD,
"""


def main():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    items = coa.read_zoho_csv(ZOHO)
    assert len(items) == 14
    assert coa.import_sources(db, "c", "zoho", items, "t") == 14
    assert coa.propose(db, "c", "zoho", items, "t") == 14
    acc = {a["full"]: a for a in coa.accounts(db, "c")}
    assert acc["Line of Credit"]["type"] == "loan", acc["Line of Credit"]
    assert acc["Clearent Processing"]["type"] == "clearing"
    assert acc["Undeposited Funds"]["type"] == "clearing"
    assert acc["T-Mobile Hot Spot Expense"]["type"] == "expense"
    assert acc["Inventory"]["type"] == "inventory" and acc["Inventory Asset"]["type"] == "inventory"
    assert acc["Payroll Liabilities:NM Income Tax"]["type"] == "payroll_liability"
    assert acc["Payroll Liabilities:NM Income Tax"]["parent_id"] == acc["Payroll Liabilities"]["id"]
    assert acc["BOCA Capital Line of Credit #1"]["type"] == "loan"
    assert acc["Tax Payable"]["type"] == "sales_tax_payable"
    assert acc["Rent"]["code"] == "6000" and not acc["Line of Credit"]["active"]
    iss = coa.issues(db, "c")
    fams = {d["family"]: d["accounts"] for d in iss["duplicates"]}
    assert fams["Inventory"] == ["Inventory", "Inventory Asset"], fams
    assert fams["Sales tax payable"] == ["Tax Payable", "Tax Payable - Liability"], fams
    assert len(iss["type_changes"]) >= 4
    # re-import keeps the mapping, proposes nothing new
    assert coa.import_sources(db, "c", "zoho", items, "t") == 0 and coa.propose(db, "c", "zoho", items, "t") == 0
    # merge Inventory into Inventory Asset
    coa.merge(db, "c", acc["Inventory"]["id"], acc["Inventory Asset"]["id"], "t")
    m = {r["source_name"]: r for r in coa.mappings(db, "c")}
    assert m["Inventory"]["target"] == "Inventory Asset" and m["Inventory"]["status"] == "confirmed"
    assert "Inventory" not in {a["full"] for a in coa.accounts(db, "c")}
    # can't merge across groups
    try:
        coa.merge(db, "c", acc["Rent"]["id"], acc["Sales"]["id"], "t")
        raise AssertionError("merged expense into income")
    except ValueError:
        pass
    # map by full name; the left-over clean account is dropped
    names = coa.by_full_name(db, "c")
    coa.set_mapping(db, "c", "zoho", "6", names["tax payable"], "t")
    assert "Tax Payable - Liability" not in {a["full"] for a in coa.accounts(db, "c")}
    # sub-account must stay in the same group
    try:
        coa.save_account(db, "c", "t", None, "Bad", None, "income", acc["Rent"]["id"], True, "")
        raise AssertionError("income under expense")
    except ValueError:
        pass
    print("OK")


if __name__ == "__main__":
    main()
