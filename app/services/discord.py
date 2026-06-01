"""Discord webhook notifications for Mimir Metadata Tool.

Sends a rich embed to a Discord channel after the daily sweep finishes,
summarising what was processed so a human can review and push.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger(__name__)

_TH_TZ = timezone(timedelta(hours=7))  # Asia/Bangkok — UTC+7


async def send_daily_summary(
    webhook_url: str,
    *,
    done: int,
    errors: int,
    new_found: int,
    source: str = "daily_sweep",
    app_url: str = "",
) -> bool:
    """POST a Discord embed summarising the daily sweep result.

    Returns True on success, False on any failure (never raises — notification
    errors must not interrupt the main batch flow).

    Only sends when something actually happened (done > 0 or new_found > 0).
    A sweep with zero work is not reported — that would just be noise.
    """
    if not webhook_url:
        return False
    if done == 0 and new_found == 0:
        logger.info("Discord: nothing to report (done=0, new=0) — skipping")
        return False

    now_th = datetime.now(_TH_TZ).strftime("%d/%m/%Y %H:%M น.")
    color = 0x2ECC71 if errors == 0 else 0xE67E22  # green = clean, orange = has errors

    lines = ["กรุณาตรวจสอบ metadata แล้วกด **Push All** เมื่อพร้อม"]
    if app_url:
        lines.append(f"\n🔗 [เปิดแอป Mimir Metadata]({app_url})")
    description = "\n".join(lines)

    payload = {
        "embeds": [
            {
                "title": "🎬  Mimir Metadata — ประมวลผลเสร็จแล้ว",
                "description": description,
                "color": color,
                "fields": [
                    {"name": "✅  Done",        "value": f"**{done}** รายการ",     "inline": True},
                    {"name": "❌  Error",       "value": f"**{errors}** รายการ",   "inline": True},
                    {"name": "🆕  ไฟล์ใหม่วันนี้", "value": f"**{new_found}** รายการ", "inline": True},
                ],
                "footer": {"text": f"Triggered by: {source}  ·  {now_th}"},
            }
        ]
    }

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(webhook_url, json=payload)
        if r.status_code not in (200, 204):
            logger.warning(
                f"Discord webhook returned HTTP {r.status_code}: {r.text[:200]}"
            )
            return False
        logger.info(f"Discord notification sent (done={done}, errors={errors}, new={new_found})")
        return True
    except Exception as exc:
        logger.warning(f"Discord notification failed: {exc}")
        return False
