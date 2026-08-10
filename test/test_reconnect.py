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

FAKE_MAC = "AA:BB:CC:DD:EE:FF"
HEART_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_SERVICE_UUID = "0000180f-0000-1000-8000-00805f9b34fb"
BATTERY_CHAR_UUID = "00002a19-0000-1000-8000-00805f9b34fb"


class FakeBatteryService:
    uuid = BATTERY_SERVICE_UUID
    description = "Battery Service"
    characteristics = [mock.MagicMock(uuid=BATTERY_CHAR_UUID,
                                      description="Battery Level",
                                      service_uuid=BATTERY_SERVICE_UUID)]


class FakeClient:
    """模拟 BleakClient：第一次连接建立后立即断连，后续连接保持稳定。"""

    total_notify = 0
    hang_connect = False   # True 时 connect() 永不完成，用于测连接超时
    hang_read = False      # True 时 read_gatt_char 永不完成，用于测保活超时
    include_battery = False

    def __init__(self, device, disconnected_callback=None, winrt=None, **kwargs):
        self._cb = disconnected_callback
        self.services = mock.MagicMock()
        self.services.services.values.return_value = (
            [FakeBatteryService()] if FakeClient.include_battery else [])
        self._connected = False

    @property
    def is_connected(self):
        return self._connected

    async def connect(self):
        if FakeClient.hang_connect:
            await asyncio.sleep(100)
        self._connected = True

    async def __aenter__(self):
        if not self._connected:
            await self.connect()
        return self

    async def __aexit__(self, *args):
        self._connected = False

    async def start_notify(self, uuid, handler):
        FakeClient.total_notify += 1
        if FakeClient.total_notify == 1:
            self._cb(self)  # 第一次连接后立即断连，模拟不稳定

    async def disconnect(self):
        self._connected = False
        if self._cb:
            self._cb(self)

    async def read_gatt_char(self, uuid):
        if FakeClient.hang_read:
            await asyncio.sleep(100)
        return bytearray([80])


class ReconnectTest(unittest.TestCase):

    def setUp(self):
        heart.cache = mock.MagicMock()
        heart.cache.get.return_value = 0
        heart.push_js = lambda s: self.pushes.append(s)
        heart.last_heartbeat_time = None
        heart.heartbeat_count = 0
        self.pushes = []
        FakeClient.total_notify = 0
        FakeClient.hang_connect = False
        FakeClient.hang_read = False
        FakeClient.include_battery = False
        self._orig_connect = FakeClient.connect
        heart.BleakClient = FakeClient
        heart.BleakScanner = mock.MagicMock()
        heart.BleakScanner.find_device_by_address = mock.AsyncMock(
            return_value=mock.MagicMock(name="FakeBand", rssi=-45))

    def tearDown(self):
        FakeClient.connect = self._orig_connect

    def _start_thread(self, **kwargs):
        t = heart.myThread(1, "T", 0, FAKE_MAC, HEART_UUID)
        for k, v in kwargs.items():
            setattr(t, k, v)
        t.start()
        return t

    def _wait_until(self, cond, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.1)
        return False

    def test_heartbeat_stats(self):
        """心跳通知应更新计数与最后心跳时间"""
        async def feed(n):
            for _ in range(n):
                await heart.notification_handler(mock.MagicMock(), bytearray(b"\x06\x54"))
        asyncio.run(feed(3))
        self.assertEqual(heart.heartbeat_count, 3)
        self.assertIsNotNone(heart.last_heartbeat_time)

    def test_heartbeat_bad_data(self):
        """不含 0x06 前缀的异常心跳数据不应抛异常"""
        async def feed():
            await heart.notification_handler(mock.MagicMock(), bytearray(b"\x00\x01"))
        asyncio.run(feed())
        self.assertEqual(heart.heartbeat_count, 0)  # 未计入统计

    def test_reconnect_flow(self):
        """断连后应自动重连并保持，用户停止后线程干净退出"""
        t = self._start_thread()
        self.assertTrue(self._wait_until(lambda: FakeClient.total_notify >= 2),
                        "断连后应自动重连")
        time.sleep(0.5)  # 让重连后的连接稳定一会
        self.assertEqual(FakeClient.total_notify, 2)

        t.stop_event.set()
        t.join(timeout=10)
        self.assertFalse(t.is_alive(), "用户停止后线程应干净退出")

        conn_pushes = [p for p in self.pushes if p.startswith("window.getConnectInfo")]
        self.assertEqual(set(conn_pushes),
                         {"window.getConnectInfo('true')",
                          "window.getConnectInfo('reconnecting')",
                          "window.getConnectInfo('reconnected')"})

    def test_connect_timeout_then_reconnect(self):
        """连接挂起超时后应清理并继续重试，最终重连成功"""
        t = self._start_thread(connect_timeout=1)
        # 前两次连接挂起超时，第三次恢复正常
        state = {"hangs": 2}

        orig_connect = FakeClient.connect

        async def flaky_connect(self):
            if state["hangs"] > 0:
                state["hangs"] -= 1
                await asyncio.sleep(100)
            await orig_connect(self)

        FakeClient.connect = flaky_connect
        self.assertTrue(self._wait_until(lambda: FakeClient.total_notify >= 1, timeout=20),
                        "超时后应重试并最终连接成功")
        self.assertEqual(state["hangs"], 0)
        t.stop_event.set()
        t.join(timeout=10)
        self.assertFalse(t.is_alive())

    def test_keepalive_timeout_triggers_reconnect(self):
        """保活读取超时应主动断开并触发重连"""
        FakeClient.include_battery = True
        FakeClient.hang_read = True
        t = self._start_thread(keepalive_interval=1, keepalive_read_timeout=1)
        self.assertTrue(self._wait_until(lambda: FakeClient.total_notify >= 2, timeout=20),
                        "保活超时后应主动断开并重连")
        t.stop_event.set()
        t.join(timeout=10)
        self.assertFalse(t.is_alive())

    def test_stop_during_reconnect_wait(self):
        """重连退避等待期间用户停止，线程应尽快退出"""
        heart.BleakScanner.find_device_by_address = mock.AsyncMock(return_value=None)
        t = self._start_thread()
        time.sleep(1.5)  # 已进入退避等待
        t.stop_event.set()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "退避等待期间停止应快速退出")
        self.assertIn("window.getConnectInfo('false')", self.pushes)

    def test_disconnect_button(self):
        """disconnectBluetooth 应后台执行：前端立即收到 stopConnect 通知，线程退出"""
        t = self._start_thread()
        self.assertTrue(self._wait_until(lambda: FakeClient.total_notify >= 1))
        heart.thread1 = t
        handler = heart.CallHandler()
        handler.disconnectBluetooth("")
        self.assertTrue(self._wait_until(
            lambda: "window.stopConnect('true')" in self.pushes, timeout=15),
            "前端应收到断开通知")
        t.join(timeout=10)
        self.assertFalse(t.is_alive(), "断开后线程应退出")

    def test_disconnect_without_connection(self):
        """未连接蓝牙时点断开：不应崩溃，前端仍收到反馈"""
        heart.thread1 = None
        handler = heart.CallHandler()
        handler.disconnectBluetooth("")
        self.assertTrue(self._wait_until(
            lambda: "window.stopConnect('true')" in self.pushes, timeout=5),
            "未连接时断开也应收到通知")


if __name__ == "__main__":
    unittest.main()
