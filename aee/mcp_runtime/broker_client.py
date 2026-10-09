"""Gateway control client: acquire/revoke job-specific broker capability."""
import http.client
import json
import socket
from pathlib import Path

from .store import JobError


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=5):
        super().__init__("localhost", timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def control(path, action, job_id):
    connection = UnixHTTP(path)
    try:
        connection.request("POST", "/" + action, json.dumps({"job_id": job_id}),
                           {"Content-Type": "application/json"})
        response = connection.getresponse()
        cap = 16384 if action == 'receipt' else 4096
        payload = response.read(cap + 1)
        if response.status != 200 or len(payload) > cap:
            raise ValueError("unavailable")
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError('invalid control response')
        if action == 'lease' and (not isinstance(value,dict) or value.get('socket') !=
                                 str(Path(path).parent/'jobs'/(job_id+'.sock'))):
            raise ValueError('unexpected socket capability')
        return value
    except (OSError, ValueError, http.client.HTTPException):
        raise JobError("BROKER_UNAVAILABLE", "Approved inference broker is unavailable") from None
    finally:
        connection.close()
