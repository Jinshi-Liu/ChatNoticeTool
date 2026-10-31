from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from todo_store import TodoStore
from todo_widget import TodoWidget


@unittest.skipUnless(sys.platform=='win32','Windows Tk modeless-window tests')
class DetailTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.tmp=tempfile.TemporaryDirectory()
        self.rules_path=Path(self.tmp.name)/'AGENTS.md'
        # Keep TodoStore's project-relative paths inside an isolated synthetic project.
        self.enterContext(patch('todo_store.ROOT',Path(self.tmp.name)))
        self.enterContext(patch('todo_store.project_path',side_effect=lambda path:Path(self.tmp.name)/path))
        self.store=TodoStore(Path(self.tmp.name)/'todos.sqlite3')
        self.store.upsert('第一项',id='a',due='2026-10-13',due_label='10/13课前',note='原内容')
        self.store.upsert('第二项',id='b',note='内容B')
        self.root=tk.Tk(); self.root.withdraw()
        self.app=TodoWidget(self.root,self.store,rules_path=self.rules_path)
        self.root.update()
    def tearDown(self):
        for editor in list(self.app.details_windows.values()): editor.close(force=True)
        if self.app.rules_window is not None: self.app.rules_window.close(force=True)
        self.app.close(); self.tmp.cleanup()
    def open(self,id):
        editor=self.app.details(self.store.get(id)); editor.window.withdraw()
        self.root.update(); return editor
    def test_native_topmost_matches_tk_after_toggle_and_remap(self):
        import ctypes
        from ctypes import wintypes as W
        user=ctypes.WinDLL('user32',use_last_error=True)
        user.GetParent.argtypes=[W.HWND]; user.GetParent.restype=W.HWND
        user.GetWindowLongW.argtypes=[W.HWND,ctypes.c_int]; user.GetWindowLongW.restype=ctypes.c_long
        a=self.open('a'); b=self.open('b')
        for window in (self.root,a.window,b.window): window.deiconify()
        self.root.update()
        def check(window,expected):
            hwnd=user.GetParent(window.winfo_id()) or window.winfo_id()
            self.assertEqual(bool(user.GetWindowLongW(hwnd,-20)&8),expected)
            self.assertEqual(bool(window.attributes('-topmost')),expected)
        check(self.root,True); check(a.window,False); check(b.window,False)
        a.toggle_pin(); self.app.toggle_pin(); self.root.update()
        check(self.root,False); check(a.window,True); check(b.window,False)
        a.window.withdraw(); self.root.update(); a.window.deiconify(); self.root.update()
        check(a.window,True)
        a.toggle_pin(); self.root.update(); check(a.window,False)

    def test_resize_burst_coalesces_canvas_layout(self):
        from types import SimpleNamespace
        canvas=self.app.canvas
        self.root.deiconify(); self.root.update()
        before=canvas.layout_count
        for width in range(250,350):
            canvas.resize(SimpleNamespace(width=width,height=200))
        self.assertEqual(canvas.layout_count,before)
        self.root.update_idletasks()
        self.assertEqual(canvas.layout_count,before+1)
        self.assertEqual((canvas.width,canvas.height),(349,200))

    def test_main_wheel_and_scrollbar_precision_and_isolation(self):
        from types import SimpleNamespace
        for i in range(12): self.store.upsert('合成事项'+str(i),id='wheel'+str(i))
        self.app.render(); self.root.geometry('500x300+20+20'); self.root.deiconify()
        detail=self.open('a'); detail.window.deiconify(); self.root.update()
        canvas=self.app.canvas; scrollbar=self.app.scrollbar
        before=canvas.layout_count
        canvas.event_generate('<MouseWheel>',delta=-120); self.root.update()
        self.assertAlmostEqual(canvas.canvasy(0),56,delta=1)
        scrollbar.event_generate('<MouseWheel>',delta=-120); self.root.update()
        self.assertAlmostEqual(canvas.canvasy(0),112,delta=1)
        self.assertAlmostEqual(float(scrollbar.get()[0]),canvas.canvasy(0)/canvas.extent,delta=.002)
        canvas.scroll_pixels(0)
        for _ in range(120): canvas.wheel(SimpleNamespace(delta=-1))
        self.assertAlmostEqual(canvas.canvasy(0),56,delta=1)
        for _ in range(120): canvas.wheel(SimpleNamespace(delta=1))
        self.assertAlmostEqual(canvas.canvasy(0),0,delta=1)
        canvas.scroll_pixels(1e9)
        canvas.wheel(SimpleNamespace(delta=-120))
        self.assertEqual(canvas.scroll_target,max(0,canvas.content_height-canvas.height))
        canvas.scroll_command('moveto','0.5')
        self.assertAlmostEqual(canvas.canvasy(0),canvas.extent*.5,delta=1)
        canvas.scroll_command('scroll',-1,'units')
        self.assertAlmostEqual(canvas.canvasy(0),canvas.extent*.5-28,delta=1)
        self.assertEqual(canvas.layout_count,before)
        self.assertEqual(detail.page_canvas.canvasy(0),0)

    def test_canvas_destroy_cancels_pending_resize_layout(self):
        canvas=self.app.canvas
        canvas.schedule_layout()
        pending=canvas.resize_pending
        canvas.destroy()
        self.assertNotIn(pending,self.root.tk.call('after','info'))
        self.root.update()

    def test_multiple_details_no_grab_and_main_operable(self):
        a=self.open('a'); b=self.open('b')
        self.assertNotEqual(a.window,b.window)
        self.assertIsNone(self.root.grab_current())
        self.assertEqual(str(a.window.transient()),'')
        self.app.checkbuttons['b'].invoke()
        self.assertTrue(self.store.get('b')['done'])
        self.assertTrue(a.window.winfo_exists()); self.assertTrue(b.window.winfo_exists())
        self.assertEqual(len(self.app.details_windows),2)
    def test_same_task_reuses_its_window(self):
        a=self.open('a')
        self.assertIs(self.app.editor(self.store.get('a')),a)
        self.assertEqual(len(self.app.details_windows),1)
    def test_edit_directly_and_preserve_external_check(self):
        a=self.open('a')
        self.assertEqual(a.note.cget('state'),'normal')
        self.store.mark('a',True)
        a.note.insert('end',' 新内容'); self.root.update()
        self.assertTrue(a.save())
        self.assertEqual(self.store.get('a')['note'],'原内容 新内容')
        self.assertTrue(self.store.get('a')['done'])
        self.assertTrue(a.window.winfo_exists())
    def test_two_editors_save_independently(self):
        a=self.open('a'); b=self.open('b')
        a.note.insert('end',' A'); b.note.insert('end',' B'); self.root.update()
        self.assertTrue(a.save()); self.assertTrue(b.save())
        self.assertEqual(self.store.get('a')['note'],'原内容 A')
        self.assertEqual(self.store.get('b')['note'],'内容B B')
    def test_external_same_field_conflict_retains_draft(self):
        a=self.open('a')
        a.note.insert('end',' 本地草稿'); self.root.update()
        original=self.store.get('a')
        self.store.patch_fields('a',{'note':'别处更新'},original)
        self.assertFalse(a.save())
        self.assertEqual(self.store.get('a')['note'],'别处更新')
        self.assertIn('本地草稿',a.note.get('1.0','end'))
    def test_date_change_clears_stale_label(self):
        a=self.open('a'); a.due_var.set('2026-10-16')
        self.assertTrue(a.save())
        self.assertEqual(self.store.get('a')['due_label'],'')
    def test_invalid_date_does_not_close_or_overwrite(self):
        a=self.open('a'); a.due_var.set('星期八')
        self.assertFalse(a.save())
        with patch('todo_detail.messagebox.askyesno',return_value=False): self.assertFalse(a.close())
        self.assertTrue(a.window.winfo_exists())
        self.assertEqual(self.store.get('a')['due'],'2026-10-13')
    def test_close_saves_dirty_note(self):
        a=self.open('a'); a.note.insert('end',' 关闭保存')
        self.assertTrue(a.close())
        self.assertEqual(self.store.get('a')['note'],'原内容 关闭保存')
    def test_editor_has_same_window_icon(self):
        a=self.open('a'); b=self.open('b')
        self.assertEqual(a.window.iconbitmap(),b.window.iconbitmap())
        self.assertEqual(a.window.iconbitmap(),self.root.iconbitmap())
    def test_minimal_layout_and_hand_drawn_icons(self):
        a=self.open('a')
        self.assertFalse(a.status.winfo_manager())
        self.assertFalse(hasattr(self.app,'subtitle'))
        self.assertFalse(hasattr(self.app,'status'))
        self.assertFalse(hasattr(self.app,'toast_until'))
        self.assertEqual(a.complete_button.kind,'check')
        self.assertGreaterEqual(self.app.pin_button.size,48)
        self.assertTrue(all(self.app.pin_button.type(i)=='line' for i in self.app.pin_button.find_withtag('icon')))
        row=self.app.canvas.rows['a']
        self.assertNotIn('glass',row)
        self.assertNotIn('circle',row)
        self.assertEqual(self.app.canvas.itemcget(row['tick'],'state'),'normal')

    def test_header_does_not_overlap_at_high_dpi(self):
        control=self.app.segment
        old=float(self.root.tk.call('tk','scaling'))
        try:
            for scale in (1,1.5,2,3):
                self.root.tk.call('tk','scaling',scale)
                self.root.update_idletasks()
                self.assertLess(control.bbox(control.left)[2],control.bbox(control.left_count)[0])
                self.assertLess(control.bbox(control.left_count)[2],control.bbox(control.right)[0])
                self.assertLess(control.bbox(control.right)[2],control.bbox(control.right_count)[0])
        finally: self.root.tk.call('tk','scaling',old)

    def test_draft_hints_save_icon_and_explicit_save(self):
        draft=self.app.editor(); draft.window.withdraw(); self.root.update()
        self.assertEqual(draft.complete_button.kind,'save')
        self.assertEqual(draft.title.winfo_class(),'Entry')
        self.assertEqual(draft.title.get(),'')
        self.assertEqual(draft.title.hint.winfo_manager(),'place')
        self.assertEqual(draft.due.hint.winfo_manager(),'place')
        draft.title.insert(0,'新的作业')
        draft.due_var.set('2026-10-18 09:00')
        self.root.update()
        self.assertIsNone(draft.pending)
        self.assertIsNone(draft.task)
        self.assertEqual(draft.title.hint.winfo_manager(),'')
        draft.complete_button.invoke()
        self.assertIsNotNone(draft.task)
        self.assertFalse(self.store.get(draft.task['id'])['done'])
        self.assertEqual(draft.complete_button.kind,'check')
        self.assertEqual(self.store.get(draft.task['id'])['title'],'新的作业')

    def test_completed_icons_are_undo_and_restore(self):
        self.store.mark('a',True); self.app.switch(True)
        a=self.open('a')
        self.assertEqual(a.complete_button.kind,'undo')
        row=self.app.canvas.rows['a']
        self.assertEqual(self.app.canvas.itemcget(row['tick'],'state'),'hidden')
        self.assertEqual(self.app.canvas.itemcget(row['undo_curve'],'state'),'normal')
        a.complete_button.invoke()
        self.assertFalse(self.store.get('a')['done'])
        self.assertEqual(a.complete_button.kind,'check')

    def test_pin_active_color_and_single_line_title(self):
        from glass_style import PIN_ACTIVE
        pin=self.app.pin_button
        pin.set_active(False)
        off={pin.itemcget(i,'fill') for i in pin.find_withtag('icon')}
        pin.set_active(True)
        on={pin.itemcget(i,'fill') for i in pin.find_withtag('icon')}
        self.assertNotEqual(off,on); self.assertEqual(on,{PIN_ACTIVE})
        a=self.open('a')
        self.assertEqual(a.title.winfo_class(),'Entry')
        a.title.delete(0,'end'); a.title.insert(0,'很长的标题'*30)
        self.assertTrue(a.save())
        self.assertEqual(len(self.store.get('a')['title']),150)

    def test_main_completion_updates_open_editor_icon(self):
        a=self.open('a')
        self.app.checkbuttons['a'].invoke()
        self.assertEqual(a.complete_button.kind,'undo')

    def test_one_deadline_preserves_qualifier(self):
        a=self.open('a')
        self.assertFalse(hasattr(a,'due_label'))
        self.assertEqual(a.due_var.get(),'2026-10-13 课前')
        a.note.insert('end',' 修改正文'); self.root.update(); self.assertTrue(a.save())
        self.assertEqual(self.store.get('a')['due_label'],'10/13课前')
        a.due_var.set('2026-10-15 09:00 上课前')
        self.assertTrue(a.save())
        self.assertEqual(self.store.get('a')['due'],'2026-10-15T09:00')
        self.assertIn('上课前',self.store.get('a')['due_label'])
        a.close(); a=self.open('a')
        self.assertEqual(a.due_var.get(),'2026-10-15 09:00 上课前')

    def test_trash_directly_removes_without_menu(self):
        from types import SimpleNamespace
        task=self.store.get('a')
        row=self.app.canvas.rows['a']
        self.assertIn('trash_body',row)
        self.assertNotIn('dot0',row)
        with patch('todo_widget.messagebox.askyesno',return_value=False):
            self.app.context_menu(task)
        self.assertFalse(self.store.get('a')['removed'])
        left,top,right,bottom=self.app.canvas.coords(row['menu_hit'])
        with patch('todo_widget.messagebox.askyesno',return_value=True):
            self.app.canvas.click_row('a',SimpleNamespace(x=(left+right)/2,y=(top+bottom)/2))
        self.assertTrue(self.store.get('a')['removed'])
        self.assertFalse(hasattr(self.app,'active_menu'))

    def test_larger_fonts_in_list_and_editor(self):
        a=self.open('a')
        canvas=self.app.canvas; row=canvas.rows['a']
        for name,pixels in (('title',34),('meta',26),('preview',28)):
            self.assertEqual(int(self.root.tk.splitlist(canvas.itemcget(row[name],'font'))[1]),-pixels)
        for widget,pixels in ((a.title,38),(a.due,27),(a.note,30)):
            self.assertEqual(int(self.root.tk.splitlist(widget.cget('font'))[1]),-pixels)
        self.assertEqual(a.title.winfo_class(),'Entry')

    def test_clear_completed_visibility_and_empty_state(self):
        self.assertFalse(self.app.completed_actions.winfo_manager())
        with patch('todo_widget.messagebox.askyesno') as confirm:
            self.app.clear_completed()
            self.app.switch(True); self.root.update()
            self.assertEqual(self.app.completed_actions.winfo_manager(),'pack')
            self.assertEqual(str(self.app.clear_completed_button.cget('state')),'disabled')
            self.app.clear_completed()
            confirm.assert_not_called()
        self.store.mark('a',True); self.app.render()
        self.assertEqual(str(self.app.clear_completed_button.cget('state')),'normal')
        self.app.switch(False)
        self.assertFalse(self.app.completed_actions.winfo_manager())

    def test_clear_completed_cancel_then_confirm_preserves_pending_and_draft(self):
        self.store.mark('a',True); self.app.switch(True)
        completed=self.open('a'); pending=self.open('b')
        draft=self.app.editor(); draft.window.withdraw()
        with patch('todo_widget.messagebox.askyesno',return_value=False):
            self.app.clear_completed_button.invoke()
        self.assertFalse(self.store.get('a')['removed'])
        self.assertFalse(completed.closed)
        with patch('todo_widget.messagebox.askyesno',return_value=True):
            self.app.clear_completed_button.invoke()
        self.assertTrue(self.store.get('a')['removed'])
        self.assertTrue(completed.closed)
        self.assertFalse(self.store.get('b')['removed'])
        self.assertFalse(pending.closed); self.assertFalse(draft.closed)
        self.assertEqual(self.app.canvas.rows,{})
        self.assertEqual(str(self.app.clear_completed_button.cget('state')),'disabled')

    def test_clear_completed_cancel_invalid_draft_keeps_entire_batch(self):
        for id in ('a','b'): self.store.mark(id,True)
        self.app.switch(True)
        a=self.open('a'); a.due_var.set('无效日期'); self.root.update()
        with patch('todo_widget.messagebox.askyesno',side_effect=[True,False]):
            self.app.clear_completed()
        self.assertFalse(a.closed)
        self.assertEqual(a.due_var.get(),'无效日期')
        self.assertEqual(len(self.store.tasks(done=True)),2)

    def test_clear_completed_saves_dirty_details_before_removing(self):
        self.store.mark('a',True); self.app.switch(True)
        a=self.open('a'); a.note.insert('end',' 未保存内容'); self.root.update()
        with patch('todo_widget.messagebox.askyesno',return_value=True):
            self.app.clear_completed()
        task=self.store.get('a')
        self.assertTrue(task['removed']); self.assertIn('未保存内容',task['note'])
        self.assertTrue(a.closed)

    def test_clear_completed_handles_changes_during_confirmation(self):
        self.store.mark('a',True); self.app.switch(True)
        a=self.open('a')
        def confirm(*args,**kwargs):
            self.store.mark('a',False)
            self.store.mark('b',True)
            return True
        with patch('todo_widget.messagebox.askyesno',side_effect=confirm):
            self.app.clear_completed()
        self.assertFalse(self.store.get('a')['removed'])
        self.assertFalse(self.store.get('b')['removed'])
        self.assertFalse(a.closed)
        self.assertEqual(set(self.app.canvas.rows),{'b'})

    def test_clear_completed_button_fits_small_windows(self):
        self.app.switch(True); self.root.deiconify()
        try:
            for width,height in ((220,160),(280,200),(500,700)):
                self.root.geometry(f'{width}x{height}+100+100'); self.root.update()
                button=self.app.clear_completed_button
                self.assertGreaterEqual(button.winfo_rootx(),self.root.winfo_rootx())
                self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),
                                     self.root.winfo_rootx()+self.root.winfo_width())
                self.assertGreaterEqual(button.winfo_rooty(),
                                        self.app.header.winfo_rooty()+self.app.header.winfo_height())
                self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),
                                     self.root.winfo_rooty()+self.root.winfo_height())
        finally: self.root.withdraw()

    def test_icon_save_never_changes_completion_and_has_no_save_words(self):
        self.store.mark('a',True)
        a=self.open('a'); a.note.insert('end',' 保存测试'); self.root.update()
        self.assertFalse(hasattr(a,'save_button'))
        self.assertEqual(a.complete_button.kind,'save')
        self.assertTrue(a.complete_button.invoke())
        self.assertEqual(self.store.get('a')['note'],'原内容 保存测试')
        self.assertTrue(self.store.get('a')['done'])
        self.assertFalse(a.status.winfo_manager())

    def test_primary_dirty_action_saves_not_completes(self):
        a=self.open('a'); a.note.insert('end',' 草稿'); self.root.update()
        self.assertEqual(a.complete_button.kind,'save')
        a.complete_button.invoke()
        self.assertEqual(self.store.get('a')['note'],'原内容 草稿')
        self.assertFalse(self.store.get('a')['done'])

    def test_friendly_deadline_input(self):
        from todo_detail import parse_deadline_text
        for text in ('10/13','10月13日','2030/10/13'):
            self.assertEqual(parse_deadline_text(text,'2030-01-01'),('2030-10-13',''))
        self.assertEqual(parse_deadline_text('10/13 9:00 上课前','2030-01-01'),
                         ('2030-10-13T09:00','2030-10-13 09:00 上课前'))
        self.assertEqual(parse_deadline_text('10月13日上课前','2030-01-01'),
                         ('2030-10-13','2030-10-13 上课前'))
        with self.assertRaises(ValueError): parse_deadline_text('2/30','2030-01-01')
        a=self.open('a'); a.due_var.set('10/16 上课前'); a.manual_save()
        self.assertEqual(self.store.get('a')['due'],'2026-10-16')

    def test_compact_spacing_and_save_error_is_visible(self):
        a=self.open('a'); a.window.deiconify(); self.root.update()
        gap=a.due.winfo_rooty()-(a.title.winfo_rooty()+a.title.winfo_height())
        self.assertLessEqual(gap,10)
        a.due_var.set('星期八'); self.assertFalse(a.manual_save()); self.root.update()
        self.assertEqual(a.status.winfo_manager(),'pack')
        self.assertLess(a.status.winfo_y(),a.content.winfo_y())
        self.assertEqual(self.store.get('a')['due'],'2026-10-13')
        self.assertEqual(a.complete_button.kind,'save')

    def test_detail_pins_independent_from_main_and_siblings(self):
        a=self.open('a'); b=self.open('b')
        self.assertFalse(a.pinned); self.assertFalse(b.pinned)
        main=self.app.pinned
        a.pin_button.invoke(); self.root.update()
        self.assertTrue(a.pinned); self.assertTrue(bool(a.window.attributes('-topmost')))
        self.assertFalse(b.pinned); self.assertEqual(self.app.pinned,main)
        self.app.toggle_pin(); self.root.update()
        self.assertTrue(a.pinned); self.assertFalse(b.pinned)
        self.assertFalse(bool(b.window.attributes('-topmost')))
        b.pin_button.invoke(); a.pin_button.invoke(); self.root.update()
        self.assertFalse(a.pinned); self.assertTrue(b.pinned)
        self.assertEqual(self.app.pinned,not main)

    def test_detail_pin_persists_on_reopen_and_draft_save(self):
        a=self.open('a'); a.toggle_pin(); a.close()
        a=self.open('a'); self.assertTrue(a.pinned)
        draft=self.app.editor(); draft.window.withdraw()
        draft.pin_button.invoke(); draft.title.insert(0,'新事项'); self.root.update()
        draft.complete_button.invoke()
        id=draft.task['id']
        self.assertTrue(self.store.setting('detail_pinned:'+id))
        draft.close(); reopened=self.open(id)
        self.assertTrue(reopened.pinned)

    def test_small_windows_are_usable_and_never_shrink_fonts(self):
        a=self.open('a'); b=self.open('b')
        original=a.values()
        self.root.geometry('220x160+20+20'); self.root.deiconify()
        a.window.geometry('220x160+260+20'); a.window.deiconify()
        b.window.deiconify(); self.root.update()
        self.assertEqual(self.root.minsize(),(390,160))
        self.assertEqual(a.window.minsize(),(220,160))
        self.assertTrue(self.app.compact); self.assertTrue(a.compact); self.assertFalse(b.compact)
        for button in (self.app.document_button,self.app.add_button,self.app.pin_button,a.complete_button,a.pin_button):
            self.assertTrue(button.winfo_viewable())
            self.assertEqual(button.size,48)
            self.assertEqual((button.winfo_width(),button.winfo_height()),(50,50))
        for button in (self.app.document_button,self.app.add_button,self.app.pin_button):
            self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
        self.assertGreater(a.note.winfo_height(),20)
        row=self.app.canvas.rows['a']
        self.assertLessEqual(max(self.app.canvas.coords(row['bg'])[::2]),self.app.canvas.winfo_width())
        self.assertEqual(self.app.canvas.itemcget(row['preview'],'state'),'normal')
        for widget,pixels in ((a.title,38),(a.due,27),(a.note,30),(a.title.hint,38),(a.due.hint,27),(a.status,16)):
            self.assertEqual(int(self.root.tk.splitlist(widget.cget('font'))[1]),-pixels)
        for name,pixels in (('title',34),('meta',26),('preview',28)):
            self.assertEqual(int(self.root.tk.splitlist(self.app.canvas.itemcget(row[name],'font'))[1]),-pixels)
        for item,pixels in ((self.app.segment.left,32),(self.app.segment.right,32),
                            (self.app.segment.left_count,16),(self.app.segment.right_count,16)):
            self.assertEqual(int(self.root.tk.splitlist(self.app.segment.itemcget(item,'font'))[1]),-pixels)
        self.app.segment.event_generate('<Button-1>',x=120,y=28); self.root.update()
        self.assertTrue(self.app.view_done)
        self.app.segment.event_generate('<Button-1>',x=20,y=28); self.root.update()
        self.assertFalse(self.app.view_done)
        row=self.app.canvas.rows['a']
        self.store.upsert('小窗新行',id='c',note='字号不变'); self.app.render(); self.root.update()
        new_row=self.app.canvas.rows['c']
        self.assertEqual(int(self.root.tk.splitlist(self.app.canvas.itemcget(new_row['title'],'font'))[1]),-34)
        self.assertEqual(a.values(),original); self.assertFalse(a.dirty)
        self.root.geometry('500x700'); a.window.geometry('560x590'); self.root.update()
        self.assertFalse(self.app.compact); self.assertFalse(a.compact)
        self.assertEqual(a.complete_button.size,48)
        self.assertEqual(int(self.root.tk.splitlist(a.note.cget('font'))[1]),-30)
        self.assertEqual(self.app.canvas.itemcget(row['preview'],'state'),'normal')

    def assert_header_geometry(self):
        segment=self.app.segment
        buttons=(self.app.document_button,self.app.pin_button,self.app.add_button)
        root_left=self.root.winfo_rootx(); root_right=root_left+self.root.winfo_width()
        boxes=[]
        for item in (segment.left,segment.left_count,segment.right,segment.right_count):
            x1,y1,x2,y2=segment.bbox(item)
            boxes.append((segment.winfo_rootx()+x1,segment.winfo_rooty()+y1,
                          segment.winfo_rootx()+x2,segment.winfo_rooty()+y2))
        for button in buttons:
            self.assertTrue(button.winfo_viewable())
            boxes.append((button.winfo_rootx(),button.winfo_rooty(),
                          button.winfo_rootx()+button.winfo_width(),
                          button.winfo_rooty()+button.winfo_height()))
        for box in boxes:
            self.assertGreaterEqual(box[0],root_left)
            self.assertLessEqual(box[2],root_right)
            self.assertGreaterEqual(box[1],self.root.winfo_rooty())
            self.assertLessEqual(box[3],self.root.winfo_rooty()+self.root.winfo_height())
        for index,a in enumerate(boxes):
            for b in boxes[index+1:]:
                self.assertTrue(a[2]<=b[0] or b[2]<=a[0] or a[3]<=b[1] or b[3]<=a[1],(a,b))
        self.assertLess(boxes[4][0],boxes[5][0]); self.assertLess(boxes[5][0],boxes[6][0])
        centers=[(box[1]+box[3])/2 for box in boxes[4:]]
        self.assertAlmostEqual(min(centers),max(centers),delta=1)
        if int(self.app.toolbar.grid_info()['row'])==0:
            self.assertAlmostEqual(centers[0],segment.winfo_rooty()+segment.coords(segment.left)[1],delta=2)
        else:
            self.assertGreaterEqual(boxes[4][1],max(box[3] for box in boxes[:4]))
        self.assertEqual(int(self.root.tk.splitlist(segment.itemcget(segment.right,'font'))[1]),-32)

    def test_toolbar_stays_fixed_when_tab_counts_expand(self):
        self.root.geometry('390x200+20+20'); self.root.deiconify(); self.root.update()
        buttons=(self.app.document_button,self.app.pin_button,self.app.add_button)
        before=[(b.winfo_rootx(),b.winfo_rooty(),b.winfo_width(),b.winfo_height()) for b in buttons]
        self.app.segment.update_state(123456789,123456789,False)
        self.app.layout_header(); self.root.update()
        after=[(b.winfo_rootx(),b.winfo_rooty(),b.winfo_width(),b.winfo_height()) for b in buttons]
        self.assertEqual(before,after)
        self.assertEqual(int(self.app.toolbar.grid_info()['row']),0)
        self.assertTrue(all(w==50 and h==50 for _,_,w,h in after))
        self.assertEqual(after[1][0]-after[0][0],54)
        self.assertEqual(after[2][0]-after[1][0],54)

    def test_header_five_controls_share_one_alignment_when_wide(self):
        for width,height in ((500,700),):
            self.root.geometry(f'{width}x{height}+20+20'); self.root.deiconify(); self.root.update()
            self.assertEqual(self.app.segment.winfo_manager(),'grid')
            self.assertEqual(int(self.app.toolbar.grid_info()['row']),0)
            self.assert_header_geometry()

    def test_header_real_geometry_at_each_width_and_high_dpi(self):
        self.root.deiconify()
        old=float(self.root.tk.call('tk','scaling'))
        try:
            for scale in (1,1.5,2,3):
                self.root.tk.call('tk','scaling',scale)
                for width in (390,400,460,500,390):
                    with self.subTest(scale=scale,width=width):
                        self.root.geometry(f'{width}x160+20+20'); self.root.update()
                        self.app.layout_header(); self.root.update()
                        self.assert_header_geometry()
                        self.assertEqual(int(self.app.toolbar.grid_info()['row']),0)
        finally: self.root.tk.call('tk','scaling',old)

    def test_document_button_opens_single_modeless_rules_editor(self):
        editor=self.app.document_button.invoke(); self.root.update()
        self.assertEqual(self.app.document_button.kind,'document')
        self.assertIs(self.app.document_button.invoke(),editor)
        self.assertIsNone(self.root.grab_current())
        self.assertEqual(str(editor.window.transient()),'')
        self.assertFalse(self.rules_path.exists())
        self.assertEqual(editor.value(),'')
        self.assertTrue(all(self.app.document_button.type(i)=='line'
                            for i in self.app.document_button.find_withtag('icon')))
        self.app.checkbuttons['b'].invoke()
        self.assertTrue(self.store.get('b')['done'])
        editor.close()
        self.assertIsNot(self.app.open_rules(),editor)

    def test_rules_are_not_read_during_startup_render_or_poll(self):
        import tkinter as tk
        self.app.close()
        self.root=tk.Tk(); self.root.withdraw()
        with patch('rules_editor.RulesEditor.read_disk',side_effect=AssertionError('unexpected rules read')) as read:
            self.app=TodoWidget(self.root,self.store,rules_path=self.rules_path)
            self.root.update(); self.app.render()
            self.root.after_cancel(self.app.poll_id)
            self.app.poll()
            read.assert_not_called()
        self.assertIsNone(self.app.rules_window)

    def test_main_close_can_be_cancelled_by_rules_draft(self):
        editor=self.app.open_rules(); editor.text.insert('1.0','未保存规则')
        with patch('rules_editor.messagebox.askyesnocancel',return_value=None): self.app.close()
        self.assertFalse(self.app.closed); self.assertTrue(editor.window.winfo_exists())
        self.assertFalse(self.rules_path.exists())
        with patch('rules_editor.messagebox.askyesnocancel',return_value=True): self.app.close()
        self.assertTrue(self.app.closed)
        self.assertEqual(self.rules_path.read_text(encoding='utf-8'),'未保存规则')

    def test_small_detail_size_remembered_per_task(self):
        a=self.open('a'); a.window.geometry('220x160+20+20'); a.window.deiconify(); self.root.update()
        a.close()
        a=self.open('a'); a.window.deiconify(); self.root.update()
        self.assertEqual((a.window.winfo_width(),a.window.winfo_height()),(220,160))
        self.assertTrue(a.compact)
        b=self.open('b'); b.window.deiconify(); self.root.update()
        self.assertFalse(b.compact)

    def test_main_close_remembers_open_detail_sizes(self):
        a=self.open('a'); a.window.geometry('220x160+20+20'); a.window.deiconify(); self.root.update()
        self.app.close()
        self.assertTrue(self.store.setting('detail_geometry:a').startswith('220x160'))
        self.assertEqual(self.store.get('a')['note'],'原内容')

    def test_small_saved_main_geometry_is_clamped(self):
        import tkinter as tk
        self.app.close()
        self.store.set_setting('geometry','220x160+20+20')
        self.root=tk.Tk(); self.app=TodoWidget(self.root,self.store,rules_path=self.rules_path); self.root.update()
        self.assertEqual((self.root.winfo_width(),self.root.winfo_height()),(390,160))
        self.assertTrue(self.app.compact)

    def test_plus_draft_saves_in_small_window(self):
        draft=self.app.editor(); draft.window.geometry('220x160+20+20'); draft.window.deiconify(); self.root.update()
        self.assertTrue(draft.compact)
        draft.title.insert(0,'小窗草稿'); draft.note.insert('end','保留正文'); self.root.update()
        self.assertTrue(draft.complete_button.invoke())
        self.assertEqual(self.store.get(draft.task['id'])['note'],'保留正文')
        self.assertFalse(self.store.get(draft.task['id'])['done'])

    def test_wheel_scrolls_headers_out_then_body_and_back(self):
        a=self.open('a'); a.window.geometry('300x200+20+20'); a.window.deiconify()
        a.note.insert('end','\n'+'\n'.join('正文复习第'+str(i)+'行' for i in range(60)))
        self.root.update(); values=a.values(); original_y=a.title.winfo_rooty()
        a.note.event_generate('<MouseWheel>',delta=-120); self.root.update()
        self.assertLess(a.title.winfo_rooty(),original_y)
        for _ in range(8): a.note.event_generate('<MouseWheel>',delta=-120)
        self.root.update()
        viewport=a.page_canvas.winfo_rooty()
        for header in (a.top,a.date_row):
            self.assertLessEqual(header.winfo_rooty()+header.winfo_height(),viewport)
        self.assertGreater(a.note.yview()[0],0)
        self.assertGreaterEqual(a.note.winfo_height(),a.page_canvas.winfo_height()-10)
        self.assertEqual(a.values(),values)
        self.assertEqual(int(self.root.tk.splitlist(a.note.cget('font'))[1]),-30)
        for _ in range(14): a.note.event_generate('<MouseWheel>',delta=120)
        self.root.update()
        self.assertAlmostEqual(a.title.winfo_rooty(),original_y,delta=1)
        self.assertAlmostEqual(a.note.yview()[0],0,delta=.001)

    def test_short_page_can_hide_headers_without_editing(self):
        a=self.open('a'); a.window.geometry('300x200+20+20'); a.window.deiconify(); self.root.update()
        values=a.values()
        a.scroll_page('moveto',1); self.root.update()
        self.assertLessEqual(a.date_row.winfo_rooty()+a.date_row.winfo_height(),a.page_canvas.winfo_rooty())
        self.assertEqual(a.values(),values); self.assertFalse(a.dirty)
        a.scroll_page('moveto',0); self.root.update()
        self.assertGreaterEqual(a.title.winfo_rooty(),a.page_canvas.winfo_rooty())
        self.assertTrue(a.manual_save())
        self.assertEqual(self.store.get('a')['note'],'原内容')

    def test_scroll_position_is_independent_per_window(self):
        a=self.open('a'); b=self.open('b')
        for editor in (a,b): editor.window.geometry('300x200+20+20'); editor.window.deiconify()
        self.root.update(); b_position=b.title.winfo_rooty()
        a.scroll_page('moveto',1); self.root.update()
        self.assertEqual(b.title.winfo_rooty(),b_position)
        self.assertEqual(b.page_canvas.canvasy(0),0)

    def test_auto_save_timer(self):
        a=self.open('a'); a.note.insert('end',' 自动'); self.root.update()
        self.assertIsNotNone(a.pending)
        self.root.after(1250,self.root.quit); self.root.mainloop()
        self.assertEqual(self.store.get('a')['note'],'原内容 自动')


if __name__=='__main__': unittest.main()
