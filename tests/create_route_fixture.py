"""Real shipped authentication and Flask routes with synthetic keys only."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock
from flask import Flask
import test_host_update_auth as auth_fixture

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'AppImage/scripts'))
# Keep common modules resident when patch.dict restores the module registry.
import create_access
import create_catalog


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RouteFixture(unittest.TestCase):
    def setUp(self):
        self.jwt_fixture = auth_fixture.AuthReasons()
        self.jwt_fixture.setUp()
        self.addCleanup(self.jwt_fixture.doCleanups)
        self.auth_config = {'enabled': True, 'declined': False,
                            'jwt_secret': self.jwt_fixture.scope['_get_jwt_secret'](), 'revoked_tokens': []}
        auth = types.ModuleType('auth_manager')
        auth.load_auth_config = lambda: self.auth_config
        auth.verify_token_full = self.jwt_fixture.scope['verify_token_full']
        auth.verify_token = lambda token: auth.verify_token_full(token)[0]
        for name in ('JWT_ALGORITHM', 'JWT_ISSUER', 'JWT_AUDIENCE'):
            setattr(auth, name, self.jwt_fixture.scope[name])
        modules = mock.patch.dict(sys.modules, {'auth_manager': auth})
        modules.start()
        self.addCleanup(modules.stop)
        middleware = load('create_fixture_jwt', ROOT / 'AppImage/scripts/jwt_middleware.py')
        with mock.patch.dict(sys.modules, {'jwt_middleware': middleware}):
            self.routes = load('create_fixture_routes', ROOT / 'AppImage/scripts/flask_create_routes.py')
        self.app = Flask(__name__)
        self.app.register_blueprint(self.routes.create_bp)
        self.app.config['CREATE_OCI_PROFILE_PLATFORM'] = 'pve'
        self.client = self.app.test_client()
        self.token = self.jwt_fixture.token(scope='full_admin')
