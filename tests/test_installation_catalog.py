"""Live installation discovery, legacy compatibility and zero-effect guards."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'AppImage/scripts'))
import create_catalog
import create_oci_engine


class InstallationCatalogTests(unittest.TestCase):
    def test_live_view_ignores_pinned_bundle_and_keeps_legacy_readers(self):
        with tempfile.TemporaryDirectory() as folder:
            legacy = Path(folder) / 'index.json'
            legacy.write_text(json.dumps({'applications': [{'id': 'legacy', 'title': 'Legacy'}]}))
            rows = {'applications': [{'id': 'native', 'title': 'Native', 'automatic_install_candidate': True}]}
            with patch.object(create_catalog, 'CATALOG_INDEX_PATH', legacy), \
                    patch.object(create_oci_engine, 'catalog', return_value=rows) as native, \
                    patch('subprocess.run', side_effect=AssertionError('no process')) as run:
                live = create_catalog.load_unified_catalog(installation=True)
                self.assertEqual([a['source_id'] for a in live['apps'] if a['provider'] == 'oci'], ['native'])
                self.assertEqual(live['sources']['oci']['origin'], 'engine')
                self.assertEqual(create_catalog.load_catalog_payload()['apps'][0]['id'], 'legacy')
                old = create_catalog.load_unified_catalog()
                self.assertEqual(next(a for a in old['apps'] if a['provider'] == 'oci')['source_id'], 'legacy')
                native.assert_called_once(); run.assert_not_called()

    def test_unavailable_engine_never_falls_back_to_legacy_index(self):
        for value in (None, {}, {'applications': None}, {'applications': [None]},
                      {'applications': [{'id': 'missing-title'}]}):
            with patch.object(create_oci_engine, 'catalog', return_value=value), \
                    patch.object(create_catalog, 'load_catalog_payload', side_effect=AssertionError('no fallback')) as legacy:
                result = create_catalog.load_unified_catalog(installation=True)
                self.assertFalse(result['sources']['oci']['available'])
                self.assertIsNone(result['sources']['oci']['count'])
                self.assertFalse(any(a['provider'] == 'oci' for a in result['apps']))
                legacy.assert_not_called()
        with patch.object(create_oci_engine, 'catalog', side_effect=OSError('unreadable')):
            self.assertFalse(create_catalog.load_unified_catalog(installation=True)['sources']['oci']['available'])

    def test_adapter_loads_exact_root_owned_native_helper(self):
        with patch.object(create_oci_engine, 'ENTRY', ROOT / 'oci/remote/oci_web_wizard.py'), \
                patch('os.geteuid', return_value=0):
            # The real workspace is user-owned. Substitute ownership metadata
            # only, while loading and executing the actual helper and catalog.
            original = Path.lstat
            def stat(path):
                value = original(path)
                return type('Meta', (), {'st_uid': 0, 'st_mode': value.st_mode & ~0o022})()
            with patch.object(Path, 'lstat', stat):
                result = create_oci_engine.catalog()
            self.assertTrue(result['applications'])
            self.assertTrue(all(r.get('automatic_install_candidate') and not r.get('hidden') for r in result['applications']))

    def test_unsafe_helper_is_rejected_before_any_source_read(self):
        import stat
        for uid, mode in ((1000, stat.S_IFREG | 0o644), (0, stat.S_IFLNK | 0o777),
                          (0, stat.S_IFREG | 0o666)):
            meta = type('Meta', (), {'st_uid': uid, 'st_mode': mode})()
            with patch.object(Path, 'lstat', return_value=meta), \
                    patch.object(Path, 'read_bytes', side_effect=AssertionError('no source read')) as read:
                with self.assertRaises(RuntimeError):
                    create_oci_engine.catalog()
                read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
