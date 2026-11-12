"""Minimal .xlsx reader (first worksheet → list of rows) using only the standard library.

Some POS exports (Linga) write column metadata openpyxl rejects ("Invalid column index 0"); this reads the cell
values directly from the sheet XML instead.
"""
import io
import re
import zipfile
import xml.etree.ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _col(ref):
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _text(si):
    return "".join(t.text or "" for t in si.iter("{%s}t" % NS["m"]))


def rows(data, sheet=0):
    z = zipfile.ZipFile(io.BytesIO(data))
    names = z.namelist()
    shared = []
    if "xl/sharedStrings.xml" in names:
        shared = [_text(si) for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS)]
    # resolve the sheet's path through the workbook relationships
    path = None
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        rid = wb.findall("m:sheets/m:sheet", NS)[sheet].get(_R)
        target = next(r.get("Target") for r in rels if r.get("Id") == rid)
        path = target.lstrip("/") if target.startswith("/") else "xl/" + target
    except Exception:
        pass
    if not path or path not in names:
        path = sorted(n for n in names if n.startswith("xl/worksheets/sheet"))[sheet]
    out = []
    for row in ET.fromstring(z.read(path)).iter("{%s}row" % NS["m"]):
        vals = {}
        for c in row.findall("m:c", NS):
            ref, t = c.get("r") or "", c.get("t")
            v = c.find("m:v", NS)
            if t == "s" and v is not None:
                val = shared[int(v.text)]
            elif t == "inlineStr":
                is_ = c.find("m:is", NS)
                val = _text(is_) if is_ is not None else ""
            elif v is None or v.text is None:
                val = None
            elif t in ("str", "e"):
                val = v.text
            elif t == "b":
                val = v.text == "1"
            else:
                try:
                    f = float(v.text)
                    val = int(f) if f.is_integer() and "." not in v.text and "E" not in v.text.upper() else f
                except ValueError:
                    val = v.text
            vals[_col(ref) if ref else len(vals)] = val
        if vals:
            r = [None] * (max(vals) + 1)
            for k, v in vals.items():
                r[k] = v.strip() if isinstance(v, str) else v
            out.append(r)
    width = max((len(r) for r in out), default=0)
    return [r + [None] * (width - len(r)) for r in out]
