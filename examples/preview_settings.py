"""Preview the management window using disposable, synthetic accounts/groups.

python -B -X utf8 examples/preview_settings.py
python -B -X utf8 examples/preview_settings.py --screenshots ABS_OUTPUT_DIRECTORY --long-list
No real configuration, client, messages or credentials are read.
"""

from pathlib import Path
import argparse
import sys
import tempfile
import tkinter as tk

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from agent_config import AgentConfig
from rules_editor import RulesEditor


def fixture(project, *, long_list=False):
    config = AgentConfig(project)
    wechat = project / 'synthetic-wechat'
    qq = project / 'synthetic-qq'
    for path in (wechat / 'db_storage', qq / 'nt_db', qq / 'nt_data'):
        path.mkdir(parents=True)
    sources = []
    for platform, name, paths in (
        ('wechat', '微信演示账号', {'account_dir': str(wechat), 'database_dir': str(wechat / 'db_storage')}),
        ('qq', 'QQ 演示账号', {'database_dir': str(qq / 'nt_db'), 'media_dir': str(qq / 'nt_data')})):
        sources.append(config.configure_source({'confirmed': True, 'expected_revision': config.load()['revision'],
                                                'platform': platform, 'name': name, 'paths': paths})['source'])
    groups = []
    inbox = config.configure_source({'confirmed': True, 'expected_revision': config.load()['revision'],
                                      'platform': 'import', 'name': '合成文件收件箱'})['source']
    for source, identity, name, enabled in (
        (sources[0], 'demo-course@chatroom', '课程通知群 · 演示', True),
        (sources[0], 'demo-class@chatroom', '班级事务群 · 演示', False),
        (sources[1], '123456789', '作业讨论群 · 演示', True),
        (inbox, 'inbox', '文件通知 · 演示', True)):
        groups.append({'source_id': source['id'], 'group_id': identity, 'name': name, 'enabled': enabled})
    if long_list:
        for index in range(60):
            source = (sources[1], sources[0], inbox)[index % 3]
            identity = (str(880000000 + index) if index % 3 == 0 else
                        f'demo-notifications-{index:02}-long-id-for-hover-preview@chatroom' if index % 3 == 1 else f'inbox-{index}')
            groups.append({'source_id': source['id'], 'group_id': identity,
                           'name': f'{index + 1:02} · 合成通知群 · 演示'})
    configured = config.apply_groups({'confirmed': True, 'expected_revision': config.load()['revision'], 'groups': groups})['groups']
    # Record one genuinely successful synthetic service read (no client access),
    # leaving other groups unknown to demonstrate both display states.
    from unittest.mock import patch
    from agent_service import AgentService
    with patch('agent_sources.LocalSource.read', return_value={'messages': [], 'snapshots': []}):
        selected = [configured[2]['key']]
        if long_list:
            selected.extend(group['key'] for group in configured[4:6])
        AgentService(project).read(group_keys=selected, prepare_images=False)
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--screenshots', type=Path)
    parser.add_argument('--long-list', action='store_true', help='增加混合平台合成群，展示整表滚动')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='chatnotice-settings-preview-') as temporary:
        project = Path(temporary).resolve()
        config = fixture(project, long_list=args.long_list)
        if sys.platform == 'win32':
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except (AttributeError, OSError):
                pass
        root = tk.Tk()
        root.withdraw()
        editor = RulesEditor(root, project / 'AGENTS.md', config=config, title='规则与群聊 · 演示（不修改真实配置）')
        editor.window.attributes('-topmost', True)
        editor.window.geometry('920x720+100+70')
        editor.focus()
        root.update()
        editor.window.bind('<Destroy>', lambda event: root.quit() if event.widget == editor.window else None, add='+')
        try:
            if args.screenshots is None:
                root.mainloop()
            else:
                from PIL import ImageGrab
                args.screenshots.mkdir(parents=True, exist_ok=True)
                shots = [('rules', '920x720', 'rules.png'), ('groups', '920x720', 'groups.png'),
                         ('groups', '480x640', 'groups-compact.png')]
                if args.long_list:
                    shots.append(('groups', '920x720', 'groups-scrolled.png'))
                for tab, size, filename in shots:
                    editor.switch_tab(tab)
                    editor.window.geometry(size + '+100+70')
                    root.update()
                    if tab == 'groups':
                        editor.groups_page.table.yview_moveto(.55 if filename == 'groups-scrolled.png' else 0)
                    root.after(250, root.quit)
                    root.mainloop()
                    win = editor.window
                    box = (win.winfo_rootx(), win.winfo_rooty(),
                           win.winfo_rootx() + win.winfo_width(), win.winfo_rooty() + win.winfo_height())
                    target = args.screenshots / filename
                    ImageGrab.grab(bbox=box).save(target)
                    print(target)
        finally:
            editor.close(force=True)
            root.destroy()


if __name__ == '__main__':
    main()
