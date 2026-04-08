# Mimir Metadata AI Tool

เครื่องมือ Web App สำหรับใช้ AI วิเคราะห์และเติม Metadata ให้กับไฟล์มีเดียใน **Mimir DAM** (Digital Asset Management) โดยอัตโนมัติ พัฒนาขึ้นเพื่อแทนที่ Google Apps Script เดิมที่ทำงานบน Google Sheets ให้กลายเป็นระบบที่รันได้บนเซิร์ฟเวอร์จริง ควบคุมผ่าน Web UI และ Deploy ด้วย Docker

---

## ภาพรวมการทำงาน

```
Mimir DAM  ──fetch──►  Local DB (SQLite)  ──analyze──►  Gemini Vision AI
                                                               │
                                                         AI Metadata
                                                               │
Mimir DAM  ◄──push───────────────────────────────────────────►
```

กระบวนการทำงานมี 3 ขั้นตอนหลัก:

1. **Fetch** — ดึงรายการไฟล์ทั้งหมดจากโฟลเดอร์ที่ระบุใน Mimir มาเก็บใน Database ภายในเครื่อง
2. **Batch** — ส่ง thumbnail ของแต่ละไฟล์พร้อม context ให้ Gemini Vision วิเคราะห์และสร้าง Metadata
3. **Push** — ส่ง Metadata ที่ AI สร้างไว้กลับขึ้น Mimir

---

## โครงสร้างโปรเจค (MVC)

```
mimir-metadata-tool/
│
├── app/
│   ├── main.py                          # Entry point — สร้าง FastAPI app + ตาราง DB
│   ├── config.py                        # ตั้งค่าทั้งหมดจาก environment variables
│   ├── database.py                      # เชื่อมต่อ SQLite ด้วย SQLAlchemy
│   │
│   ├── models/
│   │   └── asset.py                     # MODEL — โครงสร้างตาราง assets ใน DB
│   │
│   ├── controllers/
│   │   ├── mimir_controller.py          # CONTROLLER — ติดต่อ Mimir API
│   │   └── gemini_controller.py         # CONTROLLER — ติดต่อ Gemini Vision API
│   │
│   └── views/
│       ├── routes.py                    # VIEW — FastAPI routes และ SSE endpoints
│       └── templates/
│           └── index.html               # VIEW — Dashboard หน้าเดียว (Bootstrap 5)
│
├── data/                                # SQLite database ถูกสร้างที่นี่ตอน runtime
├── Dockerfile                           # Production image (multi-stage, non-root user)
├── docker-compose.yml                   # สำหรับ Deploy บน Portainer / Production
├── docker-compose.dev.yml               # สำหรับพัฒนา local (hot reload)
├── requirements.txt                     # Python dependencies
└── .env.example                         # Template สำหรับตั้งค่า environment
```

---

## รายละเอียดแต่ละชั้น

### Model — `app/models/asset.py`

เก็บข้อมูลไฟล์มีเดียแต่ละรายการในตาราง `assets` ของ SQLite โดยแบ่งเป็น 4 กลุ่ม:

| กลุ่มคอลัมน์ | ตัวอย่าง | ที่มา |
|---|---|---|
| **Workflow** | `item_id`, `status`, `error_log` | ระบบจัดการ |
| **จาก Mimir API** | `title`, `item_type`, `width`, `height`, `ingest_path` | ดึงตอน Fetch |
| **จาก AI** | `ai_title`, `ai_description`, `ai_category`, `ai_subcat`, `ai_keyword` | Gemini วิเคราะห์ |
| **Default** | `rights` | กำหนดไว้ว่า `THE STANDARD/All Rights Reserved` |

**Status lifecycle ของแต่ละ asset:**

```
pending  ──►  processing  ──►  done
                  │
                  └──►  error  ──►  (reset)  ──►  pending
```

---

### Controller — `app/controllers/mimir_controller.py`

รับผิดชอบติดต่อ Mimir REST API มีฟังก์ชันหลัก 3 ตัว:

