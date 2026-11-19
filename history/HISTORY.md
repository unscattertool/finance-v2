# finance-v2 history

Git history is kept in `finance-v2.bundle` (remote tools can't write a `.git` folder). To make this folder a git
repo later: `git clone history\finance-v2.bundle tmp`, then move `tmp\.git` into this folder.

1. 5a1ac07 Baseline: live Finance portal (finance-portal-1112c, deployed 11/12/2026)
2. 8df92e6 Merge CRS platform-client portal (finance-portal-crs, 10/28) onto the live code
3. 72fc902 Smoke test (books + platform clients) and v2 roadmap
4. 094843b Books step 1: chart of accounts — clean accounts + old→new mappings, Zoho import, type fixes,
   duplicate families, merge / re-map / re-type / re-parent (portal/coa.py, /c/<slug>/accounts, tests/test_coa.py)
5. c4d2776 Local runner: "Run Finance v2.cmd" (sets up .venv, creates instance-local\ with a local admin login,
   opens http://127.0.0.1:5050)
6. c83c020 Platform client with nothing published: Chart of accounts / Client settings links for admins
7. c2b5d2d Chart of accounts: similar-name merge suggestions ("Same thing, different name" / "Similar names"),
   "Not the same" to dismiss a group
8. 73b0faa Chart of accounts: import the Zoho export .zip for usage (uses this year, last used) and show it beside codes
9. 70311a3 Books step 2 (WWK): double-entry journal posted from the register, ledger page, reports can read the journal
10. d11e46b Card check (Heartland): alert on open days with no report, card payments not in a closed batch, and late deposits (same as live 1113a)
11. bde789d Payroll not run alerts + forecast
12. b911d9c Gusto-financed payroll counts as paid
13. 1dfeb00 Sales/alcohol tax due by the 25th in bills + projection
14. d2ad76b Tax due: paid filed returns settle their month
15. 4767e8d 1099s page: payees paid $2,000+ for services or rent, W-9 and filed status
16. fba48eb Cash expected on closeouts but not turned in comes off the tips to be paid
17. f44c452 Platform data package: Finance uses the platform's clean data per dataset (OneDrive pull + push), Data sources
    page, Customers & vendors directory, chart of accounts from the platform (portal/platformdata.py, tests/test_platformdata.py)
