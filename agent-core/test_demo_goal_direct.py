from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import yaml

from run_demo_goal_direct import (
    _DEMO_MAX_ITERATIONS,
    _DEMO_PROFILE_ID,
    _DEMO_STUCK_DETECTION,
    _DEMO_SYSTEM_MESSAGE_SUFFIX,
    _changed_snapshot_paths,
    _cleanup_new_runtime_artifacts,
    _docker_volume_spec,
    _extract_agent_result,
    _fast_snapshot,
    _runtime_presence,
)


class DirectDemoGoalTests(unittest.TestCase):
    def test_direct_demo_uses_gemini_3_5_flash_lite_profile(self) -> None:
        self.assertEqual(_DEMO_PROFILE_ID, 'gemini-3.5-flash-lite-demo')
        profiles_path = Path(__file__).resolve().parent / 'config' / 'profiles.yaml'
        raw = yaml.safe_load(profiles_path.read_text(encoding='utf-8')) or {}
        profile = next(item for item in raw.get('profiles', []) if item.get('id') == _DEMO_PROFILE_ID)
        self.assertEqual(profile.get('provider'), 'gemini')
        self.assertEqual(profile.get('model'), 'gemini/gemini-3.5-flash-lite')
        self.assertIsNone(profile.get('base_url'))
        self.assertEqual(profile.get('secret_env'), 'GEMINI_USER_A_KEY')

    def test_direct_demo_relaxes_runtime_limits_for_mvp(self) -> None:
        self.assertGreaterEqual(_DEMO_MAX_ITERATIONS, 10_000)
        self.assertFalse(_DEMO_STUCK_DETECTION)
        self.assertIn('10 browser actions', _DEMO_SYSTEM_MESSAGE_SUFFIX)
        self.assertIn('20 total steps', _DEMO_SYSTEM_MESSAGE_SUFFIX)
        self.assertIn('soft caps do not apply', _DEMO_SYSTEM_MESSAGE_SUFFIX)

    def test_direct_demo_requires_full_finish_output(self) -> None:
        self.assertIn('entire user-facing answer', _DEMO_SYSTEM_MESSAGE_SUFFIX)
        self.assertIn('actual requested result', _DEMO_SYSTEM_MESSAGE_SUFFIX)

    def test_extract_agent_result_prefers_finish_action_message(self) -> None:
        finish = SimpleNamespace(
            source='agent',
            tool_name='finish',
            action=SimpleNamespace(message='Tam final rapor'),
        )
        fallback = SimpleNamespace(role='assistant', content='Eski kısa mesaj')

        self.assertEqual(_extract_agent_result([finish], [fallback]), 'Tam final rapor')

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
