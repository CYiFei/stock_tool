#!/usr/bin/env bash
# 启动股息率追踪服务并打开浏览器
cd "$(dirname "$0")"

# 尝试后台打开浏览器（失败也无妨）
(sleep 1.2; xdg-open http://127.0.0.1:5000 >/dev/null 2>&1) &

python3 app.py
