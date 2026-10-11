"""Local document text decoding without Office, macros, or script execution."""
from __future__ import annotations
from pathlib import Path
import struct
import xml.etree.ElementTree as ET
import zipfile

def legacy_doc_text(path: Path) -> str:
    import olefile
    with olefile.OleFileIO(path) as document:
        stream=document.openstream('WordDocument').read()
        if struct.unpack_from('<H',stream)[0]!=0xA5EC:
            raise ValueError('Unsupported legacy Word signature')
        flags=struct.unpack_from('<H',stream,10)[0]
        if flags & 0x0100: raise ValueError('Encrypted Word file requires a separate document password')
        table=document.openstream('1Table' if flags & 0x0200 else '0Table').read()
        pos=32
        csw=struct.unpack_from('<H',stream,pos)[0]; pos+=2+csw*2
        cslw=struct.unpack_from('<H',stream,pos)[0]; pos+=2
        ccp_text=struct.unpack_from('<I',stream,pos+12)[0]
        pos+=cslw*4
        pairs=struct.unpack_from('<H',stream,pos)[0]; pos+=2
        if pairs<=33: raise ValueError('Unsupported legacy Word FIB layout')
        fc_clx,length=struct.unpack_from('<II',stream,pos+33*8)
        clx=table[fc_clx:fc_clx+length]
        offset=0
        while offset<len(clx) and clx[offset]==1:
            extra=struct.unpack_from('<H',clx,offset+1)[0]
            offset+=3+extra
        if offset>=len(clx) or clx[offset]!=2: raise ValueError('Word piece table not found')
        size=struct.unpack_from('<I',clx,offset+1)[0]
        piece=clx[offset+5:offset+5+size]
        if len(piece)!=size or (size-4)%12: raise ValueError('Malformed Word piece table')
        count=(size-4)//12
        cps=struct.unpack_from('<'+'I'*(count+1),piece)
        descriptor=4*(count+1)
        result=[]
        for i in range(count):
            start,end=cps[i],min(cps[i+1],ccp_text)
            if start>=ccp_text: break
            encoded=struct.unpack_from('<I',piece,descriptor+i*8+2)[0]
            compressed=bool(encoded & 0x40000000)
            offset=encoded & 0x3FFFFFFF
            if compressed:
                offset//=2; data=stream[offset:offset+end-start]; text=data.decode('cp1252',errors='replace')
            else:
                data=stream[offset:offset+2*(end-start)]; text=data.decode('utf-16le',errors='strict')
            result.append(text)
        return ''.join(result).replace('\r','\n').replace('\x07','\t').replace('\x0b','\n').replace('\x0c','\n')


def xlsx_text(path: Path) -> str:
    ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    lines=[]
    with zipfile.ZipFile(path) as archive:
        shared=[]
        if 'xl/sharedStrings.xml' in archive.namelist():
            root=ET.fromstring(archive.read('xl/sharedStrings.xml'))
            shared=[''.join(t.text or '' for t in node.findall('.//s:t',ns)) for node in root.findall('s:si',ns)]
        for filename in sorted(n for n in archive.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml')):
            lines.append('--- '+filename+' ---')
            root=ET.fromstring(archive.read(filename))
            for row in root.findall('.//s:sheetData/s:row',ns):
                cells=[]
                for cell in row.findall('s:c',ns):
                    value=cell.findtext('s:v','',ns)
                    if cell.get('t')=='s' and value: value=shared[int(value)]
                    elif cell.get('t')=='inlineStr': value=''.join(t.text or '' for t in cell.findall('.//s:t',ns))
                    if value: cells.append(cell.get('r','')+': '+value)
                if cells: lines.append(' | '.join(cells))
    return '\n'.join(lines)
