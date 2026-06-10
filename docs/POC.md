# Mimir Metadata AI Tool — Proof of Concept

> **"AI ทำ metadata แทนคน — ช่างภาพโฟกัสการถ่ายภาพ ทีมโฟกัส content"**
> The Standard × Gemini 2.5 Flash · มิถุนายน 2026
> ผู้รับผิดชอบ: ทีม Digital Product (narasit.k@thestandard.co)

---

# Part A — ฉบับย่อ

*สำหรับผู้บริหารและทีมที่ไม่ technical — อ่านจบใน 3 นาที*

## ปัญหา

ภาพข่าวใน Mimir DAM กว่า **10,000 รูปไม่มี metadata** — ค้นหาไม่เจอ ใช้ซ้ำไม่ได้ เพราะ:

| ปัญหา | รายละเอียด |
|---|---|
| **ช้า** | กรอกมือ 3–5 นาที/รูป (title, คำอธิบาย, keywords, ชื่อบุคคล, สถานที่) |
| **ไม่สม่ำเสมอ** | แต่ละคนกรอกไม่เหมือนกัน — ค้นหาใน DAM ได้ไม่เต็มประสิทธิภาพ |
| **Backlog สะสม** | ไม่มีใครมีเวลาย้อนกลับไปกรอกของเก่า |

## โซลูชัน — ทำงานอัตโนมัติ 4 ขั้น

```
Mimir DAM ──> ① Auto-Fetch ──> ② Gemini AI ──> ③ คนตรวจ ──> ④ Push กลับ Mimir
              ทุก 15 นาที       วิเคราะห์ภาพ      ผ่าน Discord     (คนกดเอง)
                                สร้าง 18 ฟิลด์     แจ้งเตือน
```

1. **Auto-Fetch** — ระบบดึงภาพใหม่จาก Mimir เองทุก 15 นาที + กวาดตกค้างทุกเช้า 09:00 น.
2. **Gemini AI วิเคราะห์** — สร้าง metadata ภาษาไทย 18 ฟิลด์/รูป (title, คำอธิบาย, ชื่อบุคคล, สถานที่, keywords ฯลฯ) โดยอ่านภาพจริงประกอบข่าวจาก Google News อัตโนมัติ
3. **คนตรวจ** — Discord แจ้งเตือนเมื่อเสร็จ ทีมเข้ามารีวิว/แก้ไขใน dashboard
4. **Push** — คนกดส่งกลับ Mimir เอง (**AI ไม่มีสิทธิ์เขียนทับ DAM โดยตรง — by design**)

## ผลจริงจากการทดสอบ (ไม่ใช่ตัวเลขประมาณการ)

ทดสอบกับ folder จริง `PHOTOGRAPHER` (เลือกตั้ง ก.พ. 2026) — กรองเฉพาะภาพ Hires อัตโนมัติ ตัดโฟลเดอร์ FB/LOGO ทิ้ง:

| ตัวชี้วัด | ผล |
|---|---|
| ภาพเข้าคิว | **1,147 รูป** (กรองจาก 10,000 รายการใน folder) |
| ประมวลผลแล้ว | 23 รูป (กำลังทยอยทำ — free tier จำกัด 500 รูป/วัน) |
| ความเร็ว | **~7 วินาที/รูป** (เทียบมือ 3–5 นาที = เร็วกว่า ~26 เท่า) |
| ต้นทุนที่ใช้ไปจริง | **฿1.49** (23 รูป ≈ ฿0.06/รูป) |
| ความแม่นยำที่เห็น | ระบุ "อนุทิน ชาญวีรกูล" + งานปราศรัย + สถานที่ ถูกต้องจากภาพจริง |

**ตัวอย่างผลจริง 1 รูป** (`B011_6Feb.jpg`):
- **Title:** `2026.02.06_ภูมิใจไทย ปราศรัยใหญ่_อนุทิน ชาญวีรกูล`
- **คำอธิบาย:** อนุทิน ชาญวีรกูล หัวหน้าพรรคภูมิใจไทย กำลังปราศรัยและชูกำปั้นขึ้นฟ้าบนเวที
- **บุคคล:** อนุทิน ชาญวีรกูล · **หมวด:** Politics/Event · **อารมณ์:** Celebratory

## ต้นทุน — เทียบชัด ๆ ที่ 1,147 รูป

| | ทำมือ | AI (Gemini) |
|---|---|---|
| เวลา/รูป | 3–5 นาที | ~7 วินาที |
| ต้นทุน/รูป | ~฿10 (editor ฿150/ชม.) | **฿0.06** |
| รวม 1,147 รูป | **~฿11,470** + แรงงาน 57–95 ชม. | **~฿69** |
| ประหยัด | — | **~99%** |