**`extract_folder_id(folder_url)`**
รับ URL หรือ UUID ดิบแล้วดึง Folder ID ออกมา รองรับทุกรูปแบบ:
```
https://apac.mjoll.no/folders/1bff1e1d-4542-47a4-b083-a98adbf1b230  →  1bff1e1d-...
https://apac.mjoll.no/folder/1bff1e1d-...?tab=assets                →  1bff1e1d-...
1bff1e1d-4542-47a4-b083-a98adbf1b230                                →  1bff1e1d-...
```

**`fetch_all_items(folder_id)`**
ดึงไฟล์จาก Mimir ด้วย pagination (ครั้งละ 100 รายการ) วน loop จนครบ แล้ว upsert ลง SQLite เฉพาะรายการที่ยังไม่มีใน DB (ไม่ทับของเดิม) ระหว่างทำงานจะ yield progress dict เพื่อส่งผ่าน SSE ไปแสดงที่หน้า UI

**`push_metadata_to_mimir(item_id)`**
อ่าน AI metadata จาก DB แล้วส่งกลับขึ้น Mimir ด้วย HTTP PATCH

---

### Controller — `app/controllers/gemini_controller.py`

รับผิดชอบวิเคราะห์ภาพด้วย Gemini Vision API

**`run_gemini_batch()`**
วน loop ผ่านทุก asset ที่มี status `pending` ทีละรายการ โดย:

1. ดาวน์โหลด thumbnail URL มาเป็น binary
2. แปลงเป็น Base64
3. สร้าง prompt ที่รวม context จากชื่อไฟล์และ ingest path เพื่อช่วยระบุชื่อคน/สถานที่ที่อาจไม่เห็นในภาพ
4. ส่งให้ Gemini Vision (`gemini-2.0-flash`) วิเคราะห์
5. รับ JSON กลับมา parse แล้วบันทึกลง DB

**Prompt ที่ใช้** ออกแบบให้ Gemini ตอบกลับเป็น JSON เสมอ ประกอบด้วย:

| Field | รูปแบบ |
|---|---|
| `title` | `YYYY.MM.DD_หัวข้อ_ชื่อบุคคลหรือแบรนด์` |
| `description` | อธิบาย 3–5 ประโยค ว่าใคร ทำอะไร ที่ไหน |
| `category` | Photo / Footage / Audio / Graphic / Deliverable |
| `subcat` | Portrait / Event / B-Roll / Drone / BTS / Interview / Press Conference / Protest / Document / Product |
| `keyword` | 5–10 คำ ครอบคลุมคน สถานที่ หัวข้อ และ action |

**Rate limit protection:** หลังวิเคราะห์แต่ละรูปจะ sleep 4.5 วินาที เพื่อไม่เกิน 15 request/นาที ของ Gemini free tier

---

### View — `app/views/routes.py`

FastAPI routes แบ่งเป็น 2 ประเภท:

**HTML Route**
| Method | Path | คำอธิบาย |
|---|---|---|
| GET | `/` | แสดงหน้า Dashboard |

**API Routes**
| Method | Path | คำอธิบาย |
|---|---|---|
| GET | `/api/stats` | สถิติจำนวน asset แยกตาม status |
| GET | `/api/assets` | รายการ asset (pagination + filter by status) |
| GET | `/api/assets/{id}` | รายละเอียด asset เดี่ยว |
| DELETE | `/api/assets` | ลบ asset ทั้งหมดออกจาก DB |
| PATCH | `/api/assets/{id}/reset` | reset status กลับเป็น pending |
| POST | `/api/fetch` | เริ่ม Fetch จาก Mimir (รับ `folder_url`) |
| GET | `/api/fetch/stream` | SSE stream แสดง progress ของ Fetch |
| POST | `/api/batch` | เริ่ม Gemini Batch |
| GET | `/api/batch/stream` | SSE stream แสดง progress ของ Batch |
| POST | `/api/assets/{id}/push` | Push metadata รายการเดียวขึ้น Mimir |
| POST | `/api/push-all` | Push metadata ทุกรายการที่ done ขึ้น Mimir (SSE) |

**Server-Sent Events (SSE):** การ Fetch และ Batch ใช้ SSE ให้ browser รับ progress แบบ real-time โดยไม่ต้อง polling ซ้ำๆ — เบราว์เซอร์เปิด connection ค้างไว้แล้วรอรับข้อความจาก server ทีละ event

