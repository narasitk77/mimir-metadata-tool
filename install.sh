#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  Mimir Metadata AI — One-time installer for macOS
#  รัน script นี้ครั้งแรกครั้งเดียว เพื่อติดตั้ง dependencies
# ─────────────────────────────────────────────────────────────
set -e
cd "$(dirname "$0")"

echo ""
echo "═══════════════════════════════════════════════"
echo "  Mimir Metadata AI — Installer"
echo "═══════════════════════════════════════════════"
echo ""

# 1. ตรวจ Python 3
if ! command -v python3 &>/dev/null; then
  echo "❌  ไม่พบ Python 3 — กรุณาติดตั้งจาก https://www.python.org/downloads/"
  exit 1
fi
PY=$(python3 --version 2>&1)
echo "✔  Python: $PY"

# 2. สร้าง virtual environment
if [ ! -d ".venv" ]; then
  echo "→  กำลังสร้าง virtual environment…"
  python3 -m venv .venv
fi
echo "✔  Virtual environment พร้อมแล้ว"

# 3. ติดตั้ง packages
echo "→  กำลังติดตั้ง dependencies (อาจใช้เวลา 1-2 นาที)…"
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt -q
echo "✔  Dependencies ติดตั้งแล้ว"

# 4. สร้าง .env ถ้ายังไม่มี
if [ ! -f ".env" ]; then
  echo ""
  echo "─────────────────────────────────────────────"
  echo "  ตั้งค่า API Keys (กด Enter เพื่อข้ามก่อนได้)"
  echo "─────────────────────────────────────────────"
  read -r -p "  MIMIR_TOKEN (Bearer token จาก Mimir): " MIMIR_TOKEN
  read -r -p "  ANTHROPIC_API_KEY (Claude key): " ANTHROPIC_KEY
  read -r -p "  GEMINI_API_KEY (ถ้าใช้ Gemini แทน): " GEMINI_KEY

  cat > .env <<EOF
MIMIR_BASE_URL=https://apac.mjoll.no
MIMIR_TOKEN=${MIMIR_TOKEN}
ANTHROPIC_API_KEY=${ANTHROPIC_KEY}
GEMINI_API_KEY=${GEMINI_KEY}
AI_PROVIDER=claude
ANTHROPIC_MODEL=claude-haiku-4-5-20251001
GEMINI_MODEL=gemini-2.5-flash
EOF
  echo "✔  สร้างไฟล์ .env แล้ว"
else
  echo "✔  ไฟล์ .env มีอยู่แล้ว"
fi

# 5. สร้าง data folder
mkdir -p data
echo "✔  โฟลเดอร์ data/ พร้อมแล้ว"

echo ""
echo "═══════════════════════════════════════════════"
echo "  ✅  ติดตั้งเสร็จสมบูรณ์!"
echo ""
echo "  รัน app ด้วย:  ./start.sh"
echo "  หรือดับเบิลคลิก:  MimirMetadataAI.app"
echo "═══════════════════════════════════════════════"
echo ""
