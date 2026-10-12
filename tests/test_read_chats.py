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
import read_chats
from chatnotice import parse_bound


class ReadChatsTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'exports').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'exports')
        self.path=Path(self.temp.name)
        self.relative=str(self.path.relative_to(ROOT))
        self.request=read_chats.make_request('qq',['123'],'2030-01-02','2030-01-05',True)
        cached=read_chats.make_request('qq',['123','999'],'2030-01-01','2030-01-08',True)
        self.report={'request':cached,'messages':[
            {'platform':'qq','group_id':group,'timestamp':parse_bound(date),'source_id':str(mid),
             'body_status':'decoded_known_fields','text':'合成通知，不是真实聊天'}
            for mid,group,date in [(1,'123','2030-01-01'),(2,'123','2030-01-03'),(3,'999','2030-01-03'),(4,'123','2030-01-06')]]}
        self.write_report()
    def tearDown(self): self.temp.cleanup()
    def write_report(self):
        (self.path/'messages.json').write_text(json.dumps(self.report),encoding='utf-8')

    def test_confirmation_before_private_file_access(self):
        with patch.object(read_chats,'read_range') as read,patch('sys.stderr',new=io.StringIO()):
            result=read_chats.main(['--platform','qq','--group-id','123','--since','2030-01-01','--until','2030-01-02'])
        self.assertEqual(result,2); read.assert_not_called()

    def test_invalid_group_and_private_wechat_rejected(self):
        with self.assertRaises(ValueError): read_chats.make_request('qq',['课程群'],'2030-01-01','2030-01-02',True)
        with self.assertRaises(ValueError): read_chats.make_request('wechat',['wxid_private'],'2030-01-01','2030-01-02',True)
        with self.assertRaises(ValueError): read_chats.make_request('qq',['123','123'],'2030-01-01','2030-01-02',True)

    def test_cache_only_requested_group_and_time_without_database(self):
        with patch.object(read_chats,'export_request') as export,patch.object(read_chats,'_load_config') as config:
            result=read_chats.read_range(self.request,from_export=self.relative,show=True)
        self.assertEqual([m['source_id'] for m in result['messages']],['2'])
        self.assertEqual(result['message_count'],1)
        self.assertTrue(result['used_cached_export']); self.assertFalse(result['read_client_database'])
        self.assertIn('未再次检查',result['cache_notice'])
        config.assert_not_called(); export.assert_not_called()

    def test_cache_is_not_automatic_fresh_fallback(self):
        for ids,since,until in [(['777'],'2030-01-02','2030-01-05'),(['123'],'2029-12-31','2030-01-05'),
                                (['123'],'2030-01-02','2030-01-09')]:
            request=read_chats.make_request('qq',ids,since,until,True)
            with patch.object(read_chats,'export_request') as export:
                with self.assertRaises(ValueError): read_chats.read_range(request,from_export=self.relative)
                export.assert_not_called()

    def test_cache_cannot_escape_export_root(self):
        for path in ('../outside','state','exports/../state'):
            with self.assertRaises(ValueError): read_chats.read_range(self.request,from_export=path)

    def test_tampered_cache_out_of_original_scope_rejected(self):
        self.report['messages'][0]['group_id']='777'; self.write_report()
        with self.assertRaises(ValueError): read_chats.read_range(self.request,from_export=self.relative)

    def test_cached_summary_does_not_print_unrequested_messages(self):
        result=read_chats.read_range(self.request,from_export=self.relative)
        self.assertNotIn('messages',result)
        self.assertEqual(result['groups'][0]['group_id'],'123')

    def test_fresh_export_uses_explicit_scope(self):
        with patch.object(read_chats,'_load_config',return_value={'fixture':True}),\
             patch.object(read_chats,'save_json') as saved,\
             patch.object(read_chats,'export_request',return_value={'message_count':0,'output_dir':str(self.path)}) as export:
            result=read_chats.read_range(self.request)
        self.assertFalse(result['used_cached_export']); self.assertTrue(result['read_client_database'])
        self.assertEqual(export.call_args.args[1]['scopes'][0]['group_ids'],('123',))
        self.assertEqual(saved.call_args.args[0],'state/tool-read-request.json')

    def test_cli_show_cache_is_one_call(self):
        out=io.StringIO()
        with patch('sys.stdout',out):
            result=read_chats.main(['--platform','qq','--group-id','123','--since','2030-01-02','--until','2030-01-05',
                                   '--confirm','--from-export',self.relative,'--show'])
        self.assertEqual(result,0)
        self.assertEqual(json.loads(out.getvalue())['messages'][0]['source_id'],'2')


if __name__=='__main__': unittest.main()
