from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from run_demo_goal_direct import (
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

    def test_fast_snapshot_ignores_openhands_runtime_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'conversations' / 'abc').mkdir(parents=True)
            (root / 'conversations' / 'abc' / 'event.json').write_text('{}', encoding='utf-8')
            (root / 'project').mkdir()
            (root / 'project' / 'real.txt').write_text('ok', encoding='utf-8')

            snap = _fast_snapshot(root)

            self.assertNotIn('conversations', snap)
            self.assertNotIn('conversations/abc/event.json', snap)
            self.assertIn('project/real.txt', snap)

    def test_cleanup_removes_only_runtime_artifacts_created_by_demo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.git').mkdir()
            before = _runtime_presence(root)

            (root / 'conversations').mkdir()
            (root / 'bash_events').mkdir()
            _cleanup_new_runtime_artifacts(root, before)

            self.assertTrue((root / '.git').exists())
            self.assertFalse((root / 'conversations').exists())
            self.assertFalse((root / 'bash_events').exists())


if __name__ == '__main__':
    unittest.main()
