"""1099 check: who was paid enough in a year to need a 1099-NEC (services) or 1099-MISC (rent).

Works from the bank register (cash basis: money paid out by check / ACH / bank transfer in the calendar year), grouped
by payee. Payments made by credit or debit card or through a payment app aren't counted here — the card company or app
reports those on a 1099-K — and neither are payroll (W-2), taxes, loans, transfers, owner draws or purchases of goods.

Threshold: $2,000 for payments made in 2026 and later (One Big Beautiful Bill Act raised it from $600; it is indexed
for inflation after 2026). Change it per client with meta "1099_threshold" or per year "1099_threshold_<year>".

Per payee you record what you know (stored in table vendor_1099): business type from the W-9 (individual / sole
proprietor, partnership, LLC, corporation, attorney), whether a W-9 is on file, whether the 1099 was filed, notes.
Corporations don't get a 1099 — except attorneys, who always do. No tax ID numbers are stored here.
"""
import datetime as dt
import re

SCHEMA = """CREATE TABLE IF NOT EXISTS vendor_1099 (
  client TEXT NOT NULL, payee TEXT NOT NULL, entity TEXT, w9 INTEGER DEFAULT 0, form TEXT, status TEXT, notes TEXT,
  updated_by TEXT, updated_at TEXT, PRIMARY KEY (client, payee))"""

DEFAULT_THRESHOLD = {2025: 600.0}   # payments in 2025 and earlier: $600
NEW_THRESHOLD = 2000.0              # payments in 2026 and later

ENTITIES = [("", "Not known yet"), ("individual", "Individual / sole proprietor"), ("partnership", "Partnership"),
            ("llc", "LLC (not taxed as a corporation)"), ("corporation", "Corporation (C or S)"), ("attorney", "Attorney / law firm"),
            ("government", "Government / nonprofit"), ("employee", "Employee (paid through payroll)")]
EXEMPT_ENTITIES = {"corporation", "government", "employee"}

# categories → what payments in them usually are
SERVICE_CATS = {"Professional Services", "Repairs & Maintenance", "Linen & Laundry", "Marketing & Signage", "Contract Labor",
                "Consulting", "Cleaning", "Security"}
RENT_CATS = {"Rent & Occupancy", "Rent", "Equipment Rental"}
UNSURE_CATS = {"Uncategorized", "Missing Check", "Needs Category", "Other Expense"}
# everything else (goods, utilities, software, insurance, fees, payroll, taxes, loans, transfers, owner…) isn't reportable

CORP_RE = re.compile(r"\b(inc|incorporated|corp|corporation|co\b|company|ltd)\b\.?", re.I)
ATTORNEY_RE = re.compile(r"\b(law|attorney|attorneys|legal|esq|pllc|lawyer)\b", re.I)
CARD_RE = re.compile(r"\b(POS|DEBIT CARD|CHECKCARD|CHECK CARD|PURCHASE|VISA|MASTERCARD|PAYPAL|VENMO|SQUARE|SQ \*|ZELLE|CASH APP|STRIPE)\b", re.I)


def ensure(db):
    db.execute(SCHEMA)


def threshold(db, client, year):
    from .books import get_meta
    for k in ("1099_threshold_%d" % year, "1099_threshold"):
        v = get_meta(db, client, k, "")
        try:
            if v:
                return float(v)
        except ValueError:
            pass
    return DEFAULT_THRESHOLD.get(year, 600.0 if year < 2026 else NEW_THRESHOLD)


def _kind(cat):
    if cat in SERVICE_CATS:
        return "nec"
    if cat in RENT_CATS:
        return "misc"
    if cat in UNSURE_CATS or not cat:
        return "unsure"
    return None


def _paid_by_card(desc):
    return bool(CARD_RE.search(desc or ""))


