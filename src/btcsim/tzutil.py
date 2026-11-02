"""タイムゾーン解決。Windows など tzdata が無い環境でも動くようにする。

JST には夏時間が無いため、ZoneInfo が使えない場合は固定オフセットに
フォールバックしても結果は厳密に一致する。
"""
from __future__ import annotations

from datetime import timedelta, timezone, tzinfo

_FIXED = {
    "Asia/Tokyo": 9,
    "UTC": 0,
}


def get_tz(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:                       # noqa: BLE001 - tzdata 不在など
        if name in _FIXED:
            return timezone(timedelta(hours=_FIXED[name]), name)
        raise
