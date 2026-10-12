"""Read only confirmed group/date scopes, preserving every selected message type.
Readable exports exclude media keys; original selected rows are DPAPI-sealed locally.
No private-chat queries, remote downloads, message sends, or screen automation.
"""
from __future__ import annotations
import base64
from collections import Counter
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from chatnotice import LOCAL_TZ, ReadScope, project_path, save_json
from database_reader import open_database
from memory_reader import protect_secret

MAX_BODY = 16 * 1024 * 1024
STRING_FIELDS = {45101:'text',45402:'filename',45403:'filepath',45419:'file_ext',
    45812:'local_path',45814:'ntos_path',45923:'card_text',47413:'reply_summary',
    47421:'reply_nickname',47602:'video_text',47901:'forward_metadata',
    48153:'call_description',48602:'forward_xml',80900:'system_content',
    80941:'system_text',80942:'system_text2'}
NUMBER_FIELDS = {45002:'content_type',45405:'filesize',45411:'width',45412:'height',
    47401:'reply_message_id',47403:'reply_timestamp',48152:'call_duration'}
WX_TYPES = {1:'text',3:'image',34:'voice',43:'video',47:'sticker',48:'location',
    49:'file_link_or_card',50:'call',10000:'system',11000:'sticker'}


def redact_media_secrets(text: str) -> str:
    text = re.sub(r'(?i)((?:cdnthumb|cdn)?aeskey|authkey|cdnthumbauthkey)(\s*=\s*["\'])[^"\']*',
                  r'\1\2[REDACTED]', text)
    text = re.sub(r'(?is)(<(?:aeskey|cdnthumbaeskey|cdnaeskey|fileuploadtoken|authkey)(?:\s[^>]*)?>).*?(</(?:aeskey|cdnthumbaeskey|cdnaeskey|fileuploadtoken|authkey)>)',
                  r'\1[REDACTED]\2', text)
    if text.lstrip().startswith('{'):
        try:
            data=json.loads(text)
            def clean(value):
                if isinstance(value,dict):
                    return {key:('[REDACTED]' if key.lower() in {'token','aeskey','authkey','fileuploadtoken','sign'} else clean(item)) for key,item in value.items()}
                if isinstance(value,list): return [clean(item) for item in value]
                return value
            text=json.dumps(clean(data),ensure_ascii=False)
        except ValueError: pass
    return text


def readable_card(text: str) -> str:
    try: data=json.loads(text)
    except (ValueError,TypeError): return text
    if data.get('app')=='com.tencent.mannounce':
        card=data.get('meta',{}).get('mannounce',{})
        if card.get('encode')==1 and card.get('text'):
            try: return '[群公告]\n'+base64.b64decode(card['text'],validate=True).decode('utf-8')
            except (ValueError,UnicodeDecodeError): return text
    return text


def json_safe(value):
    if isinstance(value, bytes):
        return {'encoding':'base64','value':base64.b64encode(value).decode('ascii')}
    if isinstance(value, dict):
        return {str(k):json_safe(v) for k,v in value.items()}
    if isinstance(value, (list,tuple)):
        return [json_safe(v) for v in value]
    return value


def varint(data: bytes, offset: int):
    value = 0
    for shift in range(0,70,7):
        if offset >= len(data):
            raise ValueError('Truncated protobuf varint')
        byte = data[offset]
        offset += 1
        value |= (byte & 127) << shift
        if byte < 128:
            if value > 0xFFFFFFFFFFFFFFFF:
                raise ValueError('Oversized protobuf integer')
            return value, offset
    raise ValueError('Invalid protobuf varint')


def wire_fields(data: bytes):
    if len(data) > MAX_BODY:
        raise ValueError('Message body exceeds parsing safety limit')
    offset = 0
    fields = []
    while offset < len(data):
        tag,offset = varint(data,offset)
        number,kind = tag >> 3, tag & 7
        if not number:
            raise ValueError('Invalid protobuf field number')
        if kind == 0:
            value,offset = varint(data,offset)
        elif kind in (1,5):
            size = 8 if kind == 1 else 4
            if offset + size > len(data):
                raise ValueError('Truncated fixed-width protobuf field')
            value = data[offset:offset+size]
            offset += size
        elif kind == 2:
            size,offset = varint(data,offset)
            if offset + size > len(data):
                raise ValueError('Truncated protobuf payload')
            value = data[offset:offset+size]
            offset += size
        else:
            raise ValueError('Unsupported protobuf wire type')
        fields.append((number,kind,value))
    return fields


