#!/usr/bin/env python3
"""vseries → central API sync worker. Runs once per invocation (driven by systemd timer)."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests

# -------- Config (resolved lazily inside main()) --------
UP2_UPLOAD_URL = "https://up2.in.th/api/v1/upload"
EP_PATTERN = re.compile(r"_EP(\d+)\.mp4$", re.IGNORECASE)
COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp")
SUMMARY_SUFFIX = "summary.txt"
HTTP_TIMEOUT = 60

# Populated in main()
VSERIES_ROOT: Path = Path("/storage/vseries")
STATE_FILE: Path = Path("/var/lib/vseries-sync/state.json")
URL_PREFIX: str = "/vseries"
UP2_API_KEY: str = ""
CENTRAL_API_BASE: str = ""
CENTRAL_API_KEY: str = ""
TG_BOT_TOKEN: str = ""
TG_CHAT_ID: str = ""


def load_config() -> None:
    """Read environment into module globals. Raises if any required var is missing."""
    global VSERIES_ROOT, STATE_FILE, URL_PREFIX
    global UP2_API_KEY, CENTRAL_API_BASE, CENTRAL_API_KEY
    global TG_BOT_TOKEN, TG_CHAT_ID

    required = ["UP2_API_KEY", "CENTRAL_API_KEY", "TG_BOT_TOKEN", "TG_CHAT_ID"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "missing required environment variables: " + ", ".join(missing) +
            "\nload config.env first, e.g.:\n"
            "  set -a; source /opt/vseries-sync/config.env; set +a"
        )

    VSERIES_ROOT = Path(os.environ.get("VSERIES_ROOT", "/storage/vseries"))
    STATE_FILE = Path(os.environ.get("STATE_FILE", "/var/lib/vseries-sync/state.json"))
    URL_PREFIX = os.environ.get("URL_PREFIX", "/vseries")
    UP2_API_KEY = os.environ["UP2_API_KEY"]
    CENTRAL_API_BASE = os.environ.get(
        "CENTRAL_API_BASE", "https://vseries.api-movie.com/api/v1"
    ).rstrip("/")
    CENTRAL_API_KEY = os.environ["CENTRAL_API_KEY"]
    TG_BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
    TG_CHAT_ID = os.environ["TG_CHAT_ID"]


# -------- State persistence --------
def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log(f"state file unreadable, starting fresh: {STATE_FILE}")
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


# -------- Folder inspection --------
def folder_signature(folder: Path) -> str:
    """Hash of (name, size) for all files in the folder. Detects new EPs and re-encodes."""
    items = []
    for f in sorted(folder.iterdir(), key=lambda p: p.name):
        try:
            items.append(f"{f.name}:{f.stat().st_size}")
        except OSError:
            continue
    return hashlib.sha256("\n".join(items).encode("utf-8")).hexdigest()


def find_cover(folder: Path) -> Optional[Path]:
    """Find cover image. Strict match: `_cover.<ext>` suffix or exact `cover.<ext>` filename."""
    candidates = []
    for f in folder.iterdir():
        if not f.is_file():
            continue
        low = f.name.lower()
        if not low.endswith(COVER_EXTS):
            continue
        stem = os.path.splitext(low)[0]
        if stem == "cover" or stem.endswith("_cover"):
            candidates.append(f)
    if not candidates:
        return None
    # prefer "_cover.jpg" suffix
    for c in candidates:
        if c.name.lower().endswith("_cover.jpg"):
            return c
    return sorted(candidates, key=lambda p: p.name)[0]


def find_summary(folder: Path) -> Optional[Path]:
    matches = [
        f for f in folder.iterdir()
        if f.is_file() and f.name.lower().endswith(SUMMARY_SUFFIX)
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda p: p.name)[0]


def find_episodes(folder: Path) -> list[tuple[int, str, str]]:
    """Return [(ep_num, name, url), ...] sorted ascending by ep number."""
    eps: list[tuple[int, str, str]] = []
    for f in folder.iterdir():
        if not f.is_file():
            continue
        m = EP_PATTERN.search(f.name)
        if not m:
            continue
        num = int(m.group(1))
        url = f"{URL_PREFIX}/{folder.name}/{f.name}"
        eps.append((num, f"EP{num}", url))
    eps.sort(key=lambda x: x[0])
    return eps


# -------- HTTP helpers --------
def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def upload_cover(cover_path: Path) -> str:
    with open(cover_path, "rb") as fh:
        r = requests.post(
            UP2_UPLOAD_URL,
            headers={"Authorization": f"Bearer {UP2_API_KEY}"},
            files={"file": (cover_path.name, fh)},
            timeout=HTTP_TIMEOUT,
        )
    if not r.ok:
        raise RuntimeError(f"up2 upload {r.status_code}: {r.text[:300]}")
    try:
        body = r.json()
    except ValueError:
        raise RuntimeError(f"up2 returned non-JSON: {r.text[:300]}")
    data = body.get("data") or {}
    urls = data.get("urls") or {}
    direct = urls.get("direct")
    if not direct:
        raise RuntimeError(f"up2 response missing data.urls.direct: {body}")
    return direct


def central_request(method: str, path: str, json_body: Optional[dict] = None) -> dict:
    url = f"{CENTRAL_API_BASE}{path}"
    r = requests.request(
        method,
        url,
        headers={
            "Authorization": f"Bearer {CENTRAL_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json=json_body,
        timeout=HTTP_TIMEOUT,
    )
    if not r.ok:
        raise RuntimeError(f"central {method} {path} -> {r.status_code}: {r.text[:500]}")
    try:
        return r.json()
    except ValueError:
        raise RuntimeError(f"central returned non-JSON: {r.text[:300]}")


def search_existing(title: str) -> Optional[int]:
    """Look up a series by exact title match. Returns id or None."""
    q = quote(title, safe="")
    body = central_request("GET", f"/movies?search={q}&type=series&per_page=200")
    for item in (body.get("data") or []):
        if item.get("title") == title and item.get("id") is not None:
            try:
                return int(item["id"])
            except (TypeError, ValueError):
                continue
    return None


def telegram_notify(text: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
    except Exception as e:
        log(f"telegram notify failed: {e}")


# -------- Per-folder work --------
def process_folder(folder: Path, state: dict, *, test_mode: bool = False) -> None:
    title = folder.name
    sig = folder_signature(folder)

    prev = state.get(title) or {}
    if not test_mode and prev.get("signature") == sig:
        return  # unchanged

    issues: list[str] = []

    # Cover
    cover_url = ""
    cover = find_cover(folder)
    if cover:
        try:
            cover_url = upload_cover(cover)
        except Exception as e:
            issues.append(f"อัพปกล้มเหลว: {e}")
    else:
        issues.append("ไม่พบไฟล์ปก (*_cover.*)")

    # Summary
    description = ""
    summary_file = find_summary(folder)
    if summary_file:
        try:
            description = summary_file.read_text(encoding="utf-8").strip()
        except Exception as e:
            issues.append(f"อ่าน summary.txt ไม่ได้: {e}")
    else:
        issues.append("ไม่พบไฟล์ summary.txt")

    # Episodes
    eps_tuples = find_episodes(folder)
    if not eps_tuples:
        issues.append("ไม่พบไฟล์ตอน (*_EP<num>.mp4)")
    episodes = [{"name": name, "url": url} for _, name, url in eps_tuples]

    payload = {
        "title": title,
        "description": description,
        "type": "series",
        "year": "",
        "sound": "Thai",
        "resolution": "FullHD",
        "imdb": "",
        "youtube": "",
        "score": "",
        "image": cover_url,
        "episodes": episodes,
    }

    # Decide create vs update
    movie_id: Optional[int] = prev.get("id")
    if not movie_id:
        try:
            movie_id = search_existing(title)
        except Exception as e:
            log(f"search_existing({title}) failed (ignored): {e}")
            movie_id = None

    if movie_id:
        central_request("PUT", f"/movies/{movie_id}", payload)
        action = "อัพเดท"
    else:
        resp = central_request("POST", "/movies", payload)
        new_id = (resp.get("data") or {}).get("id")
        if new_id is None:
            raise RuntimeError(f"central POST returned no data.id: {resp}")
        movie_id = int(new_id)
        action = "เพิ่มใหม่"

    if not test_mode:
        state[title] = {
            "id": movie_id,
            "signature": sig,
            "ep_count": len(eps_tuples),
            "last_synced": int(time.time()),
        }
        save_state(state)

    tag = "[TEST] " if test_mode else ""
    log(f"{tag}{action}: {title} (id={movie_id}, ep={len(eps_tuples)}, issues={len(issues)})")

    # Notify (escape user-controlled strings for HTML)
    safe_title = html.escape(title)
    lines = [
        f"{('🧪 [TEST] ' if test_mode else '🎬 ')}<b>{html.escape(action)}</b>: {safe_title}",
        f"ID: <code>{movie_id}</code>",
        f"จำนวน EP: {len(eps_tuples)}",
    ]
    if cover_url:
        lines.append(f"ปก: {html.escape(cover_url)}")
    if issues:
        lines.append("⚠️ <b>ปัญหา:</b>")
        for i in issues:
            lines.append(f"• {html.escape(i)}")
    telegram_notify("\n".join(lines))


# -------- Connectivity check --------
def run_check() -> int:
    """Ping up2 (account check), central (list movies), telegram (getMe). No writes."""
    ok = True

    # up2.in.th — there isn't a dedicated ping endpoint; use upload endpoint with
    # an obviously-invalid request that authenticates but fails validation.
    try:
        r = requests.get(
            "https://up2.in.th/api/v1/upload",
            headers={"Authorization": f"Bearer {UP2_API_KEY}"},
            timeout=15,
        )
        # any 2xx/4xx (other than 401) means our token reached the server.
        if r.status_code == 401:
            log(f"❌ up2: 401 Unauthorized — UP2_API_KEY ผิด")
            ok = False
        else:
            log(f"✓ up2: HTTP {r.status_code} (token accepted at network level)")
    except Exception as e:
        log(f"❌ up2: ติดต่อไม่ได้ — {e}")
        ok = False

    # central — list one movie
    try:
        body = central_request("GET", "/movies?per_page=1")
        total = (body.get("meta") or {}).get("total", "?")
        log(f"✓ central: OK, total movies = {total}")
    except Exception as e:
        log(f"❌ central: {e}")
        ok = False

    # telegram getMe
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/getMe",
            timeout=15,
        )
        body = r.json()
        if r.ok and body.get("ok"):
            uname = body.get("result", {}).get("username")
            log(f"✓ telegram: bot @{uname} ตอบกลับ ok")
        else:
            log(f"❌ telegram getMe: HTTP {r.status_code} body={body}")
            ok = False
    except Exception as e:
        log(f"❌ telegram: {e}")
        ok = False

    # Send a test message
    if ok:
        telegram_notify("✅ <b>--check</b> ผ่าน: เชื่อมต่อ up2 / central / telegram ได้ครบ")
        log("ส่งข้อความ test เข้า Telegram แล้ว ถ้าเห็นข้อความใน chat แปลว่า TG_CHAT_ID ถูก")

    return 0 if ok else 2


# -------- Main --------
def main() -> int:
    parser = argparse.ArgumentParser(description="vseries → central API sync worker")
    parser.add_argument(
        "--test",
        action="store_true",
        help="โหมดทดสอบ: ประมวลผลแค่ 3 โฟลเดอร์แรก ส่งเข้า API จริง แต่ไม่บันทึก state",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="จำกัดจำนวนโฟลเดอร์ที่ประมวลผล (override --test default ของ 3)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="ตรวจการเชื่อมต่อ up2, central, telegram โดยไม่ทำงานจริง",
    )
    args = parser.parse_args()

    load_config()

    if args.check:
        return run_check()

    if not VSERIES_ROOT.exists():
        log(f"VSERIES_ROOT ไม่พบ: {VSERIES_ROOT}")
        return 1

    test_mode = args.test
    limit = args.limit if args.limit is not None else (3 if test_mode else None)

    if test_mode:
        log("=== TEST MODE === ไม่บันทึก state, ประมวลผลแค่ "
            f"{limit if limit else 'ทั้งหมด'} โฟลเดอร์")
        telegram_notify(
            f"🧪 <b>TEST MODE</b> เริ่มทำงาน — จะทดสอบ {limit} เรื่อง (ไม่บันทึก state)"
        )

    state = load_state()
    processed = 0
    errors = 0

    folders = [f for f in sorted(VSERIES_ROOT.iterdir(), key=lambda p: p.name) if f.is_dir()]
    if limit is not None:
        folders = folders[:limit]

    for folder in folders:
        try:
            process_folder(folder, state, test_mode=test_mode)
            processed += 1
        except Exception as e:
            errors += 1
            log(f"ERROR {folder.name}: {e}")
            traceback.print_exc()
            telegram_notify(
                f"❌ <b>ERROR</b> ขณะประมวลผล <code>{html.escape(folder.name)}</code>\n"
                f"<code>{html.escape(str(e)[:500])}</code>"
            )

    if not test_mode:
        save_state(state)

    log(f"done. processed={processed} errors={errors} test_mode={test_mode}")
    if test_mode:
        telegram_notify(
            f"🧪 <b>TEST MODE</b> เสร็จ — ประมวลผล {processed} เรื่อง, errors={errors}"
        )
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
