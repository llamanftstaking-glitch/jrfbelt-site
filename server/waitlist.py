#!/usr/bin/env python3
"""JRF waitlist API — stdlib only, no dependencies.

POST /api/waitlist          form field or JSON: email  → stores signup, redirects to /?joined=1
GET  /api/waitlist.csv      ?token=ADMIN_TOKEN         → CSV export of all signups
GET  /api/waitlist/count    ?token=ADMIN_TOKEN         → {"count": N}

Listens on 127.0.0.1:8127 — only reachable through Caddy.
"""
import json, os, re, sqlite3, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DB = os.environ.get("WAITLIST_DB", "/opt/jrfbelt/waitlist.db")
ADMIN_TOKEN = os.environ["ADMIN_TOKEN"]
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
MAX_BODY = 4096

con = sqlite3.connect(DB, check_same_thread=False)
con.execute("""CREATE TABLE IF NOT EXISTS signups(
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    created TEXT NOT NULL,
    ip TEXT, user_agent TEXT)""")
con.commit()


class Handler(BaseHTTPRequestHandler):
    server_version = "jrf"

    def _send(self, code, body=b"", ctype="text/plain", extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _client_ip(self):
        return self.headers.get("X-Forwarded-For", "").split(",")[0].strip() \
            or self.client_address[0]

    def _authed(self, q):
        return (q.get("token") or [""])[0] == ADMIN_TOKEN

    def do_POST(self):
        if urlparse(self.path).path != "/api/waitlist":
            return self._send(404, b"not found")
        length = min(int(self.headers.get("Content-Length", 0)), MAX_BODY)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        ctype = self.headers.get("Content-Type", "")
        if "json" in ctype:
            try:
                email = (json.loads(raw or "{}").get("email") or "").strip().lower()
            except json.JSONDecodeError:
                email = ""
        else:
            email = (parse_qs(raw).get("email") or [""])[0].strip().lower()
        if not EMAIL_RE.match(email) or len(email) > 254:
            return self._send(400, b'{"ok":false,"error":"invalid email"}',
                              "application/json")
        try:
            con.execute("INSERT INTO signups(email, created, ip, user_agent) VALUES(?,?,?,?)",
                        (email, time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime()),
                         self._client_ip(), self.headers.get("User-Agent", "")[:300]))
            con.commit()
        except sqlite3.IntegrityError:
            pass  # duplicate signup — treat as success, don't leak membership
        if "json" in (self.headers.get("Accept") or ""):
            return self._send(200, b'{"ok":true}', "application/json")
        return self._send(303, b"", extra=(("Location", "/?joined=1#waitlist"),))

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/waitlist/count":
            if not self._authed(q):
                return self._send(401, b"unauthorized")
            n = con.execute("SELECT COUNT(*) FROM signups").fetchone()[0]
            return self._send(200, json.dumps({"count": n}).encode(), "application/json")
        if u.path == "/api/waitlist.csv":
            if not self._authed(q):
                return self._send(401, b"unauthorized")
            rows = con.execute(
                "SELECT email, created, ip FROM signups ORDER BY id").fetchall()
            body = "email,created,ip\n" + "\n".join(
                ",".join(str(x) for x in r) for r in rows)
            return self._send(200, body.encode(), "text/csv",
                              extra=(("Content-Disposition",
                                      "attachment; filename=jrf-waitlist.csv"),))
        return self._send(404, b"not found")

    def log_message(self, fmt, *args):  # journald gets one concise line
        print(f"{self._client_ip()} {fmt % args}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8127), Handler).serve_forever()
