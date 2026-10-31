"""Modeless rules editor with a read-only configured-group overview."""
from pathlib import Path
import tkinter as tk
from tkinter import messagebox

from glass_style import BG, CARD, INK, MUTED, ACCENT, RED, FONT, IconButton
from widget_ui import SegmentedControl


class RulesEditor:
    def __init__(self, parent, path: Path, *, config=None, title='规则与群聊'):
        self.path = Path(path)
        self.title = title
        self.group_config = config
        self.groups_page = None
        self.active_tab = 'rules'
        self.closed = False
        self.baseline = None
        self.loaded = False
        self.saved_text = ''
        self.load_error = None
        self.window = tk.Toplevel(parent)
        win = self.window
        win.withdraw()
        win.title(self.title)
        win.configure(bg=BG)
        win.minsize(420, 320)
        width, height = min(920, win.winfo_screenwidth() - 60), min(720, win.winfo_screenheight() - 100)
        x = max(0, min(parent.winfo_x() - width - 20, win.winfo_screenwidth() - width))
        y = max(20, min(parent.winfo_y() + 20, win.winfo_screenheight() - height - 60))
        win.geometry(f'{width}x{height}+{x}+{y}')
        icon = Path(__file__).resolve().parent / 'todo.ico'
        if icon.is_file():
            try:
                win.iconbitmap(str(icon))
            except tk.TclError:
                pass
        toolbar = tk.Frame(win, bg=BG)
        self.header = toolbar
        toolbar.pack(fill='x', padx=24, pady=(18, 4))
        toolbar.columnconfigure(0, weight=1)
        self.segment = SegmentedControl(toolbar,
            lambda groups: self.switch_tab('groups' if groups else 'rules'),
            labels=('规则', '群聊'), show_counts=False)
        self.segment.grid(row=0, column=0, sticky='ew')
        self.save_button = IconButton(toolbar, 'save', self.save)
        self.save_button.grid(row=0, column=1, sticky='ne', padx=(8, 0), pady=(3, 0))
        self.status = tk.Label(win, text='', bg=BG, fg=RED, font=(FONT, -14),
                               anchor='w', justify='left', wraplength=800)
        self.content = tk.Frame(win, bg=BG)
        self.content.pack(fill='both', expand=True, padx=(18, 10), pady=(0, 22))
        self.rules_page = tk.Frame(self.content, bg=CARD)
        self.rules_page.pack(fill='both', expand=True)
        self.text = tk.Text(self.rules_page, wrap='word', undo=True, autoseparators=True,
                            font=(FONT, -30), bg=CARD, fg=INK, insertbackground=ACCENT,
                            bd=0, highlightthickness=0, padx=18, pady=18, spacing3=5)
        self.scrollbar = tk.Scrollbar(self.rules_page, command=self.text.yview, width=8, bd=0)
        self.scrollbar.pack(side='right', fill='y')
        self.text.pack(side='left', fill='both', expand=True)
        self.text.configure(yscrollcommand=self.scrollbar.set)
        try:
            self.baseline = self.read_disk()
            self.saved_text = '' if self.baseline is None else self.baseline.decode('utf-8')
            self.loaded = True
        except (OSError, UnicodeError) as exc:
            self.load_error = '读取规则失败：' + str(exc)
            self.show_error(self.load_error)
        self.text.insert('1.0', self.saved_text)
        self.text.edit_reset()
        self.text.edit_modified(False)
        self.text.bind('<<Modified>>', self.modified)
        win.bind('<Control-s>', lambda event: (self.save(), 'break')[-1])
        win.bind('<Configure>', self.resize, add='+')
        win.protocol('WM_DELETE_WINDOW', self.close)
        self.update_tabs()

    def read_disk(self):
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return None

    def value(self):
        return self.text.get('1.0', 'end-1c')

    @property
    def rules_dirty(self):
        return self.value() != self.saved_text

    @property
    def dirty(self):
        return self.rules_dirty

    def modified(self, event=None):
        if self.text.edit_modified():
            self.text.edit_modified(False)
            self.changed()

    def changed(self):
        self.window.title(('＊' if self.dirty else '') + self.title)
        self.save_button.set_active(self.rules_dirty if self.active_tab == 'rules' else False)

    def update_tabs(self):
        self.segment.update_state(0, 0, self.active_tab == 'groups')

    def switch_tab(self, tab):
        if tab not in ('rules', 'groups') or tab == self.active_tab:
            return
        if tab == 'groups' and self.groups_page is None:
            try:
                from agent_config import AgentConfig
                from group_settings import GroupSettings
                config = self.group_config if self.group_config is not None else AgentConfig(self.path.parent)
                self.groups_page = GroupSettings(self.content, config, changed=self.changed, show_error=self.show_error)
            except (OSError, ValueError) as exc:
                self.show_error('读取群聊配置失败：' + str(exc))
                return
        self.rules_page.pack_forget()
        if self.groups_page is not None:
            self.groups_page.pack_forget()
        self.active_tab = tab
        page = self.rules_page if tab == 'rules' else self.groups_page
        page.pack(fill='both', expand=True)
        self.status.pack_forget()
        if tab == 'groups':
            self.groups_page.refresh()
        if tab == 'rules' and self.load_error:
            self.show_error(self.load_error)
        if tab == 'rules':
            self.save_button.grid()
        else:
            self.save_button.grid_remove()
        self.update_tabs()
        self.changed()

    def resize(self, event):
        if event.widget == self.window:
            self.status.configure(wraplength=max(80, event.width - 44))
            compact = event.width < 420 or event.height < 300
            self.segment.set_compact(compact)
            self.header.pack_configure(padx=8 if compact else 24,
                                       pady=(6, 0) if compact else (18, 4))
            self.content.pack_configure(padx=(6, 4) if compact else (18, 10))

    def focus(self):
        self.window.deiconify()
        self.window.lift()
        if self.active_tab == 'rules':
            self.text.focus_set()
        else:
            self.groups_page.focus()

    def show_error(self, text):
        self.status.configure(text=text)
        self.status.pack(before=self.content, fill='x', padx=22, pady=(10, 0))

    def save_rules(self):
        if not self.loaded:
            self.show_error('原规则未能读取，请保留草稿并重新打开。')
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
        self.text.edit_separator()
        self.changed()
        return True

    def save(self):
        if self.closed:
            return False
        return self.save_rules() if self.active_tab == 'rules' else False

    def prepare_close(self):
        if self.closed or not self.dirty:
            return True
        answer = messagebox.askyesnocancel('未保存的修改', '是否保存规则的修改？', parent=self.window)
        if answer is None:
            return False
        if not answer:
            return True
        if self.rules_dirty:
            self.switch_tab('rules')
            if not self.save_rules():
                return False
        return True

    def close(self, force=False):
        if self.closed:
            return True
        if not force and not self.prepare_close():
            return False
        self.closed = True
        if self.groups_page is not None:
            self.groups_page.dispose()
        self.window.destroy()
        return True
