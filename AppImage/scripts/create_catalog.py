"""Read-only projection of the canonical OCI catalog for the Create UI.

This module intentionally knows nothing about deployment.  The browser can
only browse the engine's published catalog through the small, stable payload
defined here.  A later plan/confirm/action-token contract must be added before
any endpoint may create a workload.
"""

from __future__ import annotations

import json
import os
import hashlib
import re
from html.parser import HTMLParser
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_CATALOG_INDEX = Path(
    "/usr/local/share/proxmenux/oci/engine/catalog/index.json"
)
CATALOG_INDEX_PATH = Path(os.environ.get("PROXMENUX_OCI_CATALOG_INDEX", DEFAULT_CATALOG_INDEX))
COMMUNITY_CACHE_PATH = Path("/usr/local/share/proxmenux/helpers_cache.json")
BUNDLED_COMMUNITY_PATH = Path(__file__).resolve().parent.parent / "share/proxmenux/catalog/helpers_cache.json"


class CatalogReadError(RuntimeError):
    """The canonical catalog cannot be safely presented to the browser."""

    def __init__(self, message: str, code: str = "catalog_unavailable") -> None:
        super().__init__(message)
        self.code = code


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _http_url(value: object) -> str:
    candidate = _text(value)
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return candidate
    return ""


def _app_payload(item: dict[str, Any]) -> dict[str, object] | None:
    """Return the browser-safe fields for one visible catalog application."""
    app_id = _text(item.get("id"))
    title = _text(item.get("title"))
    hidden = item.get("hidden", False)
    if not app_id or not title or not isinstance(hidden, bool) or hidden:
        return None

    return {
        "id": app_id,
        "title": title,
        "description": _text(item.get("description")),
        "website": _http_url(item.get("website")),
        "icon": _http_url(item.get("icon")),
        "category": _text(item.get("category")),
        "category_label": _text(item.get("category_label")),
        "architectures": _string_list(item.get("architectures")),
        "template_status": _text(item.get("template_status")),
        "automatic_install_candidate": item.get("automatic_install_candidate") is True,
        "requires_privileged_lxc": item.get("requires_privileged_lxc") is True,
        "requires_host_pid_namespace": item.get("requires_host_pid_namespace") is True,
        "requires_relaxed_confinement": item.get("requires_relaxed_confinement") is True,
        "requires_security_confirmation": item.get("requires_security_confirmation") is True,
    }


def load_catalog_payload(path: Path | None = None) -> dict[str, object]:
    """Load and reduce the canonical OCI catalog without starting any action."""
    catalog_path = path or CATALOG_INDEX_PATH
    try:
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CatalogReadError("The OCI catalog is not available on this server.") from exc
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise CatalogReadError("The OCI catalog could not be read safely.") from exc

    applications = raw.get("applications") if isinstance(raw, dict) else None
    if not isinstance(applications, list):
        raise CatalogReadError("The OCI catalog has an invalid application list.")

    visible = [payload for item in applications if isinstance(item, dict)
               if (payload := _app_payload(item)) is not None]
    visible.sort(key=lambda item: str(item["title"]).casefold())
    return {
        "success": True,
        "schema_version": _text(raw.get("schema_version")) if isinstance(raw, dict) else "",
        "apps": visible,
    }


class _DescriptionText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.suppressed += 1
        self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.suppressed:
            self.suppressed -= 1
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)


def _description(value: object) -> str:
    """Normalize catalog prose as text; never send executable markup."""
    parser = _DescriptionText()
    try:
        parser.feed(_text(value))
        text = "".join(parser.parts)
    except (ValueError, AssertionError):
        text = _text(value)  # Still rendered as text, never raw HTML.
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return " ".join(text.split())


COMMUNITY_CATEGORIES = {
    "Adblock & DNS": "adblock", "AI / Coding & Dev-Tools": "ai", "*Arr Suite": "arr",
    "Automation & Scheduling": "automation", "Backup & Recovery": "backup",
    "Business & ERP": "business", "Communication & Community": "communication",
    "Containers & Docker": "containers", "Databases": "databases",
    "Dashboards & Frontends": "dashboards", "Documents & Notes": "documents",
    "Files & Downloads": "downloads", "Finance & Budgeting": "finance",
    "Gaming & Leisure": "gaming", "Host Management": "management",
    "IoT & Smart Home": "smarthome", "Media & Streaming": "media",
    "Messaging & Queues": "messaging", "Miscellaneous": "misc",
    "Monitoring & Analytics": "monitoring", "NVR & Cameras": "nvr",
    "Network & Firewall": "network", "Operating Systems & Appliances": "os",
    "Productivity & Workflows": "productivity", "Remote Access & VPN": "remote",
    "Authentication & Security": "security", "Webservers & Proxies": "web",
    "ZigBee, Z-Wave & Matter": "smartdevices",
}


