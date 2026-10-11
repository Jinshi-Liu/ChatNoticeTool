from __future__ import annotations
from pathlib import Path
import hashlib
import sqlite3
import sys
import unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import messages
from chatnotice import ReadScope


def encode_varint(value):
    out=bytearray()
    while value>127:
        out.append((value & 127)|128)
        value >>= 7
    out.append(value)
    return bytes(out)


def field(number,value):
    return encode_varint((number<<3)|2)+encode_varint(len(value))+value


class MessageTests(unittest.TestCase):
    def test_qq_utf8_multiple_segments(self):
        payload=field(40800,field(45101,'通知：周一交作业'.encode()))+field(40800,field(45101,'截止18点'.encode()))
        result=messages.qq_decode(payload)
        self.assertIn('周一交作业',result['text'])
        self.assertIn('截止18点',result['text'])
        self.assertEqual(len(result['parts']),2)

    def test_qq_media_retained(self):
        part=field(45402,b'notice.png')+field(45406,bytes(range(16)))
        result=messages.qq_decode(field(40800,part))
        self.assertEqual(result['attachments'][0]['filename'],'notice.png')
        self.assertEqual(result['attachment_content_status'],'not_processed')

    def test_proto_truncated_fail_closed(self):
        with self.assertRaises(ValueError): messages.wire_fields(b'\x0a\x10x')
        with self.assertRaises(ValueError): messages.varint(b'\x80'*11,0)

    def test_wechat_zstd_text(self):
        import zstandard
        raw=zstandard.ZstdCompressor().compress('本周通知：请按时提交'.encode())
        text,status=messages.wechat_decode(raw)
        self.assertIn('请按时提交',text)
        self.assertEqual(status,'utf8')

    def test_binary_is_not_guessed(self):
        self.assertEqual(messages.wechat_decode(b'\xff\xfe')[1],'binary_body_pending')

    def test_media_keys_redacted(self):
        text='<img aeskey="sample-secret" cdnthumbAESkey="other-secret"/><aeskey>secret3</aeskey>'
        result=messages.redact_media_secrets(text)
        self.assertNotIn('sample-secret',result)
        self.assertNotIn('other-secret',result)
        self.assertNotIn('secret3',result)

    def test_wechat_v2_image_decryption_keeps_middle_and_xor_tail(self):
        import struct
        from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
        from attachments import V2_MAGIC,decrypt_local_image
        key=bytes(range(16)); xor=93
        plain=b'wxgf'+b'A'*31+b'\xff\xd9'
        prefix=plain[:16]+bytes([16])*16
        encoder=Cipher(algorithms.AES(key),modes.ECB()).encryptor()
        encrypted=encoder.update(prefix)+encoder.finalize()
        container=V2_MAGIC+struct.pack('<II',16,2)+b'\x00'+encrypted+plain[16:-2]+bytes(v^xor for v in plain[-2:])
        self.assertEqual(decrypt_local_image(container,key,xor),plain)

    def test_qq_selects_only_approved_group_and_time(self):
        conn=sqlite3.connect(':memory:')
        conn.row_factory=sqlite3.Row
        try:
            conn.execute('CREATE TABLE group_msg_table("40001","40050","40033","40020","40021","40030","40011","40012","40800")')
            for mid,group,time in [(1,'123',100),(2,'123',200),(3,'999',150),(4,'123',99)]:
                conn.execute('INSERT INTO group_msg_table VALUES(?,?,?,?,?,?,?,?,?)',(mid,time,1,'u',group,int(group),1,0,b''))
            result=messages.selected_qq(conn,ReadScope('qq',('123',),100,200))
            self.assertEqual([r['message_id'] for r in result],[1])
        finally: conn.close()

    def test_wechat_only_requested_table_and_time(self):
        conn=sqlite3.connect(':memory:')
        conn.row_factory=sqlite3.Row
        group='123@chatroom'
        table='Msg_'+hashlib.md5(group.encode()).hexdigest()
        try:
            conn.execute('CREATE TABLE Name2Id(user_name TEXT,is_session INTEGER)')
            conn.execute("INSERT INTO Name2Id VALUES('wxid_sender',0)")
            conn.execute('CREATE TABLE "'+table+'"(local_id,server_id,local_type,sort_seq,real_sender_id,create_time,download_status,message_content,compress_content)')
            for mid,timestamp in [(1,100),(2,200),(3,99)]:
                conn.execute('INSERT INTO "'+table+'" VALUES(?,?,?,?,?,?,?,?,?)',(mid,mid,1,mid,1,timestamp,0,'通知',None))
            result=messages.selected_wechat(conn,ReadScope('wechat',(group,),100,200),'synthetic')
            self.assertEqual([r['message_id'] for r in result],[1])
            self.assertEqual(result[0]['sender_username'],'wxid_sender')
        finally: conn.close()

    def test_unknown_message_type_preserved_in_raw_encoding(self):
        value={'payload':b'\x00\xff','message_type':123456}
        encoded=messages.json_safe(value)
        self.assertEqual(encoded['message_type'],123456)
        self.assertEqual(encoded['payload']['encoding'],'base64')


if __name__=='__main__': unittest.main()
