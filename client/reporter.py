"""Collect one macOS sample and send it to Device Stats."""
import ctypes
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SYSTEM = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
SYSTEM.mach_host_self.restype = ctypes.c_uint
SYSTEM.host_statistics.argtypes = (
    ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_uint),
    ctypes.POINTER(ctypes.c_uint),
)
HOST_CPU_LOAD_INFO = 3


def cpu_ticks():
    ticks = (ctypes.c_uint * 4)()
    count = ctypes.c_uint(len(ticks))
    result = SYSTEM.host_statistics(
        SYSTEM.mach_host_self(), HOST_CPU_LOAD_INFO, ticks, ctypes.byref(count)
    )
    if result or count.value != len(ticks):
        raise OSError(f"Cannot read CPU counters (Mach status {result})")
    return ticks


def command(*args):
    return subprocess.check_output(args, text=True, timeout=5, env={**os.environ, "LC_ALL": "C"})


def sample():
    before = cpu_ticks()
    time.sleep(1)
    after = cpu_ticks()
    delta = [(end - start) % (1 << 32) for start, end in zip(before, after)]
    total = sum(delta)
    if total == 0:
        raise OSError("CPU counters did not advance")

    # shortcut: approximate App + Wired + Compressed RAM, use native memory APIs for exact accounting
    vm = command("/usr/bin/vm_stat")
    page_size = int(re.search(r"page size of (\d+) bytes", vm)[1])
    pages = {name: int(count) for name, count in re.findall(r"^([^:\n]+):\s+(\d+)\.$", vm, re.M)}
    used = pages["Pages active"] + pages["Pages wired down"] + pages["Pages occupied by compressor"]
    used -= pages["Pages purgeable"]
    capacity = int(command("/usr/sbin/sysctl", "-n", "hw.memsize"))

    power = command("/usr/bin/pmset", "-g", "batt")
    battery = re.search(r"InternalBattery[^\n]*?\b(\d+)%;\s*([^;\n]+)", power)
    state = None
    if battery:
        if battery[2].strip() == "charging":
            state = "Charging"
        elif "AC Power" in power:
            state = "Plugged in"
        else:
            state = "On battery"
    return {
        "cpu": round(100 * (total - delta[2]) / total, 1),
        "ram_used": min(capacity, max(0, used * page_size)),
        "ram_total": capacity,
        "battery": int(battery[1]) if battery else None,
        "power": state,
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def report(config):
    request = urllib.request.Request(
        config["url"] + "/api/samples",
        data=json.dumps({"device": config["device"], **sample()}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + config["token"]},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=8) as response:
        if response.status != 204:
            raise OSError(f"Unexpected collector response: HTTP {response.status}")


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--sample"]:
            print(json.dumps(sample()))
        else:
            report(json.loads(Path(sys.argv[1]).read_text()))
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        sys.exit(f"{time.strftime('%Y-%m-%d %H:%M:%S')} reporter: {error}")
