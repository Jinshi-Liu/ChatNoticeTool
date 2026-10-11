from __future__ import annotations
from datetime import datetime
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from todo_store import TodoStore, normalize_due
from todo_widget import TodoWidget, deadline_badge


class TodoTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'runtime').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'runtime')
        self.store=TodoStore(Path(self.temp.name)/'todos.sqlite3')

    def tearDown(self): self.temp.cleanup()

    def test_date_validation(self):
        self.assertEqual(normalize_due('2026-10-11 17:00'),'2026-10-11T17:00')
        self.assertIsNone(normalize_due(''))
        for bad in ('tomorrow','2026-02-30','2026-10-10T25:00','2026-10-10T01:00+08:00'):
            with self.assertRaises(ValueError): normalize_due(bad)

    def test_upsert_keeps_completed_state(self):
        id=self.store.upsert('原事项',id='stable',due='2026-10-11')
        self.store.mark(id,True)
        self.store.upsert('更正标题',id=id,due='2026-10-12')
        record=self.store.get(id)
        self.assertEqual(record['title'],'更正标题')
        self.assertEqual(record['done'],1)
        self.assertIsNotNone(record['done_at'])
        self.assertEqual(len(self.store.tasks()),1)

    def test_two_handles_preserve_check_when_assistant_adds(self):
        other=TodoStore(self.store.path)
        self.store.upsert('a',id='a'); other.mark('a',True)
        self.store.upsert('updated',id='a'); self.store.upsert('b',id='b')
        self.assertTrue(other.get('a')['done'])
        self.assertFalse(other.get('b')['done'])

    def test_reopen_persists_and_undo(self):
        id=self.store.upsert('保存进度'); self.store.mark(id,True)
        other=TodoStore(self.store.path)
        self.assertEqual(len(other.tasks(done=True)),1)
        other.mark(id,False)
        self.assertIsNone(self.store.get(id)['done_at'])
        self.assertEqual(len(self.store.tasks(done=False)),1)

    def test_removed_not_resurrected_by_assistant(self):
        self.store.upsert('按需活动',id='optional',optional=True)
        self.store.remove('optional'); self.store.upsert('更新活动',id='optional')
        self.assertEqual(self.store.tasks(),[])
        self.assertEqual(len(self.store.tasks(include_removed=True)),1)
        self.store.remove('optional',False)
        self.assertEqual(len(self.store.tasks()),1)

    def test_settings_round_trip(self):
        self.store.set_setting('pinned',False)
        self.assertFalse(TodoStore(self.store.path).setting('pinned',True))

    def test_due_order_and_unknown_deadline(self):
        self.store.upsert('无明确截止',id='unknown')
        self.store.upsert('晚',id='late',due='2026-10-13')
        self.store.upsert('早',id='early',due='2026-10-11T17:00')
        self.assertEqual([t['id'] for t in self.store.tasks()],['early','late','unknown'])

    def test_unknown_database_not_modified(self):
        path=Path(self.temp.name)/'other.sqlite3'
        db=sqlite3.connect(path); db.execute('CREATE TABLE unrelated(x)'); db.commit(); db.close()
        before=path.read_bytes()
        with self.assertRaises(ValueError): TodoStore(path)
        self.assertEqual(path.read_bytes(),before)

    def test_seed_once_keeps_progress(self):
        import json
        fixture=Path(self.temp.name)
        (fixture/'initial-todos.json').write_text(json.dumps({'tasks':[{'id':'seed-fixture','title':'示例事项'}]}),encoding='utf-8')
        with patch('todo_store.ROOT',fixture):
            self.store.seed_once()
            id=self.store.tasks()[0]['id']; self.store.mark(id,True)
            total=len(self.store.tasks())
            self.store.seed_once()
        self.assertEqual(total,len(self.store.tasks()))
        self.assertTrue(self.store.get(id)['done'])

    def test_deadline_badges(self):
        now=datetime(2026,10,10,21,0)
        label,color=deadline_badge({'done':0,'due':'2026-10-11T17:00'},now)
        self.assertIn('明天',label)
        label,color=deadline_badge({'done':0,'due':'2026-10-10T17:00'},now)
        self.assertIn('已过截止',label)
        self.assertEqual(deadline_badge({'done':0,'due':None,'due_label':'截止待确认'},now)[0],'截止待确认')

    @unittest.skipUnless(sys.platform=='win32','Windows Tk regression test')
    def test_idle_refresh_does_not_rebuild_or_relayout(self):
        import tkinter as tk
        self.store.upsert('保留的事项',id='stable')
        root=tk.Tk(); root.withdraw(); widget=TodoWidget(root,self.store)
        try:
            root.update()
            ids=widget.canvas.find_all(); layouts=widget.canvas.layout_count
            for i in range(8):
                root.after_cancel(widget.poll_id)
                widget.signature=None  # Simulate clock/DB metadata invalidation.
                widget.poll(); root.update()
            self.assertEqual(widget.canvas.find_all(),ids)
            self.assertEqual(widget.canvas.layout_count,layouts)
            self.store.upsert('仅说明更新',id='stable')
            widget.render(); root.update()
            self.assertEqual(widget.canvas.rows['stable']['bg'],ids[0])
        finally: widget.close()

    @unittest.skipUnless(sys.platform=='win32','Windows Tk scroll regression test')
    def test_short_list_cannot_overscroll(self):
        import tkinter as tk
        from types import SimpleNamespace
        self.store.upsert('短列表',id='short')
        root=tk.Tk(); root.withdraw(); widget=TodoWidget(root,self.store)
        try:
            root.update()
            self.assertLess(widget.canvas.content_height,widget.canvas.height)
            for delta in (120000,-120000,120,-120):
                widget.wheel(SimpleNamespace(delta=delta))
                self.assertAlmostEqual(widget.canvas.canvasy(0),0,delta=1)
            widget.canvas.scroll_command('moveto','1')
            self.assertAlmostEqual(widget.canvas.canvasy(0),0,delta=1)
        finally: widget.close()

    @unittest.skipUnless(sys.platform=='win32','Windows Tk scroll regression test')
    def test_long_list_clamps_and_keeps_other_rows(self):
        import tkinter as tk
        for i in range(18): self.store.upsert(f'事项{i}',id=f'item{i}')
        root=tk.Tk(); root.withdraw(); widget=TodoWidget(root,self.store)
        try:
            root.update()
            canvas=widget.canvas
            keep=canvas.rows['item5']['bg']
            canvas.scroll_command('scroll','100000','pages')
            self.assertLessEqual(canvas.canvasy(0),canvas.content_height-canvas.height+1)
            canvas.scroll_command('scroll','-100000','pages')
            self.assertAlmostEqual(canvas.canvasy(0),0,delta=1)
            widget.checkbuttons['item0'].invoke(); root.update()
            self.assertEqual(canvas.rows['item5']['bg'],keep)
            self.assertTrue(self.store.get('item0')['done'])
        finally: widget.close()

    @unittest.skipUnless(sys.platform=='win32','Windows named mutex test')
    def test_cleanup_detects_widget_mutex(self):
        import ctypes
        from ctypes import wintypes as W
        import cleanup
        from todo_widget import MUTEX
        self.assertEqual(cleanup.WIDGET_MUTEX,MUTEX)
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateMutexW.argtypes=[W.LPVOID,W.BOOL,W.LPCWSTR]
        kernel.CreateMutexW.restype=W.HANDLE
        kernel.CloseHandle.argtypes=[W.HANDLE]
        handle=kernel.CreateMutexW(None,False,MUTEX)
        self.assertTrue(handle)
        try:
            with self.assertRaises(ValueError): cleanup.require_widget_closed(ROOT)
        finally: kernel.CloseHandle(handle)

    @unittest.skipUnless(sys.platform=='win32','Windows Tk smoke test')
    def test_widget_check_and_live_refresh(self):
        import tkinter as tk
        self.store.upsert('测试任务',id='test',due='2026-10-11')
        root=tk.Tk(); root.withdraw()
        widget=TodoWidget(root,self.store)
        try:
            root.update()
            widget.checkbuttons['test'].invoke()
            self.assertTrue(self.store.get('test')['done'])
            widget.switch(True)
            widget.checkbuttons['test'].invoke()
            self.assertFalse(self.store.get('test')['done'])
            widget.switch(False)
            TodoStore(self.store.path).upsert('助手新加任务',id='new')
            root.after_cancel(widget.poll_id)
            widget.poll(); root.update()
            self.assertIn('new',widget.checkbuttons)
        finally: widget.close()


if __name__=='__main__': unittest.main()
