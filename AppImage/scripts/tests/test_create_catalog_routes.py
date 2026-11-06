import importlib.util
import json
import sys
import tempfile
import types
import unittest
import subprocess
import os
import shutil
from unittest import mock
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import create_catalog


ROUTES_PATH = SCRIPTS / "flask_create_routes.py"


def load_route_module():
    """Import the route module without requiring the AppImage runtime deps."""
    flask = types.ModuleType("flask")

    class Blueprint:
        def __init__(self, _name, _import_name, url_prefix=""):
            self.url_prefix = url_prefix
            self.routes = []

        def route(self, rule, methods=None):
            def decorate(function):
                self.routes.append((rule, tuple(methods or ()), function.__name__))
                return function
            return decorate

    flask.Blueprint = Blueprint
    flask.jsonify = lambda payload: payload
    jwt_middleware = types.ModuleType("jwt_middleware")
    jwt_middleware.require_auth = lambda function: function
    spec = importlib.util.spec_from_file_location("flask_create_routes_under_test", ROUTES_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"flask": flask, "jwt_middleware": jwt_middleware}):
        spec.loader.exec_module(module)
    return module


class CreateCatalogTests(unittest.TestCase):
    PUBLIC_APP_FIELDS = {
        "id", "title", "description", "website", "icon", "category", "category_label",
        "architectures", "template_status", "automatic_install_candidate",
        "requires_privileged_lxc", "requires_host_pid_namespace",
        "requires_relaxed_confinement", "requires_security_confirmation",
    }

    def write_catalog(self, document):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "index.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_exposes_only_visible_browser_safe_fields_in_title_order(self):
        path = self.write_catalog({
            "schema_version": "0.2.0",
            "applications": [
                {
                    "id": "zeta", "title": "Zeta", "description": "Visible app",
                    "website": "https://zeta.example", "icon": "javascript:alert(1)",
                    "category": "tools", "category_label": "Tools", "architectures": ["amd64", 7],
                    "template_status": "generated-unvalidated", "automatic_install_candidate": True,
                    "requires_privileged_lxc": True, "internal_only": "must not leak",
                },
                {"id": "hidden", "title": "Hidden", "hidden": True},
                {"id": "alpha", "title": "Alpha", "architectures": ["arm64"]},
            ],
        })

        payload = create_catalog.load_catalog_payload(path)

        self.assertTrue(payload["success"])
        self.assertEqual(payload["schema_version"], "0.2.0")
        self.assertEqual([app["id"] for app in payload["apps"]], ["alpha", "zeta"])
        self.assertNotIn("internal_only", payload["apps"][1])
        self.assertEqual(payload["apps"][1]["architectures"], ["amd64"])
        self.assertEqual(payload["apps"][1]["icon"], "")
        self.assertTrue(payload["apps"][1]["requires_privileged_lxc"])
        self.assertFalse(payload["apps"][0]["automatic_install_candidate"])

    def test_projection_discards_malformed_entries_and_never_serializes_runtime_data(self):
        path = self.write_catalog({
            "schema_version": {"secret": "schema-secret"},
            "deployment_environment": {"OCI_DEPLOY_TOKEN": "env-secret"},
            "host_paths": ["/var/lib/proxmox/private"],
            "applications": [
                "not an application object",
                {"id": "bad-hidden", "title": "Bad hidden", "hidden": "false"},
                {"id": ["bad-id"], "title": "Bad id"},
                {
                    "id": "safe", "title": "Safe app", "description": ["not text"],
                    "website": "ssh://host.example", "icon": "data:image/svg+xml,unsafe",
                    "category": {"not": "text"}, "architectures": ["amd64", 7, {"cpu": "x"}],
                    "template_status": ["not text"], "automatic_install_candidate": 1,
                    "requires_privileged_lxc": "yes", "requires_host_pid_namespace": None,
                    "requires_relaxed_confinement": {}, "requires_security_confirmation": [],
                    "environment": {"OCI_DEPLOY_TOKEN": "env-secret"},
                    "secrets": ["catalog-secret"],
                    "host_path": "/var/lib/proxmox/private",
                    "oci_lifecycle": {"operation": "pct create"},
                    "shell": "bash -lc 'unsafe'", "terminal": "pts/0",
                    "process": {"pid": 1234}, "command": "pct create 999",
                },
            ],
        })

        payload = create_catalog.load_catalog_payload(path)

        self.assertEqual(set(payload), {"success", "schema_version", "apps"})
        self.assertEqual(payload["schema_version"], "")
        self.assertEqual(len(payload["apps"]), 1)
        app = payload["apps"][0]
        self.assertEqual(set(app), self.PUBLIC_APP_FIELDS)
        self.assertEqual(app["architectures"], ["amd64"])
        self.assertEqual(app["description"], "")
        self.assertEqual(app["website"], "")
        self.assertEqual(app["icon"], "")
        self.assertEqual(app["category"], "")
        self.assertEqual(app["template_status"], "")
        self.assertFalse(app["automatic_install_candidate"])
        self.assertFalse(app["requires_privileged_lxc"])
        self.assertFalse(app["requires_host_pid_namespace"])
        self.assertFalse(app["requires_relaxed_confinement"])
        self.assertFalse(app["requires_security_confirmation"])

        serialized = json.dumps(payload)
        for value in (
            "schema-secret", "env-secret", "catalog-secret", "/var/lib/proxmox/private",
            "pct create", "bash -lc", "pts/0", "1234",
        ):
            self.assertNotIn(value, serialized)

    def test_rejects_missing_or_invalid_catalog_instead_of_returning_partial_data(self):
        missing = Path(tempfile.gettempdir()) / "does-not-exist-create-catalog.json"
        with self.assertRaisesRegex(create_catalog.CatalogReadError, "not available") as unavailable:
            create_catalog.load_catalog_payload(missing)
        self.assertEqual(unavailable.exception.code, "catalog_unavailable")

        malformed = self.write_catalog({"not_applications": []})
        with self.assertRaisesRegex(create_catalog.CatalogReadError, "invalid application list"):
            create_catalog.load_catalog_payload(malformed)

    def test_malformed_http_url_does_not_hide_the_other_catalog_entries(self):
        path = self.write_catalog({"applications": [
            {"id": "bad-url", "title": "Bad URL", "icon": "https://[invalid"},
            {"id": "safe", "title": "Safe"},
        ]})
        payload = create_catalog.load_catalog_payload(path)
        self.assertEqual(len(payload["apps"]), 2)
        self.assertEqual(payload["apps"][0]["icon"], "")

    def test_invalid_json_and_encoding_are_unavailable_not_empty_success(self):
        path = self.write_catalog({})
        for content in (b"{", b"\xff"):
            path.write_bytes(content)
            with self.assertRaises(create_catalog.CatalogReadError):
                create_catalog.load_catalog_payload(path)

    def test_catalog_and_creation_surface_excludes_prototype_and_management_routes(self):
        module = load_route_module()
        paths = {r[0] for r in module.create_bp.routes}
        self.assertIn('/catalog/installation', paths)
        self.assertIn('/lxc/v1/jobs', paths)
        self.assertIn('/oci/manager/v1/jobs', paths)
        self.assertNotIn('/oci/manager/v1/view', paths)
        self.assertFalse(any('/oci/engine/' in p or '/oci/v' in p for p in paths))
        source = ROUTES_PATH.read_text()
        for legacy in ('create_oci_artifacts', 'create_oci_lxc', 'create_oci_start', 'create_oci_update'):
            self.assertNotIn(legacy, source)

    def test_route_returns_the_projection_and_a_safe_catalog_error(self):
        routes = load_route_module()
        self.assertEqual(routes.create_bp.url_prefix, "/api/create")
        with mock.patch.object(routes, "load_catalog_payload", return_value={"success": True, "apps": []}):
            self.assertEqual(routes.get_catalog(), {"success": True, "apps": []})
        with mock.patch.object(routes, "load_catalog_payload", side_effect=create_catalog.CatalogReadError("unavailable")):
            self.assertEqual(routes.get_catalog(), ({"success": False, "error": "catalog_unavailable", "message": "unavailable"}, 503))


    def test_server_registers_the_read_only_blueprint_and_builder_includes_it(self):
        appimage = SCRIPTS.parent
        server = (SCRIPTS / "flask_server.py").read_text(encoding="utf-8")
        builder = (appimage / "scripts" / "build_appimage.sh").read_text(encoding="utf-8")
        self.assertIn("from flask_create_routes import create_bp", server)
        self.assertIn("app.register_blueprint(create_bp)", server)
        self.assertIn('cp "$SCRIPT_DIR/create_catalog.py"', builder)
        self.assertIn('cp "$SCRIPT_DIR/flask_create_routes.py"', builder)


class UnifiedCatalogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.oci = Path(self.tmp.name) / "oci.json"
        self.community = Path(self.tmp.name) / "community.json"
        self.oci.write_text(json.dumps({"applications": [{"id": "same", "title": "Same", "description": "**Bold** description"}]}))
        self.entries = [
            {"slug": "same", "name": "Same", "type": "lxc", "os": "debian", "category_names": ["Adblock & DNS"],
             "desc": "<p>Hello &amp; <b>world</b></p><script>do_not_show</script>", "privileged": True,
             "notes": ["private-default"], "script_url": "secret-url", "command": "pct create", "config_path": "/private"},
            {"slug": "same", "name": "Same", "type": "lxc", "os": "alpine", "is_dev": True},
            {"slug": "guest", "name": "Guest", "type": "vm", "os": "debian"},
            {"slug": "appliance", "name": "Appliance", "type": "turnkey", "os": "debian"},
            {"slug": "host", "name": "Host maintenance", "type": "pve"},
            {"slug": "addon", "name": "Existing CT addon", "type": "addon"},
            {"slug": "broken", "name": "Broken", "type": []}, None,
        ]
        self.community.write_text(json.dumps(self.entries))

    def payload(self):
        return create_catalog.load_unified_catalog(self.oci, self.community)

    def test_namespaced_identity_keeps_provider_os_variants_and_excludes_host_tools(self):
        data = self.payload()
        self.assertTrue(data["success"])
        self.assertEqual(len(data["apps"]), 5)
        self.assertEqual(len({a["id"] for a in data["apps"]}), 5)
        variants = [a for a in data["apps"] if a["title"] == "Same"]
        self.assertEqual({(a["provider"], a["variant"]) for a in variants}, {("oci", ""), ("community", "debian"), ("community", "alpine")})
        before = {(a["provider"], a["source_id"], a["variant"]): a["id"] for a in data["apps"]}
        self.community.write_text(json.dumps(list(reversed(self.entries))))
        self.assertEqual(before, {(a["provider"], a["source_id"], a["variant"]): a["id"] for a in self.payload()["apps"]})

    def test_normalizes_text_without_exporting_commands_notes_or_credentials(self):
        data = self.payload()
        serialized = json.dumps(data)
        for forbidden in ("private-default", "secret-url", "pct create", "/private", "do_not_show", "<script>", "**Bold**"):
            self.assertNotIn(forbidden, serialized)
        item = next(a for a in data["apps"] if a["provider"] == "community" and a["variant"] == "debian" and a["source_id"] == "same")
        self.assertEqual(item["description"], "Hello & world")
        self.assertEqual(item["category"], "adblock")
        self.assertEqual(item["architectures"], [], "has_arm is not an architecture qualification")
        self.assertTrue(item["requires_privileged_lxc"])
        self.assertEqual(item["template_status"], "catalog-only")

    def test_partial_failures_are_explicit_and_empty_sources_are_successful(self):
        for target in (self.oci, self.community):
            original = target.read_bytes()
            for invalid in (b"{", b"\xff", b"null"):
                target.write_bytes(invalid)
                data = self.payload()
                self.assertTrue(data["success"])
                provider = "oci" if target == self.oci else "community"
                self.assertFalse(data["sources"][provider]["available"])
                self.assertIsNone(data["sources"][provider]["count"], "unreadable is unknown, not an empty catalog")
                self.assertTrue(data["sources"]["community" if provider == "oci" else "oci"]["available"])
                self.assertTrue(all(a["provider"] != provider for a in data["apps"]))
            target.write_bytes(original)
        self.oci.write_text('{"applications": []}')
        self.community.write_text('[]')
        self.assertEqual(self.payload()["apps"], [])
        self.assertTrue(self.payload()["success"])
        self.oci.unlink(); self.community.unlink()
        self.assertFalse(self.payload()["success"])
        self.assertEqual(self.payload()["apps"], [])

    def test_duplicate_identity_fails_provider_instead_of_choosing_arbitrarily(self):
        self.community.write_text(json.dumps([self.entries[0], self.entries[0]]))
        self.assertFalse(self.payload()["sources"]["community"]["available"])
        self.assertTrue(self.payload()["sources"]["oci"]["available"])

    def test_distinct_oci_images_with_same_name_are_visibly_distinguishable(self):
        self.oci.write_text(json.dumps({"applications": [
            {"id": "same-one", "title": "Same"}, {"id": "same-two", "title": "Same"},
        ]}))
        entries = [app for app in self.payload()["apps"] if app["provider"] == "oci"]
        self.assertEqual({app["id"] for app in entries}, {"oci:same-one", "oci:same-two"})
        self.assertEqual({app["variant"] for app in entries}, {"same-one", "same-two"})
        self.assertEqual([app["id"] for app in create_catalog.load_catalog_payload(self.oci)["apps"]], ["same-one", "same-two"])

    def test_no_network_process_or_write_and_legacy_contract_unchanged(self):
        before = self.oci.read_bytes(), self.community.read_bytes()
        legacy = create_catalog.load_catalog_payload(self.oci)
        with mock.patch("subprocess.run", side_effect=AssertionError("No process")) as run, \
             mock.patch("subprocess.Popen", side_effect=AssertionError("No process")) as popen, \
             mock.patch("socket.socket", side_effect=AssertionError("No network")) as network, \
             mock.patch("pathlib.Path.write_text", side_effect=AssertionError("No write")) as write_text, \
             mock.patch("pathlib.Path.write_bytes", side_effect=AssertionError("No write")) as write_bytes:
            data = self.payload()
            for forbidden in (run, popen, network, write_text, write_bytes):
                forbidden.assert_not_called()
        self.assertTrue(data["success"])
        self.assertEqual(before, (self.oci.read_bytes(), self.community.read_bytes()))
        self.assertEqual(legacy, create_catalog.load_catalog_payload(self.oci))
        self.assertEqual(legacy["apps"][0]["id"], "same")
        self.assertNotIn("provider", legacy["apps"][0])

    def test_only_missing_local_cache_uses_explicit_bundled_snapshot(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(create_catalog, "COMMUNITY_CACHE_PATH", Path(self.tmp.name) / "missing.json"), \
             mock.patch.object(create_catalog, "BUNDLED_COMMUNITY_PATH", self.community), \
             mock.patch.object(create_catalog, "CATALOG_INDEX_PATH", self.oci):
            data = create_catalog.load_unified_catalog()
            self.assertEqual(data["sources"]["community"]["origin"], "bundled")
            with mock.patch.object(create_catalog, "COMMUNITY_CACHE_PATH", self.oci):
                data = create_catalog.load_unified_catalog()
                self.assertFalse(data["sources"]["community"]["available"], "wrong shape must not silently use fallback")

    def test_real_repository_cache_and_packaged_snapshot_contract(self):
        root = SCRIPTS.parents[1]
        data = create_catalog.load_unified_catalog(root / "oci/catalog/index.json", root / "json/helpers_cache.json")
        self.assertTrue(all(s["available"] for s in data["sources"].values()))
        self.assertEqual(len(data["apps"]), len({a["id"] for a in data["apps"]}))
        labels = [(a["title"].casefold(), a["provider"], a["workload_type"], a["variant"]) for a in data["apps"]]
        self.assertEqual(len(labels), len(set(labels)), "real catalog choices must be distinguishable to users too")
        self.assertTrue(all(a["workload_type"] in {"lxc", "vm"} for a in data["apps"]))
        self.assertTrue(any(a["provider"] == "community" for a in data["apps"]))
        self.assertTrue(any(a["provider"] == "oci" for a in data["apps"]))
        builder = (SCRIPTS / "build_appimage.sh").read_text()
        self.assertIn('cp "$APPIMAGE_ROOT/../json/helpers_cache.json" "$APP_DIR/usr/share/proxmenux/catalog/helpers_cache.json"', builder)

    def test_actual_builder_snapshot_lines_produce_a_readable_offline_bundle(self):
        root = SCRIPTS.parents[1]
        bundle = Path(self.tmp.name) / "bundle"
        (bundle / "usr/bin").mkdir(parents=True)
        shutil.copyfile(SCRIPTS / "create_catalog.py", bundle / "usr/bin/create_catalog.py")
        builder = (SCRIPTS / "build_appimage.sh").read_text()
        lines = [line for line in builder.splitlines() if '"$APP_DIR/usr/share/proxmenux/catalog' in line]
        self.assertEqual(len(lines), 2)
        subprocess.run(["bash", "-c", "\n".join(lines)], check=True,
                       env={**os.environ, "APPIMAGE_ROOT": str(root / "AppImage"), "APP_DIR": str(bundle)})
        spec = importlib.util.spec_from_file_location("bundled_create_fixture", bundle / "usr/bin/create_catalog.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch.object(module, "COMMUNITY_CACHE_PATH", Path(self.tmp.name) / "missing-host-cache.json"), \
             mock.patch.object(module, "CATALOG_INDEX_PATH", root / "oci/catalog/index.json"):
            result = module.load_unified_catalog()
        self.assertTrue(result["sources"]["community"]["available"])
        self.assertEqual(result["sources"]["community"]["origin"], "bundled")
        self.assertEqual(module.BUNDLED_COMMUNITY_PATH.read_bytes(), (root / "json/helpers_cache.json").read_bytes())
        self.assertFalse((bundle / "usr/share/proxmenux/catalog/oci-create-profiles.json").exists(),
                         "The creation preview must not package the old private OCI profile")


if __name__ == "__main__":
    unittest.main()
