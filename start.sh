#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  Mimir Metadata AI — Launcher
#  รัน script นี้เพื่อเปิดแอป (หลังจาก install.sh แล้ว)
# ─────────────────────────────────────────────────────────────
cd "$(dirname "$0")"

# ตรวจว่าติดตั้งแล้วหรือยัง
if [ ! -f ".venv/bin/uvicorn" ]; then
  echo "⚠️  ยังไม่ได้ติดตั้ง — กำลังรัน install.sh..."
  bash install.sh
fi

PORT=8000

# ตรวจว่า port ว่างไหม ถ้าไม่ว่างใช้ port ถัดไป
while lsof -iTCP:$PORT -sTCP:LISTEN &>/dev/null; do
  PORT=$((PORT + 1))
done

URL="http://localhost:$PORT"

echo ""
echo "  🚀  กำลังเริ่ม Mimir Metadata AI…"
echo "  🌐  URL: $URL"
echo "  ⌨️   กด Ctrl+C เพื่อหยุด"
echo ""

# เปิด browser หลังจาก server พร้อม (รอ 2 วิ)
(sleep 2 && open "$URL") &

# รัน server
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port $PORT
