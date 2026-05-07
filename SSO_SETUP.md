# Google SSO Setup — จำกัดเข้าใช้เฉพาะ @thestandard.co

ระบบ SSO gate ปกป้องทุก path (ยกเว้น `/auth/*` และ `/static/*`) — ถ้ายังไม่ login จะ redirect ไปหน้า Google OAuth และยอมรับเฉพาะอีเมล `@thestandard.co` เท่านั้น

## สิ่งที่ต้องเตรียม

1. Google Cloud Console เข้าด้วยบัญชี `@thestandard.co` (Workspace admin หรือคนที่มีสิทธิ์สร้าง OAuth app)
2. URL ของเครื่องที่ deploy เช่น `http://192.168.21.220:8765`

## ขั้นตอน

### 1. สร้าง OAuth 2.0 Client ID

1. ไปที่ https://console.cloud.google.com/apis/credentials
2. เลือก project (หรือสร้างใหม่ เช่น "mimir-metadata-tool")
3. **OAuth consent screen** → User Type: **Internal** (เฉพาะ Workspace `@thestandard.co`) → กรอก:
   - App name: `Mimir Metadata Tool`
   - User support email: ของตัวเอง
   - Developer contact: ของตัวเอง
4. **Credentials → CREATE CREDENTIALS → OAuth client ID**
   - Application type: **Web application**
   - Name: `Mimir Metadata Tool`
   - **Authorized redirect URIs** เพิ่ม:
     ```
     http://192.168.21.220:8765/auth/callback
     ```
     (URL ต้องตรงกับ `GOOGLE_AUTH_REDIRECT_URI` เป๊ะ — รวม trailing slash)
5. กด CREATE → คัดลอก **Client ID** กับ **Client secret**

> **⚠️ หมายเหตุเรื่อง redirect URI:** Google OAuth ปกติ**ไม่อนุญาต** `http://` กับ public IP (อนุญาตเฉพาะ `localhost`)
> แต่ **OAuth Consent type "Internal"** บน Google Workspace อนุญาต private IP / hostname ได้
> ถ้าเจอ error `redirect_uri_mismatch` หรือ `invalid_request` → ลอง:
> - ใช้ hostname แทน IP (เช่น `mimir.thestandard.co` ชี้ไป private IP ผ่าน internal DNS)
> - หรือ deploy หลัง reverse proxy ที่มี HTTPS (Caddy, Traefik, Nginx + Let's Encrypt)

### 2. ตั้ง env vars บน Portainer

ในหน้า Stack → Environment variables เพิ่ม:

```env
GOOGLE_AUTH_CLIENT_ID=xxxxxxxxxxxx-yyyy.apps.googleusercontent.com
GOOGLE_AUTH_CLIENT_SECRET=GOCSPX-xxxxxxxxxxxxxxxx
GOOGLE_AUTH_REDIRECT_URI=http://192.168.21.220:8765/auth/callback
ALLOWED_EMAIL_DOMAIN=thestandard.co
SESSION_SECRET_KEY=<random-32-chars>
```

สุ่ม `SESSION_SECRET_KEY`:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

### 3. Redeploy stack

หลังตั้ง env vars แล้วกด **Update the stack** ใน Portainer → container restart

เปิด `http://192.168.21.220:8765/` → ต้องเด้งไปหน้า Google login → login ด้วย `@thestandard.co` → กลับมาหน้าแอป

## ตรวจว่า SSO ทำงานหรือยัง

```bash
curl http://192.168.21.220:8765/api/diag
```
ดู field `"google_sso_configured": true` ถ้าเป็น `false` แปลว่า env vars ยังไม่ครบ

## ปัญหาที่พบบ่อย

| อาการ | สาเหตุ |
|------|-------|
| เข้าหน้าแรกได้เลยไม่ต้อง login | env ไม่ครบ → `is_configured()` คืน False → middleware ปล่อยผ่าน |
| `redirect_uri_mismatch` | URI ใน Google Console ไม่ตรงกับ `GOOGLE_AUTH_REDIRECT_URI` (case-sensitive รวม port) |
| `Invalid auth state` | session cookie ไม่ persist — เช็คว่าตั้ง `SESSION_SECRET_KEY` |
| `Access denied — ไม่ใช่บัญชี @thestandard.co` | login ผิด account — กดลิงก์ "ลองใหม่ด้วยอีเมลอื่น" |
