import ctypes
import inspect


import psutil
from GPUtil import GPUtil
from loguru import logger
import sys
import os
import threading
import urllib.request
import warnings

from PyQt5 import QtGui
from diskcache import Cache
from PyQt5.QtWidgets import QApplication, QMessageBox
from PyQt5.QtCore import QObject, pyqtSlot, pyqtSignal, QUrl, Qt
from PyQt5.QtWebChannel import QWebChannel
from PyQt5.QtWebEngineWidgets import QWebEngineView

import asyncio
import time
import wmi

from bleak import BleakScanner, BleakClient, BleakGATTCharacteristic

import fastApi
from util.MyWebsocketServer import MyWebsocketServer

from util.ConfigUtil import *

warnings.filterwarnings("ignore", category=DeprecationWarning)

logger.add("application.log", rotation="50MB", encoding="utf-8", enqueue=True, level="DEBUG")
# 设备的Characteristic UUID
par_notification_characteristic = "00002a37-0000-1000-8000-00805f9b34fb"
# par_notification_characteristic = "ebe0ccc1-7a0a-4b0c-8a1a-6ff2997da3a6"
# 电量特征，用于连接保活（周期读取以维持链路活跃）
battery_characteristic = "00002a19-0000-1000-8000-00805f9b34fb"
# 设备的MAC地址（示例，请替换为实际设备地址）
# device_address = "XX:XX:XX:XX:XX:XX"
device_address = ""

# 蓝牙连接线程（模块级初始化，避免未连接时引用报错）
thread1 = None

# 前端JS执行通道：bluetooth/websocket 等子线程不能直接调用 Qt 的 runJavaScript，
# 统一通过 CallHandler.js_notify 信号排队到 GUI 线程执行
js_handler = None


def push_js(script):
    global js_handler
    if js_handler is not None:
        js_handler.js_notify.emit(script)
    else:
        view.page().runJavaScript(script)


def getSystemInfo():
    import socket, platform
    hostname = socket.gethostname()
    ip = socket.gethostbyname(hostname)
    # print(platform.machine())
    list_info = platform.uname()
    # logger.info(list_info)
    sys_name = list_info[0] + list_info[2]
    cpu_name = list_info[5]
    dic_info = {"hostname": hostname, "ip": ip, "sys_name": sys_name, "version": list_info[3], "cpu_name": cpu_name}
    # 调用js函数，实现回调
    # self.mainFrame.evaluateJavaScript('%s(%s)' % ('onGetInfo', json.dumps(dic_info)))
    memory = psutil.virtual_memory()

    c = wmi.WMI()
    for cpu in c.Win32_Processor():
        logger.info(f'CPU: {cpu.Name}')
    logger.info(f"Total Memory: {memory.total / (1024 ** 3):.2f} GB")
    logger.info(f'Hostname: {hostname}')
    logger.info(f'System: {sys_name}')
    logger.info(f'Version: {list_info[3]}')

    # 获取所有 GPU 信息
    gpus = GPUtil.getGPUs()

    for gpu in gpus:
        logger.info(f"GPU ID: {gpu.id}")
        logger.info(f"GPU Name: {gpu.name}")
        logger.info(f"GPU Load: {gpu.load * 100:.2f}%")
        logger.info(f"GPU Free Memory: {gpu.memoryFree} MB")
        logger.info(f"GPU Used Memory: {gpu.memoryUsed} MB")
        logger.info(f"GPU Total Memory: {gpu.memoryTotal} MB")
        logger.info(f"GPU Temperature: {gpu.temperature} °C")
        logger.info(f"GPU UUID: {gpu.uuid}")

    # return json.dumps(dic_info)


# 搜索蓝牙设备信息 官网不建议用
# 这些方法对于简单的程序来说很方便，但不建议用于 更高级的用例，如长时间运行的程序、GUI 或连接到 多个设备。
async def searchBluetoothDevices():
    devices = await BleakScanner.discover()
    list = [];
    for d in devices:
        name = ''
        if (d.name is not None):
            name = d.name

        bluetooth_info = {"address": d.address, "name": name}
        logger.info(bluetooth_info)
        list.append(bluetooth_info)
    logger.info(list)
    return json.dumps(list)


