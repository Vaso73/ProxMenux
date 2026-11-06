"""Existing wizard/dispatcher through the web UI transport; no host lifecycle."""
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'remote')]
spec = importlib.util.spec_from_file_location('engine_web_fixture', ROOT / 'remote/oci_web_wizard.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        self.store = web.Store(Path(self.tmp.name) / 'sessions', uid=os.geteuid(),
                               runner=lambda args: self.calls.append(args), engine=ROOT)
        self.body = dict(app_id='bentopdf', request_id='a' * 32, mode='default', language='en', node=socket.gethostname())

    def start(self):
        return self.store.start('owner', self.body)

    def question(self, kind='ask'):
        job = self.start()
        question = dict(id='b' * 32, kind=kind, text='Fixture', options=[['a', 'A']],
                        default=None, back=False, expires_at=int(web.time.time()) + 900)
        self.store.change(job['id'], status='question', question=question)
        return job['id'], question

    def test_submit_and_refresh_use_one_engine_worker_without_installer_command(self):
        first = self.start()
        self.assertEqual(self.start()['id'], first['id'])
        self.body['request_id'] = 'c' * 32
        self.assertEqual(self.start()['id'], first['id'])
        self.assertEqual(self.store.current('owner', 'bentopdf')['id'], first['id'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][-2:], ['--worker', first['id']])
        self.assertNotIn('pvesh', ' '.join(self.calls[0]))

    def test_wrong_node_and_malformed_request_have_zero_process_or_store_effects(self):
        self.body['node'] = 'different-node'
        with self.assertRaises(web.Error): self.start()
        self.assertFalse(self.store.root.exists())
        self.assertEqual(self.calls, [])

    def test_current_get_does_not_create_store(self):
        self.assertIsNone(self.store.current('owner', 'bentopdf'))
        self.assertFalse(self.store.root.exists())

    def test_observation_clock_is_server_owned_and_never_changes_private_state(self):
        job = self.start()
        state = self.store.folder(job['id']) / 'state.json'
        before = state.read_bytes()
        with patch.object(web.time, 'time', return_value=1234567.25):
            self.assertEqual(self.store.get('owner', job['id'])['server_time'], 1234567.25)
        self.assertEqual(state.read_bytes(), before)
        self.assertNotIn('server_time', json.loads(before))

    def test_menu_compatible_process_umask_keeps_transport_files_private(self):
        original = os.umask(0o022)
        try:
            job = self.start()
            self.assertIn('--property=UMask=0022', self.calls[0])
            process_mask = int(next(v.split('=')[-1] for v in self.calls[0] if v.startswith('--property=UMask=')), 8)
            archive = Path(self.tmp.name) / 'native-reader-fixture.tar'
            subprocess.run([sys.executable, '-I', '-B', '-c',
                            'import sys;open(sys.argv[1], "wb").write(b"fixture")', str(archive)],
                           umask=process_mask, check=True)
            self.assertEqual(archive.stat().st_mode & 0o777, 0o644)
            folder = self.store.folder(job['id'])
            self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
            for path in self.store.root.rglob('*'):
                if path.is_file():
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600, path.name)
        finally:
            os.umask(original)

    def test_brief_answer_lock_contention_waits_without_failing_worker(self):
        job = self.start()
        folder = self.store.folder(job['id'])
        locked, release = threading.Event(), threading.Event()
        def holder():
            with self.store.lock(folder):
                locked.set()
                release.wait(2)
        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(locked.wait(1))
        timer = threading.Timer(0.05, release.set)
        timer.start()
        try:
            self.store.change(job['id'], status='configuring')
            self.assertEqual(self.store.get('owner', job['id'])['status'], 'configuring')
        finally:
            release.set(); thread.join(); timer.join()

    def test_persistent_lock_contention_is_bounded_and_preserves_state(self):
        job = self.start()
        folder = self.store.folder(job['id'])
        before = (folder / 'state.json').read_bytes()
        with self.store.lock(folder):
            started = time.monotonic()
            with self.assertRaisesRegex(web.Error, 'busy'):
                self.store.change(job['id'], status='configuring')
            self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((folder / 'state.json').read_bytes(), before)

    def test_changed_request_is_not_reinterpreted(self):
        self.start()
        self.body['mode'] = 'advanced'
        with self.assertRaisesRegex(web.Error, 'request_changed'): self.start()
        self.assertEqual(len(self.calls), 1)

    def test_owner_denied_before_answer_or_unit_probe(self):
        job_id, q = self.question()
        before = (self.store.folder(job_id) / 'state.json').read_bytes()
        self.calls.clear()
        with self.assertRaisesRegex(web.Error, 'not_found'):
            self.store.answer('different-owner', job_id, dict(question_id=q['id'], value='x'))
        self.assertEqual(self.calls, [])
        self.assertEqual((self.store.folder(job_id) / 'state.json').read_bytes(), before)
        self.assertFalse((self.store.folder(job_id) / 'answer.json').exists())

    def test_duplicate_answer_and_stale_question_never_replace_pending_reply(self):
        job_id, q = self.question()
        self.store.answer('owner', job_id, dict(question_id=q['id'], value='first'))
        with self.assertRaisesRegex(web.Error, 'answer_pending'):
            self.store.answer('owner', job_id, dict(question_id=q['id'], value='second'))
        self.assertEqual(web.read(self.store.folder(job_id) / 'answer.json', self.store.uid)['value'], 'first')

    def test_stale_and_wrong_kind_answers_have_zero_writes(self):
        job_id, q = self.question('confirm')
        for body in [dict(question_id='c' * 32, value=True), dict(question_id=q['id'], value=1)]:
            with self.assertRaises(web.Error): self.store.answer('owner', job_id, body)
            self.assertFalse((self.store.folder(job_id) / 'answer.json').exists())
        q['expires_at'] = int(web.time.time()) - 1
        self.store.change(job_id, question=q)
        with self.assertRaisesRegex(web.Error, 'question_expired'):
            self.store.answer('owner', job_id, dict(question_id=q['id'], value=True))

    def test_installing_cannot_cancel_or_answer(self):
        job_id, _ = self.question()
        self.store.change(job_id, status='installing', question=None)
        with self.assertRaises(web.Error): self.store.answer('owner', job_id, {'cancel': True})
        self.assertFalse((self.store.folder(job_id) / 'answer.json').exists())

    def test_private_answer_is_not_in_public_state_or_argv(self):
        job_id, q = self.question('password')
        secret = 'fixture-private-value'
        self.store.answer('owner', job_id, dict(question_id=q['id'], value=secret))
        self.assertNotIn(secret, json.dumps(self.store.get('owner', job_id)))
        self.assertNotIn(secret, str(self.calls))
        self.assertEqual((self.store.folder(job_id) / 'answer.json').stat().st_mode & 0o777, 0o600)

    def test_required_empty_reply_does_not_pop_previous_engine_answers(self):
        job_id, q = self.question('password')
        q['required'] = True
        self.store.change(job_id, question=q)
        with self.assertRaisesRegex(web.Error, 'invalid_answer'):
            self.store.answer('owner', job_id, dict(question_id=q['id'], value=''))
        self.assertFalse((self.store.folder(job_id) / 'answer.json').exists())
        self.assertEqual(self.store.get('owner', job_id)['question']['id'], q['id'])

    def test_symlink_store_or_state_is_rejected(self):
        job = self.start()
        path = self.store.folder(job['id']) / 'state.json'
        target = path.with_name('real.json')
        path.rename(target); path.symlink_to(target)
        with self.assertRaisesRegex(web.Error, 'unsafe_store'): self.store.get('owner', job['id'])

    def test_uncertain_launch_never_automatically_replays(self):
        self.store.run = Mock(side_effect=subprocess.TimeoutExpired('systemd-run', 15))
        result = self.start()
        self.assertEqual(result['status'], 'recovery_required')
        self.body['request_id'] = 'f' * 32
        self.assertEqual(self.start()['id'], result['id'])
        self.assertEqual(self.store.run.call_count, 1)

    def test_dead_worker_is_uncertain_and_cannot_be_replaced_by_new_request(self):
        job = self.start()
        self.store.change(job['id'], status='installing', question=None)
        path = self.store.folder(job['id']) / 'state.json'
        value = web.read(path, self.store.uid); value['updated_at'] -= 60; web.write(path, value)
        self.store.run = Mock(side_effect=subprocess.CalledProcessError(3, 'systemctl'))
        self.body['request_id'] = 'e' * 32
        self.assertEqual(self.start()['status'], 'recovery_required')
        self.assertTrue(all(c.args[0][0] == '/usr/bin/systemctl' for c in self.store.run.call_args_list))

    def test_result_allows_full_stack_or_independent_suite_and_hides_credentials(self):
        for result in [dict(vmid=107), dict(stack_vmids={'app': 107, 'db': 109})]:
            public = web.public_result(dict(result, credentials=[{'password': 'private'}]))
            self.assertNotIn('private', json.dumps(public))
            self.assertTrue(public['credentials_available'])
        for result in [None, {}, {'vmid': True}, {'vmid': 107, 'stack_vmids': {'db': '109'}}]:
            with self.assertRaises(web.Error): web.public_result(result)

    def test_timeout_boundary_does_not_kill_retry_or_mutate_and_late_result_can_arrive(self):
        job = self.start()
        self.store.change(job['id'], status='installing', question=None, execution_deadline=1000)
        path = self.store.folder(job['id']) / 'state.json'
        before = path.read_bytes()
        self.calls.clear()
        for now, expected in [(999, 'installing'), (1000, 'recovery_required'), (1001, 'recovery_required')]:
            with patch.object(web.time, 'time', return_value=now):
                self.assertEqual(self.store.get('owner', job['id'])['status'], expected)
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.calls, [])
        self.store.change(job['id'], status='completed', result={'vmid': 107})
        self.assertEqual(self.store.get('owner', job['id'])['status'], 'completed')

    def test_guard_failure_is_not_a_retryable_bad_user_value(self):
        self.assertFalse(issubclass(web.Error, ValueError))

    def test_cached_catalog_policy_change_invalidates_revision(self):
        engine = Path(self.tmp.name) / 'engine'
        (engine / 'catalog').mkdir(parents=True)
        policy = engine / 'catalog/volume-policy.json'
        policy.write_text('{"size":1}')
        before = web.revision(engine)
        policy.write_text('{"size":2}')
        self.assertNotEqual(web.revision(engine), before)

    def run_fake_worker(self, *, stale=False, metadata=True, result=None):
        from proxmenux_oci import host
        self.store.engine = Path(self.tmp.name) / 'engine'
        (self.store.engine / 'catalog').mkdir(parents=True)
        policy = self.store.engine / 'catalog/policy.json'
        policy.write_text('{}')
        job = self.start()
        dispatched = Mock(return_value=result)
        catalog = SimpleNamespace(compose=lambda name: {'compatibility': {'automatic_install_candidate': True}})
        ui_type = web.WebUI
        def reply(_):
            question = self.store.get('owner', job['id'])['question']
            self.store.answer('owner', job['id'], {'question_id': question['id'], 'value': True})
            if stale: policy.write_text('{"changed":true}')
        def install(ui, template, app_id, mode):
            if ui.installation_review('Fixture final review'): return dispatched()
        cli = SimpleNamespace(external_services_notice=lambda template: None, install_template=install)
        with patch.object(host, 'storages', return_value=[{'storage': 'fixture'}] if metadata else []), \
                patch.object(host, 'bridges', return_value=[{'iface': 'fixture'}]), \
                patch.object(web, 'WebUI', side_effect=lambda *a, **k: ui_type(*a, **k, sleep=reply)):
            web.worker(self.store, job['id'], load=lambda: (cli, catalog), install_hook=lambda hook: None)
        return self.store.get('owner', job['id']), dispatched

    def test_changed_policy_at_confirmation_never_dispatches(self):
        job, dispatched = self.run_fake_worker(stale=True)
        self.assertEqual(job['status'], 'failed', job)
        self.assertEqual(job['error'], 'source_changed')
        dispatched.assert_not_called()

    def test_missing_host_metadata_never_reaches_review_or_dispatch(self):
        job, dispatched = self.run_fake_worker(metadata=False)
        self.assertEqual(job['status'], 'failed', job)
        self.assertEqual(job['error'], 'host_metadata_unavailable')
        dispatched.assert_not_called()

    def test_missing_result_after_approval_is_uncertain_and_not_replayed(self):
        job, dispatched = self.run_fake_worker(result=None)
        self.assertEqual(job['status'], 'recovery_required', job)
        self.assertEqual(job['error'], 'result_missing')
        dispatched.assert_called_once()
        self.body['request_id'] = 'e' * 32
        self.assertEqual(self.start()['id'], job['id'])
        dispatched.assert_called_once()