## สถานะ & ขั้นต่อไป

- ✅ ระบบ deploy แล้ว ทำงานอัตโนมัติทุกวัน — โค้ดอยู่บน GitHub
- 🔄 กำลังประมวลผล 1,124 รูปที่เหลือ (~3 วันบน free tier)
- 📋 **ตัดสินใจที่ต้องการ:** อัปเกรด Gemini paid tier (500 → 10,000 รูป/วัน) จะทำให้ backlog ทั้งหมดจบใน **วันเดียว** — ต้นทุนยังอยู่ที่ ~฿0.06/รูปเท่าเดิม

---

# Part B — ฉบับละเอียด

*สำหรับทีม technical*

## B1. สถาปัตยกรรมรวม

**Stack:** Python FastAPI · SQLAlchemy (SQLite, รองรับ Postgres) · APScheduler · Gemini 2.5 Flash API · Mimir REST API (Cognito SRP auth) · Discord Webhook · Google SSO

```
                        ┌─────────────────────────────────────────────┐
                        │                FastAPI App                   │
                        │                                              │
 ┌──────────┐  fetch    │  ┌────────────┐      ┌──────────────────┐   │
 │  Mimir   │◄──────────┼──┤  Scheduler  │      │   Web Dashboard  │   │
 │   DAM    │           │  │ ·poll 15min │      │  (SSO-gated UI)  │   │
 │ (mjoll)  │  push     │  │ ·sweep 09:00│      └────────┬─────────┘   │
 └──────────┘◄──────────┼──┐└─────┬──────┘               │             │
                        │  │      ▼                      ▼             │
                        │  │  ┌──────────────────────────────────┐    │
 ┌──────────┐           │  │  │     SQLite (6 ตาราง)             │    │
 │  Gemini  │◄──────────┼──┼──┤ assets · watch_folders ·         │    │
 │ 2.5 Flash│  analyze  │  │  │ audit_log · usage_history ·      │    │
 └──────────┘           │  │  │ mimir_options · persons          │    │
                        │  │  └──────────────────────────────────┘    │
 ┌──────────┐  notify   │  │                                          │
 │ Discord  │◄──────────┼──┘  (push = มนุษย์กดเท่านั้น)               │
 └──────────┘           └─────────────────────────────────────────────┘
```

**Asset lifecycle:** `pending → processing → done → (push) ` หรือ `→ error` (มี auto-recovery กลับเป็น pending)

## B2. การเชื่อมต่อ Mimir DAM

### Authentication — 2 โหมด
- **Cognito SRP** (โหมดหลัก): login ด้วย username/password ผ่าน `pycognito` → ได้ id_token อายุ 60 นาที → cache และ **refresh อัตโนมัติทุก 55 นาที** — credentials เก็บใน memory เท่านั้น ไม่ลง disk/DB/log
- **Static token** (fallback): ตั้ง `MIMIR_TOKEN` ใน env — ใช้สำหรับทดสอบ (หมดอายุ ~1 ชม.)
- ทุก request ใช้ header `x-mimir-cognito-id-token: Bearer <token>`

### Fetch — ดึงภาพเข้าคิว
- เพจทีละ 100 รายการผ่าน `GET /api/v1/search` (`includeSubfolders=true`)
- **Skip filter อัตโนมัติ:** ไฟล์ขยะจากการ์ดกล้อง (THMBNL, Sub-clips, XMETA, Proxies) และ sidecar (.xml .xmp .lut ฯลฯ)
- **Subfolder filter** (เลือกได้ 4 โหมด): `all` / `no_fb` / `hires_only` / `hires_no_fb` — ตัดสินจาก parent folder ใน ingest path; รู้จักชื่อ hires หลายแบบ (hires, hi-res, high res…) และ sub-type ที่ต้องข้าม (LOGO, FB, RAW, Proxies, social, web…) — **ภาพที่อยู่ตรง ๆ ใน event folder ยังถูกเก็บ** (ไม่หลุดเพราะช่างภาพไม่ได้สร้างโฟลเดอร์ Hires)
- **Upsert ปลอดภัย:** รายการที่มีอยู่แล้วไม่ถูกแตะ (แค่ backfill proxy_url ถ้าว่าง) — ดึงซ้ำกี่รอบก็ไม่ duplicate