# 实时获取心跳值
# 调试观测：记录最后心跳时间/累计次数，断连时用于判断链路何时停止上报
last_heartbeat_time = None
heartbeat_count = 0
_heartbeat_stat_time = None
_heartbeat_stat_count = 0


async def notification_handler(characteristic: BleakGATTCharacteristic, data: bytearray):
    global value, last_heartbeat_time, heartbeat_count, _heartbeat_stat_time, _heartbeat_stat_count

    # print("rev data:", data)   # 读取到的数据 rev data: bytearray(b'\x06V')
    # print("rev data:", int.from_bytes(data))

    #  rev data: bytearray(b'\x06\x82')
    # ❤: 130
    # bytearray(b'\x06T') 转换为十进制，我们首先需要理解这个字节串的含义。bytearray 表示一组字节，其中 \x06 和 \x54 是十六进制表示的两个字节。
    #
    #     1.\x06 对应的十进制值是 6。 暂时不知道这个值有啥用
    #     2.\x54 对应的十进制值是 84。  心跳的值， T 的ascii 的十六进制是54
    raw = data.hex()
    marker = raw.find('06')
    if marker == -1 or marker + 2 >= len(raw):
        logger.warning(f"无法解析的心跳数据: {raw}")
        return None
    value = int(raw[marker + 2:], 16)
    cache.set('value', value)
    maxValue = cache.get('maxValue')
    minValue = cache.get('minValue')

    if maxValue == 0 and minValue == 0:
        cache.set('maxValue', value)
        cache.set('minValue', value)

    if value > maxValue:
        cache.set('maxValue', value)
    elif value < minValue:
        cache.set('minValue', value)

    # print('❤:', value)
    now = time.time()
    last_heartbeat_time = now
    heartbeat_count += 1
    # 每分钟输出一次心跳接收速率，观察断连前手环上报节奏是否变化
    if _heartbeat_stat_time is None or now - _heartbeat_stat_time >= 60:
        interval = now - _heartbeat_stat_time if _heartbeat_stat_time else 0
        if interval > 0:
            rate = (heartbeat_count - _heartbeat_stat_count) / interval
            logger.info(f"心跳统计: {rate:.1f} 次/秒（近 {interval:.0f}s，累计 {heartbeat_count} 次）")
        _heartbeat_stat_time = now
        _heartbeat_stat_count = heartbeat_count

    push_js("window.getHeartNum('%s')" % value)
    return value
    # print(data.decode('ascii'))
    # print(data)


