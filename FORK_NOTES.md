# Fork 定制说明

本 fork 基于 [ZhuLinsen/daily_stock_analysis](https://github.com/ZhuLinsen/daily_stock_analysis) `v3.31.0`（commit `9ab79b82`），在其基础上增加了**单股手动深度分析（私人投顾模式）**。

## 与上游的差异

改动集中在 4 个文件 + 本说明：

| 文件 | 改动 |
|------|------|
| `.github/workflows/single-stock-analysis.yml` | 新增：手动触发的单股分析工作流（上游的 `00-daily-analysis.yml` 每日批量任务保持原样不受影响） |
| `main.py` | 新增 `--cost-price` / `--position-ratio` 命令行参数，传入分析管线 |
| `src/core/pipeline.py` | `run()` → `process_single_stock()` → `analyze_stock()` 逐层透传持仓参数 |
| `src/analyzer.py` | `analyze()` / `_format_prompt()` 接收持仓参数；买入成本 > 0 时注入「私人持仓诊断」模块 |

## 使用方式

### GitHub Actions（推荐）

`Actions` → `股票深度分析任务（单股手动）` → `Run workflow`，填写：

- **stock_code**：股票代码（如 `600519`、`hk00700`、`AAPL`）
- **cost_price**：买入成本价（`0` = 普通分析，不启用私人投顾）
- **position_ratio**：持仓比例 %（如 `30`）

### 本地运行

```bash
python main.py --stocks 600519 --cost-price 25.8 --position-ratio 30 --single-notify --force-run
```

## 私人投顾模式说明

当 `--cost-price > 0` 时，AI 分析会：

1. 以最新价格对比买入成本，计算盈亏比例（精确到 0.1%），写入核心结论
2. 给出量身定制的止盈位、止损位、补仓位（精确到分）
3. 持仓比例超过 50% 时，在风险警报中首要提示仓位过重
4. 持仓建议结合成本价给出（如“当前浮亏 x%，跌破成本价 y 元建议止损”）

## 所需 Secrets（Settings → Secrets and variables → Actions）

| Secret | 用途 | 状态 |
|--------|------|------|
| `GEMINI_API_KEY` | AI 分析（主选） | 沿用已有 |
| `TG_TOKEN` | Telegram 推送（映射到 `TELEGRAM_BOT_TOKEN`） | 沿用已有 |
| `TG_CHAT_ID` | Telegram 推送（映射到 `TELEGRAM_CHAT_ID`） | 沿用已有 |
| `SILICON_FLOW_KEY` | 硅基流动备选（映射到 `OPENAI_API_KEY`，可选） | 沿用已有 |

> 注意：应用代码读取的环境变量名是 `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`，workflow 中已做映射，无需重新配置 secrets。

## 同步上游

```bash
git remote add upstream https://github.com/ZhuLinsen/daily_stock_analysis.git
git fetch upstream && git rebase upstream/main
# 冲突集中在上述 4 个文件的少量插入点，逐一保留双方即可
```

## 落地步骤（把本目录部署到你的 GitHub fork）

本目录已是**完整源码树**（上游 v3.31.0 全量 + 5 处定制，共 1120 文件，已通过全库编译与组装校验）。两种方式任选：

### 方式一：GitHub 网页操作（无需本地 git）

1. 打开你的 fork `jsever-ms/daily_stock_analysis` → `Sync fork` → **Discard commits**（丢弃旧定制，main 与上游 v3.31.0 对齐；旧定制已被本目录完整重做，不会丢功能）
2. 在 fork 页面 `Add file → Upload files`，把本目录中这 5 个文件按原路径拖入：
   - `main.py`
   - `src/analyzer.py`
   - `src/core/pipeline.py`
   - `.github/workflows/single-stock-analysis.yml`
   - `FORK_NOTES.md`
3. Commit 后到 `Actions` → `股票深度分析任务（单股手动）` → `Run workflow` 验证

### 方式二：本地 git 推送

```bash
git clone https://github.com/jsever-ms/daily_stock_analysis.git dsa && cd dsa
git remote add upstream https://github.com/ZhuLinsen/daily_stock_analysis.git
git fetch upstream && git reset --hard upstream/main
# 用本目录的 5 个文件覆盖对应路径后：
git add -A && git commit -m "单股私人投顾模式（基于上游 v3.31.0 重做定制）" && git push --force
```

> Secrets 沿用 fork 里已有的 `GEMINI_API_KEY` / `TG_TOKEN` / `TG_CHAT_ID`（可选 `SILICON_FLOW_KEY`、`TUSHARE_TOKEN`），无需新增。
