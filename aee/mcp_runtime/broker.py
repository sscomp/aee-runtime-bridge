"""Per-job UDS inference broker; only this process holds the upstream key.

The production entrypoint cannot select an arbitrary upstream. Injection of
a fixture transport is an in-process testing seam, never an HTTP parameter.
"""
from __future__ import annotations

import argparse
import http.client
import json
import logging
import os
import re
import socket
import socketserver
import ssl
import stat
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from .provider_contract import approved_tools

MODEL = "gpt-6.1-sol"
MAX_REQUEST = 2 * 1024**2
MAX_RESPONSE = 16 * 1024**2
MAX_REQUESTS = 32
LEASE_SECONDS = 900
MAX_JOB_BYTES = 64 * 1024**2
JOB_ID = re.compile(r"A3JOB-[0-9]{8}-[a-f0-9]{32}")
log = logging.getLogger('aee.broker')
FIELDS = {"model", "instructions", "input", "tools", "tool_choice", "parallel_tool_calls",
          "reasoning", "text", "stream", "store", "include", "prompt_cache_key",
          "max_output_tokens", "truncation", "client_metadata"}


class PolicyError(Exception):
    pass


def validate_request(method, path, headers, payload):
    if method != "POST" or path != "/v1/responses":
        raise PolicyError("REQUEST_DENIED")
    if headers.get("transfer-encoding") or headers.get("content-encoding", "identity") != "identity":
        raise PolicyError("REQUEST_DENIED")
    if len(payload) > MAX_REQUEST:
        raise PolicyError("REQUEST_LIMIT")
    try:
        body = json.loads(payload)
    except (ValueError, UnicodeError):
        raise PolicyError("INVALID_REQUEST") from None
    if not isinstance(body, dict) or set(body) - FIELDS or body.get("model") != MODEL:
        raise PolicyError("REQUEST_DENIED")
    if body.get("stream") is not True or body.get("store", False) is not False:
        raise PolicyError("REQUEST_DENIED")
    reasoning = body.get("reasoning", {})
    if not isinstance(reasoning, dict) or reasoning.get("effort") != "high":
        raise PolicyError("REQUEST_DENIED")
    if set(reasoning) - {"effort", "summary", "context"} or reasoning.get("context", "all_turns") != "all_turns":
        raise PolicyError("REQUEST_DENIED")
    tools = body.get("tools", [])
    if not isinstance(tools, list) or tools:
        raise PolicyError("REQUEST_DENIED")
    # Reviewed Responses Lite contract carries client tools in additional_tools.
    # No arbitrary top-level function, web search, MCP or hosted tool allowance.
    inputs = body.get("input")
    if not isinstance(inputs, list) or len(inputs) > 4096:
        raise PolicyError("REQUEST_DENIED")
    additional_count = 0
    for position, item in enumerate(inputs):
        if not isinstance(item, dict) or item.get("type", "message") not in {
                "message", "function_call", "function_call_output", "reasoning", "additional_tools",
                "custom_tool_call", "custom_tool_call_output"}:
            raise PolicyError("REQUEST_DENIED")
        if item.get("type") == "additional_tools":
            additional_count += 1
            if (additional_count != 1 or position != 0 or item.get("role") != "developer"
                    or set(item) - {"type", "id", "role", "tools"}):
                raise PolicyError("REQUEST_DENIED")
            validate_codex_tools(item.get("tools"))
        if item.get("type") in {"function_call", "custom_tool_call"}:
            allowed = {"wait", "request_user_input_async"} if item["type"] == "function_call" else {"exec"}
            if item.get("namespace") != "functions" or item.get("name") not in allowed:
                raise PolicyError("REQUEST_DENIED")
        if "content" in item:
            content = item["content"]
            if isinstance(content, list):
                if any(not isinstance(c, dict) or c.get("type") not in {
                        "input_text", "output_text", "refusal"} for c in content):
                    raise PolicyError("REQUEST_DENIED")
            elif not isinstance(content, str):
                raise PolicyError("REQUEST_DENIED")
    maximum = body.get("max_output_tokens", 16384)
    if type(maximum) is not int or not 1 <= maximum <= 16384:
        raise PolicyError("REQUEST_DENIED")
    body["max_output_tokens"] = maximum
    body["store"] = False
    body.pop("client_metadata", None)  # optional client metadata is not routing/auth authority
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()


def validate_codex_tools(tools):
    if not approved_tools(tools):
        raise PolicyError("REQUEST_DENIED")


