"""Experimental DingTalk V3 source for the existing read/write interface.

Group identity and field mappings adapted from @Jy66466's PR #1. No standalone
CLI, log-based UID guessing, task classification, network or memory access.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re

from agent_config import AgentError, digest, text
from chatnotice import LOCAL_TZ

GROUP_CHAT = 2


def dingtalk_groups(connection):
    return [{'platform': 'dingtalk', 'group_id': r['cid'], 'name': r['title'] or r['cid'], 'remark': ''}
            for r in connection.execute('SELECT cid,title FROM tbconversation WHERE type=? AND status=1 ORDER BY title,cid', (GROUP_CHAT,))]


def require_group_chats(connection, group_ids):
    marks = ','.join('?' for _ in group_ids)
    known = {r['cid']: (r['type'], r['title']) for r in connection.execute(
        'SELECT cid,type,title FROM tbconversation WHERE cid IN (' + marks + ')', group_ids)}
    if any(cid not in known or known[cid][0] != GROUP_CHAT for cid in group_ids):
        raise AgentError('scope_denied', '钉钉请求包含不存在的群或单聊，未查询消息正文')
    return {cid: known[cid][1] or cid for cid in group_ids}


def decode_text(content):
    if content is None:
        return '', 'empty'
    if not isinstance(content, str):
        return '', 'binary_body_pending'
    try:
        value = json.loads(content)
    except ValueError:
        value = content
    if isinstance(value, dict):
        if not isinstance(value.get('text'), str):
            return content, 'nontext_or_unknown'
        value = value['text']
    else:
        value = content  # Plain replies such as "1" remain ordinary text.
    return value, 'utf8' if value else 'empty'


def selected_dingtalk(connection, group_ids, since_ms, until_ms, names):
    if not group_ids or since_ms >= until_ms:
        raise AgentError('invalid_input', '钉钉读取须有明确群和时间范围')
    shards = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
              if re.fullmatch(r'tbmsg_\d+', r[0])]
    if not shards:
        raise AgentError('unsupported_format', '钉钉消息分表未找到，不代表没有通知')
    messages = {}
    marks = ','.join('?' for _ in group_ids)
    for shard in shards:
        columns = {r[1] for r in connection.execute(f'PRAGMA table_info("{shard}")')}
        if not {'mid', 'cid', 'senderId', 'createdAt', 'content', 'contentType', 'recallStatus'} <= columns:
            raise AgentError('unsupported_format', '钉钉消息字段不受支持，未返回不完整的成功结果')
        # Every matching shard is queried; non-text rows remain explicit pending
        # instead of being dropped and later described as fully processed.
        sql = (f'SELECT mid,cid,senderId,createdAt,contentType,recallStatus,'
               f'CASE WHEN contentType=1 THEN content ELSE NULL END AS content FROM "{shard}" '
               f'WHERE cid IN ({marks}) AND createdAt>=? AND createdAt<? ORDER BY createdAt,mid')
        for row in connection.execute(sql, (*group_ids, since_ms, until_ms)):
            if type(row['createdAt']) is not int or not since_ms <= row['createdAt'] < until_ms or row['cid'] not in group_ids:
                raise AgentError('out_of_scope', '钉钉读取返回越界记录')
            body, status = decode_text(row['content']) if row['contentType'] == 1 else ('', 'unsupported_body_pending')
            mid, cid = str(row['mid']), row['cid']
            if not mid or mid == 'None':
                raise AgentError('unsupported_format', '钉钉消息缺少稳定标识')
            stamp = row['createdAt'] // 1000
            message = {'platform': 'dingtalk', 'group_id': cid, 'group_name': names[cid],
                       'source_id': 'dingtalk:' + digest([cid, mid]), 'message_id': mid, 'server_id': mid,
                       'timestamp': stamp, 'time': datetime.fromtimestamp(stamp, LOCAL_TZ).isoformat(),
                       'sender': str(row['senderId'] or ''), 'text': body, 'body_status': status,
                       'type': 'text' if row['contentType'] == 1 else 'unsupported_' + str(row['contentType']),
                       'message_type_code': row['contentType'], 'recalled': bool(row['recallStatus']), 'attachments': [],
                       'attachment_content_status': 'not_applicable' if row['contentType'] == 1 else 'unsupported'}
            identity = cid, mid
            if identity in messages and messages[identity] != message:
                raise AgentError('ambiguous_message', '钉钉多个分表包含冲突的同ID消息，未猜测覆盖顺序')
            messages[identity] = message
    return sorted(messages.values(), key=lambda m: (m['timestamp'], m['group_id'], m['message_id']))


class DingTalkSource:
    platform = 'dingtalk'

    @property
    def capabilities(self):
        return {'platform': self.platform, 'operating_systems': ['windows'], 'groups': True,
                'local_history': True, 'local_images': False, 'file_import': False, 'memory_read': False,
                'live_receiving': False, 'remote_downloads': False, 'experimental': True,
                'supported_format': 'local-v3-group-text', 'verified_client_versions': []}

    def configure(self, payload):
        paths = payload.get('paths')
        if not isinstance(paths, dict) or set(paths) != {'account_dir'}:
            raise AgentError('invalid_input', '钉钉paths仅提供明确的account_dir')
        path = Path(text(paths['account_dir'], 'account_dir', 2000))
        if not path.is_absolute() or not path.is_dir():
            raise AgentError('path_not_found', '钉钉account_dir须是已存在的绝对目录')
        if path.name.lower().endswith('_v2'):
            raise AgentError('unsupported_format', '钉钉实验读取只支持V3，不调用外部V2解密工具')
        uid = text(payload.get('uid'), 'uid', 14)
        if not re.fullmatch(r'\d{8,14}', uid, re.ASCII):
            raise AgentError('invalid_input', '请明确提供本人真实UID（8到14位数字），不会跨账号日志猜测')
        path = path.resolve()
        return {'account_id': digest([self.platform, os.path.normcase(str(path)), uid])[:20],
                'paths': {'account_dir': str(path)}, 'uid': uid}

    def validate_scope(self, group_id, since, until):
        text(group_id, 'group_id', 200)
        if (group_id != group_id.strip() or any(ord(c) < 32 for c in group_id)
                or type(since) is not int or type(until) is not int or not 0 <= since < until):
            raise AgentError('invalid_input', '钉钉群或时间范围无效')

    def context(self, root, source):
        from agent_config import private_path
        return {'dingtalk': {**source['paths'], 'uid': source['uid']},
                'runtime': str(private_path(root, 'runtime/dingtalk-snapshots')),
                'policy': {'allow_process_memory_read': False}}

    def _open(self, config):
        import sys
        if sys.platform != 'win32':
            raise AgentError('unsupported_os', '钉钉客户端读取实验适配仅声明Windows；其他系统尚未验证')
        from dingtalk_probe import decrypted_database
        selected = config['dingtalk']
        return decrypted_database(selected['account_dir'], selected['uid'], runtime=config['runtime'])

    def check(self, config):
        from dingtalk_probe import check_header
        check_header(config['dingtalk']['account_dir'], config['dingtalk']['uid'])
        return {'status': 'format_candidate', 'read_chat_bodies': False, 'experimental': True,
                'notice': '仅验证V3首页结构；不是已完成真实客户端兼容性验证，也不提供页MAC认证'}

    def groups(self, config):
        with self._open(config) as (db, _):
            rows = dingtalk_groups(db)
        return {'groups': rows, 'read_chat_bodies': False, 'experimental': True}

    def read(self, config, request):
        if request.get('confirmed') is not True:
            raise AgentError('scope_denied', '钉钉读取范围未确认，未读取客户端')
        scopes = request.get('scopes')
        if not isinstance(scopes, list) or not scopes:
            raise AgentError('invalid_input', '钉钉读取缺少范围')
        all_groups = []
        for scope in scopes:
            ids = scope.get('group_ids')
            if scope.get('platform') != self.platform or not isinstance(ids, list) or not ids:
                raise AgentError('scope_denied', '钉钉读取来源或群范围无效')
            for cid in ids:
                self.validate_scope(cid, scope['since'], scope['until'])
                all_groups.append(cid)
        if len(all_groups) > 100 or len(set(all_groups)) != len(all_groups):
            raise AgentError('invalid_input', '钉钉每次最多100个不重复群')
        with self._open(config) as (db, metadata):
            names = require_group_chats(db, all_groups)
            messages = [m for scope in scopes for m in selected_dingtalk(
                db, scope['group_ids'], scope['since'] * 1000, scope['until'] * 1000, names)]
        return {'messages': messages, 'snapshots': [metadata], 'parse_errors': [],
                'timings': {'snapshot_decrypt_seconds': metadata['snapshot_decrypt_seconds']},
                'experimental': True, 'local_records_only': True}

    def connect(self, config, pids, authorized):
        raise AgentError('unsupported_operation', '钉钉V3使用明确UID及本地配置，不扫描内存或猜测UID')

    def attachment_grants(self, message):
        return set() if message.get('type') == 'text' else {'unsupported'}

    def asset_allowed(self, config, path):
        return False

    def prepare(self, config, messages, output, pid=0):
        raise AgentError('unsupported_operation', '钉钉实验适配暂不读取图片、文件或卡片')
