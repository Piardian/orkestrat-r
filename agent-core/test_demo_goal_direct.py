from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from run_demo_goal_direct import _docker_volume_spec, _fast_snapshot


class DirectDemoGoalTests(unittest.TestCase):
    def test_volume_spec_mounts_selected_workspace_at_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            spec = _docker_volume_spec(root)
            self.assertTrue(spec.endswith(':/workspace'))
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


if __name__ == '__main__':
    unittest.main()
