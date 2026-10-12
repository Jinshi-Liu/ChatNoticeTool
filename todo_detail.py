"""Modeless, directly editable note windows. Debounced local saves, no grabs.
Each task has its own window. Per-field optimistic merge protects other writers.
"""
from __future__ import annotations
import sqlite3
import re
from datetime import datetime
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


def parse_deadline_text(text,reference_date=None):
    text=text.strip()
    if not text: return None,''
    if text in ('截止待确认','未设截止','待确认'): return None,text
    match=re.fullmatch(r'(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})(?:日)?'
                       r'(?:[ T]+(\d{1,2}):(\d{2}))?(?:\s*([^\d\s].*))?',text)
    if not match: raise ValueError('时间可填 10/13、10月13日或2026-10-13，可加09:00或“上课前”')
    year,month,day,hour,minute,qualifier=match.groups()
    year=int(year or (reference_date[:4] if reference_date else datetime.now().year))
    date=f'{year:04d}-{int(month):02d}-{int(day):02d}'
    due=normalize_due(date+(f'T{int(hour):02d}:{minute}' if hour is not None else ''))
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
        win.configure(bg=BG); win.minsize(220,160)
        self.compact=False
        self.layout_pending=None; self.header_height=0
        offset=len(app.details_windows)%6*28
        x=max(10,min(app.root.winfo_x()-570+offset,win.winfo_screenwidth()-590))
        y=max(10,min(app.root.winfo_y()+40+offset,win.winfo_screenheight()-640))
        width,height=560,590
        saved=self.store.setting('detail_geometry:'+task['id']) if task else None
        match=re.fullmatch(r'(\d+)x(\d+)([+-]\d+)([+-]\d+)',saved) if isinstance(saved,str) else None
        if match:
            width,height,sx,sy=map(int,match.groups())
            width=min(max(220,width),win.winfo_screenwidth())
            height=min(max(160,height),win.winfo_screenheight())
            x=max(0,min(sx,win.winfo_screenwidth()-width)); y=max(0,min(sy,win.winfo_screenheight()-height))
        win.geometry(f'{width}x{height}+{x}+{y}')
        self.pinned=bool(self.store.setting('detail_pinned:'+task['id'],False)) if task else False
        win.attributes('-topmost',self.pinned)
        icon=Path(__file__).resolve().parent/'todo.ico'
        if icon.exists(): win.iconbitmap(str(icon))
        self.scrollbar=tk.Scrollbar(win,command=self.scroll_page,width=8,bd=0,highlightthickness=0)
        self.scrollbar.pack(side='right',fill='y')
        self.page_canvas=tk.Canvas(win,bg=BG,bd=0,highlightthickness=0,yscrollincrement=1)
        self.page_canvas.pack(side='left',fill='both',expand=True)
        self.page=tk.Frame(self.page_canvas,bg=BG)
        self.page_item=self.page_canvas.create_window(0,0,anchor='nw',window=self.page)
        self.page_canvas.configure(yscrollcommand=self.update_scrollbar)
        top=tk.Frame(self.page,bg=BG); self.top=top; top.pack(fill='x',padx=26,pady=(18,4))
        self.complete_button=IconButton(top,'save' if task is None else ('undo' if task['done'] else 'check'),self.primary_action)
        self.complete_button.pack(side='right',anchor='n',padx=(14,0))
        self.title_var=tk.StringVar(value=task['title'] if task else '')
        self.title=HintEntry(top,textvariable=self.title_var,hint='输入标题',width=1,font=(FONT,-38,'bold'),bg=BG,fg=INK,
            insertbackground=ACCENT,bd=0,highlightthickness=0)
        self.title.pack(side='left',fill='x',expand=True,ipady=1)
        date_row=tk.Frame(self.page,bg=BG); self.date_row=date_row; date_row.pack(fill='x',padx=30,pady=(0,10))
        self.pin_button=IconButton(date_row,'pin',self.toggle_pin)
        self.pin_button.pack(side='right',padx=(8,0))
        self.pin_button.set_active(self.pinned)
        self.deadline_original=(task['due'],task['due_label']) if task else (None,'')
        self.deadline_original_text=deadline_text(*self.deadline_original)
        self.due_var=tk.StringVar(value=self.deadline_original_text)
        self.due=HintEntry(date_row,textvariable=self.due_var,hint='截止时间：YYYY-MM-DD HH:MM（选填）',
            font=(FONT,-27),bg=BG,fg=ACCENT,insertbackground=ACCENT,bd=0,highlightthickness=0,width=22)
        self.due.pack(side='left',fill='x',expand=True)
        self.optional=tk.BooleanVar(value=bool(task['optional']) if task else False)  # metadata preserved, no extra UI
        content=tk.Frame(self.page,bg=CARD); self.content=content; content.pack(fill='both',expand=True,padx=20,pady=(0,8))
        self.note=tk.Text(content,font=(FONT,-30),wrap='word',bg=CARD,fg=INK,insertbackground=ACCENT,
            bd=0,highlightthickness=0,padx=16,pady=16,spacing1=3,spacing3=7,undo=True)
        self.note.pack(fill='both',expand=True)
        self.note.configure(yscrollcommand=self.update_scrollbar)
        self.status=tk.Label(self.page,text='',font=(FONT,-16),bg=BG,fg=RED,
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
        win.bind('<Control-s>',lambda e:(self.manual_save(),'break')[-1])
        win.bind('<Control-Return>',lambda e:(self.manual_save(),'break')[-1])
        win.bind('<Escape>',lambda e:self.close())
        win.protocol('WM_DELETE_WINDOW',self.close)
        win.bind('<FocusIn>',lambda e:self.refresh_clean(),add='+')
        win.bind('<Configure>',self.resize_layout,add='+')
        self.page_canvas.bind('<Configure>',lambda e:self.schedule_page_layout())
        for header in (self.top,self.date_row,self.status):
            header.bind('<Configure>',lambda e:self.schedule_page_layout())
        # Override Text's wheel binding: headers and body share one scroll path.
        win.bind('<MouseWheel>',self.wheel)
        self.note.bind('<MouseWheel>',self.wheel)
        self.scrollbar.bind('<MouseWheel>',self.wheel)
        self.note.bind('<Next>',lambda e:(self.scroll_page('scroll',1,'pages'),'break')[-1])
        self.note.bind('<Prior>',lambda e:(self.scroll_page('scroll',-1,'pages'),'break')[-1])
        for entry in (self.title,self.due):
            entry.bind('<FocusIn>',lambda e:self.page_canvas.yview_moveto(0),add='+')
        self.loading=False
        if not task: self.title.focus_set()

    def schedule_page_layout(self):
        if not self.closed and self.layout_pending is None:
            self.layout_pending=self.window.after_idle(self.layout_page)

    def layout_page(self):
        self.layout_pending=None
        if self.closed: return
        width,height=self.page_canvas.winfo_width(),self.page_canvas.winfo_height()
        if width<100 or height<60: return
        def padding(widget):
            value=widget.pack_info().get('pady',0)
            parts=widget.tk.splitlist(value if isinstance(value,(tuple,list)) else str(value))
            values=list(map(int,parts))
            return sum(values) if len(values)>1 else values[0]*2
        old=max(0,self.page_canvas.canvasy(0))
        self.header_height=sum(w.winfo_reqheight()+padding(w) for w in (self.top,self.date_row))
        self.header_height+=padding(self.content)
        if self.status.winfo_manager(): self.header_height+=self.status.winfo_reqheight()+padding(self.status)
        extent=height+self.header_height
        self.page_canvas.itemconfigure(self.page_item,width=width,height=extent)
        self.page_canvas.configure(scrollregion=(0,0,width,extent))
        self.page_canvas.yview_moveto(min(old,self.header_height)/max(1,extent))
        self.update_scrollbar()

    def scroll_metrics(self):
        first,last=self.note.yview()
        body=max(1,self.note.winfo_height())/max(1e-6,last-first)
        position=max(0,self.page_canvas.canvasy(0))+first*body
        return body,position,self.header_height+body

    def update_scrollbar(self,*_):
        if self.closed: return
        body,position,total=self.scroll_metrics()
        visible=max(1,self.page_canvas.winfo_height())
        self.scrollbar.set(min(1,position/total),min(1,(position+visible)/total))

    def scroll_to(self,position):
        body,_,total=self.scroll_metrics()
        position=max(0,min(position,max(0,total-self.page_canvas.winfo_height())))
        outer=min(position,self.header_height)
        extent=max(1,self.header_height+self.page_canvas.winfo_height())
        self.page_canvas.yview_moveto(outer/extent)
        self.note.yview_moveto(max(0,position-self.header_height)/body)
        self.update_scrollbar()

    def scroll_page(self,*args):
        _,position,total=self.scroll_metrics()
        if args[0]=='moveto': self.scroll_to(float(args[1])*total)
        elif args[0]=='scroll':
            step=56 if args[2]=='units' else max(56,self.page_canvas.winfo_height()*.85)
            self.scroll_to(position+int(args[1])*step)

    def wheel(self,event):
        _,position,_=self.scroll_metrics()
        self.scroll_to(position-event.delta/120*56)
        return 'break'

    def resize_layout(self,event):
        if event.widget!=self.window or event.width<220 or event.height<160: return
        self.status.configure(wraplength=max(100,event.width-24))
        self.schedule_page_layout()
        value=event.width<450 or event.height<330
        if value==self.compact: return
        self.compact=value
        self.top.pack_configure(padx=10 if value else 26,pady=(6,2) if value else (18,4))
        self.date_row.pack_configure(padx=12 if value else 30,pady=(0,4) if value else (0,10))
        self.content.pack_configure(padx=8 if value else 20,pady=(0,4) if value else (0,8))
        # Resizing changes available space, never the user's chosen large fonts.
        self.note.configure(padx=8 if value else 16,pady=2 if value else 16,
                            spacing1=1 if value else 3,spacing3=4 if value else 7)
        self.complete_button.pack_configure(padx=(4,0) if value else (14,0))
        for button in (self.complete_button,self.pin_button): button.set_size(28 if value else 48)

    def show_error(self,text):
        self.status.configure(text=text,fg=RED)
        self.status.pack(fill='x',padx=30,pady=(0,8),before=self.content)
        self.page_canvas.yview_moveto(0)
        self.schedule_page_layout()

    def manual_save(self):
        return self.save()

    def toggle_pin(self):
        value=not self.pinned
        try:
            if self.task: self.store.set_setting('detail_pinned:'+self.task['id'],value)
            self.window.attributes('-topmost',value)
            self.pinned=value; self.pin_button.set_active(value)
        except (ValueError,OSError,sqlite3.Error,tk.TclError) as exc: self.show_error(str(exc))

    def primary_action(self):
        # Unsaved edits mean SAVE, never accidentally mark a task completed.
        try: changed=self.values()!=self.baseline
        except ValueError: changed=True
        return self.manual_save() if self.task is None or self.dirty or changed else self.toggle_complete()

    def update_action(self):
        if self.task is None or self.dirty:
            self.complete_button.set_kind('save'); self.complete_button.set_active(False)
        else:
            latest=self.store.get(self.task['id'])
            self.complete_button.set_kind('undo' if latest['done'] else 'check')
            self.complete_button.set_active(bool(latest['done']))

    def values(self):
        due,label=(self.deadline_original if self.due_var.get().strip()==self.deadline_original_text
                   else parse_deadline_text(self.due_var.get(),self.deadline_original[0]))
        return {'title':self.title.get().strip(),
                'due':due,'due_label':label,
                'note':self.note.get('1.0','end-1c'),'optional':int(self.optional.get())}

    def modified(self,event):
        if event.widget.edit_modified():
            event.widget.edit_modified(False); self.changed()

    def changed(self):
        if self.loading or self.closed: return
        self.dirty=True
        self.update_action()
        self.status.pack_forget()
        self.schedule_page_layout()
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
                self.store.set_setting('detail_pinned:'+id,self.pinned)
            elif changes:
                self.task=self.store.patch_fields(self.task['id'],changes,self.baseline)
            # Baseline tracks visible fields, not unseen concurrent edits.
            self.baseline=values; self.dirty=False
            self.deadline_original=(values['due'],values['due_label'])
            self.deadline_original_text=self.due_var.get().strip()
            self.window.title(values['title']); self.status.pack_forget()
            self.schedule_page_layout()
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

    def close(self,force=False,save_geometry=None):
        if self.closed: return True
        if not force and not self.prepare_close(): return False
        save_geometry=not force if save_geometry is None else save_geometry
        if self.task and save_geometry:
            try: self.store.set_setting('detail_geometry:'+self.task['id'],self.window.geometry())
            except (ValueError,OSError,sqlite3.Error): pass  # Preference only, never discard a saved note.
        self.closed=True
        if self.layout_pending: self.window.after_cancel(self.layout_pending); self.layout_pending=None
        if self.pending: self.window.after_cancel(self.pending); self.pending=None
        self.app.details_windows.pop(self.key,None)
        self.window.destroy()
        return True
