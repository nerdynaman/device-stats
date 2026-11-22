"""Loopback dashboard and SQLite collector. Tailscale Serve provides HTTPS."""
import hashlib
import hmac
import json
import math
import sqlite3
import sys
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

WEEK = 7 * 24 * 3600
FIELDS = {"device", "cpu", "ram_used", "ram_total", "battery", "power"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    device TEXT NOT NULL, recorded INTEGER NOT NULL, cpu REAL NOT NULL,
    ram_used INTEGER NOT NULL, ram_total INTEGER NOT NULL, battery INTEGER, power TEXT,
    PRIMARY KEY (device, recorded)
)
"""


def valid(payload):
    if not isinstance(payload, dict) or payload.keys() != FIELDS:
        return False
    cpu, used, total = payload["cpu"], payload["ram_used"], payload["ram_total"]
    battery, power = payload["battery"], payload["power"]
    return (
        isinstance(payload["device"], str)
        and type(cpu) in (int, float) and 0 <= cpu <= 100 and math.isfinite(cpu)
        and type(used) is int and type(total) is int and 0 <= used <= total <= 2**50
        and total > 0
        and (
            (battery is None and power is None)
            or (
                type(battery) is int and 0 <= battery <= 100
                and power in ("Charging", "Plugged in", "On battery")
            )
        )
    )


class Collector(ThreadingHTTPServer):
    def __init__(self, address, database, devices, dashboard):
        self.database = database
        self.devices = devices
        self.dashboard = dashboard
        self.last_cleanup = 0
        with closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.execute(SCHEMA)
        super().__init__(address, Handler)

    def connect(self):
        db = sqlite3.connect(self.database)
        db.row_factory = sqlite3.Row
        return db

    def record(self, sample):
        now = int(time.time())
        with closing(self.connect()) as db, db:
            db.execute(
                """INSERT OR IGNORE INTO samples VALUES
                   (:device, :recorded, :cpu, :ram_used, :ram_total, :battery, :power)""",
                {**sample, "recorded": now},
            )
            if now - self.last_cleanup >= 3600:
                db.execute("DELETE FROM samples WHERE recorded < ?", (now - WEEK,))
                self.last_cleanup = now

    def readings(self, hours):
        now = int(time.time())
        width = {1: 30, 24: 600, 168: 3600}[hours]
        since = now - hours * 3600
        with closing(self.connect()) as db:
            devices = []
            for device, settings in self.devices.items():
                latest = db.execute(
                    "SELECT * FROM samples WHERE device = ? AND recorded >= ? ORDER BY recorded DESC LIMIT 1",
                    (device, now - WEEK),
                ).fetchone()
                history = db.execute(
                    """SELECT recorded / ? * ? AS time, ROUND(AVG(cpu), 1) AS cpu,
                              ROUND(AVG(ram_used * 100.0 / ram_total), 1) AS ram,
                              ROUND(AVG(battery), 1) AS battery
                       FROM samples WHERE device = ? AND recorded >= ? GROUP BY time ORDER BY time""",
                    (width, width, device, since),
                )
                devices.append({
                    "id": device, "name": settings["name"],
                    "latest": dict(latest) if latest else None,
                    "history": [dict(point) for point in history],
                })
        return {"devices": devices, "now": now, "since": since, "bucket": width}


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.request.settimeout(8)

    def log_request(self, code="-", size="-"):
        pass

    def reply(self, status, body=b"", content_type="text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        if url.path == "/":
            self.reply(200, self.server.dashboard, "text/html; charset=utf-8")
        elif url.path == "/api/devices":
            hours = parse_qs(url.query).get("hours", ["1"])[0]
            if hours not in ("1", "24", "168"):
                self.reply(400, b"Invalid history range")
                return
            data = self.server.readings(int(hours))
            self.reply(200, json.dumps(data).encode(), "application/json")
        else:
            self.reply(404)

    def do_POST(self):
        if self.path != "/api/samples":
            self.reply(404)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 2048:
                self.reply(413)
                return
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
        except (ValueError, RecursionError):
            self.reply(400)
            return
        if not valid(payload):
            self.reply(400, b"Invalid sample")
            return
        settings = self.server.devices.get(payload["device"])
        auth = self.headers.get("Authorization", "")
        digest = hashlib.sha256(auth.removeprefix("Bearer ").encode()).hexdigest()
        if not settings or not auth.startswith("Bearer ") or not hmac.compare_digest(digest, settings["sha256"]):
            self.reply(401)
            return
        self.server.record(payload)
        self.reply(204)


if __name__ == "__main__":
    path = Path(sys.argv[1]).resolve()
    config = json.loads(path.read_text())
    dashboard = Path(__file__).with_name("dashboard.html").read_bytes()
    with Collector(("127.0.0.1", config["port"]), path.with_name("samples.sqlite3"), config["devices"], dashboard) as server:
        server.serve_forever()