class CallHandler(QObject):
    # 跨线程向前端页面推送 JS 代码的信号（子线程 emit，GUI 线程执行）
    js_notify = pyqtSignal(str)

    def __init__(self):
        super(CallHandler, self).__init__()

    # 异步搜索附近的蓝牙设备信息
    @pyqtSlot(str)  # 第一个参数即为回调时携带的参数类型
    def initSearch(self, str_args):
        logger.info(f'------> initSearch......{str_args}')
        global search_thread
        search_thread = mySearchThread(11, "search-Thread", 0);
        search_thread.start()

        # msg = asyncio.run(searchBluetoothDevices())
        # view.page().runJavaScript("alert('%s')" % msg)
        # view.page().runJavaScript("window.initSearch('%s')" % msg)
        # info = '蓝牙设备连接成功！'
        # view.page().runJavaScript("window.get_info('%s')" % info)
        # return 'hello, Python'



    # 接受前端传过来选择的蓝牙设备id进行连接
    @pyqtSlot(str, result=str)
    def connectBluetooth(self, str_args):
        logger.info(f'bluetooth {str_args} connecting......')
        words = str_args.split("#")
        uuid = "";
        if words[1] == "":
            uuid = par_notification_characteristic;
        else:
            uuid = words[1];
        device_address = words[0];
        logger.info('thread %s is running...' % threading.current_thread().name)
        global thread1
        # 若旧连接线程仍在运行（如正在重连中），先通知其停止，避免产生孤儿重连线程
        if thread1 is not None and thread1.is_alive():
            logger.info("已有蓝牙线程在运行，先停止旧线程")
            thread1.stop_event.set()
            thread1.join(timeout=2)  # 正常路径 0.2s 内退出；缩短新旧线程并发窗口
        logger.info(f'device_address is {device_address}, uuid is {uuid}')
        thread1 = myThread(1, "Thread-1", 0, device_address, uuid);
        try:
            thread1.start()
        except Exception as e:
            logger.error(f"An error occurred: {e}")
            info = 'false'
            view.page().runJavaScript("window.getConnectInfo('%s')" % info)
        else:
            logger.info('----connectBluetooth---')
        # asyncio.run(self.startConnect(device_address))

    @pyqtSlot(str)
    def disconnectBluetooth(self, str_args):
        global thread1
        # 断开流程放到后台线程执行，避免主线程阻塞导致前端无响应；
        # stop_event 置位后蓝牙线程会在轮询间隔内自行退出并断开连接。
        def _do_stop():
            t = thread1  # 快照：断开期间用户可能重新连接，避免误停新线程
            if t is None:
                logger.warning("未连接蓝牙设备，无需断开")
                push_js("window.stopConnect('true')")
                return
            try:
                t.stop_event.set()
                t.join(timeout=10)  # 线程可能正处于扫描/连接中，最多等其自然退出
                if t.is_alive():
                    logger.warning("蓝牙线程未及时退出，先请求断开再强制停止")
                    if t.loop is not None and t.client is not None:
                        try:
                            fut = asyncio.run_coroutine_threadsafe(t.client.disconnect(), t.loop)
                            fut.result(timeout=2)  # 给 winrt 后台 2s 完成断开，减少句柄残留
                        except Exception:
                            pass
                    stop_thread(t)
            except (SystemExit, Exception) as e:
                logger.error(f"An error occurred: {e}")
            push_js("window.stopConnect('true')")
        threading.Thread(target=_do_stop, daemon=True, name="bt-stop").start()

    @pyqtSlot(result=int)
    def getHeartNum(self):
        logger.info(f"getHeartNum: {value}")
        view.page().runJavaScript("window.getHeartNum('%s')" % value)
        return value

    @pyqtSlot(str)
    def startServer(self, port):
        logger.info(f'----- startServer ----- port: {port}---')
        global server
        server = myServer(2, "Server-1", 0, port);
        try:
            server.start()
        except Exception as e:
            logger.error(f"An error occurred: {e}")
            info = 'false'
            logger.info(info)
            view.page().runJavaScript("window.startServer('%s')" % info)
        else:
            info = 'true'
            logger.info(info)
            # server.terminate()
            view.page().runJavaScript("window.startServer('%s')" % info)

    @pyqtSlot(str)
    def stopServer(self, port):
        logger.info('----- stopServer -----')
        # myapp.exit()
        try:
            stop_thread(server)
            # server.terminate()
        except (SystemExit, Exception) as e:
            logger.error(f"An error occurred: {e}")
        # server.terminate()
        info = 'true'
        view.page().runJavaScript("window.stopServer('%s')" % info)

    # 调用js代码，将搜索到的蓝牙信息返回给前端
    @pyqtSlot(str)
    def getBlueInfo(self, str_args):
        list = []
        data = cache.get("dict")
        if data is not None:
            stop_thread(search_thread)
            for key, value in data.items():
                bluetooth_info = {"address": key, "name": value}
                list.append(bluetooth_info)
            logger.info(f"list: {list}")
            # \u0027 转义单引号：设备名来自 BLE 广播，防止恶意名称闭合 JS 字符串注入
            view.page().runJavaScript("window.initSearch('%s')" % json.dumps(list).replace("'", "\\u0027"))

    @pyqtSlot(str)
    def onSubmitConfig(self, data):
        logger.info(f'----- onSubmitConfig {data}-----')
        try:
            modify_config_file(data)
        except Exception as e:
            logger.error(f"onSubmitConfig An error occurred: {e}")
            info = 'false'
            view.page().runJavaScript("window.onSubmitConfig('%s')" % info)
        else:
            info = 'true'
            view.page().runJavaScript("window.onSubmitConfig('%s')" % info)

    @pyqtSlot(str)
    def onBackConfig(self):
        logger.info('----- onBackConfig-----')
        try:
            default_config()
        except Exception as e:
            logger.error(f"onBackConfig An error occurred: {e}")
            info = 'false'
            view.page().runJavaScript("window.onSubmitConfig('%s')" % info)
        else:
            info = 'true'
            view.page().runJavaScript("window.onSubmitConfig('%s')" % info)


