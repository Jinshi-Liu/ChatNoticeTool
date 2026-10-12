"""Quiet, persistent Canvas views: no native child-window rebuild on polling.
Apple-inspired hierarchy/spacing; native Windows fonts, no Apple assets.
"""
from __future__ import annotations
import hashlib
import tkinter as tk

from glass_style import BG,CARD,INK,MUTED,ACCENT,RED,FONT


def rounded_points(x1,y1,x2,y2,r=16):
    r=min(r,(x2-x1)/2,(y2-y1)/2)
    return [x1+r,y1,x2-r,y1,x2,y1,x2,y1+r,x2,y2-r,x2,y2,
            x2-r,y2,x1+r,y2,x1,y2,x1,y2-r,x1,y1+r,x1,y1]


class SegmentedControl(tk.Canvas):
    def __init__(self,parent,command):
        super().__init__(parent,width=246,height=65,bg=BG,bd=0,highlightthickness=0,takefocus=True)
        self.command=command; self.state=None; self.compact=False
        self.left=self.create_text(4,28,anchor='w',text='待办',font=(FONT,-32,'bold'),fill=INK)
        self.right=self.create_text(127,28,anchor='w',text='完成',font=(FONT,-32,'bold'),fill=MUTED)
        self.left_count=self.create_text(91,13,anchor='w',font=(FONT,-16,'bold'),fill=ACCENT)
        self.right_count=self.create_text(214,13,anchor='w',font=(FONT,-16,'bold'),fill=MUTED)
        self.pill=self.create_line(6,58,58,58,fill=ACCENT,width=3,capstyle='round')
        self.bind('<Button-1>',lambda e:command(e.x>=self.winfo_width()/2))
        self.bind('<Left>',lambda e:command(False)); self.bind('<Right>',lambda e:command(True))
        self.configure(cursor='hand2')
    def set_compact(self,value):
        if self.compact==value: return
        self.compact=value
        self.configure(width=204 if value else 246,height=65)
        for item,x,y in ((self.left,4,28),(self.right,108 if value else 127,28),
                         (self.left_count,79 if value else 91,13),
                         (self.right_count,181 if value else 214,13)):
            self.coords(item,x,y)
        state=self.state; self.state=None
        if state: self.update_state(*state)

    def update_state(self,pending,done,selected):
        state=(pending,done,selected)
        if state==self.state: return
        self.state=state
        x=(110 if selected else 6) if self.compact else (129 if selected else 6)
        y=58
        self.coords(self.pill,x,y,x+52,y)
        self.itemconfigure(self.left,fill=MUTED if selected else INK)
        self.itemconfigure(self.right,fill=INK if selected else MUTED)
        self.itemconfigure(self.left_count,text=str(pending),fill=MUTED if selected else ACCENT)
        self.itemconfigure(self.right_count,text=str(done),fill=ACCENT if selected else MUTED)


class CheckHandle:
    """Invoke-compatible handle for mouse/keyboard and regression tests."""
    def __init__(self,owner,id,variable): self.owner,self.id,self.variable=owner,id,variable
    def invoke(self):
        self.variable.set(not self.variable.get())
        self.owner.on_toggle(self.id,self.variable)


