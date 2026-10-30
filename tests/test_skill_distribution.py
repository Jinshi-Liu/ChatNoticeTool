"""Portable skill distribution and selective cleanup; only temporary user files."""
from pathlib import Path
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cleanup
import local_setup


class SkillDistributionTests(unittest.TestCase):
    def test_single_portable_skill_has_short_valid_utf8_frontmatter_and_guide_paths(self):
        path = ROOT / 'skills/chatnotice/SKILL.md'
        raw = path.read_bytes()
        self.assertFalse(raw.startswith(b'\xef\xbb\xbf'))
        text = raw.decode('utf-8', errors='strict')
        start, header, body = text.split('---\n', 2)
        self.assertEqual(start, '')
        fields = dict(line.split(': ', 1) for line in header.splitlines())
        self.assertEqual(set(fields), {'name', 'description'})
        self.assertEqual(fields['name'], 'chatnotice')
        self.assertIn('读取QQ/微信/已接入来源群消息', fields['description'])
        self.assertLessEqual(len(body.splitlines()), 15)
        self.assertNotRegex(text, r'[A-Za-z]:[/\\]')
        self.assertIn('`../..`', body)
        self.assertEqual((path.parent / '../..').resolve(), ROOT)
        self.assertIn('先询问项目位置', body)
        self.assertIn('AGENTS.md', body)
        for guide in ('configuration.md', 'workflow.md'):
            self.assertIn(f'<项目根>/docs/agent/{guide}', body)
            self.assertTrue((ROOT / 'docs/agent' / guide).is_file())

    def test_readme_points_to_folder_and_no_duplicate_or_zip_is_distributed(self):
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        self.assertIn('[`skills/chatnotice`](skills/chatnotice)', readme)
        self.assertIn('复制到所用 Agent 支持的 skills 目录', readme)
        self.assertFalse((ROOT / '.agents/skills/chatnotice/SKILL.md').exists())
        self.assertFalse((ROOT / 'dist/chatnotice-skill.zip').exists())
        self.assertIn('skills', cleanup.OWNED)
        self.assertEqual(cleanup.SKILL_FILE, Path('skills/chatnotice/SKILL.md'))

    def project(self, base):
        root = base / 'synthetic-project'
        root.mkdir()
        (root / 'config.example.json').write_text('{}', encoding='utf-8')
        local_setup.initialize(root)
        marker = json.loads((root / '.chatnotice-owned.json').read_text(encoding='utf-8'))
        self.assertIn('skills', marker['owned_top_level'])
        return root

    def test_all_cleanup_removes_only_known_markdown_and_preserves_local_and_other_skills(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-skill-cleanup-') as temporary:
            root = self.project(Path(temporary).resolve())
            skill = root / cleanup.SKILL_FILE
            local = skill.parent / '.local' / 'private-notes.txt'
            sibling = skill.parent / 'user-guide.md'
            other = root / 'skills/another-skill/SKILL.md'
            for path in (skill, local, sibling, other):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('synthetic ' + path.name, encoding='utf-8')
            expected = {path: path.read_bytes() for path in (local, sibling, other)}
            preview = cleanup.plan_cleanup(root, 'all')
            self.assertIn(skill, preview)
            self.assertNotIn(root / 'skills', preview)
            self.assertTrue(skill.exists())
            cleanup.perform_cleanup(root, 'all', apply=True)
            self.assertFalse(skill.exists())
            self.assertTrue(local.parent.is_dir())
            for path, data in expected.items():
                self.assertEqual(path.read_bytes(), data)

    def test_runtime_cleanup_never_touches_skill_or_local_files(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-skill-runtime-') as temporary:
            root = self.project(Path(temporary).resolve())
            skill = root / cleanup.SKILL_FILE
            local = skill.parent / '.local' / 'keep.txt'
            local.parent.mkdir(parents=True)
            skill.write_text('synthetic skill', encoding='utf-8')
            local.write_text('synthetic personal note', encoding='utf-8')
            (root / 'runtime').mkdir()
            cleanup.perform_cleanup(root, 'runtime', apply=True)
            self.assertEqual(skill.read_text(encoding='utf-8'), 'synthetic skill')
            self.assertEqual(local.read_text(encoding='utf-8'), 'synthetic personal note')

    def test_missing_owned_markdown_does_not_select_user_skill_directory(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-skill-missing-') as temporary:
            root = self.project(Path(temporary).resolve())
            local = root / 'skills/chatnotice/.local/keep.txt'
            local.parent.mkdir(parents=True)
            local.write_text('synthetic personal note', encoding='utf-8')
            self.assertEqual(cleanup.owned_skill_files(root), [])
            cleanup.perform_cleanup(root, 'all', apply=True)
            self.assertEqual(local.read_text(encoding='utf-8'), 'synthetic personal note')

    def test_linked_skill_parent_is_refused_without_touching_target(self):
        with tempfile.TemporaryDirectory(prefix='synthetic-skill-link-') as temporary:
            base = Path(temporary).resolve()
            root = self.project(base)
            outside = base / 'synthetic-outside'
            outside.mkdir()
            target = outside / 'SKILL.md'
            target.write_text('synthetic outside skill', encoding='utf-8')
            (root / 'skills').mkdir()
            link = root / 'skills/chatnotice'
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('Directory symlinks unavailable')
            try:
                with self.assertRaises(ValueError):
                    cleanup.perform_cleanup(root, 'all', apply=True)
                self.assertEqual(target.read_text(encoding='utf-8'), 'synthetic outside skill')
            finally:
                link.unlink()


if __name__ == '__main__':
    unittest.main()
