import argparse
import asyncio
import base64
import json
import os
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright


DEV_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = DEV_DIR.parents[2] if len(DEV_DIR.parents) > 2 else DEV_DIR
BACKEND_DIR = DEV_DIR / "backend"
STATE_PATH = DEV_DIR / ".skool_state.json"
REPORTS_DIR = DEV_DIR / "reports"

DEFAULT_SKOOL_URL = "https://www.skool.com/the-blooming-spot-7187/classroom/1e1a36df?md=fa3575496c7142c4b16d2dbd0f6d8720"
DEFAULT_QUIZ_URL = "https://rbt-slides-studio.netlify.app/quiz?section=A.8&autostart=true"
DEFAULT_CONFIG_PATH = DEV_DIR / "scripts" / "skool_quiz_lessons.json"


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def materialize_storage_state_from_env() -> None:
    if STATE_PATH.exists():
        return

    raw_state = os.environ.get("SKOOL_STORAGE_STATE_JSON", "").strip()
    if not raw_state:
        encoded_state = os.environ.get("SKOOL_STORAGE_STATE_B64", "").strip()
        if encoded_state:
            raw_state = base64.b64decode(encoded_state).decode("utf-8")

    if not raw_state:
        return

    parsed = json.loads(raw_state)
    STATE_PATH.write_text(json.dumps(parsed), encoding="utf-8")


def build_lesson_text(code: str, quiz_url: str) -> str:
    return (
        "Codigo de acceso para esta quiz\n\n"
        "Para abrir esta quiz, copia este codigo y pegalo cuando la pagina te lo pida.\n\n"
        f"Codigo: {code}\n\n"
        "Link de la quiz:\n"
        f"{quiz_url}"
    )


def quiz_url_for_scope(scope: str) -> str:
    return f"https://rbt-slides-studio.netlify.app/quiz?section={scope}&autostart=true"


def code_pattern(code: str) -> str:
    if not re.fullmatch(r"[A-Z0-9]+-[A-Z0-9]+-\d{6}", code):
        raise ValueError(f"Unexpected quiz access code format: {code}")
    return re.escape(code[:-6]) + r"\d{6}"


def resolve_code(scope: str, *, allow_minute_rotation: bool = False) -> str:
    if not allow_minute_rotation:
        os.environ.pop("QUIZ_ACCESS_ROTATION_MINUTES", None)

    sys.path.insert(0, str(BACKEND_DIR))
    from quiz_access import generated_quiz_access_code

    generated = generated_quiz_access_code(scope)
    if generated:
        return generated

    static_code = os.environ.get("QUIZ_ACCESS_CODE", "").strip()
    if static_code:
        return static_code

    raise RuntimeError("No quiz access code available. Set QUIZ_ACCESS_SECRET or QUIZ_ACCESS_CODE.")


