"""One read-only group table; local metadata only, one scrolling owner."""
from datetime import datetime
import math
import sqlite3
import tkinter as tk
from tkinter import ttk

from agent_processing import last_successful_reads
from project_core import LOCAL_TZ
from glass_style import BG, CARD, INK, MUTED, FONT

PLATFORM_NAMES = {'qq': 'QQ', 'wechat': '微信', 'dingtalk': '钉钉', 'import': '文件'}
FOOTER = '修改此配置请与 Agent 沟通'
REFRESH_MS = 5000
COLUMNS = ('platform', 'name', 'group_id', 'last_read_at')


def time_text(value, *, full=False):
    if value is None:
        return '尚未读取'
    stamp = datetime.fromtimestamp(value, LOCAL_TZ)
    return stamp.isoformat(timespec='seconds') if full else stamp.strftime('%m-%d %H:%M')


def group_rows(config):
    """Only read configuration and successful-acquisition metadata, never clients."""
    value = config.load()
    times = last_successful_reads(config.root, value['groups'])
    rows = [{'key': key, 'platform': value['sources'][group['source_id']]['platform'],
             'name': group['name'], 'group_id': group['group_id'], 'last_read_at': times.get(key)}
            for key, group in value['groups'].items()]
    return sorted(rows, key=lambda row: (row['name'].casefold(), row['platform'], row['group_id'], row['key']))


def group_sections(config):
    """Compatibility metadata helper for callers; the UI never creates sections."""
    value = config.load()
    sections = dict.fromkeys(('qq', 'wechat', *(s['platform'] for s in value['sources'].values())))
    sections = {key: [] for key in sections}
    for row in group_rows(config):
        sections[row['platform']].append(row)
    return sections


