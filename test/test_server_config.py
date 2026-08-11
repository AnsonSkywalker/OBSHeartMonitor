# -*- coding: utf-8 -*-
"""OBS 服务配置与优雅关闭的离线测试（无需真实蓝牙/显示器）。

覆盖：local_config.json 白名单读写（原子写）、/config 端点、uvicorn 优雅停止、
heart.py 心率倍率 slot 与 stopServer 后台化。

运行: py -m unittest test.test_server_config -v
"""
import asyncio
import json
import os
import shutil
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

import websockets
from diskcache import Cache

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fastApi  # noqa: E402
import heart  # noqa: E402
import util.MyWebsocketServer as ws_mod  # noqa: E402


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
        for bad in (3, 0, -1, "1.25", None, True,
                    float("nan"), float("inf"), float("-inf")):
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

    def test_stop_target_precision(self):
        """stop(target) 应只置位目标实例，不触碰全局 webServer（停止期间重启不误停）。"""
        fastApi.__dict__.pop("webServer", None)
        old = fastApi.prepare(8766)  # 旧实例：停止流程的目标
        new = fastApi.prepare(8767)  # 新实例：用户停止期间重启的服务
        self.assertIs(fastApi.stop(old), True)
        self.assertTrue(old.should_exit, "目标实例应被置位 should_exit")
        self.assertFalse(new.should_exit, "新实例不应被误停")
        # 无 target 时仍作用于全局 webServer（当前为新实例）
        self.assertIs(fastApi.stop(), True)
        self.assertTrue(new.should_exit)


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
            # 带跨源 Origin 的请求应被拒绝（模拟本机恶意网页绕过 CORS 预检）
            req = urllib.request.Request(
                base + "/config",
                data=json.dumps({"heartRateMultiplier": 1.25}).encode("utf-8"),
                headers={"Content-Type": "text/plain", "Origin": "http://evil.example"},
                method="POST")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 403)
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
        def __init__(self, hang=False, uvicorn_server=None):
            self.hang = hang
            self.joined = False
            self._alive = True
            self.uvicorn_server = uvicorn_server

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
        mock_stop.assert_called_once_with(None)  # 线程未保存 uvicorn 引用时用全局
        self.assertTrue(fake.joined)
        mock_stop_thread.assert_not_called()  # 优雅路径不强杀
        self.assertIn("window.stopServer('true')", self.pushes)

    def test_graceful_stop_target_instance(self):
        """线程保存了 uvicorn 实例引用时，应精确停止该实例而非全局。"""
        fake_server = mock.MagicMock()
        fake = self.FakeServerThread(uvicorn_server=fake_server)
        heart.server = fake
        with mock.patch.object(fastApi, "stop") as mock_stop:
            self.handler.stopServer("")
            self._wait_pushes(self.pushes)
        mock_stop.assert_called_once_with(fake_server)
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


class WebsocketPushTest(unittest.TestCase):
    """WebSocket 推送集成：真实连接验证推送包含 heartRateMultiplier 字段。"""

    def _run_server(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        start_server = websockets.serve(self._handle, "127.0.0.1", self.port)
        self.server_ref["server"] = loop.run_until_complete(start_server)
        self.server_ref["loop"] = loop
        loop.run_forever()

    async def _handle(self, websocket):
        server = ws_mod.MyWebsocketServer("127.0.0.1", self.port)
        await server.echo(websocket)

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache = Cache(os.path.join(self.tmpdir, "cache"))
        self.cache.set("value", 80)
        self.cache.set("maxValue", 120)
        self.cache.set("minValue", 60)
        self.cache.set("ratio", 1.25)
        self._orig_cache = ws_mod.cache
        ws_mod.cache = self.cache
        self.port = free_port()
        self.server_ref = {}
        self.thread = threading.Thread(target=self._run_server, daemon=True)
        self.thread.start()
        # 等待端口就绪
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise AssertionError("WebSocket 服务未就绪")

    def tearDown(self):
        loop = self.server_ref.get("loop")
        server = self.server_ref.get("server")
        if loop is not None:
            async def _shutdown():
                if server is not None:
                    server.close()
                    await server.wait_closed()
                loop.stop()

            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_shutdown()))
            self.thread.join(timeout=5)
        ws_mod.cache = self._orig_cache
        self.cache.close()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_push_contains_multiplier(self):
        """推送的 JSON 应包含 value/maxValue/minValue/heartRateMultiplier。"""
        async def client():
            async with websockets.connect(f"ws://127.0.0.1:{self.port}") as ws:
                await ws.send("heart-overlay")
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                return json.loads(raw)

        info = asyncio.run(client())
        self.assertEqual(info["value"], 80)
        self.assertEqual(info["maxValue"], 120)
        self.assertEqual(info["minValue"], 60)
        self.assertEqual(info["heartRateMultiplier"], 1.25)

    def test_push_default_multiplier(self):
        """cache 无倍率时推送默认 1（与叠加层默认值一致）。"""
        self.cache.delete("ratio")

        async def client():
            async with websockets.connect(f"ws://127.0.0.1:{self.port}") as ws:
                await ws.send("heart-overlay")
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                return json.loads(raw)

        info = asyncio.run(client())
        self.assertEqual(info["heartRateMultiplier"], 1)


if __name__ == "__main__":
    unittest.main()
