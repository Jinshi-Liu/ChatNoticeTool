"""Small, on-demand Windows desktop todo window. No chat reads or network.
Close the window to exit; no tray, auto-start, service, or scheduled collection.
"""
from __future__ import annotations
import ctypes
from datetime import datetime
from pathlib import Path
import re
import sqlite3
import sys
import tkinter as tk
from tkinter import messagebox

from todo_store import TodoStore
from widget_ui import TaskCanvas, SegmentedControl, BG, MUTED, ACCENT, RED, FONT, CARD
from glass_style import IconButton, set_topmost, bind_topmost
from todo_detail import DetailWindow
from rules_editor import RulesEditor
from project_core import WIDGET_MUTEX

TITLE = '群消息待办'
MUTEX = WIDGET_MUTEX
MAIN_MIN_WIDTH = 390


def deadline_badge(task: dict, now: datetime | None = None) -> tuple[str,str]:
    now = now or datetime.now()
    if task['done']: return '已完成', MUTED
    due = task.get('due')
    label = task.get('due_label', '')
    if not due: return label or '未设截止', MUTED
    when = datetime.fromisoformat(due)
    days = (when.date() - now.date()).days
    overdue = when < now if len(due) > 10 else days < 0
    if overdue: return (label or when.strftime('%m/%d')) + ' · 已过截止', RED
    base = label or when.strftime('%m/%d' + (' %H:%M' if len(due)>10 else ''))
    if days == 0: return base + ' · 今天', RED
    if days == 1: return base + ' · 明天', RED
    return base, ACCENT if days <= 7 else MUTED


