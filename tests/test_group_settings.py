"""One table, local-only refresh, wheel routing and lifecycle; synthetic data only."""
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_config import AgentConfig
from group_settings import COLUMNS, FOOTER, group_rows, group_sections, time_text


class SettingsFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='chatnotice-settings-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name).resolve()
        self.config = AgentConfig(self.project)
        database, media = self.project / 'synthetic-db', self.project / 'synthetic-media'
        database.mkdir()
        media.mkdir()
        self.source = self.config.configure_source({'confirmed': True, 'expected_revision': 0,
            'platform': 'qq', 'name': '合成 QQ', 'paths': {'database_dir': str(database), 'media_dir': str(media)}})['source']
        self.groups = self.config.apply_groups({'confirmed': True, 'expected_revision': 1, 'groups': [
            {'source_id': self.source['id'], 'group_id': identity, 'name': name, 'enabled': enabled}
            for identity, name, enabled in (('123456789', '合成课程群', True), ('987654321', '合成作业群', False))]})['groups']

    def add_many(self, count=80):
        account = self.project / 'synthetic-wechat'
        (account / 'db_storage').mkdir(parents=True)
        wechat = self.config.configure_source({'confirmed': True, 'expected_revision': self.config.load()['revision'],
            'platform': 'wechat', 'paths': {'database_dir': str(account / 'db_storage'), 'account_dir': str(account)}})['source']
        inbox = self.config.configure_source({'confirmed': True, 'expected_revision': self.config.load()['revision'],
                                              'platform': 'import'})['source']
        rows = []
        for i in range(count):
            source = (self.source, wechat, inbox)[i % 3]
            identity = str(100000000 + i) if i % 3 == 0 else f'synthetic-{i}@chatroom' if i % 3 == 1 else f'inbox-{i}'
            rows.append({'source_id': source['id'], 'group_id': identity, 'name': f'列表 {i:03} · 合成群'})
        self.config.apply_groups({'confirmed': True, 'expected_revision': self.config.load()['revision'], 'groups': rows})


class GroupOverviewTests(SettingsFixture, unittest.TestCase):
    def test_rows_are_read_only_and_never_initialize_processing_or_query_client(self):
        before = self.config.path.read_bytes()
        with patch('agent_sources.LocalSource.groups', side_effect=AssertionError('no client query')):
            rows = group_rows(self.config)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['platform'] == 'qq' and row['last_read_at'] is None for row in rows))
        self.assertFalse((self.project / 'state/processing.sqlite3').exists())
        self.assertEqual(self.config.path.read_bytes(), before)

    def test_other_platforms_are_rows_not_separate_lists(self):
        self.add_many(9)
        rows = group_rows(self.config)
        self.assertEqual([row['name'] for row in rows[:9]], [f'列表 {i:03} · 合成群' for i in range(9)])
        self.assertEqual([row['platform'] for row in rows[:6]], ['qq', 'wechat', 'import'] * 2)
        # Keep the metadata helper compatible with existing backend callers.
        self.assertEqual(set(group_sections(self.config)), {'qq', 'wechat', 'import'})

    def test_time_is_compact_with_full_value_available(self):
        from datetime import datetime
        from project_core import LOCAL_TZ
        stamp = int(datetime(2030, 11, 2, 9, 7, 42, tzinfo=LOCAL_TZ).timestamp())
        self.assertEqual(time_text(None), '尚未读取')
        self.assertEqual(time_text(stamp), '11-02 09:07')
        self.assertEqual(time_text(stamp, full=True), '2030-11-02T09:07:42+08:00')


