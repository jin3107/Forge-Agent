"""Session identifiers must not be normalized for ACL then used raw as paths."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from engine import config
from engine.session import SessionStore,ScopedSessionStore

class SessionIDBoundary(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name); (root/'sessions').mkdir(); (root/'workspaces').mkdir(); self.root=root
        p=patch.multiple(config,DATA_DIR=root,SESSIONS_DIR=root/'sessions',WORKSPACES_DIR=root/'workspaces',CONFIG_FILE=root/'config.json'); p.start(); self.addCleanup(p.stop)
        self.base=SessionStore(); self.store=ScopedSessionStore(self.base,'owner'); self.session=self.store.create(model='test')

    def test_malformed_aliases_are_not_authorized_and_do_not_create_directories(self):
        sid=self.session.id; self.assertIsNotNone(self.store.get(sid))
        for alias in ('../'+sid, sid+'.', sid+'/', '/'+sid, sid+'\x00', '', 'x'*300):
            with self.subTest(alias=alias):
                self.assertIsNone(self.store.get(alias))
                with self.assertRaises(ValueError): self.store.workspace(alias)
                self.assertFalse(self.store.delete(alias))
        self.assertFalse((self.root/sid).exists()); self.assertIsNotNone(self.store.get(sid))

    def test_path_operations_reject_invalid_ids_even_without_scoped_store(self):
        sid='../'+self.session.id
        for action in (lambda:self.base.workspace(sid),lambda:self.base.open_workspace(sid,str(self.root)),lambda:self.base.close_workspace(sid)):
            with self.assertRaises(ValueError): action()
        self.assertFalse(self.base.delete(sid)); self.assertIsNone(self.base.get(sid))

    def test_mismatched_session_id_in_file_is_not_returned(self):
        p=self.root/'sessions'/f'{self.session.id}.json'; data=json.loads(p.read_text()); data['id']='other'; p.write_text(json.dumps(data))
        self.assertIsNone(self.store.get(self.session.id)); self.assertIsNone(self.base.get(self.session.id))

    def test_valid_legacy_unicode_identifier_still_works(self):
        self.session.id='phiên_cũ-1'; self.base.save(self.session)
        self.assertEqual(self.store.get('phiên_cũ-1').id,'phiên_cũ-1')
        self.assertTrue(self.store.workspace('phiên_cũ-1').is_dir())