def qq_part(data: bytes, depth: int = 0) -> dict:
    if depth > 8:
        raise ValueError('Quoted/forward message nesting exceeds safety limit')
    part = {'unknown_fields':[]}
    for number,kind,value in wire_fields(data):
        if number in STRING_FIELDS and kind == 2:
            part[STRING_FIELDS[number]] = redact_media_secrets(value.decode('utf-8',errors='replace'))
        elif number in NUMBER_FIELDS and kind == 0:
            part[NUMBER_FIELDS[number]] = value
        elif number == 45406 and kind == 2:
            part['md5'] = value.hex()
        elif number == 47710 and kind == 2:
            part['quoted'] = qq_part(value,depth+1)
        elif number == 45815 and kind == 2:
            part.setdefault('fallback_text',[]).append(redact_media_secrets(value.decode('utf-8',errors='replace')))
        else:
            part['unknown_fields'].append(number)
    return part


def qq_decode(payload: bytes) -> dict:
    fields = wire_fields(payload)
    chunks = [value for number,kind,value in fields if number == 40800 and kind == 2]
    parts = [qq_part(chunk) for chunk in chunks] if chunks else [qq_part(payload)]
    texts = []
    media = []
    for part in parts:
        for key in ('text','card_text','reply_summary','video_text','call_description',
                    'system_content','system_text','system_text2','forward_xml','forward_metadata'):
            if part.get(key):
                texts.append(part[key])
        if part.get('quoted',{}).get('text'):
            texts.append('[引用] '+part['quoted']['text'])
        if any(part.get(key) for key in ('filename','filepath','local_path','ntos_path','md5')):
            media.append({key:part[key] for key in ('filename','filepath','local_path','ntos_path',
                        'md5','filesize','file_ext','width','height') if key in part})
    return {'text':'\n'.join(readable_card(text) for text in texts),'parts':parts,'attachments':media,
            'body_status':'decoded_known_fields' if texts or media else 'nontext_or_unknown',
            'attachment_content_status':'not_processed' if media else 'not_applicable'}


def wechat_decode(payload: bytes | str | None) -> tuple[str,str]:
    if payload is None:
        return '', 'empty'
    if isinstance(payload,str):
        return redact_media_secrets(payload), 'utf8'
    if len(payload) > MAX_BODY:
        return '', 'oversized_body_pending'
    if payload.startswith(b'\x28\xb5\x2f\xfd'):
        try:
            import zstandard
        except ImportError:
            return '', 'zstd_dependency_missing'
        try:
            parameters = zstandard.get_frame_parameters(payload)
            if parameters.content_size not in (zstandard.CONTENTSIZE_UNKNOWN,zstandard.CONTENTSIZE_ERROR) and parameters.content_size > MAX_BODY:
                return '', 'oversized_decompression_pending'
            payload = zstandard.ZstdDecompressor().decompress(payload,max_output_size=MAX_BODY)
        except (ValueError,zstandard.ZstdError):
            return '', 'zstd_decode_pending'
    try:
        text = payload.decode('utf-8')
    except UnicodeDecodeError:
        return '', 'binary_body_pending'
    # Do not strip arbitrary container headers or guess that binary bytes are text.
    text = text.rstrip('\x00')
    if any(ord(character) < 32 and character not in '\n\r\t' for character in text):
        return '', 'container_body_pending'
    return redact_media_secrets(text), 'utf8' if text else 'empty'


