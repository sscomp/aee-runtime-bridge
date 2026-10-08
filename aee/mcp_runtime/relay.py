"""Secret-free namespace-local TCP -> fixed broker UDS relay and CLI supervisor.

It has no upstream hostname, DNS, CONNECT implementation or host network.
Only the job's socket is mounted; the broker control socket is never exposed.
"""
import select
import os
import json
import socket
import socketserver
import subprocess
import sys
import threading
import time


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        target = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        target.settimeout(5)
        try:
            target.connect("/runner/inference.sock")
            peers = [self.request, target]
            for peer in peers:
                peer.setblocking(False)
            deadline = time.monotonic() + 125
            while time.monotonic() < deadline:
                readable, _, _ = select.select(peers, [], [], 0.2)
                for peer in readable:
                    value = peer.recv(16384)
                    if not value:
                        return
                    other = target if peer is self.request else self.request
                    # Bounded blocking send with deadline; no accumulating queue.
                    other.settimeout(5)
                    other.sendall(value)
                    other.setblocking(False)
        except OSError:
            pass
        finally:
            target.close()


class Server(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self):
        self.slots = threading.BoundedSemaphore(4)
        super().__init__(("127.0.0.1", 18080), Relay)

    def process_request(self, request, address):
        if not self.slots.acquire(False):
            request.close()
            return
        super().process_request(request, address)

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        pass


def main():
    server = Server()  # listen before child starts; no readiness sleep
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    collector = None
    if os.environ.get('AEE_NATIVE_TOOL_RECEIPT') == '1':
        # -I omits the script directory. Load only this operator-mounted module
        # from the read-only runner, never workspace modules or IDE contents.
        import importlib.util
        spec = importlib.util.spec_from_file_location('aee_native_telemetry', '/runner/native_telemetry.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        collector = module.Collector()
        threading.Thread(target=collector.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True).start()
    child = subprocess.Popen(sys.argv[1:], stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
                             close_fds=True)
    try:
        code = child.wait()
        return code if code >= 0 else 128 - code  # preserve SIGXFSZ/SIGXCPU contract
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        server.shutdown()
        server.server_close()
        if collector is not None:
            collector.shutdown()
            collector.server_close()
            print(json.dumps(collector.evidence.receipt(), separators=(',', ':')), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