class WebEngine(QWebEngineView):
    def __init__(self):
        super(WebEngine, self).__init__()
        self.setContextMenuPolicy(Qt.NoContextMenu)  # 设置右键菜单规则为自定义右键菜单
        # self.customContextMenuRequested.connect(self.showRightMenu)  # 这里加载并显示自定义右键菜单，我们重点不在这里略去了详细带吗
        self.setWindowTitle('心率记录器')
        self.resize(1200, 800)
        # cp = QDesktopWidget().availableGeometry().center()
        # self.move(QPoint(cp.x() - self.width() / 2, cp.y() - self.height() / 2))

    def closeEvent(self, event):
        reply = QMessageBox.question(self, '确认', '您确定要关闭窗口吗？',
                                     QMessageBox.Yes | QMessageBox.No,
                                     QMessageBox.No)

        if reply == QMessageBox.Yes:
            logger.warning("====== closeApp ======")
            webSocketServer.stopServer()
            event.accept()
        else:
            event.ignore()


# 开启另一个线程去搜索蓝牙设备 不阻塞主线程
class mySearchThread(threading.Thread):
    def __init__(self, threadID, name, delay):
        threading.Thread.__init__(self)
        self.threadID = threadID
        self.name = name
        self.delay = delay

    def run(self):
        logger.info('thread %s is running...' % threading.current_thread().name)
        # self.result = asyncio.run(self.searchBluetoothDevices())
        asyncio.run(self.getOtherDeviceInfo())
        # print("msg：", self.result)

        # runJavaScript 不能在子线程中运行，否则程序会直接退出，也没有报错信息
        # view.page().runJavaScript("window.initSearch('%s')" % msg)

    # BleakScanner 官网给的例子 https://bleak.readthedocs.io/en/latest/api/scanner.html#bleak.BleakScanner
    async def getOtherDeviceInfo(self):
        stop_event = asyncio.Event()
        dict = {}
        cache.delete("list")

        # add something that calls stop_event.set()
        # result = cache.get("list")
        # while result is not None:
        #     stop_event.set()
        def callback(device, advertising_data):
            # do something with incoming data
            # print('device', device)

            name = ''
            if (device.name is not None):
                name = device.name

            dict.update({device.address: name})

            logger.info('dict----> {dict}', dict=dict)
            cache.set('dict', dict)

        async with BleakScanner(callback) as scanner:
            # list = scanner.discovered_devices
            # print(list)
            # Important! Wait for an event to trigger stop, otherwise scanner
            # will stop immediately.
            await stop_event.wait()


