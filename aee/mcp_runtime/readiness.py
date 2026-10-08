"""systemd notification and finite condition-based health checks."""
import argparse
import http.client
import json
import os
import socket
import time


def notify(message):
    address = os.getenv("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as channel:
        channel.connect(address)
        channel.sendall(message.encode())


def wait_health(port, timeout=30, expected_source=None):
    if port not in {8790, 8791} or not 0 < timeout <= 60:
        raise ValueError("Invalid readiness policy")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            data = json.loads(response.read(4097))
            matches = expected_source is None or data.get("runtime", {}).get("source_commit") == expected_source
            if response.status == 200 and data.get("status") == "healthy" and matches:
                return
        except (OSError, ValueError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
    raise RuntimeError("Gateway readiness deadline exceeded")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, choices=[8790, 8791], required=True)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--manifest", help="Require exact candidate source identity; reject baseline health")
    args = parser.parse_args()
    if args.manifest:
        from .runtime import deployment_policy
        expected=deployment_policy(args.manifest)["source_commit"]
    else:
        expected=None
    wait_health(args.port, args.timeout, expected_source=expected)


if __name__ == "__main__":
    main()
