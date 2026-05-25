#!/usr/bin/env python3
"""vseries → central API sync worker. Runs once per invocation (driven by systemd timer)."""

from __future__ import annotations

import argparse
import hashlib
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
    candidates = []
    for f in folder.iterdir():
        if not f.is_file():
            continue
        low = f.name.lower()
        if "cover" in low and low.endswith(COVER_EXTS):
            candidates.append(f)
    if not candidates:
        return None
    # prefer "_cover.jpg" exact suffix
    for c in candidates:
        if c.name.lower().endswith("_cover.jpg"):
            return c
    return sorted(candidates, key=lambda p: p.name)[0]


def find_summary(folder: Path) -> Optional[Path]:
    for f in folder.iterdir():
        if f.is_file() and f.name.lower().endswith(SUMMARY_SUFFIX):
            return f
    return None


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
    body = r.json()
    direct = body.get("data", {}).get("urls", {}).get("direct")
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
    return r.json()


def search_existing(title: str) -> Optional[int]:
    """Look up a series by exact title match. Returns id or None."""
    q = quote(title, safe="")
    body = central_request("GET", f"/movies?search={q}&type=series&per_page=200")
    for item in body.get("data", []):
        if item.get("title") == title:
            return int(item["id"])
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
        movie_id = int(resp.get("data", {}).get("id"))
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

    # Notify
    lines = [
        f"{('🧪 [TEST] ' if test_mode else '🎬 ')}<b>{action}</b>: {title}",
        f"ID: <code>{movie_id}</code>",
        f"จำนวน EP: {len(eps_tuples)}",
    ]
    if cover_url:
        lines.append(f"ปก: {cover_url}")
    if issues:
        lines.append("⚠️ <b>ปัญหา:</b>")
        for i in issues:
            lines.append(f"• {i}")
    telegram_notify("\n".join(lines))


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
    args = parser.parse_args()

    load_config()

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
                f"❌ <b>ERROR</b> ขณะประมวลผล <code>{folder.name}</code>\n"
                f"<code>{str(e)[:500]}</code>"
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