class BoundaryTests(unittest.TestCase):
    def test_custom_registry_inspection_is_read_only_and_opt_in(self):
        guard = web.ConfigurationBoundary('/tmp/session', registry_inspection=True)
        for tail in [['inspect', 'docker://nginx:alpine'], ['inspect', '--raw', 'docker://nginx:alpine'],
                     ['inspect', '--config', 'docker://docker.io/library/nginx:alpine']]:
            guard.audit('subprocess.Popen', ('skopeo', ['skopeo', *tail], None, None))
        for tail in [['copy', 'docker://nginx:alpine', 'dir:/tmp/out'], ['login', 'example.invalid'],
                     ['inspect', '--authfile', '/etc/shadow'], ['inspect', 'file:///etc/shadow'],
                     ['inspect', 'docker://user:secret@example.invalid/image']]:
            with self.subTest(tail=tail), self.assertRaises(web.Error):
                guard.audit('subprocess.Popen', ('skopeo', ['skopeo', *tail], None, None))
        with self.assertRaises(web.Error):
            web.ConfigurationBoundary('/tmp/session').audit('subprocess.Popen', ('skopeo', ['skopeo', 'inspect', 'docker://nginx:alpine'], None, None))

    def setUp(self):
        self.guard = web.ConfigurationBoundary(Path('/tmp/owned-engine-session'))

    def test_mutating_commands_are_refused_before_process_launch(self):
        for args in [['pvesh', 'create', '/nodes/local/lxc'], ['apt-get', 'update'],
                     ['bash', 'install_oci.sh'], ['skopeo', 'copy', 'x', 'y'], ['pct', 'start', '106']]:
            with self.subTest(args=args), self.assertRaises(web.Error):
                self.guard.audit('subprocess.Popen', (args[0], args, None, None))

    def test_only_qualified_host_read_commands_are_allowed(self):
        for args in [['pvesh', 'get', '/nodes/localhost/storage', '--output-format', 'json'], ['lsusb']]:
            self.guard.audit('subprocess.Popen', (args[0], args, None, None))
        with self.assertRaises(web.Error):
            self.guard.audit('subprocess.Popen', ('pvesh', ['pvesh', 'get', '/cluster/unknown'], None, None))

    def test_outside_writes_network_and_shell_are_blocked(self):
        for event, args in [('open', ('/etc/pve/106.conf', 'w', os.O_WRONLY)),
                            ('os.mkdir', ('/tmp/different', 0o700, -1)),
                            ('socket.connect', (None, ('registry', 443))), ('os.system', ('true',))]:
            with self.subTest(event=event), self.assertRaises(web.Error): self.guard.audit(event, args)
        self.guard.audit('open', ('/tmp/owned-engine-session/state.json', 'w', os.O_WRONLY))

    def test_real_python_audit_hook_refuses_child_before_any_effect(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'must-not-exist'
            code = (
                'import importlib.util,subprocess,sys;from pathlib import Path;'
                's=importlib.util.spec_from_file_location("w",sys.argv[1]);w=importlib.util.module_from_spec(s);s.loader.exec_module(w);'
                'g=w.ConfigurationBoundary(Path(sys.argv[2]));sys.addaudithook(g.audit);'
                '\ntry:subprocess.run(["bash","-c","touch "+sys.argv[3]],check=True)'
                '\nexcept w.Error:pass'
                '\nelse:raise SystemExit(9)'
                '\nfinally:g.executing=True'
            )
            subprocess.run([sys.executable, '-I', '-B', '-c', code, str(ROOT / 'remote/oci_web_wizard.py'),
                            str(Path(folder) / 'owned'), str(target)], check=True)
            self.assertFalse(target.exists())


class ExistingEngineIntegration(unittest.TestCase):
    """Calls the real engine wizard and its real family dispatcher."""
    setUp = TransportTests.setUp
    start = TransportTests.start
    def exercise(self, app_id, expected_script, approve=True, mode='default', answer=None, definition=None, source='paste', preview=True):
        from proxmenux_oci import cli, installer
        from proxmenux_oci.catalog import Catalog
        self.body['app_id'] = app_id
        self.body['mode'] = mode
        job = (self.store.start_custom('owner', {k: self.body[k] for k in ('request_id', 'language', 'node')})
               if definition is not None else self.start())
        native = []
        questions = []
        original_ui = web.WebUI

        def reply(_):
            current = self.store.get('owner', job['id'])
            q = current['question']; questions.append(q)
            self.assertLess(len(questions), 180, 'configuration must not loop')
            if q['kind'] == 'review':
                self.assertEqual(native, [])
                value = approve if q.get('execution') else preview
            elif q['kind'] == 'multiline': value = definition
            elif q['kind'] == 'password': value = 'fixture-private-password-32bytes'
            elif q['kind'] == 'message': value = True
            elif q['kind'] == 'choose': value = q['default'] or q['options'][0][0]
            else: value = q['default'] if q['default'] is not None else ''
            if definition is not None and q['kind'] == 'choose' and any(v[0] == 'paste' for v in q['options']): value = source
            if definition is not None and q['kind'] == 'ask' and 'Image reference' in q['text']: value = definition
            if answer is not None:
                value = answer(q, value)
            self.store.answer('owner', job['id'], dict(question_id=q['id'], value=value))

        def stream(args, *a, **kw):
            native.append(args)
            self.assertEqual(Path(args[1]).name, expected_script)
            plan = json.loads(Path(args[3]).read_text())
            result = {'vmid': 107, 'urls': []}
            if plan.get('deployment_kind'):
                result['stack_vmids'] = {s['name']: 107 + i for i, s in enumerate(plan.get('services') or [{'name': 'app'}, {'name': 'database'}])}
            return result

        def run(project, template, plan, host):
            return installer._run_remote_install(project, _template_file(template), plan, 'local')

        temp_templates = []
        def _template_file(template):
            target = Path(self.tmp.name) / ('template-' + str(len(temp_templates)) + '.json')
            target.write_text(json.dumps(template)); temp_templates.append(target)
            return target

        stores = [dict(storage='local', type='dir', avail=100 * 2**30)]
        patches = [
            patch.object(web, 'WebUI', side_effect=lambda *a, **k: original_ui(*a, **k, sleep=reply)),
            patch.object(cli, 'run_remote_install', side_effect=run),
            patch.object(installer, '_stream_process', side_effect=stream),
            patch.object(installer.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='{"config":{"ExposedPorts":{"80/tcp":{}},"Entrypoint":["nginx"]}}' if definition is not None else '', stderr='')),
            patch('proxmenux_oci.installer.host.storages', return_value=stores),
            patch('proxmenux_oci.installer.host.bridges', return_value=[dict(iface='vmbr0', cidr='192.0.2.1/24')]),
            patch('proxmenux_oci.installer.host.timezone', return_value='Etc/UTC'),
            patch('proxmenux_oci.installer.host.gpus', return_value={'intel': [], 'amd': [], 'nvidia': False}),
            patch('proxmenux_oci.installer.access.ask_addresses', side_effect=lambda ui, bridge, names, *a, **k: ({name: 'dhcp' for name in names}, None)),
            patch('proxmenux_oci.management.carry_records', return_value={}),
            patch('proxmenux_oci.management.set_watchdog', return_value=True),
            patch.object(cli.images, 'offer_removal', return_value=(False, None)),
            patch.object(cli, '_print_installation_summary'),
            patch.object(cli.console, 'wait_for_enter'),
        ]
        from contextlib import ExitStack
        with ExitStack() as stack:
            for item in patches: stack.enter_context(item)
            if definition is not None:
                stack.enter_context(patch('proxmenux_oci.custom.registry_report', return_value=(True, 'Fixture accessible image')))
            web.worker(self.store, job['id'], load=lambda: (cli, Catalog(ROOT)), install_hook=lambda hook: None)
        result = self.store.get('owner', job['id'])
        self.assertEqual(result['status'], 'completed' if approve and preview else 'cancelled', result)
        self.assertEqual(len(native), int(approve and preview))
        self.assertFalse((self.store.folder(job['id']) / 'answer.json').exists())
        return result, questions

    def test_custom_compose_and_image_and_run_share_existing_dispatch_and_two_reviews(self):
        cases = [('paste', 'services:\n  app:\n    image: nginx:alpine\n    ports: ["80:80"]\n', 'install_oci.sh'),
                 ('paste', 'services:\n  app:\n    image: nginx:alpine\n    ports: ["80:80"]\n    depends_on: [redis]\n  redis:\n    image: redis:alpine\n', 'install_generic_stack.sh'),
                 ('run', 'docker run -p 80:80 nginx:alpine', 'install_oci.sh'),
                 ('image', 'nginx:alpine', 'install_oci.sh')]
        cases.append(('oci-compose-paste', (ROOT / 'catalog/apps/bentopdf.json').read_text(), 'install_oci.sh'))
        for source, definition, script in cases:
            with self.subTest(source=source, script=script):
                self.body['request_id'] = web.uuid.uuid4().hex
                result, questions = self.exercise('custom-image', script, definition=definition, source=source)
                self.assertEqual(result['operation'], 'custom')
                self.assertEqual([q['execution'] for q in questions if q['kind'] == 'review'], [False, True])
                self.assertTrue(all(q['default'] is None for q in questions if q['kind'] == 'multiline'))

    def test_custom_preview_decline_and_final_decline_have_zero_native_dispatches(self):
        for preview, approve in [(False, True), (True, False)]:
            with self.subTest(preview=preview):
                self.body['request_id'] = web.uuid.uuid4().hex
                self.exercise('custom-image', 'install_oci.sh', preview=preview, approve=approve,
                              definition='services:\n  app:\n    image: nginx:alpine\n    ports: ["80:80"]\n')

    def test_single_and_every_multi_container_family_use_existing_dispatch(self):
        for app, script in [('bentopdf', 'install_oci.sh'), ('linkwarden', 'install_generic_stack.sh'),
                            ('immich', 'install_immich_stack.sh'), ('nextcloud-stack', 'install_nextcloud_stack.sh'),
                            ('paperless-ngx', 'install_paperless_stack.sh'), ('tandoor', 'install_tandoor_stack.sh'),
                            ('suite-arr', 'install_generic_stack.sh')]:
            with self.subTest(app=app):
                self.body['request_id'] = web.uuid.uuid4().hex
                self.exercise(app, script)

    def test_declined_final_review_never_calls_installer(self):
        self.exercise('bentopdf', 'install_oci.sh', approve=False)

    def test_advanced_mode_for_every_existing_family_keeps_original_dispatch(self):
        for app, script in [('bentopdf', 'install_oci.sh'), ('linkwarden', 'install_generic_stack.sh'),
                            ('immich', 'install_immich_stack.sh'), ('nextcloud-stack', 'install_nextcloud_stack.sh'),
                            ('paperless-ngx', 'install_paperless_stack.sh'), ('tandoor', 'install_tandoor_stack.sh'),
                            ('suite-arr', 'install_generic_stack.sh')]:
            with self.subTest(app=app):
                self.body['request_id'] = web.uuid.uuid4().hex
                self.exercise(app, script, mode='advanced')

    def test_advanced_back_and_invalid_number_use_original_engine_retry_before_dispatch(self):
        seen = []
        def answer(q, value):
            if q['text'] == 'CPU cores':
                if 'back' not in seen:
                    seen.append('back'); return {'back': True}
                if 'invalid' not in seen:
                    seen.append('invalid'); return 'invalid-number-fixture'
            return value
        _, questions = self.exercise('bentopdf', 'install_oci.sh', mode='advanced', answer=answer)
        self.assertEqual(seen, ['back', 'invalid'])
        self.assertGreaterEqual(sum(q['text'] == 'Hostname' for q in questions), 2)
        self.assertGreaterEqual(sum(q['text'] == 'CPU cores' for q in questions), 3)
        self.assertTrue(any(q['kind'] == 'message' and 'number' in q['text'] for q in questions))


if __name__ == '__main__':
    unittest.main()
