"""Process local resources referenced by selected messages only. No downloads.
Copy into the current export; verify file hashes where provided. Never execute
HTML/scripts, macros, archives, or message instructions. No screen capture.
"""
from __future__ import annotations
from collections import Counter
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import xml.etree.ElementTree as ET
import zipfile

from chatnotice import project_path,save_json
from memory_reader import ReadOnlyProcess,load_secrets,merge_verified_secrets

MAX_FILE=32*1024*1024
V2_MAGIC=b'\x07\x08V2\x08\x07'
V1_MAGIC=b'\x07\x08V1\x08\x07'


def md5_file(path: Path) -> str:
    h=hashlib.md5()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()


def allowed_local_source(path: Path, config: dict) -> bool:
    resolved=path.resolve()
    values=[config.get('qq',{}).get('media_dir'),config.get('wechat',{}).get('account_dir')]
    values+=config.get('attachments',{}).get('extra_allowed_roots',[])
    roots=[Path(value).resolve() for value in values if value and Path(value).is_absolute()]
    return any(resolved.is_relative_to(root) for root in roots)


def safe_filename(name: str) -> str:
    return re.sub(r'[<>:"/\\\\|?*\x00-\x1f]','_',Path(name).name).rstrip('. ') or 'attachment'


def normalize_hash_name(name: str) -> str:
    return re.sub(r'[{}-]','',name.lower())


def image_magic(data: bytes) -> bool:
    return data.startswith((b'\xff\xd8\xff',b'\x89PNG\r\n\x1a\n',b'GIF8',b'RIFF',b'wxgf'))


