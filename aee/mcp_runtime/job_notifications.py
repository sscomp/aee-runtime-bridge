"""Durable, non-blocking MCP Job terminal notifications through Hermes.

Only sealed terminal records and allowlisted scalar fields leave the host.
Delivery receipts are separate from the immutable Job/result contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import stat
import subprocess
import tempfile
import threading
import time

from aee import _notification_guard
from .job_identity import JOB_ID
from .store import JobError, TERMINAL, read_job_record

log = logging.getLogger(__name__)
CONFIG_PATH = Path.home() / '.config/aee/job-notifications.json'
LABELS = {'completed': '成功', 'failed': '失敗', 'timed_out': '逾時', 'cancelled': '取消'}


def timestamp(value):
    at = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.tzinfo is None:
        raise ValueError('timezone required')
    return at


@dataclass(frozen=True)
class NotificationConfig:
    hermes_bin: str
    hermes_home: str
    target: str
    enabled_at: str

    @classmethod
    def load(cls, path):
        path = Path(path)
        if not path.exists():
            return None
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 8192):
            raise ValueError('private owned notification config required')
        cfg = json.loads(path.read_text())
        if cfg.get('enabled') is not True:
            return None
        result = cls(**{key: cfg[key] for key in cls.__dataclass_fields__})
        if (not all(isinstance(v, str) for v in result.__dict__.values())
                or not Path(result.hermes_bin).is_absolute()
                or not Path(result.hermes_home).is_absolute()
                or not re.fullmatch(r'telegram(?::[-]?[0-9]+(?::[0-9]+)?)?', result.target)):
            raise ValueError('invalid Hermes notification settings')
        timestamp(result.enabled_at)
        return result


def format_message(record):
    """Never include task, summary, paths, raw errors, logs or execution metadata."""
    status = record['status']
    if status not in TERMINAL or not JOB_ID.fullmatch(record['job_id']):
        raise ValueError('terminal Job identity required')
    agent = record.get('agent')
    if agent not in {'codex', 'claude', 'hermes', 'pi'}:
        agent = 'unknown'
    host = re.sub(r'[^A-Za-z0-9._-]', '_', socket.gethostname())[:80]
    lines = [f'AEE 派工結束：{LABELS[status]}', f'Job：{record["job_id"]}',
             f'節點：{host}', f'Agent：{agent}', f'狀態：{status}']
    code = record.get('error_code')
    if isinstance(code, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,79}', code):
        lines.append('Error Code：' + code)
    exit_code = record.get('exit_code')
    if type(exit_code) is int:
        lines.append(f'Exit Code：{exit_code}')
    finished = timestamp(record['finished_at'])
    lines.append('結束時間：' + finished.astimezone().isoformat(timespec='seconds'))
    if record.get('started_at'):
        seconds = max(0, int((finished - timestamp(record['started_at'])).total_seconds()))
        lines.append(f'執行時間：約 {seconds // 60} 分 {seconds % 60} 秒')
    lines.append('詳細結果請以此 Job ID 查詢 AEE。')
    return '\n'.join(lines)


def send_hermes(config, body):
    """Use Hermes' configured gateway delivery adapter, without copying bot tokens."""
    if _notification_guard.notifications_disabled():
        return {'state': 'suppressed', 'error_code': 'NOTIFICATIONS_SUPPRESSED'}
    # Hermes resolves its own platform credentials. Do not inherit unrelated
    # AEE/model credentials or Telegram overrides from the executor environment.
    env = {k: os.environ[k] for k in ('HOME', 'PATH', 'LANG', 'LC_ALL', 'TMPDIR') if k in os.environ}
    env['HERMES_HOME'] = config.hermes_home
    try:
        process = subprocess.Popen(
            [config.hermes_bin, 'send', '--to', config.target, '--file', '-', '--json'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, start_new_session=True,
        )
    except OSError:
        return {'state': 'failed', 'error_code': 'HERMES_START_FAILED'}

    # Once the CLI starts, a local failure can mean the receipt was lost
    # after Telegram accepted the notification.
    try:
        stdout, _ = process.communicate(body, timeout=30)
    except (subprocess.TimeoutExpired, OSError, UnicodeError) as error:
        timed_out = isinstance(error, subprocess.TimeoutExpired)
        if timed_out or process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        code = 'DELIVERY_TIMEOUT' if timed_out else 'DELIVERY_IO_FAILED'
        return {'state': 'unknown', 'error_code': code}
    if process.returncode:
        return {'state': 'unknown', 'error_code': 'HERMES_CLI_FAILED'}
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return {'state': 'unknown', 'error_code': 'INVALID_DELIVERY_RECEIPT'}
    message_id = payload.get('message_id') if isinstance(payload, dict) else None
    if (isinstance(payload, dict) and payload.get('success') is True
            and not payload.get('error') and not payload.get('skipped')
            and isinstance(message_id, (str, int)) and not isinstance(message_id, bool)
            and re.fullmatch(r'[0-9]{1,30}', str(message_id))):
        return {'state': 'sent', 'message_id': str(message_id)}
    return {'state': 'unknown', 'error_code': 'UNCONFIRMED_DELIVERY'}


class JobNotifier:
    def __init__(self, root, config, *, sender=send_hermes, clock=time.time):
        self.root = Path(root)
        self.config = config
        self.sender = sender
        self.clock = clock
        self.state_dir = self.root / 'notifications'
        self.stop_event = threading.Event()
        self.thread = None

    def _write(self, path, data):
        fd, name = tempfile.mkstemp(prefix='.receipt-', dir=self.state_dir)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(data, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
            directory = os.open(self.state_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _deliver(self, record):
        job_id = record['job_id']
        path = self.state_dir / (job_id + '.json')
        previous = {}
        if path.exists():
            # Invalid receipts fail closed: don't risk a duplicate send.
            if path.is_symlink() or path.stat().st_size > 8192:
                raise ValueError('invalid receipt')
            previous = json.loads(path.read_text())
            if (not isinstance(previous, dict) or previous.get('job_id') != job_id
                    or previous.get('state') not in {'sent', 'unknown', 'suppressed', 'sending', 'failed'}):
                raise ValueError('invalid receipt identity or state')
        state = previous.get('state')
        if state in {'sent', 'unknown', 'suppressed'}:
            return
        if state == 'sending':
            self._write(path, {**previous, 'state': 'unknown', 'error_code': 'DELIVERY_INTERRUPTED'})
            return
        if state == 'failed' and previous.get('error_code') != 'HERMES_START_FAILED':
            # Older receipts did not distinguish launch and post-launch errors.
            previous.pop('next_attempt_at', None)
            self._write(path, {**previous, 'state': 'unknown', 'updated_at': self.clock()})
            return
        attempts = previous.get('attempts', 0)
        if type(attempts) is not int or not 0 <= attempts < 3:
            return
        if self.clock() < previous.get('next_attempt_at', 0):
            return
        body = format_message(record)
        receipt = {'job_id': job_id, 'job_status': record['status'], 'state': 'sending',
                   'attempts': attempts + 1, 'updated_at': self.clock()}
        self._write(path, receipt)  # durable intent before the external send
        try:
            result = self.sender(self.config, body)
        except Exception:
            result = {'state': 'unknown', 'error_code': 'DELIVERY_EXCEPTION'}
        # Transport returns only fixed error codes and a numeric Telegram ID.
        receipt.update(result)
        receipt['updated_at'] = self.clock()
        if receipt['state'] == 'failed':
            receipt['next_attempt_at'] = self.clock() + (60 if attempts == 0 else 300)
        self._write(path, receipt)
        log.info('event=job_notification job_id=%s delivery=%s attempts=%d',
                 job_id, receipt['state'], receipt['attempts'])

    def poll_once(self):
        if _notification_guard.notifications_disabled():
            return
        if self.state_dir.is_symlink():
            raise ValueError('notification directory cannot be a symlink')
        self.state_dir.mkdir(mode=0o700, exist_ok=True)
        info = self.state_dir.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('private owned notification directory required')
        fd = os.open(self.state_dir / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            for path in sorted(self.root.glob('*.json')):
                if self.stop_event.is_set():
                    break
                if not JOB_ID.fullmatch(path.stem):
                    continue
                try:
                    record = read_job_record(self.root, path.stem)
                    if (record['status'] in TERMINAL and record.get('finished_at')
                            and timestamp(record['finished_at']) >= timestamp(self.config.enabled_at)):
                        self._deliver(record)
                except (JobError, OSError, ValueError, TypeError, KeyError):
                    log.warning('event=job_notification_record_rejected job_id=%s', path.stem)
        finally:
            os.close(fd)

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except Exception:
                log.warning('event=job_notification_poll_failed')
            self.stop_event.wait(2)

    def start(self):
        self.thread = threading.Thread(target=self._run, name='aee-job-notifications', daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=32)


def start_job_notifier(root, config_path=None):
    try:
        config = NotificationConfig.load(config_path or os.getenv('AEE_JOB_NOTIFICATION_CONFIG') or CONFIG_PATH)
        if config is None:
            return None
        notifier = JobNotifier(root, config).start()
        log.info('event=job_notifications_enabled transport=hermes_send')
        return notifier
    except Exception:
        log.warning('event=job_notifications_configuration_failed')
        return None
