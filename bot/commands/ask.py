# -*- coding: utf-8 -*-
"""
Ask command - analyze one or more stocks using Agent skills.

Usage:
    /ask 600519                        -> Analyze with default skill
    /ask 600519 用缠论分析              -> Parse skill from message
    /ask 600519 chan_theory             -> Specify skill id directly
    /ask 600519,000858 波浪理论         -> Multi-stock comparison with skill overlay
"""

import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed
from typing import Any, Callable, Dict, List, Optional, Tuple

from bot.commands.base import BotCommand, CATEGORY_AI
from bot.models import BotMessage, BotResponse
from data_provider.base import canonical_stock_code
from src.config import get_config
from src.storage import get_db

# ── 进度回调类型 ────────────────────────────────────────────────
# 参数: (pct, completed_stages, current_stage, failed_stages, elapsed_seconds, final_text)
# completed_stages / failed_stages 为已完成的阶段名称列表
# final_text: 若提供，表示分析结束，用此文本替换进度消息
_ProgressCallback = Optional[Callable[[int, List[str], str, List[str], float, Optional[str]], None]]

# 正在分析中的股票代码集合（用于 dedup）
_analyzing_codes: set = set()

logger = logging.getLogger(__name__)


class _FastPipelineConfig:
    """Config wrapper that overrides ``litellm_model`` for Fast Pipeline.

    Preserves all other config attributes so that LLMToolAdapter and
    route resolution continue to work with the correct channel API keys,
    fallback models, and provider settings.
    """

    __slots__ = ('_orig', '_model')

    def __init__(self, original, ask_fast_model: str):
        object.__setattr__(self, '_orig', original)
        object.__setattr__(self, '_model', ask_fast_model)

    def __getattr__(self, name):
        if name == 'litellm_model':
            return self._model
        return getattr(self._orig, name)

    def __setattr__(self, name, value):
        if name in ('_orig', '_model'):
            object.__setattr__(self, name, value)
        else:
            setattr(self._orig, name, value)

# Fast Pipeline 超时配置
_FAST_PIPELINE_TOOL_TIMEOUT_S = 8.0    # 单个工具超时（快速降级）
_FAST_PIPELINE_NEWS_TIMEOUT_S = 10.0   # 情报增强数据超时
_FAST_PIPELINE_LLM_TIMEOUT_S = 45.0    # 最终 LLM 调用超时
_FAST_PIPELINE_TOTAL_TIMEOUT_S = 90.0  # 快速问股总超时兜底


