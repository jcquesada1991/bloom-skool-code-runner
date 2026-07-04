import hashlib
import hmac
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


TRUE_VALUES = {"1", "true", "yes", "on"}
BOOTSTRAP_SECRET = ""


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in TRUE_VALUES


def _is_render_production() -> bool:
    public_base_url = os.environ.get("PUBLIC_BASE_URL", "").lower()
    return "onrender.com" in public_base_url or any(key.startswith("RENDER_") for key in os.environ)


def _secret() -> str:
    return os.environ.get("QUIZ_ACCESS_SECRET", "").strip() or BOOTSTRAP_SECRET


def _rotation_hours() -> int:
    try:
        return max(1, int(os.environ.get("QUIZ_ACCESS_ROTATION_HOURS", "24")))
    except ValueError:
        return 24


def _rotation_minutes() -> int:
    if _is_render_production():
        return _rotation_hours() * 60

    value = os.environ.get("QUIZ_ACCESS_ROTATION_MINUTES", "").strip()
    if value:
        try:
            return max(1, int(value))
        except ValueError:
            pass
    return _rotation_hours() * 60


def _anchor_hour() -> int:
    try:
        return min(23, max(0, int(os.environ.get("QUIZ_ACCESS_ROTATION_ANCHOR_HOUR", "7"))))
    except ValueError:
        return 7


def _grace_minutes() -> int:
    try:
        return max(0, int(os.environ.get("QUIZ_ACCESS_GRACE_MINUTES", "0")))
    except ValueError:
        return 0


def _timezone() -> ZoneInfo:
    return ZoneInfo(os.environ.get("QUIZ_ACCESS_TIMEZONE", "America/New_York"))


def normalize_scope(scope: str | None) -> str:
    return (scope or "GLOBAL").strip().upper()


def _scope_slug(scope: str | None) -> str:
    return normalize_scope(scope).replace(".", "").replace(" ", "")


def quiz_access_required() -> bool:
    if not _env_bool("QUIZ_ACCESS_REQUIRED", default=_is_render_production()):
        return False
    return bool(os.environ.get("QUIZ_ACCESS_CODE", "").strip() or _secret())


def current_window_start(now: datetime | None = None, offset_windows: int = 0) -> datetime:
    tz = _timezone()
    current = now.astimezone(tz) if now else datetime.now(tz)
    anchor = current.replace(hour=_anchor_hour(), minute=0, second=0, microsecond=0)
    if current < anchor:
        anchor -= timedelta(days=1)
    minutes_since_anchor = int((current - anchor).total_seconds() // 60)
    rotation_minutes = _rotation_minutes()
    window_index = minutes_since_anchor // rotation_minutes
    return anchor + timedelta(minutes=(window_index + offset_windows) * rotation_minutes)


def generated_quiz_access_code(scope: str | None, now: datetime | None = None, offset_windows: int = 0) -> str | None:
    secret = _secret()
    if not secret:
        return None
    window = current_window_start(now=now, offset_windows=offset_windows)
    normalized_scope = normalize_scope(scope)
    payload = f"{normalized_scope}|{window.isoformat()}"
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    number = int(digest[:12], 16) % 1_000_000
    prefix = os.environ.get("QUIZ_ACCESS_CODE_PREFIX", "BLOOM").strip().upper() or "BLOOM"
    return f"{prefix}-{_scope_slug(scope)}-{number:06d}"


def valid_quiz_access_codes(scope: str | None, now: datetime | None = None) -> set[str]:
    codes: set[str] = set()
    static_code = os.environ.get("QUIZ_ACCESS_CODE", "").strip()
    if static_code:
        codes.add(static_code.upper())

    grace_windows = 0
    try:
        grace_windows = max(0, int(os.environ.get("QUIZ_ACCESS_GRACE_WINDOWS", "0")))
    except ValueError:
        grace_windows = 0

    for offset in range(-grace_windows, 1):
        generated = generated_quiz_access_code(scope, now=now, offset_windows=offset)
        if generated:
            codes.add(generated.upper())

    grace_minutes = _grace_minutes()
    if grace_minutes:
        tz = _timezone()
        current = now.astimezone(tz) if now else datetime.now(tz)
        window_start = current_window_start(now=current)
        if current < window_start + timedelta(minutes=grace_minutes):
            previous = generated_quiz_access_code(scope, now=current, offset_windows=-1)
            if previous:
                codes.add(previous.upper())
    return codes


def is_valid_quiz_access_code(code: str | None, scope: str | None, now: datetime | None = None) -> bool:
    if not quiz_access_required():
        return True
    provided = (code or "").strip().upper()
    if not provided:
        return False
    return any(hmac.compare_digest(provided, valid_code) for valid_code in valid_quiz_access_codes(scope, now=now))
