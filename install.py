"""Install persistent per-user macOS launch agents for Device Stats."""
import argparse
import hashlib
import json
import os
import plistlib
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

SOURCE = Path(__file__).resolve().parent
APP = Path.home() / "Library" / "Application Support" / "Device Stats"
AGENTS = Path.home() / "Library" / "LaunchAgents"
DOMAIN = f"gui/{os.getuid()}"
PORT = 8787
LAUNCHCTL = "/bin/launchctl"
SITE_LABEL = "local.device-stats.site"
DEVICE_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def install_agent(name, script, config, interval=None):
    label = f"local.device-stats.{name}"
    path = AGENTS / (label + ".plist")
    service = f"{DOMAIN}/{label}"
    settings = {
        "Label": label,
        "ProgramArguments": [sys.executable, str(APP / script.name), str(config)],
        "RunAtLoad": True,
        "StandardErrorPath": str(APP / (name + ".log")),
        "Umask": 0o077,
    }
    if interval:
        settings["StartInterval"] = interval
    else:
        settings["KeepAlive"] = True

    target = APP / script.name
    temporary = target.with_suffix(".tmp")
    shutil.copyfile(script, temporary)
    temporary.replace(target)

    running = subprocess.run([LAUNCHCTL, "print", service], capture_output=True).returncode == 0
    if running:
        subprocess.run([LAUNCHCTL, "bootout", service], check=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(plistlib.dumps(settings))
    temporary.replace(path)
    subprocess.run([LAUNCHCTL, "bootstrap", DOMAIN, str(path)], check=True)


def configure_reporter(device, url):
    path = APP / "reporter.json"
    if path.exists():
        config = json.loads(path.read_text())
        if config["device"] != device:
            sys.exit("This Mac already has a reporter configured for another device.")
        token = config["token"]
    else:
        token = secrets.token_urlsafe(32)
    write_json(path, {"device": device, "url": url, "token": token})
    return path, hashlib.sha256(token.encode()).hexdigest()


def install_server():
    source = SOURCE / "server"
    dashboard = APP / "dashboard.html"
    temporary = dashboard.with_suffix(".tmp")
    shutil.copyfile(source / "dashboard.html", temporary)
    temporary.replace(dashboard)
    config = APP / "site.json"
    if not config.exists():
        write_json(config, {"port": PORT, "devices": {}})
    install_agent("site", source / "server.py", config)


def register_device(device, name, fingerprint):
    path = APP / "site.json"
    if not path.exists():
        sys.exit("Install the server on this Mac first.")
    config = json.loads(path.read_text())
    existing = config["devices"].get(device)
    if existing and existing["sha256"] != fingerprint:
        sys.exit("This device ID is already registered to a different reporter.")
    config["devices"][device] = {"name": name, "sha256": fingerprint}
    write_json(path, config)
    subprocess.run([LAUNCHCTL, "kickstart", "-k", f"{DOMAIN}/{SITE_LABEL}"], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="role", required=True)
    commands.add_parser("server", help="Install the dashboard and collector")
    client = commands.add_parser("client", help="Install a Mac metrics reporter")
    client.add_argument("device")
    client.add_argument("url", help="Tailscale Serve HTTPS origin or local collector")
    register = commands.add_parser("register", help="Register a reporter on the server Mac")
    register.add_argument("device")
    register.add_argument("name")
    register.add_argument("sha256")
    args = parser.parse_args()

    if args.role != "server" and not DEVICE_ID.fullmatch(args.device):
        parser.error("Use a lowercase device ID with letters, numbers, or hyphens.")
    if args.role == "client":
        try:
            url = urlsplit(args.url)
            port = url.port
        except ValueError:
            parser.error("Invalid collector URL.")
        local = url.scheme == "http" and url.hostname == "127.0.0.1" and port == PORT
        remote = url.scheme == "https" and (url.hostname or "").endswith(".ts.net") and port != 0
        if (not (local or remote) or url.username is not None or url.path not in ("", "/")
                or url.query or url.fragment):
            parser.error("Use the Tailscale HTTPS origin or http://127.0.0.1:8787.")
    if args.role == "register" and (not args.name.strip() or not re.fullmatch(r"[0-9a-f]{64}", args.sha256)):
        parser.error("Provide a display name and the client's 64-character SHA-256 fingerprint.")

    os.umask(0o077)
    APP.mkdir(parents=True, exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)

    if args.role == "server":
        install_server()
    elif args.role == "client":
        config, fingerprint = configure_reporter(args.device, args.url.rstrip("/"))
        install_agent("reporter", SOURCE / "client" / "reporter.py", config, interval=30)
        print(f"Collector fingerprint for {args.device}: {fingerprint}")
    else:
        register_device(args.device, args.name, args.sha256)


if __name__ == "__main__":
    main()
