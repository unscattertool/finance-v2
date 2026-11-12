# Client Finance Portal

A small, password-protected, read-only website. It shows each client's balance, pending bank items, bills due, and payroll.
One installation serves any number of clients. Adding a client means adding a block of settings, not new code.

---

## Install on Namecheap (cPanel), about 20 minutes

1. **Subdomain.** In cPanel → Domains, create `finance.unscatterllc.com`. Under SSL/TLS Status, run **AutoSSL** so it has HTTPS.
2. **Upload.** In File Manager, create `/home/<you>/pythonapps/finance-portal` (NOT inside public_html). Upload this zip and Extract it there.
3. **Python app.** In cPanel → **Setup Python App** → Create Application:
   - Python version: 3.9 or newer
   - Application root: `pythonapps/finance-portal`
   - Application URL: `finance.unscatterllc.com`
   - Application startup file: `passenger_wsgi.py`, Entry point: `application`
4. **Packages.** On the same screen, copy the "Enter to the virtual environment" command. Open cPanel **Terminal**, paste it, then run:
   ```
   pip install -r requirements.txt
   python manage.py new-secret
   ```
5. **Environment variables** (Setup Python App → your app → Environment variables):
   | Name | Value |
   |---|---|
   | `PORTAL_SECRET_KEY` | the value `new-secret` printed (required, or the app won't start) |
   | `PORTAL_TITLE` | `Unscatter Client Finance` (optional) |
   | `PORTAL_FRAME_ANCESTORS` | `https://shifts.unscatterllc.com` (only if you want to embed it in shifts) |
   | `GRAPH_SECRET_UNSCATTER` | Microsoft app secret (see step 7) |
6. **Clients.** Copy `instance.example/clients.json` to `instance/clients.json` and fill in the values (step 7).
7. **Microsoft connection** (once for your own Microsoft 365, and all clients whose workbooks live in your OneDrive can share it):
   - entra.microsoft.com → App registrations → New registration ("Finance Portal").
   - API permissions → Microsoft Graph → Application → `Files.Read.All` → Grant admin consent.
   - Certificates & secrets → New client secret → put the value in `GRAPH_SECRET_UNSCATTER`.
   - Copy the Directory (tenant) ID and Application (client) ID into `clients.json`.
8. **Your login.** In Terminal (inside the virtual environment):
   ```
   python manage.py add-user tammy@unscatterllc.com --name "Tammy" --admin
   python manage.py test-client wwk
   ```
9. **Restart** the app in Setup Python App, then open `https://finance.unscatterllc.com`.

## Day to day

| Task | Command (cPanel Terminal, inside the virtual environment) |
|---|---|
| Give someone access to one client | `python manage.py add-user name@example.com --name "Name" --clients wwk` |
| Reset a password (also signs them out everywhere) | `python manage.py set-password name@example.com` |
| Remove access now | `python manage.py disable-user name@example.com` |
| See who has access | `python manage.py list` |

Changes take effect right away; no restart is needed. Sign-ins and failed attempts are logged in `instance/access.log`.

## Add another client

1. Give their workbook the same tabs as the WWK workbook (Checkbook, Transactions, Accounts, Bills, Recurring).
2. Add a block to `instance/clients.json` with a short id (used in the URL `/c/<id>/`), the display name, and where the file lives:
   - **Their file is in your OneDrive:** reuse your `tenant_id`, `client_id` and `secret_env`. Only `file_path` changes.
   - **Their file is in their own Microsoft 365:** register an app in *their* tenant and add its secret as a new environment variable, e.g. `GRAPH_SECRET_CLIENTB`.
   - **No Microsoft connection at all:** set `"source": "push"` and `"push_token_env": "PUSH_TOKEN_CLIENTB"`. A Power Automate flow then sends the "Export Snapshot" script result to `https://finance.unscatterllc.com/api/<id>/snapshot` with header `Authorization: Bearer <token>`.
3. `"show"` hides sections per client, e.g. `{"payroll": false}`.

## Linking from the shifts site

Ask ChatGPT to add a "Finance" link to `https://finance.unscatterllc.com`, or an embedded panel:
`<iframe src="https://finance.unscatterllc.com/c/wwk/" style="width:100%;height:900px;border:0"></iframe>`
(Embedding needs `PORTAL_FRAME_ANCESTORS` set, as in step 5. People sign in to the portal itself; shifts never sees the numbers or the passwords.)

## Security summary
- Passwords are stored only as salted scrypt hashes. Sign-ins lock for 15 minutes after 5 failed tries.
- HTTPS only. Session cookies are Secure, HttpOnly and SameSite. Sessions end after 8 hours; changing a password ends them sooner.
- Every form is protected against cross-site requests. Strict security headers are set, and the site asks search engines not to index it.
- Each user sees only the clients assigned to them. Unassigned clients show "not found".
- Secrets live in environment variables. Settings and users live in `instance/`, outside the web root.
- The portal is read-only: it never changes a workbook.
- Shared hosting is the weak point: anyone with your cPanel login can read the secrets. Use a strong cPanel password with two-factor authentication.
- `Files.Read.All` lets the app read any file in that Microsoft 365 tenant. Keep the secret private, rotate it yearly, or have your admin restrict it with `Sites.Selected`.
