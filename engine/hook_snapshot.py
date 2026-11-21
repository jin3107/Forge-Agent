"""Prototype selection from inspected workspace bytes; not an execution gate."""
from __future__ import annotations

import copy
from typing import Any

from . import hook_inspection
from .hook_approvals import Decision, Snapshot


def select_entries(decision: Decision, snapshot: Snapshot | None,
                   event: str, global_config: dict[str, Any]) -> list[dict]:
    """Select from a checked snapshot without reopening the workspace config.

    The caller must obtain a fresh snapshot at every protected event and must
    enforce the decision. This pure helper is not an authentication boundary.
    """
    if not isinstance(snapshot, Snapshot) or not isinstance(global_config, dict) or not isinstance(event, str):
        raise ValueError('invalid hook snapshot')
    if snapshot.state == 'absent':
        if decision.state != 'absent' or decision.digest is not None or snapshot.digest is not None or snapshot.raw is not None:
            raise ValueError('invalid hook snapshot')
        cfg = {}
    elif snapshot.state in ('hooks', 'no_hooks'):
        expected = 'approved' if snapshot.state == 'hooks' else 'no_hooks'
        if decision.state != expected or not snapshot.digest or decision.digest != snapshot.digest:
            raise ValueError('invalid hook snapshot')
        checked = hook_inspection.inspect_bytes(snapshot.raw)
        if checked.state != snapshot.state or checked.digest != snapshot.digest or checked.config is None:
            raise ValueError('invalid hook snapshot')
        cfg = checked.config
    else:
        raise ValueError('invalid hook snapshot')

    chosen = cfg.get('hooks') or global_config.get('hooks') or {}
    if not isinstance(chosen, dict):
        raise ValueError('invalid global hooks')
    items = chosen.get(event) or []
    if isinstance(items, str):
        items = [{'command': items}]
    if not isinstance(items, list):
        raise ValueError('invalid hook entries')
    out = []
    for item in items:
        if isinstance(item, str):
            item = {'command': item}
        if not isinstance(item, dict) or not isinstance(item.get('command'), str) or not item['command']:
            raise ValueError('invalid hook entry')
        out.append(copy.deepcopy(item))
    if event == 'post_tool_use':
        legacy = cfg.get('on_file_change') or global_config.get('on_file_change')
        if legacy:
            if not isinstance(legacy, str):
                raise ValueError('invalid legacy hook')
            out.append({'command': legacy, 'matcher': 'write_file|edit_file', '_legacy_env': True})
    return out
