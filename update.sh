#!/bin/bash
cd ~/Downloads/PolyWeather

echo "====================================="
echo "🔄 拉取最新代码..."
git pull origin main

echo "🛑 停止旧的服务进程..."
# 1. 杀掉 uvicorn / web app
pkill -f "uvicorn" || true
pkill -f "python.*web\.app" || true
# 2. 清理端口 8001
lsof -t -i:8001 | xargs kill -9 2>/dev/null || true

echo "🚀 启动后端 (Port: 8001)..."
source .env
nohup /opt/anaconda3/bin/uvicorn web.app:app \
  --port 8001 \
  --host 127.0.0.1 \
  --workers 1 \
  > web.log 2>&1 &

# 等待后端就绪
echo "⏳ 等待后端就绪..."
for i in $(seq 1 15); do
  sleep 1
  curl -sf http://127.0.0.1:8001/healthz > /dev/null 2>&1 && break
  echo "    ...等待中 ($i/15)"
done

echo "🌡️  预热扫描终端缓存 (后台运行，首次约 60-120s)..."
curl -s \
  "http://127.0.0.1:8001/api/scan/terminal?force_refresh=true" \
  -H "x-polyweather-entitlement: ${POLYWEATHER_BACKEND_ENTITLEMENT_TOKEN:-dev-local-prewarm-token}" \
  > /dev/null 2>&1 &
PREWARM_PID=$!
echo "   预热 PID: $PREWARM_PID (后台运行，不阻塞启动)"

echo ""
echo "✅ 后端已启动，扫描终端预热中..."
echo "   日志: web.log"
echo "   预热完成后，后续请求将直接命中缓存 (毫秒级响应)"
echo "====================================="
