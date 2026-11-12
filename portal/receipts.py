"""Receipts for expenses.

The record lives in books.db (per client). Uploaded files wait in a holding area on the server (instance/receipts_pending,
owner-only) until the receipts sync on the owner's computer (tools/receipts-sync.ps1, run whenever / on a schedule) copies them
into the OneDrive folder on that computer — only under Clients/<client>/Receipts/<YYYY-MM>/ — and reports back. OneDrive then
syncs them up itself, so the Finance app never needs permission to write to OneDrive (owner's choice 11/5). Once a file is in
OneDrive, Finance shows it through its own page (Graph read), keeping the server copy until OneDrive has it.
A receipt can instead just point somewhere (a link to a Google Drive / OneDrive file or a folder path).
Statuses: pending (in the holding area) · filed (copied into OneDrive by the sync) · link.
"""
import datetime as dt
import os
import posixpath
import re
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS receipt (
  id INTEGER PRIMARY KEY, client TEXT NOT NULL, entry_id INTEGER, date TEXT, vendor TEXT, amount REAL, note TEXT,
  filename TEXT, content_type TEXT, size INTEGER, status TEXT NOT NULL,           -- pending | filed | link
  drive_user TEXT, drive_item_id TEXT, drive_path TEXT, web_url TEXT, link TEXT, local_path TEXT, error TEXT,
  created_by TEXT, created_at TEXT);
CREATE INDEX IF NOT EXISTS receipt_client ON receipt(client, date);
CREATE INDEX IF NOT EXISTS receipt_entry ON receipt(client, entry_id);
CREATE TABLE IF NOT EXISTS receipt_skip (client TEXT NOT NULL, entry_id INTEGER NOT NULL, by TEXT, at TEXT, PRIMARY KEY(client, entry_id));
"""
TYPES = {b"%PDF": ("application/pdf", ".pdf"), b"\xff\xd8\xff": ("image/jpeg", ".jpg"), b"\x89PNG": ("image/png", ".png"),
         b"RIFF": ("image/webp", ".webp"), b"GIF8": ("image/gif", ".gif")}
MAX_BYTES = 25_000_000
# spending that never has a receipt to keep
NO_RECEIPT_CATS = ("Payroll - Wages", "Payroll Taxes", "Card Tips Payable", "Merchant Processing Fees", "Transfer", "Transfers",
                   "Owner Draw", "Owner Distributions", "Loan Payables", "Sales Tax Payable", "Alc Sales Tax", "Bank Fees", "Investor Contributions")


def ensure(db):
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(receipt)")}
    for col in ("filed_at", "folder", "sha256", "ref", "orig_name", "ref_found", "doc_type", "bill_id"):
        if col not in cols:
            db.execute("ALTER TABLE receipt ADD COLUMN %s TEXT" % col)


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def sniff(data, filename=""):
    """(content_type, ext) for the receipt formats we accept, else None."""
    for magic, t in TYPES.items():
        if data.startswith(magic):
            if magic == b"RIFF" and data[8:12] != b"WEBP":
                return None
            return t
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftyphevc", b"ftypmsf1"):  # iPhone photos
        return ("image/heic", ".heic")
    return None


def target(c, all_clients):
    """(graph settings, folder) for a client's receipts. A client without its own OneDrive link uses the first one
    configured (Unscatter's OneDrive), in a folder under Clients/<name>/Receipts."""
    g = c.get("graph")
    if c.get("receipts_folder"):
        folder = c["receipts_folder"]
    elif g and g.get("file_path"):
        parts = g["file_path"].replace("\\", "/").strip("/").split("/")
        folder = "/".join(parts[:-2] + ["Receipts"]) if len(parts) >= 3 else "Receipts"
    else:
        folder = "Clients/%s/Receipts" % re.sub(r"[^\w .&-]", "", c.get("name") or "Client").strip()
    if not g:
        g = next((x["graph"] for x in all_clients.values() if x.get("graph")), None)
    return g, folder


def _safe(s, n=60):
    return re.sub(r"\s+", " ", re.sub(r"[^\w .,&()-]", "", s or "")).strip()[:n]


def vendor_part(vendor):
    """"Sam's Club #4521" → SamsClub4521 (letters and digits only, each word capitalized)."""
    words = re.findall(r"[A-Za-z0-9]+", (vendor or "").replace("'", ""))
    return "".join(w[:1].upper() + w[1:] for w in words)[:40]


def ref_part(ref):
    return re.sub(r"[^A-Za-z0-9-]+", "-", ref or "").strip("-")[:40]


def file_name(date, vendor, ref, ext):
    """Naming convention (owner, 11/5): VendorName_YYYY-MM-DD_ReferenceNumber.ext — the reference only when there is one."""
    parts = [vendor_part(vendor) or "Receipt", date or dt.date.today().isoformat()]
    if ref_part(ref):
        parts.append(ref_part(ref))
    return "_".join(parts) + ext


def original_name(upload_name, ext):
    """The uploaded file's own name, made safe for Windows/OneDrive; the extension follows what the file really is."""
    base = os.path.basename((upload_name or "").replace("\\", "/"))
    stem, old_ext = os.path.splitext(base)
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "", stem).strip(" .")[:100]
    if not stem:
        return None
    keep = old_ext.lower() in (ext, ".jpeg") and old_ext or ext
    return stem + keep


