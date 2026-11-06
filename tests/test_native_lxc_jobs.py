"""Durable task flow with an inert PVE API; no real guests are created."""
import concurrent.futures
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from native_lxc_fixture import context, draft, FakePVE
from native_lxc import NativeError
from native_lxc_jobs import Service, Store, _matches_config, _upid
from native_lxc_pve import PveAPI


class Clock:
    def __init__(self): self.now=1000.; self.elapsed=0.
    def time(self): return self.now+self.elapsed
    def monotonic(self): return self.elapsed
    def sleep(self,seconds): self.elapsed+=seconds


class NativeJobTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'journal'; self.clock=Clock(); self.api=FakePVE()
        self.ctx=context(self.clock.time()); self.draft=draft(self.ctx)
        self.store=Store(self.root/'jobs.sqlite3')
        self.queue=[]
        self.service=Service(self.api,self.store,lambda:{**copy.deepcopy(self.ctx),'observed_at':self.clock.time()},
                             clock=self.clock.time,monotonic=self.clock.monotonic,sleep=self.clock.sleep,dispatch=self.queue.append)
        self.checked=self.service.check('owner',self.draft)
        self.body={'draft':self.draft,'check':self.checked['check'],'intent':'a'*32,'confirmations':[], 'credentials':{'password':'fixture-only-password'}}

    def run_job(self,body=None):
        first=self.service.submit('owner',body or self.body)
        if self.queue: self.queue.pop(0)()
        return self.service.get('owner',first['id'])

    def test_check_and_history_leave_no_journal_or_pve_calls(self):
        self.assertFalse(self.root.exists()); self.assertEqual(self.api.calls,[])
        self.assertEqual(self.service.jobs('owner'),[]); self.assertFalse(self.root.exists())
        self.assertEqual(self.api.preparations,[])

    def test_pve_warnings_complete_create_and_start_without_replay(self):
        self.draft['start'] = True
        self.body['check'] = self.service.check('owner', self.draft)['check']
        self.api.task_statuses = [{'status': 'stopped', 'exitstatus': 'WARNINGS: 1'},
                                 {'status': 'stopped', 'exitstatus': 'WARNINGS: 12'}]
        job = self.run_job()
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertEqual(job['result']['status'], 'running')
        self.assertEqual([w['exitstatus'] for w in job['task_warnings']], ['WARNINGS: 1', 'WARNINGS: 12'])
        self.assertEqual([w['phase'] for w in job['task_warnings']], ['vzcreate', 'vzstart'])
        self.assertEqual(len({w['upid'] for w in job['task_warnings']}), 2)
        before = self.store.path.read_bytes()
        self.api.task_statuses = [{'status': 'stopped', 'exitstatus': 'WARNINGS: 12'}]
        observed = self.service.observe('owner', job['id'])
        self.assertEqual(observed['observed_task']['exitstatus'], 'WARNINGS: 12')
        self.assertEqual(self.store.path.read_bytes(), before, 'observation never writes or resumes')
        calls = list(self.api.calls)
        self.assertEqual(self.service.submit('owner', self.body)['id'], job['id'])
        self.assertEqual(self.api.calls, calls, 'same intent cannot create or start twice')
        self.assertEqual(self.queue, [])
        self.assertEqual(sum(m == 'POST' and p == '/nodes/testnode/lxc' for m,p,_ in calls), 1)
        self.assertEqual(sum(m == 'POST' and p.endswith('/status/start') for m,p,_ in calls), 1)

    def test_warning_does_not_bypass_configuration_verification(self):
        self.api.task_statuses = [{'status': 'stopped', 'exitstatus': 'WARNINGS: 1'}]
        original = self.api.request
        def changed(method, path, params=None):
            value = original(method, path, params)
            if method == 'GET' and path.endswith('/config'):
                value['hostname'] = 'wrong'
            return value
        with patch.object(self.api, 'request', side_effect=changed):
            job = self.run_job()
        self.assertNotEqual(job['status'], 'succeeded')
        self.assertEqual(len(job['task_warnings']), 1)
        self.assertFalse(any(m == 'PUT' for m,_,_ in self.api.calls))

    def test_malformed_warning_or_unknown_is_never_success(self):
        job = self.run_job()
        for value in ('WARNINGS: 1x', 'WARNINGS: 1\n', 'WARNINGS: -1', 'WARNINGS:', 'unexpected status', 1, ['OK']):
            with self.subTest(value=value):
                with self.assertRaises(NativeError):
                    self.api.task_statuses = [{'status': 'stopped', 'exitstatus': value}]
                    self.service._task(job, {}, '/nodes/testnode/lxc', 'vzcreate', 10)

    def test_template_preparation_failure_is_before_guest_effect_and_cannot_replay(self):
        with patch.object(self.api,'prepare_template',side_effect=NativeError('template_preparation_failed',503)) as prepare:
            job=self.run_job()
        self.assertEqual(job['status'],'failed_before_effect');prepare.assert_called_once()
        self.assertEqual(self.api.calls,[]);self.assertIsNone(self.api.config)
        with patch.object(self.api,'prepare_template') as prepare:
            self.assertEqual(self.service.submit('owner',self.body)['id'],job['id'])
            prepare.assert_not_called()
        self.assertEqual(self.queue,[])
        self.assertNotIn('fixture-only-password',self.store.path.read_bytes().decode('latin1'))

    def test_unexpected_template_preparation_error_still_has_no_guest_effect(self):
        with patch.object(self.api,'prepare_template',side_effect=OSError('fixture failure')):
            job=self.run_job()
        self.assertEqual(job['status'],'failed_before_effect');self.assertEqual(job['error'],'operation_failed')
        self.assertEqual(self.api.calls,[]);self.assertIsNone(self.api.config)

    def test_derived_template_is_used_only_for_create_and_original_signed_plan_is_preserved(self):
        derived='/var/cache/proxmenux-lxc-templates/v1/'+'a'*64+'/template.tar.zst'
        original=self.checked['plan']['create']['ostemplate']
        def prepare(node,vmid,template):
            self.assertEqual(self.api.calls,[])
            admitted=self.store.jobs('owner')[0]
            self.assertEqual(admitted['phase'],'template_preparation')
            self.assertEqual(admitted['status'],'creating');self.assertEqual(template,original)
            return derived
        with patch.object(self.api,'prepare_template',side_effect=prepare):job=self.run_job()
        self.assertEqual(job['status'],'succeeded',job)
        creates=[c for c in self.api.calls if c[:2]==('POST','/nodes/testnode/lxc')]
        self.assertEqual(len(creates),1);self.assertEqual(creates[0][2]['ostemplate'],derived)
        self.assertEqual(job['plan']['create']['ostemplate'],original)
        self.assertNotIn(derived,json.dumps(job))

    def test_default_writer_rejects_before_context_journal_or_process(self):
        service=Service(api=PveAPI(),store=self.store,context_provider=lambda: self.fail('forbidden probe'))
        with patch('native_lxc_pve.subprocess.Popen') as run:
            for body in ({},self.body):
                with self.assertRaises(NativeError) as caught: service.submit('owner',body)
                self.assertEqual(caught.exception.code,'execution_disabled')
            with self.assertRaises(NativeError): service.api.request('POST','/nodes/testnode/lxc',{'password':'fixture'})
            run.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_success_has_one_create_and_verified_stopped_state(self):
        job=self.run_job()
        self.assertEqual(job['status'],'succeeded',job)
        self.assertEqual(job['result'],{'node':'testnode','vmid':4900,'status':'stopped'})
        self.assertEqual(len([c for c in self.api.calls if c[:2]==('POST','/nodes/testnode/lxc')]),1)
        self.assertFalse(any('start' in c[1] for c in self.api.calls))
        self.assertNotIn('fixture-only-password',self.store.path.read_bytes().decode('latin1'))
        self.assertNotIn('credentials',json.dumps(job)); self.assertEqual(self.store.path.stat().st_mode&0o777,0o600)

    def test_duplicate_submit_same_intent_does_not_probe_dispatch_or_create_again(self):
        first=self.service.submit('owner',self.body)
        duplicate=self.service.submit('owner',self.body)
        self.assertEqual(first['id'],duplicate['id']); self.assertEqual(len(self.queue),1)
        self.queue.pop()(); count=len(self.api.calls)
        self.clock.elapsed=10000
        self.assertEqual(self.service.submit('owner',self.body)['id'],first['id'])
        self.assertEqual(len(self.api.calls),count); self.assertEqual(self.queue,[])

    def test_same_intent_changed_configuration_conflicts_without_effect(self):
        self.service.submit('owner',self.body)
        changed=copy.deepcopy(self.body); changed['draft']['hostname']='different'
        with self.assertRaises(NativeError) as caught: self.service.submit('owner',changed)
        self.assertEqual(caught.exception.code,'idempotency_conflict'); self.assertEqual(self.api.calls,[])

    def test_same_id_different_intents_concurrent_workers_have_one_reservation(self):
        self.store._connect(True).close()
        barrier=threading.Barrier(2)
        def send(key):
            body=copy.deepcopy(self.body); body['intent']=key*32; barrier.wait()
            try:return self.service.submit('owner',body)['id']
            except NativeError as exc:return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(send,['a','b']))
        self.assertIn('vmid_reserved',results); self.assertEqual(len(self.queue),1); self.assertEqual(self.api.calls,[])

    def test_same_intent_concurrent_workers_return_one_job(self):
        self.store._connect(True).close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            jobs=list(pool.map(lambda _:self.service.submit('owner',copy.deepcopy(self.body)),range(4)))
        self.assertEqual(len({j['id'] for j in jobs}),1); self.assertEqual(len(self.queue),1); self.assertEqual(self.api.calls,[])

    def test_ttl_before_exact_and_after_boundary(self):
        for elapsed,allowed in ((119.999,True),(120,False),(120.001,False)):
            with self.subTest(elapsed=elapsed):
                self.clock.elapsed=elapsed
                if allowed:
                    self.service._verify('owner',self.body['check'],self.checked['plan'])
                else:
                    with self.assertRaises(NativeError) as caught:self.service._verify('owner',self.body['check'],self.checked['plan'])
                    self.assertEqual(caught.exception.code,'check_expired')
        self.assertFalse(self.root.exists()); self.assertEqual(self.api.calls,[])

    def test_changed_plan_context_and_owner_fail_before_admission(self):
        for mode in ('plan','context','owner','signature'):
            with self.subTest(mode=mode):
                body=copy.deepcopy(self.body); ctx=copy.deepcopy(self.ctx)
                if mode=='plan': body['draft']['hostname']='changed'
                if mode=='context': self.ctx['revision']='e'*64
                if mode=='owner':body['check']['owner']='someone'
                if mode=='signature':body['check']['signature']='b'*64
                with self.assertRaises(NativeError):self.service.submit('owner',body)
                self.ctx=ctx
        self.assertFalse(self.root.exists()); self.assertEqual(self.api.calls,[])

    def test_queued_worker_rechecks_id_before_first_effect(self):
        first=self.service.submit('owner',self.body)
        self.ctx['occupied_ids'].append(4900)
        self.queue.pop()()
        self.assertEqual(self.service.get('owner',first['id'])['status'],'failed_before_effect')
        self.assertEqual(self.api.calls,[])
        self.assertEqual(self.api.preparations,[])

    def test_wrong_password_and_missing_acknowledgement_never_admit(self):
        for password in ('', 'tiny', 123):
            body=copy.deepcopy(self.body); body['credentials']['password']=password
            with self.assertRaises(NativeError):self.service.submit('owner',body)
        self.draft['unprivileged']=False;self.draft['nesting']=False
        checked=self.service.check('owner',self.draft);body={**self.body,'draft':self.draft,'check':checked['check']}
        with self.assertRaises(NativeError):self.service.submit('owner',body)
        self.assertFalse(self.root.exists()); self.assertEqual(self.api.calls,[])

    def test_task_output_before_upid_missing_or_wrong_identity_is_unknown_without_replay(self):
        for reply in ('','log\nUPID:testnode:1234:5678:6900:vzcreate:4900:root@pam:', 'UPID:testnode:1234:5678:6900:vzrestore:4900:root@pam:', 'UPID:other:1234:5678:6900:vzcreate:4900:root@pam:', {'upid':'wrong'}):
            with self.subTest(reply=reply):
                self.setUp(); self.api.create_reply=reply; job=self.run_job()
                self.assertEqual(job['status'],'unknown'); self.assertEqual(len(self.api.calls),1)
                self.assertEqual(self.service.submit('owner',self.body)['id'],job['id']); self.assertEqual(len(self.api.calls),1)

    def test_running_then_delayed_completion_is_not_premature_success(self):
        self.api.task_statuses=[{'status':'running'},{'status':'running'},{'status':'stopped','exitstatus':'OK'}]
        job=self.run_job();self.assertEqual(job['status'],'succeeded');self.assertEqual(self.clock.elapsed,4)

    def test_running_task_timeout_persists_unknown_and_readonly_observe_never_resumes(self):
        original=self.api.request
        def request(method,path,params=None):
            if '/tasks/' in path:return {'status':'running'}
            return original(method,path,params)
        self.api.request=request
        job=self.run_job();self.assertEqual(job['status'],'unknown');self.assertEqual(self.clock.elapsed,900)
        before=self.store.path.read_bytes(); calls=copy.deepcopy(self.api.calls)
        self.api.request=original
        observed=self.service.observe('owner',job['id'])
        self.assertEqual(observed['status'],'unknown');self.assertEqual(self.store.path.read_bytes(),before)
        self.assertEqual(len(self.api.calls),len(calls)+1);self.assertEqual(self.api.calls[-1][0],'GET')

    def test_missing_exitstatus_or_invalid_status_remains_unknown(self):
        for result in ({'status':'stopped'},{'status':'not-a-task'},None):
            with self.subTest(result=result):
                self.setUp();self.api.task_statuses=[result];job=self.run_job();self.assertEqual(job['status'],'unknown')

    def test_upid_hex_width_matches_primary_decoder_including_long_uptime(self):
        self.assertEqual(_upid('UPID:testnode:00001234:100005678:69000000:vzcreate:4900:root@pam:', 'testnode',4900,'vzcreate'), 'UPID:testnode:00001234:100005678:69000000:vzcreate:4900:root@pam:')
        self.api.create_reply='UPID:testnode:1234:5678:6900:vzcreate:4900:root@pam:'
        job=self.run_job();self.assertEqual(job['status'],'unknown');self.assertEqual(len(self.api.calls),1)

    def test_failed_start_preserves_onboot_zero_and_never_deletes(self):
        self.draft.update(start=True,onboot=True)
        check=self.service.check('owner',self.draft);self.body.update(draft=self.draft,check=check['check'])
        self.api.task_statuses=[{'status':'stopped','exitstatus':'OK'},{'status':'stopped','exitstatus':'ERROR'}]
        job=self.run_job();self.assertEqual(job['status'],'partial');self.assertEqual(self.api.config['onboot'],0)
        self.assertFalse(any(c[0]=='DELETE' for c in self.api.calls))

    def test_firewall_failure_never_starts_or_enables_onboot(self):
        self.draft.update(start=True,onboot=True);self.draft['firewall'].update(mode='ssh_only',ssh_sources=['192.0.2.0/24'])
        check=self.service.check('owner',self.draft);self.body.update(check=check['check'],confirmations=['firewall_restricts'])
        self.api.fail=lambda method,path,params:method=='PUT' and path.endswith('/firewall/options')
        job=self.run_job();self.assertEqual(job['status'],'unknown');self.assertEqual(self.api.config['onboot'],0)
        self.assertFalse(any(c[1].endswith('/status/start') for c in self.api.calls))
        self.assertFalse(any('/cluster/firewall' in c[1] or '/nodes/testnode/firewall' in c[1] for c in self.api.calls))

    def test_pool_ha_alternative_start_and_dual_stack_firewall_full_flow(self):
        self.draft.update(start=True,onboot=True,pool='lab',ha_managed=True)
        self.draft['firewall'].update(mode='ssh_only',ssh_sources=['192.0.2.0/24','2001:db8::/64'])
        check=self.service.check('owner',self.draft);self.body.update(check=check['check'],confirmations=['firewall_restricts'])
        job=self.run_job();self.assertEqual(job['status'],'succeeded',job)
        self.assertIn(':hastart:',job['upid']);self.assertEqual(self.api.config['onboot'],1)
        self.assertEqual(len(self.api.rules),2);self.assertTrue(self.api.calls[-2][0]=='PUT')

    def test_restarted_or_lost_worker_is_visible_and_not_replayed(self):
        first=self.service.submit('owner',self.body);self.service.active.clear();self.queue.clear()
        restarted=Service(self.api,self.store,lambda:self.ctx,clock=self.clock.time)
        job=restarted.get('owner',first['id']);self.assertEqual(job['status'],'unknown')
        self.assertEqual(restarted.submit('owner',self.body)['id'],first['id']);self.assertEqual(self.api.calls,[])
        with self.assertRaises(NativeError) as caught:restarted.get('other',first['id'])
        self.assertEqual(caught.exception.status,404)

    def test_another_live_worker_is_not_misclassified_but_reused_pid_is_unknown(self):
        job=self.service.submit('owner',self.body)
        with patch('native_lxc_jobs._INSTANCE','different-process'), patch.object(self.service,'active',set()):
            self.assertEqual(self.service.get('owner',job['id'])['status'],'queued')
            stored=self.store.get('owner',job['id']);stored['worker_start']='not-the-same-process';self.store.put(stored)
            self.assertEqual(self.service.get('owner',job['id'])['status'],'unknown')
        self.assertEqual(self.api.calls,[])

    def test_journal_failure_prevents_dispatch_and_failure_after_send_blocks_replay(self):
        with patch.object(self.store,'admit',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):self.service.submit('owner',self.body)
        self.assertEqual(self.queue,[]);self.assertEqual(self.api.calls,[])
        first=self.service.submit('owner',self.body)
        original=self.store.put
        def put(job):
            if job.get('upid'):raise OSError('disk full')
            return original(job)
        with patch.object(self.store,'put',side_effect=put):self.queue.pop()()
        self.assertEqual(self.service.get('owner',first['id'])['status'],'unknown')
        self.assertEqual(len(self.api.calls),1)

    def test_config_readback_checks_explicit_zero_flags_and_normalized_sizes(self):
        self.assertTrue(_matches_config({'onboot':0}, {'start':0,'onboot':0}))
        self.assertFalse(_matches_config({'rootfs':'local-zfs:subvol-4900-disk-0,size=8G,replicate=0'}, {'rootfs':'local-zfs:8,replicate=1'}))
        self.assertFalse(_matches_config({'mp0':'local-zfs:subvol-4900-disk-1,size=8G,backup=1'}, {'mp0':'local-zfs:8,backup=0'}))
        self.assertTrue(_matches_config({'rootfs':'local-zfs:subvol-4900-disk-0,size=8192M'}, {'rootfs':'local-zfs:8,replicate=1'}))
        self.assertFalse(_matches_config({'net0':'name=eth0,bridge=wrong'}, {'net0':'name=eth0,bridge=vmbr0'}))
        with self.assertRaises(NativeError):_upid('UPID:testnode:1234:5678:6900:vzstart:4900:root@pam:', 'testnode',4900,'vzcreate')


if __name__=='__main__':unittest.main()
