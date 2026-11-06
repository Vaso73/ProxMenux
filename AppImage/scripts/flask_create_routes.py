"""Creation UI adapters to native Proxmox and the existing OCI wizard."""
import logging
from flask import Blueprint, jsonify
from create_access import CheckError, request_guard
from create_catalog import CatalogReadError, load_catalog_payload, load_unified_catalog
from jwt_middleware import require_auth

logger = logging.getLogger('proxmenux.create.routes')
create_bp = Blueprint('create', __name__, url_prefix='/api/create')
CREATE_OPERATIONS = ('catalog.install', 'custom.install')


def _manager_reply(action):
    from flask import current_app, request
    from create_access import CheckError
    backend = None
    try:
        owner, body = request_guard('engine-custom' if request.method == 'POST' else None)
        from create_oci_engine import manager
        backend = current_app.config.get('CREATE_OCI_MANAGER_BACKEND') or manager()
        result = action(backend, owner, body)
        response = jsonify({'success': True, **result})
    except CheckError as error:
        response = jsonify({'success': False, 'error': error.code})
        response.status_code = error.status
    except Exception as error:
        code, status = 'engine_unavailable', 503
        if backend is not None and isinstance(error, backend.Error):
            code, status = error.code, error.status
        response = jsonify({'success': False, 'error': code})
        response.status_code = status
    response.headers['Cache-Control'] = 'no-store'
    return response

@create_bp.route('/oci/manager/v1/context', methods=['GET'])
@require_auth
def manager_context():
    import socket
    return _manager_reply(lambda backend, owner, body: {
        'node': socket.gethostname(), 'operations': list(CREATE_OPERATIONS)})

@create_bp.route('/oci/manager/v1/jobs', methods=['GET', 'POST'])
@require_auth
def manager_jobs():
    from flask import request
    def action(backend, owner, body):
        if request.method == 'POST':
            if type(body.get('operation')) is not str or body['operation'] not in CREATE_OPERATIONS:
                raise CheckError('check_operation_forbidden', 400)
            return {'job': backend.Store().start_operation(owner, body)}
        return {'jobs': backend.Store().jobs(owner, operations=CREATE_OPERATIONS)}
    return _manager_reply(action)


def creation_store(backend, owner, job_id):
    store = backend.Store()
    _, value = store.owned(owner, job_id)
    if value.get('operation') not in CREATE_OPERATIONS:
        raise CheckError('check_operation_forbidden', 404)
    return store

@create_bp.route('/oci/manager/v1/jobs/<job_id>', methods=['GET'])
@require_auth
def manager_job(job_id):
    return _manager_reply(lambda backend, owner, body: {'job': creation_store(backend, owner, job_id).get(owner, job_id)})

@create_bp.route('/oci/manager/v1/jobs/<job_id>/answer', methods=['POST'])
@require_auth
def manager_answer(job_id):
    return _manager_reply(lambda backend, owner, body: {'job': creation_store(backend, owner, job_id).answer(owner, job_id, body)})

@create_bp.route('/oci/manager/v1/jobs/<job_id>/credentials', methods=['POST'])
@require_auth
def manager_credentials(job_id):
    def reveal(backend, owner, body):
        if body:
            raise backend.Error('invalid_request', 400)
        return {'credentials': creation_store(backend, owner, job_id).credentials(owner, job_id)}
    return _manager_reply(reveal)

def _native_lxc_reply(action):
    from flask import current_app, request
    from native_lxc import NativeError
    from native_lxc_jobs import service
    try:
        owner, body = request_guard('native-lxc' if request.method == 'POST' else None)
        backend = current_app.config.get('CREATE_NATIVE_LXC_BACKEND') or service()
        result = action(backend, owner, body)
        response, status = jsonify({'success': True, **result}), 200
    except NativeError as error:
        response, status = jsonify({'success': False, 'error': error.code, 'field': error.field}), error.status
    except Exception as error:
        from create_access import CheckError
        code, status = (error.code, error.status) if isinstance(error, CheckError) else ('native_unavailable', 503)
        response = jsonify({'success': False, 'error': code})
    response.headers['Cache-Control'] = 'no-store'
    return response, status

@create_bp.route('/lxc/v1/context', methods=['GET'])
def native_lxc_context():
    return _native_lxc_reply(lambda backend, owner, body: {'context': backend.context()})

@create_bp.route('/lxc/v1/checks', methods=['POST'])
def native_lxc_check():
    def check(backend, owner, body):
        from native_lxc import NativeError
        if set(body) != {'draft'}:
            raise NativeError('invalid_request', 400)
        return backend.check(owner, body['draft'])
    return _native_lxc_reply(check)

@create_bp.route('/lxc/v1/jobs', methods=['GET', 'POST'])
def native_lxc_jobs():
    from flask import request
    return _native_lxc_reply(lambda backend, owner, body: {'job': backend.submit(owner, body)}
                            if request.method == 'POST' else {'jobs': backend.jobs(owner)})

@create_bp.route('/lxc/v1/jobs/<job_id>', methods=['GET'])
def native_lxc_job(job_id):
    return _native_lxc_reply(lambda backend, owner, body: {'job': backend.get(owner, job_id)})

@create_bp.route('/lxc/v1/jobs/<job_id>/observe', methods=['GET'])
def native_lxc_observe(job_id):
    return _native_lxc_reply(lambda backend, owner, body: {'job': backend.observe(owner, job_id)})

@create_bp.route('/lxc/v1/template-downloads', methods=['POST'])
def native_lxc_template_download():
    def disabled(backend, owner, body):
        from native_lxc import NativeError
        raise NativeError('download_disabled', 503)
    return _native_lxc_reply(disabled)

@create_bp.route("/catalog", methods=["GET"])
@require_auth
def get_catalog():
    """Return a browser-safe read-only view of the canonical OCI catalog."""
    try:
        return jsonify(load_catalog_payload())
    except CatalogReadError as exc:
        logger.warning("Create catalog unavailable: %s", exc)
        return jsonify({"success": False, "error": exc.code, "message": str(exc)}), 503

@create_bp.route("/catalog/unified", methods=["GET"])
@require_auth
def get_unified_catalog():
    """Separate metadata contract; the original OCI-only route is unchanged."""
    payload = load_unified_catalog()
    return jsonify(payload), 200 if payload["success"] else 503

@create_bp.route("/catalog/installation", methods=["GET"])
@require_auth
def get_installation_catalog():
    """Live native Menu choices; legacy catalog contracts remain unchanged."""
    payload = load_unified_catalog(installation=True)
    return jsonify(payload), 200 if payload["success"] else 503
