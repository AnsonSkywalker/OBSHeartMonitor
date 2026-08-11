import asyncio
import json
import logging
import math
import os
import sys
import time
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

# 心率倍率档位白名单（仅影响 OBS 叠加层颜色，不影响显示数值）：
# 1x 原始模式（默认）/ 1.25x 有氧模式 / 2x 静息模式
HEART_RATE_MULTIPLIERS = (1, 1.25, 2)


def load_config():
    """读取 local_config.json，返回 dict；文件缺失/损坏时返回空 dict。"""
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            logger.warning("local_config.json 读取失败，使用默认配置")
    return {}


def save_config(data):
    """白名单写入 local_config.json：仅接受 heartRateMultiplier（倍率档位），
    保留文件中的其他键（如 maxHeartRate）。返回写入的倍率值。"""
    if not isinstance(data, dict):
        raise ValueError("config 必须是 JSON 对象")
    m = data.get("heartRateMultiplier")
    if not (isinstance(m, (int, float)) and not isinstance(m, bool)
            and float(m) in HEART_RATE_MULTIPLIERS):
        raise ValueError(f"heartRateMultiplier 非法: {m!r}（允许档位 {HEART_RATE_MULTIPLIERS}）")
    conf = load_config()
    conf["heartRateMultiplier"] = float(m)
    # 先写临时文件再原子替换，避免写入中断时损坏原配置（丢失 maxHeartRate 等）
    tmp_path = _CONFIG_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=4)
    os.replace(tmp_path, _CONFIG_PATH)
    return float(m)


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    # return {"message": "Hello World"}
    return templates.TemplateResponse(
        request=request, name="index.html"
    )


@app.get("/config", response_class=JSONResponse)
async def get_config():
    """返回本地私有配置：最大心率 + 心率倍率（白名单键）。
    local_config.json 位于仓库根目录且已被 .gitignore 忽略，不会随仓库提交。
    示例内容: {"maxHeartRate": 196, "heartRateMultiplier": 1.25}
    """
    data = load_config()
    # 白名单：只取约定的键，避免未来加入的其他本地字段经 HTTP 暴露
    # math.isfinite 排除 JSON 允许的 Infinity/NaN（int(inf) 会抛 OverflowError）
    max_heart_rate = None
    mhr = data.get("maxHeartRate")
    if (isinstance(mhr, (int, float)) and not isinstance(mhr, bool)
            and math.isfinite(mhr) and mhr > 0):
        max_heart_rate = int(mhr)
    multiplier = 1
    m = data.get("heartRateMultiplier")
    if (isinstance(m, (int, float)) and not isinstance(m, bool)
            and float(m) in HEART_RATE_MULTIPLIERS):
        multiplier = float(m)
    return {"maxHeartRate": max_heart_rate, "heartRateMultiplier": multiplier}


@app.post("/config")
async def update_config(request: Request):
    """同源写入心率倍率（供 OBS 叠加层/调试使用；主界面走 QWebChannel）。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    try:
        value = save_config(data)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "heartRateMultiplier": value}


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        # uvicorn 优雅关闭时会取消 lifespan 协程（asyncio.CancelledError）并以
        # ERROR 记录 traceback（uvicorn 0.32 + starlette 的关闭竞态，Ctrl+C 正常
        # 退出也会出现），静默掉避免用户误以为停止服务出错
        if (record.name == "uvicorn.error" and record.exc_info
                and record.exc_info[0] is asyncio.CancelledError):
            return
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


def stop():
    """优雅停止 uvicorn：置位 should_exit，由 uvicorn 主循环自行关闭监听并
    跑完 lifespan shutdown，而不是向线程注入异常强杀。
    返回是否已发出停止请求。"""
    # 启动线程可能尚未执行到 webServer = uvicorn.Server(...)（点启动后立刻点停止），
    # 短暂等待避免读到未定义的全局变量
    server = globals().get("webServer")
    if server is None:
        for _ in range(10):
            time.sleep(0.1)
            server = globals().get("webServer")
            if server is not None:
                break
    if server is None:
        return False
    # 只置位 should_exit：uvicorn 会走完 lifespan shutdown（发 shutdown 事件并等
    # starlette 确认）再退出，关闭过程干净无 CancelledError 噪音；
    # 若再设 force_exit 会跳过等待并取消 lifespan 任务，反而产生 shutdown.failed traceback
    server.should_exit = True
    return True


# if __name__ == '__main__':

    # uvicorn.run(app="main:app", host="127.0.0.1", port=8000, reload=True)
