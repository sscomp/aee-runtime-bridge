"""Poll a Hermes run via the bridge until it ends. Pure Python — no shell metachars."""
import json
import sys
import time
from urllib import error, request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787"
RUN_ID = sys.argv[2] if len(sys.argv) > 2 else open("/tmp/bridge_last_run_id").read().strip()
KEY = open("/tmp/bridge_env.sh").read()
BRIDGE_API_KEY = [l.split("=", 1)[1].strip() for l in KEY.splitlines() if l.startswith("BRIDGE_API_KEY=")][0]

url = f"{BASE}/runs/{RUN_ID}"
req = request.Request(url, headers={"Authorization": f"Bearer {BRIDGE_API_KEY}"})

print(f"Polling {url}")
for i in range(1, 25):  # up to 120s
    try:
        with request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read()[:200].decode(errors='replace')}")
        sys.exit(1)
    status = data.get("status", "?")
    last = data.get("last_event", "?")
    print(f"  [t+{i*5:>3}s] status={status:<12}  last_event={last}")
    if status in ("completed", "failed", "cancelled"):
        print()
        print("=== final payload ===")
        print(json.dumps(data, indent=2, ensure_ascii=False)[:2000])
        sys.exit(0 if status == "completed" else 2)
    time.sleep(5)

print("TIMEOUT after 120s")
sys.exit(3)