def selected_qq(connection: sqlite3.Connection, scope: ReadScope) -> list[dict]:
    marks=','.join('?' for _ in scope.group_ids)
    sql = ('SELECT "40001" AS message_id,"40050" AS timestamp,"40033" AS sender_qq,'
           '"40020" AS sender_uid,"40021" AS group_id,"40030" AS group_qq,'
           '"40011" AS message_type,"40012" AS subtype,"40800" AS payload '
           'FROM group_msg_table WHERE ("40030" IN ('+marks+') OR "40021" IN ('+marks+')) '
           'AND "40050">=? AND "40050"<? ORDER BY "40050","40001"')
    rows=[]
    for row in connection.execute(sql,tuple(int(g) for g in scope.group_ids)+scope.group_ids+(scope.since,scope.until)):
        record=dict(row)
        candidates=(str(record['group_qq']),str(record['group_id']))
        group=next((g for g in candidates if g in scope.group_ids),None)
        if group is None or not scope.accepts('qq',group,record['timestamp']):
            raise ValueError('QQ query returned an out-of-scope row')
        record['group_id']=group
        rows.append(record)
    return rows


def verified_local_image_hashes(packed: bytes, account: str, group_id: str, month: str) -> list[str]:
    image_dir=Path(account)/'msg'/'attach'/hashlib.md5(group_id.encode()).hexdigest()/month/'Img'
    values={h.decode('ascii').lower() for h in re.findall(rb'[0-9a-fA-F]{32}',packed)}
    return sorted(value for value in values if any((image_dir/(value+suffix)).is_file() for suffix in ('.dat','_h.dat','_t.dat')))


def selected_wechat(connection: sqlite3.Connection, scope: ReadScope, shard: str) -> list[dict]:
    rows=[]
    for group in scope.group_ids:
        table='Msg_'+hashlib.md5(group.encode('utf-8')).hexdigest()
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():
            continue
        columns={r[1] for r in connection.execute('PRAGMA table_info("'+table+'")')}
        packed='packed_info_data' if 'packed_info_data' in columns else 'NULL'
        sql=('SELECT local_id AS message_id,server_id,local_type AS message_type,sort_seq,'
             'real_sender_id,create_time AS timestamp,download_status,'
             'CAST(message_content AS BLOB) AS payload,CAST(compress_content AS BLOB) AS compressed_payload,'
             'CAST('+packed+' AS BLOB) AS packed_info '
             'FROM "'+table+'" WHERE create_time>=? AND create_time<? ORDER BY create_time,local_id')
        for row in connection.execute(sql,(scope.since,scope.until)):
            record=dict(row,group_id=group,shard=shard)
            if not scope.accepts('wechat',group,record['timestamp']):
                raise ValueError('WeChat query returned an out-of-scope row')
            rows.append(record)
    sender_ids=sorted({r['real_sender_id'] for r in rows if r['real_sender_id']})
    sender_names={}
    for offset in range(0,len(sender_ids),500):
        batch=sender_ids[offset:offset+500]
        names=connection.execute('SELECT rowid,user_name FROM Name2Id WHERE rowid IN ('+
                                 ','.join('?' for _ in batch)+')',batch)
        sender_names.update({row[0]:row[1] for row in names})
    for row in rows:
        row['sender_username']=sender_names.get(row['real_sender_id'],'')
    return rows