# 开启另一个线程去连接蓝牙 实时获取心跳值
class myThread(threading.Thread):
    d_address = ""
    max_reconnect_delay = 30      # 自动重连的退避间隔上限（秒）
    connect_timeout = 15          # 建立 BLE 连接的超时（秒）
    keepalive_interval = 60       # 保活读取间隔（秒）
    keepalive_read_timeout = 10   # 保活读取超时（秒）

    def __init__(self, threadID, name, delay, bluetoothAdresss, uuid):
        threading.Thread.__init__(self, daemon=True)
        self.threadID = threadID
        self.name = name
        self.delay = delay
        self.d_address = bluetoothAdresss;
        self.uuid = uuid;
        self.stop_event = threading.Event()  # 置位表示用户主动停止，不再重连
        self.loop = None                     # 本线程的 asyncio 事件循环
        self.client = None                   # 当前 BleakClient 实例
        self.ever_connected = False          # 本次会话是否成功连接过（区分首次连接/自动重连）

    def run(self):
        logger.info('thread %s is running...' % threading.current_thread().name)
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self.startConnect(self.d_address, self.uuid))
        finally:
            self.loop.close()
            self.loop = None

    async def startConnect(self, device_address, uuid):
        reconnect_delay = 1  # 断连后的首次重连等待（秒），之后按 2、4、8... 退避，封顶 30s
        attempt = 0  # 本次会话累计的连接尝试次数（含首次）
        while not self.stop_event.is_set():
            attempt += 1
            logger.info(f"===== 连接尝试 #{attempt}（device={device_address}, uuid={uuid}）=====")
            conn_seconds = await self.connectOnce(device_address, uuid)
            if self.stop_event.is_set():
                break
            if conn_seconds is not None and conn_seconds >= 10:
                # 正常使用中掉线：重置退避，尽快重连
                reconnect_delay = 1
                logger.warning(f"蓝牙连接已断开，{reconnect_delay}s 后尝试自动重连...")
            elif conn_seconds is not None:
                # 刚连上就掉：视为不稳定，按退避递增重试，避免反复轰炸
                logger.warning(f"连接仅维持 {conn_seconds:.0f}s 即断开，{reconnect_delay}s 后重试...")
            else:
                logger.warning(f"蓝牙连接失败，{reconnect_delay}s 后重试...")
            # 等待退避间隔；用户主动停止时会立刻唤醒退出
            await self._wait_stop(timeout=reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, self.max_reconnect_delay)
        logger.info("蓝牙连接线程退出（用户停止或会话结束）")

    async def _wait_stop(self, timeout=None):
        """等待用户停止事件；timeout 为 None 时无限等待，否则最多等 timeout 秒"""
        waited = 0.0
        while not self.stop_event.is_set():
            if timeout is not None and waited >= timeout:
                return False
            await asyncio.sleep(0.2)
            waited += 0.2
        return True

    async def connectOnce(self, device_address, uuid):
        logger.info(f'bluetooth device {device_address} uuid is {uuid} start connecting...')
        # 基于MAC地址查找设备（记录扫描耗时与信号强度，重连慢时可对照）
        if self.stop_event.is_set():
            logger.info("用户已停止，跳过本次扫描")
            return None
        scan_start = time.time()
        try:
            device = await BleakScanner.find_device_by_address(
                device_address, timeout=8, cb=dict(use_bdaddr=False)  # use_bdaddr判断是否是MOC系统
            )
        except Exception as e:
            logger.error(f"扫描设备异常: {type(e).__name__}: {e}")
            return None
        scan_cost = time.time() - scan_start
        if device is None:
            logger.error(f"could not find device with address '{device_address}'（扫描耗时 {scan_cost:.1f}s）")
            if not self.ever_connected:
                push_js("window.getConnectInfo('false')")
            return None
        logger.info(f"已扫描到设备: name={getattr(device, 'name', '?')}, rssi={getattr(device, 'rssi', '?')}, 扫描耗时 {scan_cost:.1f}s")

        # 事件定义
        disconnected_event = asyncio.Event()
        conn_started = None
        reconnecting_notified = False  # 同一连接只向前端推送一次 reconnecting

        # 断开连接事件回调，当设备断开连接时，会触发该函数，存在一定延迟
        def disconnected_callback(client):
            nonlocal conn_started, reconnecting_notified
            if self.stop_event.is_set():
                logger.info("用户主动停止，断开连接")
                disconnected_event.set()
                return
            elapsed = (time.time() - conn_started) if conn_started else 0
            hb_ago = (time.time() - last_heartbeat_time) if last_heartbeat_time else None
            hb_ago_txt = f"{hb_ago:.0f}s" if hb_ago is not None else "从未收到"
            logger.warning(f"Disconnected callback called! 连接时长: {elapsed:.0f}s, 距最后心跳: {hb_ago_txt}, "
                           f"累计心跳: {heartbeat_count} 次, 进入自动重连...")
            if not reconnecting_notified:
                reconnecting_notified = True
                push_js("window.getConnectInfo('reconnecting')")
            disconnected_event.set()

        logger.info("connecting to device...")
        if self.stop_event.is_set():
            # 扫描期间用户可能已停止，不再发起连接
            logger.info("用户已停止，跳过连接")
            return None
        connect_start = time.time()
        try:
            # 重连时使用 Windows 缓存的服务列表，跳过重新发现以加快重连、减少失败点
            client = BleakClient(device, disconnected_callback=disconnected_callback,
                                 winrt=dict(use_cached_services=self.ever_connected))
        except Exception as e:
            logger.error(f"创建 BleakClient 失败: {type(e).__name__}: {e}")
            return None
        self.client = client  # 立即赋值：连接建立前被停止流程引用（用于强杀前请求断开）
        try:
            try:
                await asyncio.wait_for(client.connect(), timeout=self.connect_timeout)
            except asyncio.TimeoutError:
                logger.error(f"连接超时（{self.connect_timeout}s 无响应）")
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return None
            except Exception as e:
                logger.error(f"连接失败: {type(e).__name__}: {e}")
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return None
            # 连接已建立：手动管理生命周期（bleak 的 __aenter__ 会无条件再次 connect，避免双重连接）
            conn_started = time.time()
            logger.info(f"Connected（建立连接耗时 {conn_started - connect_start:.1f}s）")
            try:
                if self.stop_event.is_set():
                    # 连接建立期间用户已停止/发起了新连接，立即断开退出
                    logger.info("连接建立后用户已停止，立即断开退出")
                    return 0
                # 枚举服务，并顺带确认是否支持电量特征（用于保活）
                battery_uuid = None
                for service in client.services.services.values():
                    t_uuid = service.uuid
                    characteristics = service.characteristics
                    charList = []
                    for characteristic in characteristics:
                        tempUuid = characteristic.uuid
                        tempDesc = characteristic.description
                        tempServiceUuid = characteristic.service_uuid
                        charList.append({'tempUuid': tempUuid, 'tempDesc': tempDesc, 'tempServiceUuid': tempServiceUuid})
                        if tempUuid == battery_characteristic:
                            battery_uuid = tempUuid
                    description = service.description
                    logger.info('----> description: %s, uuid: %s, characteristics: %s' % (description, t_uuid, charList))

                notify_start = time.time()
                await client.start_notify(uuid, notification_handler)
                logger.info(f"start_notify 完成（耗时 {time.time() - notify_start:.1f}s）")
                if self.stop_event.is_set():
                    # 订阅期间用户已停止/发起了新连接，不推送成功通知，直接断开退出
                    logger.info("订阅完成但用户已停止，立即断开退出")
                    return 0
                # 通知前端：首次连接成功 / 自动重连成功
                if self.ever_connected:
                    push_js("window.getConnectInfo('reconnected')")
                else:
                    self.ever_connected = True
                    push_js("window.getConnectInfo('true')")

                # 保活：周期读取电量特征维持链路活跃，读失败则主动断开触发重连
                keepalive_task = None
                if battery_uuid is not None:
                    keepalive_task = asyncio.ensure_future(
                        self.keepalive(client, battery_uuid,
                                       interval=self.keepalive_interval,
                                       read_timeout=self.keepalive_read_timeout))

                # 等待：设备断开 / 用户主动停止（监听直到断开为止，有延迟）
                stop_waiter = asyncio.ensure_future(self._wait_stop())
                done, pending = await asyncio.wait(
                    {asyncio.ensure_future(disconnected_event.wait()), stop_waiter},
                    return_when=asyncio.FIRST_COMPLETED
                )
                canceled = list(pending)
                if keepalive_task is not None:
                    canceled.append(keepalive_task)
                for task in canceled:
                    task.cancel()
                if canceled:
                    await asyncio.gather(*canceled, return_exceptions=True)
                return time.time() - conn_started  # 返回本次连接时长（秒）
            finally:
                try:
                    await asyncio.wait_for(client.disconnect(), timeout=5)
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"连接过程异常: {type(e).__name__}: {e}")
            return None

    async def keepalive(self, client, battery_uuid, interval=60, read_timeout=10):
        """周期读取电量特征保活；读取超时/失败说明链路已失效，主动断开以触发自动重连"""
        while not self.stop_event.is_set():
            await asyncio.sleep(interval)
            try:
                data = await asyncio.wait_for(client.read_gatt_char(battery_uuid), timeout=read_timeout)
                battery = data[0] if data else -1
                logger.info(f"保活: 读取电量成功 = {battery}%")
            except asyncio.TimeoutError:
                logger.warning("保活读取超时，链路可能已失效，主动断开以触发重连")
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return
            except Exception as e:
                logger.warning(f"保活读取失败: {e}")
                try:
                    await client.disconnect()
                except Exception:
                    pass
                return


