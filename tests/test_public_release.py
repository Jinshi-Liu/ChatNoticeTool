from __future__ import annotations
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import chatnotice
import cleanup
import local_setup
from attachments import allowed_local_source


class PublicReleaseTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'runtime').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime')
        self.base=Path(self.temp.name)
    def tearDown(self): self.temp.cleanup()

    def test_example_has_no_account_and_memory_read_disabled(self):
        config=json.loads((ROOT/'config.example.json').read_text(encoding='utf-8'))
        self.assertEqual(config['qq']['database_dir'],'')
        self.assertEqual(config['wechat']['account_dir'],'')
        self.assertFalse(config['policy']['allow_process_memory_read'])
        self.assertEqual(config['attachments']['extra_allowed_roots'],[])
        self.assertEqual(json.loads((ROOT/'initial-todos.json').read_text())['tasks'],[])

    def test_setup_idempotent_and_preserves_private_config(self):
        root=self.base/'clone'; root.mkdir()
        template=ROOT/'config.example.json'
        (root/template.name).write_bytes(template.read_bytes())
        self.assertEqual(set(local_setup.initialize(root)),{'.chatnotice-owned.json','config.json'})
        config=root/'config.json'; config.write_text('{"user":"fixture"}')
        (root/'state').mkdir(); progress=root/'state'/'fixture'; progress.write_text('keep')
        self.assertEqual(local_setup.initialize(root),[])
        self.assertEqual(config.read_text(),'{"user":"fixture"}')
        self.assertEqual(progress.read_text(),'keep')
        marker=json.loads((root/'.chatnotice-owned.json').read_text())
        self.assertEqual(marker['root'],str(root)); self.assertEqual(marker['external_changes'],[])
        self.assertTrue(cleanup.plan_cleanup(root,'runtime'))

    def test_current_release_documents_owned_and_unknown_files_preserved(self):
        docs={'CHANGELOG.md','ARCHITECTURE.md'}
        self.assertTrue(docs<=cleanup.OWNED)
        for name in docs: self.assertTrue((ROOT/name).is_file())
        for name in ('README.md','CHANGELOG.md'):
            self.assertIn('v0.2.0',(ROOT/name).read_text(encoding='utf-8'))
        root=self.base/'upgrade'; root.mkdir()
        (root/'config.example.json').write_bytes((ROOT/'config.example.json').read_bytes())
        local_setup.initialize(root)
        for name in docs: (root/name).write_text('synthetic release document')
        personal=root/'user-notes.txt'; personal.write_text('keep')
        targets={p.name for p in cleanup.plan_cleanup(root,'all')}
        self.assertTrue(docs<=targets); self.assertNotIn(personal.name,targets)
        marker=json.loads((root/'.chatnotice-owned.json').read_text())
        self.assertTrue(docs<=set(marker['owned_top_level']))
        self.assertEqual(personal.read_text(),'keep')

    def test_setup_rejects_other_owner(self):
        root=self.base/'clone'; root.mkdir()
        (root/'.chatnotice-owned.json').write_text('{}')
        with self.assertRaises(ValueError): local_setup.initialize(root)

    def test_export_requires_confirmation_before_load_or_read(self):
        args=['chatnotice.py','export','--platform','qq','--group-id','123',
              '--since','2030-01-01','--until','2030-01-02']
        with patch.object(sys,'argv',args),patch.object(chatnotice,'load_config') as load,patch('sys.stderr',new=io.StringIO()):
            self.assertEqual(chatnotice.main(),2); load.assert_not_called()

    def test_build_exact_scope_and_date_end(self):
        req=chatnotice.build_request('wechat',['123@chatroom'],'2030-01-01','2030-01-02')
        scope=req['scopes'][0]
        self.assertTrue(req['confirmed'])
        self.assertEqual(scope['group_ids'],('123@chatroom',))
        self.assertEqual(scope['until'],chatnotice.parse_bound('2030-01-03'))
        with self.assertRaises(ValueError): chatnotice.build_request('wechat',['wxid_private'],'2030-01-01','2030-01-02')

    def test_export_cli_calls_scoped_reader(self):
        args=['chatnotice.py','export','--platform','qq','--group-id','123',
              '--since','2030-01-01','--until','2030-01-02','--confirm']
        with patch.object(sys,'argv',args),patch.object(chatnotice,'load_config',return_value={}),\
             patch.object(chatnotice,'save_json'),patch('messages.export_request',return_value={'message_count':0}) as read,\
             patch('sys.stdout',new=io.StringIO()):
            self.assertEqual(chatnotice.main(),0)
        request=read.call_args.args[1]
        self.assertEqual(request['groups'][0]['group_id'],'123')

    def test_attachment_roots_are_configured_not_personal(self):
        allowed=self.base/'allowed'; allowed.mkdir()
        outside=self.base/'outside'; outside.mkdir()
        config={'qq':{'media_dir':''},'wechat':{'account_dir':''},
                'attachments':{'extra_allowed_roots':[str(allowed)]}}
        self.assertTrue(allowed_local_source(allowed/'test.pdf',config))
        self.assertFalse(allowed_local_source(outside/'test.pdf',config))
        config['attachments']['extra_allowed_roots']=[]
        self.assertFalse(allowed_local_source(allowed/'test.pdf',config))

    def test_widget_and_cleanup_share_installation_mutex(self):
        import todo_widget
        self.assertEqual(todo_widget.MUTEX,chatnotice.WIDGET_MUTEX)
        self.assertEqual(cleanup.WIDGET_MUTEX,chatnotice.WIDGET_MUTEX)
        self.assertNotEqual(todo_widget.MUTEX,'Local\\ChatNoticeToolTodoWidget-c39dba48')


if __name__=='__main__': unittest.main()
