"""Real candidate lifecycle and fail-closed P2C metadata acceptance tests."""
import hashlib
import json
import os
import socket
import stat
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

import test_p2c_deployment as deployment
from aee.mcp_runtime.packaging import build_bundle, verify_bundle
from aee.mcp_runtime.store import JobError


class PackagingMetadata(unittest.TestCase):
    setUpClass = classmethod(deployment.P2CDeployment.setUpClass.__func__)
    tearDownClass = classmethod(deployment.P2CDeployment.tearDownClass.__func__)
    setUp = deployment.P2CDeployment.setUp
    build = deployment.P2CDeployment.build

    def reject_mode(self, name, mode):
        root = self.build()
        (root/name).chmod(mode)
        with self.assertRaises(JobError):
            verify_bundle(root, workspace=self.repo)

    def test_data_0444_pass(self):
        root = self.build()
        self.assertEqual(stat.S_IMODE((root/'source/safe.txt').stat().st_mode), 0o444)
        verify_bundle(root, workspace=self.repo)

    def test_native_0555_pass(self):
        root = self.build()
        self.assertEqual(stat.S_IMODE((root/'runner/codex').stat().st_mode), 0o555)
        verify_bundle(root, workspace=self.repo)

    def test_directory_0555_pass(self):
        root = self.build()
        for node in [root] + [p for p in root.rglob('*') if p.is_dir()]:
            self.assertEqual(stat.S_IMODE(node.stat().st_mode), 0o555)
        verify_bundle(root, workspace=self.repo)

    def test_data_0644_rejected(self): self.reject_mode('source/safe.txt', 0o644)
    def test_native_0755_rejected(self): self.reject_mode('runner/codex', 0o755)
    def test_directory_0755_rejected(self): self.reject_mode('source', 0o755)
    def test_root_0755_rejected(self): self.reject_mode('.', 0o755)
    def test_group_write_rejected(self): self.reject_mode('source/safe.txt', 0o464)
    def test_other_write_rejected(self): self.reject_mode('source/safe.txt', 0o446)
    def test_setid_rejected(self): self.reject_mode('runner/codex', 0o4555)
    def test_data_executable_rejected(self): self.reject_mode('source/safe.txt', 0o555)
    def test_native_nonexecutable_rejected(self): self.reject_mode('runner/codex', 0o444)

    def insert(self, kind):
        root = self.build()
        root.chmod(0o755)
        path = root/'extra'
        if kind == 'symlink': path.symlink_to('source/safe.txt')
        elif kind == 'fifo': os.mkfifo(path, 0o444)
        elif kind == 'directory': path.mkdir(mode=0o555)
        elif kind == 'socket':
            server = socket.socket(socket.AF_UNIX)
            self.addCleanup(server.close)
            server.bind(str(path))
        else:
            path.write_text('unexpected');path.chmod(0o444)
        root.chmod(0o555)
        with self.assertRaises(JobError): verify_bundle(root, workspace=self.repo)

    def test_symlink_rejected(self): self.insert('symlink')
    def test_fifo_rejected_without_blocking(self): self.insert('fifo')
    def test_socket_rejected(self): self.insert('socket')
    def test_extra_file_rejected(self): self.insert('file')
    def test_extra_directory_rejected(self): self.insert('directory')

    def test_missing_file_rejected(self):
        root = self.build();parent=root/'source';parent.chmod(0o755)
        (parent/'safe.txt').unlink();parent.chmod(0o555)
        with self.assertRaises(JobError): verify_bundle(root)

    def test_manifest_fifo_rejected_without_blocking(self):
        root = self.build();root.chmod(0o755)
        (root/'deployment-manifest.json').unlink()
        os.mkfifo(root/'deployment-manifest.json',0o444);root.chmod(0o555)
        with self.assertRaises(JobError): verify_bundle(root)

    def test_root_symlink_rejected(self):
        root=self.build();alias=self.path/'alias';alias.symlink_to(root)
        with self.assertRaises(JobError): verify_bundle(alias)

    def test_owner_group_mismatch_rejected_real_node(self):
        groups = [gid for gid in os.getgroups() if gid != os.getegid()]
        if not groups: self.skipTest('No permitted alternate group for real chown')
        root=self.build();target=root/'source/safe.txt'
        os.chown(target, -1, groups[0])
        self.assertNotEqual(target.stat().st_gid, os.getegid())
        with self.assertRaises(JobError): verify_bundle(root)

    def test_checkout_mode_perturbation_uses_git_intent(self):
        executable=self.repo/'committed-executable';executable.write_text('#!/bin/sh\nexit 0\n')
        executable.chmod(0o755)
        subprocess.run(['git','-C',str(self.repo),'add','committed-executable'],check=True)
        subprocess.run(['git','-C',str(self.repo),'-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                        'commit','-qm','executable intent fixture'],check=True)
        data=self.repo/'safe.txt';original=data.stat().st_mode
        try:
            executable.chmod(0o644);data.chmod(0o755)
            root=self.build()
            self.assertEqual(stat.S_IMODE((root/'source/committed-executable').stat().st_mode),0o555)
            self.assertEqual(stat.S_IMODE((root/'source/safe.txt').stat().st_mode),0o444)
            verify_bundle(root,workspace=self.repo)
        finally:
            executable.chmod(0o755);data.chmod(stat.S_IMODE(original))

    def test_umask_variations(self):
        for mask in [0o000, 0o022, 0o077]:
            with self.subTest(umask=oct(mask)):
                previous=os.umask(mask)
                try: root=self.build('mask-'+str(mask))
                finally: os.umask(previous)
                verify_bundle(root,workspace=self.repo)

    def test_generated_manifests_metadata_verified(self):
        for name in ['workspace-manifest.json','source-metadata.json','deployment-manifest.json']:
            with self.subTest(name=name):
                root=self.build(name+'-release')
                self.assertEqual(stat.S_IMODE((root/name).stat().st_mode),0o444)
                (root/name).chmod(0o644)
                with self.assertRaises(JobError): verify_bundle(root)

    def test_content_mutation_readonly_rejected(self):
        root=self.build();target=root/'source/safe.txt'
        target.chmod(0o644);target.write_text('changed content');target.chmod(0o444)
        with self.assertRaises(JobError): verify_bundle(root)

    def test_detached_manifest_pin_rejected(self):
        root=self.build()
        with self.assertRaises(JobError): verify_bundle(root,manifest_sha256='0'*64)

    def test_coordinated_source_metadata_tamper_rejected_by_git(self):
        root=self.build();metadata=root/'source-metadata.json';manifest=root/'deployment-manifest.json'
        modes=json.loads(metadata.read_text());modes['safe.txt']='100755'
        metadata.chmod(0o644);metadata.write_text(json.dumps(modes));metadata.chmod(0o444)
        (root/'source/safe.txt').chmod(0o555)
        body=json.loads(manifest.read_text());value=hashlib.sha256(metadata.read_bytes()).hexdigest()
        body['source_metadata_sha256']=value;body['artifact_files']['source-metadata.json']=value
        manifest.chmod(0o644);manifest.write_text(json.dumps(body));manifest.chmod(0o444)
        verify_bundle(root)  # Internally consistent; trusted Git remains authoritative.
        with self.assertRaises(JobError): verify_bundle(root,workspace=self.repo)

    def test_unsafe_manifest_path_rejected(self):
        root=self.build();manifest=root/'deployment-manifest.json';body=json.loads(manifest.read_text())
        body['artifact_files']['../outside']='0'*64
        manifest.chmod(0o644);manifest.write_text(json.dumps(body));manifest.chmod(0o444)
        with self.assertRaises(JobError): verify_bundle(root)

    def test_builder_metadata_failure_prevents_publication(self):
        root=self.path/'rejected'
        from aee.mcp_runtime.bundle_metadata import normalize
        def corrupt(stage,modes):
            normalize(stage,modes)
            (stage/'source/safe.txt').chmod(0o644)
        with patch('aee.mcp_runtime.packaging.normalize',side_effect=corrupt):
            with self.assertRaises(JobError): build_bundle(self.repo,root,self.native/'codex')
        self.assertFalse(root.exists())


if __name__=='__main__': unittest.main()
