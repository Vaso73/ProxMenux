"""Typed presentation adapter to the native OCI menu, never another installer.

GET projections must not enter interactive_management/inventory: those perform
reconciliation. All writes below remain owned by the original menu functions.
"""
from __future__ import annotations

import importlib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import re
import sys
import time

OPERATIONS = {
    'catalog.install': {'app_id', 'mode'}, 'custom.install': set(),
    'registry.add': set(), 'registry.storage': set(),
    **{f'registry.{action}': {'registry'} for action in ('login', 'logout', 'inspect', 'download', 'install')},
    **{f'instance.{action}': {'vmid'} for action in ('update', 'modify', 'recreate', 'watchdog', 'remove', 'recover', 'status')},
    'updates.check': set(), 'updates.selected': {'vmids'},
    'restore.register': set(), 'rclone.mount': set(), 'images.cleanup': {'vmid'},
}


def validate(operation, parameters):
    if not isinstance(operation, str) or operation not in OPERATIONS or not isinstance(parameters, dict) or set(parameters) != OPERATIONS[operation]:
        raise ValueError('invalid_operation')
    if 'vmid' in parameters and (type(parameters['vmid']) is not int or not 100 <= parameters['vmid'] <= 999999999):
        raise ValueError('invalid_target')
    if 'vmids' in parameters:
        ids = parameters['vmids']
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 256 or len(set(map(str, ids))) != len(ids)
                or any(type(v) is not int or not 100 <= v <= 999999999 for v in ids)):
            raise ValueError('invalid_target')
    if 'registry' in parameters and (not isinstance(parameters['registry'], str)
            or not re.fullmatch(r'[A-Za-z0-9\[][A-Za-z0-9._:/\[\]-]{0,319}', parameters['registry'])
            or '..' in parameters['registry'].split('/')):
        raise ValueError('invalid_target')
    if operation == 'catalog.install' and (not isinstance(parameters['app_id'], str)
            or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,127}', parameters['app_id'])
            or parameters['mode'] not in ('default', 'advanced')):
        raise ValueError('invalid_target')
    return dict(parameters)


def native(project):
    for directory in ('src', 'remote'):
        path = str(Path(project) / directory)
        if path not in sys.path:
            sys.path.insert(0, path)
    return importlib.import_module('proxmenux_oci.management')


def snapshot(project):
    """Independent unavailable states; never infer an empty inventory from I/O failure."""
    management = native(project)
    import oci_instances as instances
    result = {'schema': 'oci.manager.v1', 'operations': list(OPERATIONS)}
    try:
        rows = management.saved_inventory(project, read_only=True) if instances.ROOT.exists() else []
        groups = {}
        by_id = {row['vmid']: row for row in rows}
        for row in rows:
            record = instances.read(instances.ROOT, row['vmid'])
            primary = (record.get('stack_member') or {}).get('primary_vmid') or row['vmid']
            if primary in groups:
                continue
            if primary not in by_id:
                raise ValueError('primary_missing')
            row = by_id[primary]
            record = instances.read(instances.ROOT, primary)
            # Native cache computes stack membership/readiness from the same records.
            members = (record.get('stack') or {}).get('members') or []
            groups[primary] = {**row, 'vmid': primary,
                'members': [{'vmid': item['vmid'], 'name': str(item.get('name', ''))[:200]} for item in members],
                'supports_update': not row['stack'] or management.stack_update_supported(record),
                'watchdog': (record.get('deployment') or {}).get('watchdog') is True}
        result['installed'] = {'available': True, 'items': list(groups.values())}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        result['installed'] = {'available': False, 'error': 'inventory_unavailable'}
    try:
        from .registry_auth import RegistryAuth
        accounts = RegistryAuth().accounts()
        result['registries'] = {'available': True, 'items': [
            {key: row.get(key) for key in ('registry', 'account', 'method', 'status')} for row in accounts]}
    except (OSError, ValueError, RuntimeError):
        result['registries'] = {'available': False, 'error': 'accounts_unavailable'}
    try:
        import oci_update_check
        result['updates'] = {'available': True, 'state': oci_update_check.cached()}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        result['updates'] = {'available': False, 'error': 'update_status_unavailable'}
    return result


def selected(project, vmid):
    management = native(project)
    rows = management.saved_inventory(project, read_only=True)
    row = next((r for r in rows if r['vmid'] == vmid), None)
    if row is None:
        raise ValueError('target_missing')
    return row