class TodoWidget:
    def __init__(self, root: tk.Tk, store: TodoStore, *, rules_path: Path | None = None):
        self.root, self.store = root, store
        self.rules_path = Path(rules_path) if rules_path is not None else Path(__file__).resolve().parent/'AGENTS.md'
        self.rules_window = None
        self.poll_error = None
        self.view_done = False
        self.signature = None
        self.poll_id = None
        self.closed = False
        self.checkbuttons = {}
        self.checkvars = {}
        self.summary_state = None
        self.details_windows = {}
        self.compact=False
        root.title(TITLE)
        root.configure(bg=BG)
        root.minsize(MAIN_MIN_WIDTH, 160)
        width, height = 500, min(700, root.winfo_screenheight()-110)
        x, y = max(0,root.winfo_screenwidth()-width-35), 45
        saved = store.setting('geometry')
        if isinstance(saved,str):
            match = re.fullmatch(r'(\d+)x(\d+)\+(\d+)\+(\d+)',saved)
            if match:
                sw,sh,sx,sy = map(int,match.groups())
                if 220 <= sw <= root.winfo_screenwidth() and 160 <= sh <= root.winfo_screenheight() and sx < root.winfo_screenwidth()-80 and sy < root.winfo_screenheight()-80:
                    width,height,x,y = sw,sh,sx,sy
        width=max(MAIN_MIN_WIDTH,width)
        x=min(x,max(0,root.winfo_screenwidth()-width-12))
        root.geometry(f'{width}x{height}+{x}+{y}')
        self.pinned = bool(store.setting('pinned', True))
        set_topmost(root, self.pinned)
        bind_topmost(root, lambda: self.pinned)
        icon = Path(__file__).resolve().parent/'todo.ico'
        if icon.is_file():
            try: root.iconbitmap(default=str(icon)); root.iconbitmap(str(icon))
            except tk.TclError: pass
        root.protocol('WM_DELETE_WINDOW',self.close)
        root.bind('<Control-n>',lambda event:self.editor())

        header = tk.Frame(root,bg=BG); self.header=header
        header.pack(fill='x',padx=24,pady=(18,4))
        self.segment = SegmentedControl(header,self.switch)
        header.columnconfigure(0,weight=1)
        self.toolbar = tk.Frame(header,bg=BG)
        self.document_button=IconButton(self.toolbar,'document',self.open_rules)
        self.pin_button=IconButton(self.toolbar,'pin',self.toggle_pin)
        self.add_button=IconButton(self.toolbar,'plus',self.editor)
        for column,button in enumerate((self.document_button,self.pin_button,self.add_button)):
            button.grid(row=0,column=column,padx=(0,4) if column<2 else 0)
        self.update_pin()

        self.completed_actions = tk.Frame(root,bg=BG)
        self.clear_completed_button = tk.Button(self.completed_actions,text='清空已完成',
            command=self.clear_completed,font=(FONT,-16),fg=RED,bg=BG,
            activeforeground=RED,activebackground=CARD,disabledforeground=MUTED,
            relief='flat',bd=0,highlightthickness=0,padx=8,pady=4,cursor='hand2')
        self.clear_completed_button.pack(side='right')

        body = tk.Frame(root,bg=BG); self.body=body
        body.pack(fill='both',expand=True,padx=(18,10))
        self.canvas = TaskCanvas(body,on_toggle=self.toggle_id,on_detail=self.detail_id,
                                 on_menu=self.menu_id,badge=deadline_badge)
        self.scrollbar = tk.Scrollbar(body,orient='vertical',command=self.canvas.scroll_command,
            width=8,bd=0,highlightthickness=0,relief='flat',troughcolor=BG,
            bg='#D1D1D6',activebackground='#B9B9BD')
        self.scrollbar.pack(side='right',fill='y')
        self.canvas.pack(side='left',fill='both',expand=True)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.bind('<MouseWheel>',self.wheel)

        self.render()
        self.poll_id = root.after(1800,self.poll)
        root.bind('<Configure>',self.resize_layout,add='+')
        self.layout_header(width,height)

    def resize_layout(self,event):
        if event.widget!=self.root or event.width<220 or event.height<160: return
        self.layout_header(event.width,event.height)

    def layout_header(self,width=None,height=None):
        width = width or self.root.winfo_width()
        height = height or self.root.winfo_height()
        if width<220 or height<160: return
        compact=width<420 or height<300
        self.compact=compact
        margin=8 if compact else 24
        if self.completed_actions.winfo_manager():
            self.completed_actions.pack_configure(padx=margin)
        size=48
        self.segment.set_compact(compact)
        layout=(compact,int(self.segment.cget('width')))
        if layout!=getattr(self,'header_layout',None):
            self.header_layout=layout
            for column in range(3):
                self.toolbar.columnconfigure(column,minsize=size+2+(4 if column<2 else 0))
            self.header.pack_configure(padx=margin,pady=(6,0) if compact else (18,4))
            self.body.pack_configure(padx=(6,4) if compact else (18,10))
            # The flexible tab column yields space before the fixed toolbar.
            self.segment.grid(row=0,column=0,sticky='ew')
            self.toolbar.grid(row=0,column=1,sticky='ne',padx=(8,0),
                              pady=(max(0,28-(size+2)//2),0))
        self.canvas.set_compact(compact)

    def open_rules(self):
        if self.rules_window is not None and not self.rules_window.closed:
            self.rules_window.focus()
            return self.rules_window
        self.rules_window=RulesEditor(self.root,self.rules_path)
        polish_native_frame(self.rules_window.window)
        self.rules_window.focus()
        return self.rules_window

    def update_pin(self):
        self.pin_button.set_active(self.pinned)

    def toggle_pin(self):
        self.pinned = not self.pinned
        set_topmost(self.root,self.pinned)
        self.update_pin()
        self.safe(lambda:self.store.set_setting('pinned',self.pinned))

    def safe(self,action):
        try: return action()
        except (OSError,ValueError,sqlite3.Error) as exc:
            messagebox.showerror('待办未保存',str(exc),parent=self.root)
            return None

    def switch(self, done):
        self.view_done = done
        self.render(reset_scroll=True)

    def wheel(self,event):
        return self.canvas.wheel(event)

    def toggle_id(self,id,variable):
        self.toggle_done(self.store.get(id),variable)

    def detail_id(self,id):
        self.details(self.store.get(id))

    def menu_id(self,id,event):
        self.context_menu(self.store.get(id),event)

    def file_signature(self):
        stat = self.store.path.stat()
        return stat.st_mtime_ns, stat.st_size, datetime.now().strftime('%Y%m%d%H%M')

    def render(self, reset_scroll=False):
        observed=self.file_signature()  # Capture before query, so concurrent updates aren't swallowed.
        tasks = self.store.tasks()
        pending = sum(not t['done'] for t in tasks)
        completed = len(tasks)-pending
        summary=(datetime.now().strftime('%m月%d日'),pending,completed,self.view_done)
        if summary!=self.summary_state:
            self.segment.update_state(pending,completed,self.view_done)
            self.summary_state=summary
            self.layout_header()
        self.clear_completed_button.configure(state='normal' if completed else 'disabled')
        if self.view_done:
            if not self.completed_actions.winfo_manager():
                self.completed_actions.pack(fill='x',padx=8 if self.compact else 24,
                                            pady=(0,6),before=self.body)
        else:
            self.completed_actions.pack_forget()
        visible = [t for t in tasks if bool(t['done']) == self.view_done]
        self.canvas.sync(visible,empty_text='当前没有待办' if not self.view_done else '完成的事项会放在这里',
                         reset_scroll=reset_scroll)
        self.checkbuttons=self.canvas.checkbuttons
        self.checkvars=self.canvas.checkvars
        self.signature = observed
        for detail in list(self.details_windows.values()): detail.refresh_clean()

    def toggle_done(self,task,variable):
        done=variable.get()
        try:
            self.store.mark(task['id'],done)
        except (OSError,ValueError,sqlite3.Error) as exc:
            variable.set(bool(task['done']))
            messagebox.showerror('未保存',str(exc),parent=self.root)
            return
        self.render()

    def menu(self,task,button):
        self.context_menu(task,None,button.winfo_rootx(),button.winfo_rooty()+button.winfo_height())

    def context_menu(self,task,event=None,x=None,y=None):
        # The lower-right trash action goes directly to removal confirmation.
        self.remove(task)

    def remove(self,task):
        if not messagebox.askyesno('移除待办','移除此项？\n不影响群消息或其他待办。',parent=self.root): return
        try:
            detail=self.details_windows.get(task['id'])
            if detail and not detail.prepare_close(): return
            self.store.remove(task['id'])
            if detail: detail.close(force=True)
            self.render()
        except (OSError,ValueError,sqlite3.Error) as exc:
            messagebox.showerror('未保存',str(exc),parent=self.root)

    def details(self,task):
        return self.editor(task)

    def clear_completed(self):
        if not self.view_done: return
        try:
            tasks = self.store.tasks(done=True)
            if not tasks:
                self.render()
                return
            if not messagebox.askyesno('清空已完成',
                    f'移除这 {len(tasks)} 项已完成待办？\n不影响未完成事项或群消息。',
                    parent=self.root): return
            for task in tasks:
                detail = self.details_windows.get(task['id'])
                if detail and not detail.prepare_close(): return
            removed = self.store.remove_completed([task['id'] for task in tasks])
            for id in removed:
                detail = self.details_windows.get(id)
                if detail: detail.close(force=True)
            self.render()
        except (OSError,ValueError,sqlite3.Error) as exc:
            messagebox.showerror('未保存',str(exc),parent=self.root)

    def editor(self,task=None):
        if task:
            task=self.store.get(task['id'])
            existing=self.details_windows.get(task['id'])
            if existing and not existing.closed:
                if self.root.state()!='withdrawn':
                    existing.window.deiconify(); existing.window.lift(); existing.window.focus_set()
                return existing
        detail=DetailWindow(self,task)
        self.details_windows[detail.key]=detail
        polish_native_frame(detail.window)
        if self.root.state()!='withdrawn':
            detail.window.deiconify()
            polish_native_frame(detail.window)
        return detail

    def poll(self):
        if self.closed: return
        try:
            if not self.store.path.exists():
                # Do not recreate deleted project data behind a cleanup action.
                messagebox.showinfo('待办库已移除','待办数据已被清理，组件将退出。',parent=self.root)
                self.close(save_geometry=False); return
            if self.signature!=self.file_signature(): self.render()
            self.poll_error=None
        except (OSError,ValueError,sqlite3.Error) as exc:
            error=str(exc)
            if error!=self.poll_error:
                self.poll_error=error
                messagebox.showerror('读取待办失败',error,parent=self.root)
        self.poll_id=self.root.after(1800,self.poll)

    def close(self,save_geometry=True):
        if self.closed: return
        if self.rules_window is not None and not self.rules_window.prepare_close(): return
        editors=list(self.details_windows.values())
        if save_geometry:
            for detail in editors:
                if not detail.prepare_close(): return
        if self.rules_window is not None: self.rules_window.close(force=True)
        for detail in editors: detail.close(force=True,save_geometry=save_geometry)
        if save_geometry and self.store.path.exists():
            try:
                self.store.set_setting('geometry',self.root.geometry())
            except (OSError,ValueError,sqlite3.Error) as exc:
                # Completion was already saved on each click; geometry failure is harmless.
                pass
        self.closed=True
        if self.poll_id:
            try: self.root.after_cancel(self.poll_id)
            except tk.TclError: pass
        self.root.destroy()


def polish_native_frame(root):
    """Style only this app's HWND; never change global settings or client windows."""
    if sys.platform!='win32': return
    from ctypes import wintypes as W
    try:
        root.update_idletasks()
        user=ctypes.WinDLL('user32')
        user.GetParent.argtypes=[W.HWND]; user.GetParent.restype=W.HWND
        hwnd=user.GetParent(root.winfo_id()) or root.winfo_id()
        dwm=ctypes.WinDLL('dwmapi')
        dwm.DwmSetWindowAttribute.argtypes=[W.HWND,W.DWORD,W.LPCVOID,W.DWORD]
        for attribute,value in ((33,2),(35,0xF5F5F5),(36,0x282525)):
            data=W.DWORD(value)
            dwm.DwmSetWindowAttribute(hwnd,attribute,ctypes.byref(data),ctypes.sizeof(data))
    except (OSError,AttributeError,tk.TclError): pass  # Older Windows: keep native frame.


def single_instance():
    if sys.platform!='win32': return None
    from ctypes import wintypes as W
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateMutexW.argtypes=[W.LPVOID,W.BOOL,W.LPCWSTR]
    kernel.CreateMutexW.restype=W.HANDLE
    handle=kernel.CreateMutexW(None,False,MUTEX)
    error=ctypes.get_last_error()
    if not handle: raise ctypes.WinError(error)
    if error==183:
        user=ctypes.WinDLL('user32',use_last_error=True)
        user.FindWindowW.argtypes=[W.LPCWSTR,W.LPCWSTR]; user.FindWindowW.restype=W.HWND
        user.ShowWindow.argtypes=[W.HWND,ctypes.c_int]
        user.SetForegroundWindow.argtypes=[W.HWND]
        window=user.FindWindowW(None,TITLE)
        if window: user.ShowWindow(window,9); user.SetForegroundWindow(window)
        kernel.CloseHandle.argtypes=[W.HANDLE]; kernel.CloseHandle(handle)
        return False
    return handle


def main():
    handle=single_instance()
    if handle is False: return
    root=None
    try:
        if sys.platform=='win32':
            try: ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except (AttributeError,OSError): pass
        root=tk.Tk(); root.withdraw()
        store=TodoStore()
        app=TodoWidget(root,store,rules_path=Path(__file__).resolve().parent/'AGENTS.md')
        root.deiconify()
        polish_native_frame(root)
        root.mainloop()
    except Exception as exc:
        messagebox.showerror('组件无法打开',f'{type(exc).__name__}: {exc}',parent=root)
        if root:
            try: root.destroy()
            except tk.TclError: pass
    finally:
        if handle:
            from ctypes import wintypes as W
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            kernel.CloseHandle.argtypes=[W.HANDLE]; kernel.CloseHandle(handle)


if __name__=='__main__': main()
