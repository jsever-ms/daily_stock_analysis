# -*- coding: utf-8 -*-
"""运行时代码版本信息。

用于在日志与 /status 输出中标识当前进程实际加载的代码版本，
排查“代码已更新但进程未重启 / 部署了旧镜像”导致的版本漂移问题。
"""

import logging
import os
import subprocess
import time as _time
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_REVISION_CACHE: Optional[str] = None

# 进程启动时间：模块首次被导入时记录，跨模块统一作为"进程启动时间"
_PROCESS_START_MONOTONIC = _time.time()


def get_process_startup_time() -> str:
    """返回当前进程的启动时间（本地时区，``YYYY-MM-DD HH:MM:SS``）。"""
    try:
        return datetime.fromtimestamp(_PROCESS_START_MONOTONIC).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:  # pragma: no cover - 防御性兜底
        return "unknown"


def get_deployment_env() -> str:
    """返回当前部署环境标识（Railway 环境变量可得时），否则 ``unknown``。

    只读取环境变量名，不含任何 Secret。优先级：
    ``RAILWAY_ENVIRONMENT_NAME`` > ``RAILWAY_DEPLOYMENT_ID`` > ``RAILWAY_SERVICE_NAME``。
    """
    for var in ("RAILWAY_ENVIRONMENT_NAME", "RAILWAY_DEPLOYMENT_ID", "RAILWAY_SERVICE_NAME"):
        value = (os.getenv(var) or "").strip()
        if value:
            return value
    return "unknown"


def get_runtime_revision() -> str:
    """返回当前进程代码的 git revision（短 hash）。

    解析顺序：
    1. 环境变量 ``DSA_GIT_COMMIT``（CI / Docker 构建时注入）
    2. 环境变量 ``RAILWAY_GIT_COMMIT_SHA``（Railway 自动注入的部署 commit）
    3. ``git rev-parse --short HEAD``（源码目录运行）
    4. ``unknown``（无法确定，如精简容器内无 .git）
    """
    global _REVISION_CACHE
    if _REVISION_CACHE is not None:
        return _REVISION_CACHE

    env_revision = (os.getenv("DSA_GIT_COMMIT") or "").strip()
    if env_revision:
        _REVISION_CACHE = env_revision
        return _REVISION_CACHE

    # Railway 自动注入的部署 commit SHA（40 位完整 hash）
    railway_revision = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "").strip()
    if railway_revision:
        _REVISION_CACHE = railway_revision[:7]
        return _REVISION_CACHE

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            _REVISION_CACHE = result.stdout.strip()
            return _REVISION_CACHE
    except Exception as exc:
        logger.debug("[RuntimeInfo] 读取 git revision 失败: %s", exc)

    _REVISION_CACHE = "unknown"
    return _REVISION_CACHE
