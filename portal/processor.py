"""Card processor reporting (Heartland / Global Payments "Portico" gateway): pulls card transactions and batches
straight from the processor, so card sales, tips and deposits don't depend on the POS's reports.

clients.json: "processor": {"provider": "portico", "env": "cert" | "prod", "key_env": "HEARTLAND_SECRET_KEY"}
(the secret API key itself lives only in the app's environment variables).

Reads only: ReportActivity (every transaction in a time range), ReportTxnDetail (tip/gratuity, batch) and
ReportBatchHistory (each closed batch = one bank deposit). Never charges, voids or refunds anything.
Results: processor_txn / processor_batch tables, and per business day card sales + tips in sales_day
(source 'processor') for days the Linga batch report doesn't cover, so card deposits keep matching.
"""
import datetime as dt
import json
import os
import re
import xml.etree.ElementTree as ET

import requests

URLS = {"cert": "https://cert.api2.heartlandportico.com/Hps.Exchange.PosGateway/PosGatewayService.asmx",
        "prod": "https://api2.heartlandportico.com/Hps.Exchange.PosGateway/PosGatewayService.asmx"}
NS = "{http://Hps.Exchange.PosGateway}"
SCHEMA = """
CREATE TABLE IF NOT EXISTS processor_txn (
  client TEXT NOT NULL, txn_id TEXT NOT NULL, provider TEXT, at_utc TEXT, business_date TEXT, service TEXT, status TEXT,
  card TEXT, card_type TEXT, amount REAL, auth_amount REAL, settle_amount REAL, tip REAL, batch_id TEXT, device_id TEXT,
  clerk TEXT, raw TEXT, fetched_at TEXT, PRIMARY KEY (client, txn_id));
CREATE INDEX IF NOT EXISTS processor_txn_d ON processor_txn(client, business_date);
CREATE TABLE IF NOT EXISTS processor_batch (
  client TEXT NOT NULL, batch_id TEXT NOT NULL, provider TEXT, opened_utc TEXT, closed_utc TEXT, txn_count INTEGER,
  amount REAL, raw TEXT, fetched_at TEXT, PRIMARY KEY (client, batch_id));
"""
SALE_SERVICES = {"CreditSale", "CreditAuth", "CreditAddToBatch", "CreditOfflineSale", "CreditOfflineAuth", "DebitSale",
                 "CreditIncrementalAuth"}
RETURN_SERVICES = {"CreditReturn", "DebitReturn"}
TZ_OFFSET_HOURS = -6  # Central time for business dates (close enough: business days end by 2 AM)


class ProcessorError(Exception):
    pass


def ensure(db):
    db.executescript(SCHEMA)


def config(c):
    p = c.get("processor") or {}
    if p.get("provider") != "portico":
        return None
    key = os.environ.get(p.get("key_env", "HEARTLAND_SECRET_KEY"), "").strip()
    return dict(p, key=key, url=URLS.get(p.get("env", "prod"), URLS["prod"])) if key else None


def _call(cfg, name, inner="", timeout=60):
    body = ('<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
            '<PosRequest xmlns="http://Hps.Exchange.PosGateway"><Ver1.0><Header><SecretAPIKey>%s</SecretAPIKey>'
            '</Header><Transaction><%s>%s</%s></Transaction>'
            '</Ver1.0></PosRequest></soap:Body></soap:Envelope>') % (_esc(cfg["key"]), name, inner, name)
    r = requests.post(cfg["url"], data=body.encode(), headers={"Content-Type": "text/xml; charset=utf-8"}, timeout=timeout)
    if r.status_code != 200:
        raise ProcessorError("Heartland answered HTTP %s" % r.status_code)
    root = ET.fromstring(r.content)
    ver = root.find(".//%sVer1.0" % NS)
    hdr = ver.find(NS + "Header") if ver is not None else None
    code = (hdr.findtext(NS + "GatewayRspCode") if hdr is not None else None) or "0"
    if code not in ("0", "00"):
        msg = hdr.findtext(NS + "GatewayRspMsg") if hdr is not None else ""
        raise ProcessorError("Heartland: %s (code %s)" % (msg or "error", code))
    tx = ver.find(NS + "Transaction")
    return tx.find(NS + name) if tx is not None else None


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _todict(el):
    """XML element → dict of child tag (no namespace) → text or nested dict."""
    out = {}
    for ch in list(el):
        tag = ch.tag.replace(NS, "")
        val = _todict(ch) if len(ch) else (ch.text or "").strip()
        if tag in out:
            if not isinstance(out[tag], list):
                out[tag] = [out[tag]]
            out[tag].append(val)
        else:
            out[tag] = val
    return out


def _num(v):
    if isinstance(v, dict):
        v = v.get("GratuityAmt") or v.get("Amt") or ""
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _bdate(utc_str):
    try:
        t = dt.datetime.fromisoformat(utc_str.replace("Z", "")[:19])
    except ValueError:
        return None
    local = t + dt.timedelta(hours=TZ_OFFSET_HOURS)
    if local.hour < 4:  # after-midnight closes belong to the previous business day
        local -= dt.timedelta(days=1)
    return local.date().isoformat()


def _utc(d):
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def activity(cfg, start_utc, end_utc):
    el = _call(cfg, "ReportActivity", "<RptStartUtcDT>%s</RptStartUtcDT><RptEndUtcDT>%s</RptEndUtcDT>" % (_utc(start_utc), _utc(end_utc)))
    if el is None:
        return []
    return [_todict(d) for d in el.findall(NS + "Details")]


def detail(cfg, txn_id):
    el = _call(cfg, "ReportTxnDetail", "<TxnId>%s</TxnId>" % _esc(str(txn_id)))
    return _todict(el) if el is not None else {}