def report(db, client, year):
    """Payees paid in `year`, sorted: needs a 1099 / check these / under the threshold / not reportable."""
    ensure(db)
    th = threshold(db, client, year)
    a, b = "%d-01-01" % year, "%d-12-31" % year
    info = {r["payee"]: dict(r) for r in db.execute("SELECT * FROM vendor_1099 WHERE client=?", (client,))}
    pay = {}
    for e in db.execute("""SELECT id, date, kind, check_no, description, payee, category, amount FROM entry
                           WHERE client=? AND status!='void' AND amount<0 AND date BETWEEN ? AND ? ORDER BY date""", (client, a, b)):
        name = (e["payee"] or "").strip() or "(no payee) " + (e["description"] or "")[:40]
        p = pay.setdefault(name, dict(payee=name, nec=0.0, misc=0.0, unsure=0.0, card=0.0, other=0.0, count=0, cats={}, last=None,
                                       checks=0, first=e["date"]))
        amt = -e["amount"]
        k = _kind(e["category"])
        if k and e["kind"] == "bank" and _paid_by_card(e["description"]):
            p["card"] += amt          # card / app payment: on the processor's 1099-K, not ours
        elif k:
            p[k] += amt
        else:
            p["other"] += amt
        p["count"] += 1
        p["checks"] += 1 if e["kind"] == "check" or e["check_no"] else 0
        p["cats"][e["category"] or "Uncategorized"] = p["cats"].get(e["category"] or "Uncategorized", 0.0) + amt
        p["last"] = e["date"]
    needs, check, under, no = [], [], [], []
    for name, p in pay.items():
        for f in ("nec", "misc", "unsure", "card", "other"):
            p[f] = round(p[f], 2)
        p["reportable"] = round(p["nec"] + p["misc"], 2)
        p["total"] = round(p["nec"] + p["misc"] + p["unsure"] + p["card"] + p["other"], 2)
        p["cats"] = sorted(((c, round(v, 2)) for c, v in p["cats"].items()), key=lambda x: -x[1])
        i = info.get(name, {})
        p.update(entity=i.get("entity") or "", w9=bool(i.get("w9")), status=i.get("status") or "", notes=i.get("notes") or "",
                 form=i.get("form") or ("1099-MISC" if p["misc"] > p["nec"] else "1099-NEC"))
        guess = "attorney" if ATTORNEY_RE.search(name) else "corporation" if CORP_RE.search(name) else ""
        p["guess"] = guess
        ent = p["entity"] or ""
        attorney = ent == "attorney" or (not ent and guess == "attorney")
        exempt = ent in EXEMPT_ENTITIES
        if p["reportable"] + p["unsure"] < 0.005:
            no.append(p)
        elif exempt and not attorney:
            p["why"] = dict(ENTITIES).get(ent, ent) + " — no 1099"
            no.append(p)
        elif p["reportable"] >= th:
            p["why"] = []
            if not ent:
                p["why"].append("business type not known — get a W-9" + (" (name looks like a corporation)" if guess == "corporation" else ""))
            if not p["w9"]:
                p["why"].append("no W-9 on file")
            needs.append(p)
        elif p["reportable"] + p["unsure"] >= th:
            p["why"] = ["%s of it is uncategorized, a missing check or Other Expense — categorize it to know" % _m(p["unsure"])]
            check.append(p)
        else:
            under.append(p)
    key = lambda p: -(p["reportable"] + p["unsure"])
    needs.sort(key=key)
    check.sort(key=key)
    under.sort(key=key)
    no.sort(key=lambda p: -p["total"])
    today = dt.date.today()
    return dict(year=year, threshold=th, needs=needs, check=check, under=under, no=no,
                due=dt.date(year + 1, 1, 31).isoformat(),
                filed=sum(1 for p in needs if p["status"] == "filed"),
                missing_w9=sum(1 for p in needs if not p["w9"]),
                total_reportable=round(sum(p["reportable"] for p in needs), 2),
                partial_year=year == today.year)


def save(db, client, payee, user, entity=None, w9=None, status=None, notes=None, form=None):
    ensure(db)
    cur = db.execute("SELECT * FROM vendor_1099 WHERE client=? AND payee=?", (client, payee)).fetchone()
    cur = dict(cur) if cur else {}
    vals = dict(entity=entity if entity is not None else cur.get("entity"), w9=int(w9) if w9 is not None else cur.get("w9", 0),
                status=status if status is not None else cur.get("status"), notes=notes if notes is not None else cur.get("notes"),
                form=form if form is not None else cur.get("form"))
    db.execute("""INSERT INTO vendor_1099 (client, payee, entity, w9, form, status, notes, updated_by, updated_at) VALUES (?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(client, payee) DO UPDATE SET entity=excluded.entity, w9=excluded.w9, form=excluded.form,
                  status=excluded.status, notes=excluded.notes, updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
               (client, payee, vals["entity"], vals["w9"], vals["form"], vals["status"], vals["notes"], user,
                dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
    db.commit()


def payments(db, client, payee, year):
    """The register entries behind one payee's total (for the drill-down)."""
    a, b = "%d-01-01" % year, "%d-12-31" % year
    if payee.startswith("(no payee) "):
        q, args = "(payee IS NULL OR payee='') AND substr(description,1,40)=?", (payee[len("(no payee) "):],)
    else:
        q, args = "payee=?", (payee,)
    out = []
    for e in db.execute("SELECT id, date, kind, check_no, description, category, amount FROM entry WHERE client=? AND status!='void' "
                        "AND amount<0 AND date BETWEEN ? AND ? AND " + q + " ORDER BY date", (client, a, b) + args):
        k = _kind(e["category"])
        out.append(dict(e, paid=-e["amount"], counts=("card / app (1099-K)" if k and e["kind"] == "bank" and _paid_by_card(e["description"])
                                                      else {"nec": "1099-NEC", "misc": "1099-MISC", "unsure": "categorize"}.get(k, "not reportable"))))
    return out


def _m(v):
    return "${:,.2f}".format(v)