### Push — ส่ง metadata กลับ พร้อมระบบเรียนรู้
จุดที่ฉลาดที่สุดของระบบ: **Mimir ไม่มี API บอกว่า dropdown แต่ละ field รับค่าอะไรบ้าง** ระบบจึงเรียนรู้เอง:

1. ค่าที่ AI สร้าง → กรองผ่าน cache (`mimir_options`): ค่าที่เคยถูกปฏิเสธ (`bad`) ไม่ส่งซ้ำ
2. POST ไป Mimir — ถ้าโดน HTTP 400 → regex หาว่าค่าไหนผิด → ตัดเฉพาะค่านั้น บันทึกเป็น `bad` → retry
3. พอ 200 → ทุกค่าที่ส่งสำเร็จบันทึกเป็น `ok` (พร้อม accept_count)
4. รอบถัดไป ค่า `ok` ยังถูกป้อนกลับเข้า prompt ให้ AI เลือกใช้คำที่ Mimir รับแน่นอน (self-learning vocabulary)

## B3. AI Pipeline — วิเคราะห์ 1 รูปทำอะไรบ้าง

### แหล่ง context 6 ชั้น (เรียงตามลำดับความน่าเชื่อถือใน prompt)
1. **หลักฐานจากภาพ** — สิ่งที่เห็นจริงเท่านั้น (กฎเหล็ก: ห้ามใส่ชื่อคนถ้าไม่เห็นหน้าชัด)
2. **Cross-asset context** — ภาพในงานเดียวกันแชร์ข้อมูลกัน (ชื่อคน = union, สถานที่/ชื่องาน = ค่าล่าสุด) → ภาพหลัง ๆ ของ event แม่นขึ้นเรื่อย ๆ
3. **Google News อัตโนมัติ** — ค้น RSS ไทยด้วยชื่องาน + วันที่จาก path, ดึง 6 หัวข้อข่าว + เนื้อหาเต็ม 2 บทความแรก (cache ต่อ event — ค้นครั้งเดียวใช้ทั้งงาน)
4. **บทความที่ทีมแนบ** — สูงสุด 5 URLs
5. **โน้ตจากผู้ใช้** — free text ต่อ folder/รูป
6. **People Directory face-ID** — เทียบใบหน้ากับรูป reference ของบุคคลในระบบก่อนวิเคราะห์หลัก (confirm แล้วใส่ชื่อได้เลย — ความเชื่อถือสูงสุด)

### เสริมอัตโนมัติ
- **EXIF:** photographer/กล้อง/credit/ISO/f-stop/shutter ดึงจาก Mimir exifTagsUrl — เติมเฉพาะช่องที่ว่าง
- **GPS → สถานที่:** แปลง DMS เป็นพิกัด → reverse-geocode ผ่าน OSM Nominatim (ภาษาไทย)
- **วิดีโอ:** วิเคราะห์จาก thumbnail frame + technical metadata (duration/fps/codec) — **คง filename เดิมเป็น title** (ไม่เขียนทับ)

### Output: 18 ฟิลด์ JSON
title (format `YYYY.MM.DD_เรื่อง_บุคคล`) · description · category/subcat · editorial_categories · location · persons · event_occasion · emotion_mood · language · department · project_series · right_license · deliverable_type · subject_tags · technical_tags · visual_attributes · episode_segment · keywords (5–10)

### Rate limit & ความทนทาน
| กลไก | พฤติกรรม |
|---|---|
| RPM pacing | หน่วง 6 วิ/รูป = 10 req/นาที (พอดี free tier cap) |
| Daily guard | หยุดเองที่ 90% ของ 500 req/วัน หรือ 1M tokens/วัน — รอ reset เที่ยงคืน UTC |
| 429 (quota) | รีเซ็ตรูปนั้นกลับ pending → นับถอยหลัง 90 วิ → ลองรูปเดิมใหม่ (ไม่นับเป็น error) |
| 503 (Gemini ล่ม) | retry 3 ครั้ง backoff 15s/30s |
| Crash กลางคัน | รูปค้าง `processing` ถูกรีเซ็ตเป็น `pending` ตอน start + ก่อน batch ทุกรอบ |
| ยกเลิกกลางคัน | cancel flag เช็คระหว่างรูป — กดหยุดได้จาก UI ทันที |

## B4. Automation

### 2 jobs (APScheduler)
| Job | กำหนดเวลา | ทำอะไร |
|---|---|---|
| **Poll** | ทุก 15 นาที (+ ทันทีตอน start) | ดึง watch folders ทุกตัวที่ enabled → ถ้า **มีของใหม่ OR มี pending ค้าง** → auto-batch |
| **Daily Sweep** | 02:00 UTC = **09:00 น. ไทย** | poll เต็มรอบ + batch ซ้ำรอบสอง (เผื่อรอบแรกโดน lock จาก manual batch) → ส่ง Discord |

