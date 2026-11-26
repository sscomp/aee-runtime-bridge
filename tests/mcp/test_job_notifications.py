"""Terminal Job delivery isolation, restart recovery and Telegram acknowledgement."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from aee.mcp_runtime.job_notifications import (
    JobNotifier, NotificationConfig, format_message, send_hermes, start_job_notifier,
)
from aee.mcp_runtime.store import JobStore, now
from completed_result_fixtures import successful_fields


class TerminalNotifications(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = JobStore(self.root, job_id_prefix='N2JOB')
        self.addCleanup(self.store.close)
        self.config = NotificationConfig('/bin/hermes', '/private/hermes', 'telegram:123',
                                         '2020-01-01T00:00:00Z')
        self.sender = Mock(return_value={'state': 'sent', 'message_id': '1234'})
        self.guard = patch('aee.mcp_runtime.job_notifications._notification_guard.notifications_disabled',
                           return_value=False)
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def terminal(self, status):
        job = self.store.create('codex', 'PRIVATE TASK', str(self.root), 'read_only')
        if status == 'completed':
            fields = successful_fields(summary='PRIVATE SUMMARY')
        else:
            fields = dict(status=status, started_at=now(), finished_at=now(), exit_code=0,
                          error_code='REQUIRED_TOOL_FAILED', error='PRIVATE ERROR')
        return self.store.update(job['job_id'], **fields)

    def notifier(self, **kwargs):
        return JobNotifier(self.root, self.config, sender=self.sender, **kwargs)

    def receipt(self, record):
        return json.loads((self.root/'notifications'/(record['job_id']+'.json')).read_text())

    def test_all_terminal_results_send_once_and_never_modify_job_results(self):
        records = [self.terminal(status) for status in ['completed', 'failed', 'timed_out', 'cancelled']]
        original = {p: p.read_bytes() for p in self.root.glob('*.json')}
        self.notifier().poll_once()
        self.notifier().poll_once()  # gateway restart / repeated polling
        self.assertEqual(self.sender.call_count, 4)
        for record in records:
            receipt = self.receipt(record)
            self.assertEqual(receipt['job_status'], record['status'])
            self.assertEqual(receipt['state'], 'sent')
            self.assertEqual(receipt['attempts'], 1)
        for p, body in original.items():
            self.assertEqual(p.read_bytes(), body)
        for call in self.sender.call_args_list:
            text = call.args[1]
            self.assertNotIn('PRIVATE', text)
            self.assertNotIn(str(self.root), text)

    def test_failed_status_wins_over_exit_zero(self):
        text = format_message(self.terminal('failed'))
        self.assertIn('失敗', text)
        self.assertIn('Exit Code：0', text)
        self.assertIn('REQUIRED_TOOL_FAILED', text)

    def test_running_and_history_before_enable_are_not_sent(self):
        self.terminal('failed')
        future = NotificationConfig('/bin/hermes', '/private/hermes', 'telegram',
                                    '2099-01-01T00:00:00Z')
        self.store.create('codex', 'queued', str(self.root), 'read_only')
        JobNotifier(self.root, future, sender=self.sender).poll_once()
        self.sender.assert_not_called()

    def test_explicit_failures_retry_with_backoff_and_stop_after_three(self):
        record = self.terminal('failed')
        self.sender.return_value = {'state': 'failed', 'error_code': 'HERMES_UNAVAILABLE'}
        clock = Mock(return_value=1000)
        notifier = self.notifier(clock=clock)
        notifier.poll_once()
        notifier.poll_once()
        self.assertEqual(self.sender.call_count, 1)
        clock.return_value = 1060
        notifier.poll_once()
        clock.return_value = 1360
        notifier.poll_once()
        clock.return_value = 2000
        notifier.poll_once()
        self.assertEqual(self.sender.call_count, 3)
        self.assertEqual(self.receipt(record)['attempts'], 3)

    def test_interrupted_send_is_uncertain_and_never_automatically_duplicated(self):
        record = self.terminal('cancelled')
        notifier = self.notifier()
        notifier.state_dir.mkdir(mode=0o700)
        notifier._write(notifier.state_dir/(record['job_id']+'.json'),
                        {'job_id': record['job_id'], 'state': 'sending', 'attempts': 1})
        notifier.poll_once()
        notifier.poll_once()
        self.sender.assert_not_called()
        self.assertEqual(self.receipt(record)['state'], 'unknown')

    def test_delivery_recovers_after_restart_and_explicit_failure(self):
        record = self.terminal('failed')
        self.sender.return_value = {'state': 'failed', 'error_code': 'HERMES_UNAVAILABLE'}
        self.notifier(clock=lambda: 1000).poll_once()
        self.sender.return_value = {'state': 'sent', 'message_id': '1234'}
        self.notifier(clock=lambda: 1060).poll_once()
        self.notifier(clock=lambda: 2000).poll_once()
        self.assertEqual(self.sender.call_count, 2)
        self.assertEqual(self.receipt(record)['state'], 'sent')

    def test_tampered_job_or_receipt_cannot_trigger_delivery(self):
        record = self.terminal('failed')
        path = self.root/(record['job_id']+'.json')
        original = path.read_bytes()
        altered = json.loads(original)
        altered['exit_code'] = 99
        path.write_text(json.dumps(altered))  # invalid integrity seal
        self.notifier().poll_once()
        self.sender.assert_not_called()
        path.write_bytes(original)
        receipt = self.root/'notifications'/(record['job_id']+'.json')
        receipt.write_text('{"state":"unrecognized"}')
        self.notifier().poll_once()
        self.sender.assert_not_called()

    def test_cross_worker_lock_prevents_duplicate_delivery(self):
        self.terminal('failed')
        other = self.notifier()
        self.sender.side_effect = lambda *_: (other.poll_once() or {'state': 'sent', 'message_id': '12'})
        self.notifier().poll_once()
        self.assertEqual(self.sender.call_count, 1)

    def test_shared_suppression_guard_blocks_outbox_and_transport(self):
        self.terminal('failed')
        with patch('aee.mcp_runtime.job_notifications._notification_guard.notifications_disabled',
                   return_value=True), patch('subprocess.Popen') as popen:
            self.notifier().poll_once()
            self.assertEqual(send_hermes(self.config, 'test')['state'], 'suppressed')
        self.sender.assert_not_called()
        popen.assert_not_called()
        self.assertFalse((self.root/'notifications').exists())

    def test_config_opt_in_and_private_permissions(self):
        path = self.root/'config.json'
        self.assertIsNone(start_job_notifier(self.root, path))
        path.write_text(json.dumps({'enabled': True, **self.config.__dict__}))
        path.chmod(0o644)
        with self.assertRaises(ValueError):
            NotificationConfig.load(path)
        path.chmod(0o600)
        self.assertEqual(NotificationConfig.load(path), self.config)

    def test_transport_requires_receipt_and_keeps_credentials_out_of_child_env(self):
        proc = Mock(returncode=0)
        proc.communicate.return_value = ('{"success":true,"message_id":123}', '')
        with patch.dict(os.environ, {'MCP_BRIDGE_API_KEY': 'not-for-hermes',
                                    'TELEGRAM_BOT_TOKEN': 'not-for-hermes'}), \
                patch('subprocess.Popen', return_value=proc) as popen:
            self.assertEqual(send_hermes(self.config, 'safe body'),
                             {'state': 'sent', 'message_id': '123'})
        env = popen.call_args.kwargs['env']
        self.assertNotIn('MCP_BRIDGE_API_KEY', env)
        self.assertNotIn('TELEGRAM_BOT_TOKEN', env)
        self.assertEqual(env['HERMES_HOME'], self.config.hermes_home)
        self.assertNotIn('safe body', popen.call_args.args[0])
        proc.communicate.return_value = ('{"success":true,"skipped":true}', '')
        with patch('subprocess.Popen', return_value=proc):
            self.assertEqual(send_hermes(self.config, 'safe')['state'], 'unknown')

    def test_timeout_kills_sender_and_does_not_auto_retry(self):
        record = self.terminal('timed_out')
        proc = Mock(pid=123, returncode=-9)
        proc.communicate.side_effect = [subprocess.TimeoutExpired('hermes', 30), ('', '')]
        with patch('subprocess.Popen', return_value=proc), patch('os.killpg') as kill:
            result = send_hermes(self.config, 'safe')
        kill.assert_called_once()
        self.assertEqual(result['state'], 'unknown')
        self.sender.return_value = result
        self.notifier().poll_once()
        self.notifier().poll_once()
        self.assertEqual(self.sender.call_count, 1)
        self.assertEqual(self.receipt(record)['state'], 'unknown')


if __name__ == '__main__':
    unittest.main()
