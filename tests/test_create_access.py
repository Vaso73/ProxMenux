"""Creation admission rejects invalid callers before an engine/store probe."""
import copy
import hashlib
import json
import time
from unittest import mock
import unittest
from create_route_fixture import RouteFixture


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.h = RouteFixture()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.backend = mock.Mock()
        self.h.app.config['CREATE_OCI_MANAGER_BACKEND'] = self.backend

    def request(self, body=None, token=None, **headers):
        return self.h.client.post('/api/create/oci/manager/v1/jobs',
            headers={'Authorization':'Bearer ' + (self.h.token if token is None else token), **headers},
            data=json.dumps(body or {}), content_type='application/json')

    def test_disabled_missing_revoked_expired_and_restricted_identity_never_open_store(self):
        for change in ({'enabled':False}, {'declined':True}, {'jwt_secret':''},
                       {'revoked_tokens':'invalid'},
                       {'revoked_tokens':[hashlib.sha256(self.h.token.encode()).hexdigest()]}):
            before = copy.deepcopy(self.h.auth_config)
            self.h.auth_config.update(change)
            self.assertIn(self.request().status_code, (401,403))
            self.h.auth_config.clear()
            self.h.auth_config.update(before)
        for token in ('invalid', self.h.jwt_fixture.token(exp=time.time()-10),
                      self.h.jwt_fixture.token(scope='read_only'),
                      self.h.jwt_fixture.token(username='a'*129),
                      self.h.jwt_fixture.token(username='bad\nidentity')):
            self.assertIn(self.request(token=token).status_code, (401,403))
        self.backend.Store.assert_not_called()

    def test_origin_query_and_platform_fail_before_backend(self):
        self.assertEqual(self.request(Origin='https://foreign.invalid').status_code,403)
        for value, status in ((None,503), ('pbs',403)):
            self.h.app.config['CREATE_OCI_PROFILE_PLATFORM'] = value
            self.assertEqual(self.request().status_code,status)
        self.h.app.config['CREATE_OCI_PROFILE_PLATFORM']='pve'
        response=self.h.client.get('/api/create/oci/manager/v1/context?node=foreign',
            headers={'Authorization':'Bearer '+self.h.token})
        self.assertEqual(response.status_code,400)
        self.backend.Store.assert_not_called()

    def test_ambiguous_or_non_object_envelope_has_no_backend_effect(self):
        for raw in ('{"operation":"custom.install","operation":"instance.remove"}',
                    '{"operation":NaN}', '[]', 'null', '{', '{"n":'+'['*1200+'0'+']'*1200+'}'):
            response=self.h.client.post('/api/create/oci/manager/v1/jobs', data=raw,
                headers={'Authorization':'Bearer '+self.h.token},content_type='application/json')
            self.assertEqual(response.status_code,400,response.json)
        self.backend.Store.assert_not_called()

    def test_management_operations_are_refused_without_store_or_worker(self):
        for operation in ('instance.remove','instance.update','registry.add','updates.selected',[],None):
            self.assertEqual(self.request({'operation':operation}).status_code,400)
        self.backend.Store.assert_not_called()


if __name__ == '__main__':
    unittest.main()
