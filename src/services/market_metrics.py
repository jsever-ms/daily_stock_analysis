"""程序化计算的交易指标（展示层专用，不访问网络）。

设计原则：
- 只做纯计算，输入来自 AnalysisResult / dashboard 已有字段
- 数据缺失时返回 None 或明确标注，绝不推测数值
- 供单股报告与通知层复用；LLM 不参与这些计算
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

_BULLISH = "\U0001F7E2"   # 🟢
WATCH = "\U0001F7E1"       # 🟡
REDUCE = "\U0001F7E0"      # 🟠
RISK = "\U0001F534"        # 🔴


def _to_float(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def action_state_label(action: Optional[str], score: Any) -> Tuple[str, str]:
    """映射为统一四态：🟢偏多 / 🟡观望 / 🟠减仓 / 🔴风险较高。

    Returns:
        (emoji, label)
    """
    a = (action or "").strip().lower()
    s = _to_float(score)
    if a in ("buy", "add"):
        return _BULLISH, "偏多"
    if a == "reduce":
        return REDUCE, "减仓"
    if a in ("sell", "alert"):
        return RISK, "风险较高"
    if a == "avoid":
        return RISK, "风险较高"
    if a == "hold":
        return WATCH, "观望"
    # 无 action 时按评分分档
    if s is not None:
        if s >= 70:
            return _BULLISH, "偏多"
        if s >= 40:
            return WATCH, "观望"
        return RISK, "风险较高"
    return WATCH, "观望"


def classify_volume_price(
    change_pct: Any,
    volume_ratio: Any,
    volume_status: Any = None,
) -> Optional[Dict[str, str]]:
    """量价六分类：放量上涨/温和放量上涨/缩量上涨/放量下跌/缩量下跌/量价背离/平量震荡。

    Returns:
        {"label", "tone", "detail"}；change_pct 或 volume_ratio 缺失时返回 None。
    """
    pct = _to_float(change_pct)
    vr = _to_float(volume_ratio)
    if pct is None:
        return None
    # 量比缺失但 LLM 给出了放量/缩量状态时退而用之
    if vr is None:
        status = str(volume_status or "").strip()
        if not status:
            return None
        up = pct > 0.5
        expanded = "放量" in status
        if expanded and up:
            label, tone = "放量上涨", _BULLISH
        elif expanded:
            label, tone = "放量下跌", RISK
        elif up:
            label, tone = "缩量上涨", WATCH
        else:
            label, tone = "缩量下跌", WATCH
        return {"label": label, "tone": tone, "detail": f"量比缺失，按「{status}」判断"}

    up = pct >= 0.5
    down = pct <= -0.5
    if up and vr >= 1.5:
        label, tone = "放量上涨", _BULLISH
        detail = "涨幅明显且量比显著放大，关注量能能否持续"
    elif up and vr >= 1.1:
        label, tone = "温和放量上涨", _BULLISH
        detail = "上涨伴随成交量小幅增加，尚未达到明显放量突破标准"
    elif up and vr < 0.9:
        label, tone = "量价背离（缩量上涨）", WATCH
        detail = "价涨量缩，反弹缺乏量能确认，谨防冲高回落"
    elif down and vr >= 1.5:
        label, tone = "放量下跌", RISK
        detail = "跌幅伴随明显放量，抛压较重"
    elif down and vr < 0.9:
        label, tone = "缩量下跌", WATCH
        detail = "缩量回调，抛压有所减轻，但承接同样不足"
    elif down:
        label, tone = "平量下跌", WATCH
        detail = "小幅下跌、量能平稳"
    elif vr >= 1.5:
        label, tone = "放量滞涨", WATCH
        detail = "量能放大但价格未涨，多空分歧加大"
    else:
        label, tone = "量价正常（窄幅震荡）", WATCH
        detail = "量能平稳、价格波动有限"
    return {"label": label, "tone": tone, "detail": detail}


def multi_period_trend(
    price: Any,
    ma5: Any,
    ma10: Any,
    ma20: Any,
    trend_score: Any = None,
) -> Optional[Dict[str, Dict[str, str]]]:
    """多周期趋势判断（短线 1-5 日 / 波段 5-20 日 / 中期 20-60 日 / 长期）。

    仅基于均线相对位置计算；MA60 及以上数据缺失时长期标注「待确认」。
    """
    p = _to_float(price)
    m5 = _to_float(ma5)
    m10 = _to_float(ma10)
    m20 = _to_float(ma20)
    if p is None and m5 is None and m10 is None and m20 is None:
        return None

    def _has(*vals: Optional[float]) -> bool:
        return all(v is not None for v in vals)

    short = {"label": "待确认", "tone": WATCH}
    if _has(p, m5):
        if _has(p, m5, m10) and p > m5 > m10:
            short = {"label": "反弹走强", "tone": _BULLISH}
        elif p > m5:
            short = {"label": "反弹", "tone": WATCH}
        elif p < m5:
            short = {"label": "走弱", "tone": RISK}

    swing = {"label": "待确认", "tone": WATCH}
    if _has(p, m20):
        if p > m20 and _has(m5, m20) and m5 > m20:
            swing = {"label": "偏强", "tone": _BULLISH}
        elif p < m20:
            swing = {"label": "偏弱", "tone": RISK}
        else:
            swing = {"label": "震荡", "tone": WATCH}

    mid = {"label": "待确认", "tone": WATCH}
    if _has(m5, m10, m20):
        if m5 > m10 > m20:
            mid = {"label": "上行", "tone": _BULLISH}
        elif m5 < m10 < m20:
            mid = {"label": "下行", "tone": RISK}
        else:
            mid = {"label": "纠缠", "tone": WATCH}
    elif _has(m5, m10):
        mid = {"label": "偏强" if m5 > m10 else "偏弱", "tone": _BULLISH if m5 > m10 else RISK}

    long_term = {"label": "待确认（缺少 MA60 及以上数据）", "tone": WATCH}

    return {"short": short, "swing": swing, "mid": mid, "long": long_term}


def ma_alignment_label(ma5: Any, ma10: Any, ma20: Any) -> Optional[Tuple[str, str]]:
    """均线结构：多头排列 / 空头排列 / 交织纠缠。Returns (label, tone) or None."""
    m5, m10, m20 = _to_float(ma5), _to_float(ma10), _to_float(ma20)
    if None in (m5, m10, m20):
        return None
    if m5 > m10 > m20:
        return "多头排列", _BULLISH
    if m5 < m10 < m20:
        return "空头排列", RISK
    return "均线交织", WATCH


def risk_reward(
    current: Any,
    support: Any,
    resistance: Any,
) -> Optional[Dict[str, Any]]:
    """风险收益比（纯价格空间计算，不代表目标价必然达到）。"""
    p, s, r = _to_float(current), _to_float(support), _to_float(resistance)
    if None in (p, s, r) or p <= 0 or s >= p or r <= p:
        return None
    down_pct = (s - p) / p * 100
    up_pct = (r - p) / p * 100
    ratio = up_pct / abs(down_pct) if down_pct != 0 else None
    return {
        "down_pct": down_pct,
        "up_pct": up_pct,
        "ratio": ratio,
        "support": s,
        "resistance": r,
    }


def format_signed_pct(value: Any) -> Optional[str]:
    """涨跌幅统一格式：+2.50% / -1.30%；非数值返回 None（调用方兜底原值）。"""
    f = _to_float(value)
    if f is None:
        return None
    return f"{f:+.2f}%"


def extract_price(value: Any) -> Optional[float]:
    """从狙击点文本（如 '1260.00元（跌破此位…）'）提取首个价格数字。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return _to_float(value)
    m = re.search(r"[-+]?\d+(?:\.\d+)?", str(value))
    return _to_float(m.group(0)) if m else None


