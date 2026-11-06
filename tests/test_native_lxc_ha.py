"""HA admission policy, using real discovery/planner/jobs with inert PVE."""
import copy
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from native_lxc_fixture import context, draft, FakePVE
from native_lxc import NativeError, plan
import native_lxc_pve as transport
from native_lxc_jobs import Service, Store
from test_native_lxc_pve import ReadAPI


class HAReadinessTests(unittest.TestCase):
    def test_schema_support_does_not_admit_unverified_or_inactive_ha(self):
        for readiness in (None, {}, {'state':'unknown','reason':'unverified'},
                          {'state':'unavailable','reason':'services_inactive'},
                          {'state':'unavailable','reason':'no_quorum'},
                          {'state':'ready','reason':'unverified'}):
            with self.subTest(readiness=readiness):
                ctx=context();ctx['ha_readiness']=readiness
                value=draft(ctx);value['ha_managed']=True
                with self.assertRaises(NativeError) as caught:plan(value,ctx)
                self.assertEqual(caught.exception.field,'ha_managed')
                value['ha_managed']=False
                self.assertFalse(plan(value,ctx)[0]['ha_managed'])

    def test_readiness_uses_real_service_and_quorum_shapes(self):
        api=ReadAPI()
        api.overrides.update({
            '/cluster/status':[{'type':'cluster','name':'fixture-cluster','nodes':1,'quorate':1}],
            '/nodes/testnode/services':[{'service':'pve-ha-crm','state':'running','active-state':'active'},
                                        {'service':'pve-ha-lrm','state':'running','active-state':'active'}]})
        self.assertEqual(transport.read_ha_readiness(api,'testnode',True),{'state':'ready','reason':'ready'})
        for service in ('pve-ha-crm','pve-ha-lrm'):
            rows=copy.deepcopy(api.overrides['/nodes/testnode/services'])
            next(x for x in rows if x['service']==service)['state']='stopped'
            api.overrides['/nodes/testnode/services']=rows
            self.assertEqual(transport.read_ha_readiness(api,'testnode',True),
                             {'state':'unavailable','reason':'services_inactive'})
            next(x for x in rows if x['service']==service)['state']='running'
        api.overrides['/cluster/status'][0]['quorate']=0
        self.assertEqual(transport.read_ha_readiness(api,'testnode',True),{'state':'unavailable','reason':'no_quorum'})
        api.calls.clear()
        self.assertEqual(transport.read_ha_readiness(api,'testnode',False),{'state':'unsupported','reason':'unsupported'})
        self.assertEqual(api.calls,[])

    def test_missing_partial_invalid_errors_and_timeouts_are_unknown(self):
        valid_cluster=[{'type':'cluster','quorate':1,'nodes':1}]
        valid_services=[{'service':'pve-ha-crm','state':'running','active-state':'active'},
                        {'service':'pve-ha-lrm','state':'running','active-state':'active'}]
        for bad in (None, {}, [], ['bad'], [{'type':'cluster'}], [{'type':'cluster','quorate':'1'}],
                    valid_cluster+valid_cluster):
            api=ReadAPI();api.overrides.update({'/cluster/status':bad,'/nodes/testnode/services':valid_services})
            self.assertEqual(transport.read_ha_readiness(api,'testnode',True)['state'],'unknown')
        for bad in (None, {}, [], ['bad'], valid_services[:1], valid_services+valid_services[:1],
                    [{'service':'pve-ha-crm','state':True},valid_services[1]]):
            api=ReadAPI();api.overrides.update({'/cluster/status':valid_cluster,'/nodes/testnode/services':bad})
            self.assertEqual(transport.read_ha_readiness(api,'testnode',True)['state'],'unknown')
        for error in (NativeError('pve_timeout',504),NativeError('pve_unavailable',503),OSError(),ValueError(),subprocess.TimeoutExpired('fixture',1)):
            api=ReadAPI()
            with patch.object(api,'request',side_effect=error):
                self.assertEqual(transport.read_ha_readiness(api,'testnode',True)['state'],'unknown')
        api=ReadAPI();clock=iter([0,0,6])
        api.overrides['/cluster/status']=valid_cluster
        with patch('native_lxc_pve.time.monotonic',side_effect=lambda:next(clock)):
            self.assertEqual(transport.read_ha_readiness(api,'testnode',True)['state'],'unknown')
        self.assertEqual(len(api.calls),1)

    def test_discovery_unknown_does_not_break_non_ha_or_change_revision(self):
        api=ReadAPI()
        api.overrides.update({'/cluster/status':[{'type':'cluster','quorate':1}],
                              '/nodes/testnode/services':[{'service':'pve-ha-crm','state':'running','active-state':'active'},
                                                         {'service':'pve-ha-lrm','state':'running','active-state':'active'}]})
        ready=transport.read_context(api,node='testnode',clock=lambda:1000)
        self.assertTrue(ready['capabilities']['ha'])
        self.assertEqual(ready['ha_readiness']['state'],'ready')
        api.fail='/cluster/status'
        unknown=transport.read_context(api,node='testnode',clock=lambda:1001)
        self.assertTrue(unknown['capabilities']['ha'])
        self.assertEqual(unknown['ha_readiness']['state'],'unknown')
        self.assertEqual(ready['revision'],unknown['revision'])
        self.assertTrue(all(method in ('GET','SCHEMA') for method,_,_ in api.calls))

    def test_rejected_checks_submit_and_stale_plan_have_no_admission_or_dispatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx=context(1000);ctx['ha_readiness']={'state':'ready','reason':'ready'}
            d=draft(ctx);d['ha_managed']=True
            api=FakePVE();queue=[];store=Store(Path(temporary)/'jobs.sqlite3')
            service=Service(api,store,lambda:copy.deepcopy(ctx),clock=lambda:1000,dispatch=queue.append)
            checked=service.check('owner',d)
            body={'draft':d,'check':checked['check'],'intent':'a'*32,'confirmations':[],
                  'credentials':{'password':'fixture-only-password'}}
            journal=Path(temporary)/'jobs.sqlite3'
            before=journal.read_bytes() if journal.exists() else None
            ctx['ha_readiness']={'state':'unavailable','reason':'services_inactive'}
            for action in (lambda:service.check('owner',d),lambda:service.submit('owner',body)):
                with self.assertRaises(NativeError) as caught:action()
                self.assertEqual(caught.exception.code,'ha_not_ready')
                self.assertEqual(journal.read_bytes() if journal.exists() else None,before)
            self.assertEqual(queue,[]);self.assertEqual(api.calls,[]);self.assertEqual(api.preparations,[])

    def test_queued_job_rechecks_before_preparation_and_legacy_replay_stays_readonly(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx=context(1000);ctx['ha_readiness']={'state':'ready','reason':'ready'}
            d=draft(ctx);d['ha_managed']=True
            api=FakePVE();queue=[];store=Store(Path(temporary)/'jobs.sqlite3')
            service=Service(api,store,lambda:copy.deepcopy(ctx),clock=lambda:1000,dispatch=queue.append)
            checked=service.check('owner',d)
            body={'draft':d,'check':checked['check'],'intent':'b'*32,'confirmations':[],
                  'credentials':{'password':'fixture-only-password'}}
            job=service.submit('owner',body)
            ctx['ha_readiness']={'state':'unknown','reason':'unverified'}
            queue.pop()()
            self.assertEqual(service.get('owner',job['id'])['status'],'failed_before_effect')
            self.assertEqual(api.calls,[]);self.assertEqual(api.preparations,[])
            with patch.object(service,'context',side_effect=AssertionError('history must not rediscover')):
                self.assertEqual(service.submit('owner',body)['id'],job['id'])
            self.assertEqual(queue,[])

    def test_real_transport_rechecks_after_admission_before_ha_create(self):
        api=transport.PveAPI()
        with (patch.object(api,'_authorize_write') as admit,patch.object(api,'_capture') as capture,
              patch('native_lxc_pve.read_ha_readiness',return_value={'state':'unavailable','reason':'services_inactive'}) as probe):
            with self.assertRaises(NativeError) as caught:
                api.request('POST','/nodes/testnode/lxc',{'vmid':4924,'ha-managed':1})
            self.assertEqual(caught.exception.code,'ha_not_ready')
            self.assertEqual(admit.call_count,1);capture.assert_not_called();probe.assert_called_once()

    def test_probe_deadline_before_at_and_after_boundary(self):
        for finish,expected in ((4.999,'ready'),(5,'unknown'),(5.001,'unknown')):
            api=ReadAPI();ticks=iter([0,0,0,0,finish])
            with patch('native_lxc_pve.time.monotonic',side_effect=lambda:next(ticks)):
                self.assertEqual(transport.read_ha_readiness(api,'testnode',True)['state'],expected)
            self.assertTrue(all(m=='GET' for m,_,_ in api.calls))

    def test_verified_pve_dead_service_shape_is_inactive_not_unknown(self):
        rows=[{'service':'pve-ha-crm','state':'dead','active-state':'inactive','unit-state':'disabled'},
              {'service':'pve-ha-lrm','state':'dead','active-state':'inactive','unit-state':'disabled'}]
        api=ReadAPI();api.overrides['/nodes/testnode/services']=rows
        self.assertEqual(transport.read_ha_readiness(api,'testnode',True),
                         {'state':'unavailable','reason':'services_inactive'})

    def test_existing_journal_is_unchanged_by_rejected_ha(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx=context(1000);store=Store(Path(temporary)/'jobs.sqlite3')
            original,_=store.admit('other','c'*32,'fixture',plan(draft(ctx),ctx)[0],1000)
            journal=Path(temporary)/'jobs.sqlite3';before=journal.read_bytes()
            api=FakePVE();queue=[]
            service=Service(api,store,lambda:copy.deepcopy(ctx),clock=lambda:1000,dispatch=queue.append)
            d=draft(ctx);d['ha_managed']=True
            ctx['ha_readiness']={'state':'unknown','reason':'unverified'}
            with self.assertRaises(NativeError):service.check('owner',d)
            with self.assertRaises(NativeError):service.submit('owner',{'draft':d,'check':{},'intent':'d'*32,'confirmations':[],'credentials':{}})
            self.assertEqual(journal.read_bytes(),before)
            self.assertEqual(store.lookup('other','c'*32)['id'],original['id'])
            self.assertEqual(api.calls,[]);self.assertEqual(api.preparations,[]);self.assertEqual(queue,[])

    def test_post_preparation_refusal_is_not_misreported_as_dispatched_create(self):
        from native_lxc import HAReadinessError
        with tempfile.TemporaryDirectory() as temporary:
            ctx=context(1000);d=draft(ctx);d['ha_managed']=True
            api=FakePVE();queue=[];store=Store(Path(temporary)/'jobs.sqlite3')
            service=Service(api,store,lambda:copy.deepcopy(ctx),clock=lambda:1000,dispatch=queue.append)
            checked=service.check('owner',d)
            body={'draft':d,'check':checked['check'],'intent':'e'*32,'confirmations':[],
                  'credentials':{'password':'fixture-only-password'}}
            job=service.submit('owner',body)
            # Same local error the real transport raises before its POST.
            with patch.object(api,'request',side_effect=HAReadinessError('ha_not_ready',422,'ha_managed')):
                queue.pop()()
            result=service.get('owner',job['id'])
            self.assertEqual(result['status'],'failed_before_effect')
            self.assertIsNone(result['upid']);self.assertEqual(result['error'],'ha_not_ready')
            self.assertEqual(len(api.preparations),1);self.assertEqual(api.calls,[])


if __name__=='__main__':unittest.main()
