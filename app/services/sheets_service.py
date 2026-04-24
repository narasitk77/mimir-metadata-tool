"""
Google Sheets integration — OAuth2 Web App flow.

Auth flow (automatic callback, no copy-paste):
  1. GET  /api/sheets/auth      → redirect to Google login
  2. User logs in, Google redirects to /api/sheets/callback?code=...
  3. App exchanges code for tokens, saves to /app/data/google_oauth_tokens.json
  4. Redirect back to UI with success message

Tokens auto-refresh; persisted in Docker volume so survive restarts.

Sheet layout:
  Tab "รายงาน"   → one row per batch run (appended, never deleted)
  Tab "by_folder" → overwritten each push with current folder breakdown
  Tab "by_day"    → overwritten each push with per-day breakdown
"""
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_TOKEN_FILE = Path("/app/data/google_oauth_tokens.json")
_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_SUMMARY_HEADERS = [
    "วันที่", "Provider", "Model",
    "Total Assets", "Done", "Pending", "Error", "Folders",
    "Tokens In", "Tokens Out", "Tokens Total",
    "Cost USD", "Cost THB",
    "First Processed", "Last Processed", "Elapsed (min)",
]
_FOLDER_HEADERS = ["Album / Folder", "Total", "Done", "Error", "Tokens Total", "Cost USD", "Cost THB"]
_DAY_HEADERS   = ["Date", "Done", "Tokens", "Cost USD", "Cost THB"]


def _get_flow():
    from google_auth_oauthlib.flow import Flow
    from app.config import settings
    if not settings.GOOGLE_OAUTH_CLIENT_ID or not settings.GOOGLE_OAUTH_CLIENT_SECRET:
        raise ValueError("GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET not set")
    client_config = {
        "web": {
            "client_id":     settings.GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uris": [settings.GOOGLE_REDIRECT_URI],
            "auth_uri":      "https://accounts.google.com/o/oauth2/auth",
            "token_uri":     "https://oauth2.googleapis.com/token",
        }
    }
    return Flow.from_client_config(
        client_config,
        scopes=_SCOPES,
        redirect_uri=settings.GOOGLE_REDIRECT_URI,
    )


def get_auth_url() -> str:
    flow = _get_flow()
    url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    return url


def complete_auth(code: str) -> dict:
    """Exchange auth code for tokens and persist."""
    try:
        flow = _get_flow()
        flow.fetch_token(code=code.strip())
        creds = flow.credentials
        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(json.dumps({
            "token":         creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri":     creds.token_uri,
            "client_id":     creds.client_id,
            "client_secret": creds.client_secret,
            "scopes":        list(creds.scopes) if creds.scopes else _SCOPES,
        }))
        logger.info("Google OAuth tokens saved")
        return {"ok": True}
    except Exception as e:
        logger.error(f"OAuth complete_auth failed: {e}")
        return {"ok": False, "error": str(e)}


def is_connected() -> bool:
    return _TOKEN_FILE.exists()


def _get_creds():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    if not _TOKEN_FILE.exists():
        raise ValueError("Not authenticated — visit /api/sheets/auth first")
    data = json.loads(_TOKEN_FILE.read_text())
    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=data.get("client_id"),
        client_secret=data.get("client_secret"),
        scopes=data.get("scopes", _SCOPES),
    )
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        data["token"] = creds.token
        _TOKEN_FILE.write_text(json.dumps(data))
    return creds


def _get_client():
    import gspread
    return gspread.authorize(_get_creds())


def _ensure_tab(spreadsheet, title: str, headers: list):
    try:
        ws = spreadsheet.worksheet(title)
    except Exception:
        ws = spreadsheet.add_worksheet(title=title, rows=2000, cols=len(headers))
    if not ws.row_values(1) or ws.row_values(1)[0] != headers[0]:
        ws.update("A1", [headers])
        try:
            ws.format("1:1", {"textFormat": {"bold": True}})
        except Exception:
            pass
    return ws


def push_report_to_sheets(report: dict) -> dict:
    from app.config import settings
    try:
        gc = _get_client()
        sh = gc.open_by_key(settings.GOOGLE_SHEET_ID)
    except Exception as e:
        logger.error(f"Sheets connect failed: {e}")
        return {"ok": False, "error": str(e)}

    try:
        s = report.get("summary", {})
        elapsed_min = round(s.get("elapsed_sec", 0) / 60, 1) if s.get("elapsed_sec") else ""

        # Tab: รายงาน — append one row per batch (never deleted)
        ws_s = _ensure_tab(sh, "รายงาน", _SUMMARY_HEADERS)
        ws_s.append_row([
            report.get("generated_at", ""),
            report.get("provider", ""),
            report.get("model", ""),
            s.get("total_assets", 0), s.get("total_done", 0),
            s.get("total_pending", 0), s.get("total_error", 0),
            s.get("total_folders", 0),
            s.get("tokens_in", 0), s.get("tokens_out", 0), s.get("tokens_total", 0),
            s.get("cost_usd", 0), s.get("cost_thb", 0),
            s.get("first_processed", ""), s.get("last_processed", ""),
            elapsed_min,
        ], value_input_option="USER_ENTERED")

        # Tab: by_folder — overwrite with latest data
        ws_f = _ensure_tab(sh, "by_folder", _FOLDER_HEADERS)
        rows = [_FOLDER_HEADERS] + [
            [f.get("folder",""), f.get("total",0), f.get("done",0),
             f.get("error",0), f.get("tokens_total",0), f.get("cost_usd",0), f.get("cost_thb",0)]
            for f in report.get("by_folder", [])
        ]
        if len(rows) > 1:
            ws_f.clear()
            ws_f.update("A1", rows)
            ws_f.format("1:1", {"textFormat": {"bold": True}})

        # Tab: by_day — overwrite with latest data
        ws_d = _ensure_tab(sh, "by_day", _DAY_HEADERS)
        drows = [_DAY_HEADERS] + [
            [d.get("date",""), d.get("done",0), d.get("tokens",0),
             d.get("cost_usd",0), d.get("cost_thb",0)]
            for d in report.get("by_day", [])
        ]
        if len(drows) > 1:
            ws_d.clear()
            ws_d.update("A1", drows)
            ws_d.format("1:1", {"textFormat": {"bold": True}})

        url = f"https://docs.google.com/spreadsheets/d/{settings.GOOGLE_SHEET_ID}"
        logger.info("Report pushed to Sheets OK")
        return {"ok": True, "url": url}

    except Exception as e:
        logger.error(f"Sheets write failed: {e}")
        return {"ok": False, "error": str(e)}