# https://blog.csdn.net/hp_cpp/article/details/83040162 强行停止python子线程最佳方案
def _async_raise(tid, exctype):
    """raises the exception, performs cleanup if needed"""
    tid = ctypes.c_long(tid)
    if not inspect.isclass(exctype):
        exctype = type(exctype)
    res = ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, ctypes.py_object(exctype))
    if res == 0:
        raise ValueError("invalid thread id")
    elif res != 1:
        # """if it returns a number greater than one, you're in trouble,
        # and you should call it again with exc=NULL to revert the effect"""
        ctypes.pythonapi.PyThreadState_SetAsyncExc(tid, None)
        raise SystemError("PyThreadState_SetAsyncExc failed")


def stop_thread(thread):
    _async_raise(thread.ident, SystemExit)


# 开启另一个线程去启动web服务
class myServer(threading.Thread):
    def __init__(self, threadID, name, delay, port):
        logger.info(f"Starting myServer port:{port}")
        threading.Thread.__init__(self)
        self.threadID = threadID
        self.name = name
        self.delay = delay
        self.port = port

    def run(self):
        # myapp.main(self.port)
        fastApi.start(int(self.port))

    def terminate(self):
        pass


class myWebSocketServer(threading.Thread):
    def __init__(self, threadID, name, delay):
        threading.Thread.__init__(self)
        self.threadID = threadID
        self.name = name
        self.delay = delay

    def run(self):
        # 启动websocket服务端
        MyWebsocketServer.main()


