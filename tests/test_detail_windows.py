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
        (ROOT/'runtime').mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=ROOT/'runtime')
        self.store=TodoStore(Path(self.tmp.name)/'todos.sqlite3')
        self.store.upsert('第一项',id='a',due='2026-10-13',due_label='10/13课前',note='原内容')
        self.store.upsert('第二项',id='b',note='内容B')
        self.root=tk.Tk(); self.root.withdraw()
        self.app=TodoWidget(self.root,self.store)
        self.root.update()
    def tearDown(self):
        for editor in list(self.app.details_windows.values()): editor.close(force=True)
        self.app.close(); self.tmp.cleanup()
    def open(self,id):
        editor=self.app.details(self.store.get(id)); editor.window.withdraw()
        self.root.update(); return editor
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
        self.assertFalse(self.app.subtitle.winfo_manager())
        self.assertFalse(self.app.status.winfo_manager())
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
        self.assertIsNone(self.app.active_menu)

    def test_larger_fonts_in_list_and_editor(self):
        a=self.open('a')
        canvas=self.app.canvas; row=canvas.rows['a']
        for name,pixels in (('title',34),('meta',26),('preview',28)):
            self.assertEqual(int(self.root.tk.splitlist(canvas.itemcget(row[name],'font'))[1]),-pixels)
        for widget,pixels in ((a.title,38),(a.due,27),(a.note,30)):
            self.assertEqual(int(self.root.tk.splitlist(widget.cget('font'))[1]),-pixels)
        self.assertEqual(a.title.winfo_class(),'Entry')

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
        self.assertEqual(self.root.minsize(),(220,160))
        self.assertEqual(a.window.minsize(),(220,160))
        self.assertTrue(self.app.compact); self.assertTrue(a.compact); self.assertFalse(b.compact)
        for button in (self.app.add_button,self.app.pin_button,a.complete_button,a.pin_button):
            self.assertTrue(button.winfo_viewable())
            self.assertEqual(button.size,28)
        for button in (self.app.add_button,self.app.pin_button):
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

    def test_small_header_four_controls_share_one_alignment(self):
        self.root.geometry('367x180+20+20'); self.root.deiconify(); self.root.update()
        segment=self.app.segment
        self.assertEqual(segment.pack_info()['side'],'left')
        y=segment.winfo_rooty()+segment.coords(segment.left)[1]
        self.assertEqual(segment.coords(segment.left)[1],segment.coords(segment.right)[1])
        for button in (self.app.pin_button,self.app.add_button):
            self.assertAlmostEqual(button.winfo_rooty()+button.winfo_height()/2,y,delta=2)
            self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
        self.assertEqual(int(self.root.tk.splitlist(segment.itemcget(segment.left,'font'))[1]),-32)

    def test_extreme_narrow_header_is_aligned_and_reflows_without_font_change(self):
        self.root.deiconify()
        for width in (220,250,367,240,280):
            self.root.geometry(f'{width}x180+20+20'); self.root.update()
            segment=self.app.segment
            if width<280:
                self.assertEqual(segment.pack_info()['side'],'top')
                for button,item in ((self.app.pin_button,segment.left),(self.app.add_button,segment.right)):
                    box=segment.bbox(item)
                    center=segment.winfo_rootx()+(box[0]+box[2])/2
                    self.assertAlmostEqual(button.winfo_rootx()+button.winfo_width()/2,center,delta=3)
            else: self.assertEqual(segment.pack_info()['side'],'left')
            self.assertAlmostEqual(self.app.pin_button.winfo_rooty()+self.app.pin_button.winfo_height()/2,
                                   self.app.add_button.winfo_rooty()+self.app.add_button.winfo_height()/2,delta=1)
            self.assertEqual(int(self.root.tk.splitlist(segment.itemcget(segment.right,'font'))[1]),-32)

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

    def test_small_saved_main_geometry_is_not_expanded(self):
        import tkinter as tk
        self.app.close()
        self.store.set_setting('geometry','220x160+20+20')
        self.root=tk.Tk(); self.app=TodoWidget(self.root,self.store); self.root.update()
        self.assertEqual((self.root.winfo_width(),self.root.winfo_height()),(220,160))
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
