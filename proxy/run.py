#!/usr/bin/env python3
"""监听 8080 反向代理 Jellyfin，并在 8081 提供配置页面。"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from config_server import start_config_server
from mapping import DEFAULT_MITM_DIR, LISTEN_PORT, mapping_path, read_upstream


class ProxySupervisor:
    """mitmdump 的转发目标在启动时确定。上游变更后停掉并按新地址再拉起。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._mitm_dir = os.environ.get("MITM_CONFDIR", str(DEFAULT_MITM_DIR))
        self._addon = Path(__file__).resolve().parent / "addon.py"
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._call_lock = threading.Lock()
        self._restart = threading.Event()
        self._shutdown = threading.Event()
        self._ready = threading.Event()
        self._error: str | None = None

    def restart(self) -> None:
        with self._call_lock:
            with self._lock:
                if self._shutdown.is_set():
                    raise RuntimeError("进程正在退出")
                proc = self._proc
                self._error = None
                self._ready.clear()
                self._restart.set()
            self._stop(proc)
            if not self._ready.wait(20):
                raise TimeoutError("反代重启超时")
            if self._shutdown.is_set():
                raise RuntimeError("进程正在退出")
            if self._error:
                raise RuntimeError(self._error)

    def run(self) -> int:
        Path(self._mitm_dir).mkdir(parents=True, exist_ok=True)
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)
        first = True
        while not self._shutdown.is_set():
            proc = self._spawn()
            if not self._wait_until_listening(proc):
                if self._shutdown.is_set():
                    self._ready.set()
                    return 0
                # 启动过程中又收到一次重启，这次失败不算数，按新配置再拉起。
                if self._restart.is_set():
                    self._restart.clear()
                    continue
                self._error = self._exit_error(proc)
                self._ready.set()
                if first:
                    return proc.returncode if proc.returncode is not None else 1
                if not self._wait_restart():
                    return 0
                continue
            first = False
            self._ready.set()
            proc.wait()
            if self._shutdown.is_set() or not self._restart.is_set():
                return proc.returncode if proc.returncode is not None else 0
            self._restart.clear()
        return 0

    def _spawn(self) -> subprocess.Popen[bytes]:
        upstream = read_upstream(self._path)
        argv = [
            "mitmdump",
            "-s",
            str(self._addon),
            "--set",
            f"confdir={self._mitm_dir}",
            "--listen-host",
            "0.0.0.0",
            "--listen-port",
            str(LISTEN_PORT),
            "--mode",
            f"reverse:{upstream}",
        ]
        print(
            f"jellyfin-openlist: listen=0.0.0.0:{LISTEN_PORT} reverse -> {upstream}",
            file=sys.stderr,
        )
        proc = subprocess.Popen(argv)
        with self._lock:
            self._proc = proc
        return proc

    def _wait_until_listening(self, proc: subprocess.Popen[bytes]) -> bool:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self._shutdown.is_set() or proc.poll() is not None:
                return False
            try:
                with socket.create_connection(("127.0.0.1", LISTEN_PORT), 0.2):
                    return True
            except OSError:
                time.sleep(0.05)
        return False

    def _wait_restart(self) -> bool:
        while not self._shutdown.is_set() and not self._restart.is_set():
            self._restart.wait(0.2)
        if self._shutdown.is_set():
            return False
        self._restart.clear()
        return True

    def _on_signal(self, signum: int, frame: object) -> None:
        self._shutdown.set()
        self._error = "进程正在退出"
        self._ready.set()
        self._restart.set()
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            proc.terminate()

    @staticmethod
    def _exit_error(proc: subprocess.Popen[bytes]) -> str:
        code = proc.poll()
        if code is None:
            return "反代没有监听成功"
        return f"反代退出（代码 {code}）"

    @staticmethod
    def _stop(proc: subprocess.Popen[bytes] | None) -> None:
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def main() -> None:
    supervisor = ProxySupervisor(mapping_path())
    try:
        config_port_bound = start_config_server(supervisor.restart)
    except OSError as exc:
        config_port_bound = None
        print(f"配置页面未启动: {exc}", file=sys.stderr)
    if config_port_bound is not None:
        print(f"配置页面 http://127.0.0.1:{config_port_bound}", file=sys.stderr)
    raise SystemExit(supervisor.run())


if __name__ == "__main__":
    main()