def _community_catalog(path: Path) -> list[dict[str, object]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CatalogReadError("The Community Scripts catalog is unavailable.") from exc
    if not isinstance(raw, list):
        raise CatalogReadError("The Community Scripts catalog has an invalid list.")
    apps = []
    identities = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        kind = _text(item.get("type"))
        if kind not in {"lxc", "vm", "turnkey"}:
            continue
        slug, title, system = (_text(item.get(k)) for k in ("slug", "name", "os"))
        if not slug or not title:
            continue
        identity = json.dumps([slug, kind, system], separators=(",", ":"))
        key = "community:" + hashlib.sha256(identity.encode()).hexdigest()
        if key in identities:
            raise CatalogReadError("The Community Scripts catalog has ambiguous entries.")
        identities.add(key)
        labels = _string_list(item.get("category_names"))
        label = labels[0] if labels else ""
        apps.append({
            "id": key, "source_id": slug, "provider": "community",
            "workload_type": "vm" if kind == "vm" else "lxc",
            "variant": system, "development": item.get("is_dev") is True,
            "title": title, "description": _description(item.get("desc")),
            "website": _http_url(item.get("website")), "icon": _http_url(item.get("logo")),
            "category": COMMUNITY_CATEGORIES.get(label, "misc"), "category_label": label,
            # has_arm describes cache metadata, not a verified architecture
            # contract for this particular script + OS combination.
            "architectures": [], "template_status": "catalog-only",
            "requires_privileged_lxc": item.get("privileged") is True,
            "requires_host_pid_namespace": False, "requires_relaxed_confinement": False,
            "requires_security_confirmation": False,
        })
    return apps


def _community_source() -> tuple[Path, str]:
    override = os.environ.get("PROXMENUX_COMMUNITY_CATALOG")
    if override:
        return Path(override), "local"
    # A corrupt local cache remains an error; only an absent one uses the
    # explicitly labelled bundled snapshot. Never download during browsing.
    if COMMUNITY_CACHE_PATH.exists():
        return COMMUNITY_CACHE_PATH, "local"
    return BUNDLED_COMMUNITY_PATH, "bundled"


def load_installation_catalog_payload() -> dict[str, object]:
    """Project only the native engine's installation choices, never a bundle."""
    try:
        from create_oci_engine import catalog
        raw = catalog()
        if not isinstance(raw, dict) or not isinstance(raw.get("applications"), list):
            raise ValueError("Invalid engine catalog")
        entries = raw["applications"]
        if not all(isinstance(item, dict) for item in entries):
            raise ValueError("Invalid engine entry")
        apps = [_app_payload(item) for item in entries]
        if any(item is None for item in apps):
            raise ValueError("Incomplete engine entry")
        return {"success": True, "schema_version": _text(raw.get("schema_version")),
                "apps": sorted(apps, key=lambda item: str(item["title"]).casefold())}
    except Exception as exc:
        raise CatalogReadError("The OCI engine catalog is unavailable.") from exc


def load_unified_catalog(oci_path: Path | None = None, community_path: Path | None = None,
                         *, installation: bool = False) -> dict[str, object]:
    apps = []
    sources = {}
    for provider in ("oci", "community"):
        origin = "local"
        try:
            if provider == "oci":
                if installation:
                    origin = "engine"
                payload = load_installation_catalog_payload() if installation else load_catalog_payload(oci_path)
                entries = [{**app, "id": "oci:" + str(app["id"]), "source_id": app["id"],
                            "provider": "oci", "workload_type": "lxc", "variant": "",
                            "development": False, "description": _description(app["description"])}
                           for app in payload["apps"]]
            else:
                path, origin = (community_path, "local") if community_path is not None else _community_source()
                entries = _community_catalog(path)
            if len({entry["id"] for entry in entries}) != len(entries):
                raise CatalogReadError("The catalog has ambiguous entries.")
            labels = Counter((str(entry["title"]).casefold(), entry["workload_type"], entry["variant"]) for entry in entries)
            for entry in entries:
                label = (str(entry["title"]).casefold(), entry["workload_type"], entry["variant"])
                if labels[label] > 1:
                    # Distinct images/scripts may share a display name. Keep
                    # their stable IDs and make the difference visible too.
                    entry["variant"] = " · ".join(filter(None, [entry["variant"], str(entry["source_id"])]))
            apps.extend(entries)
            sources[provider] = {"available": True, "count": len(entries), "origin": origin}
        except (CatalogReadError, OSError):
            sources[provider] = {"available": False, "count": None, "origin": origin}
    apps.sort(key=lambda app: (str(app["title"]).casefold(), str(app["provider"]), str(app["variant"])))
    return {"success": any(s["available"] for s in sources.values()), "schema_version": "1",
            "apps": apps, "sources": sources}
