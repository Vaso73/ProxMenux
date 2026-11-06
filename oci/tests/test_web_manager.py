"""Native Menu/Web parity and zero-effect boundaries, without any Proxmox guest."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'remote')]
from proxmenux_oci import web_manager as actions, management, registry_accounts, registry_auth
import oci_web_manager as web


class ManagerStoreTests(unittest.TestCase):
    def test_native_readable_tags_are_visible_but_internal_ids_stay_internal(self):
        from proxmenux_oci.ui import ReadableTags
        ui=web.UI.__new__(web.UI);ui.operation='catalog.install';ui.initial_choice=None
        ui.question=Mock(return_value='vmbr0')
        self.assertEqual(ui.choose('Bridge',ReadableTags([('vmbr0','192.0.2.1/24')]),'vmbr0'),'vmbr0')
        self.assertTrue(ui.question.call_args.kwargs.get('show_tags'))
        ui.question.reset_mock();ui.choose('Internal',[('paste','Compose')],'paste')
        self.assertFalse(ui.question.call_args.kwargs.get('show_tags',False))
    def test_isolated_worker_entry_imports_its_transport_before_argument_guard(self):
        # systemd starts the actual entry with -I, which removes the script
        # directory from Python's import path. Module-loaded fixtures miss it.
        result = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'remote/oci_web_manager.py')],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Mock()
        self.store = web.Store(Path(self.tmp.name) / 'sessions', uid=os.geteuid(), runner=self.run, engine=ROOT)
        self.body = dict(operation='registry.add', parameters={}, request_id='a' * 32,
                         language='en', node=socket.gethostname())

    def test_get_empty_history_does_not_create_a_store_or_run_a_worker(self):
        self.assertEqual(self.store.jobs('alice'), [])
        self.assertFalse(self.store.root.exists())
        self.run.assert_not_called()

    def test_invalid_requests_have_zero_effects(self):
        variants = [dict(self.body, operation='shell'), dict(self.body, parameters={'command': 'ignored'}),
                    dict(self.body, node='different-node'), dict(self.body, request_id='not-a-key'),
                    dict(self.body, operation='instance.remove', parameters={'vmid': True}),
                    dict(self.body, operation='updates.selected', parameters={'vmids': [100, 100]}),
                    dict(self.body, operation='registry.logout', parameters={'registry': 'https://user:secret@host'})]
        for value in variants:
            with self.subTest(value=value):
                with self.assertRaises(web.Error):
                    self.store.start_operation('alice', value)
                self.assertFalse(self.store.root.exists())
        self.run.assert_not_called()

    def test_existing_native_ipv6_registry_account_is_a_valid_selection(self):
        # Native RegistryAuth accepts bracketed IPv6 authorities too. The
        # selector must not make those existing accounts impossible to use.
        parameters = {'registry': '[2001:db8::1]:5000/namespace'}
        self.assertEqual(actions.validate('registry.inspect', parameters), parameters)
        self.run.assert_not_called()

    def test_retries_are_idempotent_but_changed_intent_is_rejected(self):
        first = self.store.start_operation('alice', self.body)
        self.assertEqual(self.store.start_operation('alice', self.body)['id'], first['id'])
        self.run.assert_called_once()
        with self.assertRaises(web.Error):
            self.store.start_operation('alice', dict(self.body, operation='custom.install'))
        self.assertNotIn('request_hash', first)

    def test_jobs_answers_and_credentials_are_owner_scoped(self):
        value = self.store.start_operation('alice', self.body)
        self.assertEqual(self.store.jobs('bob'), [])
        for callback in (lambda: self.store.get('bob', value['id']),
                         lambda: self.store.answer('bob', value['id'], {'cancel': True}),
                         lambda: self.store.credentials('bob', value['id'])):
            with self.assertRaises(web.Error):
                callback()
        self.run.assert_called_once()

    def question(self, answer, execution=True):
        value = self.store.start_operation('alice', self.body)
        boundary = web.Boundary(self.store.folder(value['id']), ROOT)
        def respond(_):
            q = self.store.get('alice', value['id'])['question']
            self.store.answer('alice', value['id'], {'question_id': q['id'], 'value': answer})
        ui = web.UI(self.store, value['id'], boundary, lambda **_: None, 'registry.add', sleep=respond)
        return ui.question('review', 'Native scope', execution=execution), boundary, value['id']

    def test_declined_native_confirmation_preserves_execution_guard(self):
        approved, boundary, _ = self.question(False)
        self.assertFalse(approved)
        self.assertFalse(boundary.executing)
        self.assertFalse(boundary.effects)
        with self.assertRaises(web.Error):
            boundary.audit('subprocess.Popen', (None, ['pct', 'destroy', '100']))

    def test_native_config_defaults_and_execution_approval_are_separate(self):
        creation = {'catalog.install', 'custom.install', 'registry.install'}
        parameters = dict(vmid=100, vmids=[100], registry='registry.example', app_id='2fauth', mode='default')
        for operation, keys in actions.OPERATIONS.items():
            for default in (True, False):
                for final in (False, True):
                    with self.subTest(operation=operation, default=default, final=final):
                        body = dict(self.body, operation=operation,
                                    parameters={k: parameters[k] for k in keys}, request_id=web.transport.uuid.uuid4().hex)
                        value = self.store.start_operation('alice', body)
                        boundary = web.Boundary(self.store.folder(value['id']), ROOT)
                        seen = []
                        def respond(_):
                            question = self.store.get('alice', value['id'])['question']
                            seen.append(question)
                            self.store.answer('alice', value['id'], {'question_id': question['id'], 'value': False})
                        ui = web.UI(self.store, value['id'], boundary, lambda **_: None, operation, sleep=respond)
                        self.run.reset_mock()
                        method = ui.installation_review if final else ui.confirm
                        self.assertFalse(method('Native question', default=default))
                        execution = final or operation not in creation
                        self.assertEqual(seen[0]['default'], False if execution else default)
                        self.assertEqual(seen[0]['execution'], execution)
                        self.assertFalse(boundary.executing)
                        self.assertFalse(boundary.effects)
                        self.run.assert_not_called()

    def test_approval_alone_is_not_an_observed_guest_mutation(self):
        approved, boundary, job_id = self.question(True)
        self.assertTrue(approved)
        self.assertTrue(boundary.executing)
        folder = self.store.folder(job_id)
        boundary.audit('os.rename', (str(folder / '.write'), str(folder / 'state.json')))
        self.assertFalse(boundary.effects)

    def test_worker_dispatches_once_and_false_result_is_never_success(self):
        value = self.store.start_operation('alice', self.body)
        dispatch = Mock(return_value=False)
        web.worker(self.store, value['id'], dispatch=dispatch, install_hook=lambda _: None)
        self.assertEqual(self.store.get('alice', value['id'])['status'], 'cancelled')
        dispatch.assert_called_once()
        with self.assertRaises(web.Error):
            web.worker(self.store, value['id'], dispatch=dispatch, install_hook=lambda _: None)
        dispatch.assert_called_once()

    def test_stale_and_duplicate_answers_cannot_authorize_an_action(self):
        value = self.store.start_operation('alice', self.body)
        q = dict(id='b' * 32, kind='review', text='Scope', options=[], default=False,
                 back=False, expires_at=1, execution=True)
        self.store.change(value['id'], status='question', question=q)
        with self.assertRaises(web.Error):
            self.store.answer('alice', value['id'], {'question_id': q['id'], 'value': True})
        self.assertFalse((self.store.folder(value['id']) / 'answer.json').exists())

    def test_answer_ttl_boundaries_and_pending_duplicate_have_zero_extra_writes(self):
        value = self.store.start_operation('alice', self.body)
        q = dict(id='b' * 32, kind='review', text='Scope', options=[], default=False, back=False, expires_at=1000)
        for now in (1000, 1001):
            self.store.change(value['id'], status='question', question=q)
            with patch.object(web.transport.time, 'time', return_value=now), self.assertRaises(web.Error):
                self.store.answer('alice', value['id'], {'question_id': q['id'], 'value': True})
            self.assertFalse((self.store.folder(value['id']) / 'answer.json').exists())
        with patch.object(web.transport.time, 'time', return_value=999):
            self.store.answer('alice', value['id'], {'question_id': q['id'], 'value': False})
            before = (self.store.folder(value['id']) / 'answer.json').read_bytes()
            with self.assertRaises(web.Error):
                self.store.answer('alice', value['id'], {'question_id': q['id'], 'value': True})
            self.assertEqual((self.store.folder(value['id']) / 'answer.json').read_bytes(), before)

    def test_back_rewinds_native_wizard_without_enabling_execution(self):
        from proxmenux_oci.ui import BackRequested
        value = self.store.start_operation('alice', self.body)
        boundary = web.Boundary(self.store.folder(value['id']), ROOT)
        def reply(_):
            q = self.store.get('alice', value['id'])['question']
            self.store.answer('alice', value['id'], {'question_id': q['id'], 'value': {'back': True}})
        ui = web.UI(self.store, value['id'], boundary, lambda **_: None, 'custom.install', sleep=reply)
        ui.back_enabled = True
        with self.assertRaises(BackRequested):
            ui.ask('Native question')
        self.assertFalse(boundary.executing)
        self.assertFalse(boundary.effects)

    def test_source_change_at_final_review_refuses_permission_before_effect(self):
        value = self.store.start_operation('alice', self.body)
        boundary = web.Boundary(self.store.folder(value['id']), ROOT)
        def changed(final=False):
            if final:
                raise web.Error('source_changed')
        def reply(_):
            q = self.store.get('alice', value['id'])['question']
            self.store.answer('alice', value['id'], {'question_id': q['id'], 'value': True})
        ui = web.UI(self.store, value['id'], boundary, changed, 'registry.add', sleep=reply)
        with self.assertRaises(web.Error):
            ui.effect_review('Native scope')
        self.assertFalse(boundary.executing)
        self.assertFalse(boundary.effects)

    def test_caught_forbidden_effect_cannot_become_a_completed_operation(self):
        value = self.store.start_operation('alice', self.body)
        hooks = []
        def dispatch(*_):
            try:
                hooks[0]('subprocess.Popen', (None, ['pct', 'destroy', '100']))
            except web.Error:
                pass  # A native defensive catch must not hide the denied effect.
            return True
        web.worker(self.store, value['id'], dispatch=dispatch, install_hook=hooks.append)
        job = self.store.get('alice', value['id'])
        self.assertEqual((job['status'], job['error']), ('failed', 'configuration_effect_blocked'))


class NativeBoundaryTests(unittest.TestCase):
    def test_default_menu_keeps_carried_check_and_web_decline_stops_before_it(self):
        row = {'vmid': 100}
        for defer in (False, True):
            ui = SimpleNamespace(message=Mock())
            if defer:
                ui.requires_management_review = True
                ui.prepare_management = Mock(return_value=False)
            with patch.object(management, 'carry_records', return_value={}) as carry, \
                    patch.object(management, 'check_selected', return_value={'reason': 'identity-unconfirmed'}), \
                    patch.object(management, '_run_lifecycle') as lifecycle:
                self.assertFalse(management.manage_instance(ROOT, ui, row, action='remove'))
                self.assertEqual(carry.call_count, 0 if defer else 1)
                lifecycle.assert_not_called()

    def test_read_only_selector_uses_no_native_lock_or_configuration_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '100').mkdir()
            fake = SimpleNamespace(ROOT=root, locked=Mock(side_effect=AssertionError('no lock writes')),
                has_contract=Mock(return_value=True), guest_exists=Mock(return_value=True),
                read=Mock(return_value={'vmid': 100, 'status': 'installed', 'template': {'catalog_ui': {'title': 'App'}}}))
            before = root.stat().st_mtime_ns
            with patch.dict(sys.modules, {'oci_instances': fake}):
                self.assertEqual(management.saved_inventory(ROOT, read_only=True)[0]['vmid'], 100)
            fake.locked.assert_not_called()
            self.assertEqual(root.stat().st_mtime_ns, before)

    def test_registry_menu_and_web_share_the_same_operation_function(self):
        ui = SimpleNamespace(choose=Mock(side_effect=['add', None]), message=Mock())
        auth = SimpleNamespace(accounts=Mock(return_value=[]))
        with patch.object(registry_accounts, 'require_root'), \
                patch.object(registry_accounts, 'account_operation') as action:
            registry_accounts.registry_accounts_menu(ui, auth=auth)
        action.assert_called_once_with(ui, 'add', None, auth)

    def test_declined_registry_login_never_calls_native_login_or_rewrites_account(self):
        ui = SimpleNamespace(ask=Mock(side_effect=['ghcr.io', 'fixture-user']),
            password=Mock(return_value='fixture-secret'), info=Mock(), effect_review=Mock(return_value=False))
        auth = SimpleNamespace(login=Mock())
        with patch.object(registry_accounts, 'require_root'):
            self.assertIsNone(registry_accounts.account_operation(ui, 'add', None, auth))
        auth.login.assert_not_called()

    def test_wrong_registry_image_is_rejected_before_any_probe(self):
        ui = SimpleNamespace(ask=Mock(return_value='quay.io/example/app:latest'), effect_review=Mock())
        with patch.object(registry_accounts, 'require_root'), \
                patch.object(registry_accounts, 'inspect_registry_image') as inspect:
            with self.assertRaises(registry_auth.RegistryError):
                registry_accounts.account_operation(ui, 'inspect', {'registry': 'ghcr.io', 'account': 'fixture'}, None)
        inspect.assert_not_called()
        ui.effect_review.assert_not_called()

    def test_unknown_action_never_imports_or_dispatches_native_modules(self):
        with patch.object(actions, 'native') as load:
            with self.assertRaises(ValueError):
                actions.run_operation(ROOT, None, 'shell', {'command': 'ignored'})
        load.assert_not_called()

    def test_interrupted_status_uses_native_text_without_exposing_the_saved_plan(self):
        import oci_instances as instances
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / 'journal.json'
            ui = SimpleNamespace(message=Mock())
            for identity in (100, None, 200):
                journal.write_text(json.dumps({'vmid': identity, 'operation': 'update', 'phase': 'backup-ready',
                    'deployment': {'secret': 'never-display'}, 'backup': {'secret': 'never-display'}}))
                with patch.object(actions, 'selected', return_value={'vmid': 100}), \
                        patch.object(instances, 'read', return_value={'pending_transaction': str(journal)}), \
                        patch('subprocess.run') as command:
                    if identity != 100:
                        with self.assertRaises(ValueError):
                            actions.run_operation(ROOT, ui, 'instance.status', {'vmid': 100})
                    else:
                        self.assertTrue(actions.run_operation(ROOT, ui, 'instance.status', {'vmid': 100}))
                        text = ui.message.call_args.args[0]
                        self.assertIn('Backup created', text)
                        self.assertNotIn('never-display', text)
                    command.assert_not_called()

    def test_browser_exposes_all_eight_native_definition_sources_and_keeps_paste_in_web(self):
        from proxmenux_oci import custom
        from proxmenux_oci.ui import UserCancelled
        ui = SimpleNamespace(definition_input=True, all_definition_sources=True,
            choose=Mock(return_value=None), multiline=Mock(return_value='services: {}'))
        with self.assertRaises(UserCancelled):
            custom.read_definition(ui)
        keys = {row[0] for row in ui.choose.call_args.args[1]}
        self.assertEqual(keys, {'paste', 'file', 'url', 'run', 'image', 'oci-compose-paste', 'oci-compose', 'oci-compose-url'})
        with patch('sys.stdin') as terminal:
            self.assertEqual(custom._read_pasted(ui), 'services: {}')
        terminal.read.assert_not_called()

    def test_native_url_sources_use_the_same_bounded_reader_hook(self):
        from proxmenux_oci import custom
        for source, limit in [('url', custom.MAX_COMPOSE_BYTES), ('oci-compose-url', custom.MAX_OCI_COMPOSE_BYTES)]:
            ui = SimpleNamespace(all_definition_sources=True, choose=Mock(return_value=source),
                ask=Mock(return_value='https://example.invalid/definition'), definition_url=Mock(return_value='fixture'))
            value, _ = custom.read_definition(ui)
            self.assertEqual(value, 'fixture')
            if source == 'oci-compose-url':
                ui.definition_url.assert_called_once_with('https://example.invalid/definition', limit)
            else:
                ui.definition_url.assert_called_once_with('https://example.invalid/definition')

    def test_secondary_record_never_overwrites_primary_title_or_pending_state(self):
        rows = [dict(vmid=100, title='Database', stack=True, pending=False),
                dict(vmid=200, title='Main app', stack=True, pending=True)]
        records = {100: {'stack_member': {'primary_vmid': 200}},
                   200: {'stack': {'members': [{'vmid': 100, 'name': 'db'}, {'vmid': 200, 'name': 'app'}]}}}
        with tempfile.TemporaryDirectory() as folder:
            fake = SimpleNamespace(ROOT=Path(folder), read=lambda _, vmid: records[vmid])
            manager = SimpleNamespace(saved_inventory=Mock(return_value=rows), stack_update_supported=Mock(return_value=True))
            with patch.object(actions, 'native', return_value=manager), patch.dict(sys.modules, {'oci_instances': fake}), \
                    patch.object(registry_auth, 'RegistryAuth', side_effect=OSError()):
                value = actions.snapshot(ROOT)['installed']['items']
        self.assertEqual(len(value), 1)
        self.assertEqual((value[0]['vmid'], value[0]['title'], value[0]['pending']), (200, 'Main app', True))

    def test_declined_logout_is_not_reported_as_success(self):
        ui = SimpleNamespace(confirm=Mock(return_value=False))
        auth = SimpleNamespace(logout=Mock())
        with patch.object(registry_accounts, 'require_root'):
            self.assertIsNone(registry_accounts.account_operation(ui, 'logout', {'registry': 'ghcr.io'}, auth))
        auth.logout.assert_not_called()

    def test_snapshot_uses_saved_read_only_records_and_redacts_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = SimpleNamespace(ROOT=Path(folder), read=Mock())
            manager = SimpleNamespace(saved_inventory=Mock(return_value=[]))
            auth = Mock(); auth.accounts.return_value = [{'registry': 'ghcr.io', 'account': 'fixture',
                'method': 'token', 'status': 'Saved', 'auth': 'fixture-secret'}]
            checker = SimpleNamespace(cached=Mock(return_value={'available': [], 'checked': False, 'unknown': 0}))
            with patch.object(actions, 'native', return_value=manager), \
                    patch.object(registry_auth, 'RegistryAuth', return_value=auth), \
                    patch.dict(sys.modules, {'oci_instances': fake, 'oci_update_check': checker}), \
                    patch('subprocess.run', side_effect=AssertionError('no process')) as run:
                value = actions.snapshot(ROOT)
            self.assertNotIn('fixture-secret', json.dumps(value))
            manager.saved_inventory.assert_called_once_with(ROOT, read_only=True)
            run.assert_not_called()

    def test_inventory_error_is_unknown_not_empty(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = SimpleNamespace(ROOT=Path(folder))
            with patch.object(actions, 'native', return_value=SimpleNamespace(saved_inventory=Mock(side_effect=OSError()))), \
                    patch.dict(sys.modules, {'oci_instances': fake}), \
                    patch.object(registry_auth, 'RegistryAuth', side_effect=OSError()):
                value = actions.snapshot(ROOT)
            self.assertEqual(value['installed'], {'available': False, 'error': 'inventory_unavailable'})
            self.assertFalse(value['registries']['available'])


if __name__ == '__main__':
    unittest.main()
