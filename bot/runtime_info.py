# -*- coding: utf-8 -*-
"""运行时代码版本信息。

用于在日志与 /status 输出中标识当前进程实际加载的代码版本，
排查“代码已更新但进程未重启 / 部署了旧镜像”导致的版本漂移问题。
"""

import logging
import os
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)

_REVISION_CACHE: Optional[str] = None


def get_runtime_revision() -> str:
    """返回当前进程代码的 git revision（短 hash）。

    解析顺序：
    1. 环境变量 ``DSA_GIT_COMMIT``（CI / Docker 构建时注入）
    2. ``git rev-parse --short HEAD``（源码目录运行）
    3. ``unknown``（无法确定，如精简容器内无 .git）
    """
    global _REVISION_CACHE
    if _REVISION_CACHE is not None:
        return _REVISION_CACHE

    env_revision = (os.getenv("DSA_GIT_COMMIT") or "").strip()
    if env_revision:
        _REVISION_CACHE = env_revision
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
