"""Keep native process failures readable even under a UTF-8 host locale."""
import subprocess
import sys
from pathlib import Path
from osia import agent


def test_native_non_utf8_stderr_reaches_caller_and_script_is_cleaned(monkeypatch):
    real_run = subprocess.run
    scripts=[]
    def arguments(path):
        scripts.append(path)
        return [sys.executable,'-c',"import sys; sys.stdout.buffer.write(b'known stdout'); sys.stderr.buffer.write(b'compiler error: \\x8a'); sys.exit(7)"]
    def utf8_host(*args,**kwargs):
        return real_run(*args,encoding='utf-8',**kwargs)
    monkeypatch.setattr(agent,'_windows_powershell_file_args',arguments)
    monkeypatch.setattr(agent.subprocess,'run',utf8_host)
    result=agent._run_windows_powershell('invalid syntax retained for feedback',10)
    assert result.returncode==7
    assert result.stdout=='known stdout'
    assert result.stderr=='compiler error: \ufffd'
    assert scripts and all(not Path(path).exists() for path in scripts)
