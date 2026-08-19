from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import yaml

from run_demo_goal_direct import (
    _DEMO_DEFAULT_TOOLS,
    _DEMO_MAX_ITERATIONS,
    _DEMO_PROFILE_ID,
    _DEMO_RECOVERY_TOOL_NAMES,
    _DEMO_STUCK_DETECTION,
    _DEMO_STUCK_RECOVERY_ATTEMPTS,
    _DEMO_SYSTEM_MESSAGE_SUFFIX,
    _build_stuck_recovery_prompt,
    _changed_snapshot_paths,
    _cleanup_new_runtime_artifacts,
    _docker_volume_spec,
    _extract_agent_result,
    _fast_snapshot,
    _is_stuck_error,
    _runtime_presence,
)


class DirectDemoGoalTests(unittest.TestCase):
    def test_direct_demo_uses_gemini_3_5_flash_lite_with_secondary_key(self) -> None:
        self.assertEqual(_DEMO_PROFILE_ID, 'gemini-3.5-flash-lite-demo')
        profiles_path = Path(__file__).resolve().parent / 'config' / 'profiles.yaml'
        raw = yaml.safe_load(profiles_path.read_text(encoding='utf-8')) or {}
        profile = next(item for item in raw.get('profiles', []) if item.get('id') == _DEMO_PROFILE_ID)
        self.assertEqual(profile.get('provider'), 'gemini')
        self.assertEqual(profile.get('model'), 'gemini/gemini-3.5-flash-lite')
        self.assertIsNone(profile.get('base_url'))
        self.assertEqual(profile.get('secret_env'), 'GEMINI_USER_B_KEY')

    def test_google_a_and_openrouter_demo_profiles_remain_available(self) -> None:
        profiles_path = Path(__file__).resolve().parent / 'config' / 'profiles.yaml'
        raw = yaml.safe_load(profiles_path.read_text(encoding='utf-8')) or {}
        profiles = {item.get('id'): item for item in raw.get('profiles', [])}

        google_a = profiles['gemini-3.5-flash-lite-google-demo']
        self.assertEqual(google_a.get('provider'), 'gemini')
        self.assertEqual(google_a.get('model'), 'gemini/gemini-3.5-flash-lite')
        self.assertIsNone(google_a.get('base_url'))
        self.assertEqual(google_a.get('secret_env'), 'GEMINI_USER_A_KEY')

        openrouter = profiles['gemini-3.5-flash-lite-openrouter-demo']
        self.assertEqual(openrouter.get('provider'), 'openrouter')
        self.assertEqual(openrouter.get('model'), 'openrouter/google/gemini-3.5-flash-lite')
        self.assertEqual(openrouter.get('base_url'), 'https://openrouter.ai/api/v1')
        self.assertEqual(openrouter.get('secret_env'), 'OPENROUTER_API_KEY')

    def test_direct_demo_relaxes_runtime_limits_but_keeps_loop_protection(self) -> None:
        self.assertGreaterEqual(_DEMO_MAX_ITERATIONS, 10_000)
        self.assertTrue(_DEMO_STUCK_DETECTION)
        self.assertIn('10 browser actions', _DEMO_SYSTEM_MESSAGE_SUFFIX)
        self.assertIn('20 total steps', _DEMO_SYSTEM_MESSAGE_SUFFIX)
        self.assertIn('soft caps do not apply', _DEMO_SYSTEM_MESSAGE_SUFFIX)

    def test_direct_demo_avoids_think_tool_and_has_compatibility_recovery(self) -> None:
        self.assertEqual(_DEMO_DEFAULT_TOOLS, ('FinishTool',))
        self.assertNotIn('ThinkTool', _DEMO_DEFAULT_TOOLS)
        self.assertGreaterEqual(_DEMO_STUCK_RECOVERY_ATTEMPTS, 1)
        self.assertIn('think tool', _DEMO_SYSTEM_MESSAGE_SUFFIX.lower())

    def test_recovery_removes_file_editor_but_keeps_terminal(self) -> None:
        self.assertIn('TerminalTool', _DEMO_RECOVERY_TOOL_NAMES)
        self.assertIn('TaskTrackerTool', _DEMO_RECOVERY_TOOL_NAMES)
        self.assertIn('BrowserToolSet', _DEMO_RECOVERY_TOOL_NAMES)
        self.assertNotIn('FileEditorTool', _DEMO_RECOVERY_TOOL_NAMES)

    def test_stuck_error_detection_is_targeted(self) -> None:
        self.assertTrue(_is_stuck_error(RuntimeError('Remote conversation got stuck')))
        self.assertTrue(_is_stuck_error(RuntimeError('stuck pattern detected')))
        self.assertFalse(_is_stuck_error(RuntimeError('429 rate limit exceeded')))
        self.assertFalse(_is_stuck_error(RuntimeError('docker connection failed')))

    def test_recovery_prompt_continues_existing_filesystem_state(self) -> None:
        prompt = _build_stuck_recovery_prompt('Eksik CSS dosyasını oluştur')
        self.assertIn('Eksik CSS dosyasını oluştur', prompt)
        self.assertIn('CURRENT filesystem state', prompt)
        self.assertIn('/workspace/host', prompt)
        self.assertIn('think tool is intentionally unavailable', prompt)
        self.assertIn('file editor is intentionally unavailable', prompt.lower())
        self.assertIn('use the terminal tool directly', prompt.lower())

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
