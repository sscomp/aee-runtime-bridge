"""Portable prefix lifecycle regression; temporary fixtures, never real jobs."""
import asyncio
import importlib.util
import json
import os
import sys
import threading
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aee.mcp_runtime.budget import ExecutionBudget
from aee.mcp_runtime.job_identity import JOB_ID
from aee.mcp_runtime.migration import inspect_records, migrate, rollback_copy
from aee.mcp_runtime.spool import SpoolStore
from aee.mcp_runtime.store import JobError, JobStore, read_job_record
from completed_result_fixtures import successful_fields


class JobIdentity(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='aee-prefix-id-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        if sys.platform == 'darwin':
            # Canonical main's unrelated owner probe requires Linux /proc.
            # Exercise prefix/persistence semantics here with explicit identity;
            # Linux runs this suite against the real probe without a patch.
            owner = patch('aee.mcp_runtime.store.owner_identity', return_value={
                'pid': os.getpid(), 'start_ticks': 'fixture', 'boot_id': 'fixture'})
            owner.start()
            self.addCleanup(owner.stop)

    def store(self, prefix='A3JOB', root=None):
        store = JobStore(root or self.root/'jobs', job_id_prefix=prefix)
        self.addCleanup(store.close)
        return store

    def create(self, store):
        return store.create('codex', 'fixture inspection', str(self.root), 'read_only')

    def test_generation_setting_keeps_legacy_completed_result_unchanged(self):
        old_store = self.store()
        old = self.create(old_store)
        self.assertTrue(old['job_id'].startswith('A3JOB-'))
        old_store.update(old['job_id'], **successful_fields('legacy result'))
        path = self.root/'jobs'/(old['job_id']+'.json')
        original = path.read_bytes()
        new_store = self.store('N2JOB')
        new = self.create(new_store)
        self.assertRegex(new['job_id'], r'^N2JOB-[0-9]{8}-[a-f0-9]{32}$')
        self.assertEqual(new_store.get(old['job_id'])['summary'], 'legacy result')
        self.assertEqual(read_job_record(path.parent, old['job_id'])['status'], 'completed')
        self.assertEqual(set(new_store.list_ids()), {old['job_id'], new['job_id']})
        new_store.update(new['job_id'], status='cancelled', error_code='EXECUTION_CANCELLED')
        self.assertEqual(new_store.get(new['job_id'])['status'], 'cancelled')
        self.assertFalse(new_store.has_active())
        self.assertEqual(path.read_bytes(), original)

    def test_live_n2_lease_blocks_other_instances_then_restart_recovers(self):
        store = self.store('N2JOB')
        job = self.create(store)
        store.update(job['job_id'], status='running')
        other = self.store()
        self.assertTrue(other.has_active())
        self.assertEqual(other.reconcile(), 0)
        with self.assertRaises(JobError) as error:
            self.create(other)
        self.assertEqual(error.exception.code, 'BUSY')
        store.close()
        reopened = self.store('N2JOB')
        result = reopened.get(job['job_id'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'EXECUTION_INTERRUPTED')
        self.assertFalse(reopened.has_active())

    def test_mixed_spool_identities_reconcile_and_keep_evidence(self):
        spool = SpoolStore(self.root/'spool'); self.addCleanup(spool.close)
        ids = [prefix+'-20261113-'+'a'*32 for prefix in ('A3JOB', 'N2JOB', 'M2JOB', 'FUTURE42JOB')]
        for job_id in ids:
            job = spool.create(job_id, ExecutionBudget(), 'fixture-budget')
            job.append('stdout', b'bounded evidence')
            job.close()
        recovered = {}
        spool.reconcile(86400, lambda job_id, data: recovered.update({job_id: data}))
        self.assertEqual(set(recovered), set(ids))
        for data in recovered.values():
            self.assertEqual(data['state'], 'interrupted')
            self.assertEqual(data['streams']['stdout']['spool_bytes'], len(b'bounded evidence'))

    def test_mixed_prefix_migration_inventory_preserves_original_ids(self):
        for prefix in ('A3JOB', 'N2JOB', 'M2JOB', 'FUTURE42JOB'):
            store = self.store(prefix, self.root/'source')
            job = self.create(store)
            store.update(job['job_id'], status='cancelled', error_code='EXECUTION_CANCELLED')
            store.close()
        inventory = inspect_records(self.root/'source')
        self.assertEqual(len(inventory), 4)
        self.assertEqual({name.split('-')[0] for name in inventory}, {'A3JOB', 'N2JOB', 'M2JOB', 'FUTURE42JOB'})
        for name, original in inventory.items():
            self.assertEqual((self.root/'source'/name).read_bytes(), original)

    def test_invalid_prefix_fails_before_filesystem_changes(self):
        for prefix in ('N2JOB-', 'n2job', '../N2JOB', 'N2/JOB', 'N2\\JOB', 'N2JOB\n',
                       ' N2JOB', 'N2JOB ', 'N2-JOB', 'N2_JOB', '2JOB', 'JOB',
                       'Ａ2JOB', 'A'*17+'JOB', '', None, 123, [], {}):
            path = self.root/'uncreated'
            with self.assertRaises(ValueError):
                JobStore(path, job_id_prefix=prefix)
            self.assertFalse(path.exists())

    def test_invalid_ids_remain_rejected_and_corrupt_n2_records_block_admission(self):
        store = self.store('N2JOB')
        for job_id in ('../N2JOB-20261113-'+'a'*32, 'N2JOB-20261113-'+'a'*32+'\n',
                       'N2JOB-20261113-'+'A'*32, 'N2JOB-20261113-'+'a'*32+'/../escape'):
            self.assertIsNone(JOB_ID.fullmatch(job_id))
            with self.assertRaises(JobError) as error:
                store.get(job_id)
            self.assertEqual(error.exception.code, 'JOB_NOT_FOUND')
        bad = store._root/'N2JOB-malformed.json'; bad.write_text('{}')
        with self.assertRaises(JobError) as error:
            self.create(store)
        self.assertEqual(error.exception.code, 'JOB_STORE_CORRUPT')
        self.assertEqual(bad.read_text(), '{}')

    def test_broker_accepts_and_revokes_mixed_namespaces(self):
        from aee.mcp_runtime.broker import Broker
        broker = Broker(self.root/'broker', os.getuid(), object())
        self.addCleanup(broker.close)
        for prefix in ('A3JOB', 'N2JOB', 'M2JOB', 'FUTURE42JOB'):
            job_id = prefix+'-20261113-'+'b'*32
            path = Path(broker.create(job_id))
            self.assertTrue(path.exists())
            broker.revoke(job_id)
            self.assertFalse(path.exists())

    def test_grammar_bounds_and_hostile_inputs_at_read_boundaries(self):
        from aee.mcp_runtime.broker import Broker, PolicyError
        from aee.mcp_runtime.job_identity import validate_job_id_prefix
        for prefix in ('AJOB', 'A' + '9'*15 + 'JOB', 'M2JOB', 'FUTURE42JOB'):
            self.assertEqual(validate_job_id_prefix(prefix), prefix)
            self.assertIsNotNone(JOB_ID.fullmatch(prefix+'-20261117-'+'a'*32))
        broker = Broker(self.root/'broker', os.getuid(), object())
        self.addCleanup(broker.close)
        spool = SpoolStore(self.root/'spool')
        self.addCleanup(spool.close)
        store = self.store()
        valid = 'M2JOB-20261117-'+'a'*32
        for job_id in ('../'+valid, valid+'/../../escape', valid+'\n',
                       valid.replace('M2JOB', 'm2job'), valid.replace('M2JOB', 'Ｍ2JOB'),
                       valid.replace('M2JOB', 'A'*17+'JOB'), valid+'x'):
            with self.subTest(job_id=job_id):
                with self.assertRaises(PolicyError):
                    broker.create(job_id)
                with self.assertRaises(JobError):
                    spool.create(job_id, ExecutionBudget(), 'fixture-budget')
                with self.assertRaises(JobError) as error:
                    read_job_record(store._root, job_id)
                self.assertEqual(error.exception.code, 'JOB_NOT_FOUND')
        self.assertEqual(broker.jobs, {})
        self.assertFalse((self.root/'escape').exists())

    @unittest.skipIf(sys.platform == 'darwin', 'Canonical migration safety gate requires real Linux /tmp')
    def test_rollback_rejects_hostile_manifest_paths(self):
        backup = self.root/'backup'
        backup.mkdir()
        valid = 'M2JOB-20261117-'+'a'*32+'.json'
        for name in ('../'+valid, '/tmp/'+valid, valid+'\n', valid+'.bak', 'm2job-20261117-'+'a'*32+'.json'):
            (backup/'backup-manifest.json').write_text(json.dumps({'records': {name: 'a'*64}}))
            with self.assertRaises(JobError) as error:
                rollback_copy(backup, self.root/'restored', apply=True)
            self.assertEqual(error.exception.code, 'MIGRATION_INVALID')
            self.assertFalse((self.root/'restored').exists())

    def gateway(self, prefix=None):
        root = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location('prefix_fixture_gateway', root/'mcp_gateway.py')
        gateway = importlib.util.module_from_spec(spec)
        env = {'A3_JOB_STORE_DIR': str(self.root/'gateway'),
               'AEE_MCP_PORT': '8790', 'AEE_MCP_SURFACE': 'local'}
        if prefix is not None:
            env['AEE_JOB_ID_PREFIX'] = prefix
        with patch.dict(os.environ, env, clear=True):
            spec.loader.exec_module(gateway)
        self.addCleanup(gateway.JOB_STORE.close)
        return gateway

    def test_gateway_defaults_and_operator_generation(self):
        gateway = self.gateway()
        job = self.create(gateway.JOB_STORE)
        self.assertTrue(job['job_id'].startswith('A3JOB-'))
        gateway.JOB_STORE.update(job['job_id'], status='cancelled')
        gateway.JOB_STORE.close()
        gateway = self.gateway('M2JOB')
        job = self.create(gateway.JOB_STORE)
        self.assertTrue(job['job_id'].startswith('M2JOB-'))

    def test_gateway_invalid_setting_creates_no_store(self):
        with self.assertRaises(ValueError):
            self.gateway('../M2JOB')
        self.assertFalse((self.root/'gateway').exists())

    def test_prefix_rollback_round_trip_and_cross_prefix_entropy_collision(self):
        snapshots = {}
        # Equal date and random suffix across nodes must still name distinct files.
        with patch('aee.mcp_runtime.store.secrets.token_hex', return_value='c'*32):
            for prefix in ('A3JOB', 'N2JOB', 'M2JOB', 'FUTURE42JOB'):
                store = self.store(prefix)
                job = self.create(store)
                store.update(job['job_id'], **successful_fields(prefix))
                path = store._root/(job['job_id']+'.json')
                snapshots[job['job_id']] = path.read_bytes()
                store.close()
        rollback = self.store()  # revert generation config to the legacy default
        self.assertEqual(set(rollback.list_ids()), set(snapshots))
        for job_id, original in snapshots.items():
            self.assertEqual(rollback.get(job_id)['summary'], job_id.split('-')[0])
            self.assertEqual(read_job_record(rollback._root, job_id)['job_id'], job_id)
            self.assertEqual((rollback._root/(job_id+'.json')).read_bytes(), original)
        new = self.create(rollback)
        self.assertTrue(new['job_id'].startswith('A3JOB-'))

    @unittest.skipIf(sys.platform == 'darwin', 'Canonical migration safety gate requires real Linux /tmp')
    def test_migration_and_rollback_manifest_round_trip(self):
        source = self.root/'source'
        for prefix in ('A3JOB', 'N2JOB', 'M2JOB'):
            store = self.store(prefix, source)
            job = self.create(store)
            store.update(job['job_id'], **successful_fields(prefix))
            store.close()
        original = inspect_records(source)
        migrated, backup, restored = (self.root/p for p in ('migrated', 'backup', 'restored'))
        preview = migrate(source, migrated, backup)
        self.assertEqual(set(preview['records']), set(original))
        self.assertFalse(backup.exists())
        result = migrate(source, migrated, backup, apply=True)
        self.assertTrue(result['source_untouched'])
        self.assertEqual(inspect_records(source), original)
        self.assertEqual(set(inspect_records(migrated)), set(original))
        rollback_copy(backup, restored, apply=True)
        self.assertEqual(inspect_records(restored), original)
        store = self.store(root=restored)
        self.assertEqual(set(store.list_ids()), {Path(name).stem for name in original})

    def test_gateway_status_result_and_cancel_across_generation_changes(self):
        gateway = self.gateway('M2JOB')
        for prefix in ('A3JOB', 'N2JOB', 'M2JOB'):
            with self.subTest(prefix=prefix):
                store = self.store(prefix)
                job = self.create(store)
                job_id = job['job_id']
                event = threading.Event()
                with patch.object(gateway, 'JOB_STORE', store), \
                     patch.dict(gateway._cancel_events, {job_id: event}):
                    def call(tool):
                        return json.loads(asyncio.run(tool(job_id)))
                    self.assertEqual(call(gateway.aee_job_status)['status'], 'queued')
                    self.assertEqual(call(gateway.aee_job_result)['error_code'], 'JOB_NOT_COMPLETE')
                    self.assertTrue(call(gateway.aee_job_cancel)['ok'])
                    self.assertTrue(event.is_set())
                    # Synthetic worker acknowledgement; no process is dispatched.
                    store.update(job_id, status='cancelled', error_code='EXECUTION_CANCELLED')
                    self.assertEqual(call(gateway.aee_job_status)['status'], 'cancelled')
                    self.assertEqual(call(gateway.aee_job_result)['error_code'], 'EXECUTION_CANCELLED')
                    self.assertEqual(call(gateway.aee_job_cancel)['error_code'], 'JOB_NOT_ACTIVE')
                    completed = self.create(store)
                    job_id = completed['job_id']
                    store.update(job_id, **successful_fields(prefix))
                    path = store._root/(job_id+'.json')
                    original = path.read_bytes()
                    self.assertTrue(call(gateway.aee_job_result)['ok'])
                    self.assertEqual(call(gateway.aee_job_result)['summary'], prefix)
                    self.assertEqual(call(gateway.aee_job_cancel)['error_code'], 'JOB_NOT_ACTIVE')
                    self.assertEqual(path.read_bytes(), original)
                store.close()


if __name__ == '__main__':
    unittest.main()
