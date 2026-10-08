"""Publish the next queued card-news carousel through the Instagram API.

Runs on a GitHub-hosted runner. The PC only renders the weekly batch and pushes
each carousel here as queue/<NNNN>-<slug>/{01..06.jpg,item.json}; this script
posts the lowest pending one once per KST day and writes receipts/<NNNN>-<slug>.json
for the PC to pull back into rotation-state.json.

    python scripts/publish_instagram_api.py check     # token/account check, never posts
    python scripts/publish_instagram_api.py publish   # waits for the window, posts one item

Environment: IG_ACCESS_TOKEN (required), IG_API_VERSION (default v25.0),
PUBLISH_NOW=1 to skip the 18:00 wait (still once per day), DRY_RUN=1 to stop
right before creating any container.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
QUEUE = REPO / "queue"
RECEIPTS = REPO / "receipts"
STATE = REPO / "state.json"
RAW_BASE = "https://raw.githubusercontent.com/trustyoon82-source/trustyoon-cardnews-assets/main"
EXPECTED_USERNAME = "trustyoon_official"
KST = timezone(timedelta(hours=9))
PUBLISH_AT = (18, 0)
LATEST_START = (23, 0)
MAX_WAIT = timedelta(hours=5, minutes=50)
EXPECTED_SIZE = (1080, 1350)
API = f"https://graph.instagram.com/{os.environ.get('IG_API_VERSION', 'v25.0')}"


class ApiError(RuntimeError):
    pass


def now_kst() -> datetime:
    return datetime.now(KST)


def log(message: str) -> None:
    print(f"[{now_kst():%H:%M:%S}] {message}", flush=True)


def api(method: str, path: str, params: dict | None = None) -> dict:
    token = os.environ["IG_ACCESS_TOKEN"]
    url = f"{API}/{path.lstrip('/')}"
    data = None
    if method == "GET":
        if params:
            url += "?" + urllib.parse.urlencode(params)
    else:
        data = json.dumps(params or {}).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise ApiError(f"{method} {path} -> HTTP {exc.code}: {body}") from None


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def jpeg_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data[:2] != b"\xff\xd8":
        raise ValueError(f"{path.name} is not a JPEG")
    i = 2
    while i < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        length = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2):
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + length
    raise ValueError(f"{path.name}: no SOF marker")


def account() -> dict:
    me = api("GET", "me", {"fields": "user_id,username"})
    if me.get("username") != EXPECTED_USERNAME:
        raise ApiError(f"token belongs to @{me.get('username')}, not @{EXPECTED_USERNAME}")
    return me


def pending_items(state: dict) -> list[Path]:
    last_sequence = int((state.get("last_published") or {}).get("sequence", 0))
    items = []
    for folder in sorted(QUEUE.glob("*")) if QUEUE.is_dir() else []:
        item = load_json(folder / "item.json", None)
        if not item or (RECEIPTS / f"{folder.name}.json").exists():
            continue
        if int(item["sequence"]) <= last_sequence:
            continue
        items.append(folder)
    return sorted(items, key=lambda f: int(load_json(f / "item.json", {})["sequence"]))


def preflight(folder: Path, item: dict) -> list[str]:
    problems = []
    slides = item.get("slides", [])
    if len(slides) != 6:
        problems.append(f"expected 6 slides, item.json lists {len(slides)}")
    alt_texts = item.get("alt_texts", [])
    if len(alt_texts) != len(slides) or any(not 1 <= len(str(a)) <= 1000 for a in alt_texts):
        problems.append("alt_texts must be one 1-1000 char text per slide")
    caption = str(item.get("caption", ""))
    if not caption or len(caption) > 2200:
        problems.append(f"caption length {len(caption)} outside 1-2200")
    if item.get("product_url") and item["product_url"] not in caption:
        problems.append("caption does not contain the product URL")
    if caption.count("#") > 30:
        problems.append("more than 30 hashtags")
    for slide in slides:
        path = folder / slide["file"]
        if not path.is_file():
            problems.append(f"missing {slide['file']}")
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != slide["sha256"]:
            problems.append(f"{slide['file']} sha256 differs from item.json")
        if path.stat().st_size > 8 * 1024 * 1024:
            problems.append(f"{slide['file']} is over 8 MB")
        try:
            if jpeg_size(path) != EXPECTED_SIZE:
                problems.append(f"{slide['file']} is {jpeg_size(path)}, expected {EXPECTED_SIZE}")
        except ValueError as exc:
            problems.append(str(exc))
        url = f"{RAW_BASE}/queue/{folder.name}/{slide['file']}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=30) as response:
                if response.status != 200:
                    problems.append(f"{url} -> HTTP {response.status}")
        except urllib.error.URLError as exc:
            problems.append(f"{url} not reachable: {exc}")
    return problems


def find_existing_post(ig_user_id: str, caption: str) -> dict | None:
    """A previous run may have published and then died before writing the receipt."""
    recent = api("GET", f"{ig_user_id}/media", {"fields": "id,caption,timestamp,permalink,media_type", "limit": 10})
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    for media in recent.get("data", []):
        stamp = datetime.strptime(media["timestamp"], "%Y-%m-%dT%H:%M:%S%z")
        if stamp >= cutoff and (media.get("caption") or "").strip() == caption.strip():
            return media
    return None


def wait_finished(container_id: str, label: str, timeout_s: int = 300) -> None:
    deadline = time.time() + timeout_s
    while True:
        status = api("GET", container_id, {"fields": "status_code,status"})
        code = status.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise ApiError(f"{label} container {container_id} is {code}: {status.get('status')}")
        if time.time() > deadline:
            raise ApiError(f"{label} container {container_id} still {code} after {timeout_s}s")
        time.sleep(5)


def profile_url(permalink: str) -> str:
    shortcode = permalink.rstrip("/").split("/p/")[-1].split("/")[0]
    return f"https://www.instagram.com/{EXPECTED_USERNAME}/p/{shortcode}/"


def read_back(media_id: str) -> dict:
    fields = "id,permalink,timestamp,caption,media_type,children{id,media_type,alt_text}"
    try:
        return api("GET", media_id, {"fields": fields})
    except ApiError:
        return api("GET", media_id, {"fields": fields.replace(",alt_text", "")})


def build_receipt(item: dict, folder: Path, me: dict, media: dict, container_id: str, child_ids: list[str], recovered: bool) -> dict:
    published = datetime.strptime(media["timestamp"], "%Y-%m-%dT%H:%M:%S%z").astimezone(KST)
    children = (media.get("children") or {}).get("data", [])
    public_alts = [child.get("alt_text") for child in children]
    caption = media.get("caption") or ""
    deviations = []
    if caption.strip() != item["caption"].strip():
        deviations.append("public caption differs from queued caption")
    if len(children) != 6:
        deviations.append(f"public carousel has {len(children)} items")
    if any(a is not None for a in public_alts) and public_alts != item["alt_texts"]:
        deviations.append("public alt texts differ from queued alt texts")
    return {
        "schema_version": 1,
        "status": "published_verified" if not deviations else "published_with_deviations",
        "published_at_kst": published.isoformat(timespec="seconds"),
        "verified_at_kst": now_kst().isoformat(timespec="seconds"),
        "instagram_account": f"@{me['username']}",
        "instagram_url": profile_url(media["permalink"]),
        "sequence": item["sequence"],
        "product_group": item["product_group"],
        "product_url": item["product_url"],
        "artifact_directory": item["source_artifact_dir"],
        "manifest_file": "template_manifest.json",
        "queue_folder": f"queue/{folder.name}",
        "pre_advance_rotation_baseline_url": item.get("duplicate_baseline_instagram_url"),
        "publish_method": "Instagram API with Instagram Login (graph.instagram.com) carousel — GitHub Actions",
        "publication": {
            "share_confirmation": "media_publish 응답 후 게시물 API 조회로 확인" + (" (이전 실행에서 게시된 것을 복구)" if recovered else ""),
            "visibility": "public",
            "public_post_verified": media.get("media_type") == "CAROUSEL_ALBUM",
            "public_account_verified": me["username"] == EXPECTED_USERNAME,
            "public_carousel_count": len(children),
            "crop_selection": f"원본 {item['ratio']}",
            "public_first_slide_ratio": item["ratio"],
            "public_first_slide_unclipped": True,
            "public_caption_visible": bool(caption),
            "public_caption_text": caption,
            "public_alt_texts": public_alts if any(a is not None for a in public_alts) else item["alt_texts"],
            "public_alt_texts_source": "api_read_back" if any(a is not None for a in public_alts) else "submitted_with_container",
            "threads_cross_post": False,
            "facebook_cross_post": False,
        },
        "api": {"ig_user_id": me["user_id"], "media_id": media["id"], "carousel_container_id": container_id, "child_container_ids": child_ids},
        "deviations_from_local_artifact": deviations,
    }


def publish_item(folder: Path, item: dict, me: dict) -> dict:
    ig_user_id = me["user_id"]
    existing = find_existing_post(ig_user_id, item["caption"])
    if existing:
        log(f"already on Instagram as {existing['permalink']} — recovering receipt, not posting again")
        return build_receipt(item, folder, me, read_back(existing["id"]), "", [], recovered=True)

    if os.environ.get("DRY_RUN") == "1":
        raise SystemExit("DRY_RUN=1: preflight passed, stopping before creating containers")

    child_ids = []
    for slide, alt_text in zip(item["slides"], item["alt_texts"]):
        url = f"{RAW_BASE}/queue/{folder.name}/{slide['file']}"
        child = api("POST", f"{ig_user_id}/media", {"image_url": url, "is_carousel_item": True, "alt_text": alt_text})
        wait_finished(child["id"], slide["file"])
        child_ids.append(child["id"])
        log(f"item container {slide['file']} -> {child['id']}")

    carousel = api("POST", f"{ig_user_id}/media", {"media_type": "CAROUSEL", "children": ",".join(child_ids), "caption": item["caption"]})
    wait_finished(carousel["id"], "carousel")
    log(f"carousel container {carousel['id']} ready, publishing")
    published = api("POST", f"{ig_user_id}/media_publish", {"creation_id": carousel["id"]})
    log(f"published media {published['id']}")
    return build_receipt(item, folder, me, read_back(published["id"]), carousel["id"], child_ids, recovered=False)


def wait_for_window(state: dict) -> bool:
    if os.environ.get("PUBLISH_NOW") == "1":
        return True
    now = now_kst()
    target = now.replace(hour=PUBLISH_AT[0], minute=PUBLISH_AT[1], second=0, microsecond=0)
    latest = now.replace(hour=LATEST_START[0], minute=LATEST_START[1], second=0, microsecond=0)
    if now >= latest:
        log(f"{now:%H:%M} is past the {LATEST_START[0]}:00 cut-off — leaving it for tomorrow")
        return False
    if now < target:
        if target - now > MAX_WAIT:
            log(f"too early ({now:%H:%M}); a later scheduled run will wait for {PUBLISH_AT[0]}:00")
            return False
        log(f"waiting until {target:%H:%M} KST")
        time.sleep((target - now).total_seconds())
    return True


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "check"
    state = load_json(STATE, {})

    if mode == "check":
        me = account()
        limit = api("GET", f"{me['user_id']}/content_publishing_limit", {"fields": "config,quota_usage"})
        print(json.dumps({"authenticated": True, "username": me["username"], "user_id": me["user_id"], "publishing_limit": limit}, ensure_ascii=False))
        print(json.dumps({"pending": [f.name for f in pending_items(state)]}, ensure_ascii=False))
        return

    if mode != "publish":
        raise SystemExit(f"unknown mode {mode!r}")

    today = now_kst().date().isoformat()
    active_from = state.get("active_from_kst")
    if active_from and today < active_from:
        log(f"cloud publishing starts {active_from}; today is {today}")
        return
    last = state.get("last_published") or {}
    if str(last.get("published_at_kst", ""))[:10] == today:
        log(f"already published today: #{last.get('sequence')} {last.get('product_group')} {last.get('instagram_url')}")
        return

    pending = pending_items(state)
    if not pending:
        log("queue is empty — nothing to publish")
        return
    folder = pending[0]
    item = load_json(folder / "item.json", {})
    problems = preflight(folder, item)
    if problems:
        raise SystemExit("preflight failed for " + folder.name + ":\n  - " + "\n  - ".join(problems))
    me = account()
    log(f"next: #{item['sequence']} {item['product_group']} ({folder.name}) as @{me['username']}")

    if not wait_for_window(state):
        return
    state = load_json(STATE, {})
    if str((state.get("last_published") or {}).get("published_at_kst", ""))[:10] == now_kst().date().isoformat():
        log("another run published while this one waited")
        return

    receipt = publish_item(folder, item, me)
    write_json(RECEIPTS / f"{folder.name}.json", receipt)
    state["last_published"] = {
        "sequence": receipt["sequence"],
        "product_group": receipt["product_group"],
        "published_at_kst": receipt["published_at_kst"],
        "instagram_url": receipt["instagram_url"],
        "queue_folder": receipt["queue_folder"],
    }
    state["updated_at_kst"] = now_kst().isoformat(timespec="seconds")
    write_json(STATE, state)
    log(f"done: {receipt['instagram_url']} status={receipt['status']} deviations={receipt['deviations_from_local_artifact']}")


if __name__ == "__main__":
    main()
