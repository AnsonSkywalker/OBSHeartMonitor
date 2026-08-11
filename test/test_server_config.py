# -*- coding: utf-8 -*-
"""OBS 服务配置与优雅关闭的离线测试（无需真实蓝牙/显示器）。

覆盖：local_config.json 白名单读写（原子写）、/config 端点、uvicorn 优雅停止、
heart.py 心率倍率 slot 与 stopServer 后台化。

运行: py -m unittest test.test_server_config -v
"""
import asyncio
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fastApi  # noqa: E402
import heart  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ConfigFileTest(unittest.TestCase):
    """local_config.json 白名单读写（指向临时文件，不污染真实配置）。"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._cfg = os.path.join(self._tmpdir, "local_config.json")
        with open(self._cfg, "w", encoding="utf-8") as f:
            json.dump({"maxHeartRate": 196}, f)
        self._orig_path = fastApi._CONFIG_PATH
        fastApi._CONFIG_PATH = self._cfg

    def tearDown(self):
        fastApi._CONFIG_PATH = self._orig_path
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_save_and_load_multiplier(self):
        self.assertEqual(fastApi.save_config({"heartRateMultiplier": 1.25}), 1.25)
        data = fastApi.load_config()
        self.assertEqual(data, {"maxHeartRate": 196, "heartRateMultiplier": 1.25})

    def test_save_keeps_other_keys(self):
        fastApi.save_config({"heartRateMultiplier": 2})
        self.assertEqual(fastApi.load_config()["maxHeartRate"], 196)

    def test_atomic_write_no_tmp_leftover(self):
        fastApi.save_config({"heartRateMultiplier": 1.25})
        self.assertFalse(os.path.exists(self._cfg + ".tmp"))

    def test_invalid_values_rejected(self):
        for bad in (3, 0, -1, "1.25", None, True):
            with self.assertRaises(ValueError, msg=f"应拒绝 {bad!r}"):
                fastApi.save_config({"heartRateMultiplier": bad})
        # 拒绝后原配置未被破坏
        self.assertEqual(fastApi.load_config()["maxHeartRate"], 196)

    def test_get_config_whitelist(self):
        fastApi.save_config({"heartRateMultiplier": 1.25})
        resp = asyncio.run(fastApi.get_config())
        self.assertEqual(resp, {"maxHeartRate": 196, "heartRateMultiplier": 1.25})

    def test_get_config_defaults(self):
        resp = asyncio.run(fastApi.get_config())
        self.assertEqual(resp, {"maxHeartRate": 196, "heartRateMultiplier": 1})

    def test_stop_without_start(self):
        """从未启动服务时 stop() 应返回 False 且不抛错。"""
        fastApi.__dict__.pop("webServer", None)
        self.assertIs(fastApi.stop(), False)


class UvicornGracefulStopTest(unittest.TestCase):
    """uvicorn 启动后应能被优雅停止：无 traceback、端口释放。"""

    def setUp(self):
        # 服务器测试期间把配置指向临时文件，避免 POST /config 污染真实 local_config.json
        self._tmpdir = tempfile.mkdtemp()
        self._cfg = os.path.join(self._tmpdir, "local_config.json")
        with open(self._cfg, "w", encoding="utf-8") as f:
            json.dump({"maxHeartRate": 196}, f)
        self._orig_path = fastApi._CONFIG_PATH
        fastApi._CONFIG_PATH = self._cfg

    def tearDown(self):
        fastApi._CONFIG_PATH = self._orig_path
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_graceful_stop(self):
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        errors = []

        def server_main():
            try:
                fastApi.start(port)
            except BaseException as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")

        t = threading.Thread(target=server_main, daemon=True)
        t.start()
        try:
            # 等待端口就绪
            ready = False
            for _ in range(50):
                try:
                    with urllib.request.urlopen(base + "/", timeout=5) as r:
                        self.assertEqual(r.status, 200)
                        ready = True
                        break
                except Exception:
                    time.sleep(0.2)
            self.assertTrue(ready, "uvicorn 端口未就绪")

            # 同源 POST /config 白名单
            req = urllib.request.Request(
                base + "/config",
                data=json.dumps({"heartRateMultiplier": 2}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=5) as r:
                self.assertEqual(r.status, 200)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(urllib.request.Request(
                    base + "/config",
                    data=json.dumps({"heartRateMultiplier": 7}).encode("utf-8"),
                    headers={"Content-Type": "application/json"}, method="POST"), timeout=5)
            self.assertEqual(cm.exception.code, 400)

            # 优雅停止
            fastApi.stop()
            t.join(timeout=10)
            self.assertFalse(t.is_alive(), "uvicorn 线程未退出")
            self.assertEqual(errors, [], f"退出过程出现异常: {errors}")
            # 端口应已释放
            with self.assertRaises(Exception):
                urllib.request.urlopen(base + "/", timeout=2)
        finally:
            if t.is_alive():
                t.join(timeout=2)


class HeartMultiplierSlotTest(unittest.TestCase):
    """heart.py 心率倍率 QWebChannel slot。"""

    def setUp(self):
        self.pushes = []
        heart.cache = mock.MagicMock()
        heart.push_js = lambda s: self.pushes.append(s)
        self.handler = heart.CallHandler()

    def test_save_multiplier(self):
        with mock.patch.object(fastApi, "save_config", return_value=1.25) as mock_save:
            self.handler.saveHeartRateMultiplier("1.25")
        mock_save.assert_called_once_with({"heartRateMultiplier": 1.25})
        heart.cache.set.assert_called_once_with("ratio", 1.25)
        self.assertIn("window.onHeartRateMultiplierSaved('true')", self.pushes)

    def test_save_invalid(self):
        with mock.patch.object(fastApi, "save_config", side_effect=ValueError("非法")):
            self.handler.saveHeartRateMultiplier("3")
        self.assertIn("window.onHeartRateMultiplierSaved('false')", self.pushes)

    def test_load_multiplier(self):
        with mock.patch.object(fastApi, "load_config",
                               return_value={"heartRateMultiplier": 2}):
            self.handler.getHeartRateMultiplier("")
        self.assertIn("window.onHeartRateMultiplierLoaded('2')", self.pushes)

    def test_load_default(self):
        with mock.patch.object(fastApi, "load_config", return_value={}):
            self.handler.getHeartRateMultiplier("")
        self.assertIn("window.onHeartRateMultiplierLoaded('1')", self.pushes)

    def test_load_ignores_bool(self):
        with mock.patch.object(fastApi, "load_config",
                               return_value={"heartRateMultiplier": True}):
            self.handler.getHeartRateMultiplier("")
        self.assertIn("window.onHeartRateMultiplierLoaded('1')", self.pushes)


class StopServerThreadTest(unittest.TestCase):
    """stopServer 后台化：不阻塞调用方、优雅路径不强杀、超时兜底强杀。"""

    class FakeServerThread:
        def __init__(self, hang=False):
            self.hang = hang
            self.joined = False
            self._alive = True

        def is_alive(self):
            return self._alive

        def join(self, timeout=None):
            self.joined = True
            if not self.hang:
                time.sleep(0.1)
                self._alive = False  # 优雅退出完成

    def _wait_pushes(self, pushes, timeout=5):
        deadline = time.time() + timeout
        while not pushes and time.time() < deadline:
            time.sleep(0.05)

    def setUp(self):
        self.pushes = []
        heart.push_js = lambda s: self.pushes.append(s)
        self.handler = heart.CallHandler()
        self._orig_server = heart.server

    def tearDown(self):
        heart.server = self._orig_server

    def test_graceful_stop_background(self):
        fake = self.FakeServerThread()
        heart.server = fake
        with mock.patch.object(fastApi, "stop") as mock_stop, \
                mock.patch.object(heart, "stop_thread") as mock_stop_thread:
            t0 = time.time()
            self.handler.stopServer("")
            elapsed = time.time() - t0
            self.assertLess(elapsed, 0.2, "stopServer 不应阻塞调用方")
            self._wait_pushes(self.pushes)
        mock_stop.assert_called_once_with()
        self.assertTrue(fake.joined)
        mock_stop_thread.assert_not_called()  # 优雅路径不强杀
        self.assertIn("window.stopServer('true')", self.pushes)

    def test_hang_forced_stop(self):
        fake = self.FakeServerThread(hang=True)
        heart.server = fake
        with mock.patch.object(fastApi, "stop"), \
                mock.patch.object(heart, "stop_thread") as mock_stop_thread:
            self.handler.stopServer("")
            self._wait_pushes(self.pushes)
        mock_stop_thread.assert_called_once_with(fake)  # 超时兜底强杀
        self.assertIn("window.stopServer('true')", self.pushes)

    def test_stop_without_server(self):
        heart.server = None
        self.handler.stopServer("")
        self._wait_pushes(self.pushes)
        self.assertIn("window.stopServer('true')", self.pushes)


if __name__ == "__main__":
    unittest.main()
