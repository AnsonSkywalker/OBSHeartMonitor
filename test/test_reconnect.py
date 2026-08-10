# -*- coding: utf-8 -*-
"""蓝牙自动重连逻辑的离线冒烟测试（mock bleak，无需真实蓝牙设备）。

运行: py -m unittest test.test_reconnect -v
"""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import heart  # noqa: E402


class FakeClient:
    """模拟 BleakClient：第一次连接建立后立即断连，后续连接保持稳定。"""

    total_notify = 0

    def __init__(self, device, disconnected_callback=None, winrt=None, **kwargs):
        self._cb = disconnected_callback
        self.services = mock.MagicMock()
        self.services.services.values.return_value = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def start_notify(self, uuid, handler):
        FakeClient.total_notify += 1
        if FakeClient.total_notify == 1:
            self._cb(self)  # 第一次连接后立即断连，模拟不稳定

    async def disconnect(self):
        if self._cb:
            self._cb(self)

    async def read_gatt_char(self, uuid):
        return bytearray([80])


class ReconnectTest(unittest.TestCase):

    def setUp(self):
        heart.cache = mock.MagicMock()
        heart.cache.get.return_value = 0
        heart.push_js = lambda s: self.pushes.append(s)
        heart.last_heartbeat_time = None
        heart.heartbeat_count = 0
        self.pushes = []
        self.events = []
        FakeClient.total_notify = 0
        heart.BleakClient = FakeClient
        heart.BleakScanner = mock.MagicMock()
        heart.BleakScanner.find_device_by_address = mock.AsyncMock(
            return_value=mock.MagicMock(name="FakeBand", rssi=-45))

    def test_heartbeat_stats(self):
        """心跳通知应更新计数与最后心跳时间"""
        async def feed(n):
            for _ in range(n):
                await heart.notification_handler(mock.MagicMock(), bytearray(b"\x06\x54"))
        asyncio.run(feed(3))
        self.assertEqual(heart.heartbeat_count, 3)
        self.assertIsNotNone(heart.last_heartbeat_time)

    def test_reconnect_flow(self):
        """断连后应自动重连并保持，用户停止后线程干净退出"""
        t = heart.myThread(1, "T", 0, "AA:BB:CC:DD:EE:FF", "00002a37-0000-1000-8000-00805f9b34fb")
        t.start()

        deadline = time.time() + 20
        while time.time() < deadline:
            if FakeClient.total_notify >= 2:
                break
            time.sleep(0.1)
        time.sleep(0.5)  # 让重连后的连接稳定一会
        self.assertEqual(FakeClient.total_notify, 2, "断连后应自动重连一次并保持")

        # 模拟用户停止（与 disconnectBluetooth 相同路径）
        t.stop_event.set()
        if t.loop is not None and t.client is not None:
            fut = asyncio.run_coroutine_threadsafe(t.client.disconnect(), t.loop)
            fut.result(timeout=5)
        t.join(timeout=10)
        self.assertFalse(t.is_alive(), "用户停止后线程应干净退出")

        conn_pushes = [p for p in self.pushes if p.startswith("window.getConnectInfo")]
        self.assertEqual(set(conn_pushes),
                         {"window.getConnectInfo('true')",
                          "window.getConnectInfo('reconnecting')",
                          "window.getConnectInfo('reconnected')"})

    def test_stop_during_reconnect_wait(self):
        """重连退避等待期间用户停止，线程应尽快退出"""
        t = heart.myThread(1, "T", 0, "AA:BB:CC:DD:EE:FF", "00002a37-0000-1000-8000-00805f9b34fb")
        # 设备永远找不到 → 反复失败重试
        heart.BleakScanner.find_device_by_address = mock.AsyncMock(return_value=None)
        t.start()
        time.sleep(1.5)  # 已进入退避等待
        t.stop_event.set()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "退避等待期间停止应快速退出")
        # 从未连接成功过，首次失败应通知前端
        self.assertIn("window.getConnectInfo('false')", self.pushes)


if __name__ == "__main__":
    unittest.main()
