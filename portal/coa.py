"""Chart of accounts (books step 1).

Every client gets a clean chart: accounts with a type, an optional parent, a code and active/inactive. Whatever the
client's books came from — a Zoho chart of accounts, or Finance's own spending categories (WWK) — is kept as
*source accounts* and mapped to the clean chart, so history can be re-run through the mapping instead of being edited
in place. Several source accounts may map to one clean account (that's how duplicates get merged).

Tables (books.db):
  ledger_account  the clean chart (one row per account)
  account_map     source account (zoho id / category name / bank account id) → ledger_account
"""
import collections
import csv
import datetime as dt
import io
import re

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_account (
  id INTEGER PRIMARY KEY, client TEXT NOT NULL, name TEXT NOT NULL, code TEXT, type TEXT NOT NULL,
  parent_id INTEGER, active INTEGER NOT NULL DEFAULT 1, description TEXT, sort INTEGER DEFAULT 0,
  created_by TEXT, created_at TEXT, updated_by TEXT, updated_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ledger_account_name ON ledger_account(client, parent_id, name);
CREATE TABLE IF NOT EXISTS account_map (
  client TEXT NOT NULL, source TEXT NOT NULL, source_key TEXT NOT NULL, source_name TEXT, source_type TEXT,
  source_parent TEXT, source_code TEXT, source_active INTEGER DEFAULT 1, uses INTEGER,
  account_id INTEGER, status TEXT NOT NULL DEFAULT 'suggested', note TEXT, updated_by TEXT, updated_at TEXT,
  PRIMARY KEY (client, source, source_key));
CREATE TABLE IF NOT EXISTS account_not_dup (
  client TEXT NOT NULL, pair TEXT NOT NULL, by_user TEXT, at TEXT, PRIMARY KEY (client, pair));
"""

# clean account types: key -> (label, class, normal balance, statement section)
TYPES = {
    "bank": ("Bank", "asset", "debit", "Cash and bank"),
    "cash": ("Cash on hand", "asset", "debit", "Cash and bank"),
    "clearing": ("Clearing / undeposited", "asset", "debit", "Cash and bank"),
    "accounts_receivable": ("Accounts receivable", "asset", "debit", "Current assets"),
    "inventory": ("Inventory", "asset", "debit", "Current assets"),
    "other_current_asset": ("Other current asset", "asset", "debit", "Current assets"),
    "fixed_asset": ("Fixed asset", "asset", "debit", "Fixed assets"),
    "other_asset": ("Other asset", "asset", "debit", "Other assets"),
    "credit_card": ("Credit card", "liability", "credit", "Current liabilities"),
    "accounts_payable": ("Accounts payable", "liability", "credit", "Current liabilities"),
    "sales_tax_payable": ("Sales tax payable", "liability", "credit", "Current liabilities"),
    "payroll_liability": ("Payroll liability", "liability", "credit", "Current liabilities"),
    "other_current_liability": ("Other current liability", "liability", "credit", "Current liabilities"),
    "loan": ("Loan / line of credit", "liability", "credit", "Loans"),
    "long_term_liability": ("Long-term liability", "liability", "credit", "Long-term liabilities"),
    "equity": ("Equity", "equity", "credit", "Equity"),
    "income": ("Income", "income", "credit", "Income"),
    "other_income": ("Other income", "income", "credit", "Other income"),
    "cogs": ("Cost of goods sold", "expense", "debit", "Cost of goods sold"),
    "expense": ("Expense", "expense", "debit", "Expenses"),
    "other_expense": ("Other expense", "expense", "debit", "Other expenses"),
}
CLASS_ORDER = ["asset", "liability", "equity", "income", "expense"]
TYPE_ORDER = list(TYPES)

# Zoho Books account types -> clean type
ZOHO_TYPES = {
    "bank": "bank", "cash": "cash", "credit card": "credit_card", "payment clearing": "clearing",
    "payment clearing account": "clearing", "accounts receivable": "accounts_receivable", "stock": "inventory",
    "other current asset": "other_current_asset", "fixed asset": "fixed_asset", "other asset": "other_asset",
    "accounts payable": "accounts_payable", "other current liability": "other_current_liability",
    "other liability": "long_term_liability", "long term liability": "long_term_liability", "equity": "equity",
    "income": "income", "other income": "other_income", "cost of goods sold": "cogs", "expense": "expense",
    "other expense": "other_expense", "overseas tax payable": "sales_tax_payable",
}
# Finance (WWK) P&L groups -> clean type (Not in P&L handled by name)
PL_GROUP_TYPES = {"Revenue": "income", "Cost of Goods Sold": "cogs", "Labor": "expense", "Operating Expenses": "expense",
                  "Other Income": "other_income", "Sales Tax Remitted": "sales_tax_payable", "Needs Category": "other_current_asset"}


def ensure(db):
    db.executescript(SCHEMA)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def norm(name):
    """Name for comparing: lower case, no punctuation/® marks, no account numbers like (-3660) or '- 6082'."""
    n = (name or "").lower().replace(" ", " ")
    n = re.sub(r"[®™]", "", n)
    n = re.sub(r"\(?-?\s*\d{3,}\)?", " ", n)
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    return " ".join(n.split())


# ------------------------------------------------------------------ type checks (what looks wrong)
def suggest_type(name, source_type):
    """(clean type, reason or None). The reason says why the source type was changed."""
    n = norm(name)
    t = source_type
    if source_type in ("bank", "cash", "clearing"):
        if re.search(r"\bline of credit\b|\bloan\b", n):
            return "loan", "a line of credit / loan set up as a bank account"
        if re.search(r"\bclearing\b|\bprocessing\b|\bundeposited\b|\bbatch\b", n):
            return "clearing", "a processor / clearing account set up as a bank account" if source_type == "bank" else None
        if re.search(r"\bretention credit\b|\bgrant\b", n):
            return "other_current_asset", "a credit / grant receivable set up as a bank account"
    if source_type in ("other_current_liability", "long_term_liability"):
        if re.search(r"\bexpense\b", n) and not re.search(r"payable|accrued", n):
            return "expense", "named like an expense but set up as a liability"
        if re.search(r"\breceived\b", n) and not re.search(r"payable", n):
            return "expense", "named like a purchase (\"received\") but set up as a liability"
        if re.search(r"\bproperty tax\b", n):
            return "other_current_liability", None
        if re.search(r"\bpayroll\b|\bwithholding\b|\bunemployment\b|\bwages\b|\b94[0-4]\b|\bbenefits payable\b|\bdeductions payable\b|\bdirect deposit\b|\bincome tax\b", n):
            return "payroll_liability", None
        if re.search(r"\bsales tax\b|\btaxation and revenue\b|\bdept of revenue\b|\btax payable\b|\bcolorado sales\b", n):
            return "sales_tax_payable", None
        if re.search(r"\bline of credit\b|\bloan\b", n):
            return "loan", None
        if n in ("accounts payable a p", "accounts payable"):
            return "accounts_payable", "a second accounts payable set up as an other liability"
    if source_type in ("expense", "cogs", "other_expense") and re.search(r"\bpayable\b", n):
        return source_type, None
    if source_type == "other_current_asset" and re.search(r"\binventory\b", n):
        return "inventory", "inventory set up as an other current asset"
    return t, None


# duplicate families: accounts whose names say they hold the same thing
FAMILIES = [
    ("Sales tax payable", lambda n, t: t == "sales_tax_payable"),
    ("Inventory", lambda n, t: t == "inventory" and "inventory" in n),
    ("Accounts payable", lambda n, t: t == "accounts_payable"),
    ("Opening balance", lambda n, t: t == "equity" and "opening balance" in n or t in ("other_current_liability",) and "opening balance" in n),
    ("Owner / shareholder contributions", lambda n, t: t == "equity" and "contribution" in n),
    ("Owner / shareholder distributions", lambda n, t: t == "equity" and ("distribution" in n or "drawing" in n)),
    ("Federal payroll taxes (941)", lambda n, t: t == "payroll_liability" and "941" in n),
    ("Wages / payroll payable", lambda n, t: t == "payroll_liability" and re.search(r"wages|salaries|payroll wages|payroll tax payable|payroll tax to pay", n)),
]


# similar names: words that say nothing about what an account holds, and words that mean the same thing
_STOP = {"and", "of", "the", "to", "for", "on", "in", "a", "an", "from", "expense", "expenses", "exp", "account",
         "acct", "paid", "given", "misc", "miscellaneous", "general", "other", "non", "hand", "x"}
_STOP_BY_CLASS = {
    "income": {"income", "sale", "revenue", "received", "inc"},
    "expense": {"cost", "good", "sold", "cog", "purchased"},
    "asset": {"asset"},
    "liability": {"liability", "payable", "pay"},
    "equity": {"equity"},
}
_SAME = {"automobile": "car", "auto": "car", "vehicle": "car", "telephone": "phone", "cellular": "phone",
         "cell": "phone", "freight": "shipping", "charity": "charitable", "earned": "", "salary": "wage",
         "fee": "charge", "recievable": "receivable", "txble": "taxable", "dept": "department",
         "reimbursment": "reimbursement", "contractor": "contract", "consultant": "contract", "consulting": "contract",
         "dues": "subscription", "stationery": "printing", "photocopying": "printing", "copying": "printing",
         "inking": "ink", "taxe": "tax", "nm": "new mexico", "withholding": "income tax", "withhold": "income tax"}
# a name with one of these and a name without are opposites, not duplicates (Discounts on Labor vs Labor Sales)
# too general to call two accounts duplicates on their own (Other Charges vs Shipping Charge, Taxes vs Payroll Taxes)
_GENERIC = {"charge", "service", "tax", "payment", "supply", "purchase", "office", "reimbursement", "loan", "payroll",
            "employee", "income", "sale", "labor", "deposit", "insurance", "receivable", "clearing"}
_CONTRA = {"discount", "refund", "return", "rebate", "allowance", "shrinkage", "write", "bad", "void", "fraud"}


def _stem(w):
    if w in _SAME:
        return _SAME[w]
    for suf, rep in (("ies", "y"), ("ses", "s"), ("s", "")):
        if len(w) > 3 and w.endswith(suf) and not w.endswith("ss"):
            w = w[: -len(suf)] + rep
            break
    return _SAME.get(w, w)


def name_words(name, cls):
    """The words that say what an account holds: plurals and synonyms folded, filler words dropped. Account numbers
    (5119, #2, 1.5) are kept, so cards / loans / lines that differ only by number are not called duplicates."""
    raw = (name or "").replace("'", "").replace("’", "")
    nums = {"#" + x for x in re.findall(r"\d+(?:\.\d+)?", raw)}
    n = re.sub(r"[^a-z ]+", " ", raw.lower().replace(":", " "))
    stop = _STOP | _STOP_BY_CLASS.get(cls, set())
    out = set()
    for w in n.split():
        for x in _stem(w).split():
            if x and x not in stop:
                out.add(x)
    return frozenset(out | nums)


def _pairkey(i, j):
    return f"{min(i, j)}-{max(i, j)}"


def similar_groups(acc, dismissed=()):
    """Accounts in the same class (asset, liability, ...) whose names say the same thing.

    'same'      same type and the words match once filler / plurals / synonyms are folded
                (Rent ~ Rent Expense, Automobile Expense ~ Car Expense, Taxes paid:Property taxes ~ Taxes:Property Tax Paid)
    'possible'  one name is the other plus one word (Advertising ~ Advertising And Marketing), the same words under a
                different parent (Postage ~ Office Expense:Shipping & postage), or a 'same' match across types
                (Interest Income ~ Interest earned). Never an account and its own parent / sub-account; sub-accounts
                of one parent are compared without the parent's words (Supply Sales:Misc Supply Sales is not a
                duplicate of Supply Sales:Inking Sales).
    `dismissed` holds 'id-id' pairs marked "not the same"; a group whose pairs are all dismissed is left out.
    Returns [(kind, [ids])]."""
    cls = {i: TYPES.get(a["type"], ("", "?"))[1] for i, a in acc.items()}
    full = {i: name_words(full_name(acc, a), cls[i]) for i, a in acc.items()}
    leaf = {i: name_words(a["name"], cls[i]) for i, a in acc.items()}
    anc = {}
    for i in acc:
        seen, p = set(), acc[i].get("parent_id")
        while p and p in acc and p not in seen:
            seen.add(p)
            p = acc[p].get("parent_id")
        anc[i] = seen
    dismissed = set(dismissed)

    def kind(i, j):
        if cls[i] != cls[j] or i in anc[j] or j in anc[i] or _pairkey(i, j) in dismissed:
            return None
        pi, pj = acc[i].get("parent_id"), acc[j].get("parent_id")
        if pi and pi == pj:
            pw = full.get(pi, frozenset())
            wi, wj = leaf[i] - pw, leaf[j] - pw
        else:
            wi, wj = full[i], full[j]
        if not wi or not wj or ((wi ^ wj) & _CONTRA):
            return None
        same_type = acc[i]["type"] == acc[j]["type"]
        if wi == wj:
            return "same" if same_type else "possible"
        if not same_type:
            return None
        def sub(a, b):  # b is a plus one word, and a says something specific
            return a < b and len(b) - len(a) == 1 and bool(a - _GENERIC)
        if sub(wi, wj) or sub(wj, wi):
            return "possible"
        if not (pi and pi == pj):
            li, lj = leaf[i], leaf[j]
            if (pi and not pj and (wj == li or sub(wj, li))) or (pj and not pi and (wi == lj or sub(wi, lj))):
                return "possible"
        return None

    ids = sorted(acc)
    root = {i: i for i in ids}

    def find(i):
        while root[i] != i:
            root[i] = root[root[i]]
            i = root[i]
        return i
    poss = []
    for x, i in enumerate(ids):
        for j in ids[x + 1:]:
            k = kind(i, j)
            if k == "same":
                root[find(j)] = find(i)
            elif k == "possible":
                poss.append((i, j))
    groups = collections.defaultdict(list)
    for i in ids:
        groups[find(i)].append(i)
    out = [("same", g) for g in groups.values() if len(g) > 1]
    # possible: cluster around each strong group / account, but don't chain (A~B and B~C doesn't make A~C)
    near = collections.defaultdict(set)
    for i, j in poss:
        # not when one already matches the other's parent (Taxes ~ Taxes paid, so not ~ Taxes paid:Property taxes)
        if {find(p) for p in anc[j]} & {find(i)} or {find(p) for p in anc[i]} & {find(j)}:
            continue
        if find(i) != find(j):
            near[find(i)].add(find(j))
            near[find(j)].add(find(i))
    shown = set()
    for r in sorted(near, key=lambda r: -len(near[r])):
        members = frozenset([r] + sorted(near[r]))
        if members in shown or any(members <= m for m in shown):
            continue
        shown.add(members)
    for m in shown:
        g = sorted(x for r in m for x in groups[r])
        out.append(("possible", g))
    return out


# ------------------------------------------------------------------ source accounts
def read_zoho_csv(text):
    """Zoho Books "Chart of Accounts" export (or COA_ActiveAccounts) -> list of source dicts."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        raise ValueError("The file has no rows.")
    keys = {k.strip().lower(): k for k in rows[0].keys()}
    def col(*names):
        for n in names:
            if n in keys:
                return keys[n]
        return None
    c_id, c_name, c_type = col("account id", "account_id"), col("account name"), col("account type")
    c_code, c_status, c_parent = col("account code"), col("account status"), col("parent account", "parent account name")
    c_desc = col("description")
    if not (c_name and c_type):
        raise ValueError("This doesn't look like a Zoho chart of accounts (needs Account Name and Account Type columns).")
    out = []
    for r in rows:
        name = (r.get(c_name) or "").strip()
        if not name:
            continue
        ztype = (r.get(c_type) or "").strip()
        out.append(dict(key=(r.get(c_id) or name).strip(), name=name, ztype=ztype,
                        type=ZOHO_TYPES.get(ztype.lower(), "expense" if not ztype else "other_current_asset"),
                        code=(r.get(c_code) or "").strip() or None,
                        active=0 if (c_status and (r.get(c_status) or "").strip().lower() == "inactive") else 1,
                        parent=(r.get(c_parent) or "").strip() or None, description=(r.get(c_desc) or "").strip() if c_desc else ""))
    return out


def import_sources(db, client, source, items, user):
    """Store/refresh source accounts. Existing mappings are kept (a re-import only updates names/types/usage)."""
    ensure(db)
    ts = now()
    n_new = 0
    for it in items:
        have = db.execute("SELECT 1 FROM account_map WHERE client=? AND source=? AND source_key=?", (client, source, it["key"])).fetchone()
        if have:
            db.execute("""UPDATE account_map SET source_name=?, source_type=?, source_parent=?, source_code=?, source_active=?,
                          uses=coalesce(?, uses) WHERE client=? AND source=? AND source_key=?""",
                       (it["name"], it.get("ztype") or it["type"], it.get("parent"), it.get("code"), it.get("active", 1), it.get("uses"),
                        client, source, it["key"]))
        else:
            n_new += 1
            db.execute("""INSERT INTO account_map(client, source, source_key, source_name, source_type, source_parent, source_code,
                          source_active, uses, status, updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?,'new',?,?)""",
                       (client, source, it["key"], it["name"], it.get("ztype") or it["type"], it.get("parent"), it.get("code"),
                        it.get("active", 1), it.get("uses"), user, ts))
    db.commit()
    return n_new


def _leaf(name):
    return name.split(":")[-1].strip() if ":" in name else name.strip()


def propose(db, client, source, items, user):
    """Build the clean chart from source accounts that aren't mapped yet: one clean account per source account (same
    name, corrected type, same parent), so nothing is lost. Merging duplicates is then a mapping change in the UI."""
    ensure(db)
    ts = now()
    by_key = {it["key"]: it for it in items}
    created = 0
    # parents first so children can point at them
    order = sorted(items, key=lambda it: (it.get("parent") is not None, it["name"].count(":"), it["name"]))
    name_to_id = {}
    for it in order:
        m = db.execute("SELECT account_id, status FROM account_map WHERE client=? AND source=? AND source_key=?",
                       (client, source, it["key"])).fetchone()
        if m and m["account_id"]:
            name_to_id[it["name"]] = m["account_id"]
            continue
        typ, reason = suggest_type(it["name"], it["type"])
        parent_id = name_to_id.get(it.get("parent")) if it.get("parent") else None
        if parent_id is None and ":" in it["name"]:
            parent_id = name_to_id.get(it["name"].rsplit(":", 1)[0])
        leaf = _leaf(it["name"])
        row = db.execute("SELECT id FROM ledger_account WHERE client=? AND coalesce(parent_id,0)=? AND name=?",
                         (client, parent_id or 0, leaf)).fetchone()
        if row:
            aid = row["id"]
        else:
            cur = db.execute("""INSERT INTO ledger_account(client, name, code, type, parent_id, active, description, created_by, created_at)
                                VALUES (?,?,?,?,?,?,?,?,?)""",
                             (client, leaf, it.get("code"), typ, parent_id, it.get("active", 1), it.get("description") or None, user, ts))
            aid = cur.lastrowid
            created += 1
        name_to_id[it["name"]] = aid
        db.execute("""UPDATE account_map SET account_id=?, status='suggested', note=?, updated_by=?, updated_at=?
                      WHERE client=? AND source=? AND source_key=?""",
                   (aid, ("Type changed: " + reason) if reason else None, user, ts, client, source, it["key"]))
    db.commit()
    return created


# ------------------------------------------------------------------ WWK-style books: categories + bank accounts
def sources_from_books(db, client):
    """Finance's own categories (with P&L groups) and SheetLink bank accounts as source accounts."""
    items = []
    uses = {r[0]: r[1] for r in db.execute("SELECT category, count(*) FROM entry WHERE client=? AND status!='void' GROUP BY category", (client,))}
    for r in db.execute("SELECT name, pl_group FROM category WHERE client=? ORDER BY sort, name", (client,)):
        g = r["pl_group"] or "Needs Category"
        n = norm(r["name"])
        if g == "Not in P&L" or g == "Sales Tax Remitted":
            if re.search(r"tips payable", n):
                t = "other_current_liability"
            elif re.search(r"sales tax|alc sales", n):
                t = "sales_tax_payable"
            elif re.search(r"loan", n):
                t = "loan"
            elif re.search(r"owner|investor|contribution|draw|distribution", n):
                t = "equity"
            elif re.search(r"transfer", n):
                t = "clearing"
            else:
                t = "other_current_liability"
        elif g == "Needs Category":
            t = "other_current_asset"
        else:
            t = PL_GROUP_TYPES.get(g, "expense")
        items.append(dict(key=r["name"], name=r["name"], ztype=g, type=t, code=None, active=1, parent=None, uses=uses.get(r["name"], 0)))
    bank = []
    for r in db.execute("SELECT account_id, name, mask, institution FROM account WHERE client=?", (client,)):
        nm = (r["name"] or "Bank account") + (" " + r["mask"] if r["mask"] else "")
        t = "credit_card" if re.search(r"card|visa|mastercard|amex", (r["name"] or ""), re.I) else "bank"
        bank.append(dict(key=r["account_id"], name=nm, ztype="Bank feed", type=t, code=r["mask"], active=1, parent=None))
    return items, bank


# ------------------------------------------------------------------ reads / edits
def accounts(db, client):
    ensure(db)
    rows = [dict(r) for r in db.execute("SELECT * FROM ledger_account WHERE client=?", (client,))]
    byid = {r["id"]: r for r in rows}
    kids = collections.defaultdict(list)
    for r in rows:
        kids[r["parent_id"]].append(r)
    mapped = collections.Counter(r[0] for r in db.execute("SELECT account_id FROM account_map WHERE client=? AND account_id IS NOT NULL", (client,)))
    out = []

    def walk(parent, depth):
        for r in sorted(kids.get(parent, []), key=lambda x: (TYPE_ORDER.index(x["type"]) if x["type"] in TYPE_ORDER else 99, x["code"] or "", x["name"].lower())):
            r.update(depth=depth, full=full_name(byid, r), label=TYPES.get(r["type"], (r["type"],))[0], cls=TYPES.get(r["type"], ("", "?"))[1], sources=mapped.get(r["id"], 0),
                     children=len(kids.get(r["id"], [])))
            out.append(r)
            walk(r["id"], depth + 1)
    walk(None, 0)
    return out


def mappings(db, client):
    ensure(db)
    acc = {r["id"]: dict(r) for r in db.execute("SELECT * FROM ledger_account WHERE client=?", (client,))}
    out = []
    for r in db.execute("SELECT * FROM account_map WHERE client=? ORDER BY source, source_name", (client,)):
        r = dict(r)
        a = acc.get(r["account_id"])
        r["target"] = full_name(acc, a) if a else None
        r["target_type"] = a["type"] if a else None
        out.append(r)
    return out


def full_name(acc, a):
    parts = [a["name"]]
    p = a.get("parent_id")
    seen = set()
    while p and p in acc and p not in seen:
        seen.add(p)
        parts.append(acc[p]["name"])
        p = acc[p].get("parent_id")
    return ":".join(reversed(parts))


def issues(db, client):
    """What needs a decision: type changes, duplicate families, unmapped and inactive-but-used source accounts."""
    ensure(db)
    allacc = {r["id"]: dict(r) for r in db.execute("SELECT * FROM ledger_account WHERE client=?", (client,))}
    acc = {i: a for i, a in allacc.items() if a["active"]}
    out = dict(type_changes=[], duplicates=[], unmapped=[], inactive_used=[], same_name=[])
    for r in db.execute("SELECT * FROM account_map WHERE client=?", (client,)):
        if not r["account_id"]:
            out["unmapped"].append(dict(r))
        elif (r["note"] or "").startswith("Type changed"):
            t = allacc.get(r["account_id"])
            out["type_changes"].append(dict(r, target=full_name(allacc, t) if t else None,
                                            target_type=TYPES.get(t["type"], (t["type"],))[0] if t else None))
        if not r["source_active"] and (r["uses"] or 0) > 0:
            out["inactive_used"].append(dict(r))
    fam = collections.defaultdict(list)
    names = collections.defaultdict(list)
    for a in acc.values():
        n = norm(a["name"])
        names[(a["type"], n)].append(a)
        for label, test in FAMILIES:
            if test(n, a["type"]):
                fam[label].append(a)
                break
    for label, lst in fam.items():
        top = [a for a in lst if not (a["parent_id"] and acc.get(a["parent_id"]) in lst)]
        if len(top) > 1:
            out["duplicates"].append(dict(family=label, accounts=sorted((full_name(acc, a) for a in top), key=str.lower)))
    in_family = [set(d["accounts"]) for d in out["duplicates"]]
    for (t, n), lst in names.items():
        names_ = [full_name(acc, a) for a in lst]
        if len(lst) > 1 and not any(set(names_) <= f for f in in_family):
            out["same_name"].append(dict(type=t, accounts=names_))
            in_family.append(set(names_))
    # similar names (Rent ~ Rent Expense, Advertising ~ Advertising And Marketing)
    dismissed = [r["pair"] for r in db.execute("SELECT pair FROM account_not_dup WHERE client=?", (client,))]
    similar = []
    for kind, ids in similar_groups(allacc, dismissed):
        if not any(allacc[i]["active"] for i in ids):
            continue
        names_ = [full_name(allacc, allacc[i]) + ("" if allacc[i]["active"] else " (inactive)") for i in ids]
        plain = {full_name(allacc, allacc[i]) for i in ids}
        if any(plain <= f for f in in_family):
            continue
        similar.append(dict(kind=kind, ids=ids, accounts=names_, plain=sorted(plain, key=str.lower)))
    out["similar"] = sorted(similar, key=lambda d: (d["kind"] != "same", d["plain"][0].lower()))
    return out


def not_duplicates(db, client, ids, user):
    """Mark every pair in `ids` as "not the same" so the group stops being suggested."""
    ensure(db)
    ids = sorted({int(i) for i in ids})
    for x, i in enumerate(ids):
        for j in ids[x + 1:]:
            db.execute("INSERT OR REPLACE INTO account_not_dup (client, pair, by_user, at) VALUES (?,?,?,?)",
                       (client, _pairkey(i, j), user, now()))
    db.commit()


def set_mapping(db, client, source, key, account_id, user, note=None):
    ensure(db)
    if account_id and not db.execute("SELECT 1 FROM ledger_account WHERE client=? AND id=?", (client, account_id)).fetchone():
        raise ValueError("Unknown account.")
    db.execute("""UPDATE account_map SET account_id=?, status='confirmed', note=coalesce(?, note), updated_by=?, updated_at=?
                  WHERE client=? AND source=? AND source_key=?""", (account_id, note, user, now(), client, source, key))
    drop_unused(db, client)
    db.commit()


def drop_unused(db, client):
    """Remove clean accounts nothing maps to and nothing hangs under (left behind after merging)."""
    while True:
        cur = db.execute("""DELETE FROM ledger_account WHERE client=? AND id NOT IN (SELECT account_id FROM account_map
                            WHERE client=? AND account_id IS NOT NULL) AND id NOT IN (SELECT parent_id FROM ledger_account
                            WHERE client=? AND parent_id IS NOT NULL) AND created_by != 'manual'""", (client, client, client))
        if not cur.rowcount:
            break


def save_account(db, client, user, aid, name, code, typ, parent_id, active, description):
    ensure(db)
    if typ not in TYPES:
        raise ValueError("Choose an account type.")
    name = (name or "").strip()
    if not name:
        raise ValueError("Enter a name.")
    if parent_id:
        p = db.execute("SELECT id, type FROM ledger_account WHERE client=? AND id=?", (client, parent_id)).fetchone()
        if not p:
            raise ValueError("Unknown parent account.")
        if TYPES[p["type"]][1] != TYPES[typ][1]:
            raise ValueError("A sub-account must be in the same group (asset, liability, equity, income or expense) as its parent.")
        q, seen = parent_id, set()
        while q:  # no loops
            if q == aid or q in seen:
                raise ValueError("An account can't be under itself.")
            seen.add(q)
            r = db.execute("SELECT parent_id FROM ledger_account WHERE id=?", (q,)).fetchone()
            q = r["parent_id"] if r else None
    ts = now()
    if aid:
        db.execute("""UPDATE ledger_account SET name=?, code=?, type=?, parent_id=?, active=?, description=?, updated_by=?, updated_at=?
                      WHERE client=? AND id=?""", (name, code or None, typ, parent_id or None, 1 if active else 0, description or None,
                                                   user, ts, client, aid))
    else:
        aid = db.execute("""INSERT INTO ledger_account(client, name, code, type, parent_id, active, description, created_by, created_at)
                            VALUES (?,?,?,?,?,?,?, 'manual', ?)""", (client, name, code or None, typ, parent_id or None,
                                                                     1 if active else 0, description or None, ts)).lastrowid
    db.commit()
    return aid


def merge(db, client, from_id, to_id, user):
    """Merge clean account `from_id` into `to_id`: its source accounts and sub-accounts move to `to_id`."""
    ensure(db)
    if from_id == to_id:
        return
    acc = {r["id"]: dict(r) for r in db.execute("SELECT * FROM ledger_account WHERE client=?", (client,))}
    if from_id not in acc or to_id not in acc:
        raise ValueError("Unknown account.")
    if TYPES[acc[from_id]["type"]][1] != TYPES[acc[to_id]["type"]][1]:
        raise ValueError("Those accounts are in different groups (asset, liability, equity, income, expense); change the type first.")
    p = acc[to_id]["parent_id"]
    while p:
        if p == from_id:
            raise ValueError("Can't merge an account into one of its own sub-accounts.")
        p = acc[p]["parent_id"] if p in acc else None
    ts = now()
    db.execute("""UPDATE account_map SET account_id=?, status='confirmed', updated_by=?, updated_at=?,
                  note=trim(coalesce(note || ' · ', '') || ?) WHERE client=? AND account_id=?""",
               (to_id, user, ts, "Merged into " + full_name(acc, acc[to_id]), client, from_id))
    db.execute("UPDATE ledger_account SET parent_id=? WHERE client=? AND parent_id=?", (to_id, client, from_id))
    db.execute("DELETE FROM ledger_account WHERE client=? AND id=?", (client, from_id))
    db.commit()


def by_full_name(db, client):
    acc = {r["id"]: dict(r) for r in db.execute("SELECT * FROM ledger_account WHERE client=?", (client,))}
    return {full_name(acc, a).lower(): i for i, a in acc.items()}


def confirm(db, client, source, key, user):
    db.execute("UPDATE account_map SET status='confirmed', updated_by=?, updated_at=? WHERE client=? AND source=? AND source_key=?",
               (user, now(), client, source, key))
    db.commit()


def summary(db, client):
    ensure(db)
    a = db.execute("SELECT count(*), sum(active) FROM ledger_account WHERE client=?", (client,)).fetchone()
    m = db.execute("""SELECT count(*), sum(status='confirmed'), sum(account_id IS NULL) FROM account_map WHERE client=?""", (client,)).fetchone()
    srcs = db.execute("SELECT source, count(*) FROM account_map WHERE client=? GROUP BY source", (client,)).fetchall()
    return dict(accounts=a[0] or 0, active=a[1] or 0, sources=m[0] or 0, confirmed=m[1] or 0, unmapped=m[2] or 0,
                by_source={r[0]: r[1] for r in srcs})
