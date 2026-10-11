"""Modeless, directly editable note windows. Debounced local saves, no grabs.
Each task has its own window. Per-field optimistic merge protects other writers.
"""
from __future__ import annotations
import sqlite3
import re
import tkinter as tk
from tkinter import messagebox
from pathlib import Path
from glass_style import BG,CARD,INK,MUTED,ACCENT,RED,FONT,IconButton
from todo_store import normalize_due


def deadline_text(due, label):
    if not due: return label or ''
    date=due[:10]
    # Fold a repeated date prefix into one readable date + qualifier.
    short=date[5:].replace('-','/')
    suffix=(label or '').strip()
    for prefix in (due.replace('T',' '),date,short):
        if suffix.startswith(prefix):
            rest=suffix[len(prefix):]
            if not rest or not rest[0].isdigit(): suffix=rest.strip(' ·'); break
    return due.replace('T',' ')+((' '+suffix) if suffix else '')


def parse_deadline_text(text):
    text=text.strip()
    if not text: return None,''
    if text in ('截止待确认','未设截止','待确认'): return None,text
    match=re.fullmatch(r'(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}:\d{2}))?(?:\s+(.+))?',text)
    if not match: raise ValueError('时间格式：2026-10-13，可加09:00或“上课前”')
    date,clock,qualifier=match.groups()
    due=normalize_due(date+('T'+clock if clock else ''))
    # Preserve the full date in the label so list badges remain self-contained.
    return due,(due.replace('T',' ')+' '+qualifier if qualifier else '')


class HintEntry(tk.Entry):
    """A real single-line entry. Hint is an overlay, never stored as user data."""
    def __init__(self,parent,*,textvariable,hint='',**kwargs):
        super().__init__(parent,textvariable=textvariable,**kwargs)
        self.value=textvariable
        self.hint=tk.Label(self,text=hint,font=kwargs.get('font'),bg=kwargs.get('bg',BG),
                           fg=MUTED,anchor='w',cursor='xterm',bd=0)
        self.hint.bind('<Button-1>',lambda e:self.focus_set())
        self.trace=self.value.trace_add('write',lambda *_:self.update_hint())
        self.update_hint()
        self.bind('<Destroy>',self.cleanup_hint,add='+')
    def update_hint(self):
        if not self.value.get() and self.hint.cget('text'):
            self.hint.place(x=2,rely=.5,anchor='w',relwidth=.98)
        else: self.hint.place_forget()
    def cleanup_hint(self,event):
        if event.widget==self: self.value.trace_remove('write',self.trace)