class GroupSettings(tk.Frame):
    def __init__(self, parent, config, *, changed=None, show_error=None):
        super().__init__(parent, bg=BG)
        self.config, self.show_error = config, show_error
        self.rows = {}
        self._disposed = False
        self._poll_after = self._hover_after = None
        self._tooltip = None
        self._hover_cell = None
        self._wheel_units = 0.0
        self._layout_width = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.viewport = tk.Frame(self, bg=CARD)
        self.viewport.grid(row=0, column=0, sticky='nsew')
        self.viewport.columnconfigure(0, weight=1)
        self.viewport.rowconfigure(0, weight=1)
        self.style_name = f'GroupOverview{id(self)}.Treeview'
        style = ttk.Style(self)
        style.configure(self.style_name, font=(FONT, -30), rowheight=52, background=CARD,
                        fieldbackground=CARD, foreground=INK, borderwidth=0)
        style.configure(self.style_name + '.Heading', font=(FONT, -24), foreground=MUTED, padding=(6, 8))
        self.table = ttk.Treeview(self.viewport, columns=COLUMNS, show='headings', height=8,
                                  style=self.style_name, selectmode='browse')
        for key, label in zip(COLUMNS, ('平台', '群名', '群号 / 群 ID', '最后读取')):
            self.table.heading(key, text=label, anchor='w')
            self.table.column(key, minwidth=0, stretch=False, anchor='w')
        self.table.grid(row=0, column=0, sticky='nsew')
        self.vertical = tk.Scrollbar(self.viewport, command=self.table.yview, width=8, bd=0)
        self.vertical.grid(row=0, column=1, sticky='ns')
        self.horizontal = ttk.Scrollbar(self.viewport, orient='horizontal', command=self.table.xview)
        self.table.configure(yscrollcommand=self.vertical.set, xscrollcommand=self._xscroll)
        self.empty = tk.Label(self.table, text='暂无已配置群聊', font=(FONT, -30), fg=MUTED, bg=CARD)
        self.footer = tk.Label(self, text=FOOTER, font=(FONT, -24), wraplength=800, bg=BG, fg=MUTED, anchor='w')
        self.footer.grid(row=1, column=0, sticky='ew', pady=(14, 0))
        # A private, leading bindtag handles the event exactly once, before the
        # Treeview/Scrollbar class defaults. Never bind_all or bind a toplevel.
        self._wheel_tag = f'GroupWheel{id(self)}'
        self._wheel_bindings = {}
        for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>'):
            self._wheel_bindings[sequence] = self.bind_class(self._wheel_tag, sequence, self.wheel)
        for widget in (self, self.viewport, self.table, self.vertical, self.horizontal, self.empty, self.footer):
            widget.bindtags((self._wheel_tag, *widget.bindtags()))
        self.table.bind('<Configure>', self._resize)
        self.table.bind('<Motion>', self._motion)
        self.table.bind('<Leave>', self._hide_tooltip)
        self.table.bind('<ButtonPress>', self._hide_tooltip, add='+')
        self.bind('<Unmap>', self._hide_tooltip)
        self.bind('<Destroy>', self._destroyed, add='+')
        self.refresh()
        self._schedule_poll()

    @property
    def dirty(self):
        return False

    def _values(self, row):
        return (PLATFORM_NAMES.get(row['platform'], row['platform']), row['name'], row['group_id'],
                time_text(row['last_read_at']))

    def refresh(self):
        if self._disposed:
            return
        try:
            rows = group_rows(self.config)
        except (OSError, ValueError, sqlite3.Error) as exc:
            if self.show_error:
                self.show_error('读取群聊信息失败：' + str(exc))
            return
        old_order = self.table.get_children()
        position = self.table.yview()[0]
        anchor = old_order[min(len(old_order) - 1, round(position * len(old_order)))] if old_order else None
        selection, focus = self.table.selection(), self.table.focus()
        new_rows = {row['key']: row for row in rows}
        order = tuple(new_rows)
        for key in old_order:
            if key not in new_rows:
                self.table.delete(key)
        for index, row in enumerate(rows):
            key = row['key']
            if key not in self.rows or not self.table.exists(key):
                self.table.insert('', index, iid=key, values=self._values(row))
            elif self.rows[key] != row:
                self.table.item(key, values=self._values(row))
            if order != old_order:
                self.table.move(key, '', index)
        self.rows = new_rows
        # Timestamp-only refreshes do not move/recreate any item or viewport.
        if order != old_order:
            kept = [key for key in selection if key in new_rows]
            self.table.selection_set(kept)
            if focus in new_rows:
                self.table.focus(focus)
            self.table.yview_moveto(order.index(anchor) / len(order) if anchor in new_rows else position)
        if rows:
            self.empty.place_forget()
        else:
            self.empty.place(relx=.5, rely=.45, anchor='center')
        self._hide_tooltip()

    def _schedule_poll(self):
        if not self._disposed:
            self._poll_after = self.after(REFRESH_MS, self._poll)

    def _poll(self):
        self._poll_after = None
        if self._disposed:
            return
        try:
            if self.winfo_viewable():
                self.refresh()
        finally:
            self._schedule_poll()

    def _resize(self, event):
        self.footer.configure(wraplength=max(80, event.width - 12))
        available = max(1, event.width - 4)
        if available == self._layout_width:
            return
        self._layout_width = available
        # A regular window fits all columns without a horizontal scrollbar.
        # Compact windows retain usable minimums; full IDs are also hoverable.
        flexible = max(360, available - 300)
        name = int(flexible * .46)
        for key, width in zip(COLUMNS, (100, name, flexible - name, 200)):
            self.table.column(key, width=width)

    def _xscroll(self, first, last):
        self.horizontal.set(first, last)
        if float(first) > .001 or float(last) < .999:
            self.horizontal.grid(row=1, column=0, sticky='ew')
        else:
            self.horizontal.grid_remove()

    def wheel(self, event):
        self._hide_tooltip()
        button = getattr(event, 'num', None)
        units = -3 if button == 4 else 3 if button == 5 else -getattr(event, 'delta', 0) / 40
        if units * self._wheel_units < 0:
            self._wheel_units = 0.0
        self._wheel_units += units
        steps = math.trunc(self._wheel_units)
        self._wheel_units -= steps
        if steps:
            self.table.yview_scroll(steps, 'units')
        return 'break'

    def _motion(self, event):
        key, column = self.table.identify_row(event.y), self.table.identify_column(event.x)
        cell = (key, column)
        if cell == self._hover_cell:
            return
        self._hide_tooltip()
        if key in self.rows and column in ('#1', '#2', '#3', '#4'):
            self._hover_cell = cell
            self._hover_after = self.after(450, lambda: self._show_tooltip(key, column, event.x_root, event.y_root))

    def _show_tooltip(self, key, column, x, y):
        self._hover_after = None
        if self._disposed or key not in self.rows or not self.winfo_viewable():
            return
        row = self.rows[key]
        value = time_text(row['last_read_at'], full=True) if column == '#4' else str(row[COLUMNS[int(column[1:]) - 1]])
        tip = self._tooltip = tk.Toplevel(self)
        tip.withdraw()
        tip.overrideredirect(True)
        tk.Label(tip, text=value, font=(FONT, -24), bg=CARD, fg=INK, justify='left',
                 wraplength=520, padx=10, pady=7, relief='solid', borderwidth=1).pack()
        tip.update_idletasks()
        x = max(0, min(x + 12, tip.winfo_screenwidth() - tip.winfo_reqwidth() - 8))
        y = max(0, min(y + 18, tip.winfo_screenheight() - tip.winfo_reqheight() - 8))
        tip.geometry(f'+{x}+{y}')
        tip.deiconify()

    def _hide_tooltip(self, event=None):
        if self._hover_after is not None:
            self.after_cancel(self._hover_after)
            self._hover_after = None
        self._hover_cell = None
        if self._tooltip is not None:
            self._tooltip.destroy()
            self._tooltip = None

    def dispose(self):
        if self._disposed:
            return
        self._disposed = True
        self._hide_tooltip()
        if self._poll_after is not None:
            self.after_cancel(self._poll_after)
            self._poll_after = None
        for sequence, command in self._wheel_bindings.items():
            self.unbind_class(self._wheel_tag, sequence)
            self._root().deletecommand(command)
        self._wheel_bindings.clear()

    def _destroyed(self, event):
        if event.widget == self:
            self.dispose()

    def destroy(self):
        self.dispose()
        super().destroy()

    def focus(self):
        self.table.focus_set()
