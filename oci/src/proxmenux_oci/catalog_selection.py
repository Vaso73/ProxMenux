"""The OCI Menu selection rule and its read-only metadata view."""
from __future__ import annotations


def installable(applications: list[dict]) -> list[dict]:
    """Keep the Menu's established visibility/automatic-install behavior."""
    return [item for item in applications if not item.get("hidden") and item.get("automatic_install_candidate")]


def read_catalog(root):
    # Import only on a metadata request, never when Menu imports its selector.
    import sys
    from pathlib import Path
    source = str(Path(root) / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from proxmenux_oci.catalog import Catalog

    class ReadOnlyCatalog(Catalog):
        def sync_index(self):
            # Catalog.load_index normally bootstraps a missing index. A web
            # metadata request must not do so, including a disappearance race.
            raise FileNotFoundError("OCI catalog is not available")

    # Reading an index needs no GitHubSource object or registry credentials.
    value = ReadOnlyCatalog(Path(root), source=object(), read_only=True).load_index()
    if not isinstance(value, dict) or not isinstance(value.get("applications"), list):
        raise ValueError("Invalid OCI catalog")
    if not all(isinstance(item, dict) for item in value["applications"]):
        raise ValueError("Invalid OCI catalog entry")
    return {**value, "applications": installable(value["applications"])}
