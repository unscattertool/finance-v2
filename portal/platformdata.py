"""Clean client data from the Unscatter platform ("platform data package").

The platform is where client data is cleaned: customers (business entities), locations, contacts, vendors,
chart of accounts, invoices, payments, sales tax. It saves that as one package in the client's OneDrive folder
(<client>/Data/Platform/platform-data.json.gz + manifest.json) and can also push it here
(POST /api/<slug>/platform-data).

Finance uses the platform's copy of a dataset when the package has it and keeps using its own data when it
doesn't, so a client doesn't have to be in the platform, and a books client (WWK) can use platform data too.

clients.json, per client (all optional):
  "platform": {
    "push_token_env": "PUSH_TOKEN_CRS",          # token the platform sends (falls back to "push_token_env" for
                                                 #   "source": "platform" clients)
    "onedrive_folder": "Clients/CRS/Data/Platform",  # pull the package from OneDrive with Microsoft Graph
    "graph": {...},                              # Graph app (tenant_id, client_id, secret_env, drive_user);
                                                 #   defaults to the client's own "graph" block
    "use": ["entities", "accounts", ...],        # only these datasets (default: everything in the package)
    "ignore": ["invoices"]                       # never these
  }
"""
import gzip
import hashlib
import io
import json
import os
import threading

KIND = "unscatter-platform-data"
MANIFEST_KIND = "unscatter-platform-manifest"
VERSION = 1
MAX_COMPRESSED = 40 * 1024 * 1024
MAX_JSON = 400 * 1024 * 1024
PACKAGE_FILE = "platform-data.json.gz"
MANIFEST_FILE = "manifest.json"

# dataset -> (label, what Finance uses instead when the platform doesn't have it)
DATASETS = {
    "groups": ("Business groups", None),
    "entities": ("Customers", "Payee names on the register"),
    "locations": ("Locations", None),
    "contacts": ("Contacts", None),
    "contactLinks": ("Contact links", None),
    "vendors": ("Vendors", "Payee names and bills"),
    "accounts": ("Chart of accounts", "Finance categories / Zoho export upload"),
    "products": ("Products", "POS item sales"),
    "invoices": ("Invoices", None),
    "customerPayments": ("Customer payments", "Deposits on the register"),
    "vendorPayments": ("Vendor payments", "Checks and payments on the register"),
    "expenses": ("Expenses", "The register"),
    "transactions": ("Account transactions", "The register / bank feed"),
    "taxJurisdictions": ("Tax jurisdictions", "Sales tax settings"),
    "salesTax": ("Sales tax by period", "Sales tax report from POS sales"),
}

_cache = {}
_cache_lock = threading.Lock()