# ------------------------------------------------------------------ reference number from the file itself
_LABEL = re.compile(r"(?i)\b(invoice|receipt|order|confirmation|reference|ref|transaction|credit\s*memo|statement|bill|document)"
                    r"\s*(?:number|num|no\.?|#|id)?\s*[:#]\s*#?\s*([A-Z0-9][A-Z0-9\-/]{3,30})")
_LABEL2 = re.compile(r"(?i)\b(invoice|receipt|order|confirmation|reference|transaction)\s+(?:number|no\.?|#|id)\s+#?\s*([A-Z0-9][A-Z0-9\-]{3,30})")
_GENERIC = re.compile(r"(?i)^(img|dsc|dcim|scan|scanned|image|photo|document|doc|file|receipt|invoice|untitled|screenshot|pxl|mvimg|wa)\d*$")


_CAMERA = re.compile(r"(?i)^(img|dsc|dcim|scan|scanned|image|photo|pxl|mvimg|screenshot|wa|untitled)[\d-]*$")


def pdf_text(data, pages=3):
    try:
        import io
        from pypdf import PdfReader
        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages[:pages])
    except Exception:  # noqa: BLE001 — no pypdf, a scanned PDF, or a damaged file: just no text
        return ""


def _is_date(tok):
    return bool(re.fullmatch(r"(19|20)\d{2}(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])", tok) or re.fullmatch(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", tok)
                or re.fullmatch(r"(19|20)\d{2}-\d{2}-\d{2}", tok))


def doc_kind(data, ctype, upload_name=None):
    """'credit memo' when the document says so (text or file name), else None (a receipt / invoice)."""
    name = re.sub(r"[\s_-]+", "", (upload_name or "").lower())
    if re.search(r"credit(memo|note)|refund", name):
        return "credit memo"
    text = pdf_text(data, pages=1) if ctype == "application/pdf" else ""
    if re.search(r"(?i)\bcredit\s*(memo|note)\b|\brefund\s+receipt\b", text):
        return "credit memo"
    return None


def find_ref(data, ctype, upload_name=None):
    """A likely invoice / receipt / order number: from labels in the PDF's text ("Invoice number: 5722515584") and from
    the uploaded file's own name (e.g. 5722515584.pdf, USFOODS-64553233_Invoice_20261021_3202772.pdf). A number that is
    both in the name and in the text wins. Returns (ref, where) or (None, None)."""
    text = pdf_text(data) if ctype == "application/pdf" else ""
    score, where = {}, {}
    for rx in (_LABEL, _LABEL2):
        for m in rx.finditer(text):
            tok = m.group(2).strip("-/")
            if not re.search(r"\d", tok) or _is_date(tok) or len(tok) < 4:
                continue
            score[tok] = score.get(tok, 0) + 3
            where.setdefault(tok, "the file's text (%s)" % m.group(1).lower())
    stem = os.path.splitext(os.path.basename((upload_name or "").replace("\\", "/")))[0]
    toks = [t for t in re.split(r"[_\s.,()\[\]]+", stem) if t]
    if stem and not _CAMERA.match(re.sub(r"[\s_-]+", "", stem)):
        keyword_at = max([i for i, t in enumerate(toks) if re.search(r"(?i)invoice|receipt|memo|order|bill|ref", t)] or [-1])
        for i, t in enumerate(toks):
            t2 = t.strip("-")
            if len(re.sub(r"\D", "", t2)) < 4 or _is_date(t2) or _GENERIC.match(t2):
                continue
            if re.fullmatch(r"[A-Za-z]+-\d+", t2) and not re.search(r"(?i)inv|ord|rec|ref", t2):
                continue  # "USFOODS-64553233" style: a vendor + account number, not the document number
            sc = 2 + (2 if keyword_at >= 0 and i > keyword_at else 0) + (3 if t2 in text else 0) + (1 if len(toks) == 1 else 0)
            score[t2] = score.get(t2, 0) + sc
            where[t2] = "the file's name and text" if (t2 in text or t2 in where) else "the file's name"
    if not score:
        return None, None
    best = max(score, key=lambda k: (score[k], len(k)))
    return best, where[best]


def add_file(db, client, user, data, fields, folder, pending_dir, upload_name=None, keep_name=False):
    """Save a receipt file in the holding area. Returns the receipt id."""
    import hashlib
    t = sniff(data)
    if not t:
        raise ValueError("That isn't a photo or PDF (JPG, PNG, HEIC, WebP or PDF).")
    ctype, ext = t
    found = None
    fields = dict(fields, vendor=canonical_vendor(db, client, fields.get("vendor"), fields.get("entry_id")))
    doc_type = fields.get("doc_type") or doc_kind(data, ctype, upload_name)
    if not fields.get("ref"):
        ref, found = find_ref(data, ctype, upload_name)
        if ref:
            fields = dict(fields, ref=ref[:40])
    name = (keep_name and original_name(upload_name, ext)) or file_name(fields.get("date"), fields.get("vendor"), fields.get("ref"), ext)
    cur = db.execute("""INSERT INTO receipt(client,entry_id,date,vendor,amount,note,ref,ref_found,orig_name,filename,content_type,size,status,folder,
                        sha256,created_by,created_at,doc_type) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?)""",
                     (client, fields.get("entry_id"), fields.get("date"), fields.get("vendor"), fields.get("amount"), fields.get("note"),
                      fields.get("ref"), found, os.path.basename((upload_name or "").replace("\\", "/"))[:200] or None, name, ctype, len(data),
                      posixpath.join(folder, sub_path(fields.get("date"), fields.get("vendor"))), hashlib.sha256(data).hexdigest(), user, _now(), doc_type))
    rid = cur.lastrowid
    os.makedirs(pending_dir, mode=0o700, exist_ok=True)
    local = os.path.join(pending_dir, "%d%s" % (rid, ext))
    with open(local, "wb") as f:
        f.write(data)
    os.chmod(local, 0o600)
    db.execute("UPDATE receipt SET local_path=? WHERE id=?", (local, rid))
    db.commit()
    if not fields.get("entry_id"):
        auto_match(db, client, rid)
    return rid


def safe_folder(folder):
    """Documents may only go inside a client folder: Clients/<client>/<folder>/… (no '..', no absolute paths, no
    characters Windows/OneDrive refuse)."""
    f = re.sub(r"/+", "/", (folder or "").replace("\\", "/")).strip().strip("/")
    parts = [p.strip() for p in f.split("/")]
    ok = (len(parts) >= 3 and parts[0] == "Clients" and all(p not in ("", ".", "..") and not re.search(r'[:*?"<>|]', p) for p in parts))
    return "/".join(parts) if ok else None


def _key(v):
    return re.sub(r"[^a-z0-9]", "", (v or "").lower())


def canonical_vendor(db, client, typed, entry_id=None):
    """The vendor as it is in the vendor list (payees on the books), so one vendor = one folder:
    the transaction's payee when attached; else the payee it matches (ignoring case/punctuation, or the start of the
    name: "Victor Welding" → "Victor Welding Supply"); else a bank rule's payee; else what was typed (a new vendor)."""
    if entry_id:
        e = db.execute("SELECT payee FROM entry WHERE client=? AND id=?", (client, entry_id)).fetchone()
        if e and e["payee"]:
            return e["payee"]
    t = (typed or "").strip()
    k = _key(t)
    if not k:
        return t
    rows = db.execute("""SELECT payee, count(*) n FROM entry WHERE client=? AND payee IS NOT NULL AND payee!=''
                         GROUP BY payee ORDER BY n DESC""", (client,)).fetchall()
    for r in rows:
        if _key(r["payee"]) == k:
            return r["payee"]
    if len(k) >= 4:
        starts = [r["payee"] for r in rows if _key(r["payee"]).startswith(k) or (len(_key(r["payee"])) >= 4 and k.startswith(_key(r["payee"])))]
        if starts:
            return starts[0]  # most used
    try:
        from .books import apply_rules
        p, _ = apply_rules(db, client, t)
        if p:
            return p
    except Exception:  # noqa: BLE001
        pass
    return t


def refile(db, client, rid, base):
    """Vendor follows the attached transaction's payee; folder follows the vendor (a filed document is then moved by the sync)."""
    r = db.execute("SELECT * FROM receipt WHERE client=? AND id=?", (client, rid)).fetchone()
    if not r or r["status"] == "link":
        return
    v = canonical_vendor(db, client, r["vendor"], r["entry_id"])
    folder = posixpath.join(base, sub_path(r["date"] or (r["created_at"] or "")[:10], v))
    db.execute("UPDATE receipt SET vendor=?, folder=? WHERE id=?", (v, folder, rid))
    db.commit()


def vendor_folder(vendor):
    """Folder name for a vendor ("US Foods", "Sam's Club"): readable, minus characters Windows/OneDrive refuse."""
    v = re.sub(r'[\\/:*?"<>|\x00-\x1f#%]+', " ", vendor or "")
    v = re.sub(r"\s+", " ", v).strip(" .")[:60].strip(" .")
    return v or "Other"


def sub_path(date, vendor):
    """Where a document goes inside the client's folder (owner, 11/5): <Vendor>/<YYYY>/."""
    year = (date or dt.date.today().isoformat())[:4]
    return posixpath.join(vendor_folder(vendor), year)


def set_folder(db, client, folder, move_waiting=True, move_filed=False):
    """New OneDrive folder for a client's documents; documents still waiting for the sync can move along, and documents
    already in OneDrive can be moved by the computer sync the next time it runs (move_filed)."""
    n = 0
    if move_filed:
        for r in db.execute("SELECT id, date, created_at, vendor FROM receipt WHERE client=? AND status='filed'", (client,)).fetchall():
            db.execute("UPDATE receipt SET folder=? WHERE id=?", (posixpath.join(folder, sub_path(r["date"] or (r["created_at"] or "")[:10], r["vendor"])), r["id"]))
        db.commit()
    if move_waiting:
        for r in db.execute("SELECT id, date, created_at, vendor FROM receipt WHERE client=? AND status='pending'", (client,)).fetchall():
            db.execute("UPDATE receipt SET folder=? WHERE id=?", (posixpath.join(folder, sub_path(r["date"] or (r["created_at"] or "")[:10], r["vendor"])), r["id"]))
            n += 1
        db.commit()
    return n


def for_sync(db):
    """Receipts in the holding area, every client: what the computer sync copies into OneDrive."""
    out = []
    for r in db.execute("SELECT id, client, folder, filename, size, sha256, local_path FROM receipt WHERE status='pending' ORDER BY id").fetchall():
        if r["local_path"] and os.path.exists(r["local_path"]) and safe_folder(r["folder"]):
            out.append(dict(id=r["id"], client=r["client"], folder=safe_folder(r["folder"]), filename=r["filename"], size=r["size"], sha256=r["sha256"]))
    return out


def for_moves(db):
    """Documents already in OneDrive whose folder or name changed in Finance (vendor/year layout, a new client folder, an
    edited vendor / date / reference #): the computer sync moves / renames them on the computer and reports back."""
    out = []
    for r in db.execute("SELECT id, client, folder, drive_path, filename FROM receipt WHERE status='filed' AND drive_path IS NOT NULL AND folder IS NOT NULL").fetchall():
        cur = posixpath.dirname(r["drive_path"])
        to = safe_folder(r["folder"])
        want = r["filename"] or posixpath.basename(r["drive_path"])
        if to and safe_folder(cur) and (cur != to or posixpath.basename(r["drive_path"]) != want):
            out.append(dict(id=r["id"], client=r["client"], src=r["drive_path"], folder=to, filename=want))
    return out


def update(db, client, rid, fields, base, rename=True):
    """Edit a document's details. With rename, a file named by the convention gets the convention name for the new
    details; the folder follows the vendor/year. Waiting documents change right away; documents already in OneDrive are
    renamed / moved by the computer sync next time."""
    r = db.execute("SELECT * FROM receipt WHERE client=? AND id=?", (client, rid)).fetchone()
    if not r:
        return None
    ext = os.path.splitext(r["filename"] or "")[1] or ".pdf"
    was_convention = (r["filename"] or "") in (file_name(r["date"], r["vendor"], r["ref"], ext), file_name(r["date"], r["vendor"], None, ext))
    vendor = canonical_vendor(db, client, fields.get("vendor"), None) if fields.get("vendor") else r["vendor"]
    new = dict(date=fields.get("date") or r["date"], vendor=vendor, ref=fields.get("ref"), note=fields.get("note"),
               amount=fields.get("amount") if fields.get("amount") is not None else r["amount"])
    name = r["filename"]
    if rename and r["status"] != "link" and (was_convention or fields.get("force_rename")):
        name = file_name(new["date"], new["vendor"], new["ref"], ext)
    folder = posixpath.join(base, sub_path(new["date"], new["vendor"])) if r["status"] != "link" else r["folder"]
    db.execute("UPDATE receipt SET date=?, vendor=?, ref=?, note=?, amount=?, filename=?, folder=? WHERE id=?",
               (new["date"], new["vendor"], new["ref"], new["note"], new["amount"], name, folder, rid))
    db.commit()
    return dict(filename=name, folder=folder, status=r["status"], renamed=name != r["filename"])


def mark_moved(db, rid, path):
    r = db.execute("SELECT folder FROM receipt WHERE id=? AND status='filed'", (rid,)).fetchone()
    p = (path or "").replace("\\", "/").strip("/")
    if not r or not safe_folder(posixpath.dirname(p)) or posixpath.dirname(p) != safe_folder(r["folder"]):
        return False
    db.execute("UPDATE receipt SET drive_path=?, filename=? WHERE id=?", (p, posixpath.basename(p), rid))
    db.commit()
    return True


def mark_filed(db, rid, path):
    """The computer sync copied the file to OneDrive at `path` (relative to the OneDrive root)."""
    r = db.execute("SELECT folder FROM receipt WHERE id=? AND status='pending'", (rid,)).fetchone()
    p = (path or "").replace("\\", "/").strip("/")
    if not r or not safe_folder(posixpath.dirname(p)) or not p.startswith(safe_folder(r["folder"]) + "/"):
        return False
    db.execute("UPDATE receipt SET status='filed', drive_path=?, filename=?, filed_at=?, error=NULL WHERE id=?",
               (p, posixpath.basename(p), _now(), rid))
    db.commit()
    return True


def drop_local(db, rid):
    """OneDrive has the file now: remove the holding copy."""
    r = db.execute("SELECT local_path FROM receipt WHERE id=?", (rid,)).fetchone()
    if r and r["local_path"] and os.path.exists(r["local_path"]):
        os.remove(r["local_path"])
    db.execute("UPDATE receipt SET local_path=NULL WHERE id=?", (rid,))
    db.commit()


def add_link(db, client, user, link, fields):
    cur = db.execute("""INSERT INTO receipt(client,entry_id,date,vendor,amount,note,ref,status,link,created_by,created_at)
                        VALUES(?,?,?,?,?,?,?,'link',?,?,?)""",
                     (client, fields.get("entry_id"), fields.get("date"), fields.get("vendor"), fields.get("amount"), fields.get("note"),
                      fields.get("ref"), link, user, _now()))
    db.commit()
    if not fields.get("entry_id"):
        auto_match(db, client, cur.lastrowid)
    return cur.lastrowid


def auto_match(db, client, rid):
    """Attach a document to the one transaction of the same amount within 5 days of its date (when exactly one fits):
    a receipt/invoice → money out; a credit memo → money in (refund / credit) first, else money out."""
    r = db.execute("SELECT * FROM receipt WHERE id=?", (rid,)).fetchone()
    if not r or not r["amount"] or r["entry_id"]:
        return None
    d = r["date"] or dt.date.today().isoformat()
    signs = ("amount>0", "amount<0") if (r["doc_type"] or "") == "credit memo" else ("amount<0",)
    for sign in signs:
        rows = db.execute("""SELECT id FROM entry WHERE client=? AND status!='void' AND %s AND abs(abs(amount)-?)<0.005
                             AND date BETWEEN date(?,'-5 day') AND date(?,'+5 day')
                             AND id NOT IN (SELECT entry_id FROM receipt WHERE client=? AND entry_id IS NOT NULL)""" % sign,
                          (client, abs(r["amount"]), d, d, client)).fetchall()
        if len(rows) == 1:
            db.execute("UPDATE receipt SET entry_id=? WHERE id=?", (rows[0]["id"], rid))
            db.commit()
            return rows[0]["id"]
    return None


# ------------------------------------------------------------------ unpaid invoices → upcoming bills
def link_bill(db, client, rid, due, user):
    """An unpaid invoice: attach it to the open bill for the same vendor (same reference # or same amount), or add a new
    bill (vendor, amount, due date, invoice # = reference, category = the vendor's usual one). When the bill is paid the
    invoice follows to the payment. Returns ('matched'|'added', bill) or (None, reason)."""
    from . import books as B
    r = db.execute("SELECT * FROM receipt WHERE client=? AND id=?", (client, rid)).fetchone()
    if not r:
        return None, "not found"
    if r["bill_id"]:
        b = db.execute("SELECT * FROM bill WHERE id=?", (r["bill_id"],)).fetchone()
        if b:
            return "matched", dict(b)
    vendor = r["vendor"] or ""
    k = _key(vendor)
    opens = [dict(b) for b in db.execute("SELECT * FROM bill WHERE client=? AND status='open' ORDER BY due_date", (client,))]
    same = [b for b in opens if k and (_key(b["vendor"]) == k or _key(b["vendor"]).startswith(k) or k.startswith(_key(b["vendor"]) or "~"))]
    pick = [b for b in same if r["ref"] and (b["invoice_no"] or "") == r["ref"]] or \
           [b for b in same if r["amount"] and abs(b["amount"] - r["amount"]) < 0.005]
    if pick:
        b = pick[0]
        db.execute("UPDATE receipt SET bill_id=? WHERE id=?", (b["id"], rid))
        if r["ref"] and not b["invoice_no"]:
            db.execute("UPDATE bill SET invoice_no=? WHERE id=?", (r["ref"], b["id"]))
        db.commit()
        return "matched", b
    if not r["amount"]:
        return None, "enter the invoice amount to add it to the bills"
    cat = db.execute("""SELECT category, count(*) n FROM entry WHERE client=? AND payee=? AND category IS NOT NULL
                        AND category NOT IN ('Uncategorized','Missing Check') GROUP BY category ORDER BY n DESC LIMIT 1""",
                     (client, vendor)).fetchone()
    due = due or r["date"] or dt.date.today().isoformat()
    bid = B.add_bill(db, client, user, vendor or "Unknown vendor", r["amount"], due, category=cat["category"] if cat else None,
                     invoice_no=r["ref"], notes="Added from the uploaded invoice %s" % (r["filename"] or ""))
    db.execute("UPDATE receipt SET bill_id=? WHERE id=?", (bid, rid))
    db.commit()
    return "added", dict(db.execute("SELECT * FROM bill WHERE id=?", (bid,)).fetchone())


def follow_bills(db, client):
    """Invoices attached to a bill move to the payment once the bill is paid (entry.bill_id)."""
    db.execute("""UPDATE receipt SET entry_id=(SELECT e.id FROM entry e WHERE e.client=receipt.client AND e.bill_id=CAST(receipt.bill_id AS INTEGER)
                                               AND e.status!='void' ORDER BY e.id DESC LIMIT 1)
                  WHERE client=? AND bill_id IS NOT NULL AND entry_id IS NULL""", (client,))
    db.commit()


def for_bills(db, client):
    """{bill id: [documents]} for the bills list."""
    out = {}
    try:
        for r in db.execute("SELECT id, bill_id, status, filename FROM receipt WHERE client=? AND bill_id IS NOT NULL", (client,)):
            out.setdefault(int(r["bill_id"]), []).append(dict(r))
    except sqlite3.OperationalError:
        pass
    return out


def attach(db, client, rid, entry_id):
    db.execute("UPDATE receipt SET entry_id=? WHERE client=? AND id=?", (entry_id, client, rid))
    db.commit()


def delete(db, client, rid):
    r = db.execute("SELECT local_path FROM receipt WHERE client=? AND id=?", (client, rid)).fetchone()
    if r and r["local_path"] and os.path.exists(r["local_path"]):
        os.remove(r["local_path"])
    db.execute("DELETE FROM receipt WHERE client=? AND id=?", (client, rid))
    db.commit()


def skip(db, client, entry_id, user, on=True):
    if on:
        db.execute("INSERT OR REPLACE INTO receipt_skip(client,entry_id,by,at) VALUES(?,?,?,?)", (client, entry_id, user, _now()))
    else:
        db.execute("DELETE FROM receipt_skip WHERE client=? AND entry_id=?", (client, entry_id))
    db.commit()


def get(db, client, rid):
    r = db.execute("SELECT * FROM receipt WHERE client=? AND id=?", (client, rid)).fetchone()
    return dict(r) if r else None


def for_entries(db, client, ids):
    out = {}
    ids = [i for i in ids if i]
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in db.execute("SELECT id, entry_id, status, filename, link FROM receipt WHERE client=? AND entry_id IN (%s)" % ",".join("?" * len(part)),
                            (client, *part)):
            out.setdefault(r["entry_id"], []).append(dict(r))
    return out


def listing(db, client, days=90, today=None):
    today = today or dt.date.today()
    follow_bills(db, client)
    since = (today - dt.timedelta(days=days)).isoformat()
    recs = [dict(r) for r in db.execute(
        """SELECT r.*, e.date AS e_date, e.payee AS e_payee, e.description AS e_desc, e.amount AS e_amount, e.category AS e_cat,
                  b.vendor AS b_vendor, b.amount AS b_amount, b.due_date AS b_due, b.status AS b_status
           FROM receipt r LEFT JOIN entry e ON e.id = r.entry_id LEFT JOIN bill b ON b.id = r.bill_id
           WHERE r.client=? AND (coalesce(r.date, substr(r.created_at,1,10)) >= ? OR r.status='pending' OR r.entry_id IS NULL)
           ORDER BY coalesce(r.date, substr(r.created_at,1,10)) DESC, r.id DESC""", (client, since))]
    skip_cats = ",".join("?" * len(NO_RECEIPT_CATS))
    missing = [dict(r) for r in db.execute(
        """SELECT id, date, payee, description, category, amount, check_no FROM entry e WHERE client=? AND status!='void' AND amount<0 AND date>=?
           AND coalesce(category,'') NOT IN (%s) AND coalesce(category,'') NOT LIKE 'Payroll%%'
           AND id NOT IN (SELECT entry_id FROM receipt WHERE client=? AND entry_id IS NOT NULL)
           AND id NOT IN (SELECT entry_id FROM receipt_skip WHERE client=?)
           ORDER BY date DESC, id DESC""" % skip_cats, (client, since, *NO_RECEIPT_CATS, client, client))]
    choices = [dict(r) for r in db.execute(
        """SELECT id, date, payee, description, amount FROM entry WHERE client=? AND status!='void' AND date>=?
           ORDER BY date DESC, id DESC LIMIT 600""", (client, (today - dt.timedelta(days=days + 60)).isoformat()))]
    return dict(receipts=recs, missing=missing, choices=choices, pending=sum(1 for r in recs if r["status"] == "pending"),
                last_sync=sync_info().get("last_sync"),
                unmatched=sum(1 for r in recs if not r["entry_id"]), days=days)


# ------------------------------------------------------------------ sync key (the computer sync's password)
SYNC_FILE = None  # set by the app: instance/receipts_sync.json


def sync_info():
    import json
    try:
        return json.load(open(SYNC_FILE)) if SYNC_FILE and os.path.exists(SYNC_FILE) else {}
    except (OSError, ValueError):
        return {}


def _save_sync(info):
    import json
    fd = os.open(SYNC_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(info, f)


def new_sync_key(user):
    """Create a new key (shown once); only its hash is kept. Replaces any earlier key."""
    import hashlib
    import secrets
    key = secrets.token_urlsafe(32)
    info = sync_info()
    info.update(sha256=hashlib.sha256(key.encode()).hexdigest(), created=_now(), created_by=user)
    _save_sync(info)
    return key


def check_sync_key(sent):
    import hashlib
    import hmac
    h = sync_info().get("sha256")
    return bool(h and sent and hmac.compare_digest(hashlib.sha256(sent.encode()).hexdigest(), h))


def note_denied():
    info = sync_info()
    if info.get("sha256"):
        info["last_denied"] = _now()
        _save_sync(info)


def touch_sync(n):
    info = sync_info()
    info.update(last_sync=_now(), last_count=n)
    _save_sync(info)