@unittest.skipUnless(sys.platform == 'win32', 'Windows Tk settings tests')
class ManagementWindowTests(SettingsFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        import tkinter as tk
        from rules_editor import RulesEditor
        self.rules_path = self.project / 'AGENTS.md'
        self.root = tk.Tk()
        self.root.withdraw()
        self.editor = RulesEditor(self.root, self.rules_path, config=self.config)
        self.editor.window.geometry('920x720+100+70')
        self.editor.focus()
        self.root.update()
        self.addCleanup(self.close_window)

    def close_window(self):
        self.editor.close(force=True)
        self.root.destroy()

    def open_groups(self):
        self.editor.switch_tab('groups')
        self.root.update()
        return self.editor.groups_page

    def wait(self, milliseconds):
        import tkinter as tk
        done = tk.BooleanVar(self.root)
        self.root.after(milliseconds, lambda: done.set(True))
        self.root.wait_variable(done)

    def all_children(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self.all_children(child)

    def test_lazy_read_only_single_table_exact_columns_and_footer(self):
        self.assertIsNone(self.editor.groups_page)
        before = self.config.path.read_bytes()
        with patch('agent_sources.LocalSource.groups', side_effect=AssertionError('no client query')):
            page = self.open_groups()
        self.assertEqual(len(page.table.get_children()), 2)
        self.assertEqual(tuple(page.table['columns']), COLUMNS)
        self.assertEqual([page.table.heading(key, 'text') for key in COLUMNS], ['平台', '群名', '群号 / 群 ID', '最后读取'])
        self.assertEqual(page.footer.cget('text'), FOOTER)
        self.assertFalse(page.dirty)
        self.assertFalse(self.editor.save_button.winfo_viewable())
        self.assertFalse(self.editor.save())
        widgets = list(self.all_children(page))
        self.assertEqual(sum(widget.winfo_class() == 'Treeview' for widget in widgets), 1)
        self.assertFalse(any(widget.winfo_class() in ('Entry', 'Text', 'TCombobox', 'Checkbutton', 'Button', 'Canvas') for widget in widgets))
        self.assertEqual(self.config.path.read_bytes(), before)
        self.assertFalse(self.rules_path.exists())
        self.assertFalse((self.project / 'state/processing.sqlite3').exists())

    def test_tabs_keep_large_rule_draft_and_group_ctrl_s_cannot_write(self):
        self.assertIn('-23', self.editor.text.cget('font'))
        self.editor.text.insert('1.0', '用户自己的规则')
        before = self.config.path.read_bytes()
        page = self.open_groups()
        self.editor.window.focus_force()
        page.table.focus_force()
        self.root.update()
        page.table.event_generate('<Control-s>')
        self.root.update()
        self.assertEqual(self.config.path.read_bytes(), before)
        self.assertFalse(self.rules_path.exists())
        self.assertTrue(self.editor.rules_dirty)
        self.editor.switch_tab('rules')
        self.root.update()
        self.assertTrue(self.editor.save_button.winfo_viewable())
        self.assertEqual(self.editor.value(), '用户自己的规则')
        self.assertTrue(self.editor.save())

    def test_close_from_groups_only_prompts_for_rules(self):
        self.editor.text.insert('1.0', '关闭前的规则')
        self.open_groups()
        with patch('rules_editor.messagebox.askyesnocancel', return_value=None):
            self.assertFalse(self.editor.close())
        with patch('rules_editor.messagebox.askyesnocancel', return_value=True):
            self.assertTrue(self.editor.close())
        self.assertEqual(self.rules_path.read_text(encoding='utf-8'), '关闭前的规则')

    def test_mixed_platforms_scroll_together_and_footer_stays_fixed(self):
        self.add_many()
        page = self.open_groups()
        order = page.table.get_children()
        self.assertEqual([page.table.item(key, 'values')[0] for key in order[:6]], ['QQ', '微信', '文件'] * 2)
        footer_y = page.footer.winfo_rooty()
        self.assertEqual(page.table.yview()[0], 0)
        page.table.event_generate('<MouseWheel>', delta=-120, x=180, y=60)
        self.root.update()
        self.assertGreater(page.table.yview()[0], 0)
        self.assertEqual(page.footer.winfo_rooty(), footer_y)
        page.table.yview_moveto(1)
        self.root.update()
        self.assertTrue(page.footer.winfo_viewable())
        self.assertEqual(page.footer.winfo_rooty(), footer_y)

    def test_wheel_on_rows_blank_and_footer_is_once_with_same_speed(self):
        self.add_many()
        page = self.open_groups()
        for widget in (page.table, page.viewport, page, page.footer, page.vertical, page.horizontal, page.empty):
            self.assertEqual(widget.bindtags()[0], page._wheel_tag)
            if not widget.winfo_ismapped():
                continue
            with self.subTest(widget=str(widget)):
                page.table.yview_moveto(0)
                with patch.object(page.table, 'yview_scroll', wraps=page.table.yview_scroll) as scroll:
                    widget.event_generate('<MouseWheel>', delta=-120, x=2, y=2)
                    self.root.update()
                scroll.assert_called_once_with(3, 'units')
                self.assertAlmostEqual(page.table.yview()[0], 3 / len(page.rows))

    def test_small_delta_accumulates_and_never_double_scrolls(self):
        self.add_many()
        page = self.open_groups()
        for _ in range(6):
            page.table.event_generate('<MouseWheel>', delta=-20)
        self.root.update()
        self.assertAlmostEqual(page.table.yview()[0], 3 / len(page.rows))
        for _ in range(120):
            page.wheel(SimpleNamespace(delta=-1))
        self.root.update()
        self.assertAlmostEqual(page.table.yview()[0], 6 / len(page.rows))

    def test_normal_width_has_no_horizontal_bar_compact_only_if_overflow(self):
        page = self.open_groups()
        self.assertFalse(page.horizontal.winfo_ismapped())
        self.assertEqual(page.table.xview(), (0.0, 1.0))
        self.editor.window.geometry('480x640')
        self.root.update()
        self.assertTrue(page.horizontal.winfo_ismapped())
        self.assertLess(page.table.xview()[1], 1)
        with patch.object(page.table, 'yview_scroll') as scroll:
            page.horizontal.event_generate('<MouseWheel>', delta=-120)
            self.root.update()
        scroll.assert_called_once_with(3, 'units')
        page.table.xview_moveto(1)
        self.assertGreater(page.table.xview()[0], 0)
        self.assertTrue(page.footer.winfo_viewable())
        self.editor.window.geometry('920x720')
        self.root.update()
        self.assertFalse(page.horizontal.winfo_ismapped())
        self.assertEqual(page.table.xview(), (0.0, 1.0))

    def test_full_timestamp_and_long_id_have_lightweight_hover(self):
        key = self.groups[0]['key']
        stamp = 1893758400
        with patch('group_settings.last_successful_reads', return_value={key: stamp}):
            page = self.open_groups()
        page._show_tooltip(key, '#4', 100, 100)
        self.root.update()
        label = page._tooltip.winfo_children()[0]
        self.assertEqual(label.cget('text'), time_text(stamp, full=True))
        page._hide_tooltip()
        page.rows[key]['group_id'] = 'synthetic-' + 'x' * 120 + '@chatroom'
        page._show_tooltip(key, '#3', 100, 100)
        self.assertEqual(page._tooltip.winfo_children()[0].cget('text'), page.rows[key]['group_id'])
        self.assertIsNone(self.root.grab_current())
        page._hide_tooltip()
        self.assertIsNone(page._tooltip)

    def test_open_page_periodically_refreshes_local_times_without_moving_selection_or_scroll(self):
        self.add_many()
        times = {}
        with patch('group_settings.REFRESH_MS', 25), patch('group_settings.last_successful_reads', side_effect=lambda *args: dict(times)) as metadata:
            with patch('agent_sources.LocalSource.read', side_effect=AssertionError('no client reads')):
                page = self.open_groups()
                page.table.yview_moveto(.4)
                key = page.table.get_children()[40]
                page.table.selection_set(key)
                page.table.focus(key)
                position = page.table.yview()
                before = self.config.path.read_bytes()
                times[key] = 1893758400
                self.wait(85)
                self.assertGreaterEqual(metadata.call_count, 3)
                self.assertEqual(page.table.item(key, 'values')[-1], time_text(times[key]))
                self.assertEqual(page.table.yview(), position)
                self.assertEqual(page.table.selection(), (key,))
                self.assertEqual(page.table.focus(), key)
                self.assertEqual(self.config.path.read_bytes(), before)
                self.assertFalse((self.project / 'state/processing.sqlite3').exists())
                self.editor.switch_tab('rules')
                calls = metadata.call_count
                self.wait(65)
                self.assertEqual(metadata.call_count, calls)

    def test_reenter_refreshes_agent_changes_without_rebuilding_table(self):
        page = self.open_groups()
        table = page.table
        self.editor.switch_tab('rules')
        self.config.apply_groups({'confirmed': True, 'expected_revision': 2,
                                  'groups': [{**self.groups[0], 'name': 'Agent 更新的群名'}]})
        self.editor.switch_tab('groups')
        self.root.update()
        self.assertIs(page.table, table)
        self.assertIn('Agent 更新的群名', page.table.item(self.groups[0]['key'], 'values'))

    def test_refresh_removal_and_empty_table_preserve_surviving_selection(self):
        page = self.open_groups()
        keep, remove = (group['key'] for group in self.groups)
        page.table.selection_set(keep)
        page.table.focus(keep)
        self.config.remove_groups({'confirmed': True, 'expected_revision': 2, 'group_keys': [remove]})
        page.refresh()
        self.assertEqual(page.table.selection(), (keep,))
        self.config.remove_groups({'confirmed': True, 'expected_revision': 3, 'group_keys': [keep]})
        page.refresh()
        self.root.update()
        self.assertEqual(page.table.get_children(), ())
        self.assertTrue(page.empty.winfo_viewable())
        with patch.object(page.table, 'yview_scroll') as scroll:
            page.empty.event_generate('<MouseWheel>', delta=-120)
            self.root.update()
        scroll.assert_called_once_with(3, 'units')

    def test_private_wheel_tag_does_not_intercept_another_window(self):
        import tkinter as tk
        self.add_many()
        page = self.open_groups()
        other = tk.Toplevel(self.root)
        other.geometry('250x150+1050+70')
        label = tk.Label(other, text='合成的其他窗口')
        label.pack(fill='both', expand=True)
        received = []
        label.bind('<MouseWheel>', lambda event: received.append(event.delta))
        self.root.update()
        try:
            self.assertNotIn(page._wheel_tag, label.bindtags())
            with patch.object(page.table, 'yview_scroll') as scroll:
                label.event_generate('<MouseWheel>', delta=-120)
                self.root.update()
            scroll.assert_not_called()
            self.assertEqual(received, [-120])
        finally:
            other.destroy()

    def test_close_cancels_timers_tooltip_and_private_bindings_without_global_leaks(self):
        all_before = self.root.bind_all('<MouseWheel>')
        page = self.open_groups()
        tag = page._wheel_tag
        poll = page._poll_after
        key = self.groups[0]['key']
        page._show_tooltip(key, '#4', 100, 100)
        tip = page._tooltip
        page._hover_after = page.after(450, lambda: page._show_tooltip(key, '#4', 100, 100))
        hover = page._hover_after
        self.assertTrue(self.editor.close())
        pending = self.root.tk.call('after', 'info')
        self.assertNotIn(poll, pending)
        self.assertNotIn(hover, pending)
        self.assertFalse(tip.winfo_exists())
        self.assertEqual(self.root.tk.call('bind', tag, '<MouseWheel>'), '')
        self.assertEqual(self.root.bind_all('<MouseWheel>'), all_before)
        page.dispose()  # idempotent even after Tcl widgets have gone


if __name__ == '__main__':
    unittest.main()
