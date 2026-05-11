#!/bin/bash
# PolyWeather single-command launcher
# Usage: bash ~/Downloads/PolyWeather/start.sh
#
# Runs both backend and frontend in this terminal session.
# Backend:  http://127.0.0.1:8001
# Frontend: http://localhost:3002

set -e
cd ~/Downloads/PolyWeather

echo "====================================="
echo "🚀 Starting PolyWeather..."

# Kill any existing processes
pkill -f "uvicorn" 2>/dev/null || true
lsof -t -i:8001 2>/dev/null | xargs kill -9 2>/dev/null || true

# Start backend
echo "📡 Starting backend (port 8001)..."
source .env
nohup /opt/anaconda3/bin/uvicorn web.app:app \
  --port 8001 \
  --host 127.0.0.1 \
  --workers 4 \
  > web.log 2>&1 &

# Wait for backend healthz
echo "⏳ Waiting for backend..."
for i in $(seq 1 20); do
  sleep 1
  curl -sf http://127.0.0.1:8001/healthz > /dev/null 2>&1 && echo "   Backend ready ($i s)" && break
  echo "   ...waiting ($i/20)"
done

# Fire background prewarm
echo "🌡️  Warming scan terminal cache (runs in background)..."
curl -s \
  "http://127.0.0.1:8001/api/scan/terminal?force_refresh=true" \
  -H "x-polyweather-entitlement: ${POLYWEATHER_BACKEND_ENTITLEMENT_TOKEN:-dev-local-prewarm-token}" \
  > /dev/null 2>&1 &

# Start frontend
echo "🖥️  Starting frontend (port 3002)..."
cd ~/Downloads/PolyWeather/frontend
nohup ./node_modules/.bin/next dev -p 3002 > /tmp/next.log 2>&1 &

# Wait for frontend
echo "⏳ Waiting for frontend..."
for i in $(seq 1 30); do
  sleep 1
  curl -sf http://127.0.0.1:3002/ > /dev/null 2>&1 && echo "   Frontend ready!" && break
  echo "   ...waiting ($i/30)"
done

echo ""
echo "====================================="
echo "✅ PolyWeather is running!"
echo "   Dashboard: http://localhost:3002"
echo "   Backend:   http://127.0.0.1:8001"
echo "   Logs:      tail -f ~/Downloads/PolyWeather/web.log"
echo "   Next logs: tail -f /tmp/next.log"
echo "====================================="
echo "Press Ctrl+C to stop all services."
echo ""

# Cleanup function
cleanup() {
  echo ""
  echo "🛑 Stopping PolyWeather..."
  pkill -f "uvicorn" 2>/dev/null || true
  pkill -f "next dev" 2>/dev/null || true
  echo "✅ Stopped."
  exit 0
}
trap cleanup SIGINT SIGTERM

# Keep script alive
wait
