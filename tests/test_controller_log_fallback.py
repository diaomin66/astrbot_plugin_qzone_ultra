from __future__ import annotations

from qzone_bridge.controller import QzoneDaemonController


def test_spawn_daemon_falls_back_when_daemon_log_is_not_writable(tmp_path, monkeypatch):
    blocked_log = tmp_path / "daemon.log"
    blocked_log.mkdir()
    temp_root = tmp_path / "temp"
    captured = {}

    def fake_popen(cmd, cwd=None, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["stdout_name"] = getattr(kwargs["stdout"], "name", "")
        captured["stderr_name"] = getattr(kwargs["stderr"], "name", "")

        class _Process:
            pid = 12345

            def poll(self):
                return None

        return _Process()

    monkeypatch.setattr("qzone_bridge.controller.tempfile.gettempdir", lambda: str(temp_root))
    monkeypatch.setattr("qzone_bridge.controller.subprocess.Popen", fake_popen)
    controller = QzoneDaemonController(plugin_root=tmp_path, data_dir=tmp_path / "data")
    monkeypatch.setattr(controller, "_daemon_log_path", lambda: blocked_log)

    controller._spawn_daemon(18999)

    assert "daemon_main.py" in str(captured["cmd"][1])
    assert str(blocked_log) not in captured["stdout_name"]
    assert captured["stdout_name"] == captured["stderr_name"]
    assert str(temp_root / "astrbot_qzone_daemon_logs") in captured["stdout_name"]
