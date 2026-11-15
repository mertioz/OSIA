"""No global cursor changes: verify native ownership through an injected API."""
import base64
import io
import json
import struct
import threading
import time
import sys
from pathlib import Path
import subprocess

import pytest

from osia.control_cursor import CURSOR_IDS, CursorSession, run_helper, validate_cursor_bytes


def cursor_bytes():
    width, height = 8, 8
    bitmap = bytes([0, 0, 0, 255]) * width * height
    mask = bytes(4 * height)
    dib = struct.pack("<IiiHHIIiiII", 40, width, height * 2, 1, 32, 0, len(bitmap), 0, 0, 0, 0) + bitmap + mask
    return struct.pack("<HHH", 0, 2, 1) + struct.pack("<BBBBHHII", width, height, 0, 0, 1, 1, len(dib), 22) + dib


def packet():
    value = base64.b64encode(cursor_bytes()).decode("ascii")
    return {"arrow": value, "hand": value}


class API:
    def __init__(self, fail_backup=False, fail_replace=False):
        self.calls = []
        self.fail_backup = fail_backup
        self.fail_replace = fail_replace

    def copy_original(self, identity):
        self.calls.append(("backup", identity))
        if self.fail_backup and identity == CURSOR_IDS["hand"]:
            raise OSError("backup")
        return f"original-{identity}"

    def load(self, path):
        validate_cursor_bytes(path.read_bytes())
        return f"new-{path.stem}"

    def replace(self, handle, identity):
        self.calls.append(("replace", handle, identity))
        if self.fail_replace and handle == "new-hand":
            raise OSError("replace")

    def destroy(self, handle):
        self.calls.append(("destroy", handle))


def test_originals_are_backed_up_before_any_change_and_restored_once(tmp_path):
    api = API()
    session = CursorSession(api)
    session.apply(packet(), tmp_path)
    session.apply(packet(), tmp_path)  # theme change must not back up our own cursors
    assert api.calls[:2] == [("backup", 32512), ("backup", 32649)]
    assert sum(call[0] == "backup" for call in api.calls) == 2
    session.restore()
    session.restore()
    assert api.calls[-2:] == [("replace", "original-32512", 32512), ("replace", "original-32649", 32649)]


def test_backup_failure_never_replaces_any_cursor(tmp_path):
    api = API(fail_backup=True)
    with pytest.raises(OSError):
        CursorSession(api).apply(packet(), tmp_path)
    assert not any(call[0] == "replace" for call in api.calls)
    assert api.calls[-1] == ("destroy", "original-32512")


def test_partial_failure_restores_both_originals(tmp_path):
    api = API(fail_replace=True)
    with pytest.raises(OSError):
        CursorSession(api).apply(packet(), tmp_path)
    assert api.calls[-2:] == [("replace", "original-32512", 32512), ("replace", "original-32649", 32649)]


@pytest.mark.parametrize("ending", [b"", b'{"active":false}\n', b'broken json\n'])
def test_pipe_eof_stop_and_invalid_input_restore_original_cursors(ending):
    api = API()
    stream = io.BytesIO(json.dumps({"active": True, "cursors": packet()}).encode() + b"\n" + ending)
    assert run_helper(stream, api=api) == 0
    assert api.calls[-2:] == [("replace", "original-32512", 32512), ("replace", "original-32649", 32649)]


def test_malformed_cursor_never_reaches_native_loader(tmp_path):
    for value in (b"", bytes(100), cursor_bytes()[:-1], cursor_bytes() + b"extra"):
        with pytest.raises(ValueError):
            validate_cursor_bytes(value)
    api = API()
    data = packet()
    data["hand"] = base64.b64encode(b"invalid").decode()
    with pytest.raises(ValueError):
        CursorSession(api).apply(data, tmp_path)
    assert not api.calls


def test_missing_parent_heartbeat_restores_without_waiting_for_pipe_eof():
    api = API()
    release = threading.Event()

    class HungParent:
        first = True

        def readline(self, _limit):
            if self.first:
                self.first = False
                return json.dumps({"active": True, "cursors": packet()}).encode() + b"\n"
            release.wait(5)
            return b""

    helper = threading.Thread(target=run_helper, args=(HungParent(),), kwargs={"api": api})
    helper.start()
    try:
        deadline = time.monotonic() + 4
        while not any(call[:2] == ("replace", "original-32512") for call in api.calls) and time.monotonic() < deadline:
            time.sleep(0.02)
        assert api.calls[-2:] == [("replace", "original-32512", 32512), ("replace", "original-32649", 32649)]
    finally:
        release.set()
        helper.join(3)
    assert not helper.is_alive()


def test_windowed_launcher_routes_helper_without_gui_start(monkeypatch):
    import runpy
    from pathlib import Path
    import osia.control_cursor as module

    launcher = runpy.run_path(str(Path(__file__).resolve().parents[1] / "packaging/windows/launcher.py"))
    monkeypatch.setattr(module, "run_helper", lambda: 17)
    assert launcher["main"]([module.CURSOR_HELPER_MODE]) == 17
    assert launcher["main"]([module.CURSOR_HELPER_MODE, "unexpected"]) == 2


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native cursor resources")
def test_real_windows_cursor_resource_load_and_backup_without_display(tmp_path):
    from osia.control_cursor import WindowsCursorAPI
    from osia.gui.desktop_feedback import cursor_file_bytes, cursor_image
    from PyQt6.QtGui import QColor

    api = WindowsCursorAPI()
    for name, identity in CURSOR_IDS.items():
        path = tmp_path / (name + ".cur")
        path.write_bytes(cursor_file_bytes(cursor_image(QColor("#c4b5f5"), hand=name == "hand"), 3, 3))
        handle = api.load(path)
        assert handle
        api.destroy(handle)
        saved = api.copy_original(identity)
        assert saved
        api.destroy(saved)
    # Intentionally never calls SetSystemCursor: the user's pointer is untouched.


@pytest.mark.skipif(sys.platform != "win32", reason="Windows pythonw inherited stdin")
def test_pythonw_inherited_pipe_restores_on_parent_eof(tmp_path):
    output = tmp_path / "restored.json"
    code = '''
import json, pathlib, sys
from osia.control_cursor import run_helper
class API:
    def __init__(self): self.calls=[]
    def copy_original(self, i): return 'original-'+str(i)
    def load(self, p): return 'new-'+p.stem
    def replace(self, h, i): self.calls.append([h,i])
    def destroy(self, h): pass
api=API()
result=run_helper(api=api)
pathlib.Path(sys.argv[1]).write_text(json.dumps({'result':result,'calls':api.calls}))
'''
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    process = subprocess.Popen([str(pythonw), "-c", code, str(output)], stdin=subprocess.PIPE)
    process.communicate(json.dumps({"active": True, "cursors": packet()}).encode()+b"\n",timeout=10)
    assert process.returncode == 0
    result = json.loads(output.read_text())
    assert result["result"] == 0
    assert result["calls"][-2:] == [["original-32512", 32512], ["original-32649", 32649]]
