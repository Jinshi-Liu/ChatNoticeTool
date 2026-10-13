"""Remove only this project's owned files. Never follow links or client data paths."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys

from chatnotice import WIDGET_MUTEX
OWNER_ID = 'chatnoticetool-owned-v1'
OWNED = frozenset({".chatnotice-owned.json", "README.md", "config.json", "chatnotice.py",
    "cleanup.py", "run.cmd", "cleanup.cmd", "tests", "research", "state", "exports",
    "runtime", "vendor", "__pycache__", "cipher.py", "memory_reader.py", "database_reader.py", "groups.py", "messages.py", "read_chats.py", "attachments.py", "document_formats.py", "widget_ui.py", "glass_style.py", "todo_detail.py", "todo_store.py", "todo_widget.py", "todo-widget.cmd", "todo.ico", "initial-todos.json", "local_setup.py", "config.example.json", "requirements.txt",
    ".gitignore", ".gitattributes", "SECURITY.md", "CONTRIBUTING.md", "NOTICE.md", "LICENSE", "CHANGELOG.md", "ARCHITECTURE.md", ".github"})
RUNTIME = frozenset({"state", "exports", "runtime", "__pycache__"})


def is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def validate_tree(path: Path) -> None:
    if is_link(path):
        raise ValueError(f"Refusing link/junction/reparse point: {path}")
    if path.is_dir():
        for child in path.iterdir():
            validate_tree(child)


def desktop_directory() -> Path:
    if sys.platform != 'win32':
        return Path.home() / 'Desktop'
    import ctypes
    from ctypes import wintypes as W
    shell = ctypes.WinDLL('shell32')
    shell.SHGetFolderPathW.argtypes = [W.HWND, ctypes.c_int, W.HANDLE, W.DWORD, W.LPWSTR]
    shell.SHGetFolderPathW.restype = ctypes.c_long
    buffer = ctypes.create_unicode_buffer(32768)
    if shell.SHGetFolderPathW(None, 0x10, None, 0, buffer) != 0:
        raise ValueError('Cannot resolve the current desktop; refusing external cleanup')
    return Path(buffer.value).resolve()


def owned_shortcuts(root: Path, marker: dict) -> list[Path]:
    targets = []
    for change in marker.get('external_changes', []):
        if change.get('kind') != 'desktop_shortcut':
            continue
        path = Path(change['path'])
        if (path.parent.resolve() != desktop_directory() or
                not path.name.startswith('群消息待办') or path.suffix.lower() != '.lnk' or
                os.path.normcase(change.get('widget', '')) != os.path.normcase(str(root / 'todo_widget.py'))):
            raise ValueError('External shortcut ownership/path is invalid')
        if not path.exists():
            continue
        validate_tree(path)
        if not path.is_file() or path.stat().st_size > 1024*1024:
            raise ValueError('External shortcut is not a regular small file')
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != change.get('sha256'):
            print(f'Preserving modified desktop shortcut: {path}', file=sys.stderr)
            continue
        targets.append(path)
    return targets


def require_widget_closed(root: Path) -> None:
    if sys.platform != 'win32' or root.resolve() != Path(__file__).resolve().parent:
        return
    import ctypes
    from ctypes import wintypes as W
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenMutexW.argtypes = [W.DWORD, W.BOOL, W.LPCWSTR]
    kernel.OpenMutexW.restype = W.HANDLE
    handle = kernel.OpenMutexW(0x100000, False, WIDGET_MUTEX)
    if handle:
        kernel.CloseHandle.argtypes = [W.HANDLE]
        kernel.CloseHandle(handle)
        raise ValueError('请先关闭桌面待办小组件，再执行清理（清理会删除勾选进度）。')
    if ctypes.get_last_error() == 5:
        raise ValueError('Cannot verify widget exit; refusing cleanup')


def plan_cleanup(root: Path, mode: str) -> list[Path]:
    if mode not in ("runtime", "all"):
        raise ValueError("Unknown cleanup mode")
    if is_link(root) or str(root.resolve()) != str(root.absolute()):
        raise ValueError("Project root must not be a link or redirected path")
    marker = json.loads((root / ".chatnotice-owned.json").read_text(encoding="utf-8"))
    if marker.get("owner_id") != OWNER_ID or marker.get("project") != "ChatNoticeTool":
        raise ValueError("Project ownership marker does not match")
    if os.path.normcase(str(root.resolve())) != os.path.normcase(marker.get("root", "")):
        raise ValueError("Project ownership path does not match")
    names = OWNED if mode == "all" else RUNTIME
    targets = [p for p in root.iterdir() if p.name in names]
    for path in targets:
        validate_tree(path)
    if mode == 'all':
        targets += owned_shortcuts(root, marker)
    # Keep the ownership marker until all other removals succeed.
    return sorted(targets, key=lambda p: (p.name == ".chatnotice-owned.json", p.name))


def perform_cleanup(root: Path, mode: str, apply: bool = False) -> list[Path]:
    targets = plan_cleanup(root, mode)
    if not apply:
        return targets
    require_widget_closed(root)
    for target in targets:
        validate_tree(target)
        if target.parent != root:
            # Re-check the registered shortcut hash immediately before deletion.
            marker = json.loads((root / '.chatnotice-owned.json').read_text(encoding='utf-8'))
            if target not in owned_shortcuts(root, marker):
                continue
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    if mode == "all" and not any(root.iterdir()):
        root.rmdir()
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely remove ChatNoticeTool only")
    parser.add_argument("mode", choices=["runtime", "all"], nargs="?", default="runtime")
    parser.add_argument("--apply", action="store_true", help="Actually remove files")
    parser.add_argument("--yes", action="store_true", help="Explicit non-interactive confirmation")
    parser.add_argument("--pause", action="store_true", help="Keep the standalone cleanup window open")
    args = parser.parse_args()
    root = Path(__file__).absolute().parent
    try:
        targets = plan_cleanup(root, args.mode)
        print("Deletion preview:" if not args.apply else "Files selected for removal:")
        for target in targets:
            print(target)
        if not args.apply:
            print("Nothing deleted. Add --apply to remove the listed files.")
            return 0
        if not args.yes:
            print("This deletes local exports, caches, keys and todo check-off progress. Close the widget first.")
            if input("Type DELETE to confirm: ").strip() != "DELETE":
                print("Cancelled")
                return 1
        perform_cleanup(root, args.mode, apply=True)
        print("Done. Original QQ/WeChat data and the existing Python installation were not touched.")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Cleanup refused or incomplete: {exc}", file=sys.stderr)
        return 2
    finally:
        if args.pause and sys.stdin.isatty():
            input("Press Enter to close this window...")


if __name__ == "__main__":
    raise SystemExit(main())
