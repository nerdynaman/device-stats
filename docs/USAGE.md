# Device Stats

Python 3.9+. Clone this repo on the Air and each reporting Mac, or copy the checkout from the Air. Run commands from the repo root while signed in. All devices should already be on your tailnet.

### MacBook Air · host the site

```sh
python3 install.py server
tailscale serve --bg --https=8443 http://127.0.0.1:8787
```

Use the HTTPS URL printed by Serve. If `tailscale` is unavailable, use `/Applications/Tailscale.app/Contents/MacOS/Tailscale`.

### Each reporting Mac · install the client

On the Air:

```sh
python3 install.py client macbook-air http://127.0.0.1:8787
```

On the Pro or another Mac, replace the URL with the Serve URL and use a unique ID:

```sh
python3 install.py client macbook-pro https://AIR.TAILNET.ts.net:8443
```

### MacBook Air · register each client

Use the fingerprint printed by each client installer:

```sh
python3 install.py register macbook-air "MacBook Air" AIR_FINGERPRINT
python3 install.py register macbook-pro "MacBook Pro" PRO_FINGERPRINT
```

Open the Serve URL on any permitted tailnet device.

### Apply changes and restart

On the Air for website changes, or on each reporting Mac for reporter changes:

```sh
git pull --ff-only && python3 install.py restart
```

Skip the pull if the changes are already in your checkout. The command restarts the services installed on that Mac, starting the site before the reporter when both are installed. Refresh the website to see HTML changes.

### Recover a fingerprint

If you missed the installer output, run this on the reporting Mac:

```sh
python3 - <<'PY'
import hashlib, json
from pathlib import Path

config = Path.home() / "Library/Application Support/Device Stats/reporter.json"
print(hashlib.sha256(json.loads(config.read_text())["token"].encode()).hexdigest())
PY
```