def decrypt_local_image(data: bytes, aes_key: bytes | None, xor_key: int | None) -> bytes:
    if image_magic(data): return data
    if data[:6]==V2_MAGIC:
        if not aes_key: raise ValueError('Local WeChat image AES key unavailable')
        from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
        aes_size,xor_size=struct.unpack_from('<II',data,6)
        block=((aes_size//16)+1)*16
        if block>len(data)-15 or xor_size>len(data)-15-block: raise ValueError('Invalid image container size')
        decryptor=Cipher(algorithms.AES(aes_key),modes.ECB()).decryptor()
        first=decryptor.update(data[15:15+block])+decryptor.finalize()
        padding=first[-1]
        if 1<=padding<=16 and first[-padding:]==bytes([padding])*padding: first=first[:-padding]
        middle=data[15+block:len(data)-xor_size] if xor_size else data[15+block:]
        tail=data[-xor_size:] if xor_size else b''
        choices=[xor_key] if xor_key is not None else range(256)
        for key in choices:
            out=first+middle+bytes(b^key for b in tail)
            if image_magic(out):
                if out.startswith(b'\xff\xd8\xff'):
                    end=out.rfind(b'\xff\xd9')
                    if end>=0 and len(out)-end<=64: out=out[:end+2]
                return out
        raise ValueError('Image plaintext signature invalid')
    body=data[22:] if data[:6]==V1_MAGIC else data
    for key in ([xor_key] if xor_key is not None else range(256)):
        out=bytes(b^key for b in body)
        if image_magic(out): return out
    raise ValueError('Unknown local image encryption format')


def display_image_bytes(data: bytes) -> bytes:
    if not data.startswith(b'wxgf'): return data
    import subprocess
    executable=shutil.which('ffmpeg')
    if not executable: raise ValueError('Local WXGF/HEVC decoder is unavailable')
    start=data.find(b'\x00\x00\x00\x01')
    if start<0: raise ValueError('WXGF container has no HEVC stream')
    result=subprocess.run([executable,'-nostdin','-v','error','-threads','1',
        '-protocol_whitelist','pipe','-f','hevc','-i','pipe:0','-frames:v','1',
        '-threads','1','-f','image2pipe','-vcodec','png','pipe:1'],
        input=data[start:],capture_output=True,timeout=30,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode or not result.stdout.startswith(b'\x89PNG'):
        raise ValueError('Local HEVC image conversion failed')
    if len(result.stdout)>MAX_FILE: raise ValueError('Converted image exceeds size limit')
    return result.stdout


def wx_image_key(config: dict, probes: list[Path], pid: int) -> tuple[bytes | None,int | None]:
    encrypted=[]
    for path in probes:
        with path.open('rb') as f: head=f.read(31)
        if head[:6]==V2_MAGIC and len(head)>=31: encrypted.append(head[15:31])
    if not encrypted: return None,None
    from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
    account=config['wechat']['account_dir']
    def verifies(key):
        for sample in encrypted:
            decryptor=Cipher(algorithms.AES(key),modes.ECB()).decryptor()
            if image_magic(decryptor.update(sample)+decryptor.finalize()): return True
        return False
    cached=load_secrets().get('wechat_images',{}).get(account)
    if cached:
        key=bytes.fromhex(cached['aes_key_hex'])
        if verifies(key): return key,cached['xor_key']
    if not pid or not config['policy'].get('allow_process_memory_read'): return None,None
    expected=re.sub(r'_\w{4}$','',Path(account).name)
    with ReadOnlyProcess(pid,'wechat',timeout=120) as process:
        anchors=process.find([b'global_config'])[b'global_config']
        for address in anchors:
            landmark=process.read(address,32)
            if len(landmark)<32 or struct.unpack_from('<Q',landmark,16)[0]!=13 or struct.unpack_from('<Q',landmark,24)[0]!=15: continue
            v18_data=process.read(address+16-0x138,8)
            if len(v18_data)!=8: continue
            v18=struct.unpack('<Q',v18_data)[0]
            cfg_data=process.read(v18+0x68,8)
            if len(cfg_data)!=8: continue
            cfg=struct.unpack('<Q',cfg_data)[0]
            wxstring=process.read(cfg+0x48,32)
            dword_data=process.read(cfg+0x40,4)
            if len(wxstring)!=32 or len(dword_data)!=4: continue
            length=struct.unpack_from('<Q',wxstring,16)[0]
            if not 0<length<128: continue
            wxbytes=wxstring[:length] if length<=15 else process.read(struct.unpack_from('<Q',wxstring)[0],length)
            if wxbytes.decode('utf-8',errors='replace')!=expected: continue
            dword=struct.unpack('<I',dword_data)[0]
            key=hashlib.md5((str(dword)+expected).encode()).hexdigest()[:16].encode()
            if verifies(key):
                merge_verified_secrets('wechat_images',{account:{'aes_key_hex':key.hex(),'xor_key':dword & 255}})
                return key,dword & 255
    return None,None


class VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__(); self.skipping=0; self.parts=[]
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'): self.skipping+=1
        if tag in ('p','div','br','h1','h2','h3','li','tr'): self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style') and self.skipping: self.skipping-=1
    def handle_data(self,data):
        if not self.skipping: self.parts.append(data)


def document_text(path: Path) -> str:
    ext=path.suffix.lower()
    if ext=='.pdf':
        from pypdf import PdfReader
        return '\n\n'.join(f'--- Page {i+1} ---\n'+(page.extract_text() or '') for i,page in enumerate(PdfReader(path).pages))
    if ext=='.doc':
        from document_formats import legacy_doc_text
        return legacy_doc_text(path)
    if ext=='.xlsx':
        from document_formats import xlsx_text
        return xlsx_text(path)
    if ext=='.docx':
        with zipfile.ZipFile(path) as archive:
            info=archive.getinfo('word/document.xml')
            if info.file_size>MAX_FILE: raise ValueError('DOCX XML too large')
            root=ET.fromstring(archive.read(info))
        ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        return '\n'.join(''.join(n.text or '' for n in p.findall('.//w:t',ns)) for p in root.findall('.//w:p',ns))
    if ext in ('.html','.htm'):
        parser=VisibleHTML(); parser.feed(path.read_text(encoding='utf-8',errors='replace'))
        return '\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())
    if ext in ('.txt','.md','.csv'):
        return path.read_text(encoding='utf-8',errors='replace')
    raise ValueError('Document format not yet supported: '+ext)


def safe_zip_extract(path: Path, target: Path) -> list[Path]:
    target.mkdir(parents=True,exist_ok=True)
    output=[]
    with zipfile.ZipFile(path) as archive:
        if sum(i.file_size for i in archive.infolist())>64*1024*1024: raise ValueError('Archive uncompressed size too large')
        for info in archive.infolist():
            name=info.filename
            try: name=name.encode('cp437').decode('gbk') if not info.flag_bits & 0x800 else name
            except (UnicodeError,UnicodeDecodeError): pass
            dest=(target/name).resolve()
            if not dest.is_relative_to(target.resolve()) or ':' in name or '\\' in name or info.file_size>MAX_FILE:
                raise ValueError('Unsafe archive member path/size')
            if info.is_dir(): dest.mkdir(parents=True,exist_ok=True); continue
            dest.parent.mkdir(parents=True,exist_ok=True)
            if dest.exists():
                import zlib
                if dest.stat().st_size!=info.file_size or zlib.crc32(dest.read_bytes()) & 0xFFFFFFFF != info.CRC:
                    raise ValueError('Existing extracted file differs; refusing to overwrite')
            else:
                with archive.open(info) as incoming,dest.open('xb') as outgoing: shutil.copyfileobj(incoming,outgoing)
            output.append(dest)
    return output


def collect(report: dict,config: dict,output: Path,wx_pid: int) -> dict:
    selected=report['messages']
    items=[]
    qq_value=config.get('qq',{}).get('media_dir','')
    qq_root=Path(qq_value) if qq_value else None
    # Index names only under the client's media storage, never unrelated user folders.
    hashes={a.get('md5','').lower() for m in selected if m['platform']=='qq' for a in m['attachments'] if a.get('md5')}
    qq_files={}
    for directory in ('Pic','File','Emoji'):
        if qq_root is None: continue
        root=qq_root/directory
        if not root.exists(): continue
        for base,_,names in os.walk(root):
            for name in names:
                normalized=normalize_hash_name(name)
                for digest in hashes:
                    if digest in normalized:
                        qq_files.setdefault(digest,[]).append(Path(base)/name)
    wx_probes=[]
    wx_value=config.get('wechat',{}).get('account_dir','')
    wxroot=Path(wx_value) if wx_value else None
    for message in selected:
        if message['platform']=='qq':
            for attachment in message['attachments']:
                candidates=[]
                for field in ('filepath','local_path','ntos_path'):
                    if attachment.get(field):
                        candidates.append(Path(attachment[field].removeprefix('::NTOSFull::')))
                candidates+=qq_files.get(attachment.get('md5','').lower(),[])
                if attachment.get('filename'):
                    for root in config.get('attachments',{}).get('extra_allowed_roots',[]):
                        if root and Path(root).is_absolute():
                            candidates.append(Path(root)/Path(attachment['filename']).name)
                items.append({'source_id':message['source_id'],'platform':'qq','group_name':message['group_name'],
                              'filename':attachment.get('filename',''),'md5':attachment.get('md5',''),
                              'candidates':[str(p) for p in candidates if p.is_file()]})
        elif message['type'] in ('image','file_link_or_card'):
            start=message['text'].find('<?xml')
            if start<0: start=message['text'].find('<msg>')
            if start<0: continue
            xml=ET.fromstring(message['text'][start:])
            if message['type']=='image':
                image=xml.find('img')
                if image is None: continue
                digest=image.get('md5','')
                local_hashes=message.get('local_media_hashes',[]) or [digest]
                folder=wxroot/'msg'/'attach'/hashlib.md5(message['group_id'].encode()).hexdigest()
                matches=[]
                target_names={name for value in local_hashes for name in (value+'.dat',value+'_h.dat',value+'_t.dat')}
                for base,_,names in os.walk(folder):
                    for name in names:
                        if name in target_names:
                            matches.append(Path(base)/name)
                wx_probes+=matches
                items.append({'source_id':message['source_id'],'platform':'wechat','group_name':message['group_name'],
                              'filename':digest+'.jpg','md5':digest,'candidates':[str(p) for p in matches],'image':True})
            else:
                app=xml.find('appmsg')
                if app is None or app.findtext('type')!='6': continue
                filename=app.findtext('title') or ''
                name=Path(filename).name
                matches=list((wxroot/'msg'/'file').glob('*/'+name)) if name else []
                items.append({'source_id':message['source_id'],'platform':'wechat','group_name':message['group_name'],
                              'filename':filename,'md5':app.findtext('md5') or '',
                              'candidates':[str(p) for p in matches if p.is_file()]})
    # Reuse an explicitly selected identical file, even if its displayed filename changed.
    hash_sources={}
    for item in items:
        if not item.get('image') and item.get('md5'):
            hash_sources.setdefault(item['md5'],set()).update(item['candidates'])
    for item in items:
        if not item.get('image') and item.get('md5'):
            item['candidates']=sorted(set(item['candidates'])|hash_sources.get(item['md5'],set()))
    aes,xor=wx_image_key(config,wx_probes,wx_pid) if wx_probes else (None,None)
    asset_root=output/'attachments'; asset_root.mkdir(exist_ok=True)
    for n,item in enumerate(items,1):
        item['asset_number']=n
        candidates=[Path(p) for p in item['candidates'] if allowed_local_source(Path(p),config)]
        # Prefer originals/compressed originals over thumbnails.
        candidates.sort(key=lambda p:('_t.dat' in p.name or 'Thumb' in p.parts,-p.stat().st_size))
        item['status']='not_saved_locally'
        for source in candidates:
            try:
                if source.stat().st_size>MAX_FILE: raise ValueError('Attachment too large for current safety limit')
                destination=asset_root/f'{n:02d}-{safe_filename(item["filename"])}'
                if item.get('image'):
                    decoded=display_image_bytes(decrypt_local_image(source.read_bytes(),aes,xor))
                    from PIL import Image
                    import io
                    image=Image.open(io.BytesIO(decoded)); image.load()
                    destination=destination.with_suffix('.png')
                    image.save(destination,format='PNG')
                    item['status']='image_decoded'; item['image_size']=list(image.size)
                    item['quality']='thumbnail' if '_t.dat' in source.name else 'local_original_or_compressed'
                else:
                    digest=md5_file(source)
                    if item.get('md5') and digest.lower()!=item['md5'].lower():
                        if 'Thumb' in source.parts and item['md5'] in normalize_hash_name(source.name):
                            from PIL import Image
                            with Image.open(source) as image: image.verify()
                            shutil.copyfile(source,destination)
                            item['status']='thumbnail_available'; item['quality']='thumbnail'
                            item['source_path']=str(source); item['local_path']=str(destination)
                            break
                        item['status']='local_file_hash_mismatch'
                        continue
                    shutil.copyfile(source,destination)
                    item['status']='copied_and_hash_verified' if item.get('md5') else 'copied_local_file'
                    try:
                        text=document_text(destination)
                        textpath=destination.with_suffix(destination.suffix+'.txt')
                        textpath.write_text(text,encoding='utf-8')
                        item['text_path']=str(textpath)
                    except ValueError: pass
                    if destination.suffix.lower()=='.zip':
                        members=safe_zip_extract(destination,asset_root/f'{n:02d}-unpacked')
                        item['archive_members']=[]
                        for member in members:
                            entry={'path':str(member)}
                            try:
                                text=document_text(member)
                                textpath=member.with_suffix(member.suffix+'.txt')
                                textpath.write_text(text,encoding='utf-8'); entry['text_path']=str(textpath)
                            except ValueError: entry['status']='format_pending'
                            item['archive_members'].append(entry)
                item['source_path']=str(source); item['local_path']=str(destination)
                break
            except (OSError,ValueError,zipfile.BadZipFile,ET.ParseError) as exc:
                item['status']='processing_pending'; item['error']=str(exc)
    result={'items':items,'status_counts':dict(Counter(i['status'] for i in items)),
            'remote_downloads':False,'image_key_printed':False}
    save_json(str((output/'attachments-manifest.json').relative_to(Path(__file__).resolve().parent)),result)
    return result


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--wx-pid',type=int,default=0)
    parser.add_argument('--authorize-memory-read',action='store_true',help='Allow read-only image-key acquisition for this run')
    args=parser.parse_args()
    root=Path(__file__).resolve().parent
    from chatnotice import load_config
    config=load_config()
    if args.authorize_memory_read: config['policy']['allow_process_memory_read']=True
    latest=json.loads((root/'state/latest-export.json').read_text(encoding='utf-8'))
    output=project_path('exports/'+latest['batch'])
    report=json.loads((output/'messages.json').read_text(encoding='utf-8'))
    result=collect(report,config,output,args.wx_pid)
    print(json.dumps(result,ensure_ascii=False,indent=2))
