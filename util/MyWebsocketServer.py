import asyncio
import json
import logging
import os
import threading
import time

import websockets
from diskcache import Cache
from loguru import logger

global myWebsocket

cache = Cache('/cache')


class MyWebsocketServer:
    def __init__(self, host, port):
        self.host = host
        self.port = port

    async def echo(self, websocket):
        logger.info("------ Echoing WebSocket --------")
        try:
            async for message in websocket:
                logger.info(f'------ WebSockert server recive messages: {message}')
                # await websocket.send("Received: " + message)

                while True:
                    value = cache.get("value")
                    maxValue = cache.get('maxValue')
                    minValue = cache.get('minValue')

                    heartInfo = {"value": value, "maxValue": maxValue, "minValue": minValue}
                    # print(heartInfo)
                    await websocket.send(json.dumps(heartInfo))
                    await asyncio.sleep(0.5)
        except websockets.exceptions.ConnectionClosed:
            # 客户端正常断开（如 OBS 刷新浏览器源/页面关闭）：
            # websockets 会把"向已关闭连接发送"抛为 ConnectionClosed 并用 ERROR 记录，
            # 这里捕获后静默退出，避免污染日志，也不影响服务端继续接受新连接。
            logger.info("------ WebSocket client disconnected --------")

    def connect(self):
        logger.info("======== websocket server is starting up ... ========")
        asyncio.set_event_loop(asyncio.new_event_loop())
        start_server = websockets.serve(self.echo, self.host, self.port)
        asyncio.get_event_loop().run_until_complete(start_server)
        asyncio.get_event_loop().run_forever()
        logger.info("连接成功！")

    # 另开启一个线程,用于启动websocket服务。
    def run(self):
        t = threading.Thread(target=self.connect)
        t.start()

    def stopServer(self):
        os._exit(0)