> เงื่อนไข `OR pending ค้าง` คือ bug fix สำคัญ — เวอร์ชันแรก batch ทำงานเฉพาะเมื่อมีไฟล์ใหม่ ทำให้รูปที่ค้างจาก rate limit ไม่ถูก retry จนกว่าจะมีไฟล์ใหม่เข้ามา

### กลไกความทนทาน
- **Self-heal watchdog:** ทุกครั้งที่ UI poll `/api/automation/status` ระบบเช็คว่า scheduler ยังมีชีวิต — ตายเมื่อไหร่ restart เอง + ลง audit log
- **Heartbeat health:** unhealthy เมื่อ heartbeat ขาดเกิน 2×interval+60s (กัน false alarm ตอนเครื่องโหลดหนัก)
- **Kill switch:** pause ทั้งระบบได้ปุ่มเดียว (ถือว่า healthy เพราะเป็นเจตนาผู้ใช้)
- **Cost warn:** แจ้งเตือนครั้งเดียว/วันเมื่อค่าใช้จ่ายเกิน $5/วัน (ปรับได้) — **เตือนอย่างเดียว ไม่หยุดระบบ**
- **Concurrency guard:** `max_instances=1` + lock ร่วมกับ manual batch — ไม่มีทาง batch ซ้อนกัน

### Discord Notification
- ยิงหลัง daily sweep เมื่อ **มีงานเสร็จหรือมีไฟล์ใหม่เท่านั้น** (sweep ว่าง = เงียบ ไม่ spam)
- Embed ภาษาไทย: ✅ Done / ❌ Error / 🆕 ไฟล์ใหม่ + ลิงก์เปิดแอป + เวลาไทย — เขียว = ไม่มี error, ส้ม = มี
- ส่งพลาด = log warning เฉย ๆ **ไม่มีทางทำ batch พัง** (double-guarded)

## B5. ความปลอดภัย & Human-in-the-loop

| ชั้น | กลไก |
|---|---|
| **ไม่มี auto-push** | automation จบที่ `done` — เขียนกลับ DAM ต้องมีคนกด Push เสมอ (เจตนาออกแบบ ระบุใน docstring) |
| **SSO gate** | ทุกหน้า (ยกเว้น health/login) ต้อง login Google ด้วยบัญชี `@thestandard.co` — โดเมน **hardcode ในโค้ด** จงใจไม่ให้ config ผิดแล้วหลุด |
| **Audit log** | ทุก action ลงตาราง append-only พร้อม user email (จาก SSO session ผ่าน contextvar — ไม่ปนกันแม้ request พร้อมกัน) / `__scheduler__` สำหรับงานอัตโนมัติ |
| **Usage history** | แยกตารางจาก assets — กด Clear DB ประวัติต้นทุน/ผลงาน **ไม่หาย** (บันทึก snapshot ก่อนลบ) |
| **Credentials** | Mimir password อยู่ใน memory เท่านั้น · Gemini key อยู่ใน `.env` (git-ignored) |

## B6. Data Model (6 ตาราง)

| ตาราง | บทบาท |
|---|---|
| `assets` | 1 แถว/รูป — สถานะ, metadata จาก Mimir, ai_* 18 ฟิลด์, exif_* 7 ฟิลด์, token ที่ใช้ |
| `watch_folders` | folder ที่ scheduler เฝ้า (enabled flag, last_polled, last_error) |
| `audit_log` | append-only ทุก action — ใคร ทำอะไร เมื่อไหร่ ผลเป็นไง |
| `usage_history` | ต้นทุน/ผลงานรายเหตุการณ์ — snapshot ราคา ณ เวลานั้น, อยู่รอด Clear DB |
| `mimir_options` | คลังคำที่ Mimir รับ/ปฏิเสธ (self-learning, unique ต่อ field+value) |
| `persons` | People Directory — ชื่อ, ตำแหน่ง, keywords, รูป reference (base64 ใน DB) |

Migration แบบ additive-only (ALTER TABLE + try/except) — ไม่ใช้ Alembic, รัน startup ทุกครั้ง idempotent, ไม่มีทางทำข้อมูลเดิมพัง

## B7. API หลัก

