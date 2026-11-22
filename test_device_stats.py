import hashlib
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import call, patch

import install
from client import reporter
from server.server import Collector

SAMPLE = {"cpu": 27.2, "ram_used": 7, "ram_total": 16, "battery": 63, "power": "Charging"}


class DeviceStatsTest(unittest.TestCase):
    def test_restart_updates_only_installed_services(self):
        reporter_label = "local.device-stats.reporter"
        for labels in ((reporter_label,), (install.SITE_LABEL, reporter_label)):
            with self.subTest(labels=labels), tempfile.TemporaryDirectory() as folder:
                app = Path(folder) / "application"
                agents = Path(folder) / "agents"
                app.mkdir()
                agents.mkdir()
                for label in labels:
                    (agents / (label + ".plist")).write_text("existing launch agent")
                (app / "reporter.json").write_text("existing reporter configuration")
                (app / "samples.sqlite3").write_bytes(b"existing samples")
                if install.SITE_LABEL in labels:
                    (app / "site.json").write_text('{"port": 8787, "devices": {}}')
                with patch.object(install, "APP", app), patch.object(install, "AGENTS", agents), \
                        patch.object(install.subprocess, "run") as launchctl, \
                        patch.object(install, "wait_for_site") as wait_for_site:
                    install.restart_agents()

                expected = [
                    call([install.LAUNCHCTL, "kickstart", "-k", f"{install.DOMAIN}/{label}"], check=True)
                    for label in labels
                ]
                self.assertEqual(launchctl.call_args_list[-len(labels):], expected)
                self.assertEqual((app / "reporter.py").read_bytes(),
                                 (install.SOURCE / "client/reporter.py").read_bytes())
                self.assertEqual((app / "reporter.json").read_text(), "existing reporter configuration")
                self.assertEqual((app / "samples.sqlite3").read_bytes(), b"existing samples")
                if install.SITE_LABEL in labels:
                    wait_for_site.assert_called_once_with(8787)
                    self.assertEqual((app / "dashboard.html").read_bytes(),
                                     (install.SOURCE / "server/dashboard.html").read_bytes())
                else:
                    wait_for_site.assert_not_called()
                    self.assertFalse((app / "dashboard.html").exists())

    def test_register_client_preserves_other_devices(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(install, "APP", Path(folder)):
            fingerprint = "a" * 64
            with patch.object(install, "install_agent"), patch.object(install.subprocess, "run"):
                install.install_server()
                install.register_device("macbook-air", "MacBook Air", fingerprint)
                install.register_device("spare-mac", "Spare Mac", "b" * 64)
                with self.assertRaises(SystemExit):
                    install.register_device("macbook-air", "Another Mac", "c" * 64)
                install.install_server()

            config = json.loads((Path(folder) / "site.json").read_text())
            self.assertEqual(config["devices"]["macbook-air"]["sha256"], fingerprint)
            self.assertEqual(config["devices"]["spare-mac"]["name"], "Spare Mac")

    def test_reporters_and_dashboard(self):
        devices = {
            name: {"name": name.title(), "sha256": hashlib.sha256(token.encode()).hexdigest()}
            for name, token in (("air", "air-token"), ("pro", "pro-token"))
        }
        with tempfile.TemporaryDirectory() as folder, Collector(
            ("127.0.0.1", 0), Path(folder) / "stats.sqlite3", devices, b"<title>Device Stats</title>"
        ) as collector:
            worker = threading.Thread(target=collector.serve_forever, daemon=True)
            worker.start()
            url = f"http://127.0.0.1:{collector.server_port}"
            try:
                now = time.time()
                with patch.object(reporter, "sample", return_value=SAMPLE), patch("server.server.time.time", return_value=now - 180):
                    reporter.report({"url": url, "device": "air", "token": "air-token"})
                with patch.object(reporter, "sample", return_value=SAMPLE):
                    reporter.report({"url": url, "device": "pro", "token": "pro-token"})
                    with self.assertRaises(urllib.error.HTTPError) as invalid:
                        reporter.report({"url": url, "device": "air", "token": "pro-token"})
                    self.assertEqual(invalid.exception.code, 401)
                    invalid.exception.close()
                with patch.object(reporter, "sample", return_value=dict(SAMPLE, cpu=101)):
                    with self.assertRaises(urllib.error.HTTPError) as invalid:
                        reporter.report({"url": url, "device": "pro", "token": "pro-token"})
                    self.assertEqual(invalid.exception.code, 400)
                    invalid.exception.close()

                with urllib.request.urlopen(url) as response:
                    self.assertIn(b"Device Stats", response.read())
                for hours in (1, 24, 168):
                    with urllib.request.urlopen(f"{url}/api/devices?hours={hours}") as response:
                        data = json.load(response)
                    air, pro = data["devices"]
                    self.assertGreater(data["now"] - air["latest"]["recorded"], 90)
                    self.assertLess(data["now"] - pro["latest"]["recorded"], 90)
                    self.assertEqual(pro["history"][0]["cpu"], 27.2)
                    self.assertEqual(pro["history"][0]["ram"], 43.8)
                    self.assertEqual(air["history"][0]["battery"], 63)
            finally:
                collector.shutdown()
                worker.join()


if __name__ == "__main__":
    unittest.main()
