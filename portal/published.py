"""Clients whose numbers come from the Unscatter platform ("source": "platform" in clients.json).

The platform (on Unscatter's PC) builds a summary and POSTs it to /api/<slug>/platform when someone
clicks "Publish to portal". This module checks and stores that summary and prepares it for the pages.
Nothing here talks to Zoho or the platform; the portal only shows what was published.
"""
import gzip
import json
import os

KIND = "unscatter-platform"
VERSION = 1
MAX_COMPRESSED = 8 * 1024 * 1024
MAX_JSON = 40 * 1024 * 1024
# url name -> (snapshot module, page title)
PAGES = {
    "sales-tax": ("sales_tax", "Sales tax"),
    "sales": ("sales", "Sales"),
    "receivables": ("receivables", "Unpaid invoices"),
    "bank": ("bank", "Bank"),
    "cleanup": ("cleanup", "Cleanup"),
}


class Rejected(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


def file_name(slug):
    return "platform_%s.json" % "".join(ch for ch in slug if ch.isalnum() or ch in "-_")


def parse_upload(raw, encoding):
    """Body bytes (maybe gzip) -> snapshot dict, or raise Rejected."""
    if len(raw) > MAX_COMPRESSED:
        raise Rejected(413, "too large")
    if (encoding or "").lower() == "gzip":
        try:
            with gzip.GzipFile(fileobj=__import__("io").BytesIO(raw)) as f:
                raw = f.read(MAX_JSON + 1)
        except OSError:
            raise Rejected(400, "bad gzip")
    if len(raw) > MAX_JSON:
        raise Rejected(413, "too large")
    try:
        snap = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise Rejected(400, "not json")
    if not isinstance(snap, dict) or snap.get("kind") != KIND:
        raise Rejected(400, "not a platform snapshot")
    if int(snap.get("version") or 0) > VERSION:
        raise Rejected(400, "newer snapshot version than this portal understands; update the portal")
    if not isinstance(snap.get("modules"), list):
        raise Rejected(400, "missing modules")
    return snap


def keep_previous(instance_path, slug):
    """Keep the last published copy next to the new one (platform_<slug>.prev.json)."""
    cur = os.path.join(instance_path, file_name(slug))
    if os.path.exists(cur):
        os.replace(cur, cur[:-5] + ".prev.json")


def visible_pages(c, snap, has_bank=False):
    """Pages this client's users can open: published modules (plus Bank and Cleanup when the portal has the
    client's bank from SheetLink), limited by clients.json "modules"."""
    allowed = c.get("modules")
    have = set((snap or {}).get("modules") or [])
    if has_bank:
        have |= {"bank", "cleanup"}
    out = []
    for name, (module, title) in PAGES.items():
        if module in have and (not allowed or module in allowed):
            out.append((name, title))
    return out


def bars_svg(months, label="Sales by month"):
    """Simple monthly bar chart (same look as the reports page)."""
    data = [(m["month"], float(m.get("sales") or 0)) for m in months]
    if not data:
        return ""
    import datetime as dt
    top = max([v for _, v in data] + [1])
    step = 10 ** max(0, len(str(int(top))) - 1)
    top = ((int(top) // step) + 1) * step
    W, H, L, B, T = 640, 220, 56, 28, 12
    pw, ph = W - L - 8, H - B - T
    gw = pw / len(data)
    bw = min(26, gw * 0.6)

    def y(v):
        return T + ph - ph * v / top

    def fmt(v):
        return "${:,.0f}".format(v)
    parts = ['<svg viewBox="0 0 %d %d" role="img" aria-label="%s" class="chart">' % (W, H, label)]
    for k in range(5):
        v = top * k / 4
        parts.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="grid"/>' % (L, W - 8, y(v), y(v)))
        parts.append('<text x="%d" y="%.1f" class="ax" text-anchor="end">%s</text>'
                     % (L - 6, y(v) + 4, "$%dk" % (v / 1000) if top >= 10000 else fmt(v)))
    for i, (ym, v) in enumerate(data):
        d = dt.date(int(ym[:4]), int(ym[5:7]), 1)
        cx = L + gw * i + gw / 2
        x = cx - bw / 2
        hgt = max(ph * v / top, 0)
        r = min(4, hgt / 2, bw / 2)
        yy, base = y(v), T + ph
        if hgt > 0.5:
            parts.append('<path class="s1" d="M%.1f,%.1f V%.1f Q%.1f,%.1f %.1f,%.1f H%.1f Q%.1f,%.1f %.1f,%.1f V%.1f Z">'
                         '<title>%s: %s</title></path>'
                         % (x, base, yy + r, x, yy, x + r, yy, x + bw - r, x + bw, yy, x + bw, yy + r, base,
                            d.strftime("%B %Y"), fmt(v)))
        parts.append('<text x="%.1f" y="%d" class="ax" text-anchor="middle">%s</text>'
                     % (cx, H - 8, d.strftime("%b") if len(data) <= 13 else d.strftime("%m")))
    parts.append("</svg>")
    return "".join(parts)


# CSV downloads: (page, table) -> (snapshot path, [(key, header)])
CSV_TABLES = {
    ("sales-tax", "periods"): (("salesTax", "periods"), [
        ("period", "Period"), ("liability", "Owed per books"), ("filed", "Filed"), ("paid", "Paid"),
        ("variance", "Filed minus owed"), ("jurisdictions", "Jurisdictions"),
        ("missingReturns", "Returns missing"), ("needsReview", "Needs review"), ("due", "Due"),
        ("status", "Status")]),
    ("sales-tax", "exceptions"): (("salesTax", "exceptions"), [
        ("severity", "Severity"), ("title", "Issue"), ("jurisdiction", "Jurisdiction"),
        ("period", "Period"), ("amount", "Amount"), ("action", "Suggested action")]),
    ("sales", "products"): (("sales", "byProduct"), [
        ("product", "Product"), ("sku", "SKU"), ("qty", "Quantity"), ("sales", "Sales")]),
    ("sales", "locations"): (("sales", "byLocation"), [
        ("group", "Group"), ("company", "Company"), ("location", "Location"),
        ("invoices", "Invoices"), ("sales", "Sales"), ("last", "Last invoice")]),
    ("sales", "orders"): (("sales", "openOrders"), [
        ("number", "Sales order"), ("date", "Date"), ("company", "Company"), ("location", "Location"),
        ("po", "PO"), ("status", "Status"), ("shipment", "Shipment"), ("amount", "Amount")]),
    ("receivables", "companies"): (("receivables", "byCompany"), [
        ("group", "Group"), ("company", "Company"), ("invoices", "Invoices"), ("current", "Not due yet"),
        ("1_30", "1-30 late"), ("31_60", "31-60 late"), ("61_90", "61-90 late"),
        ("90_plus", "Over 90 late"), ("total", "Total")]),
    ("receivables", "past-due"): (("receivables", "pastDue"), [
        ("invoice", "Invoice"), ("date", "Invoice date"), ("due", "Due"), ("daysLate", "Days late"),
        ("group", "Group"), ("company", "Company"), ("location", "Location"), ("po", "PO"),
        ("total", "Invoice total"), ("balance", "Balance due")]),
    ("cleanup", "bank"): (("cleanup", "bank", "uncategorized"), [
        ("date", "Date"), ("account", "Account"), ("description", "Description"),
        ("reference", "Reference"), ("direction", "In/out"), ("amount", "Amount")]),
    ("cleanup", "accounts"): (("cleanup", "coa", "issues"), [
        ("account", "Account"), ("code", "Number"), ("type", "Type"), ("issue", "Issue"),
        ("detail", "Detail")]),
}


BANK_COLS = [("date", "Date"), ("account", "Account"), ("description", "Description"), ("full", "Bank description"),
             ("check", "Check #"), ("amount", "Amount"), ("note", "Note"), ("notedBy", "Note by")]


def csv_rows(snap, page, table, period=None, bank=None):
    if table in ("bank-todo", "bank-recent", "bank-pending"):
        if not bank:
            return None
        rows = bank[{"bank-todo": "todo", "bank-recent": "recent", "bank-pending": "pending"}[table]]
        return [h for _, h in BANK_COLS], [[r.get(k) for k, _ in BANK_COLS] for r in rows]
    if page == "sales-tax" and table == "detail":
        rows = ((snap.get("salesTax") or {}).get("detail") or {}).get(period) or []
        cols = [("jurisdiction", "Jurisdiction"), ("code", "Code"), ("state", "State"),
                ("method", "Collection"), ("frequency", "Frequency"), ("liability", "Owed per books"),
                ("filed", "Filed"), ("paid", "Paid"), ("variance", "Variance"), ("status", "Status"),
                ("due", "Due")]
    else:
        spec = CSV_TABLES.get((page, table))
        if not spec:
            return None
        path, cols = spec
        rows = snap
        for k in path:
            rows = (rows or {}).get(k) if isinstance(rows, dict) else None
        rows = rows or []
    return [h for _, h in cols], [[r.get(k) for k, _ in cols] for r in rows]
