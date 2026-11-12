"""Download a client's workbook from OneDrive / SharePoint with Microsoft Graph (app-only)."""
import time
from urllib.parse import quote

import requests

_tokens = {}  # (tenant, client_id) -> (token, expires)


def _token(tenant, client_id, secret):
    key = (tenant, client_id)
    tok = _tokens.get(key)
    if tok and time.time() < tok[1] - 60:
        return tok[0]
    r = requests.post("https://login.microsoftonline.com/%s/oauth2/v2.0/token" % tenant,
                      data={"client_id": client_id, "client_secret": secret,
                            "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"},
                      timeout=20)
    r.raise_for_status()
    j = r.json()
    _tokens[key] = (j["access_token"], time.time() + int(j.get("expires_in", 3600)))
    return j["access_token"]


def download_workbook(tenant, client_id, secret, drive_user, file_path):
    url = "https://graph.microsoft.com/v1.0/users/%s/drive/root:/%s:/content" % (
        quote(drive_user), quote(file_path.strip("/")))
    r = requests.get(url, headers={"Authorization": "Bearer " + _token(tenant, client_id, secret)}, timeout=60)
    r.raise_for_status()
    return r.content


def list_folder(tenant, client_id, secret, drive_user, folder_path):
    """[(name, size, download_url)] for the files directly in a OneDrive folder."""
    url = "https://graph.microsoft.com/v1.0/users/%s/drive/root:/%s:/children?$top=200" % (
        quote(drive_user), quote(folder_path.strip("/")))
    out = []
    while url:
        r = requests.get(url, headers={"Authorization": "Bearer " + _token(tenant, client_id, secret)}, timeout=60)
        r.raise_for_status()
        j = r.json()
        out += [(it["name"], it.get("size", 0), it.get("@microsoft.graph.downloadUrl")) for it in j.get("value", []) if "file" in it]
        url = j.get("@odata.nextLink")
    return out


def download_url(url):
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return r.content


def fetch_linga_archive(c, env):
    """Files of the final Linga export: Data/LingaReportData/*.xlsx plus the Batch Report and Payroll Journal CSVs
    in the client's Data folder (the folder holding its workbook)."""
    import os
    import posixpath
    g = c["graph"]
    sec = env.get(g["secret_env"], "")
    data_dir = posixpath.dirname(g["file_path"].replace("\\", "/"))
    files = []
    for name, size, url in list_folder(g["tenant_id"], g["client_id"], sec, g["drive_user"], data_dir + "/LingaReportData"):
        if name.lower().endswith(".xlsx") and url:
            files.append((name, download_url(url)))
    for name, size, url in list_folder(g["tenant_id"], g["client_id"], sec, g["drive_user"], data_dir):
        low = name.lower()
        if url and low.endswith(".csv") and (low.startswith("batch report") or low.startswith("payroll journal")):
            files.append((name, download_url(url)))
    return files


def _item_path(drive_user, path):
    return "https://graph.microsoft.com/v1.0/users/%s/drive/root:/%s" % (quote(drive_user), quote(path.strip("/")))


def upload_file(tenant, client_id, secret, drive_user, path, data, content_type="application/octet-stream"):
    """Upload `data` to OneDrive at `path` (folders are created; a name clash gets a new name). Returns the drive item.
    Needs the app permission Files.ReadWrite.All. Small files in one request, larger ones with an upload session."""
    tok = _token(tenant, client_id, secret)
    hdr = {"Authorization": "Bearer " + tok}
    if len(data) <= 4_000_000:
        r = requests.put(_item_path(drive_user, path) + ":/content?@microsoft.graph.conflictBehavior=rename",
                         headers=dict(hdr, **{"Content-Type": content_type}), data=data, timeout=120)
        r.raise_for_status()
        return r.json()
    r = requests.post(_item_path(drive_user, path) + ":/createUploadSession", headers=hdr,
                      json={"item": {"@microsoft.graph.conflictBehavior": "rename"}}, timeout=30)
    r.raise_for_status()
    url, size, chunk, pos = r.json()["uploadUrl"], len(data), 16 * 327_680, 0
    while pos < size:
        part = data[pos:pos + chunk]
        r = requests.put(url, data=part, timeout=120, headers={"Content-Length": str(len(part)),
                                                              "Content-Range": "bytes %d-%d/%d" % (pos, pos + len(part) - 1, size)})
        r.raise_for_status()
        pos += len(part)
    return r.json()


def download_item(tenant, client_id, secret, drive_user, item_id):
    url = "https://graph.microsoft.com/v1.0/users/%s/drive/items/%s/content" % (quote(drive_user), quote(item_id))
    r = requests.get(url, headers={"Authorization": "Bearer " + _token(tenant, client_id, secret)}, timeout=60)
    r.raise_for_status()
    return r.content