def load_lesson_config(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise RuntimeError(f"Skool quiz lesson config not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    entries = raw.get("lessons", raw) if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise RuntimeError("Skool quiz lesson config must be a list or an object with a lessons list.")

    normalized: list[dict[str, str]] = []
    for index, item in enumerate(entries, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"Invalid lesson config item #{index}: expected object.")
        scope = str(item.get("scope", "")).strip().upper()
        skool_url = str(item.get("skool_url", "")).strip()
        quiz_url = str(item.get("quiz_url") or quiz_url_for_scope(scope)).strip()
        if not scope or not skool_url:
            raise RuntimeError(f"Invalid lesson config item #{index}: scope and skool_url are required.")
        normalized.append({"scope": scope, "skool_url": skool_url, "quiz_url": quiz_url})
    return normalized


async def replace_template(editor, page, lesson_text: str) -> None:
    await editor.fill(lesson_text)
    await page.evaluate(
        """() => {
            const target = document.activeElement;
            if (target) target.dispatchEvent(new InputEvent('input', { bubbles: true }));
        }""",
    )
    await page.wait_for_timeout(500)


async def replace_code_only(editor, page, code: str) -> None:
    pattern = code_pattern(code)
    changed = await editor.evaluate(
        """(root, args) => {
            const regex = new RegExp(args.pattern, 'i');
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode())) {
                if (regex.test(node.nodeValue || '')) {
                    node.nodeValue = node.nodeValue.replace(regex, args.code);
                    root.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: args.code }));
                    return true;
                }
            }
            return false;
        }""",
        {"pattern": pattern, "code": code},
    )
    if not changed:
        raise RuntimeError(
            f"Could not find an existing code matching {code[:-6]}###### in the Skool lesson. "
            "Set the template once manually or run with --replace-template."
        )
    await page.wait_for_timeout(500)


async def publish_to_skool(
    skool_url: str,
    lesson_text: str,
    code: str,
    *,
    headless: bool,
    dry_run: bool,
    replace_template_mode: bool,
    replace_template_if_missing: bool,
) -> Path:
    if not STATE_PATH.exists():
        raise RuntimeError(f"Skool storage state not found: {STATE_PATH}")

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    screenshot_path = REPORTS_DIR / "skool_quiz_code_publish.png"

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(storage_state=str(STATE_PATH), viewport={"width": 1440, "height": 1000})
        page = await context.new_page()
        await page.goto(skool_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        if "/login" in page.url:
            raise RuntimeError("Skool session is expired. Run scripts/_skool_login_wait.py first.")

        edit_button = page.locator("button").last
        await edit_button.click()
        await page.wait_for_timeout(1000)

        editor = page.locator('[contenteditable="true"]').last
        await editor.wait_for(state="visible", timeout=10000)
        if replace_template_mode:
            await replace_template(editor, page, lesson_text)
        else:
            try:
                await replace_code_only(editor, page, code)
            except RuntimeError:
                if not replace_template_if_missing:
                    raise
                await replace_template(editor, page, lesson_text)
        await page.wait_for_timeout(500)

        if not dry_run:
            save_button = page.get_by_role("button", name="SAVE")
            await save_button.wait_for(state="visible", timeout=10000)
            if await save_button.is_enabled():
                await save_button.click()
                await page.wait_for_timeout(2500)
            else:
                await page.wait_for_timeout(500)

        await page.screenshot(path=str(screenshot_path), full_page=False)
        await browser.close()

    return screenshot_path


async def main() -> None:
    parser = argparse.ArgumentParser(description="Publish the active Bloom quiz access code into a Skool lesson.")
    parser.add_argument("--scope", default="A.8", help="Quiz scope used to generate the code, e.g. A.8")
    parser.add_argument("--skool-url", default=DEFAULT_SKOOL_URL)
    parser.add_argument("--quiz-url", default=DEFAULT_QUIZ_URL)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="JSON config for --all mode.")
    parser.add_argument("--all", action="store_true", help="Publish/update all configured Skool quiz lessons.")
    parser.add_argument("--headed", action="store_true", help="Show browser window")
    parser.add_argument("--dry-run", action="store_true", help="Open editor and screenshot without saving")
    parser.add_argument(
        "--replace-template",
        action="store_true",
        help="Replace the whole Skool lesson body. Use only for first-time setup; default updates only the 6 digits.",
    )
    parser.add_argument(
        "--replace-template-if-missing",
        action="store_true",
        help="Update only the 6 digits when possible; create the template only if no matching code exists yet.",
    )
    parser.add_argument(
        "--allow-minute-rotation-test",
        action="store_true",
        help="Allow QUIZ_ACCESS_ROTATION_MINUTES for short manual tests. Never use for production publishing.",
    )
    args = parser.parse_args()

    load_env_file(ROOT_DIR / ".env")
    load_env_file(DEV_DIR / ".env")
    materialize_storage_state_from_env()

    entries = load_lesson_config(Path(args.config)) if args.all else [
        {
            "scope": args.scope.strip().upper(),
            "skool_url": args.skool_url,
            "quiz_url": args.quiz_url,
        }
    ]

    for entry in entries:
        code = resolve_code(entry["scope"], allow_minute_rotation=args.allow_minute_rotation_test)
        lesson_text = build_lesson_text(code, entry["quiz_url"])
        screenshot_path = await publish_to_skool(
            entry["skool_url"],
            lesson_text,
            code,
            headless=not args.headed,
            dry_run=args.dry_run,
            replace_template_mode=args.replace_template,
            replace_template_if_missing=args.replace_template_if_missing,
        )
        print(f"scope={entry['scope']} published_code={code}")
        if args.replace_template:
            mode = "replace_template"
        elif args.replace_template_if_missing:
            mode = "replace_digits_or_template_if_missing"
        else:
            mode = "replace_digits_only"
        print(f"mode={mode}")
        print(f"screenshot={screenshot_path}")


if __name__ == "__main__":
    asyncio.run(main())
