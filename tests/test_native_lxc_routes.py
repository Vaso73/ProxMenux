"""Actual Flask/JWT/origin routes through the native planner and journal."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import create_route_fixture as fixture
from native_lxc_fixture import FakePVE, context, draft
from native_lxc_jobs import Service, Store
from native_lxc_pve import PveAPI


class NativeRoutesTests(unittest.TestCase):
    def setUp(self):
        self.h=fixture.RouteFixture();self.h.setUp();self.addCleanup(self.h.doCleanups)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.api=FakePVE();self.ctx=context();self.value=draft(self.ctx);self.queue=[]
        self.provider=Mock(side_effect=lambda:{**copy.deepcopy(self.ctx),'observed_at':time.time()})
        self.root=Path(self.tmp.name)/'journal'
        self.backend=Service(self.api,Store(self.root/'jobs.sqlite3'),self.provider,dispatch=self.queue.append)
        self.h.app.config['CREATE_NATIVE_LXC_BACKEND']=self.backend

    def request(self,path='checks',method='POST',body=None,token=None,**kwargs):
        args={'headers':{'Authorization':'Bearer '+(self.h.token if token is None else token),**kwargs.pop('headers',{})}}
        if method=='POST':args.update(data=kwargs.pop('raw',json.dumps({'draft':self.value} if body is None else body)),content_type=kwargs.pop('content_type','application/json'))
        return self.h.client.open('/api/create/lxc/v1/'+path,method=method,**args,**kwargs)

    def test_read_context_plan_and_history_never_write_or_allocate(self):
        for path,method in [('context','GET'),('checks','POST'),('jobs','GET')]:
            response=self.request(path,method)
            self.assertEqual(response.status_code,200,response.json)
            self.assertEqual(response.headers['Cache-Control'],'no-store')
        self.assertEqual(self.api.calls,[]);self.assertEqual(self.queue,[]);self.assertFalse(self.root.exists())

    def test_ha_readiness_rejection_precedes_admission_even_with_previous_valid_check(self):
        self.value['ha_managed']=True
        checked=self.request().json
        self.ctx['ha_readiness']={'state':'unavailable','reason':'services_inactive'}
        for path,body in [('checks',{'draft':self.value}),
                          ('jobs',{'draft':self.value,'check':checked['check'],'intent':'f'*32,
                                   'confirmations':[],'credentials':{'password':'fixture-only-password'}})]:
            reply=self.request(path,body=body)
            self.assertEqual(reply.status_code,422,reply.json)
            self.assertEqual(reply.json['error'],'ha_not_ready')
        self.assertEqual(self.api.calls,[]);self.assertEqual(self.api.preparations,[])
        self.assertEqual(self.queue,[]);self.assertFalse(self.root.exists())
        self.ctx['ha_readiness']={'state':'unknown','reason':'unverified'}
        reply=self.request()
        self.assertEqual(reply.status_code,503,reply.json)
        self.assertEqual(reply.json['error'],'ha_status_unknown')
        self.assertFalse(self.root.exists())

    def test_vlan_check_uses_native_tag_contract_without_journal_or_network_effect(self):
        self.ctx['bridges'][0]['vlan_aware']=False
        before=copy.deepcopy(self.value)
        with patch('native_lxc_pve.subprocess.Popen') as process:
            for tag in (None,1,100,4094):
                self.value['network']['tag']=tag
                response=self.request()
                self.assertEqual(response.status_code,200,response.json)
                network=dict(v.split('=',1) for v in response.json['plan']['create']['net0'].split(','))
                self.assertEqual(network.get('tag'),None if tag is None else str(tag))
            for tag in (0,4095,True,'100'):
                self.value['network']['tag']=tag
                self.assertEqual(self.request().status_code,422)
            self.value['network'].update(tag=100,bridge='missing')
            self.assertEqual(self.request().status_code,422)
            process.assert_not_called()
        self.assertEqual(self.ctx['bridges'][0]['vlan_aware'],False)
        self.assertEqual(self.api.calls,[]);self.assertEqual(self.queue,[])
        self.assertFalse(self.root.exists())
        self.assertEqual(before['node'],self.value['node'])

    def test_auth_origin_scope_platform_and_query_fail_before_any_provider(self):
        for token in ('','invalid',self.h.jwt_fixture.token(scope='read_only'),self.h.jwt_fixture.token(exp=time.time()-10)):
            self.assertIn(self.request(token=token).status_code,(401,403))
        self.assertEqual(self.request(headers={'Origin':'https://foreign.invalid'}).status_code,403)
        for platform,status in [('pbs',403),(None,503)]:
            self.h.app.config['CREATE_OCI_PROFILE_PLATFORM']=platform
            self.assertEqual(self.request().status_code,status)
        self.h.app.config['CREATE_OCI_PROFILE_PLATFORM']='pve'
        for path in ('context?node=other','checks?path=/etc'):
            self.assertEqual(self.request(path,'GET' if path.startswith('context') else 'POST').status_code,400)
        self.provider.assert_not_called();self.assertEqual(self.api.calls,[]);self.assertFalse(self.root.exists())

    def test_envelope_duplicates_unknown_fields_and_large_bodies_fail_without_probe(self):
        for kwargs in ({'raw':'{"draft":{},"draft":{}}'},{'raw':'{"draft":NaN}'},{'body':{'draft':{},'command':'never'}},{'raw':' '*131073},{'content_type':'text/plain'}):
            response=self.request(**kwargs);self.assertIn(response.status_code,(400,413,415))
        self.provider.assert_not_called();self.assertEqual(self.api.calls,[])

    def test_default_gate_and_download_cannot_be_enabled_by_request_or_config_flags(self):
        self.h.app.config['CREATE_NATIVE_LXC_BACKEND']=Service(PveAPI(),Store(self.root/'jobs.sqlite3'),self.provider)
        self.h.app.config.update(CREATE_NATIVE_LXC_ENABLED=True,CREATE_NATIVE_LXC_ALLOW_CREATE=True)
        with patch('native_lxc_pve.subprocess.Popen') as run:
            for path in ('jobs','template-downloads'):
                response=self.request(path,body={'enabled':True,'command':'never'})
                self.assertEqual(response.status_code,503,response.json)
            run.assert_not_called()
        self.provider.assert_not_called();self.assertFalse(self.root.exists())

    def test_real_route_check_admit_poll_duplicate_and_owner_isolation(self):
        checked=self.request().json
        body={'draft':self.value,'check':checked['check'],'intent':'a'*32,'confirmations':[], 'credentials':{'password':'fixture-only-password'}}
        first=self.request('jobs',body=body);self.assertEqual(first.status_code,200,first.json)
        duplicate=self.request('jobs',body=body);self.assertEqual(duplicate.json['job']['id'],first.json['job']['id']);self.assertEqual(len(self.queue),1)
        self.queue.pop()();job_id=first.json['job']['id']
        result=self.request('jobs/'+job_id,'GET');self.assertEqual(result.json['job']['status'],'succeeded')
        history=self.request('jobs','GET');self.assertEqual(len(history.json['jobs']),1)
        self.assertNotIn('fixture-only-password',result.get_data(as_text=True))
        other=self.request('jobs/'+job_id,'GET',token=self.h.jwt_fixture.token(username='different'))
        self.assertEqual(other.status_code,404,other.json)

    def test_acl_disable_checks_and_submission_have_zero_effects_and_history_still_reads(self):
        checked = self.request().json
        for target in ('rootfs', 'mounts.0'):
            value = copy.deepcopy(self.value)
            if target == 'rootfs':
                value['rootfs']['acl'] = False
            else:
                value['mounts'] = [{**copy.deepcopy(value['rootfs']), 'id': 0,
                    'kind': 'volume', 'path_id': '', 'target': '/data',
                    'ro': False, 'backup': True, 'keepattrs': False, 'acl': False}]
            with self.subTest(target=target), patch('native_lxc_pve.subprocess.Popen') as process:
                for endpoint, body in [('checks', {'draft': value}), ('jobs', {
                    'draft': value, 'check': checked['check'], 'intent': 'a'*32,
                    'confirmations': [], 'credentials': {'password': 'fixture-only-password'}})]:
                    response = self.request(endpoint, body=body)
                    self.assertEqual(response.status_code, 422, response.json)
                    self.assertEqual(response.json['error'], 'acl_disable_unsupported')
                process.assert_not_called()
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.api.preparations, [])
        self.assertEqual(self.queue, [])
        self.assertFalse(self.root.exists())

    def test_errors_never_expose_external_stderr_or_secrets(self):
        self.provider.side_effect=RuntimeError('never expose /secret/path fixture-password')
        response=self.request();self.assertEqual(response.status_code,503)
        self.assertNotIn('/secret/path',response.get_data(as_text=True));self.assertNotIn('fixture-password',response.get_data(as_text=True))

    def test_legacy_acl_zero_job_reads_and_same_intent_returns_without_revalidation_or_replay(self):
        for storage in self.ctx['storages']:
            storage['acl_disable'] = True
        self.value['rootfs']['acl'] = False
        checked = self.request().json
        body = {'draft': self.value, 'check': checked['check'], 'intent': 'b'*32,
                'confirmations': [], 'credentials': {'password': 'fixture-only-password'}}
        created = self.request('jobs', body=body)
        self.assertEqual(created.status_code, 200, created.json)
        job_id = created.json['job']['id']
        for storage in self.ctx['storages']:
            storage['acl_disable'] = False
        before = self.backend.store.get(self.h.username if hasattr(self.h, 'username') else checked['check']['owner'], job_id)
        for reply in (self.request('jobs', 'GET'), self.request('jobs/'+job_id, 'GET'), self.request('jobs', body=body)):
            self.assertEqual(reply.status_code, 200, reply.json)
            self.assertIn('acl=0', reply.get_data(as_text=True))
        self.assertEqual(len(self.queue), 1)
        self.assertEqual(self.api.calls, [])
        self.assertEqual(self.api.preparations, [])
        self.assertEqual(self.backend.store.get(checked['check']['owner'], job_id), before)


if __name__=='__main__':unittest.main()
