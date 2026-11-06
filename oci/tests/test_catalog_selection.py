"""One Menu/Web selector; read-only metadata never bootstraps the catalog."""
import ast
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from proxmenux_oci.catalog_selection import installable, read_catalog
from proxmenux_oci.catalog import Catalog


class SelectionTests(unittest.TestCase):
    def test_cached_web_read_never_creates_snapshot_leases(self):
        from proxmenux_oci import catalog_refresh
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);snapshot=root/'.catalog-cache/snapshots'/('a'*40)
            snapshot.mkdir(parents=True);(snapshot/'index.json').write_text('{"applications":[]}')
            before={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            with patch.object(catalog_refresh,'current_catalog',return_value=snapshot),\
                    patch.object(catalog_refresh,'pin_catalog',side_effect=AssertionError('lease writes forbidden')) as pin,\
                    patch('subprocess.run',side_effect=AssertionError('process forbidden')) as run:
                self.assertEqual(read_catalog(root)['applications'],[])
            pin.assert_not_called();run.assert_not_called()
            self.assertEqual(before,{str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()})
    def test_actual_menu_wrapper_uses_exact_shared_rule(self):
        source = ast.parse((ROOT / 'src/proxmenux_oci/cli.py').read_text())
        wrapper = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == '_installable')
        shared = Mock(wraps=installable)
        scope = {'Any': object, '_catalog_installable': shared}
        exec(compile(ast.Module(body=[wrapper], type_ignores=[]), 'actual-menu-selector', 'exec'), scope)
        rows = [{'id': 'yes', 'automatic_install_candidate': True},
                {'id': 'hidden', 'automatic_install_candidate': True, 'hidden': True},
                {'id': 'manual', 'automatic_install_candidate': False}, {'id': 'unknown'}]
        self.assertEqual(scope['_installable'](rows), [rows[0]])
        self.assertEqual(scope['_installable'](rows), installable(rows))
        self.assertEqual(shared.call_count, 2, 'Menu must delegate to the shared selector')

    def test_missing_index_cannot_sync_download_or_start_a_process(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(Catalog, 'sync_index', side_effect=AssertionError('sync forbidden')) as sync, \
                patch('socket.create_connection', side_effect=AssertionError('network forbidden')) as net, \
                patch('subprocess.run', side_effect=AssertionError('process forbidden')) as run:
            before = list(Path(folder).rglob('*'))
            with self.assertRaises(FileNotFoundError):
                read_catalog(Path(folder))
            self.assertEqual(list(Path(folder).rglob('*')), before)
            sync.assert_not_called(); net.assert_not_called(); run.assert_not_called()

    def test_real_native_reader_enrichment_is_reused_and_input_not_rewritten(self):
        rows = [{'id': 'native', 'title': 'Native', 'automatic_install_candidate': True},
                {'id': 'manual', 'title': 'Manual'}]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'catalog/index.json'; path.parent.mkdir()
            path.write_text(json.dumps({'applications': rows})); before = path.read_bytes()
            with patch.object(Catalog, '_enrich_index_from_templates', wraps=Catalog._enrich_index_from_templates) as enrich:
                # Use a small fake enrichment callback, retaining the actual
                # load_index path and verification callback on real files.
                enrich.side_effect = lambda payload: payload['applications'][0].update(title='Enriched')
                result = read_catalog(Path(folder))
                enrich.assert_called_once()
            self.assertEqual(result['applications'][0]['title'], 'Enriched')
            self.assertEqual([r['id'] for r in result['applications']], ['native'])
            self.assertEqual(path.read_bytes(), before)

    def test_malformed_partial_and_valid_empty_are_distinct(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'catalog/index.json'; path.parent.mkdir()
            for value in ('{', 'null', '{}', '{"applications":[null]}'):
                path.write_text(value)
                with self.assertRaises((ValueError, TypeError, AttributeError)):
                    read_catalog(Path(folder))
            path.write_text('{"applications":[]}')
            self.assertEqual(read_catalog(Path(folder))['applications'], [])


if __name__ == '__main__':
    unittest.main()
