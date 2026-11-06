"""Exercise the actual authenticated manager routes with an inert native worker."""
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import create_route_fixture as fixture

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'oci/src'), str(ROOT / 'oci/remote')]
import oci_web_manager as web
from proxmenux_oci import web_manager as native


class ManagerRoutes(unittest.TestCase):
    def setUp(self):
        self.h = fixture.RouteFixture()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.launch = Mock()
        self.store = web.Store(Path(self.tmp.name) / 'sessions', uid=os.geteuid(), runner=self.launch, engine=ROOT / 'oci')
        self.factory = Mock(return_value=self.store)
        self.projection = Mock(return_value={'schema': 'oci.manager.v1'})
        adapter = Mock(return_value=SimpleNamespace(OPERATIONS=native.OPERATIONS, snapshot=self.projection))
        self.h.app.config['CREATE_OCI_MANAGER_BACKEND'] = SimpleNamespace(Store=self.factory, Error=web.Error, adapter=adapter, ENGINE=ROOT / 'oci')
        self.body = dict(operation='catalog.install', parameters={'app_id': 'fixture', 'mode': 'default'}, request_id='a' * 32, language='en', node=socket.gethostname())

    def request(self, path='jobs', method='POST', body=None, token=None, **headers):
        args = {'headers': {'Authorization': 'Bearer ' + (self.h.token if token is None else token), **headers}}
        if method == 'POST':
            args['json'] = self.body if body is None else body
        return self.h.client.open('/api/create/oci/manager/v1/' + path, method=method, **args)

    def test_metadata_and_history_do_not_allocate_or_launch_any_operation(self):
        for path in ('context', 'jobs'):
            response = self.request(path, 'GET')
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertFalse(self.store.root.exists())
        self.launch.assert_not_called()
        self.projection.assert_not_called()
        self.assertEqual(self.request('context', 'GET').json['operations'], ['catalog.install', 'custom.install'])

    def test_refresh_and_retry_never_start_a_second_worker(self):
        job = self.request().json['job']
        self.assertEqual(self.request().json['job']['id'], job['id'])
        self.assertEqual(self.request('jobs/' + job['id'], 'GET').json['job']['id'], job['id'])
        self.assertEqual(len(self.request('jobs', 'GET').json['jobs']), 1)
        self.launch.assert_called_once()

    def test_auth_role_platform_and_origin_fail_before_backend_access(self):
        for token in ('', 'invalid', self.h.jwt_fixture.token(scope='read_only')):
            self.assertIn(self.request(token=token).status_code, (401, 403))
        self.assertEqual(self.request(Origin='https://untrusted.invalid').status_code, 403)
        self.h.app.config['CREATE_OCI_PROFILE_PLATFORM'] = lambda: 'pbs'
        self.assertEqual(self.request().status_code, 403)
        self.factory.assert_not_called()
        self.launch.assert_not_called()

    def test_malformed_intent_cannot_allocate_and_unknown_platform_is_unavailable(self):
        for body in [dict(self.body, node='another-node'), dict(self.body, parameters={'command': 'never'}), dict(self.body, operation=[] )]:
            response = self.request(body=body)
            self.assertEqual(response.status_code, 400, response.json)
        self.assertFalse(self.store.root.exists())
        self.launch.assert_not_called()
        self.h.app.config['CREATE_OCI_PROFILE_PLATFORM'] = lambda: None
        self.assertEqual(self.request().status_code, 503)

    def test_password_reply_never_appears_in_public_job_and_oversized_input_is_refused(self):
        job = self.request().json['job']
        q = dict(id='b' * 32, kind='password', text='Password', options=[], default=None, back=False, expires_at=int(time.time()) + 900)
        self.store.change(job['id'], status='question', question=q)
        response = self.request('jobs/' + job['id'] + '/answer', body={'question_id': q['id'], 'value': 'fixture-private'})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertNotIn('fixture-private', json.dumps(response.json))
        response = self.h.client.post('/api/create/oci/manager/v1/jobs', data='x' * (13 * 1024 * 1024),
            headers={'Authorization': 'Bearer ' + self.h.token}, content_type='application/json')
        self.assertEqual(response.status_code, 413)
        self.launch.assert_called_once()

    def test_existing_non_creation_job_cannot_be_polled_answered_or_revealed(self):
        import hashlib
        other=dict(self.body,operation='registry.add',parameters={})
        job=self.store.start_operation('fixture-admin',other)
        path=self.store.root/job['id']/'state.json'
        before=path.read_bytes()
        self.launch.reset_mock()
        for suffix,method,body in (('', 'GET', None),('/answer','POST',{'cancel':True}),('/credentials','POST',{})):
            response=self.request('jobs/'+job['id']+suffix,method,body=body)
            self.assertEqual(response.status_code,404,response.json)
        self.assertEqual(self.request('jobs','GET').json['jobs'],[])
        self.assertEqual(path.read_bytes(),before)
        self.launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
