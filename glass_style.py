"""Solid-color Tk style and hand-drawn icons. No capture, network or Pillow.
The historical filename is kept for import compatibility.
"""
import tkinter as tk

BG='#F5F5F5'
CARD='#FFFFFF'
INK='#252528'
MUTED='#7C7C82'
ACCENT='#55556A'
PIN_ACTIVE='#1677E8'
RED='#B44956'
FONT='Microsoft YaHei UI'


class IconButton(tk.Canvas):
    def __init__(self,parent,kind,command,*,bg=BG,size=48,color=INK):
        super().__init__(parent,width=size,height=size,bg=bg,highlightthickness=0,bd=0,
                         takefocus=True,cursor='hand2')
        self.kind,self.command,self.color,self.size=kind,command,color,size
        self.active=False
        self.paint()
        self.bind('<Button-1>',lambda e:self.invoke())
        self.bind('<space>',lambda e:self.invoke())
        self.bind('<Return>',lambda e:self.invoke())
        self.bind('<Enter>',lambda e:self.configure(bg=CARD))
        self.bind('<Leave>',lambda e:self.configure(bg=bg))
        self.bind('<FocusIn>',lambda e:self.configure(highlightthickness=1,highlightbackground=ACCENT))
        self.bind('<FocusOut>',lambda e:self.configure(highlightthickness=0))
    def invoke(self): return self.command()
    def set_active(self,value):
        self.active=bool(value); self.paint()
    def set_size(self,size):
        if self.size!=size:
            self.size=size; self.configure(width=size,height=size); self.paint()
    def set_kind(self,kind):
        if self.kind!=kind:
            self.kind=kind; self.paint()
    def paint(self):
        self.delete('icon'); s=self.size/36
        color=(PIN_ACTIVE if self.kind=='pin' else ACCENT) if self.active else self.color
        def line(*p): self.create_line(*(v*s for v in p),fill=color,width=2.4,capstyle='round',joinstyle='round',tags='icon')
        if self.kind=='plus': line(18,9,18,27); line(9,18,27,18)
        elif self.kind=='pin':
            line(12,9,24,9); line(14,9,14,17,10,22,26,22,22,17,22,9); line(18,22,18,29)
        elif self.kind=='check': line(9,18,15,24,27,11)
        elif self.kind=='undo':
            self.create_arc(10*s,11*s,28*s,29*s,start=-90,extent=245,style='arc',
                            outline=color,width=2.4,tags='icon')
            line(9,10,9,18,17,18)
        elif self.kind=='save':
            line(10,9,24,9,27,12,27,27,9,27,9,9,10,9); line(14,9,14,15,22,15,22,9); line(13,27,13,21,23,21,23,27)
        elif self.kind=='close': line(11,11,25,25); line(25,11,11,25)
        elif self.kind=='more':
            for x in (11,18,25): self.create_oval((x-1)*s,17*s,(x+1)*s,19*s,fill=color,outline='',tags='icon')
