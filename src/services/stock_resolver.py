# -*- coding: utf-8 -*-
"""
===================================
统一股票身份解析
===================================

为 /ask、/analyze 等命令提供轻量、快速的股票代码→名称解析。

优先级（由快到慢）：
1. 本地静态映射 STOCK_NAME_MAP
2. DataFetcherManager.get_stock_name()（轻量行情接口，超时 3s）
3. 降级：仅返回代码

禁止调用 LLM、Agent、新闻搜索来猜股票名。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Optional

from src.data.stock_mapping import STOCK_NAME_MAP

logger = logging.getLogger(__name__)

_RESOLVE_TIMEOUT = 3.0  # 数据源查询超时


def resolve_stock_label(code: str) -> str:
    """将股票代码解析为显示用标签，格式为 ``名称（代码）``。

    优先本地静态映射，其次轻量行情接口，失败则降级为仅显示代码。

    Args:
        code: 股票代码（如 ``300502``、``AAPL``、``00700``）

    Returns:
        显示标签，例如 ``新易盛（300502）`` 或 ``300502``（降级时）
    """
    name = _resolve_name(code)
    if name:
        return f"{name}（{code}）"
    return code


def _resolve_name(code: str) -> Optional[str]:
    """尝试解析股票中文名称，返回 None 表示无法解析。"""
    normalized = code.strip().upper()

    # 1. 本地静态映射（最快，零网络）
    name = STOCK_NAME_MAP.get(normalized)
    if name:
        logger.debug("[StockResolver] 命中静态映射: %s -> %s", normalized, name)
        return name

    # 2. 轻量行情接口（超时 3s）
    try:
        from data_provider.base import DataFetcherManager

        mgr = DataFetcherManager()
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(mgr.get_stock_name, normalized, False)
            name = fut.result(timeout=_RESOLVE_TIMEOUT)
        if name:
            logger.debug("[StockResolver] 从行情接口获取: %s -> %s", normalized, name)
            return name
    except FutureTimeoutError:
        logger.warning("[StockResolver] 查询股票名称超时(%s): %s", _RESOLVE_TIMEOUT, normalized)
    except Exception as exc:
        logger.debug("[StockResolver] 查询股票名称失败 %s: %s", normalized, exc)

    return None