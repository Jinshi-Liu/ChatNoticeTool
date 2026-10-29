from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rules_editor import RulesEditor


@unittest.skipUnless(sys.platform == 'win32', 'Windows Tk editor tests')
class RulesEditorTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'AGENTS.md'
        self.root = tk.Tk()
        self.root.withdraw()
        self.editor = None

    def tearDown(self):
        if self.editor is not None:
            self.editor.close(force=True)
        self.root.destroy()
        self.tmp.cleanup()

    def open(self):
        self.editor = RulesEditor(self.root, self.path)
        self.editor.focus()
        self.root.update()
        return self.editor

    def replace(self, text):
        self.editor.text.delete('1.0', 'end')
        self.editor.text.insert('1.0', text)
        self.root.update()

    def test_missing_file_is_blank_and_not_created_until_save(self):
        editor = self.open()
        self.assertEqual(editor.value(), '')
        self.assertFalse(editor.dirty)
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.root.grab_current())
        self.assertEqual(str(editor.window.transient()), '')
        self.assertTrue(editor.close())
        self.assertFalse(self.path.exists())
        editor = self.open()
        self.assertTrue(editor.save())
        self.assertEqual(self.path.read_bytes(), b'')

    def test_edit_save_chinese_and_exact_empty_content(self):
        self.path.write_bytes('原规则\n第二行\n'.encode('utf-8'))
        editor = self.open()
        self.assertEqual(editor.value(), '原规则\n第二行\n')
        self.replace('仅处理QQ、微信和文件。\n保留中文，不加模板。')
        self.assertTrue(editor.dirty)
        self.assertEqual(editor.save_button.kind, 'save')
        self.assertTrue(editor.save_button.invoke())
        self.assertEqual(self.path.read_bytes(), editor.value().encode('utf-8'))
        self.assertFalse(editor.dirty)
        self.replace('')
        self.assertTrue(editor.save())
        self.assertEqual(self.path.read_bytes(), b'')
        self.assertTrue(editor.close())
        self.assertEqual(self.open().value(), '')

    def test_keyboard_save_and_standard_undo_redo(self):
        editor = self.open()
        editor.window.focus_force()
        editor.text.focus_force()
        self.root.update()
        editor.text.insert('1.0', '中文草稿')
        editor.text.edit_separator()
        self.root.update()
        editor.text.event_generate('<Control-z>')
        self.root.update()
        self.assertEqual(editor.value(), '')
        editor.text.event_generate('<<Redo>>')
        self.root.update()
        self.assertEqual(editor.value(), '中文草稿')
        editor.text.event_generate('<Control-s>')
        self.root.update()
        self.assertEqual(self.path.read_text(encoding='utf-8'), '中文草稿')
        self.assertFalse(editor.dirty)
        editor.text.edit_undo()
        self.root.update()
        self.assertTrue(editor.dirty)

    def test_close_cancel_discard_and_save(self):
        editor = self.open()
        self.replace('关闭前保存')
        with patch('rules_editor.messagebox.askyesnocancel', return_value=None):
            self.assertFalse(editor.close())
        self.assertTrue(editor.window.winfo_exists())
        self.assertEqual(editor.value(), '关闭前保存')
        with patch('rules_editor.messagebox.askyesnocancel', return_value=False):
            self.assertTrue(editor.close())
        self.assertFalse(self.path.exists())
        editor = self.open()
        self.replace('保存并关闭')
        with patch('rules_editor.messagebox.askyesnocancel', return_value=True):
            self.assertTrue(editor.close())
        self.assertEqual(self.path.read_text(encoding='utf-8'), '保存并关闭')

    def test_save_error_visible_and_draft_kept_even_on_close(self):
        editor = self.open()
        self.replace('不要丢失的草稿')
        with patch.object(Path, 'write_bytes', side_effect=PermissionError('合成写入错误')):
            self.assertFalse(editor.save())
            self.root.update()
            self.assertEqual(editor.status.winfo_manager(), 'pack')
            self.assertIn('合成写入错误', editor.status.cget('text'))
            with patch('rules_editor.messagebox.askyesnocancel', return_value=True):
                self.assertFalse(editor.close())
        self.assertEqual(editor.value(), '不要丢失的草稿')
        self.assertTrue(editor.dirty)
        self.assertFalse(editor.closed)
        self.assertTrue(editor.save())
        self.assertEqual(editor.status.winfo_manager(), '')

    def test_external_change_requires_explicit_overwrite(self):
        self.path.write_text('基线', encoding='utf-8')
        editor = self.open()
        self.replace('本地草稿')
        self.path.write_text('外部修改', encoding='utf-8')
        with patch('rules_editor.messagebox.askyesno', return_value=False) as prompt:
            self.assertFalse(editor.save())
            prompt.assert_called_once()
        self.assertEqual(editor.value(), '本地草稿')
        self.assertEqual(self.path.read_text(encoding='utf-8'), '外部修改')
        self.assertTrue(editor.dirty)
        self.assertEqual(editor.status.winfo_manager(), 'pack')
        with patch('rules_editor.messagebox.askyesno', return_value=True):
            self.assertTrue(editor.save())
        self.assertEqual(self.path.read_text(encoding='utf-8'), '本地草稿')
        with patch('rules_editor.messagebox.askyesno') as prompt:
            self.assertTrue(editor.save())
            prompt.assert_not_called()

    def test_external_creation_and_deletion_are_conflicts(self):
        editor = self.open()
        self.replace('本地')
        self.path.write_text('外部新建', encoding='utf-8')
        with patch('rules_editor.messagebox.askyesno', return_value=False):
            self.assertFalse(editor.save())
        editor.close(force=True)
        editor = self.open()
        self.replace('第二份本地')
        self.path.unlink()
        with patch('rules_editor.messagebox.askyesno', return_value=False):
            self.assertFalse(editor.save())
        self.assertFalse(self.path.exists())
        self.assertEqual(editor.value(), '第二份本地')

    def test_read_error_is_visible_and_cannot_overwrite_unknown_file(self):
        self.path.write_bytes(b'\xff')
        editor = self.open()
        self.assertFalse(editor.loaded)
        self.assertEqual(editor.status.winfo_manager(), 'pack')
        self.replace('仍可保留草稿')
        self.assertFalse(editor.save())
        self.assertEqual(self.path.read_bytes(), b'\xff')
        self.assertEqual(editor.value(), '仍可保留草稿')

    def test_preserves_line_endings_and_no_extra_newline(self):
        self.path.write_bytes('第一行\r\n第二行'.encode('utf-8'))
        editor = self.open()
        self.assertEqual(editor.value(), '第一行\r\n第二行')
        self.assertTrue(editor.save())
        self.assertEqual(self.path.read_bytes(), '第一行\r\n第二行'.encode('utf-8'))


if __name__ == '__main__':
    unittest.main()
