"""Standalone experimental registry; its writer must be a trusted issuer."""
import concurrent.futures
from contextlib import closing
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from engine.worktree_binding import Binding

class BindingRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'bindings.sqlite3'
        self.binding=Binding('owner','parent','child',str(Path(self.tmp.name).resolve()),'wtbase-child','a'*40)

    def registry(self):
        from engine.worktree_binding_registry import BindingRegistry
        return BindingRegistry(self.path)

    def test_reopen_preserves_exact_binding_and_scoped_lookup(self):
        self.assertEqual(self.registry().record(self.binding),'created')
        r=self.registry(); self.assertEqual(r.lookup('owner','parent','child'),('found',self.binding))
        self.assertEqual(r.lookup('other','parent','child'),('missing',None))
        self.assertEqual(r.lookup('owner','other-parent','child'),('missing',None))

    def test_fresh_process_reads_persisted_binding(self):
        self.assertEqual(self.registry().record(self.binding),'created')
        code = ("from engine.worktree_binding_registry import BindingRegistry; "
                "import sys; s,b=BindingRegistry(sys.argv[1]).lookup('owner','parent','child'); "
                "assert s=='found' and b.repo_key=='wtbase-child'; print(s)")
        process=subprocess.run([sys.executable,'-c',code,str(self.path)],capture_output=True,text=True,timeout=10)
        self.assertEqual(process.returncode,0,process.stderr); self.assertEqual(process.stdout.strip(),'found')

    def test_idempotency_and_conflict_do_not_replace_first_record(self):
        r=self.registry(); self.assertEqual(r.record(self.binding),'created')
        self.assertEqual(r.record(self.binding),'existing')
        for other in (replace(self.binding,parent_sid='other'),replace(self.binding,owner_id='other'),
                      replace(self.binding,base_commit='b'*40)):
            self.assertEqual(r.record(other),'conflict')
        self.assertEqual(r.lookup('owner','parent','child'),('found',self.binding))

    def test_concurrent_conflicting_writers_have_one_winner(self):
        self.registry(); b=replace(self.binding,parent_sid='other')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda binding:self.registry().record(binding),(self.binding,b)))
        self.assertEqual(sorted(results),['conflict','created'])
        r=self.registry(); a=r.lookup('owner','parent','child'); c=r.lookup('owner','other','child')
        self.assertEqual(sum(x[0]=='found' for x in (a,c)),1)

    def test_invalid_input_and_corrupt_stored_record_fail_closed(self):
        r=self.registry()
        for b in (replace(self.binding,child_sid='../bad'),replace(self.binding,repo_key='other'),
                  replace(self.binding,base_commit='bad'),replace(self.binding,parent_workspace='relative'),
                  replace(self.binding,owner_id=''),replace(self.binding,parent_sid='child')):
            self.assertEqual(r.record(b),'invalid')
        self.assertEqual(r.record(self.binding),'created')
        with closing(sqlite3.connect(self.path)) as db:
            with db: db.execute("UPDATE bindings SET base_commit='bad'")
        self.assertEqual(r.lookup('owner','parent','child'),('error',None))

    def test_missing_replaced_or_unsupported_database_is_not_silently_recreated(self):
        from engine.worktree_binding_registry import BindingRegistry, RegistryError
        r=self.registry(); r.record(self.binding); self.path.unlink()
        self.assertEqual(r.lookup('owner','parent','child'),('error',None))
        self.assertEqual(r.record(self.binding),'error')
        self.path.write_bytes(b'')
        with self.assertRaises(RegistryError): BindingRegistry(self.path)
        self.path.unlink()
        with closing(sqlite3.connect(self.path)) as db:
            with db: db.execute('PRAGMA user_version=99')
        with self.assertRaises(RegistryError): BindingRegistry(self.path)

    def test_symlink_database_is_rejected(self):
        from engine.worktree_binding_registry import BindingRegistry, RegistryError
        target=self.path.parent/'target'; target.write_bytes(b'keep')
        try: self.path.symlink_to(target)
        except (OSError,NotImplementedError): self.skipTest('symlinks unavailable')
        with self.assertRaises(RegistryError): BindingRegistry(self.path)
        self.assertEqual(target.read_bytes(),b'keep')

    def test_store_cannot_authenticate_a_forged_issuer(self):
        r=self.registry(); forged=replace(self.binding,parent_sid='plausible-impostor')
        self.assertEqual(r.record(forged),'created')
        self.assertEqual(r.lookup('owner','plausible-impostor','child'),('found',forged))