def read_credential(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid not in {0, os.getuid()}:
            raise PolicyError("CREDENTIAL_UNAVAILABLE")
        value = f.read(4097).strip()
    if not 16 <= len(value) <= 4096 or any(c < 33 or c > 126 for c in value):
        raise PolicyError("CREDENTIAL_UNAVAILABLE")
    return value.decode("ascii")


class OpenAITransport:
    """Fixed TLS hostname, normal DNS/CDN resolution, no redirects or env proxy."""
    def __init__(self, credential):
        self.credential = credential

    def open(self, body):
        connection = http.client.HTTPSConnection("api.openai.com", 443, timeout=30,
                                                context=ssl.create_default_context())
        try:
            connection.request("POST", "/v1/responses", body=body,
                               headers={"Authorization": "Bearer " + self.credential,
                                        "Content-Type": "application/json", "Accept": "text/event-stream"})
            response = connection.getresponse()
            if response.status != 200 or response.getheader("Content-Type", "").split(";")[0] != "text/event-stream":
                raise PolicyError("UPSTREAM_FAILURE")
            return connection, response
        except Exception:
            connection.close()
            raise PolicyError("UPSTREAM_FAILURE") from None


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, path, handler, *, uid, state):
        self.uid, self.state = uid, state
        self.slots = threading.BoundedSemaphore(8)
        self.connections = set()
        self.guard = threading.Lock()
        super().__init__(str(path), handler)
        os.chmod(path, 0o600)

    def verify_request(self, request, address):
        _, uid, _ = struct.unpack("3i", request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        return uid == self.uid

    def process_request(self, request, address):
        if not self.slots.acquire(False):
            request.close()
            return
        with self.guard:
            self.connections.add(request)
        try:
            super().process_request(request, address)
        except Exception:
            self.shutdown_request(request)
            raise

    def shutdown_request(self, request):
        with self.guard:
            if request in self.connections:
                self.connections.remove(request)
                self.slots.release()
        super().shutdown_request(request)

    def handle_error(self, request, address):
        pass  # never log exception objects, headers, prompts or upstream credential

    def close_connections(self):
        with self.guard:
            for request in list(self.connections):
                try:
                    request.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "AEEBroker"
    sys_version = ""

    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, *args):
        pass

    def reply(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def send_error(self, code, message=None, explain=None):
        self.reply(code, {"error": "REQUEST_DENIED"})

    def payload(self, limit):
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not lengths[0].isdigit() or self.headers.get("Transfer-Encoding"):
            raise PolicyError("INVALID_REQUEST")
        size = int(lengths[0])
        if size > limit:
            raise PolicyError("REQUEST_LIMIT")
        value = self.rfile.read(size)
        if len(value) != size:
            raise PolicyError("INVALID_REQUEST")
        return value

    def do_GET(self):
        self.reply(403, {"error": "REQUEST_DENIED"})

    def do_CONNECT(self):
        self.reply(403, {"error": "REQUEST_DENIED"})


class Lease:
    def __init__(self, transport, seconds=LEASE_SECONDS):
        self.transport = transport
        self.deadline = time.monotonic() + seconds
        self.requests, self.bytes = 0, 0
        self.lock = threading.Lock()
        self.expected_calls = {}
        self.receipt_valid = True
        self.response_complete = False

    def receipt(self):
        with self.lock:
            return {'valid': self.receipt_valid, 'response_complete': self.response_complete,
                    'expected_calls': dict(self.expected_calls)}

    def reserve(self, size):
        with self.lock:
            if time.monotonic() >= self.deadline or self.requests >= MAX_REQUESTS:
                raise PolicyError("LEASE_EXPIRED")
            if self.bytes + size > MAX_JOB_BYTES:
                raise PolicyError("REQUEST_LIMIT")
            self.requests += 1
            self.bytes += size

    def account(self, size):
        with self.lock:
            self.bytes += size
            if self.bytes > MAX_JOB_BYTES or time.monotonic() >= self.deadline:
                raise PolicyError("LEASE_EXPIRED")


class InferenceHandler(Handler):
    def do_POST(self):
        connection = None
        streaming = False
        started = time.monotonic()
        try:
            payload = self.payload(MAX_REQUEST)
            body = validate_request("POST", self.path, {k.lower(): v for k, v in self.headers.items()}, payload)
            lease = self.server.state
            lease.reserve(len(body))
            from .provider_receipt import ResponseReceipt
            receipt = ResponseReceipt(lease)
            log.info('event=broker_request_admitted requests=%d',lease.requests)
            connection, response = lease.transport.open(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            streaming = True
            used = 0
            started = time.monotonic()
            while True:
                chunk = response.read1(16384)
                if not chunk:
                    break
                used += len(chunk)
                if used > MAX_RESPONSE or time.monotonic() - started > 120:
                    raise PolicyError("RESPONSE_LIMIT")
                lease.account(len(chunk))
                receipt.feed(chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
            receipt.finish()
        except Exception as error:
            if 'lease' in locals():
                with lease.lock:
                    lease.receipt_valid = False
            code = str(error) if isinstance(error, PolicyError) else "BROKER_FAILURE"
            log.warning('event=broker_request_failed error_code=%s duration_ms=%d',code,
                        int((time.monotonic()-started)*1000))
            if not streaming:
                self.reply(403 if code in {"REQUEST_DENIED", "LEASE_EXPIRED"} else 502, {"error": code})
        finally:
            if connection is not None:
                connection.close()


class Broker:
    def __init__(self, root, uid, transport):
        self.root, self.uid, self.transport = Path(root), uid, transport
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.jobs = {}
        self.lock = threading.Lock()

    def create(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise PolicyError("INVALID_JOB_ID")
        with self.lock:
            if self.jobs:
                raise PolicyError("BUSY")
            path = self.root / (job_id + ".sock")
            if path.exists() or path.is_symlink():
                raise PolicyError("SOCKET_CONFLICT")
            server = UnixServer(path, InferenceHandler, uid=self.uid, state=Lease(self.transport))
            os.chmod(path, 0o660)  # dedicated broker/gateway shared inference group
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            self.jobs[job_id] = (server, path)
            thread.start()
            return str(path)

    def revoke(self, job_id):
        with self.lock:
            pair = self.jobs.pop(job_id, None)
        if pair:
            server, path = pair
            server.state.deadline = 0
            server.close_connections()
            server.shutdown()
            server.server_close()
            path.unlink(missing_ok=True)

    def receipt(self, job_id):
        with self.lock:
            pair = self.jobs.get(job_id)
            if pair is None:
                raise PolicyError('INVALID_JOB_ID')
            return pair[0].state.receipt()

    def expire(self):
        with self.lock:
            ids = [job for job, (server, _) in self.jobs.items() if time.monotonic() >= server.state.deadline]
        for job in ids:
            self.revoke(job)

    def close(self):
        for job in list(self.jobs):
            self.revoke(job)


class ControlHandler(Handler):
    def do_POST(self):
        try:
            body = json.loads(self.payload(1024))
            if not isinstance(body, dict) or set(body) != {"job_id"}:
                raise PolicyError("INVALID_REQUEST")
            job = body["job_id"]
            if not isinstance(job, str) or not JOB_ID.fullmatch(job):
                raise PolicyError("INVALID_JOB_ID")
            if self.path == "/lease":
                self.reply(200, {"socket": self.server.state.create(job)})
            elif self.path == "/revoke":
                self.server.state.revoke(job)
                self.reply(200, {"ok": True})
            elif self.path == '/receipt':
                # Gateway-only control socket. Never mounted in a job; no new
                # route or caller override on the inference socket or MCP.
                self.reply(200, self.server.state.receipt(job))
            else:
                raise PolicyError("REQUEST_DENIED")
        except Exception as error:
            code = str(error) if isinstance(error, PolicyError) else "BROKER_FAILURE"
            self.reply(403, {"error": code})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--gateway-uid", type=int, required=True)
    parser.add_argument("--credential-file", type=Path, required=True)
    args = parser.parse_args()
    # No API call occurs until an admitted lease sends a validated request.
    broker = Broker(args.directory / "jobs", args.gateway_uid, OpenAITransport(read_credential(args.credential_file)))
    control = UnixServer(args.directory / "control.sock", ControlHandler, uid=args.gateway_uid, state=broker)
    # systemd grants the dedicated gateway group access; never world accessible.
    os.chmod(args.directory, 0o750)
    os.chmod(args.directory / "jobs", 0o750)
    os.chmod(args.directory / "control.sock", 0o660)
    from .readiness import notify
    notify("READY=1")
    thread = threading.Thread(target=control.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        while thread.is_alive():
            broker.expire()
            time.sleep(0.1)
    finally:
        control.shutdown()
        control.server_close()
        broker.close()


if __name__ == "__main__":
    main()