def checklist_stats(items: Any) -> Optional[Dict[str, int]]:
    """统计检查清单：✅ 满足 / ⚠️ 部分 / ❌ 未满足。"""
    if not isinstance(items, (list, tuple)) or not items:
        return None
    ok = warn = fail = 0
    for item in items:
        text = str(item)
        if text.lstrip().startswith("✅"):
            ok += 1
        elif text.lstrip().startswith(("⚠️", "⚠", "🟡")):
            warn += 1
        elif text.lstrip().startswith(("❌", "🔴")):
            fail += 1
    return {"ok": ok, "warn": warn, "fail": fail, "total": ok + warn + fail}


def position_pnl(current: Any, cost: Any) -> Optional[float]:
    """持仓盈亏百分比（(现价-成本)/成本*100）。数据不全返回 None。"""
    p, c = _to_float(current), _to_float(cost)
    if p is None or c is None or c <= 0:
        return None
    return (p - c) / c * 100


def build_data_status(result: Any) -> List[Tuple[str, bool, str]]:
    """数据完整度盘点：返回 [(维度, 是否可用, 缺失说明或"")]。

    只依据 AnalysisResult 上真实存在的数据判断。
    """
    snapshot = getattr(result, "market_snapshot", None) or {}
    dashboard = getattr(result, "dashboard", None) or {}
    persp = dashboard.get("data_perspective") or {}
    price_pos = persp.get("price_position") or {}
    chip = persp.get("chip_structure") or {}

    ma_present = any(
        _to_float(price_pos.get(k)) is not None for k in ("ma5", "ma10", "ma20")
    )
    quote_present = bool(snapshot) and snapshot.get("close") is not None

    news_ok = bool(getattr(result, "news_evidence_present", False)) or (
        getattr(result, "news_result_count", None) or 0
    ) > 0
    fundamental_ok = bool(
        getattr(result, "fundamental_analysis", None)
        or getattr(result, "fundamental_context", None)
    )
    chip_ok = bool(chip) and not chip.get("unavailable") and any(
        chip.get(k) not in (None, "", "N/A") for k in ("profit_ratio", "avg_cost", "concentration")
    )
    sector_ok = bool(getattr(result, "sector_position", None))

    status: List[Tuple[str, bool, str]] = [
        ("行情", quote_present, "缺少当日行情快照" if not quote_present else ""),
        ("技术指标", ma_present, "缺少均线等指标数据" if not ma_present else ""),
        ("基本面", fundamental_ok, "缺少基本面数据" if not fundamental_ok else ""),
        ("新闻", news_ok, "新闻数据源未配置或零命中" if not news_ok else ""),
        ("筹码", chip_ok, "暂无可靠筹码分布数据" if not chip_ok else ""),
        ("板块", sector_ok, "缺少板块归属数据" if not sector_ok else ""),
    ]
    return status


def overall_data_confidence(status: List[Tuple[str, bool, str]]) -> str:
    """按可用维度占比给出置信度：高 / 中 / 一般 / 低。"""
    if not status:
        return "未知"
    ok_ratio = sum(1 for _, ok, _ in status if ok) / len(status)
    if ok_ratio >= 0.99:
        return "高"
    if ok_ratio >= 0.7:
        return "中"
    if ok_ratio >= 0.4:
        return "一般"
    return "低"
