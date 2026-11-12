# Finance v2 — roadmap

Finance v2 = the live Finance portal (finance-portal-1112c, deployed 11/12/2026) merged with the CRS
"platform client" code (finance-portal-crs.zip, 10/28/2026, never deployed). The live site
(finance.unscatterllc.com) is not touched by this project. Run it locally until it's ready.

## Where things stand (commit history)

1. Baseline — the live code exactly as deployed 11/12 (WWK books: bank register, receipts, card deposits & tips,
   sales tax report/split/returns, payroll estimate + Gusto preview, cash/till, Shifts closeout checks, reports).
2. Merge — the CRS platform code applied on top: `portal/published.py`, `portal/bankview.py`,
   `templates/platform.html`, the `/api/<slug>/platform` push route, platform pages (`/c/<slug>/p/<page>`),
   platform client settings (`cleanup_since`). The CRS fork was 10/28 build "u" + exactly these changes, so the
   merge applied cleanly; `tests/smoke_test.py` publishes a sample snapshot and opens every page of both kinds of
   client.

## Goal: Finance keeps real books (CRS and later clients)

Foundation (the Unscatter platform) takes in the client's data, cleans it and stores it as related entities:

- **Location** — every address is its own location.
- **Business entity** — unique; linked to one or more locations and one or more contacts.
- **Contact** — unique; can be linked to several business entities (and so share entities with other contacts).

Finance v2 books sit on top of those entities: invoices, bills, payments and transactions point at a business
entity (customer / vendor), optionally a location (ship-to / site) and a contact — never at free-text names.

### What the books engine needs (gaps found in the CRS Zoho export, see claude/crs-finance-fit.md)

| Area | Today (WWK) | Needed |
|---|---|---|
| Chart of accounts | flat category + P&L group | account types (bank, card, clearing, AR, AP, inventory, fixed asset, liabilities, equity, income, COGS, expense), parent/child, inactive, codes; mapping old (Zoho) account → clean account |
| Ledger | one register, one category per entry (+ splits) | double-entry journal: every transaction = lines that balance; registers per bank / card / clearing account; transfers and card payments |
| Receivables | none | invoices, customer payments, credit notes, deposits via Undeposited Funds / Stripe / Clearent clearing; aging by entity / location |
| Payables | open bills | bills with lines, vendor payments, vendor credits, recurring bills, aging |
| Inventory | none | inventory asset, purchases into stock, COGS on sale, adjustments (Foundation already imports items / POs / receives) |
| Loans | Loan Payables category | loan accounts with principal / interest split, lines of credit (BOCA #1/#2, Boyd, US Bank, Wehner) |
| Sales tax | Oklahoma (one return), POS-based | multiple states and home-rule cities (CO, NM), liability per jurisdiction, filed returns, payments (Foundation's Colorado reconciliation) |
| Payroll | Gusto estimate | payroll journal from the payroll provider, liability accounts |
| Bank feed | SheetLink (Plaid) | same, per account, with reconciliation per statement |

### Proposed order (each step usable on its own, WWK keeps working throughout)

1. **Accounts and chart** — `account` table with type/parent/code/active per client; WWK categories become
   accounts (P&L groups → types); Zoho COA import for CRS with a mapping table (old → clean).
2. **Journal core** — `journal` + `journal_line` (balanced), posted from the current entries/splits for WWK so its
   reports don't change; reports (P&L, balance sheet, cash flow) read the journal.
3. **Registers** — one register per bank/card/clearing account, transfers, reconciliation.
4. **Entities link** — customers/vendors from Foundation (business entity / location / contact ids) instead of
   payee text; payee rules map to entities.
5. **AR / AP** — invoices, bills, payments, credits, aging; import from Foundation (Zoho history).
6. **Inventory, loans, multi-jurisdiction sales tax, payroll journals.**
7. **Cut-over** — run v2 against a copy of the live database side by side, compare every WWK report, then deploy.

## Working rules

- Never deploy this folder to finance.unscatterllc.com until step 7 is signed off.
- `python tests/smoke_test.py` must pass before each commit.
- Live data stays on the server; local runs use a copy of `instance/` (not in git).