def run_operation(project, ui, operation, parameters):
    """Only fixed native entrypoints; parameters can never name code or commands."""
    parameters = validate(operation, parameters)
    management = native(project)
    from . import cli
    from .ui import UserCancelled
    if operation == 'catalog.install':
        catalog = cli.Catalog(Path(project), read_only=True)
        template = catalog.compose(parameters['app_id'])
        if not template.get('compatibility', {}).get('automatic_install_candidate'):
            raise ValueError('application_blocked')
        notice = cli.external_services_notice(template)
        if notice and not ui.confirm(notice, False):
            raise UserCancelled()
        return cli.install_template(ui, template, parameters['app_id'], parameters['mode'])
    if operation == 'custom.install':
        from .custom import install_custom
        return install_custom(ui)
    if operation.startswith('registry.'):
        from .registry_accounts import account_operation
        from .registry_auth import RegistryAuth
        auth, row = RegistryAuth(), None
        if 'registry' in parameters:
            row = next((r for r in auth.accounts() if r['registry'] == parameters['registry']), None)
            if row is None:
                raise ValueError('account_changed')
        return account_operation(ui, operation.split('.')[1], row, auth)
    if operation.startswith('instance.'):
        action = operation.split('.')[1]
        row = selected(project, parameters['vmid'])
        if action == 'status':
            import oci_instances as instances
            import oci_instance_transaction as transaction
            record = instances.read(instances.ROOT, row['vmid'])
            path = record.get('pending_stack_transaction') or record.get('pending_transaction')
            if not path:
                raise ValueError('operation_not_pending')
            value = json.loads(Path(path).read_text())
            if value.get('vmid', value.get('plan', {}).get('primary_vmid')) != row['vmid']:
                raise ValueError('journal_identity_changed')
            safe = {'vmid': row['vmid'], 'operation': value.get('operation'), 'phase': value.get('phase')}
            if type(value.get('stage')) is int:
                safe['stage'] = value['stage']
            if isinstance(value.get('backup'), str):
                safe['backup'] = value['backup']
            output = io.StringIO()
            with redirect_stdout(output):
                transaction.show_status(Path(path), safe)
            ui.message(output.getvalue())
            return {'operation': operation}
        if action in ('status', 'recover'):
            # The native management path offers the journal's actual choices;
            # a caller must not turn a non-pending instance into a recovery.
            if not (row['pending'] or row['stack_pending']):
                raise ValueError('operation_not_pending')
            ui.initial_choice = action
            action = None
        return management.manage_instance(Path(project), ui, row, action=action)
    if operation == 'restore.register':
        return management.offer_recovery(Path(project), ui)
    if operation == 'rclone.mount':
        return cli._rclone_mount(cli.Catalog(Path(project), read_only=True), ui)
    if operation == 'images.cleanup':
        from .images import offer_removal
        ui.defer_image_cleanup = False
        shown, removed = offer_removal(ui, [selected(project, parameters['vmid'])['vmid']])
        return bool(shown and removed)
    from .updates import UpdateCheck, select_updates
    if operation == 'updates.selected':
        with UpdateCheck(Path(project)) as checker:
            available = checker.status()['available']
        requested = set(parameters['vmids'])
        chosen = [row for row in available if row['vmid'] in requested]
        if {row['vmid'] for row in chosen} != requested:
            raise ValueError('updates_changed')
        ui.initial_selection = [str(row['vmid']) for row in chosen]
        # select_updates deliberately returns False after a batch; outcome is
        # captured by the native per-instance results, never inferred from it.
        outcomes = []
        original = management.manage_instance
        def tracked(*args, **kwargs):
            value = original(*args, **kwargs)
            outcomes.append(bool(value))
            return value
        management.manage_instance = tracked
        try:
            select_updates(Path(project), ui, chosen)
        finally:
            management.manage_instance = original
        return bool(outcomes and len(outcomes) == len(chosen) and all(outcomes))
    if operation == 'updates.check':
        from .i18n import translate
        if not ui.effect_review(translate('Check for updates')):
            raise UserCancelled()
        with UpdateCheck(Path(project)) as checker:
            checker.start()
            deadline = time.monotonic() + 1800
            while checker.active:
                checker.status()
                if time.monotonic() >= deadline:
                    raise TimeoutError('update_check_timeout')
                ui.info(checker.label(checker.status()))
                time.sleep(.5)
            if checker.failed:
                raise RuntimeError('update_check_failed')
            return {'operation': operation, 'unknown': len(checker.failed_applications)}
    raise ValueError('invalid_operation')