class DetailWindow:
    def __init__(self,app,task=None):
        self.app=app; self.store=app.store; self.task=dict(task) if task else None
        self.key=task['id'] if task else 'draft:'+str(id(self))
        self.pending=None; self.closed=False; self.loading=True; self.dirty=False
        self.window=tk.Toplevel(app.root)
        win=self.window
        win.withdraw()  # Build once before mapping to avoid a blank-window flash.
        win.title(task['title'] if task else '新待办')
        # Deliberately no transient() or grab_set(): main and sibling windows remain usable.
        win.configure(bg=BG); win.minsize(490,430)
        offset=len(app.details_windows)%6*28
        x=max(10,min(app.root.winfo_x()-570+offset,win.winfo_screenwidth()-590))
        y=max(10,min(app.root.winfo_y()+40+offset,win.winfo_screenheight()-640))
        win.geometry(f'560x590+{x}+{y}')
        win.attributes('-topmost',app.pinned)
        icon=Path(__file__).resolve().parent/'todo.ico'
        if icon.exists(): win.iconbitmap(str(icon))
        top=tk.Frame(win,bg=BG); top.pack(fill='x',padx=26,pady=(24,14))
        self.complete_button=IconButton(top,'save' if task is None else ('undo' if task['done'] else 'check'),self.primary_action)
        self.complete_button.pack(side='right',anchor='n',padx=(14,0))
        self.title_var=tk.StringVar(value=task['title'] if task else '')
        self.title=HintEntry(top,textvariable=self.title_var,hint='输入标题',width=1,font=(FONT,-38,'bold'),bg=BG,fg=INK,
            insertbackground=ACCENT,bd=0,highlightthickness=0)
        self.title.pack(side='left',fill='x',expand=True,ipady=6)
        date_row=tk.Frame(win,bg=BG); date_row.pack(fill='x',padx=30,pady=(0,18))
        self.deadline_original=(task['due'],task['due_label']) if task else (None,'')
        self.deadline_original_text=deadline_text(*self.deadline_original)
        self.due_var=tk.StringVar(value=self.deadline_original_text)
        self.due=HintEntry(date_row,textvariable=self.due_var,hint='截止时间：YYYY-MM-DD HH:MM（选填）',
            font=(FONT,-27),bg=BG,fg=ACCENT,insertbackground=ACCENT,bd=0,highlightthickness=0,width=22)
        self.due.pack(side='left',fill='x',expand=True)
        self.optional=tk.BooleanVar(value=bool(task['optional']) if task else False)  # metadata preserved, no extra UI
        content=tk.Frame(win,bg=CARD); content.pack(fill='both',expand=True,padx=20,pady=(0,8))
        self.note=tk.Text(content,font=(FONT,-30),wrap='word',bg=CARD,fg=INK,insertbackground=ACCENT,
            bd=0,highlightthickness=0,padx=16,pady=16,spacing1=3,spacing3=7,undo=True)
        scrollbar=tk.Scrollbar(content,command=self.note.yview,width=8,bd=0,highlightthickness=0)
        scrollbar.pack(side='right',fill='y'); self.note.pack(fill='both',expand=True)
        self.note.configure(yscrollcommand=scrollbar.set)
        self.status=tk.Label(win,text='',font=(FONT,-13),bg=BG,fg=RED,
                             anchor='w',justify='left',wraplength=490)
        # Only validation/conflict errors are shown. No instructional/source footer.
        self.note.insert('1.0',task['note'] if task else '')
        self.baseline=self.values()
        self.update_action()
        self.note.edit_modified(False)
        self.note.bind('<<Modified>>',self.modified)
        self.title_var.trace_add('write',lambda *_:self.changed())
        self.due_var.trace_add('write',lambda *_:self.changed())
        for text in (self.title,self.note):
            text.bind('<Tab>',lambda e:(e.widget.tk_focusNext().focus_set(),'break')[-1])
        win.bind('<Control-s>',lambda e:self.save())
        win.bind('<Control-Return>',lambda e:self.save())
        win.bind('<Escape>',lambda e:self.close())
        win.protocol('WM_DELETE_WINDOW',self.close)
        win.bind('<FocusIn>',lambda e:self.refresh_clean(),add='+')
        self.loading=False
        if not task: self.title.focus_set()

    def show_error(self,text):
        self.status.configure(text=text,fg=RED)
        self.status.pack(fill='x',padx=30,pady=(0,12))

    def primary_action(self):
        return self.save() if self.task is None else self.toggle_complete()

    def update_action(self):
        if self.task is None:
            self.complete_button.set_kind('save'); self.complete_button.set_active(False)
        else:
            latest=self.store.get(self.task['id'])
            self.complete_button.set_kind('undo' if latest['done'] else 'check')
            self.complete_button.set_active(bool(latest['done']))

    def values(self):
        due,label=(self.deadline_original if self.due_var.get().strip()==self.deadline_original_text
                   else parse_deadline_text(self.due_var.get()))
        return {'title':self.title.get().strip(),
                'due':due,'due_label':label,
                'note':self.note.get('1.0','end-1c'),'optional':int(self.optional.get())}

    def modified(self,event):
        if event.widget.edit_modified():
            event.widget.edit_modified(False); self.changed()

    def changed(self):
        if self.loading or self.closed: return
        self.dirty=True
        self.status.pack_forget()
        if self.pending: self.window.after_cancel(self.pending)
        self.pending=None
        if self.task is not None: self.pending=self.window.after(1000,self.save)
        # New tasks are drafts until explicitly saved; the save icon stays a save icon.

    def save(self):
        if self.closed: return True
        if self.pending: self.window.after_cancel(self.pending); self.pending=None
        try:
            values=self.values()
            if not values['title']: raise ValueError('请填写标题，草稿尚未保存')
            changes={key:value for key,value in values.items() if value!=self.baseline[key]}
            if not self.task:
                id=self.store.upsert(values['title'],due=values['due'],due_label=values['due_label'],
                    note=values['note'],optional=bool(values['optional']))
                self.task=self.store.get(id)
                self.app.details_windows.pop(self.key,None)
                self.key=id; self.app.details_windows[id]=self
            elif changes:
                self.task=self.store.patch_fields(self.task['id'],changes,self.baseline)
            # Baseline tracks visible fields, not unseen concurrent edits.
            self.baseline=values; self.dirty=False
            self.deadline_original=(values['due'],values['due_label'])
            self.deadline_original_text=self.due_var.get().strip()
            self.window.title(values['title']); self.status.pack_forget()
            self.update_action()
            self.app.render()
            return True
        except (ValueError,OSError,sqlite3.Error) as exc:
            self.show_error(str(exc)); return False

    def refresh_clean(self):
        if self.loading or self.closed or self.dirty or not self.task: return
        # Completion can change in the list while the editor is open.
        try:
            self.update_action()
        except (ValueError,OSError,sqlite3.Error): pass

    def toggle_complete(self):
        if not self.save(): return
        try:
            latest=self.store.get(self.task['id'])
            self.store.mark(latest['id'],not latest['done'])
            self.update_action()
            self.app.render()
        except (ValueError,OSError,sqlite3.Error) as exc: self.show_error(str(exc))

    def prepare_close(self):
        try: self.dirty=self.dirty or self.values()!=self.baseline
        except ValueError: self.dirty=True
        if not self.dirty: return True
        if not self.task and not any((self.title.get().strip(),self.note.get('1.0','end-1c').strip(),self.due_var.get().strip())): return True
        if self.save(): return True
        return messagebox.askyesno('保留修改？','这条修改还没有保存。是否放弃当前窗口的修改？\n选择“否”返回继续编辑。',parent=self.window)

    def close(self,force=False):
        if self.closed: return True
        if not force and not self.prepare_close(): return False
        self.closed=True
        if self.pending: self.window.after_cancel(self.pending); self.pending=None
        self.app.details_windows.pop(self.key,None)
        self.window.destroy()
        return True