def export_request(config: dict, request: dict) -> dict:
    if not request.get('confirmed'):
        raise PermissionError('Group selection not confirmed')
    scopes={s['platform']:ReadScope(s['platform'],tuple(s['group_ids']),s['since'],s['until']) for s in request['scopes']}
    groups={(g['platform'],g['group_id']):g['name'] for g in request['groups']}
    if set(groups) != {(p,g) for p,s in scopes.items() for g in s.group_ids}:
        raise ValueError('Selected group metadata and scopes do not match exactly')
    raw=[]
    snapshots=[]
    if 'qq' in scopes:
        with open_database(config,'qq','nt_msg.db') as (connection,meta):
            raw += [dict(r,platform='qq') for r in selected_qq(connection,scopes['qq'])]
            snapshots.append(dict(meta,platform='qq'))
    if 'wechat' in scopes:
        dbroot=Path(config['wechat']['database_dir'])
        shards=sorted((dbroot/'message').glob('message_[0-9]*.db'))
        if not shards:
            raise RuntimeError('No WeChat message shards found')
        for path in shards:
            relative=str(path.relative_to(dbroot))
            with open_database(config,'wechat',relative) as (connection,meta):
                raw += [dict(r,platform='wechat') for r in selected_wechat(connection,scopes['wechat'],relative)]
                snapshots.append(dict(meta,platform='wechat'))
    raw.sort(key=lambda r:(r['timestamp'],r['platform'],r['group_id'],str(r['message_id'])))
    readable=[]
    errors=[]
    for row in raw:
        platform=row['platform']
        identity=f"{platform}:{row['group_id']}:{row.get('shard','main')}:{row['message_id']}"
        message={'source_id':identity,'platform':platform,'group_id':row['group_id'],
                 'group_name':groups[(platform,row['group_id'])],'message_id':row['message_id'],
                 'timestamp':row['timestamp'],'time':datetime.fromtimestamp(row['timestamp'],LOCAL_TZ).isoformat(),
                 'message_type_code':row['message_type'],'sender':str(row.get('sender_qq') or row.get('sender_username') or ''),
                 'shard':row.get('shard','main')}
        try:
            if platform == 'qq':
                message.update(qq_decode(row['payload'] or b''))
                message['type']='qq_type_'+str(row['message_type'])
            else:
                text,status=wechat_decode(row['payload'])
                if not text and row.get('compressed_payload'):
                    text,status=wechat_decode(row['compressed_payload'])
                typ=WX_TYPES.get(row['message_type'] & 0xFFFFFFFF,'unknown')
                # Never publish arbitrary 32-hex metadata strings: they could be
                # media credentials. Keep only actual selected-group filenames.
                local_hashes=verified_local_image_hashes(row.get('packed_info') or b'',config['wechat']['account_dir'],row['group_id'],message['time'][:7])
                message.update(text=text,body_status=status,type=typ,attachments=[],local_media_hashes=local_hashes,
                    attachment_content_status='not_applicable' if typ in ('text','system','call') else 'not_processed')
        except (ValueError,TypeError) as exc:
            message.update(text='',body_status='parse_pending',type='unknown',attachments=[],
                           attachment_content_status='unknown')
            errors.append({'source_id':identity,'error':str(exc)})
        readable.append(message)
    batch=datetime.now(LOCAL_TZ).strftime('%Y%m%d-%H%M%S-%f')
    output=project_path('exports/'+batch)
    output.mkdir(parents=True,exist_ok=False)
    sealed=protect_secret(json.dumps(json_safe(raw),ensure_ascii=False).encode('utf-8'))
    (output/'raw-selected.dpapi').write_bytes(sealed)
    report={'request':request,'message_count':len(readable),'messages':readable,'snapshots':snapshots,
            'parse_errors':errors,'local_records_only':True,'all_attachment_contents_processed':False}
    save_json(f'exports/{batch}/messages.json',report)
    counts=Counter((m['platform'],m['group_id']) for m in readable)
    status=Counter(m['body_status'] for m in readable)
    summary={'batch':batch,'output_dir':str(output),'since':request['since'],'until':request['until'],
        'message_count':len(readable),'groups':[dict(g,message_count=counts[(g['platform'],g['group_id'])]) for g in request['groups']],
        'body_status_counts':dict(status),'parse_error_count':len(errors),'all_attachment_contents_processed':False,
        'raw_selected_protection':'Windows user-bound DPAPI','unrelated_private_chats_queried':False}
    save_json(f'exports/{batch}/summary.json',summary)
    save_json('state/latest-export.json',summary)
    lines=['# Selected messages',f"Window: {request['since']} — {request['until']}",'']
    for message in readable:
        lines += [f"## {message['time']} | {message['group_name']} | {message['type']} | {message['sender']}",
                  'Source: '+message['source_id'],message['text'] or '['+message['body_status']+']']
        if message['attachments']:
            lines.append('Attachments: '+json.dumps(message['attachments'],ensure_ascii=False))
        if message['attachment_content_status'] not in ('not_applicable',):
            lines.append('Attachment content: '+message['attachment_content_status'])
        lines.append('')
    (output/'messages.md').write_text('\n'.join(lines),encoding='utf-8')
    return summary


if __name__ == '__main__':
    root=Path(__file__).resolve().parent
    config=json.loads((root/'config.json').read_text(encoding='utf-8'))
    request=json.loads((root/'state/read-request.json').read_text(encoding='utf-8'))
    print(json.dumps(export_request(config,request),ensure_ascii=False,indent=2))