class TaskCanvas(tk.Canvas):
    def __init__(self,parent,*,on_toggle,on_detail,on_menu,badge):
        super().__init__(parent,bg=BG,bd=0,highlightthickness=0,confine=True,yscrollincrement=0,takefocus=True)
        self.on_toggle,self.on_detail,self.on_menu,self.badge=on_toggle,on_detail,on_menu,badge
        self.rows={}; self.order=[]; self.tasks={}; self.checkbuttons={}; self.checkvars={}
        self.width=350; self.height=400; self.content_height=0; self.extent=400
        self.selected=None; self.empty=None; self.empty_text=''; self.compact=False
        self.visual_state=None; self.layout_count=0
        self.bind('<Configure>',self.resize)
        self.bind('<MouseWheel>',self.wheel)
        self.bind('<Up>',lambda e:self.navigate(-1))
        self.bind('<Down>',lambda e:self.navigate(1))
        self.bind('<space>',self.keyboard_toggle)
        self.bind('<Return>',lambda e:self.on_detail(self.selected) if self.selected else None)
        self.bind('<FocusIn>',lambda e:self.focus_outline())
        self.bind('<FocusOut>',lambda e:self.focus_outline())

    def create_row(self,id):
        tag='row-'+hashlib.sha256(id.encode()).hexdigest()[:16]
        row={'tag':tag,'items':[],'data':None}
        def add(name,kind,*coords,**options):
            item=getattr(self,'create_'+kind)(*coords,tags=(tag,name+'-'+tag),**options)
            row[name]=item; row['items'].append(item); return item
        add('bg','polygon',*rounded_points(0,0,300,90),smooth=True,fill=CARD,outline='',width=1)
        add('check_hit','rectangle',0,0,1,1,fill='',outline='')
        add('tick','line',0,0,1,1,2,2,fill='#B5B5BC',width=2.4,capstyle='round',joinstyle='round',state='normal')
        add('undo_curve','arc',0,0,1,1,start=-90,extent=245,style='arc',outline=ACCENT,width=2.4,state='hidden')
        add('undo_head','line',0,0,1,1,2,2,fill=ACCENT,width=2.4,capstyle='round',joinstyle='round',state='hidden')
        add('title','text',0,0,anchor='nw',justify='left',font=(FONT,-34,'bold'),fill=INK)
        add('meta','text',0,0,anchor='nw',justify='left',font=(FONT,-26),fill=ACCENT)
        add('preview','text',0,0,anchor='nw',justify='left',font=(FONT,-28),fill=MUTED)
        add('menu_hit','rectangle',0,0,1,1,fill='',outline='')
        for name in ('trash_lid','trash_handle','trash_body','trash_left','trash_right'):
            add(name,'line',0,0,1,1,fill=MUTED,width=1.8,capstyle='round',joinstyle='round')
        self.tag_bind(tag,'<Button-1>',lambda e,i=id:self.click_row(i,e))
        self.tag_bind(tag,'<Button-3>',lambda e,i=id:self.on_menu(i,e))
        self.tag_bind(tag,'<Enter>',lambda e:self.configure(cursor='hand2'))
        self.tag_bind(tag,'<Leave>',lambda e:self.configure(cursor=''))
        var=tk.BooleanVar(master=self,value=False)
        self.checkvars[id]=var; self.checkbuttons[id]=CheckHandle(self,id,var)
        self.rows[id]=row
        return row

    def sync(self,tasks,*,empty_text='',reset_scroll=False):
        # Ignore timestamps/preferences/nonvisual metadata. Clock refreshes do not
        # redraw anything unless the displayed deadline actually changed.
        state=tuple((t['id'],t['title'],t.get('note',''),bool(t['done']),bool(t['optional']),self.badge(t)) for t in tasks)
        self.tasks={t['id']:dict(t) for t in tasks}
        if state==self.visual_state and empty_text==self.empty_text:
            if reset_scroll: self.scroll_pixels(0)
            return False
        self.visual_state=state; self.empty_text=empty_text
        ids=[t['id'] for t in tasks]
        for id in list(self.rows):
            if id not in self.tasks:
                self.delete(self.rows[id]['tag']); del self.rows[id]
                self.checkbuttons.pop(id,None); self.checkvars.pop(id,None)
        for task in tasks:
            row=self.rows.get(task['id']) or self.create_row(task['id'])
            badge,color=self.badge(task)
            meta=badge+('  ·  按需' if task['optional'] else '')
            preview=' '.join(task.get('note','').split())[:72]
            if len(' '.join(task.get('note','').split()))>72: preview+='…'
            data=(task['title'],bool(task['done']),meta,color,preview)
            if data!=row['data']:
                self.itemconfigure(row['title'],text=task['title'],fill=MUTED if task['done'] else INK,
                    font=(FONT,-34,'overstrike' if task['done'] else 'bold'))
                self.itemconfigure(row['preview'],text=preview or '点击添加内容')
                self.itemconfigure(row['meta'],text=meta,fill=color)
                self.itemconfigure(row['tick'],state='hidden' if task['done'] else 'normal',fill='#85858F')
                for name in ('undo_curve','undo_head'):
                    self.itemconfigure(row[name],state='normal' if task['done'] else 'hidden')
                row['data']=data
            self.checkvars[task['id']].set(bool(task['done']))
        self.order=ids
        if self.selected not in self.rows: self.selected=ids[0] if ids else None
        if not ids:
            if self.empty is None:
                self.empty=self.create_text(0,0,anchor='n',font=(FONT,-24),fill=MUTED)
            self.itemconfigure(self.empty,text=empty_text)
        elif self.empty is not None:
            self.delete(self.empty); self.empty=None
        self.layout(reset_scroll=reset_scroll)
        return True

    def set_compact(self,value):
        if self.compact==value: return
        self.compact=value
        self.layout()

    def resize(self,event):
        if (event.width,event.height)==(self.width,self.height): return
        self.width,self.height=event.width,event.height
        self.layout()

    def layout(self,reset_scroll=False):
        self.layout_count+=1
        old_top=max(0,self.canvasy(0))
        y=4; right=max(80,self.width-8); text_width=max(20,right-(61 if self.compact else 83))
        left=12 if self.compact else 24; top=10 if self.compact else 20
        for index,id in enumerate(self.order):
            row=self.rows[id]
            self.itemconfigure(row['title'],width=text_width)
            self.coords(row['title'],left,y+top)
            title_bounds=self.bbox(row['title'])
            title_height=title_bounds[3]-title_bounds[1] if title_bounds else 20
            meta_y=y+top+title_height+(4 if self.compact else 8)
            self.itemconfigure(row['meta'],width=text_width)
            self.coords(row['meta'],left,meta_y)
            meta_bounds=self.bbox(row['meta'])
            meta_height=meta_bounds[3]-meta_bounds[1] if meta_bounds else 18
            preview_y=meta_y+meta_height+10
            self.coords(row['preview'],left,preview_y)
            self.itemconfigure(row['preview'],state='normal')
            self.itemconfigure(row['preview'],width=text_width)
            bounds=self.bbox(row['preview'])
            preview_height=bounds[3]-bounds[1] if bounds else 20
            bottom=preview_y+preview_height+(16 if self.compact else 22)
            self.coords(row['bg'],*rounded_points(4,y,right,bottom,20))
            cx=right-(20 if self.compact else 30); cy=y+(22 if self.compact else 34)
            hit=16 if self.compact else 24
            self.coords(row['check_hit'],cx-hit,cy-hit,cx+hit,cy+hit)
            self.coords(row['tick'],cx-10,cy,cx-3,cy+7,cx+11,cy-9)
            self.coords(row['undo_curve'],cx-9,cy-10,cx+11,cy+10)
            self.coords(row['undo_head'],cx-10,cy-11,cx-10,cy-2,cx-1,cy-2)
            self.coords(row['menu_hit'],right-46,bottom-39,right-10,bottom-6)
            tx,ty=right-28,bottom-23
            self.coords(row['trash_lid'],tx-10,ty-7,tx+10,ty-7)
            self.coords(row['trash_handle'],tx-4,ty-7,tx-4,ty-11,tx+4,ty-11,tx+4,ty-7)
            self.coords(row['trash_body'],tx-8,ty-7,tx-6,ty+11,tx+6,ty+11,tx+8,ty-7)
            self.coords(row['trash_left'],tx-3,ty-2,tx-2,ty+6)
            self.coords(row['trash_right'],tx+3,ty-2,tx+2,ty+6)
            y=bottom+10
        self.content_height=y+4 if self.order else 0
        if self.empty is not None:
            self.itemconfigure(self.empty,width=max(20,self.width-(24 if self.compact else 50)))
            self.coords(self.empty,self.width/2,24 if self.compact else 65)
            bounds=self.bbox(self.empty)
            if bounds: self.content_height=max(0,bounds[3]+8)
        self.extent=max(self.height,self.content_height,1)
        self.configure(scrollregion=(0,0,self.width,self.extent))
        self.scroll_pixels(0 if reset_scroll else old_top)
        self.focus_outline()

    def scroll_pixels(self,target):
        maximum=max(0,self.content_height-self.height)
        target=max(0,min(float(target),maximum))
        self.yview_moveto(target/max(1,self.extent))

    def scroll_command(self,*args):
        if args[0]=='moveto': self.scroll_pixels(float(args[1])*self.extent)
        elif args[0]=='scroll':
            pixels=28 if args[2]=='units' else max(28,self.height*.85)
            self.scroll_pixels(self.canvasy(0)+int(args[1])*pixels)

    def wheel(self,event):
        self.scroll_pixels(self.canvasy(0)-event.delta/120*56)
        return 'break'

    def click_row(self,id,event):
        x,y=self.canvasx(event.x),self.canvasy(event.y)
        row=self.rows[id]
        for name in ('check_hit','menu_hit'):
            left,top,right,bottom=self.coords(row[name])
            if left<=x<=right and top<=y<=bottom:
                if name=='check_hit': self.click_check(id)
                else: self.on_menu(id,event)
                return
        self.on_detail(id)

    def click_check(self,id):
        self.focus_set(); self.selected=id
        self.checkbuttons[id].invoke()

    def keyboard_toggle(self,event=None):
        if self.selected in self.checkbuttons: self.checkbuttons[self.selected].invoke()
        return 'break'

    def navigate(self,direction):
        if not self.order: return 'break'
        index=self.order.index(self.selected) if self.selected in self.order else 0
        self.selected=self.order[max(0,min(len(self.order)-1,index+direction))]
        box=self.bbox(self.rows[self.selected]['bg'])
        if box:
            top=self.canvasy(0)
            if box[1]<top: self.scroll_pixels(box[1]-4)
            elif box[3]>top+self.height: self.scroll_pixels(box[3]-self.height+4)
        self.focus_outline()
        return 'break'

    def focus_outline(self):
        focused=self.focus_get()==self
        for id,row in self.rows.items():
            desired=ACCENT if focused and id==self.selected else ''
            if self.itemcget(row['bg'],'outline')!=desired:
                self.itemconfigure(row['bg'],outline=desired)
