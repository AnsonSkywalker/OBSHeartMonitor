import json
import logging
import os
import sys
from types import FrameType
from typing import cast

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger

app = FastAPI()

# 资源目录固定定位：开发模式用本文件目录；cx_Freeze 打包后 fastApi 模块在 zip 内，
# 用 exe 所在目录（setup.py 将 static/templates 复制到 exe 同级），从任意目录启动均生效
if getattr(sys, "frozen", False):
    _BASE_DIR = os.path.dirname(sys.executable)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_STATIC_DIR = os.path.join(_BASE_DIR, "static")
_TEMPLATES_DIR = os.path.join(_BASE_DIR, "templates")
_CONFIG_PATH = os.path.join(_BASE_DIR, "local_config.json")

app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

templates = Jinja2Templates(directory=_TEMPLATES_DIR)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    # return {"message": "Hello World"}
    return templates.TemplateResponse(
        request=request, name="index.html"
    )


@app.get("/config", response_class=JSONResponse)
async def get_config():
    """返回本地私有配置中的最大心率（白名单键）。
    local_config.json 位于仓库根目录且已被 .gitignore 忽略，不会随仓库提交。
    示例内容: {"maxHeartRate": 196}
    """
    max_heart_rate = None
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            # 白名单：只取 maxHeartRate，避免未来加入的其他本地字段经 HTTP 暴露
            mhr = data.get("maxHeartRate") if isinstance(data, dict) else None
            if isinstance(mhr, (int, float)) and not isinstance(mhr, bool) and mhr > 0:
                max_heart_rate = int(mhr)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            logger.warning("local_config.json 读取失败，使用默认配置")
    return {"maxHeartRate": max_heart_rate}


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        # Get corresponding Loguru level if it exists
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = str(record.levelno)

        # Find caller from where originated the logged message
        frame, depth = logging.currentframe(), 2
        while frame.f_code.co_filename == logging.__file__:  # noqa: WPS609
            frame = cast(FrameType, frame.f_back)
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage(),
        )





def start(port):
    global config
    config= uvicorn.Config("fastApi:app", host='127.0.0.1', port=port, reload=False)
    # config = uvicorn.Config("fastApi:app", host='0.0.0.0', port=port, reload=False)
    global webServer
    webServer = uvicorn.Server(config)

    # 将uvicorn输出的全部让loguru管理
    LOGGER_NAMES = ("uvicorn.asgi", "uvicorn.access", "uvicorn")

    # change handler for default uvicorn logger
    logging.getLogger().handlers = [InterceptHandler()]
    for logger_name in LOGGER_NAMES:
        logging_logger = logging.getLogger(logger_name)
        logging_logger.handlers = [InterceptHandler()]

    webServer.run()
    # uvicorn.run(app="fastApi:app", host="127.0.0.1", port=port, reload=False)


# if __name__ == '__main__':

    # uvicorn.run(app="main:app", host="127.0.0.1", port=8000, reload=True)
