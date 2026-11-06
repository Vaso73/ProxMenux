"""The preview is a creation adapter, not a second management/lifecycle UI."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from create_route_fixture import RouteFixture

ROOT=Path(__file__).resolve().parents[1]


class PreviewScope(unittest.TestCase):
    def test_legacy_executors_and_management_view_have_no_route(self):
        h=RouteFixture();h.setUp();self.addCleanup(h.doCleanups)
        with patch('create_oci_engine.manager',side_effect=AssertionError('no engine probe')) as manager:
            for path in ('oci/v1/artifact-plans','oci/v2/workspace-plan','oci/v4/lxc-jobs',
                         'oci/v5/start-jobs','oci/v6/register-jobs','oci/v7/update-plans',
                         'oci/v8/recovery-status','oci/engine/v1/sessions','oci/manager/v1/view'):
                for method in ('GET','POST'):
                    self.assertEqual(h.client.open('/api/create/'+path,method=method).status_code,404)
        manager.assert_not_called()

    def test_creation_entry_uses_original_inventory_without_manager_page(self):
        inventory=(ROOT/'AppImage/components/virtual-machines.tsx').read_text()
        create=(ROOT/'AppImage/components/create-workloads.tsx').read_text()
        self.assertIn('<CreateWorkloads />',inventory)
        self.assertIn('from "./oci-manager-operation"',create)
        self.assertNotIn('from "./oci-manager"',create)
        for name in ('create-oci-lxc-creation.tsx','create-oci-first-start.tsx',
                     'create-oci-registration.tsx','create-oci-update.tsx','oci-manager.tsx'):
            self.assertFalse((ROOT/'AppImage/components'/name).exists())
        self.assertFalse((ROOT/'AppImage/app/create-testing').exists())

    def test_original_locale_values_are_preserved_and_new_en_sk_keys_match(self):
        def flatten(value,prefix=''):
            result={}
            for key,item in value.items():
                name=prefix+'.'+key if prefix else key
                if isinstance(item,dict):result.update(flatten(item,name))
                else:result[name]=item
            return result
        en=flatten(json.loads((ROOT/'AppImage/messages/en/common.json').read_text()))
        sk=flatten(json.loads((ROOT/'AppImage/messages/sk/common.json').read_text()))
        for prefix in ('create.','ociManager.'):
            self.assertEqual({k for k in en if k.startswith(prefix)}, {k for k in sk if k.startswith(prefix)})
            self.assertTrue(all(type(v)is str and v.strip() for k,v in en.items()if k.startswith(prefix)))
            self.assertTrue(all(type(v)is str and v.strip() for k,v in sk.items()if k.startswith(prefix)))

    def test_real_packaged_modules_import_without_prototype_or_host_access(self):
        builder=(ROOT/'AppImage/scripts/build_appimage.sh').read_text()
        start=builder.index('cp "$SCRIPT_DIR/create_access.py"')
        end=builder.index('# Backend notification',start)
        block=builder[start:end]
        self.assertNotIn('create_oci_lxc',block)
        with tempfile.TemporaryDirectory() as temporary:
            target=Path(temporary);(target/'usr/bin').mkdir(parents=True)
            script='set -eu\nSCRIPT_DIR='+str(ROOT/'AppImage/scripts')+'\nAPPIMAGE_ROOT='+str(ROOT/'AppImage')+'\nAPP_DIR='+str(target)+'\n'+block
            subprocess.run(['bash','-c',script],check=True,capture_output=True,text=True)
            expected=['create_access','create_catalog','create_oci_engine','flask_create_routes',
                      'native_lxc','native_lxc_jobs','native_lxc_pve','native_lxc_policy',
                      'native_lxc_activation','lxc_template_compat','lxc_template_archive',
                      'lxc_template_mtu','pve_task_status']
            for name in expected:
                self.assertEqual((target/'usr/bin'/f'{name}.py').read_bytes(),
                                 (ROOT/'AppImage/scripts'/f'{name}.py').read_bytes())
            self.assertEqual((target/'usr/share/proxmenux/catalog/helpers_cache.json').read_bytes(),
                             (ROOT/'json/helpers_cache.json').read_bytes())
            self.assertFalse(list(target.rglob('*create-profiles*')))
            code='''import importlib,os,sys,types
sys.path.insert(0,sys.argv[1])
def guard(event,args):
    if event in ('subprocess.Popen','os.system','socket.connect','os.mkdir','os.rename','os.remove'):
        raise AssertionError('forbidden effect: '+event)
    if event=='open' and args[2] & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND):
        raise AssertionError('forbidden write')
sys.addaudithook(guard)
sys.modules['jwt_middleware']=types.SimpleNamespace(require_auth=lambda f:f)
for name in sys.argv[2:]:importlib.import_module(name)
'''
            subprocess.run([sys.executable,'-I','-B','-c',code,str(target/'usr/bin'),*expected],
                           check=True,capture_output=True,text=True)


if __name__=='__main__':
    unittest.main()
