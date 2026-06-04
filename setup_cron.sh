#!/bin/bash
# 安装 cron 定时任务
# 用法: bash setup_cron.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$(which python3)"
SCHEDULER="$SCRIPT_DIR/scheduler.py"
LOG="$SCRIPT_DIR/logs/cron.log"

# 确保日志目录存在
mkdir -p "$SCRIPT_DIR/logs"
mkdir -p "$SCRIPT_DIR/data"

# 加载 .env 环境变量的包装脚本
WRAPPER="$SCRIPT_DIR/run_job.sh"
cat > "$WRAPPER" << 'WRAPPER_EOF'
#!/bin/bash
# 加载环境变量并运行
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/.env" ]; then
    set -a
    source "$SCRIPT_DIR/.env"
    set +a
fi
exec python3 "$SCRIPT_DIR/scheduler.py" "$@"
WRAPPER_EOF
chmod +x "$WRAPPER"

echo "📦 安装 Python 依赖..."
pip3 install -r "$SCRIPT_DIR/requirements.txt" -q

echo "🗄️  初始化数据库..."
cd "$SCRIPT_DIR" && python3 -c "from storage.db import init_db; init_db()"

echo ""
echo "📋 将以下内容添加到 crontab (运行 crontab -e):"
echo ""
echo "# Guru Tracker — 投资大佬跟单系统"
echo "# ARK 每日交易: 工作日 07:00 和 20:00"
echo "0 7,20 * * 1-5 $WRAPPER --job ark_trades >> $LOG 2>&1"
echo ""
echo "# SEC Form4 内部人交易: 每6小时"
echo "0 */6 * * * $WRAPPER --job sec_form4 >> $LOG 2>&1"
echo ""
echo "# 国会交易: 每12小时"
echo "0 */12 * * * $WRAPPER --job congress >> $LOG 2>&1"
echo ""
echo "# 社交媒体: 每30分钟"
echo "*/30 * * * * $WRAPPER --job social_media >> $LOG 2>&1"
echo ""
echo "# SEC 13F 季度报告: 每周一 08:00"
echo "0 8 * * 1 $WRAPPER --job sec_13f >> $LOG 2>&1"
echo ""
echo "# 每日汇总推送: 每天 21:00"
echo "0 21 * * * $WRAPPER --job daily_digest >> $LOG 2>&1"
echo ""
echo ""
echo "💡 也可以直接复制到剪贴板并运行 'crontab -e' 粘贴:"

# 自动安装到 crontab
read -p "是否自动安装到 crontab? [y/N] " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    # 先备份当前 crontab
    crontab -l 2>/dev/null > /tmp/current_crontab || true

    # 检查是否已安装
    if grep -q "Guru Tracker" /tmp/current_crontab 2>/dev/null; then
        echo "⚠️  Guru Tracker cron 任务已存在，跳过安装"
        echo "   如需更新，请手动运行 'crontab -e'"
    else
        cat >> /tmp/current_crontab << EOF

# Guru Tracker — 投资大佬跟单系统
0 7,20 * * 1-5 $WRAPPER --job ark_trades >> $LOG 2>&1
0 */6 * * * $WRAPPER --job sec_form4 >> $LOG 2>&1
0 */12 * * * $WRAPPER --job congress >> $LOG 2>&1
*/30 * * * * $WRAPPER --job social_media >> $LOG 2>&1
0 8 * * 1 $WRAPPER --job sec_13f >> $LOG 2>&1
0 21 * * * $WRAPPER --job daily_digest >> $LOG 2>&1
EOF
        crontab /tmp/current_crontab
        echo "✅ cron 任务安装成功！"
        echo "   查看任务: crontab -l"
        echo "   查看日志: tail -f $LOG"
    fi
fi

echo ""
echo "🧪 测试运行 ARK 采集器:"
echo "   cd $SCRIPT_DIR && bash run_job.sh --job ark_trades"
