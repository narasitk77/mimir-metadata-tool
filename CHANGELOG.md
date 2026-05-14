# Changelog

บันทึกการเปลี่ยนแปลงทุกครั้งของ Mimir Metadata AI Tool
รูปแบบ: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) · [Semantic Versioning](https://semver.org/spec/v2.0.0.html)

---

## [Unreleased]
สิ่งที่กำลังจะมา / ในใจ:
- Auto-discovery un-cached values สำหรับ Mimir option learning
- Google SSO กลับ (ต้องตัดสินใจ deployment ก่อน — Render หรือ subdomain)

---

## 2026-05-14
### Added
- **Audit log ทุก action** — บันทึก `fetch_start`, `batch_start`, `push`, `reset`, `clear_db`, `mimir_options_reset` ลง DB
  - GET `/api/audit-log?action=push&status=error` — ดูประวัติทุก action
  - บันทึก HTTP status code + Mimir response body excerpt บนทุก push
- **Self-learning Mimir option cache** — ระบบเรียนรู้ค่า UUID field ที่ Mimir ยอมรับ
  - Filter AI output ผ่าน cache ก่อน push → ไม่ส่งค่าที่ Mimir reject ซ้ำๆ
  - GET/DELETE `/api/mimir-options` — ดู/รีเซ็ต cache
  - Self-correcting: cache โตขึ้นเองจากการใช้งานจริง
- **SSE auto-reconnect** บน batch stream — 5 retries หลัง Render free spin-down
- **ETA + rate display** ใน batch progress (เช่น `ETA ~4 นาที · 8.7/min`)
- **Parallel Push All** — เร็วขึ้น 3 เท่า (จาก sequential → semaphore 3 concurrent)

### Fixed
- ราคา Gemini ผิด ($0.15/$0.60 → $0.30/$2.50 ตาม [Google pricing](https://ai.google.dev/gemini-api/docs/pricing))
- Pricing เป็น dynamic ตาม `GEMINI_MODEL` (รองรับ Flash, Flash-Lite, Pro)
- Push retry **drop เฉพาะ field ที่ผิด** ไม่ใช่ทั้งหมด (root cause ทำให้ AI metadata ไม่เข้า Mimir UI)
- 5 latent bugs: `provider` NameError, push-by-album filter, race condition, EXIF token, stale watchdog
- `/auth/me` survives missing SessionMiddleware

### Changed
- Gemini delay 7s → 6s (ตรงเส้น 10 RPM free tier)
- `_batch_running` flag คงไว้ระหว่าง SSE reconnect (ไม่ flip กลับ idle ก่อนเวลา)

---

## 2026-05-13
### Removed (BIG refactor — ลบ 1,200+ LOC)
- **Qdrant vector search** — ไม่เคยใช้ใน UI ลบทั้ง service + 5 routes + startup indexer (image ลดลง ~1.5 GB)
- **Google Sheets export** — frontend ไม่เคย wire ลบทั้ง service + 4 routes
- **Google SSO + admin/whitelist** — ก่อนใช้ Caddy/nip.io มีปัญหา DNS rebinding ลบ middleware + templates + AllowedUser model
- **Debug endpoints** (`/api/debug/mimir-*`) — ใช้ตอน reverse engineer UUID เสร็จแล้ว
- **Saved-report JSON list/get/CSV** — frontend ไม่เคยเรียก
- **Caddy reverse proxy** — สลับเป็น plain HTTP บน port 8765 สำหรับ LAN test
- **Dead code**: `_extract_event` duplicate, `_ensure_person_table`, unused imports

### Changed
- `main.py` 165 → 40 บรรทัด (ลบ 3 middleware chains)
- `routes.py` 1927 → 1423 บรรทัด

---

## 2026-05-11
### Removed
- **Claude/Anthropic provider** — ตัด `AI_PROVIDER` switch, Gemini-only (ลบ 690 บรรทัด)

### Added
- HTTPS via **Caddy reverse proxy** (self-signed local CA)
- Caddy on-demand TLS รับ any hostname/IP

### Fixed
- Caddyfile config naming (rename to force Portainer recreate)

---

## 2026-05-07
### Added
- **Google SSO gate** จำกัด `@thestandard.co` only
- Login portal page + admin console สำหรับ whitelist
- `CanonicalHostMiddleware` redirect ไป OAuth canonical host
- Public `/healthz` endpoint (Docker healthcheck)
- Dashboard auto-redirect 401 → login page

### Fixed
- Hard-enforce `@thestandard.co` domain บน auth paths (whitelist ห้าม override domain)
- Settings ignore unrelated env vars (ป้องกัน startup crash)
- Compose env passthroughs (admin emails, whitelist, sheets)
- Dockerfile healthcheck `/api/stats` → `/healthz` (auth-gated path fail)
- Auto-clear stuck batch flag เมื่อ stream ไม่ connect
- pull_policy: always ใน compose (กัน `:latest` cache stale)

---

## 2026-04-27
### Added
- **Google SSO gate** initial — จำกัด `@thestandard.co` accounts

---

## 2026-04-24 (Production setup day)
### Added
- **Postgres** เก็บข้อมูลถาวร (default แทน SQLite)
- **Cognito SRP auth** สำหรับ Mimir (auto-refresh token ทุก 55 นาที)
- Runtime Mimir login (20-min session, no persistence)
- GitHub Actions build → GHCR image
- Docker Compose สำหรับ Portainer (web editor)
- Default port `8765` (avoid common host conflict)
- Mimir session กลับมาคงเดิม — logout = end

### Fixed
- Dockerfile use Debian adduser/groupadd
- Remove opencv apt deps (libgl1, libglib2.0-0)

---

## 2026-04-20
### Added
- **People Directory** — เก็บข้อมูลบุคคลในระบบ
- **CSV export** สำหรับ report
- Render/Fly.io deployment config

---

## 2026-04-11
### Added
- **Multi-folder fetch** — fetch จากหลาย folder พร้อมกัน
- **Claude (Anthropic)** เป็น AI provider ทางเลือก (ภายหลังตัดออก)
- Auto-retry on rate limit
- Auto-refresh expired thumbnail URLs
- Startup reset stuck "processing" assets
- BW UI redesign (theme)

---

## 2026-04-10
### Added
- Full Mimir field support (`default_*` keys)
- EXIF auto-fill (photographer, camera, credit)
- Bulk edit UI

---

## 2026-04-08 (Day 1 — initial build)
### Added
- ✨ **Mimir Metadata AI Tool** — first release
- FastAPI + SQLAlchemy + Bootstrap dashboard
- Gemini Vision AI สำหรับ analyze ภาพ (`gemini-2.5-flash`)
- Mimir API client (fetch/push)
- Token usage tracking + cost calculation
- Free-tier guard + daily usage limit
- Multi-language UI (TH/EN)
- รับ Mimir Folder URL หรือ UUID ดิบ
- Modal สำหรับแก้ AI metadata ก่อน push
- Live progress bar (SSE)
- Support `PORT` env (Railway/Render deploy)

---

## รูปแบบเขียนรายการใหม่

แต่ละวันที่ deploy ใหม่ ให้เพิ่ม section ที่ด้านบน:

```markdown
## YYYY-MM-DD
### Added
- ฟีเจอร์ใหม่ที่เพิ่ม
### Changed
- เปลี่ยนพฤติกรรมของของเดิม
### Fixed
- bug ที่แก้
### Removed
- สิ่งที่ลบทิ้ง
```

อ้างอิง commit hash หรือ link issue ถ้าจำเป็น เพื่อย้อนดูบริบทได้