class Rejected(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


# ------------------------------------------------------------------ config
def enabled(c):
    """Does this client take data from the platform at all?"""
    return bool(c.get("platform")) or c.get("source") == "platform"


def cfg(c):
    p = c.get("platform")
    return p if isinstance(p, dict) else {}


def token_env(c):
    p = cfg(c)
    if p.get("push_token_env"):
        return p["push_token_env"]
    return c.get("push_token_env") if c.get("source") == "platform" else None


def graph_cfg(c):
    p = cfg(c)
    g = dict(c.get("graph") or {})
    g.update(p.get("graph") or {})
    if not p.get("onedrive_folder") or not all(g.get(k) for k in ("tenant_id", "client_id", "secret_env", "drive_user")):
        return None
    g["folder"] = p["onedrive_folder"].replace("\\", "/").strip("/")
    return g


def allowed(c, name):
    p = cfg(c)
    use, ignore = p.get("use"), p.get("ignore") or []
    return name not in ignore and (not use or name in use)


# ------------------------------------------------------------------ files in instance/
def _safe(slug):
    return "".join(ch for ch in slug if ch.isalnum() or ch in "-_")


def file_name(slug):
    return "platform_data_%s.json.gz" % _safe(slug)


def meta_name(slug):
    return "platform_data_%s.meta.json" % _safe(slug)


def parse(raw, encoding=None):
    """Body bytes (gzip or plain JSON) -> (package dict, gzip bytes to store), or raise Rejected."""
    if len(raw) > MAX_COMPRESSED:
        raise Rejected(413, "too large")
    gz = (encoding or "").lower() == "gzip" or raw[:2] == b"\x1f\x8b"
    if gz:
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:
                body = f.read(MAX_JSON + 1)
        except (OSError, EOFError):
            raise Rejected(400, "bad gzip")
    else:
        body = raw
    if len(body) > MAX_JSON:
        raise Rejected(413, "too large")
    try:
        pkg = json.loads(body.decode("utf-8"))
    except ValueError:
        raise Rejected(400, "not json")
    if not isinstance(pkg, dict) or pkg.get("kind") != KIND:
        raise Rejected(400, "not a platform data package")
    try:
        version = int(pkg.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version < 1:
        raise Rejected(400, "missing version")
    if version > VERSION:
        raise Rejected(400, "newer package version than this Finance build understands; update Finance")
    ds = pkg.get("datasets")
    if not isinstance(ds, dict):
        raise Rejected(400, "missing datasets")
    for name, d in ds.items():
        if not isinstance(d, dict) or not isinstance(d.get("rows"), list):
            raise Rejected(400, "dataset %s has no rows" % name)
    rep = pkg.get("reports")
    if rep is not None and (not isinstance(rep, dict) or not isinstance(rep.get("modules"), list)):
        raise Rejected(400, "bad reports")
    return pkg, (raw if gz else gzip.compress(raw, mtime=0))


def store(instance_path, slug, pkg, gz, via):
    """Save the package (keeping the previous one) and a small description next to it. Returns that description."""
    import datetime as dt
    path = os.path.join(instance_path, file_name(slug))
    if os.path.exists(path):
        os.replace(path, path[:-8] + ".prev.json.gz")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(gz)
    os.replace(tmp, path)
    meta = {
        "receivedAt": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "via": via,
        "sha256": hashlib.sha256(gz).hexdigest(),
        "bytes": len(gz),
        "asOf": pkg.get("asOf"),
        "generatedAt": pkg.get("generatedAt"),
        "historySince": pkg.get("historySince"),
        "client": pkg.get("client"),
        "datasets": {k: {"count": int(v.get("count") or len(v.get("rows") or [])), "updatedAt": v.get("updatedAt")}
                     for k, v in pkg["datasets"].items()},
        "reports": (pkg.get("reports") or {}).get("modules") or [],
    }
    tmp = os.path.join(instance_path, meta_name(slug) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=1)
    os.replace(tmp, os.path.join(instance_path, meta_name(slug)))
    with _cache_lock:
        _cache.pop((instance_path, slug), None)
    return meta


def meta(instance_path, slug):
    try:
        with open(os.path.join(instance_path, meta_name(slug)), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def load(instance_path, slug):
    """The stored package (cached until the file changes), or None."""
    path = os.path.join(instance_path, file_name(slug))
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    key = (instance_path, slug)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] == mtime:
            return hit[1]
    with open(path, "rb") as f:
        pkg = json.loads(gzip.decompress(f.read()).decode("utf-8"))
    with _cache_lock:
        _cache[key] = (mtime, pkg)
    return pkg


# ------------------------------------------------------------------ what Finance uses
def rows(instance_path, c, name):
    """Rows of a platform dataset this client uses, or None (= use Finance's own data)."""
    if not enabled(c) or not allowed(c, name):
        return None
    pkg = load(instance_path, c["slug"])
    d = ((pkg or {}).get("datasets") or {}).get(name)
    return d.get("rows") if d and d.get("rows") else None


def dataset(instance_path, c, name):
    if rows(instance_path, c, name) is None:
        return None
    return load(instance_path, c["slug"])["datasets"][name]


def sources(instance_path, c):
    """[(name, label, 'platform' | 'finance' | 'none', count, fallback text)] for the Data sources page."""
    m = meta(instance_path, c["slug"]) if enabled(c) else None
    have = (m or {}).get("datasets") or {}
    out = []
    for name, (label, fallback) in DATASETS.items():
        n = (have.get(name) or {}).get("count") or 0
        if n and allowed(c, name):
            out.append((name, label, "platform", n, fallback))
        elif n:
            out.append((name, label, "off", n, fallback))
        else:
            out.append((name, label, "finance" if fallback else "none", 0, fallback))
    return out


def directory(instance_path, c):
    """Customers with their locations and contacts, and vendors, from the platform (for the Directory page)."""
    ents = rows(instance_path, c, "entities") or []
    locs = rows(instance_path, c, "locations") or []
    cons = {x["id"]: x for x in rows(instance_path, c, "contacts") or []}
    links = rows(instance_path, c, "contactLinks") or []
    groups = {x["id"]: x.get("name") for x in rows(instance_path, c, "groups") or []}
    by_ent_loc, by_ent_con = {}, {}
    loc_by_id = {x["id"]: x for x in locs}
    for loc in locs:
        if loc.get("entityId"):
            by_ent_loc.setdefault(loc["entityId"], []).append(loc)
    for e in ents:  # billing / shipping locations not tied through a billing account
        for lid in (e.get("billingLocationId"), e.get("shippingLocationId")):
            if lid and lid in loc_by_id and loc_by_id[lid] not in by_ent_loc.get(e["id"], []):
                by_ent_loc.setdefault(e["id"], []).append(loc_by_id[lid])
    for ln in links:
        con = cons.get(ln.get("contactId"))
        if con and ln.get("entityId"):
            by_ent_con.setdefault(ln["entityId"], []).append(dict(con, role=ln.get("role"), primary=ln.get("primary")))
    customers = [dict(e, group=groups.get(e.get("groupId")), locations=by_ent_loc.get(e["id"], []),
                      contacts=by_ent_con.get(e["id"], [])) for e in ents]
    customers.sort(key=lambda e: ((e.get("group") or "~").lower(), (e.get("name") or "").lower()))
    vendors = sorted(rows(instance_path, c, "vendors") or [], key=lambda v: (v.get("name") or "").lower())
    return customers, vendors


def coa_items(instance_path, c):
    """The platform's chart of accounts as chart-of-accounts source items (see coa.import_sources).
    Accounts that came from Zoho keep the Zoho account id as their key, so mappings made from a Zoho export
    upload carry over."""
    from .coa import ZOHO_TYPES
    accts = rows(instance_path, c, "accounts")
    if not accts:
        return None, None
    names = {a["id"]: a.get("name") for a in accts}
    by_source = {"zoho": [], "platform": []}
    seen = set()
    for a in accts:
        name = (a.get("name") or "").strip()
        if not name:
            continue
        src = a.get("source") or {}
        if (src.get("system") or "").lower().startswith("zoho") and src.get("id"):
            source, key = "zoho", str(src["id"])
        else:
            source, key = "platform", str(a["id"])
        if (source, key) in seen:
            continue
        seen.add((source, key))
        ztype = (a.get("type") or "").strip()
        by_source[source].append(dict(
            key=key, name=name, ztype=ztype,
            type=ZOHO_TYPES.get(ztype.lower().replace("_", " "), "expense" if not ztype else "other_current_asset"),
            code=(a.get("code") or None), active=1 if a.get("active", True) else 0,
            parent=a.get("parentName") or names.get(a.get("parentId")) or None, description=""))
    return by_source, len(accts)


# ------------------------------------------------------------------ OneDrive
def pull_onedrive(instance_path, c, env=os.environ, force=False):
    """Fetch the package from the client's OneDrive Platform folder when the manifest says it changed.
    Returns (meta or None, message)."""
    g = graph_cfg(c)
    if not g:
        return None, "No OneDrive folder set for this client's platform data."
    from . import graph
    secret = env.get(g["secret_env"], "")
    try:
        man = json.loads(graph.download_workbook(g["tenant_id"], g["client_id"], secret, g["drive_user"],
                                                 g["folder"] + "/" + MANIFEST_FILE).decode("utf-8-sig"))
    except Exception as e:
        return None, "Couldn't read the platform manifest from OneDrive (%s)." % type(e).__name__
    if man.get("kind") != MANIFEST_KIND:
        return None, "The OneDrive manifest isn't a platform manifest."
    have = meta(instance_path, c["slug"]) or {}
    if not force and have.get("sha256") and have.get("sha256") == man.get("sha256"):
        return have, "Already up to date (%s)." % (man.get("generatedAt") or "")
    try:
        raw = graph.download_workbook(g["tenant_id"], g["client_id"], secret, g["drive_user"],
                                      g["folder"] + "/" + (man.get("file") or PACKAGE_FILE))
    except Exception as e:
        return None, "Couldn't download the platform data from OneDrive (%s)." % type(e).__name__
    if man.get("sha256") and hashlib.sha256(raw).hexdigest() != man["sha256"]:
        return None, "The OneDrive copy is still syncing (checksum doesn't match the manifest yet). Try again shortly."
    try:
        pkg, gz = parse(raw, "gzip")
    except Rejected as e:
        return None, "The OneDrive platform data was rejected: %s." % e
    return store(instance_path, c["slug"], pkg, gz, "onedrive"), "Loaded the platform data from OneDrive."
