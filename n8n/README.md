# n8n — External Scheduler สำหรับ Mimir Metadata Tool

n8n ทำหน้าที่ **สั่งงาน + แจ้งเตือน** แทน APScheduler ภายในแอป
Logic ทั้งหมด (fetch / Gemini / push) ยังอยู่ในแอป Python — n8n แค่เรียก API

## สถาปัตยกรรม

```
n8n (cron)──POST /api/automation/run-now?wait=false──>  ┌──────────────┐
n8n (09:00)──POST /api/automation/sweep-now?wait=false─>│  Mimir Tool  │
n8n (loop)──GET  /api/automation/status ───────────────>│   (FastAPI)  │
     │                                                   └──────────────┘
     └──> Discord webhook (เมื่อ sweep เสร็จ + มีงานให้รีวิว)
```

ทุก request ใช้ header `X-API-Key` (ผ่าน SSO gate โดยไม่ต้อง login Google)

## การติดตั้ง

### 1. ตั้งค่า .env (ระดับ stack)

```bash
AUTOMATION_SCHEDULER_ENABLED=false        # ปิด scheduler ในแอป — n8n เป็นเจ้าของ schedule
AUTOMATION_API_KEY=<สุ่ม 32+ ตัวอักษร>     # openssl rand -hex 24
DISCORD_WEBHOOK_URL=                       # ⚠️ ต้องว่าง! ไม่งั้นแจ้งเตือนซ้ำ 2 ทาง
DISCORD_WEBHOOK_URL_N8N=https://discord.com/api/webhooks/...   # n8n ใช้ตัวนี้
APP_BASE_URL=https://mimir-tool.thestandard.co                  # ลิงก์ในข้อความ
```

### 2. เปิด n8n

```bash
docker compose up -d n8n
# เปิด http://<host>:5678 → สร้าง owner account ครั้งแรก
```

### 3. Import workflows

ใน n8n UI: **Workflows → Import from File** เลือกทั้ง 2 ไฟล์:

| ไฟล์ | ทำอะไร |
|---|---|
| `workflow-poll-15min.json` | ทุก 15 นาที → trigger poll (async) — เงียบๆ ไม่แจ้งเตือน |
| `workflow-daily-sweep.json` | 09:00 น. → trigger sweep → วน loop เช็คสถานะทุก 5 นาทีจนเสร็จ → Discord |
| `workflow-chat-agent.json` | **"มีมี่"** — AI agent คุยภาษาไทย สั่งงาน/ถามสถานะระบบผ่านแชท (ดูหัวข้อด้านล่าง) |

จากนั้น **Activate** ทั้งสอง workflow (สวิตช์มุมขวาบน)

> ค่า URL / API key / webhook ถูกอ่านจาก env ของ container (`MIMIR_APP_URL`,
> `MIMIR_API_KEY`, `MIMIR_DISCORD_WEBHOOK`, `MIMIR_PUBLIC_URL`) — ตั้งครั้งเดียวใน
> docker-compose ไม่ต้องแก้ใน workflow

### 4. ทดสอบ

```bash
# bypass ทำงาน?
curl -s -X POST "http://localhost:8000/api/automation/run-now?wait=false" \
     -H "X-API-Key: $AUTOMATION_API_KEY"          # → 202 {"started": true}
curl -s -X POST "http://localhost:8000/api/automation/run-now?wait=false"  # → 401

# แอปอยู่ในโหมด n8n?
curl -s "http://localhost:8000/api/automation/status" -H "X-API-Key: $AUTOMATION_API_KEY" \
  | python3 -m json.tool | grep -E "mode|healthy|sweep"
# → "mode": "n8n", "healthy": true
```

แล้วกด **Execute Workflow** ใน daily-sweep ด้วยมือ 1 ครั้ง → ควรเห็นข้อความใน Discord

## Rollback กลับโหมดเดิม (ไม่ต้องแตะโค้ด)

```bash
AUTOMATION_SCHEDULER_ENABLED=true
DISCORD_WEBHOOK_URL=<webhook เดิม>
# แล้ว restart แอป + Deactivate workflows ใน n8n
```

## "มีมี่" — Chat Agent (สั่งงานด้วยภาษาคน)

`workflow-chat-agent.json` คือ AI agent (Gemini เป็นสมอง) ที่คุยภาษาไทยได้เหมือนคนๆ นึง
มี 9 tools เรียก API ของแอป — ตัวอย่างที่คุยได้:

- "สถานะตอนนี้เป็นไง" / "เหลือกี่รูป" → รายงานคิว + ค่าใช้จ่ายวันนี้
- "ไล่งานเลย" / "sweep เลย" → สั่ง sweep ทันที (async)
- "ดึงรูปใหม่หน่อย" → trigger poll
- "หยุดก่อน" / "ทำต่อ" → pause / resume ทั้งระบบ
- "ใช้เงินไปเท่าไหร่แล้ว" → ต้นทุนสะสม + quota วันนี้
- "เฝ้า folder นี้ให้หน่อย <URL>" → เพิ่ม watch folder
- การ Push → มีมี่จะชี้ไปที่ dashboard (human-in-the-loop ยังบังคับ)

### เปิดใช้
1. Import `workflow-chat-agent.json`
2. เปิด node **Gemini** → สร้าง credential "Google Gemini (PaLM) API" → วาง `GEMINI_API_KEY` เดิม
3. Activate → กดปุ่ม **Chat** ใน editor เพื่อคุยทดสอบ หรือใช้ **public chat URL**
   (Chat Trigger ตั้ง `public: true` แล้ว — n8n จะโชว์ URL หน้าแชทพร้อมแชร์ให้ทีม)

### ช่องทางเพิ่มเติม (ต่อยอด)
| ช่องทาง | วิธี |
|---|---|
| n8n Webchat | ได้ทันทีหลัง activate (ตามด้านบน) |
| Claude (MCP) | เชื่อม n8n MCP แล้วสั่ง execute_workflow แบบ chat ได้จากแชท Claude เลย |
| Discord bot | สร้าง Discord Application + bot token → เปลี่ยน trigger เป็น Discord Trigger |
| LINE OA | LINE Messaging API webhook → ชี้เข้า Webhook trigger + ตอบกลับผ่าน Reply API |

## หมายเหตุ dev (รันแอปนอก Docker)

แอป local รันที่ port 8765 — ตั้ง `MIMIR_APP_URL=http://host.docker.internal:8765`
ใน environment ของ n8n service แทน
