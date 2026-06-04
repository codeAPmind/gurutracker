# Guru Tracker — 投资大佬跟单系统

自动追踪顶级投资人的持仓变动与公开发言，通过 DeepSeek 提取投资信号，实时推送到飞书群。

## 功能特性

- **SEC 13F**：季度持仓变动（巴菲特等机构投资者）
- **SEC Form 4**：内部人实时交易申报（高管买卖）
- **ARK Invest**：Cathie Wood 每日交易
- **国会交易**：佩洛西等议员持仓（DeepSeek 联网搜索）
- **社交媒体**：雪球（段永平）、X/Twitter（马斯克）
- **FMP**：Financial Modeling Prep 机构持仓数据（需付费套餐）
- **AI 信号提取**：DeepSeek-V3 结构化分析，输出置信度评分
- **飞书推送**：交互式卡片，包含交易日期、原因、数据源链接

## 系统架构

```
定时任务 (cron)
    │
    ├── SEC EDGAR (13F / Form4) ──┐
    ├── ARK Invest API            ├──→ DeepSeek 信号提取 ──→ 置信度评分 ──→ 飞书推送
    ├── DeepSeek 联网搜索          │                                        │
    └── 雪球 / X / FMP ───────────┘                                    SQLite 存储
```

## 快速开始

### 1. 环境准备

```bash
conda create -n gurutracker python=3.10 -y
conda activate gurutracker
pip install -r requirements.txt
```

### 2. 配置

```bash
cp .env.example .env
```

编辑 `.env`：

```env
DEEPSEEK_API_KEY=sk-xxxxxxxx          # https://platform.deepseek.com
FMP_API_KEY=xxxxxxxx                   # https://financialmodelingprep.com（可选）
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
FEISHU_WEBHOOK_SECRET=                 # 可选签名校验
XUEQIU_COOKIE=xq_a_token=xxx; ...     # 从浏览器开发者工具复制
QUIVER_API_KEY=                        # 可选，国会交易数据
```

**飞书 Webhook 获取**：飞书群 → 设置 → 群机器人 → 添加自定义机器人 → 复制 Webhook URL

**雪球 Cookie 获取**：登录 xueqiu.com → F12 → Network → 任意请求 → 复制 Request Headers 中的 Cookie

### 3. 生成运行脚本

```bash
# 创建加载 .env 的运行包装脚本
cat > run_job.sh << 'EOF'
#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="/path/to/miniforge3/envs/gurutracker/bin/python"
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a && source "$SCRIPT_DIR/.env" && set +a
fi
cd "$SCRIPT_DIR"
exec "$PYTHON" "$SCRIPT_DIR/scheduler.py" "$@"
EOF
chmod +x run_job.sh
```

将 `PYTHON` 替换为实际路径（`which python` 在激活环境后获取）。

### 4. 初始化数据库并测试

```bash
# 测试单个采集器
bash run_job.sh --job ark_trades      # ARK 每日交易
bash run_job.sh --job sec_form4       # SEC 内部人交易
bash run_job.sh --job sec_13f         # 季度持仓（首次运行建立基准快照）
bash run_job.sh --job congress        # 国会交易（DeepSeek 联网搜索）
bash run_job.sh --job social_media    # 雪球 + X
bash run_job.sh --job daily_digest    # 发送今日汇总到飞书

# 一次性跑全部
bash run_job.sh --job all
```

### 5. 配置 cron 定时任务

```bash
crontab -e
```

添加（按需调整时间，避开已有任务）：

```cron
# Guru Tracker — 每天 20:20 全量采集
20 20 * * * /path/to/Guru_Tracker/run_job.sh --job all >> /path/to/Guru_Tracker/logs/cron.log 2>&1
```

## 项目结构

```
Guru_Tracker/
├── config/
│   ├── settings.py          # 全局配置（从环境变量读取）
│   └── gurus.yaml           # 跟踪目标配置
├── collectors/
│   ├── base.py              # 采集器基类
│   ├── sec_13f.py           # SEC 13F 季度持仓
│   ├── sec_form4.py         # SEC Form 4 内部人交易
│   ├── ark_trades.py        # ARK Invest 每日交易
│   ├── congress.py          # 国会交易（QuiverQuant）
│   ├── congress_deepseek.py # 国会交易（DeepSeek 联网搜索，无需 API Key）
│   └── social_media.py      # 雪球 / X / FMP
├── processor/
│   ├── deepseek_engine.py   # DeepSeek 信号提取
│   ├── confidence_scorer.py # 置信度评分（0-100分）
│   └── deduplicator.py      # 24小时去重
├── notifier/
│   ├── feishu_bot.py        # 飞书 Webhook 推送
│   └── card_templates.py    # 飞书卡片模板
├── storage/
│   └── db.py                # SQLite 操作
├── scheduler.py             # 主入口
├── requirements.txt
├── .env.example             # 环境变量模板
└── setup_cron.sh            # cron 安装辅助脚本
```

## 跟踪目标配置

编辑 `config/gurus.yaml` 添加或调整跟踪目标：

```yaml
gurus:
  - name: "巴菲特"
    name_en: "Warren Buffett"
    tier: "长期"           # 长期 / 中期 / 短期（影响置信度评分）
    sources:
      - type: "sec_13f"
        cik: "0001067983"  # SEC EDGAR CIK
      - type: "sec_form4"
        cik: "0001067983"
    thresholds:
      heavy_position_pct: 5.0
      significant_change_pct: 20.0
```

## 置信度评分规则

| 维度 | 满分 | 说明 |
|------|------|------|
| 仓位权重 | 30 | 重仓 30分 / 未知 15分 / 试水 10分 |
| 数据源可靠性 | 30 | SEC 文件 30分 > ARK 25分 > 社交媒体 12分 |
| 动作强度 | 20 | 建仓/清仓 20分 > 买入/卖出 18分 > 看好/看空 8分 |
| 交叉验证 | 20 | 近7天多位大佬同向操作同一标的 |
| 大佬层级加成 | 5 | 长期价值型 +5分 |

**推送阈值**：默认 ≥ 中（40分），可在 `config/settings.py` 调整 `MIN_CONFIDENCE_TO_PUSH`。

## 月度成本估算

| 项目 | 费用 |
|------|------|
| DeepSeek API（约5000次/月） | ¥10-30 |
| FMP API（可选，免费计划500次/天） | ¥0 |
| SEC EDGAR / ARK / Capitol Trades | 免费 |
| 飞书机器人 | 免费 |
| **合计** | **¥10-30/月** |

## 注意事项

- SEC 13F 首次运行只建立基准快照，不产生信号；下一个季度报告才开始对比差异
- 雪球因 WAF 限制自动降级为 DeepSeek 联网搜索
- 国会交易默认使用 DeepSeek 联网搜索（无需 QuiverQuant Key），12小时搜索一次
- 同一大佬 + 同一股票 + 同一动作在 24 小时内只推送一次

## License

MIT
