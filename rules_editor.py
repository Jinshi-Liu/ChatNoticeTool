"""Small, modeless UTF-8 rules-file editor. Reads only when opened."""
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

from glass_style import BG, CARD, INK, ACCENT, RED, FONT, IconButton


class RulesEditor:
    def __init__(self, parent, path: Path):
        self.path = Path(path)
        self.closed = False
        self.baseline = None  # None means the file did not exist, not an empty file.
        self.loaded = False
        self.saved_text = ''
        self.window = tk.Toplevel(parent)
        win = self.window
        win.withdraw()
        win.title('规则 · AGENTS.md')
        win.configure(bg=BG)
        win.minsize(220, 160)
        win.geometry('560x440')
        # No transient or grab: the todo window stays independently operable.
        toolbar = tk.Frame(win, bg=BG)
        toolbar.pack(fill='x', padx=8, pady=4)
        self.save_button = IconButton(toolbar, 'save', self.save, size=32)
        self.save_button.pack(side='right')
        self.status = tk.Label(win, text='', bg=BG, fg=RED, font=(FONT, -14),
                               anchor='w', justify='left', wraplength=500)
        content = tk.Frame(win, bg=CARD)
        content.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        self.text = tk.Text(content, wrap='word', undo=True, autoseparators=True,
                            font=(FONT, -18), bg=CARD, fg=INK, insertbackground=ACCENT,
                            bd=0, highlightthickness=0, padx=8, pady=8)
        self.scrollbar = tk.Scrollbar(content, command=self.text.yview)
        self.scrollbar.pack(side='right', fill='y')
        self.text.pack(side='left', fill='both', expand=True)
        self.text.configure(yscrollcommand=self.scrollbar.set)
        try:
            self.baseline = self.read_disk()
            self.saved_text = '' if self.baseline is None else self.baseline.decode('utf-8')
            self.loaded = True
        except (OSError, UnicodeError) as exc:
            self.show_error('读取失败：' + str(exc))
        self.text.insert('1.0', self.saved_text)
        self.text.edit_reset()
        self.text.edit_modified(False)
        self.text.bind('<<Modified>>', self.modified)
        win.bind('<Control-s>', lambda event: (self.save(), 'break')[-1])
        win.bind('<Configure>', self.resize, add='+')
        win.protocol('WM_DELETE_WINDOW', self.close)

    def read_disk(self):
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def value(self):
        return self.text.get('1.0', 'end-1c')

    @property
    def dirty(self):
        return self.value() != self.saved_text

    def modified(self, event=None):
        if self.text.edit_modified():
            self.text.edit_modified(False)
            self.window.title(('＊' if self.dirty else '') + '规则 · AGENTS.md')

    def resize(self, event):
        if event.widget == self.window:
            self.status.configure(wraplength=max(80, event.width - 16))

    def focus(self):
        self.window.deiconify()
        self.window.lift()
        self.text.focus_set()

    def show_error(self, text):
        self.status.configure(text=text)
        self.status.pack(before=self.text.master, fill='x', padx=8, pady=(0, 4))

    def save(self):
        if self.closed:
            return False
        if not self.loaded:
            self.show_error('原文件未能读取，无法安全保存。请保留草稿并重新打开。')
            return False
        text = self.value()
        try:
            current = self.read_disk()
            if current != self.baseline:
                self.show_error('文件已被外部修改；草稿仍保留。')
                if not messagebox.askyesno('规则文件已更改',
                        '文件在打开后已被外部修改。是否用当前编辑内容覆盖？', parent=self.window):
                    return False
            data = text.encode('utf-8')
            self.path.write_bytes(data)
        except (OSError, UnicodeError) as exc:
            self.show_error('保存失败：' + str(exc))
            return False
        self.baseline = data
        self.saved_text = text
        self.status.pack_forget()
        self.window.title('规则 · AGENTS.md')
        self.text.edit_separator()
        return True

    def prepare_close(self):
        if self.closed or not self.dirty:
            return True
        answer = messagebox.askyesnocancel('未保存的规则', '是否保存对 AGENTS.md 的修改？',
                                          parent=self.window)
        if answer is None:
            return False
        return self.save() if answer else True

    def close(self, force=False):
        if self.closed:
            return True
        if not force and not self.prepare_close():
            return False
        self.closed = True
        self.window.destroy()
        return True
