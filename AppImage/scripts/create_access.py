"""Authentication and request envelopes for the creation adapters only.

Reading a request never repairs credentials, detects the host or enters an
installer. The native engine remains the owner of the installation questions.
"""
import hashlib
import json
import re


class CheckError(ValueError):
    def __init__(self, code, status=400, field=None):
        super().__init__(code)
        self.code, self.status, self.field = code, status, field


def principal(config, authorization):
    import jwt
    from auth_manager import JWT_ALGORITHM, JWT_ISSUER, JWT_AUDIENCE
    if not isinstance(config, dict) or config.get('enabled') is not True or config.get('declined', False):
        raise CheckError('check_auth_required', 403)
    secret, revoked = config.get('jwt_secret'), config.get('revoked_tokens', [])
    if not isinstance(secret, str) or len(secret) < 32 or not isinstance(revoked, list) or any(not isinstance(v, str) for v in revoked):
        raise CheckError('check_auth_required', 403)
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        raise CheckError('check_auth_required', 401)
    if hashlib.sha256(parts[1].encode()).hexdigest() in revoked:
        raise CheckError('check_auth_required', 401)
    try:
        claims = jwt.decode(parts[1], secret, algorithms=[JWT_ALGORITHM], issuer=JWT_ISSUER,
                            audience=JWT_AUDIENCE, options={'require': ['username', 'exp', 'iss', 'aud']})
        owner = claims['username']
        if type(owner) is not str or not 1 <= len(owner) <= 128 or any(ord(c) < 32 for c in owner):
            raise ValueError()
    except (jwt.InvalidTokenError, ValueError, KeyError) as exc:
        raise CheckError('check_auth_required', 401) from exc
    if claims.get('scope', 'full_admin') != 'full_admin':
        raise CheckError('check_admin_required', 403)
    return owner


def strict_json(raw):
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate_key')
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non_finite')))


def request_guard(kind=None):
    from flask import current_app, request
    from auth_manager import load_auth_config
    owner = principal(load_auth_config(), request.headers.get('Authorization', ''))
    origin = request.headers.get('Origin')
    public = current_app.config.get('CREATE_OCI_PUBLIC_ORIGIN')
    if origin is not None and origin != request.host_url.rstrip('/') and not (
            type(public) is str and re.fullmatch(r'https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?', public)
            is not None and origin == public):
        raise CheckError('check_origin_forbidden', 403)
    if request.args:
        raise CheckError('check_request_invalid')
    context = current_app.config.get('CREATE_OCI_PROFILE_PLATFORM')
    try:
        product = context() if callable(context) else context
    except Exception:
        product = None
    if product != 'pve':
        raise CheckError('check_platform_forbidden' if product == 'pbs' else 'check_platform_unknown',
                         403 if product == 'pbs' else 503)
    if kind is None:
        if request.content_length:
            raise CheckError('check_request_invalid')
        return owner, None
    if request.mimetype != 'application/json':
        raise CheckError('check_content_type', 415)
    body_limit = 12 * 1024 * 1024 + 16384 if kind == 'engine-custom' else 131072
    if kind == 'engine-custom':
        request.max_content_length = body_limit
    if request.content_length is not None and request.content_length > body_limit:
        raise CheckError('check_body_large', 413)
    raw = request.stream.read(body_limit + 1)
    if len(raw) > body_limit:
        raise CheckError('check_body_large', 413)
    try:
        body = strict_json(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise CheckError('check_request_invalid') from None
    if not isinstance(body, dict):
        raise CheckError('check_request_invalid')
    return owner, body