if __name__ == '__main__':
    cache = Cache('/cache')
    s = """                                                                                         
                                                                          
            |||      |||    ||||||||       ||||       ||||||||    |||||||||||||          
            |||      |||    ||||||||      ||||||      |||||||||   |||||||||||||          
            |||      |||    ||||||||      ||||||      ||||||||||  |||||||||||||          
            |||      |||    |||           ||||||      |||   ||||       |||               
            |||      |||    |||          ||||||||     |||    |||       |||               
            |||      |||    |||          |||  |||     |||    |||       |||               
            ||||||||||||    |||||||      |||  |||     |||   ||||       |||               
            ||||||||||||    |||||||     ||||  ||||    |||||||||        |||               
            ||||||||||||    |||||||     |||    |||    ||||||||         |||               
            |||      |||    |||         ||||||||||    ||||||||         |||               
            |||      |||    |||        ||||||||||||   |||  ||||        |||               
            |||      |||    |||        ||||||||||||   |||   ||||       |||               
            |||      |||    ||||||||   |||      |||   |||    |||       |||               
            |||      |||    ||||||||  ||||      ||||  |||    ||||      |||               
            |||      |||    ||||||||  |||       ||||  |||     ||||     |||               
            https://space.bilibili.com/31060761 @六道轮回lk                                                                  
                                                                                      
    """
    logger.info(s)
    # 实例化缓存对象，指定缓存目录
    cache = Cache('/cache')
    cache.set('value', 0)
    cache.set('maxValue', 0)
    cache.set('minValue', 0)
    getSystemInfo()

    webSocketServer = MyWebsocketServer("localhost", 8000)
    # webSocketServer = MyWebsocketServer("0.0.0.0", 8000)
    webSocketServer.run()
    # 加载程序主窗口
    app = QApplication(sys.argv)
    view = WebEngine()

    icon = QtGui.QIcon()
    icon.addPixmap(QtGui.QPixmap("static/heart.ico"), QtGui.QIcon.Normal, QtGui.QIcon.Off)
    app.setWindowIcon(icon)

    channel = QWebChannel()
    handler = CallHandler()  # 实例化QWebChannel的前端处理对象
    # 子线程推送的 JS 通过信号在 GUI 线程执行（跨线程调用 runJavaScript 不安全）
    handler.js_notify.connect(lambda script: view.page().runJavaScript(script))
    js_handler = handler
    channel.registerObject('PyHandler', handler)  # 将前端处理对象在前端页面中注册为名PyHandler对象，此对象在前端访问时名称即为PyHandler'
    view.page().setWebChannel(channel)  # 挂载前端处理对象
    url_string = urllib.request.pathname2url(os.path.join(os.getcwd(), "./web/index.html"))  # 加载本地html文件
    view.load(QUrl(url_string))
    view.show()
    sys.exit(app.exec_())