```
POST /api/fetch                    ดึง folder (รับหลาย URL + context)
POST /api/batch                    เริ่ม AI batch  ·  /api/batch/stream (SSE progress)
POST /api/push-all                 push ทุกรูป done (3 ขนาน — Mimir รับได้)
POST /api/watch-folders            เพิ่ม folder เฝ้าอัตโนมัติ
POST /api/automation/run-now       สั่ง poll ทันที
POST /api/automation/sweep-now     สั่ง daily sweep ทันที
POST /api/automation/test-discord  ทดสอบ webhook
GET  /api/health                   DB + Mimir + Gemini quota + scheduler (ละเอียด)
GET  /api/report · /api/usage/summary · /api/audit-log   รายงาน/ตรวจสอบ + export CSV
```

## B8. Config สำคัญ (.env)

```bash
# Mimir — เลือกอย่างใดอย่างหนึ่ง
MIMIR_COGNITO_USER_POOL_ID= MIMIR_COGNITO_CLIENT_ID=   # SRP (แนะนำ — refresh เอง)
MIMIR_USERNAME= MIMIR_PASSWORD=
MIMIR_TOKEN=                                            # หรือ static (ทดสอบ)

GEMINI_API_KEY=  GEMINI_MODEL=gemini-2.5-flash
GEMINI_DELAY_MS=6000          # 10 req/นาที พอดี free tier

AUTOMATION_POLL_INTERVAL_MINUTES=15
AUTOMATION_DAILY_HOUR=2       # UTC → 09:00 ไทย
AUTOMATION_DAILY_WARN_USD=5.0

DISCORD_WEBHOOK_URL=          # ว่าง = ปิดแจ้งเตือน
APP_BASE_URL=                 # ลิงก์ใน Discord — ⚠️ ต้องอัปเดตเมื่อ IP เครื่องเปลี่ยน

FREE_TIER_RPD=500  FREE_TIER_TPD=1000000  FREE_TIER_WARN_PCT=0.9

GOOGLE_AUTH_CLIENT_ID/SECRET/REDIRECT_URI + SESSION_SECRET_KEY   # ครบ 4 = SSO on
```

## B9. ข้อจำกัดที่เจอจริงระหว่าง POC + วิธีแก้

| เหตุการณ์ | ผลกระทบ | การแก้/บทเรียน |
|---|---|---|
| Gemini API key หมดอายุ | batch หยุดหลังรูปแรก | สร้าง key ใหม่ — ควรตั้ง billing alert ฝั่ง Google Cloud |
| เครื่องสลับ WiFi กลางbatch | 50 รูป error (DNS) | error เป็น transient ทั้งหมด → reset เป็น pending แล้ว retry สำเร็จ — production ควรอยู่บนเครื่อง/เซิร์ฟเวอร์ network นิ่ง |
| IP เครื่องเปลี่ยน | ลิงก์ใน Discord เข้าไม่ได้ | แก้ APP_BASE_URL — ระยะยาวควรใช้ static IP หรือ domain จริง |
| Free tier 500 req/วัน | 1,147 รูปต้องรอ ~3 วัน | ระบบจัดการเองด้วย daily sweep — หรืออัปเกรด paid tier จบในวันเดียว |
| โฟลเดอร์ Mimir ตั้งชื่อไม่มาตรฐาน (`็HIRES` typo, ไม่มี Hires) | ภาพเกือบหลุดจาก filter | filter ออกแบบให้ tolerant: รับ typo ผ่าน path-segment matching + เก็บภาพที่อยู่ตรง ๆ ใน event folder |

## B10. Roadmap

| ระยะ | รายการ |
|---|---|
| **ตอนนี้** | ประมวลผล backlog 1,124 รูปให้จบ (อัตโนมัติ ~3 วัน) → ทีมรีวิว → Push |
| **ถัดไป** | อัปเกรด Gemini paid tier (10,000 req/วัน · ต้นทุนเท่าเดิม/รูป) · เพิ่ม watch folders ครอบคลุมช่างภาพทุกคน · ย้ายขึ้นเซิร์ฟเวอร์ถาวร + domain |
| **อนาคต** | เปิดใช้ person verification รอบสอง (โค้ดมีแล้ว ปิดอยู่) · ขยาย People Directory · รองรับวิดีโอเต็มรูปแบบ (multi-frame) · Auto-push แบบมีเงื่อนไขหลัง human approve ครั้งแรกของ event |

---

*เอกสารนี้สร้างจากการอ่านโค้ดจริงทั้งระบบ (24 ไฟล์) + ตัวเลขจริงจาก database ณ วันที่ 2 มิ.ย. 2026*
