"""Initialize private local files only; never overwrite config or progress."""
from __future__ import annotations
import json
from pathlib import Path
import shutil

from chatnotice import ROOT, project_path
from cleanup import OWNER_ID, OWNED


def initialize(root: Path = ROOT) -> list[str]:
    root = root.absolute()
    if root.is_symlink() or root.resolve() != root or bool(getattr(root.lstat(), 'st_file_attributes', 0) & 0x400):
        raise ValueError('Project root must not be a link/junction')
    changed = []
    marker = root / '.chatnotice-owned.json'
    if marker.exists():
        if marker.is_symlink() or bool(getattr(marker.lstat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('Refusing linked ownership marker')
        data = json.loads(marker.read_text(encoding='utf-8'))
        if data.get('owner_id') != OWNER_ID or data.get('root') != str(root):
            raise ValueError('Existing ownership marker does not match; not overwritten')
    else:
        marker.write_text(json.dumps({'project': 'ChatNoticeTool', 'owner_id': OWNER_ID,
            'root': str(root), 'owned_top_level': sorted(OWNED), 'external_changes': []},
            ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
        changed.append(marker.name)
    config = root / 'config.json'
    if not config.exists():
        if config.is_symlink(): raise ValueError('Refusing linked configuration')
        shutil.copyfile(root / 'config.example.json', config)
        changed.append(config.name)
    # Every other module creates its private data directories on demand.
    return changed


if __name__ == '__main__':
    try:
        created = initialize()
        print('Created: '+(', '.join(created) or 'nothing; existing files kept'))
        print('Todo widget: python todo_widget.py')
        print('Chat reading: edit config.json, then follow README. Memory scanning is disabled by default.')
    except (OSError, ValueError) as exc:
        raise SystemExit('Initialization refused: '+str(exc))
