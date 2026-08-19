from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from run_demo_goal_direct import (
    _changed_snapshot_paths,
    _cleanup_new_runtime_artifacts,
    _docker_volume_spec,
    _fast_snapshot,
    _runtime_presence,
)


class DirectDemoGoalTests(unittest.TestCase):
    def test_volume_spec_mounts_selected_workspace_under_workspace_host(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            spec = _docker_volume_spec(root)
            self.assertTrue(spec.endswith(':/workspace/host'))
            self.assertTrue(spec.startswith(str(root)))

    def test_fast_snapshot_detects_directory_creation_without_reading_contents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = _fast_snapshot(root)
            (root / 'yahsi').mkdir()
            after = _fast_snapshot(root)
            self.assertNotIn('yahsi', before)
            self.assertIn('yahsi', after)
            self.assertEqual(after['yahsi'][0], 'dir')

    def test_changed_snapshot_reports_new_directory_but_ignores_existing_directory_mtime(self) -> None:
        before = {
            'project': ('dir', 0, 10),
            'project/app.py': ('file', 4, 10),
        }
        after = {
            'project': ('dir', 0, 99),
            'project/app.py': ('file', 4, 10),
            'yahsi': ('dir', 0, 20),
        }

        self.assertEqual(_changed_snapshot_paths(before, after), ['yahsi'])

    def test_changed_snapshot_still_reports_file_modification(self) -> None:
        before = {'app.py': ('file', 4, 10)}
        after = {'app.py': ('file', 5, 20)}
        self.assertEqual(_changed_snapshot_paths(before, after), ['app.py'])

    def test_fast_snapshot_ignores_openhands_runtime_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'conversations' / 'abc').mkdir(parents=True)
            (root / 'conversations' / 'abc' / 'event.json').write_text('{}', encoding='utf-8')
            (root / '.agent_tmp' / 'browser_observations').mkdir(parents=True)
            (root / '.agent_tmp' / 'browser_observations' / '1.json').write_text('{}', encoding='utf-8')
            (root / 'project').mkdir()
            (root / 'project' / 'real.txt').write_text('ok', encoding='utf-8')

            snap = _fast_snapshot(root)

            self.assertNotIn('conversations', snap)
            self.assertNotIn('conversations/abc/event.json', snap)
            self.assertNotIn('.agent_tmp', snap)
            self.assertNotIn('.agent_tmp/browser_observations/1.json', snap)
            self.assertIn('project/real.txt', snap)

    def test_cleanup_removes_only_runtime_artifacts_created_by_demo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.git').mkdir()
            before = _runtime_presence(root)

            (root / 'conversations').mkdir()
            (root / 'bash_events').mkdir()
            (root / '.agent_tmp').mkdir()
            _cleanup_new_runtime_artifacts(root, before)

            self.assertTrue((root / '.git').exists())
            self.assertFalse((root / 'conversations').exists())
            self.assertFalse((root / 'bash_events').exists())
            self.assertFalse((root / '.agent_tmp').exists())


if __name__ == '__main__':
    unittest.main()
