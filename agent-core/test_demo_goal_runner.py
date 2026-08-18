from __future__ import annotations

from pathlib import Path
import tarfile
import tempfile
import unittest

from run_demo_goal import (
    _is_protected_relative,
    _safe_extract,
    _snapshot,
    _sync_from_extracted,
)


class DemoGoalRunnerTests(unittest.TestCase):
    def test_protected_paths_are_never_host_synced(self) -> None:
        self.assertTrue(_is_protected_relative(Path('.env')))
        self.assertTrue(_is_protected_relative(Path('.git/config')))
        self.assertTrue(_is_protected_relative(Path('.ssh/id_rsa')))
        self.assertTrue(_is_protected_relative(Path('docker/config.json')))
        self.assertFalse(_is_protected_relative(Path('merhaba.txt')))

    def test_sync_can_create_update_and_delete_regular_demo_files(self) -> None:
        with tempfile.TemporaryDirectory() as host_tmp, tempfile.TemporaryDirectory() as src_tmp:
            host = Path(host_tmp)
            src = Path(src_tmp)

            (host / 'keep.txt').write_text('old', encoding='utf-8')
            (host / 'delete.txt').write_text('remove me', encoding='utf-8')
            (host / '.env').write_text('SECRET=preserve', encoding='utf-8')

            (src / 'keep.txt').write_text('new', encoding='utf-8')
            (src / 'created').mkdir()
            (src / 'created' / 'merhaba.txt').write_text('Merhaba', encoding='utf-8')

            _sync_from_extracted(src, host)

            self.assertEqual((host / 'keep.txt').read_text(encoding='utf-8'), 'new')
            self.assertFalse((host / 'delete.txt').exists())
            self.assertEqual((host / 'created' / 'merhaba.txt').read_text(encoding='utf-8'), 'Merhaba')
            self.assertEqual((host / '.env').read_text(encoding='utf-8'), 'SECRET=preserve')

    def test_safe_extract_ignores_escape_and_secret_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_path = root / 'result.tar.gz'
            source_file = root / 'payload.txt'
            source_file.write_text('ok', encoding='utf-8')

            with tarfile.open(archive_path, 'w:gz') as archive:
                archive.add(source_file, arcname='merhaba.txt')
                archive.add(source_file, arcname='.env')
                archive.add(source_file, arcname='../escape.txt')

            destination = root / 'extract'
            destination.mkdir()
            _safe_extract(archive_path, destination)

            self.assertEqual((destination / 'merhaba.txt').read_text(encoding='utf-8'), 'ok')
            self.assertFalse((destination / '.env').exists())
            self.assertFalse((root / 'escape.txt').exists())

    def test_snapshot_tracks_regular_files_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'a.txt').write_text('a', encoding='utf-8')
            (root / '.env').write_text('SECRET=x', encoding='utf-8')
            (root / '.venv').mkdir()
            (root / '.venv' / 'cache.bin').write_bytes(b'x')

            snap = _snapshot(root)
            self.assertEqual(list(snap), ['a.txt'])


if __name__ == '__main__':
    unittest.main()
