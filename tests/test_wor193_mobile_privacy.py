from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from osia.deletion_plan import ColossalDeletionPolicy, build_deletion_plan
from osia.server import server
from osia.system_resources import SystemResourceRegistry, WindowsRuntimeAdapter


class _Channel:
    def __init__(self):
        self.frames = []

    async def send_json(self, frame):
        self.frames.append(dict(frame))


def _plan(tmp_path: Path, *, clock=None):
    target = tmp_path / "private-mobile-fixture.txt"
    target.write_text("fixture", encoding="utf-8")
    unused = tmp_path / "never-system"
    registry = SystemResourceRegistry(
        platform_family="windows",
        windows=WindowsRuntimeAdapter(
            windows_directory=str(unused),
            system_directory=str(unused),
            system_windows_directory=str(unused),
            wrp_probe=lambda _path: False,
            storage_rows=(),
        ),
    )
    policy = ColossalDeletionPolicy(
        object_hard_limit=1,
        byte_hard_limit=10_000,
        root_hard_limit=10,
        object_risk_limit=10,
        byte_risk_limit=10_000,
        risk_score_limit=10,
    )
    return target, build_deletion_plan(
        [target],
        recursive=False,
        registry=registry,
        policy=policy,
        clock=clock,
    )


def test_mobile_frame_is_sanitized_nonapprovable_and_stale(tmp_path):
    target, plan = _plan(tmp_path)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    channel = _Channel()
    thread.start()
    token = server._turn_target.set((channel, loop))
    try:
        assert server._ws_reinforced_deletion_provider(plan) is False
    finally:
        server._turn_target.reset(token)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()

    assert len(channel.frames) == 1
    frame = channel.frames[0]
    assert frame["confirmation_kind"] == "colossal_deletion_pc_only"
    assert frame["can_approve"] is False
    assert set(frame) == {
        "type", "id", "server", "tool", "summary", "confirmation_kind",
        "can_approve", "files_count", "folders_count", "total_bytes",
        "root_count", "safe_reason", "expires_at",
    }
    assert str(target) not in repr(frame)
    assert server._resolve_confirm(frame["id"], True) is False


def test_expired_reinforced_request_is_not_sent_or_resurrected(tmp_path):
    now = time.time()
    _target, plan = _plan(tmp_path, clock=lambda: now - 60)
    channel = _Channel()
    loop = asyncio.new_event_loop()
    token = server._turn_target.set((channel, loop))
    try:
        assert server._ws_reinforced_deletion_provider(plan) is False
    finally:
        server._turn_target.reset(token)
        loop.close()
    assert channel.frames == []
    assert server._resolve_confirm(plan.plan_id, True) is False
