import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo


APP_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = APP_DIR / "backend"
PUBLIC_QUIZ_API = os.environ.get(
    "PUBLIC_QUIZ_GENERATE_URL",
    "https://rbt-slides-studio.netlify.app/api/quiz/generate",
)

last_run = {
    "started_at": None,
    "finished_at": None,
    "ok": None,
    "message": "not run yet",
}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def log(message: str) -> None:
    print(f"[{datetime.now(ZoneInfo('America/New_York')).isoformat()}] {message}", flush=True)


def should_run_on_start() -> bool:
    return os.environ.get("RUN_ON_START", "true").strip().lower() in {"1", "true", "yes", "on"}


def scheduler_timezone() -> ZoneInfo:
    return ZoneInfo(os.environ.get("SKOOL_CODE_TIMEZONE", "America/New_York"))


def next_run_at(now: datetime | None = None) -> datetime:
    tz = scheduler_timezone()
    current = now.astimezone(tz) if now else datetime.now(tz)
    hour = min(23, max(0, env_int("SKOOL_CODE_RUN_HOUR", 5)))
    minute = min(59, max(0, env_int("SKOOL_CODE_RUN_MINUTE", 5)))
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if current >= target:
        target += timedelta(days=1)
    return target


def active_code(scope: str) -> str:
    sys.path.insert(0, str(BACKEND_DIR))
    from quiz_access import generated_quiz_access_code

    code = generated_quiz_access_code(scope)
    if not code:
        raise RuntimeError(f"No active quiz access code generated for {scope}.")
    return code


def verify_scope(scope: str) -> None:
    code = active_code(scope)
    body = json.dumps(
        {
            "subsection_id": scope,
            "num_questions": 10,
            "mode": "study",
            "access_code": code,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        PUBLIC_QUIZ_API,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            if response.status != 200:
                raise RuntimeError(f"{scope} API verify returned HTTP {response.status}.")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{scope} API verify returned HTTP {exc.code}: {detail}") from exc


def run_publish_cycle(reason: str) -> None:
    last_run["started_at"] = datetime.now(scheduler_timezone()).isoformat()
    last_run["finished_at"] = None
    last_run["ok"] = None
    last_run["message"] = f"running: {reason}"
    log(f"START publish cycle ({reason})")

    command = [sys.executable, "scripts/skool_publish_quiz_code.py", "--all"]
    completed = subprocess.run(command, cwd=APP_DIR, text=True, capture_output=True)
    if completed.stdout:
        print(completed.stdout, flush=True)
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, flush=True)
    if completed.returncode != 0:
        raise RuntimeError(f"Skool publisher failed with exit code {completed.returncode}.")

    verify_scope(os.environ.get("SKOOL_VERIFY_SCOPE", "A.8"))
    last_run["ok"] = True
    last_run["message"] = "publish and API verify ok"
    last_run["finished_at"] = datetime.now(scheduler_timezone()).isoformat()
    log("DONE publish cycle")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in {"/", "/health"}:
            self.send_response(404)
            self.end_headers()
            return

        payload = json.dumps(
            {
                "status": "ok" if last_run["ok"] is not False else "error",
                "last_run": last_run,
                "next_run_at": next_run_at().isoformat(),
            }
        ).encode("utf-8")
        self.send_response(200 if last_run["ok"] is not False else 500)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:
        return


def start_health_server() -> None:
    port = env_int("PORT", 8080)
    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log(f"health server listening on port {port}")


def main() -> None:
    start_health_server()
    if should_run_on_start():
        try:
            run_publish_cycle("startup")
        except Exception as exc:
            last_run["ok"] = False
            last_run["message"] = str(exc)
            last_run["finished_at"] = datetime.now(scheduler_timezone()).isoformat()
            log(f"ERROR startup publish cycle: {exc}")

    while True:
        target = next_run_at()
        seconds = max(1, int((target - datetime.now(scheduler_timezone())).total_seconds()))
        log(f"next publish cycle scheduled for {target.isoformat()}")
        while seconds > 0:
            nap = min(seconds, 300)
            time.sleep(nap)
            seconds -= nap

        try:
            run_publish_cycle("scheduled")
        except Exception as exc:
            last_run["ok"] = False
            last_run["message"] = str(exc)
            last_run["finished_at"] = datetime.now(scheduler_timezone()).isoformat()
            log(f"ERROR scheduled publish cycle: {exc}")


if __name__ == "__main__":
    main()
