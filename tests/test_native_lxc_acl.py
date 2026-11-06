"""ACL disable is a separate verified capability, not ACL support itself."""
import copy
import unittest
from unittest.mock import patch
from native_lxc_fixture import context, draft
from native_lxc import NativeError, plan
from native_lxc_pve import read_context
from test_native_lxc_pve import ReadAPI


class NativeACLTests(unittest.TestCase):
    def make(self, field):
        ctx = context()
        value = draft(ctx)
        disk = value['rootfs']
        if field == 'mounts.0':
            disk = {**copy.deepcopy(disk), 'id': 0, 'kind': 'volume',
                    'path_id': '', 'target': '/data', 'ro': False,
                    'backup': True, 'keepattrs': False}
            value['mounts'] = [disk]
        return ctx, value, disk

    def test_unqualified_disable_rejected_before_any_process_or_pve_effect(self):
        for field in ('rootfs', 'mounts.0'):
            for capability in (None, False, 1, 'true'):
                with self.subTest(field=field, capability=capability):
                    ctx, value, disk = self.make(field)
                    for storage in ctx['storages']:
                        if capability is not None:
                            storage['acl_disable'] = capability
                    disk['acl'] = False
                    before = copy.deepcopy((ctx, value))
                    with patch('subprocess.Popen') as process, patch('native_lxc_pve.PveAPI.request') as api:
                        with self.assertRaises(NativeError) as caught:
                            plan(value, ctx)
                        self.assertEqual(caught.exception.code, 'acl_disable_unsupported')
                        self.assertEqual(caught.exception.field, field + '.acl')
                        process.assert_not_called()
                        api.assert_not_called()
                    self.assertEqual((ctx, value), before)

    def test_default_and_enable_keep_existing_pve_parameters(self):
        for field in ('rootfs', 'mounts.0'):
            for acl in (None, True):
                ctx, value, disk = self.make(field)
                disk['acl'] = acl
                result, _ = plan(value, ctx)
                output = result['create']['rootfs' if field == 'rootfs' else 'mp0']
                self.assertEqual('acl=1' in output, acl is True)
                self.assertNotIn('acl=0', output)

    def test_explicit_qualified_capability_only_and_unsupported_enable_unchanged(self):
        ctx, value, disk = self.make('rootfs')
        storage = next(s for s in ctx['storages'] if s['id'] == disk['storage'])
        storage['acl_disable'] = True
        disk['acl'] = False
        self.assertIn('acl=0', plan(value, ctx)[0]['create']['rootfs'])
        storage['acl'] = False
        with self.assertRaises(NativeError) as caught:
            plan(value, ctx)
        self.assertEqual(caught.exception.code, 'acl_unsupported')

    def test_discovery_never_infers_disable_from_storage_type(self):
        api = ReadAPI()
        ctx = read_context(api, 'testnode')
        self.assertTrue(ctx['storages'])
        self.assertTrue(all(s['acl_disable'] is False for s in ctx['storages']))
        self.assertTrue(any(s['acl'] for s in ctx['storages']))
        self.assertTrue(all(method in ('GET', 'SCHEMA') for method, _, _ in api.calls))
