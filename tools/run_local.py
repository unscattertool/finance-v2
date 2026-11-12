"""Run Finance v2 on this computer only (http://127.0.0.1:5050). Started by "Run Finance v2.cmd".

First run creates instance-local\\ (not in git) with:
  users.json   one admin login: admin@local and a password shown in the window (also saved in instance-local\\LOGIN.txt)
  clients.json a "crs" client (published from the Platform; its chart of accounts can be built from a Zoho export)
               and an empty "demo" books client
To try WWK's real data, copy a books.db / clients.json from the live server into instance-local\\ yourself.
"""
import json
import os
import secrets
import sys
import threading
import webbrowser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
INST = os.path.join(ROOT, "instance-local")


def first_run():
    from werkzeug.security import generate_password_hash
    os.makedirs(INST, exist_ok=True)
    key_file = os.path.join(INST, ".secret")
    if not os.path.exists(key_file):
        open(key_file, "w").write(secrets.token_urlsafe(48))
    users = os.path.join(INST, "users.json")
    if not os.path.exists(users):
        pw = secrets.token_urlsafe(9)
        json.dump({"users": {"admin@local": {"name": "Local admin", "clients": ["*"], "admin": True,
                                             "hash": generate_password_hash(pw)}}}, open(users, "w"), indent=1)
        open(os.path.join(INST, "LOGIN.txt"), "w").write("Finance v2 (local)\nhttp://127.0.0.1:5050\nemail: admin@local\npassword: %s\n" % pw)
    clients = os.path.join(INST, "clients.json")
    if not os.path.exists(clients):
        json.dump({"clients": {
            "crs": {"name": "CRS (Cash Register Systems)", "source": "platform", "push_token_env": "PUSH_TOKEN_CRS",
                    "modules": ["sales_tax", "sales", "receivables", "cleanup"]},
            "demo": {"name": "Demo books client", "books": True},
        }}, open(clients, "w"), indent=1)
    return open(key_file).read().strip()


def main():
    os.environ["PORTAL_SECRET_KEY"] = first_run()
    os.environ["PORTAL_INSECURE_DEV"] = "1"  # plain http on this computer only
    from portal import create_app
    app = create_app(instance_path=INST)
    print("\n  Finance v2 is running at http://127.0.0.1:5050  (close this window to stop it)")
    print("  " + open(os.path.join(INST, "LOGIN.txt")).read().replace("\n", "\n  "))
    threading.Timer(1.5, lambda: webbrowser.open("http://127.0.0.1:5050")).start()
    app.run(host="127.0.0.1", port=5050, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
