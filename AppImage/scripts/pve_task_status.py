"""Proxmox UPID completion contract (PVE::UPID::status_is_error)."""
import re


def task_status_type(value):
    if not isinstance(value, str) or not value or value == 'unexpected status':
        return 'unknown'
    if value == 'OK':
        return 'ok'
    if re.fullmatch(r'WARNINGS: [0-9]+', value):
        return 'warning'
    return 'error'