---

### View — `app/views/templates/index.html`

Single-page dashboard เขียนด้วย Bootstrap 5 + vanilla JavaScript ไม่มี framework เพิ่มเติม ประกอบด้วย:

- **Folder Input** — วาง URL หรือ UUID ของโฟลเดอร์ Mimir
- **Stats cards** — แสดงจำนวน Total / Pending / Done / Error อัพเดทอัตโนมัติทุก 5 วินาที
- **Action bar** — ปุ่ม Fetch / Run Gemini Batch / Push All พร้อม filter ตาม status
- **Progress bar + Log** — แสดงความคืบหน้าแบบ real-time ผ่าน SSE
- **Assets table** — แสดง thumbnail, ชื่อเดิม, AI title, category, status พร้อมปุ่ม action
- **Detail modal** — เปิดดู metadata ครบ รวมถึง Push / Reset ทีละรายการ

---

## การตั้งค่า Environment Variables

คัดลอก `.env.example` เป็น `.env` แล้วแก้ค่า:

```env
# Mimir
MIMIR_BASE_URL=https://apac.mjoll.no      # URL ของ Mimir server
MIMIR_TOKEN=sakm.xxxx...                   # API Token จาก Mimir

# Gemini
GEMINI_API_KEY=AIzaSy...                   # Google AI Studio API Key
GEMINI_MODEL=gemini-2.0-flash              # โมเดลที่ใช้

# การประมวลผล
ITEMS_PER_PAGE=100                         # ดึงจาก Mimir ครั้งละกี่รายการ
GEMINI_DELAY_MS=4500                       # หน่วง ms ระหว่างแต่ละรูป (ป้องกัน rate limit)
BATCH_SIZE=20                              # จำนวน asset ต่อ batch

# App
APP_PORT=8000                              # Port ที่ expose
```

---

## วิธีรัน

### Local (Python)
```bash
cp .env.example .env        # แก้ token และ key
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p data
uvicorn app.main:app --reload --port 8000
```
เปิด [http://localhost:8000](http://localhost:8000)

### Local (Docker)
```bash
docker compose -f docker-compose.dev.yml up --build
```

### Production (Portainer)
1. เข้า Portainer → **Stacks** → **Add Stack**
2. เลือก **Repository** แล้วกรอก:
   - Repository URL: `https://github.com/narasitk77/mimir-metadata-tool`
   - Compose path: `docker-compose.yml`
3. ตั้งค่า Environment Variables ใน Portainer UI
4. กด **Deploy the stack**

Data จะถูกเก็บใน Docker named volume `mimir_data` ไม่หายเมื่อ redeploy

---

## Docker Architecture

### Production (`Dockerfile`)
ใช้ **multi-stage build** เพื่อลดขนาด image:
- **Stage 1 (builder):** ติดตั้ง Python packages
- **Stage 2 (runtime):** copy เฉพาะ package ที่ build แล้ว ไม่มี build tools
- รัน process ด้วย **non-root user** (`app`) เพื่อความปลอดภัย
- มี **HEALTHCHECK** ตรวจสอบ `/api/stats` ทุก 30 วินาที

### Development (`docker-compose.dev.yml`)
- Mount `./app` เข้า container เพื่อ **hot reload** แก้โค้ดแล้วเห็นผลทันที
- Mount `./data` เพื่อให้เข้าถึง SQLite file ได้โดยตรง
- ใช้คำสั่ง `uvicorn --reload`

---

## Tech Stack

| Layer | เทคโนโลยี | หน้าที่ |
|---|---|---|
| Web Framework | FastAPI | HTTP server, routing, SSE |
| Database | SQLite + SQLAlchemy | เก็บข้อมูล asset และ status |
| HTTP Client | httpx (async) | เรียก Mimir API และ Gemini API |
| Template Engine | Jinja2 | render HTML |
| Frontend | Bootstrap 5 + Vanilla JS | Dashboard UI |
| Container | Docker + Docker Compose | packaging และ deployment |
| AI | Google Gemini Vision | วิเคราะห์ภาพและสร้าง metadata |