def batches(cfg, start_utc, end_utc):
    el = _call(cfg, "ReportBatchHistory", "<RptStartUtcDT>%s</RptStartUtcDT><RptEndUtcDT>%s</RptEndUtcDT>" % (_utc(start_utc), _utc(end_utc)))
    if el is None:
        return []
    return [_todict(d) for d in el.findall(NS + "Details")]


def pull(db, client, c, start, end, details=True):
    """Fetch transactions and batches for business dates start..end (date objects). Returns a summary."""
    ensure(db)
    cfg = config(c)
    if not cfg:
        raise ProcessorError("No card processor set up for this client (clients.json \"processor\" + the secret key env var).")
    ts = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    a = dt.datetime.combine(start, dt.time(4)) - dt.timedelta(hours=TZ_OFFSET_HOURS)          # 4 AM local, in UTC
    b = dt.datetime.combine(end + dt.timedelta(days=1), dt.time(4)) - dt.timedelta(hours=TZ_OFFSET_HOURS)
    n_tx = n_bt = 0
    cur = a
    while cur < b:  # one day per request keeps responses small
        nxt = min(cur + dt.timedelta(days=1), b)
        for t in activity(cfg, cur, nxt):
            tid = str(t.get("GatewayTxnId") or "")
            if not tid:
                continue
            d = detail(cfg, tid) if details and t.get("ServiceName") in SALE_SERVICES | RETURN_SERVICES else {}
            merged = dict(t, **{k: v for k, v in d.items() if v not in (None, "")})
            tip = _num(merged.get("GratuityAmtInfo"))
            amt, auth, settle = _num(merged.get("Amt")), _num(merged.get("AuthAmt")), _num(merged.get("SettlementAmt"))
            when = merged.get("TxnUtcDT") or merged.get("ReqUtcDT") or ""
            db.execute("""INSERT OR REPLACE INTO processor_txn VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (client, tid, "portico", when, _bdate(when), merged.get("ServiceName"), merged.get("TxnStatus") or merged.get("Status"),
                        merged.get("MaskedCardNbr"), merged.get("CardType") or "", amt, auth, settle, tip,
                        str(merged.get("BatchId") or merged.get("BatchSeqNbr") or ""), str(merged.get("DeviceId") or ""),
                        str(merged.get("ClerkID") or merged.get("ClerkId") or ""), json.dumps(merged)[:20000], ts))
            n_tx += 1
        cur = nxt
    for bt in batches(cfg, a - dt.timedelta(days=1), b + dt.timedelta(days=1)):
        bid = str(bt.get("BatchId") or "")
        if not bid:
            continue
        db.execute("INSERT OR REPLACE INTO processor_batch VALUES(?,?,?,?,?,?,?,?,?)",
                   (client, bid, "portico", bt.get("OpenUtcDT"), bt.get("CloseUtcDT"), int(_num(bt.get("TxnCnt")) or 0),
                    _num(bt.get("TotalAmt")), json.dumps(bt)[:20000], ts))
        n_bt += 1
    days = to_sales_days(db, client, start.isoformat(), end.isoformat())
    db.execute("INSERT INTO meta(client,k,v) VALUES(?,?,?) ON CONFLICT(client,k) DO UPDATE SET v=excluded.v",
               (client, "processor_pulled_at", ts))
    db.commit()
    return dict(transactions=n_tx, batches=n_bt, days=days)


def to_sales_days(db, client, a, b):
    """Card sales and card tips per business day from processor transactions (active sales minus returns), written to
    sales_day as source 'processor' for days with no Linga batch report."""
    rows = db.execute("""SELECT business_date d, service s, status st, coalesce(settle_amount, amount) amt, coalesce(tip, 0) tip
                         FROM processor_txn WHERE client=? AND business_date BETWEEN ? AND ?""", (client, a, b)).fetchall()
    days = {}
    for r in rows:
        if (r["st"] or "A").upper() not in ("A", "ACTIVE", "C", "CLOSED", ""):
            continue  # voided / reversed
        x = days.setdefault(r["d"], [0.0, 0.0])
        sign = -1 if r["s"] in RETURN_SERVICES else 1 if r["s"] in SALE_SERVICES else 0
        x[0] += sign * ((r["amt"] or 0) - (r["tip"] or 0))
        x[1] += sign * (r["tip"] or 0)
    n = 0
    for d, (sales, tips) in days.items():
        have = db.execute("SELECT source FROM sales_day WHERE client=? AND date=?", (client, d)).fetchone()
        if have and have["source"] == "batch":
            continue
        db.execute("""INSERT INTO sales_day(client,date,card_sales,card_tips,cash_sales,cash_tips,shifts,source,fetched_at)
                      VALUES(?,?,?,?,0,0,0,'processor',?) ON CONFLICT(client,date) DO UPDATE SET card_sales=excluded.card_sales,
                      card_tips=excluded.card_tips, source='processor', fetched_at=excluded.fetched_at""",
                   (client, d, round(sales, 2), round(tips, 2), dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
        n += 1
    return n


def status(db, client, c):
    ensure(db)
    cfg = c.get("processor") or {}
    r = db.execute("SELECT count(*), min(business_date), max(business_date) FROM processor_txn WHERE client=?", (client,)).fetchone()
    pulled = db.execute("SELECT v FROM meta WHERE client=? AND k='processor_pulled_at'", (client,)).fetchone()
    return dict(configured=bool(config(c)), provider=cfg.get("provider"), env=cfg.get("env"), key_env=cfg.get("key_env", "HEARTLAND_SECRET_KEY"),
                txns=r[0], first=r[1], last=r[2], pulled_at=pulled[0] if pulled else None,
                batches=db.execute("SELECT count(*) FROM processor_batch WHERE client=?", (client,)).fetchone()[0])