class AskCommand(BotCommand):
    """
    Ask command handler - invoke Agent with a specific skill to analyze stocks.
    """

    _MULTI_ANALYZE_TIMEOUT_S = 150.0

    @property
    def name(self) -> str:
        return "ask"

    @property
    def aliases(self) -> List[str]:
        return ["问股"]

    @property
    def description(self) -> str:
        return "AI Agent 智能问股"

    @property
    def usage(self) -> str:
        return "/ask <股票代码[,代码2,...]> [技能名称]"

    @property
    def examples(self) -> List[str]:
        return ["/ask 600519", "/ask 600519,000858 缠论"]

    @property
    def category(self) -> str:
        return CATEGORY_AI

    @property
    def menu_label(self) -> str:
        return "🤖 AI 智能问股"

    @staticmethod
    def _build_progress_text(
        stock_label: str,
        pct: int,
        completed: List[str],
        current: str,
        failed: List[str],
        elapsed: float,
    ) -> str:
        """Build a progress message for Telegram.

        Args:
            stock_label: e.g. "首都在线（300846）"
            pct: 0-100 progress percentage
            completed: completed stage names (e.g. ["行情数据", "技术分析"])
            current: current stage name (e.g. "AI 综合判断")
            failed: failed stage names (e.g. ["情报搜索"])
            elapsed: elapsed seconds so far
        """
        bar_len = 10
        filled = min(pct * bar_len // 100, bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)

        parts = [f"🔄 正在分析 {stock_label} {bar} {pct}%"]
        for name in completed:
            parts.append(f"✅ {name}")
        for name in failed:
            parts.append(f"⚠️ {name} 不可用，已跳过")
        if current:
            parts.append(f"🔄 {current}")
        parts.append(f"已用时：{elapsed:.0f}秒")
        return " | ".join(parts)

    def _merge_code_args(self, args: List[str]) -> tuple[str, List[str]]:
        """Merge stock code arguments separated by commas or explicit ``vs`` markers."""
        if not args:
            return "", []

        code_like = re.compile(
            r"^,?(\d{6}|hk\d{5}|[A-Za-z]{1,5}(\.[A-Za-z]{1,2})?),?$",
            re.IGNORECASE,
        )
        raw_codes_parts = [args[0]]
        rest_args = list(args[1:])

        while rest_args:
            token = rest_args[0]
            prev = raw_codes_parts[-1].rstrip()

            if token.lower() == "vs" and len(rest_args) > 1 and code_like.match(rest_args[1]):
                raw_codes_parts.append(rest_args[1])
                rest_args = rest_args[2:]
                continue

            has_comma_separator = (
                prev.endswith(",")
                or prev.endswith("，")
                or token.lstrip().startswith(",")
                or token.lstrip().startswith("，")
            )
            if code_like.match(token) and has_comma_separator:
                raw_codes_parts.append(token)
                rest_args = rest_args[1:]
                continue

            break

        normalized_parts = [part.strip(",，") for part in raw_codes_parts]
        raw_code_str = ",".join(normalized_parts)
        return raw_code_str, rest_args

    def _parse_stock_codes(self, raw: str) -> List[str]:
        """Parse one or more stock codes from the first argument."""
        parts = [p.strip().upper() for p in raw.replace("，", ",").split(",") if p.strip()]
        return [canonical_stock_code(part) for part in parts]

    def _validate_single_code(self, code: str) -> Optional[str]:
        """Validate a single stock code format."""
        normalized = code.upper()
        is_a_stock = re.match(r"^\d{6}$", normalized)
        is_hk_stock = re.match(r"^HK\d{5}$", normalized)
        is_us_stock = re.match(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$", normalized)

        if not (is_a_stock or is_hk_stock or is_us_stock):
            return f"无效的股票代码: {normalized}（A股6位数字 / 港股HK+5位数字 / 美股1-5个字母）"
        return None

    def validate_args(self, args: List[str]) -> Optional[str]:
        """Validate arguments."""
        if not args:
            return "请输入股票代码。用法: /ask <股票代码[,代码2,...]> [技能名称]"

        raw_code_str, _ = self._merge_code_args(args)
        codes = self._parse_stock_codes(raw_code_str)
        if not codes:
            return "请输入至少一个有效的股票代码"

        for code in codes:
            error = self._validate_single_code(code)
            if error:
                return error

        if len(codes) > 5:
            return "一次最多分析 5 只股票"

        return None

    @staticmethod
    def _load_skills() -> List[object]:
        try:
            from src.agent.factory import get_skill_manager

            sm = get_skill_manager()
            return list(sm.list_skills())
        except Exception as e:
            logger.warning("_load_skills failed: Failed to load skills: %s", e, exc_info=True)
            return []

    @classmethod
    def _get_default_skill_id(cls) -> str:
        try:
            from src.agent.skills.defaults import get_primary_default_skill_id

            return get_primary_default_skill_id(cls._load_skills())
        except Exception as e:
            logger.warning("_get_default_skill_id failed: Failed to resolve default skill id: %s", e, exc_info=True)
            return ""

    @classmethod
    def _build_skill_alias_pairs(cls) -> List[tuple[str, str]]:
        alias_pairs: List[tuple[str, str]] = []
        for skill in cls._load_skills():
            skill_id = str(getattr(skill, "name", "")).strip()
            if not skill_id:
                continue
            aliases = [skill_id, getattr(skill, "display_name", "")] + list(getattr(skill, "aliases", []) or [])
            for alias in aliases:
                alias_text = str(alias).strip()
                if alias_text:
                    alias_pairs.append((alias_text, skill_id))

        alias_pairs.sort(key=lambda item: (len(item[0]), item[0]), reverse=True)
        return alias_pairs

    def _parse_skill(self, args: List[str]) -> str:
        """Parse skill from arguments, returning the resolved skill id."""
        default_skill_id = self._get_default_skill_id()
        if len(args) < 2:
            return default_skill_id

        skill_text = " ".join(args[1:]).strip()
        available_ids = {str(getattr(skill, "name", "")).strip() for skill in self._load_skills()}
        if skill_text in available_ids:
            return skill_text

        for alias_text, skill_id in self._build_skill_alias_pairs():
            if alias_text in skill_text:
                return skill_id

        return default_skill_id

    def _resolve_skill_name(self, skill_id: Optional[str]) -> str:
        """Resolve a skill id to a human-readable display name."""
        if not skill_id:
            return "default"
        for skill in self._load_skills():
            if str(getattr(skill, "name", "")).strip() == skill_id:
                display_name = str(getattr(skill, "display_name", "")).strip()
                return display_name or skill_id
        return skill_id

    @staticmethod
    def _build_execution_context(stock_code: str, skill_id: str) -> Dict[str, Any]:
        selected = [skill_id] if skill_id else []
        return {
            "stock_code": stock_code,
            "skills": selected,
            "strategies": selected,
        }

    @staticmethod
    def _build_user_message(stock_code: str, skill_id: str, skill_text: str) -> str:
        user_msg = f"请分析股票 {stock_code}"
        if skill_id:
            user_msg = f"请使用 {skill_id} 技能分析股票 {stock_code}"
        if skill_text:
            user_msg = f"请分析股票 {stock_code}，{skill_text}"
        return user_msg

    def execute(self, message: BotMessage, args: List[str]) -> BotResponse:
        """Execute the ask command via Agent pipeline. Supports multi-stock."""
        config = get_config()

        if not config.agent_mode:
            return BotResponse.text_response(
                "⚠️ Agent 模式未开启，无法使用问股功能。\n请在配置中设置 `AGENT_MODE=true`。"
            )

        raw_code_str, remaining_args = self._merge_code_args(args)
        codes = self._parse_stock_codes(raw_code_str)

        # 检测 detail 模式：在剩余参数中查找 detail/详细/d，并从 skill_text 中移除
        detail_mode = False
        filtered_args = []
        for token in remaining_args:
            if token.lower() in ("detail", "详细", "d"):
                detail_mode = True
            else:
                filtered_args.append(token)
        remaining_args = filtered_args

        skill_id = self._parse_skill(["placeholder"] + remaining_args) if remaining_args else self._get_default_skill_id()
        skill_text = " ".join(remaining_args).strip()

        logger.info(
            "[AskCommand] Stocks: %s, Skill: %s, Extra: %s, Detail: %s",
            codes, skill_id, skill_text, detail_mode,
        )

        if len(codes) == 1:
            code = codes[0]
            # ── 单股 dedup ──
            if code in _analyzing_codes:
                logger.info("[AskCommand] Dedup: %s is already being analyzed, skipping", code)
                return BotResponse.text_response(f"⏳ {code} 正在分析中，请等待当前任务完成")

            # ── 构建进度回调（仅 Telegram 平台生效） ──
            progress_cb: _ProgressCallback = None
            if message.platform == "telegram":
                try:
                    import requests as _requests
                    from src.notification_sender.telegram_sender import TelegramSender
                    sender = TelegramSender(config)
                    bot_token = getattr(config, "telegram_bot_token", "")
                    stock_label = code

                    # 先发一条初始进度消息，拿到 message_id
                    init_text = self._build_progress_text(
                        stock_label, 0, [], "任务已接收", [], 0.0,
                    )
                    send_resp = _requests.post(
                        f"https://api.telegram.org/bot{bot_token}/sendMessage",
                        json={
                            "chat_id": message.chat_id,
                            "text": init_text,
                            "disable_web_page_preview": True,
                        },
                        timeout=10,
                    )
                    send_data = send_resp.json()
                    if send_data.get("ok"):
                        sent_message_id = str(send_data["result"]["message_id"])
                        _chat_id = message.chat_id

                        def _make_progress_cb(_sid, _cid, _label):
                            def _cb(pct: int, completed: List[str], current: str, failed: List[str], elapsed: float, final_text: Optional[str] = None):
                                try:
                                    if final_text is not None:
                                        sender.edit_message(_cid, _sid, final_text, timeout_seconds=10)
                                    else:
                                        text = AskCommand._build_progress_text(_label, pct, completed, current, failed, elapsed)
                                        sender.edit_message(_cid, _sid, text, timeout_seconds=10)
                                except Exception:
                                    logger.warning("[AskCommand] Progress edit failed (non-fatal)", exc_info=True)
                            return _cb

                        progress_cb = _make_progress_cb(sent_message_id, _chat_id, stock_label)
                except Exception as exc:
                    logger.warning("[AskCommand] Failed to set up progress (non-fatal): %s", exc)

            # ── 注册分析锁 ──
            _analyzing_codes.add(code)
            try:
                return self._analyze_single(
                    config, message, code, skill_id, skill_text,
                    detail_mode=detail_mode, progress_cb=progress_cb,
                )
            finally:
                _analyzing_codes.discard(code)

        return self._analyze_multi(config, message, codes, skill_id, skill_text)

    def _analyze_single(
        self,
        config,
        message: BotMessage,
        code: str,
        skill_id: str,
        skill_text: str,
        detail_mode: bool = False,
        progress_cb: _ProgressCallback = None,
    ) -> BotResponse:
        """Analyze a single stock.

        Args:
            detail_mode: 若为 True，执行完整 Agent 四阶段（含 Step4 完整报告）；
                否则使用固定 Fast Pipeline — 并行数据采集 + 单次 LLM 生成五段式简报。
            progress_cb: 进度回调函数，用于实时更新进度消息。
        """
        try:
            if detail_mode:
                return self._analyze_single_agent(config, message, code, skill_id, skill_text)

            # 默认快速问股：固定 Fast Pipeline，禁止多轮 Agent 循环
            return self._fast_pipeline_analyze(config, code, skill_id, skill_text, progress_cb=progress_cb)

        except Exception as exc:
            logger.error("Ask command failed: %s", exc)
            logger.exception("Ask error details:")
            if progress_cb:
                try:
                    progress_cb(0, [], "", [], 0.0,
                                final_text=f"❌ 分析失败｜异常：{type(exc).__name__}｜{exc}")
                except Exception:
                    pass
            return BotResponse.text_response(f"⚠️ 问股执行出错: {str(exc)}")

    def _analyze_single_agent(
        self,
        config,
        message: BotMessage,
        code: str,
        skill_id: str,
        skill_text: str,
    ) -> BotResponse:
        """完整 Agent 模式，用于 /ask detail 和 /research。"""
        from src.agent.factory import build_agent_executor

        executor = build_agent_executor(
            config,
            skills=[skill_id] if skill_id else None,
            brief_mode=False,
        )
        user_msg = self._build_user_message(code, skill_id, skill_text)
        session_id = f"{message.platform}_{message.user_id}:ask_{code}_{uuid.uuid4()}"
        result = executor.chat(
            message=user_msg,
            session_id=session_id,
            context=self._build_execution_context(code, skill_id),
        )

        if result.success:
            skill_name = self._resolve_skill_name(skill_id)
            timing_lines = self._format_timing_summary(result)
            header = f"📊 {code} | 技能: {skill_name}\n{'─' * 30}\n"
            return BotResponse.text_response(
                header + result.content + "\n\n" + timing_lines
            )

        return BotResponse.text_response(f"⚠️ 分析失败: {result.error}")

    @staticmethod
    def _init_llm_adapter(config):
        """初始化 LLMToolAdapter，复用项目现有通道路由/API key 注入逻辑。"""
        from src.agent.llm_adapter import LLMToolAdapter
        return LLMToolAdapter(config)

    def _fast_pipeline_analyze(
        self,
        config,
        code: str,
        skill_id: str,
        skill_text: str,
        *,
        progress_cb: _ProgressCallback = None,
    ) -> BotResponse:
        """固定 Fast Pipeline：并行数据采集 + 单次 LLM 生成五段式简报。

        调用链（对比旧 Agent 多轮循环）：
        - 旧：LLM→tool→LLM→tool→LLM→tool→LLM→tool→LLM（5 次 LLM 调用）
        - 新：并行 tool→LLM（1 次 LLM 调用）

        最终 LLM 调用复用项目现有 LLMToolAdapter.call_text()，走已配置的
        LITELLM_MODEL + LLM_CHANNELS 通道路由，不自行创建 litellm client。

        Args:
            progress_cb: 可选进度回调，用于实时更新 Telegram 进度消息。
        """
        t_start = time.monotonic()

        # ── 安全包装进度回调：回调失败不阻塞分析 ──
        def _safe_progress_cb(*args, **kwargs):
            if progress_cb is None:
                return
            try:
                progress_cb(*args, **kwargs)
            except Exception:
                logger.warning(
                    "[AskCommand] progress callback failed (non-fatal) — "
                    "analysis continues regardless",
                    exc_info=True,
                )

        # ── Phase 1: 并行数据获取（行情 + 历史K线） + 本地技术计算 ──
        # 策略：先并行获取 quote 和 history（各自独立超时，失败不阻塞）；
        #       若 history 成功，用已获取数据在本地计算 trend（无需再次网络请求）。
        #       避免 trend 工具内部重复拉取 60 日 K 线拖慢流程。
        t_data_start = time.monotonic()
        data_results: Dict[str, Any] = {}
        data_errors: Dict[str, str] = {}
        tool_timings: Dict[str, str] = {}  # 用于诊断日志

        from src.agent.tools.data_tools import _handle_get_realtime_quote, _handle_get_daily_history

        def _run_tool_safe(name: str, fn, *args, timeout: float = _FAST_PIPELINE_TOOL_TIMEOUT_S):
            """执行单个工具并记录耗时/状态/异常。"""
            t0 = time.monotonic()
            try:
                with ThreadPoolExecutor(max_workers=1) as _pool:
                    fut = _pool.submit(fn, *args)
                    result = fut.result(timeout=timeout)
                elapsed = time.monotonic() - t0
                tool_timings[name] = f"{name} {elapsed:.1f}s ✅"
                logger.info("[FastPipeline] %s for %s: %.1fs ✅", name, code, elapsed)
                return result
            except FutureTimeoutError:
                elapsed = time.monotonic() - t0
                tool_timings[name] = f"{name} {elapsed:.1f}s ❌ timeout"
                data_errors[name] = f"timeout ({elapsed:.1f}s)"
                logger.warning("[FastPipeline] %s timeout for %s (%.1fs)", name, code, elapsed)
                return None
            except Exception as exc:
                elapsed = time.monotonic() - t0
                tool_timings[name] = f"{name} {elapsed:.1f}s ❌ {type(exc).__name__}"
                data_errors[name] = str(exc)
                logger.warning("[FastPipeline] %s failed for %s (%.1fs): %s", name, code, elapsed, exc)
                return None

        # Step 1: 并行获取 quote 和 history（各自独立超时 8s）
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_quote = pool.submit(_run_tool_safe, "quote", _handle_get_realtime_quote, code)
            fut_history = pool.submit(_run_tool_safe, "history", _handle_get_daily_history, code, 60)
            quote_result = fut_quote.result()
            history_result = fut_history.result()

        if quote_result is not None:
            data_results["quote"] = quote_result
        if history_result is not None:
            data_results["history"] = history_result

        # 进度更新：行情数据阶段完成
        completed_stages = []
        failed_stages = []
        if quote_result is not None:
            completed_stages.append("行情数据")
        else:
            failed_stages.append("行情数据")
        if history_result is not None:
            completed_stages.append("历史数据")
        else:
            failed_stages.append("历史数据")
        _safe_progress_cb(30, completed_stages, "技术分析", failed_stages, time.monotonic() - t_start)

        # Step 2: 本地技术分析（复用已获取的 history 数据，不重复网络请求）
        history_data = history_result if history_result is not None else None
        if history_data and isinstance(history_data, dict):
            hist_records = history_data.get("data") or []
            if isinstance(hist_records, list) and len(hist_records) >= 20:
                t0 = time.monotonic()
                try:
                    import pandas as pd
                    from src.stock_analyzer import StockTrendAnalyzer

                    df = pd.DataFrame(hist_records)
                    # 确保必要的列存在
                    required_cols = {"close", "date"}
                    if required_cols.issubset(df.columns):
                        analyzer = StockTrendAnalyzer()
                        result = analyzer.analyze(df, code)
                        trend_data = {
                            "code": result.code,
                            "trend_status": result.trend_status.value,
                            "ma_alignment": result.ma_alignment,
                            "trend_strength": result.trend_strength,
                            "ma5": result.ma5,
                            "ma10": result.ma10,
                            "ma20": result.ma20,
                            "ma60": result.ma60,
                            "current_price": result.current_price,
                            "bias_ma5": round(result.bias_ma5, 2),
                            "bias_ma10": round(result.bias_ma10, 2),
                            "bias_ma20": round(result.bias_ma20, 2),
                            "volume_status": result.volume_status.value,
                            "volume_ratio_5d": round(result.volume_ratio_5d, 2),
                            "volume_trend": result.volume_trend,
                            "support_levels": result.support_levels,
                            "resistance_levels": result.resistance_levels,
                            "macd_dif": round(result.macd_dif, 4),
                            "macd_dea": round(result.macd_dea, 4),
                            "macd_bar": round(result.macd_bar, 4),
                            "macd_status": result.macd_status.value,
                            "macd_signal": result.macd_signal,
                            "rsi_6": round(result.rsi_6, 2),
                            "rsi_12": round(result.rsi_12, 2),
                            "rsi_24": round(result.rsi_24, 2),
                            "rsi_status": result.rsi_status.value,
                            "rsi_signal": result.rsi_signal,
                            "buy_signal": result.buy_signal.value,
                            "signal_score": result.signal_score,
                            "signal_reasons": result.signal_reasons,
                            "risk_factors": result.risk_factors,
                        }
                        elapsed = time.monotonic() - t0
                        tool_timings["trend"] = f"trend {elapsed:.1f}s ✅ (local)"
                        logger.info("[FastPipeline] trend (local) for %s: %.1fs ✅", code, elapsed)
                        data_results["trend"] = trend_data
                    else:
                        elapsed = time.monotonic() - t0
                        tool_timings["trend"] = f"trend {elapsed:.1f}s ❌ missing_cols"
                        data_errors["trend"] = f"history missing columns: {required_cols - set(df.columns)}"
                        logger.warning("[FastPipeline] trend (local) for %s missing cols: %s", code, required_cols - set(df.columns))
                except Exception as exc:
                    elapsed = time.monotonic() - t0
                    tool_timings["trend"] = f"trend {elapsed:.1f}s ❌ {type(exc).__name__}"
                    data_errors["trend"] = str(exc)
                    logger.warning("[FastPipeline] trend (local) failed for %s (%.1fs): %s", code, elapsed, exc)
            elif hist_records:
                # 历史数据不足 20 条，尝试远端 trend 工具
                t0 = time.monotonic()
                from src.agent.tools.analysis_tools import _handle_analyze_trend
                trend_result = _run_tool_safe("trend", _handle_analyze_trend, code)
                if trend_result is not None:
                    data_results["trend"] = trend_result
            else:
                tool_timings["trend"] = "trend skipped (no history)"
                data_errors["trend"] = "no history data available"
        else:
            # 完全无历史数据，尝试远端 trend 工具
            t0 = time.monotonic()
            from src.agent.tools.analysis_tools import _handle_analyze_trend
            trend_result = _run_tool_safe("trend", _handle_analyze_trend, code)
            if trend_result is not None:
                data_results["trend"] = trend_result

        t_data_tech = time.monotonic() - t_data_start

        # 进度更新：技术分析阶段完成
        if data_results.get("trend"):
            completed_stages.append("技术分析")
        else:
            failed_stages.append("技术分析")
        _safe_progress_cb(50, completed_stages, "情报搜索", failed_stages, time.monotonic() - t_start)

        # 工具诊断日志（一行输出便于问题定位）
        diag_line = " | ".join(tool_timings.values()) if tool_timings else "no tools completed"
        logger.info("[FastPipeline] Tool diagnostics for %s: %s", code, diag_line)

        # ── Phase 2: 情报增强数据（Best-effort，超时或失败立即跳过） ──
        t_intel_start = time.monotonic()
        news_data: Optional[Dict[str, Any]] = None
        try:
            from src.agent.tools.search_tools import _handle_search_stock_news

            with ThreadPoolExecutor(max_workers=1) as pool:
                news_future = pool.submit(_handle_search_stock_news, code, "")
                try:
                    news_data = news_future.result(timeout=_FAST_PIPELINE_NEWS_TIMEOUT_S)
                except FutureTimeoutError:
                    logger.warning("[FastPipeline] News fetch timed out for %s (skipped)", code)
                except Exception as exc:
                    logger.warning("[FastPipeline] News fetch failed for %s: %s (skipped)", code, exc)
        except Exception as exc:
            logger.warning("[FastPipeline] News module load failed: %s (skipped)", exc)

        t_intel = time.monotonic() - t_intel_start

        # 进度更新：情报检索阶段完成
        if news_data is not None:
            completed_stages.append("情报搜索")
        else:
            failed_stages.append("情报搜索")
        _safe_progress_cb(70, completed_stages, "AI 综合判断", failed_stages, time.monotonic() - t_start)

        # ── Phase 3: 单次 LLM 调用生成五段式简报（复用项目 LLMToolAdapter） ──
        t_llm_start = time.monotonic()
        # 进度更新：进入 AI 分析阶段（保持 85% 直到完成）
        _safe_progress_cb(85, completed_stages, "AI 综合判断", failed_stages, time.monotonic() - t_start)

        # 提取结构化数据
        quote = data_results.get("quote", {}) or {}
        history = data_results.get("history", {}) or {}
        trend = data_results.get("trend", {}) or {}

        stock_name = quote.get("name") or code
        current_price = quote.get("price")
        change_pct = quote.get("change_pct")
        pe_ratio = quote.get("pe_ratio")
        pb_ratio = quote.get("pb_ratio")
        total_mv = quote.get("total_mv")
        circ_mv = quote.get("circ_mv")
        volume_ratio = quote.get("volume_ratio")
        turnover_rate = quote.get("turnover_rate")

        # 历史数据摘要
        history_records = []
        if isinstance(history, dict):
            hist_data = history.get("data") or []
            if isinstance(hist_data, list):
                history_records = hist_data[-20:]  # 最近 20 条

        # 趋势/技术指标
        ma_alignment = trend.get("ma_alignment", "")
        trend_strength = trend.get("trend_strength", "")
        trend_status = trend.get("trend_status", "")
        macd_status = trend.get("macd_status", "")
        macd_signal = trend.get("macd_signal", "")
        rsi_6 = trend.get("rsi_6")
        rsi_12 = trend.get("rsi_12")
        rsi_status = trend.get("rsi_status", "")
        volume_status = trend.get("volume_status", "")
        volume_ratio_5d = trend.get("volume_ratio_5d")
        bias_ma5 = trend.get("bias_ma5")
        bias_ma10 = trend.get("bias_ma10")
        bias_ma20 = trend.get("bias_ma20")
        ma5 = trend.get("ma5")
        ma10 = trend.get("ma10")
        ma20 = trend.get("ma20")
        support_levels = trend.get("support_levels")
        resistance_levels = trend.get("resistance_levels")
        buy_signal = trend.get("buy_signal", "")
        signal_score = trend.get("signal_score")
        signal_reasons = trend.get("signal_reasons", [])
        risk_factors = trend.get("risk_factors", [])

        # 构建结构化的 LLM 输入 Prompt
        llm_prompt = self._build_fast_pipeline_prompt(
            code=code,
            stock_name=stock_name,
            current_price=current_price,
            change_pct=change_pct,
            pe_ratio=pe_ratio,
            pb_ratio=pb_ratio,
            total_mv=total_mv,
            circ_mv=circ_mv,
            volume_ratio=volume_ratio,
            turnover_rate=turnover_rate,
            history_records=history_records,
            ma_alignment=ma_alignment,
            trend_strength=trend_strength,
            trend_status=trend_status,
            macd_status=macd_status,
            macd_signal=macd_signal,
            rsi_6=rsi_6,
            rsi_12=rsi_12,
            rsi_status=rsi_status,
            volume_status=volume_status,
            volume_ratio_5d=volume_ratio_5d,
            bias_ma5=bias_ma5,
            bias_ma10=bias_ma10,
            bias_ma20=bias_ma20,
            ma5=ma5,
            ma10=ma10,
            ma20=ma20,
            support_levels=support_levels,
            resistance_levels=resistance_levels,
            buy_signal=buy_signal,
            signal_score=signal_score,
            signal_reasons=signal_reasons,
            risk_factors=risk_factors,
            news_data=news_data,
            data_errors=data_errors,
        )

        # 复用项目现有 LLMToolAdapter（走 LITELLM_MODEL + LLM_CHANNELS 通道路由）
        # ASK_FAST_MODEL 优先用于 Fast Pipeline 最终总结，空则继承 LITELLM_MODEL
        llm_result = None
        llm_error_category = None
        try:
            from src.agent.llm_adapter import LLMToolAdapter

            fast_model = getattr(config, "ask_fast_model", "") or ""
            pipeline_config = _FastPipelineConfig(config, fast_model) if fast_model else config
            if fast_model:
                logger.info(
                    "[FastPipeline] Using ASK_FAST_MODEL=%s for %s",
                    fast_model, code,
                )

            adapter = LLMToolAdapter(pipeline_config)
            llm_response = adapter.call_text(
                messages=[{"role": "user", "content": llm_prompt}],
                temperature=0.3,
                max_tokens=1200,
                timeout=_FAST_PIPELINE_LLM_TIMEOUT_S,
            )

            if llm_response.provider == "error":
                llm_error_category = "LLM_CALL_FAILED"
                logger.error(
                    "[FastPipeline] LLM_CALL_FAILED for %s: provider=error, msg=%s, model=%s",
                    code, llm_response.content, llm_response.model,
                )
            elif llm_response.content and len(llm_response.content.strip()) > 50:
                llm_result = llm_response.content.strip()
                logger.info(
                    "[FastPipeline] LLM summary OK for %s: model=%s, provider=%s, len=%d",
                    code, llm_response.model, llm_response.provider, len(llm_response.content),
                )
            else:
                llm_error_category = "LLM_RESPONSE_PARSE_FAILED"
                content_preview = (llm_response.content or "")[:200]
                logger.error(
                    "[FastPipeline] LLM_RESPONSE_PARSE_FAILED for %s: "
                    "model=%s, provider=%s, content_len=%d, preview=%r",
                    code, llm_response.model, llm_response.provider,
                    len(llm_response.content or ""), content_preview,
                )
        except Exception as exc:
            llm_error_category = "LLM_CALL_FAILED"
            logger.error(
                "[FastPipeline] LLM_CALL_FAILED for %s: type=%s, msg=%s",
                code, type(exc).__name__, exc,
                exc_info=True,
            )

        t_llm = time.monotonic() - t_llm_start
        total_duration = time.monotonic() - t_start

        # 时序信息
        timing_line = (
            f"⏱ 数据获取 {t_data_tech:.1f}s"
            f" | 技术计算 {t_data_tech:.1f}s"
            f" | 情报 {t_intel:.1f}s"
            f" | AI总结 {t_llm:.1f}s"
            f" | 总耗时 {total_duration:.1f}s"
        )

        # 最终进度更新
        if progress_cb:
            if llm_result is not None:
                completion_text = (
                    f"✅ 分析完成｜耗时 {total_duration:.1f}秒"
                )
            else:
                # 找出失败阶段
                fail_phase = "数据获取"
                if data_errors:
                    fail_phase = "、".join(data_errors.keys())
                completion_text = (
                    f"❌ 分析失败｜失败阶段：{fail_phase}｜耗时 {total_duration:.1f}秒"
                )
            _safe_progress_cb(100, completed_stages + (["AI 综合判断"] if llm_result else []),
                            "", failed_stages, total_duration, final_text=completion_text)

        if llm_result is not None:
            return BotResponse.markdown_response(llm_result + "\n\n" + timing_line)

        # 兜底：直接展示原始数据
        fallback = self._fallback_content_summary(code, stock_name, data_results, news_data)
        return BotResponse.text_response(fallback + "\n\n" + timing_line)

    @staticmethod
    def _build_fast_pipeline_prompt(
        code: str,
        stock_name: str,
        current_price: Any,
        change_pct: Any,
        pe_ratio: Any,
        pb_ratio: Any,
        total_mv: Any,
        circ_mv: Any,
        volume_ratio: Any,
        turnover_rate: Any,
        history_records: List[Dict[str, Any]],
        ma_alignment: str,
        trend_strength: str,
        trend_status: str,
        macd_status: str,
        macd_signal: str,
        rsi_6: Any,
        rsi_12: Any,
        rsi_status: str,
        volume_status: str,
        volume_ratio_5d: Any,
        bias_ma5: Any,
        bias_ma10: Any,
        bias_ma20: Any,
        ma5: Any,
        ma10: Any,
        ma20: Any,
        support_levels: Any,
        resistance_levels: Any,
        buy_signal: str,
        signal_score: Any,
        signal_reasons: Any,
        risk_factors: Any,
        news_data: Optional[Dict[str, Any]],
        data_errors: Dict[str, str],
    ) -> str:
        """构建 Fast Pipeline 最终 LLM 调用的 Prompt。

        输入已由 Python 工具计算好的结构化数据，LLM 只需组织成五段式简报输出。
        """
        # 格式化实时行情
        price_str = f"{current_price}" if current_price is not None else "N/A"
        chg_str = f"{change_pct:+.2f}%" if change_pct is not None else "N/A"

        pe_str = f"{pe_ratio:.2f}" if isinstance(pe_ratio, (int, float)) else "N/A"
        pb_str = f"{pb_ratio:.2f}" if isinstance(pb_ratio, (int, float)) else "N/A"
        mv_str = ""
        if total_mv is not None:
            mv_str += f"总市值: {total_mv/1e8:.2f}亿"
        if circ_mv is not None:
            mv_str += f"  流通市值: {circ_mv/1e8:.2f}亿"

        vol_ratio_str = f"{volume_ratio:.2f}" if isinstance(volume_ratio, (int, float)) else "N/A"
        turn_str = f"{turnover_rate:.2f}%" if isinstance(turnover_rate, (int, float)) else "N/A"

        # 最近 K 线摘要
        kline_summary = ""
        if history_records:
            close_prices = [r.get("close") for r in history_records if r.get("close") is not None]
            if close_prices:
                low_5d = min(close_prices[-5:]) if len(close_prices) >= 5 else min(close_prices)
                high_5d = max(close_prices[-5:]) if len(close_prices) >= 5 else max(close_prices)
                kline_summary = (
                    f"近5日最低: {low_5d}  近5日最高: {high_5d}"
                )

        # 构建技术指标摘要
        tech_parts = []
        if ma_alignment:
            tech_parts.append(f"均线排列: {ma_alignment}")
        if trend_strength:
            tech_parts.append(f"趋势强度: {trend_strength}")
        if trend_status:
            tech_parts.append(f"趋势状态: {trend_status}")
        if macd_status and macd_signal:
            tech_parts.append(f"MACD: {macd_status} ({macd_signal})")
        if rsi_status:
            tech_parts.append(f"RSI(6): {rsi_6}  RSI(12): {rsi_12}  状态: {rsi_status}")
        if volume_status:
            tech_parts.append(f"量价状态: {volume_status}")
        if isinstance(volume_ratio_5d, (int, float)):
            tech_parts.append(f"5日量比: {volume_ratio_5d:.2f}")
        if bias_ma5 is not None:
            tech_parts.append(f"乖离率MA5: {bias_ma5:+.2f}%  MA10: {bias_ma10:+.2f}%  MA20: {bias_ma20:+.2f}%")
        if ma5 is not None:
            tech_parts.append(f"MA5: {ma5}  MA10: {ma10}  MA20: {ma20}")
        if buy_signal and signal_score is not None:
            tech_parts.append(f"信号: {buy_signal} (评分: {signal_score})")
        if signal_reasons:
            reasons = signal_reasons if isinstance(signal_reasons, list) else [signal_reasons]
            tech_parts.append(f"信号理由: {'; '.join(str(r) for r in reasons[:5])}")
        if risk_factors:
            risks = risk_factors if isinstance(risk_factors, list) else [risk_factors]
            tech_parts.append(f"风险因素: {'; '.join(str(r) for r in risks[:5])}")

        support_str = ""
        if support_levels:
            if isinstance(support_levels, list):
                support_str = ", ".join(str(s) for s in support_levels[:3])
            else:
                support_str = str(support_levels)
        resistance_str = ""
        if resistance_levels:
            if isinstance(resistance_levels, list):
                resistance_str = ", ".join(str(r) for r in resistance_levels[:3])
            else:
                resistance_str = str(resistance_levels)

        tech_summary = "\n".join(f"  - {p}" for p in tech_parts)

        # 新闻情报
        news_summary = ""
        if news_data and isinstance(news_data, dict) and news_data.get("success"):
            items = news_data.get("results") or []
            news_lines = []
            for item in items[:5]:
                if isinstance(item, dict):
                    title = item.get("title") or item.get("news_title") or ""
                    if title:
                        news_lines.append(f"  - {title}")
            if news_lines:
                news_summary = "最新新闻:\n" + "\n".join(news_lines)
            else:
                news_summary = "有新闻搜索返回，但无有效标题"
        elif news_data and isinstance(news_data, dict) and news_data.get("error"):
            news_summary = f"新闻搜索不可用: {news_data['error']}"
        else:
            news_summary = "新闻搜索未配置或已跳过"

        # 数据错误提示
        error_notes = ""
        if data_errors:
            error_notes = "以下数据获取失败: " + "; ".join(f"{k}={v}" for k, v in data_errors.items())

        return f"""你是一个股票分析专家。请根据以下系统采集的结构化数据，直接生成一份五段式股票分析简报。

## 数据摘要

### 实时行情
股票: {stock_name}（{code}）
当前价: {price_str}  涨跌幅: {chg_str}
PE: {pe_str}  PB: {pb_str}
{mv_str}
量比: {vol_ratio_str}  换手率: {turn_str}
{kline_summary}

### 技术指标
{tech_summary if tech_parts else "（技术分析数据不可用）"}

### 支撑/压力
支撑位: {support_str if support_str else "暂无数据"}
压力位: {resistance_str if resistance_str else "暂无数据"}

### 情报
{news_summary}

{error_notes}

## 输出要求

请严格按照以下五段式格式输出（不要带任何额外前缀或注释），每条依据/风险要求简洁具体：

📊 **股票名称（代码）**

🎯 **核心结论**
买入/观望/减仓/卖出
综合评分：X/10
一句话理由

📈 **关键依据**
• 依据1
（最多 4 条，基于上述数据）

⚠️ **主要风险**
• 风险1
（最多 3 条，无可靠风险则不输出本段）

🎯 **操作点位**
• 理想买入区：xxx
• 支撑位：xxx
• 止损位：xxx
• 压力位/目标位：xxx
（暂无可靠点位则输出"暂无可靠点位"）

🔄 **触发条件**
• 转强条件：xxx
• 失效条件：xxx"""

    @staticmethod
    def _fallback_content_summary(
        code: str,
        stock_name: str,
        data_results: Dict[str, Any],
        news_data: Optional[Dict[str, Any]],
    ) -> str:
        """LLM 调用失败时的兜底展示。"""
        quote = data_results.get("quote", {}) or {}
        trend = data_results.get("trend", {}) or {}

        price = quote.get("price", "N/A")
        chg = quote.get("change_pct", "N/A")
        if isinstance(chg, (int, float)):
            chg = f"{chg:+.2f}%"

        lines = [
            f"📊 **{stock_name}（{code}）**",
            "",
            "⚠️ AI 分析摘要生成失败，展示原始数据：",
            "",
            f"当前价: {price}  涨跌幅: {chg}",
        ]

        ma_alignment = trend.get("ma_alignment")
        if ma_alignment:
            lines.append(f"均线排列: {ma_alignment}")

        macd = trend.get("macd_status")
        if macd:
            lines.append(f"MACD: {macd}")

        rsi = trend.get("rsi_status")
        if rsi:
            lines.append(f"RSI: {rsi}")

        buy_signal = trend.get("buy_signal")
        signal_score = trend.get("signal_score")
        if buy_signal and signal_score is not None:
            lines.append(f"信号: {buy_signal} (评分: {signal_score})")

        lines.append("")
        lines.append(f"查看完整分析：/ask {code} detail")
        return "\n".join(lines)

    @staticmethod
    def _format_timing_summary(result) -> str:
        """从 AgentResult 的 step_timings 生成时序摘要。"""
        step_timings = getattr(result, "step_timings", None) or []
        total_duration = getattr(result, "total_duration_s", 0)

        if not step_timings:
            if total_duration > 0:
                return f"⏱ 总耗时：{total_duration:.1f}s"
            return ""

        lines = []
        for s in step_timings:
            step_num = s.get("step", "?")
            llm_s = s.get("llm_duration_s", 0)
            tool_s = s.get("tool_duration_s", 0)
            tools = s.get("tools", [])
            total_s = s.get("total_step_s", 0)

            if tools:
                tools_str = " → ".join(tools)
                lines.append(
                    f"  Step {step_num}：LLM {llm_s:.1f}s + 工具 {tool_s:.1f}s"
                    f"（{tools_str}）"
                )
            else:
                lines.append(f"  Step {step_num}：LLM {llm_s:.1f}s（最终报告）")

        total = f"⏱ 总耗时：{total_duration:.1f}s（{len(step_timings)} 步）"
        return total + "\n" + "\n".join(lines)

    @staticmethod
    def _lightweight_summarize(content: str, code: str) -> Optional[str]:
        """从 Agent 完整报告文本中提炼结构化摘要的轻量 LLM 调用。

        仅一次轻量调用，无工具调用、不重新抓取数据。
        返回 None 表示提炼失败。
        """
        try:
            import litellm

            config = get_config()
            model = getattr(config, "litellm_model", None) or "deepseek/deepseek-chat"
            report_text = content
            if len(report_text) > 8000:
                report_text = report_text[:8000] + "\n...[截断]"

            prompt = f"""你是一个股票分析摘要助手。请根据以下完整分析报告，提取结构化摘要。

要求：
- 只从报告中提取已有信息，不要编造数字
- 用中文输出
- 输出格式如下（不要带多余前缀）：

📊 **股票名称（{code}）**

🎯 **核心结论**
买入/观望/减仓/卖出
综合评分：X/10
一句话理由

📈 **关键依据**
• 依据1
• 依据2
（最多 4 条）

⚠️ **主要风险**
• 风险1
（最多 3 条，无可靠风险则不输出本段）

🎯 **操作点位**
• 理想买入区：xxx
• 支撑位：xxx
• 止损位：xxx
• 压力位/目标位：xxx
（暂无可靠点位则输出"暂无可靠点位"）

🔄 **触发条件**
• 转强条件：xxx
• 失效条件：xxx

以下是完整分析报告：
{report_text}"""

            response = litellm.completion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=800,
            )
            text = response.choices[0].message.content.strip()
            if len(text) < 50:
                return None
            return text
        except Exception:
            return None

    @staticmethod
    def _fallback_summary(content: str, code: str, skill_name: str) -> str:
        """最终降级：从内容中提取可用的基本信息，不截断。"""
        lines = [
            f"📊 **{code}** | 技能: {skill_name}",
            "",
        ]

        # 如果内容非空，先展示原始内容
        if content and len(content.strip()) > 10:
            lines.append(content.strip())
            lines.append("")
            lines.append("—" * 20)
            lines.append("")

        # 尝试从 JSON 格式提取字段
        import re
        json_fields_found = False
        for field, label in [
            ("stock_name", "股票名称"),
            ("sentiment_score", "评分"),
            ("decision_type", "决策"),
            ("trend_prediction", "趋势"),
            ("operation_advice", "操作建议"),
            ("risk_warning", "风险提示"),
        ]:
            m = re.search(rf'"{field}"\s*:\s*"([^"]+)"', content)
            if m:
                val = m.group(1).strip()
                if val and val not in ("", "无", "none"):
                    if not json_fields_found:
                        lines.append("可用字段：")
                        json_fields_found = True
                    lines.append(f"• {label}：{val}")

        # 提取 sentiment_score 数字
        m = re.search(r'"sentiment_score"\s*:\s*(\d+)', content)
        if m:
            if not json_fields_found:
                lines.append("可用字段：")
                json_fields_found = True
            lines.append(f"• 综合评分：{m.group(1)}/100")

        if not json_fields_found and not (content and len(content.strip()) > 10):
            lines.append("暂无可靠数据")

        lines.append("")
        lines.append(f"查看完整分析：/ask {code} detail")
        return "\n".join(lines)

    @staticmethod
    def _format_mobile_summary(code: str, skill_name: str, dashboard: dict) -> str:
        """从 dashboard 生成手机友好的结构化摘要。

        固定结构（绝不通过截断详细报告生成）：
            📊 股票名称（代码）
            🎯 核心结论（买入/观望/减仓/卖出 + 综合评分 + 一句话）
            📈 关键依据（趋势/量价/估值/市场环境，最多 4 条）
            ⚠️ 主要风险（最多 3 条）
            🎯 操作点位（理想买入区/支撑位/止损位/压力位）
            🔄 触发条件（什么条件下转为买入，什么条件下失效）
            查看完整分析：/ask <代码> detail

        所有内部字段名（bull_trend、sentiment_score、ma_golden_cross 等）
        绝不直接暴露，必须映射为自然中文。
        """

        # ── 内部字段名 → 人类可读中文映射表 ──
        # 用于 dashboard 字段值中可能出现的 snake_case / internal ID 清理
        _ID_MAP = {
            "bull_trend": "多头趋势",
            "sentiment_score": "情绪评分",
            "ma_golden_cross": "均线金叉",
            "volume_breakout": "放量突破",
            "support_level": "支撑位",
            "resistance_level": "压力位",
            "stop_loss": "止损位",
            "take_profit": "目标位",
            "ideal_buy": "理想买入区",
            "secondary_buy": "次优买入区",
            "trend_prediction": "趋势预测",
            "operation_advice": "操作建议",
            "risk_warning": "风险提示",
            "analysis_summary": "分析摘要",
            "price_position": "价格位置",
            "volume_analysis": "量价分析",
            "chip_structure": "筹码结构",
            "turnover_rate": "换手率",
            "profit_ratio": "盈利比例",
            "avg_cost": "平均成本",
            "concentration": "筹码集中度",
            "phase_decision": "阶段决策",
            "watch_conditions": "观察条件",
            "immediate_action": "当前动作",
            "position_advice": "仓位建议",
            "sniper_points": "狙击点位",
            "signal_attribution": "信号归因",
            "intelligence": "情报分析",
            "core_conclusion": "核心结论",
            "battle_plan": "作战计划",
            "data_perspective": "数据视角",
            "trend_status": "趋势状态",
            "ma_alignment": "均线排列",
            "is_bullish": "看涨",
            "action_checklist": "执行清单",
            "suggested_position": "建议仓位",
            "risk_control": "风险控制",
            "entry_plan": "入场计划",
            "confidence_level": "置信度",
            "decision_type": "决策类型",
            "stock_name": "股票名称",
            "one_sentence": "一句话总结",
            "time_sensitivity": "时效性",
            "latest_news": "最新消息",
            "risk_alerts": "风险预警",
            "positive_catalysts": "正面催化剂",
            "earnings_outlook": "业绩展望",
            "sentiment_summary": "情绪摘要",
            "technical_indicators": "技术指标",
            "news_sentiment": "新闻情绪",
            "fundamentals": "基本面",
            "market_conditions": "市场环境",
            "strongest_bullish_signal": "最强看多信号",
            "strongest_bearish_signal": "最强看空信号",
        }

        def _safe(val: str) -> str:
            """去除空值标记，并清理内部 ID 字符串。"""
            if not val or str(val).strip() in ("", "-", "—", "N/A", "None", "无", "none"):
                return ""
            text = str(val).strip()
            # 将文本中出现的内部 ID 替换为中文
            for eng, cn in _ID_MAP.items():
                # 只替换完整单词（前后非字母数字）
                text = re.sub(r'\b' + re.escape(eng) + r'\b', cn, text)
            return text

        def _get_str(d: dict, *keys: str, default: str = "") -> str:
            """安全地从嵌套 dict 中提取字符串值。"""
            for key in keys:
                if not isinstance(d, dict):
                    return default
                d = d.get(key, {})
            return _safe(str(d)) if isinstance(d, str) else default

        stock_name = _safe(str(dashboard.get("stock_name") or code))

        # ── 决策映射 ──
        raw_decision = str(dashboard.get("decision_type", "")).strip().lower()
        decision_map = {"buy": "买入", "hold": "观望", "sell": "卖出", "reduce": "减仓"}
        decision_text = decision_map.get(raw_decision, raw_decision)

        # ── 综合评分（0-10）──
        sentiment = dashboard.get("sentiment_score")
        comprehensive_score = ""
        if isinstance(sentiment, (int, float)) and 0 <= sentiment <= 100:
            score_val = round(sentiment / 10.0, 1)
            comprehensive_score = f"综合评分：{score_val}/10"
        elif isinstance(sentiment, (int, float)):
            score_val = max(0, min(10, round(sentiment / 10.0)))
            comprehensive_score = f"综合评分：{score_val}/10"

        # ── 一句话原因 ──
        nested = dashboard.get("dashboard") or {}
        core = nested.get("core_conclusion") or {}
        one_sentence = _safe(str(core.get("one_sentence") or ""))

        # ── 信号归因（关键依据）──
        attribution = nested.get("signal_attribution") or {}
        evidence_items = []

        # 趋势
        dp = nested.get("data_perspective") or {}
        ts = dp.get("trend_status") or {}
        trend_text = _safe(str(ts.get("ma_alignment") or ""))
        if ts.get("is_bullish") is not None:
            trend_label = "看涨" if ts.get("is_bullish") else "看跌"
            if trend_text:
                evidence_items.append(f"趋势：{trend_text}（{trend_label}）")
            else:
                evidence_items.append(f"趋势：{trend_label}")

        # 量价
        volume = dp.get("volume_analysis") or {}
        vol_status = _safe(str(volume.get("volume_status") or ""))
        vol_ratio = volume.get("volume_ratio")
        if vol_status:
            evidence_items.append(f"量价：{vol_status}")
        elif isinstance(vol_ratio, (int, float)):
            evidence_items.append(f"量价：量比 {vol_ratio:.2f}")

        # 估值/基本面
        fundamentals_val = attribution.get("fundamentals")
        if isinstance(fundamentals_val, (int, float)) and fundamentals_val > 0:
            evidence_items.append(f"估值/基本面：贡献 {fundamentals_val:.0f}%")

        # 市场环境
        market_val = attribution.get("market_conditions")
        if isinstance(market_val, (int, float)) and market_val > 0:
            evidence_items.append(f"市场环境：贡献 {market_val:.0f}%")

        # 若信号归因不足 4 条，补充技术指标和新闻情绪
        tech_val = attribution.get("technical_indicators")
        if isinstance(tech_val, (int, float)) and tech_val > 0 and len(evidence_items) < 4:
            evidence_items.append(f"技术指标：贡献 {tech_val:.0f}%")
        news_val = attribution.get("news_sentiment")
        if isinstance(news_val, (int, float)) and news_val > 0 and len(evidence_items) < 4:
            evidence_items.append(f"新闻情绪：贡献 {news_val:.0f}%")

        # 最强信号补位
        if len(evidence_items) < 2:
            strong_bull = _safe(str(attribution.get("strongest_bullish_signal") or ""))
            strong_bear = _safe(str(attribution.get("strongest_bearish_signal") or ""))
            if strong_bull and strong_bull not in ("", "none", "无"):
                evidence_items.append(f"最强看多：{strong_bull}")
            if strong_bear and strong_bear not in ("", "none", "无") and len(evidence_items) < 4:
                evidence_items.append(f"最强看空：{strong_bear}")

        # 最多保留 4 条
        evidence_items = evidence_items[:4]

        # ── 主要风险 ──
        intelligence = nested.get("intelligence") or {}
        risk_alerts = intelligence.get("risk_alerts") or []
        raw_risk_warning = _safe(str(dashboard.get("risk_warning") or ""))

        risk_items = []
        for alert in risk_alerts:
            if len(risk_items) >= 3:
                break
            if isinstance(alert, str):
                txt = _safe(alert)
                if txt:
                    risk_items.append(txt)
            elif isinstance(alert, dict):
                desc = alert.get("description") or alert.get("alert") or ""
                txt = _safe(str(desc))
                if txt:
                    risk_items.append(txt)
        if raw_risk_warning and len(risk_items) < 3:
            risk_items.append(raw_risk_warning)

        # ── 操作点位 ──
        battle_plan = nested.get("battle_plan") or {}
        sniper = battle_plan.get("sniper_points") or {}

        def _sniper_val(key: str) -> str:
            v = AskCommand._format_sniper_value(sniper.get(key))
            return _safe(v) if v else ""

        ideal_buy = _sniper_val("ideal_buy")
        secondary_buy = _sniper_val("secondary_buy")
        stop_loss = _sniper_val("stop_loss")
        take_profit = _sniper_val("take_profit")

        # 支撑位：优先从 price_position 获取，其次用 secondary_buy
        support = _safe(str(dp.get("price_position", {}).get("support_level") or ""))
        if not support:
            support = secondary_buy

        # 压力位/目标位：优先 take_profit，其次 price_position.resistance_level
        resistance = _safe(str(dp.get("price_position", {}).get("resistance_level") or ""))
        if not resistance:
            resistance = take_profit

        # 是否有可靠点位
        has_reliable_points = any(v for v in [ideal_buy, support, stop_loss, resistance])

        # ── 触发条件 ──
        phase = nested.get("phase_decision") or {}
        watch_conditions = phase.get("watch_conditions") or []
        immediate_action = _safe(str(phase.get("immediate_action") or ""))
        next_check = _safe(str(phase.get("next_check_time") or ""))
        position_advice = core.get("position_advice") or {}

        trigger_lines = []
        if immediate_action:
            trigger_lines.append(f"• 当前动作：{immediate_action}")
        for wc in watch_conditions[:3]:
            wc_str = _safe(str(wc))
            if wc_str:
                trigger_lines.append(f"• {wc_str}")
        if next_check:
            trigger_lines.append(f"• 下次检查：{next_check}")
        # 空仓/持仓建议作为触发/失效条件的补充
        np_text = _safe(str(position_advice.get("no_position") or ""))
        hp_text = _safe(str(position_advice.get("has_position") or ""))
        if np_text and np_text not in ("", "-", "无"):
            trigger_lines.append(f"• 空仓者：{np_text}")
        if hp_text and hp_text not in ("", "-", "无"):
            trigger_lines.append(f"• 持仓者：{hp_text}")

        # ── 组装输出 ──
        lines = [
            f"📊 **{stock_name}（{code}）**",
            "",
        ]

        # 🎯 核心结论
        lines.append("🎯 **核心结论**")
        lines.append(decision_text)
        if comprehensive_score:
            lines.append(comprehensive_score)
        if one_sentence:
            lines.append(one_sentence)
        lines.append("")

        # 📈 关键依据
        if evidence_items:
            lines.append("📈 **关键依据**")
            lines.extend(evidence_items)
            lines.append("")

        # ⚠️ 主要风险
        if risk_items:
            lines.append("⚠️ **主要风险**")
            for item in risk_items[:3]:
                lines.append(f"• {item}")
            lines.append("")

        # 🎯 操作点位
        lines.append("🎯 **操作点位**")
        if has_reliable_points:
            if ideal_buy:
                lines.append(f"• 理想买入区：{ideal_buy}")
            if support:
                lines.append(f"• 支撑位：{support}")
            if stop_loss:
                lines.append(f"• 止损位：{stop_loss}")
            if resistance:
                lines.append(f"• 压力位/目标位：{resistance}")
        else:
            lines.append("暂无可靠点位")
        lines.append("")

        # 🔄 触发条件
        lines.append("🔄 **触发条件**")
        if trigger_lines:
            lines.extend(trigger_lines)
        else:
            # 从 phase_decision 中提取 basic 触发和失效语义
            trigger = _safe(str(phase.get("action_window") or ""))
            if trigger:
                lines.append(f"• {trigger}")
            else:
                lines.append("暂无明确触发条件，请结合市场走势判断")
        lines.append("")

        # 尾部引导
        lines.append(f"查看完整分析：/ask {code} detail")

        return "\n".join(lines).strip()

    def _analyze_multi(
        self,
        config,
        message: BotMessage,
        codes: List[str],
        skill_id: str,
        skill_text: str,
    ) -> BotResponse:
        """Analyze multiple stocks in parallel and produce a comparison summary."""
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError, as_completed

        skill_name = self._resolve_skill_name(skill_id)
        results: Dict[str, Dict[str, Any]] = {}
        errors: Dict[str, str] = {}
        started_at = time.monotonic()
        overall_timeout_s = self._MULTI_ANALYZE_TIMEOUT_S

        platform = message.platform
        user_id = message.user_id

        def _run_one(stock_code: str) -> Tuple[str, Optional[Dict[str, Any]], Optional[str]]:
            try:
                from src.agent.conversation import conversation_manager
                from src.agent.factory import build_agent_executor

                executor = build_agent_executor(config, skills=[skill_id] if skill_id else None)
                user_msg = self._build_user_message(stock_code, skill_id, skill_text)
                session_id = f"{platform}_{user_id}:ask_{stock_code}_{uuid.uuid4()}"
                conversation_manager.add_message(session_id, "user", user_msg)

                result = executor.run(
                    task=user_msg,
                    context=self._build_execution_context(stock_code, skill_id),
                )
                if result.success or self._should_accept_fallback_content(result):
                    dashboard = result.dashboard if isinstance(result.dashboard, dict) else None
                    formatted_analysis = self._format_stock_result(stock_code, dashboard, result.content)
                    conversation_manager.add_message(session_id, "assistant", formatted_analysis)
                    return (
                        stock_code,
                        {
                            "content": result.content,
                            "dashboard": dashboard,
                            "signal": self._extract_signal(dashboard),
                            "confidence": self._extract_confidence(dashboard),
                            "summary": self._extract_summary(stock_code, dashboard, result.content),
                            "markdown": formatted_analysis,
                            "stock_name": self._extract_stock_name(stock_code, dashboard),
                            "risk_flags": self._extract_risk_flags(dashboard),
                        },
                        None,
                    )

                error_note = f"[分析失败] {result.error or '未知错误'}"
                conversation_manager.add_message(session_id, "assistant", error_note)
                return (stock_code, None, result.error or "未知错误")
            except Exception as exc:
                return (stock_code, None, str(exc))

        # Warm up DB connections before parallel history writes.
        get_db()
        pool = ThreadPoolExecutor(max_workers=min(len(codes), 5))
        future_map = {pool.submit(_run_one, code): code for code in codes}
        try:
            for future in as_completed(future_map, timeout=overall_timeout_s):
                try:
                    code, content, error = future.result(timeout=5)
                    if content is not None:
                        results[code] = content
                    else:
                        errors[code] = error or "未知错误"
                except Exception as exc:
                    code = future_map[future]
                    errors[code] = f"执行异常: {exc}"
        except FutureTimeoutError:
            logger.warning("[AskCommand] Multi-stock analysis hit overall timeout (%.1fs)", overall_timeout_s)
            for future, code in future_map.items():
                if code in results or code in errors:
                    continue
                if future.done():
                    try:
                        code_done, content, error = future.result(timeout=0)
                        if content is not None:
                            results[code_done] = content
                        else:
                            errors[code] = error or "未知错误"
                    except Exception as exc:
                        errors[code] = f"执行异常: {exc}"
                else:
                    errors[code] = "分析超时（未在 150 秒内完成）"
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        for code in codes:
            if code not in results and code not in errors:
                errors[code] = "分析超时"

        parts = [f"📊 **多股对比分析** | 技能: {skill_name}", f"{'─' * 30}", ""]

        remaining_timeout_s = max(0.0, overall_timeout_s - (time.monotonic() - started_at))
        portfolio_section = self._build_portfolio_section(
            config,
            codes,
            results,
            timeout_s=remaining_timeout_s,
        )
        if portfolio_section:
            parts.append(portfolio_section)
            parts.append("")

        if len(results) >= 2:
            parts.append("| 股票 | 信号 | 置信度 | 摘要 |")
            parts.append("|------|------|--------|------|")
            for code in codes:
                if code in results:
                    item = results[code]
                    signal = item.get("signal") or "unknown"
                    confidence = item.get("confidence")
                    confidence_text = f"{confidence:.0%}" if isinstance(confidence, (int, float)) else "-"
                    summary_line = str(item.get("summary") or "分析完成").replace("|", "/")[:80]
                    parts.append(f"| {code} | {signal} | {confidence_text} | {summary_line} |")
                elif code in errors:
                    parts.append(f"| {code} | error | - | ⚠️ {errors[code][:40]} |")
            parts.append("")

        for code in codes:
            if code in results:
                parts.append(f"### {code}")
                parts.append(results[code]["markdown"])
                parts.append("")
            elif code in errors:
                parts.append(f"### {code}")
                parts.append(f"⚠️ 分析失败: {errors[code]}")
                parts.append("")

        return BotResponse.markdown_response("\n".join(parts))

    @staticmethod
    def _should_accept_fallback_content(result: Any) -> bool:
        """Keep usable free-form answers when dashboard JSON parsing fails."""
        if getattr(result, "success", False):
            return True

        content = getattr(result, "content", "")
        error = str(getattr(result, "error", "") or "")
        if not isinstance(content, str) or not content.strip():
            return False

        return error == "Failed to parse dashboard JSON from agent response"

    @staticmethod
    def _extract_stock_name(stock_code: str, dashboard: Optional[Dict[str, Any]]) -> str:
        if isinstance(dashboard, dict):
            stock_name = dashboard.get("stock_name")
            if isinstance(stock_name, str) and stock_name.strip():
                return stock_name.strip()
        return stock_code

    @staticmethod
    def _extract_signal(dashboard: Optional[Dict[str, Any]]) -> str:
        if isinstance(dashboard, dict):
            signal = dashboard.get("decision_type")
            if isinstance(signal, str) and signal.strip():
                return signal.strip()
        return "unknown"

    @staticmethod
    def _extract_confidence(dashboard: Optional[Dict[str, Any]]) -> Optional[float]:
        if not isinstance(dashboard, dict):
            return None

        score = dashboard.get("sentiment_score")
        try:
            return max(0.0, min(1.0, float(score) / 100.0))
        except (TypeError, ValueError):
            pass

        level = str(dashboard.get("confidence_level") or "").strip()
        return {"高": 0.85, "中": 0.65, "低": 0.45}.get(level)

    @staticmethod
    def _extract_summary(stock_code: str, dashboard: Optional[Dict[str, Any]], raw_content: str) -> str:
        if isinstance(dashboard, dict):
            for key in ("analysis_summary", "risk_warning", "trend_prediction"):
                value = dashboard.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            dashboard_block = dashboard.get("dashboard")
            if not isinstance(dashboard_block, dict):
                dashboard_block = {}
            core_conclusion = dashboard_block.get("core_conclusion")
            if not isinstance(core_conclusion, dict):
                core_conclusion = {}
            core = core_conclusion.get("one_sentence")
            if isinstance(core, str) and core.strip():
                return core.strip()

        for line in raw_content.splitlines():
            stripped = line.strip()
            if stripped and len(stripped) > 4 and not stripped.startswith(("{", "}", "\"")):
                return stripped[:120]
        return f"{stock_code} 分析完成"

    @staticmethod
    def _extract_risk_flags(dashboard: Optional[Dict[str, Any]]) -> List[Dict[str, str]]:
        if not isinstance(dashboard, dict):
            return []

        flags: List[Dict[str, str]] = []
        dashboard_block = dashboard.get("dashboard")
        if not isinstance(dashboard_block, dict):
            dashboard_block = {}
        intelligence = dashboard_block.get("intelligence")
        if not isinstance(intelligence, dict):
            intelligence = {}
        for alert in intelligence.get("risk_alerts", [])[:5]:
            if isinstance(alert, str) and alert.strip():
                flags.append({"category": "portfolio_input", "description": alert.strip(), "severity": "medium"})

        risk_warning = dashboard.get("risk_warning")
        if isinstance(risk_warning, str) and risk_warning.strip():
            flags.append({"category": "portfolio_input", "description": risk_warning.strip(), "severity": "medium"})
        return flags

    @staticmethod
    def _format_sniper_value(value: Any) -> Optional[str]:
        if value is None:
            return None

        text = str(value).strip()
        if not text or text in {"-", "—", "N/A", "None"}:
            return None

        prefixes = (
            "理想买入点：",
            "次优买入点：",
            "止损位：",
            "目标位：",
            "理想买入点:",
            "次优买入点:",
            "止损位:",
            "目标位:",
        )
        for prefix in prefixes:
            if text.startswith(prefix):
                stripped = text[len(prefix):].strip()
                return stripped or None

        return text

    @staticmethod
    def _format_stock_result(stock_code: str, dashboard: Optional[Dict[str, Any]], raw_content: str) -> str:
        if not isinstance(dashboard, dict):
            content = raw_content
            if len(content) > 800:
                content = content[:800] + "\n... (已截断，完整分析请单独查询)"
            return content

        lines = []
        stock_name = dashboard.get("stock_name")
        if isinstance(stock_name, str) and stock_name.strip() and stock_name.strip() != stock_code:
            lines.append(f"**名称**: {stock_name.strip()}")

        decision = dashboard.get("decision_type")
        confidence = AskCommand._extract_confidence(dashboard)
        trend = dashboard.get("trend_prediction")
        if isinstance(decision, str):
            lines.append(
                f"**结论**: {decision}"
                + (f" | **置信度**: {confidence:.0%}" if isinstance(confidence, (int, float)) else "")
                + (f" | **趋势**: {trend}" if isinstance(trend, str) and trend.strip() else "")
            )

        summary = AskCommand._extract_summary(stock_code, dashboard, raw_content)
        if summary:
            lines.append(f"**摘要**: {summary}")

        operation = dashboard.get("operation_advice")
        if isinstance(operation, str) and operation.strip():
            lines.append(f"**操作建议**: {operation.strip()}")

        risk_warning = dashboard.get("risk_warning")
        if isinstance(risk_warning, str) and risk_warning.strip():
            lines.append(f"**风险提示**: {risk_warning.strip()}")

        dashboard_block = dashboard.get("dashboard")
        if not isinstance(dashboard_block, dict):
            dashboard_block = {}
        battle_plan = dashboard_block.get("battle_plan")
        if not isinstance(battle_plan, dict):
            battle_plan = {}
        sniper = battle_plan.get("sniper_points")
        if isinstance(sniper, dict):
            price_parts = []
            for key in ("ideal_buy", "secondary_buy", "stop_loss", "take_profit"):
                value = AskCommand._format_sniper_value(sniper.get(key))
                if value:
                    price_parts.append(f"{key}={value}")
            if price_parts:
                lines.append("**关键点位**: " + " | ".join(price_parts))

        return "\n\n".join(lines) if lines else raw_content[:800]

    def _build_portfolio_section(
        self,
        config,
        codes: List[str],
        results: Dict[str, Dict[str, Any]],
        timeout_s: Optional[float] = None,
    ) -> str:
        """Generate a portfolio-level overlay for multi-stock ask results."""
        if len(results) < 2:
            return ""

        if timeout_s is not None and timeout_s <= 0:
            logger.info("[AskCommand] Skip portfolio overlay because no timeout budget remains")
            return ""

        def _render_overlay() -> str:
            from src.agent.agents.portfolio_agent import PortfolioAgent
            from src.agent.factory import get_tool_registry
            from src.agent.llm_adapter import LLMToolAdapter
            from src.agent.protocols import AgentContext

            stock_opinions: Dict[str, Dict[str, Any]] = {}
            risk_flags: List[Dict[str, str]] = []
            stock_list: List[str] = []
            for code in codes:
                item = results.get(code)
                if not item:
                    continue
                stock_list.append(code)
                stock_opinions[code] = {
                    "signal": item.get("signal", "unknown"),
                    "confidence": item.get("confidence", 0.5),
                    "summary": item.get("summary", ""),
                    "stock_name": item.get("stock_name", code),
                }
                risk_flags.extend(item.get("risk_flags", []))

            ctx = AgentContext(query=f"Portfolio overlay for {', '.join(stock_list)}")
            ctx.data["stock_opinions"] = stock_opinions
            ctx.data["stock_list"] = stock_list
            ctx.risk_flags.extend(risk_flags[:10])

            agent = PortfolioAgent(
                tool_registry=get_tool_registry(),
                llm_adapter=LLMToolAdapter(config),
            )
            stage_result = agent.run(ctx)
            if not stage_result.success:
                return ""

            assessment = ctx.data.get("portfolio_assessment")
            if not isinstance(assessment, dict):
                return ""

            lines = ["## 组合视角", ""]
            summary = assessment.get("summary")
            if isinstance(summary, str) and summary.strip():
                lines.append(summary.strip())
                lines.append("")

            risk_score = assessment.get("portfolio_risk_score")
            if risk_score is not None:
                lines.append(f"- 组合风险分: {risk_score}")
            sector_warnings = assessment.get("sector_warnings") or []
            if sector_warnings:
                lines.append(f"- 行业集中: {'；'.join(str(item) for item in sector_warnings[:3])}")
            correlation_warnings = assessment.get("correlation_warnings") or []
            if correlation_warnings:
                lines.append(f"- 相关性风险: {'；'.join(str(item) for item in correlation_warnings[:3])}")
            rebalance = assessment.get("rebalance_suggestions") or []
            if rebalance:
                lines.append(f"- 调仓建议: {'；'.join(str(item) for item in rebalance[:3])}")
            positions = assessment.get("positions") or []
            if positions:
                position_parts = []
                for position in positions[:5]:
                    if not isinstance(position, dict):
                        continue
                    code = position.get("code")
                    weight = position.get("suggested_weight")
                    signal = position.get("signal")
                    if code and weight is not None:
                        try:
                            weight_text = f"{float(weight):.0%}"
                        except (TypeError, ValueError):
                            weight_text = str(weight)
                        suffix = f" ({signal})" if signal else ""
                        position_parts.append(f"{code}: {weight_text}{suffix}")
                if position_parts:
                    lines.append(f"- 建议仓位: {'；'.join(position_parts)}")

            return "\n".join(lines)

        if timeout_s is None:
            try:
                return _render_overlay()
            except Exception as exc:
                logger.warning("[AskCommand] Portfolio overlay failed: %s", exc)
                return ""

        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(_render_overlay)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeoutError:
            logger.warning("[AskCommand] Portfolio overlay timed out after %.2fs", timeout_s)
            return ""
        except Exception as exc:
            logger.warning("[AskCommand] Portfolio overlay failed: %s", exc)
            return ""
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
