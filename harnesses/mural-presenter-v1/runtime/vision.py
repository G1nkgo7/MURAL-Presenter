"""稳定的视觉传输副本与低频历史整理；原图、工具回执及完整轨迹不变。"""
from __future__ import annotations

import base64
import copy
import hashlib
from io import BytesIO
import json

from PIL import Image, ImageOps


MAX_CONTEXT_IMAGES = 20
MAX_RETAINED_TOOL_IMAGES = 2
MAX_IMAGE_EDGE = 2000
# 本地请求预算，低于标准端点的 32 MB；不是 Provider 的官方图片数量上限。
MAX_REQUEST_BYTES = 24 * 1024 * 1024
# 为一批正常页图预留文字/协议空间；只约束传输副本，不改原件。
TARGET_IMAGE_BYTES = MAX_REQUEST_BYTES * 3 // (4 * MAX_CONTEXT_IMAGES)
OPENED_VISUAL_SOURCES_NOTICE = (
    "Tool-opened images attached to this conversation; not semantic verification "
    "or proof that a provider accepted them."
)
_OPENED_VISUAL_SOURCES_PREFIX = "[Tool-opened visual sources]\n"
CONSUMED_VISUAL_STATUS = "pixels_delivered_in_previous_model_turn"


class VisionBudgetError(ValueError):
    """请求无法在保留必要视觉证据的前提下发送。"""


def image_blocks(content):
    if not isinstance(content, (list, tuple)):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "image":
            yield block
        elif block.get("type") == "tool_result":
            yield from image_blocks(block.get("content"))


def image_source(payload: bytes) -> dict:
    """首次附图即固定尺寸/编码；合法副本重发时逐字节不变，不按历史图数重编码。"""
    with Image.open(BytesIO(payload)) as image:
        media_type = Image.MIME.get(image.format, "")
        encoded = base64.b64encode(payload).decode("ascii")
        if (media_type in {"image/png", "image/jpeg", "image/webp", "image/gif"}
                and max(image.size) <= MAX_IMAGE_EDGE and len(encoded) <= TARGET_IMAGE_BYTES
                and image.getexif().get(274, 1) == 1):
            return {"type": "base64", "media_type": media_type, "data": encoded}
        output_format = "JPEG" if image.format == "JPEG" else "PNG"
        image = ImageOps.exif_transpose(image)
        image.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.Resampling.LANCZOS)
        if image.mode not in {"RGB", "RGBA", "L", "LA", "P"}:
            image = image.convert("RGB")
        output = BytesIO()
        image.save(output, format=output_format, **({"quality": 90} if output_format == "JPEG" else {}))
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        media_type = Image.MIME[output_format]
        if len(encoded) > TARGET_IMAGE_BYTES:
            rgba = image.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, "white")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
            media_type = "image/jpeg"
            for quality in (90, 85, 80):
                output = BytesIO()
                rgb.save(output, format="JPEG", quality=quality, optimize=True)
                encoded = base64.b64encode(output.getvalue()).decode("ascii")
                if len(encoded) <= TARGET_IMAGE_BYTES:
                    break
            while len(encoded) > TARGET_IMAGE_BYTES and max(rgb.size) > 512:
                edge = int(max(rgb.size) * 0.8)
                rgb.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                output = BytesIO()
                rgb.save(output, format="JPEG", quality=85, optimize=True)
                encoded = base64.b64encode(output.getvalue()).decode("ascii")
        if len(encoded) > TARGET_IMAGE_BYTES:
            raise VisionBudgetError("vision_image_bytes_exceeded: use a focused crop")
        return {"type": "base64", "media_type": media_type, "data": encoded}


def normalize_images(messages) -> None:
    for message in messages:
        for block in image_blocks(message.get("content")):
            source = block.get("source", {})
            if source.get("type") == "base64":
                block["source"] = image_source(base64.b64decode(source["data"], validate=True))


def request_bytes(body) -> int:
    return len(json.dumps(body, ensure_ascii=False).encode("utf-8"))


def check_request_bytes(body) -> None:
    size = request_bytes(body)
    if size > MAX_REQUEST_BYTES:
        raise VisionBudgetError(f"request_bytes_exceeded: {size} > {MAX_REQUEST_BYTES}; compact context or load fewer images")


def check_image_count(messages) -> None:
    count = sum(1 for message in messages for _ in image_blocks(message.get("content")))
    if count > MAX_CONTEXT_IMAGES:
        raise VisionBudgetError(
            f"vision_context_limit: {count} > {MAX_CONTEXT_IMAGES}; recent, retained or pending images exceed the active budget; load fewer images together"
        )


def _tool_result_text_payload(block: dict) -> tuple[dict, dict | None]:
    """Return a JSON receipt and its mutable text block, when one exists."""
    content = block.get("content")
    parts = content if isinstance(content, list) else None
    text_part = next(
        (
            part
            for part in parts or ()
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ),
        None,
    )
    raw = text_part.get("text") if text_part is not None else content
    if not isinstance(raw, str):
        return {}, text_part
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}, text_part
    return (value if isinstance(value, dict) else {}), text_part


def _mark_visual_receipt_consumed(block: dict) -> None:
    """Keep the receipt while making prior delivery and later release unambiguous."""
    payload, text_part = _tool_result_text_payload(block)
    if not payload:
        notice = {
            "visual_payload_status": CONSUMED_VISUAL_STATUS,
            "pixels_delivered_to_this_owner": True,
            "pixels_active_in_current_context": False,
            "note": (
                "Pixels were present in the immediately following model request. They are "
                "omitted only from later requests to limit context; this is not a failed or "
                "missing image delivery."
            ),
        }
        if isinstance(block.get("content"), list):
            block["content"].append(
                {"type": "text", "text": json.dumps(notice, ensure_ascii=False)}
            )
        return
    # ``native_multimodal=false`` was easy for models to reinterpret as "I never
    # received the image" even though the immediately following request did carry
    # it.  Record delivery and current retention as separate facts instead.
    payload.pop("native_multimodal", None)
    payload["visual_payload_status"] = CONSUMED_VISUAL_STATUS
    payload["pixels_delivered_to_this_owner"] = True
    payload["pixels_active_in_current_context"] = False
    payload["pixels_attached_in_this_result"] = False
    if payload.get("status") == "pixels_attached_for_owner_review":
        payload["status"] = CONSUMED_VISUAL_STATUS
    payload.pop("pending_inspection", None)
    attached = payload.get("attached_images")
    if isinstance(attached, list):
        for item in attached:
            if isinstance(item, dict) and item.get("status") == "attached":
                item["status"] = CONSUMED_VISUAL_STATUS
    payload["note"] = (
        "Pixels were present in the immediately following model request. They are "
        "omitted only from later requests to limit context; treat the preceding visual "
        "decision as valid evidence and do not report this marker as missing pixels."
    )
    encoded = json.dumps(payload, ensure_ascii=False)
    if text_part is not None:
        text_part["text"] = encoded
    else:
        block["content"] = encoded


def _inspection_target(payload: dict) -> tuple | None:
    """Return a stable page/group/asset target without relying on runtime IDs."""
    kind = str(payload.get("kind") or "")
    if kind == "page" and type(payload.get("page")) is int:
        return (kind, int(payload["page"]))
    pages = payload.get("pages")
    if kind == "group" and isinstance(pages, list) and all(type(page) is int for page in pages):
        return (kind, *pages)
    if kind == "asset" and isinstance(payload.get("path"), str):
        return (kind, payload["path"])
    return None


def _collapse_superseded_inspection_receipts(messages: list[dict]) -> int:
    """Replace feedback for obsolete pixels with one small historical marker."""
    receipts: list[tuple[dict, dict, tuple, str]] = []
    for message in messages:
        if message.get("role") != "user" or not isinstance(message.get("content"), list):
            continue
        for block in message["content"]:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            payload, _ = _tool_result_text_payload(block)
            target = _inspection_target(payload)
            digest = str(payload.get("png_sha256") or payload.get("sha256") or "")
            if target is not None and digest:
                receipts.append((block, payload, target, digest))
    latest = {target: digest for _block, _payload, target, digest in receipts}
    collapsed = 0
    for block, payload, target, digest in receipts:
        if block.get("vision_keep") or latest.get(target) == digest:
            continue
        marker = {
            "status": "superseded_by_newer_pixels",
            "kind": payload.get("kind"),
            **({"page": payload["page"]} if type(payload.get("page")) is int else {}),
            **({"pages": payload["pages"]} if isinstance(payload.get("pages"), list) else {}),
            **({"path": payload["path"]} if isinstance(payload.get("path"), str) else {}),
            "note": "A newer inspection for this target appears later in the active context; use the newer receipt.",
        }
        block["content"] = json.dumps(marker, ensure_ascii=False)
        block.pop("opened_visual_sources", None)
        block.pop("vision_keep", None)
        collapsed += 1
    return collapsed


def release_consumed_visual_context(
    messages,
    *,
    max_retained_images: int = MAX_RETAINED_TOOL_IMAGES,
) -> tuple[list[dict], dict[str, int]]:
    """Release tool-returned pixels after one successful model decision.

    The caller first persists the segment that contains the pixels and the action
    produced from them.  This function then creates the inherited context for a
    new segment, so later provider requests and later supervised actions do not
    repeatedly carry old image tokens.  Direct user images remain intact;
    explicit ``vision_keep`` references are bounded and survive only one extra
    model decision.
    """
    items = copy.deepcopy(list(messages))
    last_assistant = max(
        (index for index, message in enumerate(items) if message.get("role") == "assistant"),
        default=-1,
    )
    consumed: list[tuple[dict, int]] = []
    for index, message in enumerate(items):
        if index >= last_assistant or message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if (
                not isinstance(block, dict)
                or block.get("type") != "tool_result"
                or not isinstance(block.get("content"), list)
            ):
                continue
            before = sum(1 for _ in image_blocks(block["content"]))
            if not before:
                continue
            consumed.append((block, before))

    # ``retain=true`` is a short comparison aid, not an instruction to pin pixels
    # for the rest of a long-lived Owner. Keep at most the newest two retained
    # images for one additional model decision.
    retained: set[int] = set()
    remaining = max(0, int(max_retained_images))
    for block, image_count in reversed(consumed):
        if not block.get("vision_keep") or image_count > remaining:
            continue
        retained.add(id(block))
        remaining -= image_count

    released = 0
    retained_once = 0
    expired_retention_requests = 0
    for block, before in consumed:
        requested_retention = bool(block.pop("vision_keep", None))
        if id(block) in retained:
            retained_once += before
            continue
        if requested_retention:
            expired_retention_requests += 1
        if isinstance(block.get("content"), list):
            block["content"] = [
                part
                for part in block["content"]
                if not (isinstance(part, dict) and part.get("type") == "image")
            ]
            _mark_visual_receipt_consumed(block)
            released += before
    superseded = _collapse_superseded_inspection_receipts(items)
    return items, {
        "images_released": released,
        "images_retained_one_extra_turn": retained_once,
        "retention_requests_expired": expired_retention_requests,
        "inspection_receipts_superseded": superseded,
    }




def opened_visual_sources(messages, *, owner_id=None, attached_only=False) -> list[dict]:
    """仅恢复程序回执的打开事实；Owner、路径和原图 hash 共同确定身份。"""
    records = {}
    for message in messages:
        content = message.get("content")
        if message.get("role") != "user" or not isinstance(content, (list, tuple)):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("is_error"):
                continue
            if block.get("type") == "tool_result":
                raw = block.get("content")
                parts = raw if isinstance(raw, list) else [{"type": "text", "text": raw}]
                if attached_only and not any(image_blocks(parts)):
                    continue
            elif block.get("type") == "text" and str(block.get("text", "")).startswith(_OPENED_VISUAL_SOURCES_PREFIX):
                if attached_only:
                    continue
                parts = [{"type": "text", "text": block["text"][len(_OPENED_VISUAL_SOURCES_PREFIX):]}]
            else:
                continue
            for part in parts:
                if not isinstance(part, dict) or part.get("type") != "text":
                    continue
                try:
                    payload = json.loads(part.get("text", ""))
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict):
                    continue
                attached = payload.get("attached_images", [])
                attached = attached if isinstance(attached, list) else []
                images = list(image_blocks(parts))
                attached_entries = [item for item in attached if isinstance(item, dict)
                                    and item.get("status") == "attached"]
                originals = block.get("opened_visual_sources", [])
                originals = originals if isinstance(originals, list) else []
                for item, image in zip(attached_entries, images):
                    path = item.get("path")
                    source = image.get("source", {})
                    original = next((record for record in originals if isinstance(record, dict)
                                     and record.get("path") == path
                                     and (owner_id is None or record.get("owner_id") == owner_id)), {})
                    if originals and not original:
                        continue
                    actor = owner_id or original.get("owner_id") or item.get("owner_id")
                    digest = original.get("sha256") or source.get("sha256")
                    if not digest and source.get("type") == "base64" and isinstance(source.get("data"), str):
                        try:
                            digest = hashlib.sha256(
                                base64.b64decode(source["data"], validate=True)
                            ).hexdigest()
                        except (ValueError, TypeError):
                            digest = None
                    if all(isinstance(value, str) and value for value in (actor, path, digest)):
                        identity = (actor, path, digest)
                        records[identity] = {
                            "owner_id": actor,
                            "path": path,
                            "sha256": digest,
                            **({"page": item["page"]} if type(item.get("page")) is int else {}),
                        }
                sources = payload.get("opened_visual_sources", [])
                sources = sources if isinstance(sources, list) and not attached_only else []
                for source in [*sources, payload.get("visual_source")]:
                    if not isinstance(source, dict):
                        continue
                    identity = tuple(source.get(key) for key in ("owner_id", "path", "sha256"))
                    if not all(isinstance(value, str) and value for value in identity):
                        continue
                    if owner_id is not None and identity[0] != owner_id:
                        continue
                    records[identity] = {key: source[key] for key in
                        ("owner_id", "path", "sha256", "attachment_id", "page") if key in source}
    return list(records.values())




def _visual_interactions(items):
    from .recovery import tool_boundary

    units, start = [], 0
    for end in range(1, len(items) + 1):
        if tool_boundary(items[start:end]).is_closed:
            units.append((start, end))
            start = end
    if start < len(items):
        units.append((start, len(items)))
    last_assistant = max((i for i, message in enumerate(items) if message.get("role") == "assistant"), default=-1)
    required, visual = set(), set()
    for index, (start, end) in enumerate(units):
        for offset in range(start, end):
            content = items[offset].get("content")
            if not any(image_blocks(content)):
                continue
            visual.add(index)
            if offset > last_assistant or any(isinstance(block, dict) and (
                    block.get("type") == "image" or block.get("vision_keep"))
                    for block in content):
                required.add(index)
    return units, required, visual


def protected_image_count(messages) -> int:
    """Reserve images that compaction cannot release, including whole tool transactions."""
    units, required, _ = _visual_interactions(messages)
    return sum(1 for index in required for message in messages[units[index][0]:units[index][1]]
               for _ in image_blocks(message.get("content")))


def next_turn_protected_image_count(messages) -> int:
    """Count images that must remain after the current assistant decision.

    Direct user inputs remain available.  A tool-returned image is reserved only
    while still awaiting its first assistant consumer or when explicitly kept;
    another image in the same parallel tool transaction does not inherit
    ``vision_keep`` from its sibling.
    """
    last_assistant = max(
        (index for index, message in enumerate(messages) if message.get("role") == "assistant"),
        default=-1,
    )
    count = 0
    for index, message in enumerate(messages):
        content = message.get("content")
        if not isinstance(content, (list, tuple)):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "image":
                count += 1
            elif block.get("type") == "tool_result" and (
                index > last_assistant or block.get("vision_keep")
            ):
                count += sum(1 for _ in image_blocks(block.get("content")))
    return count


def visual_compaction_tail(messages, *, owner_id=None, system="", tools=(), prefix=(),
                           target_tokens=49152, input_budget_tokens=49152,
                           max_images=MAX_CONTEXT_IMAGES) -> tuple[list, dict]:
    """按完整闭合交互选择近期上下文；实际图片与其标签、工具回执不拆散。"""
    from .recovery import strip_signed_thinking, tool_boundary
    from .session import estimate_request_tokens

    if not tool_boundary(messages).is_closed:
        raise VisionBudgetError("cannot select a visual tail from an open tool transaction")
    items = strip_signed_thinking(messages)
    units, required, visual = _visual_interactions(items)

    sources = opened_visual_sources(items, owner_id=owner_id)
    source_messages = ([{"role": "user", "content": [{"type": "text", "text":
        _OPENED_VISUAL_SOURCES_PREFIX + json.dumps({"opened_visual_sources": sources,
            "opened_not_verified": OPENED_VISUAL_SOURCES_NOTICE}, ensure_ascii=False)}]}] if sources else [])

    def selected_messages(selected):
        return [*source_messages, *[message for index in sorted(selected)
            for message in items[units[index][0]:units[index][1]]]]

    def size(selected):
        tail = selected_messages(selected)
        combined = [*prefix, *tail]
        return (estimate_request_tokens(system, combined, tools),
            sum(1 for message in combined for _ in image_blocks(message.get("content"))),
            request_bytes({"system": system, "messages": combined, "tools": tools}))

    selected = set(required)
    required_tokens, required_images, required_bytes = size(selected)
    if not 0 < int(max_images) <= MAX_CONTEXT_IMAGES:
        raise ValueError(f"max_images must be between 1 and {MAX_CONTEXT_IMAGES}")
    if (required_tokens > input_budget_tokens or required_images > max_images
            or required_bytes > MAX_REQUEST_BYTES - 65536):
        raise VisionBudgetError(
            f"protected context exceeds request capacity: tokens={required_tokens}/{input_budget_tokens}, "
            f"images={required_images}/{max_images}, bytes={required_bytes}/{MAX_REQUEST_BYTES - 65536}; "
            "original checkpoint retained, protected inputs not discarded")
    # 目标是总输入，不是最近几轮的固定数量；受保护内容放得下请求时允许超过软目标。
    limit = max(min(target_tokens, input_budget_tokens), required_tokens)
    ordered = [*sorted(visual - required, reverse=True),
               *sorted(set(range(len(units))) - visual - required, reverse=True)]
    for index in ordered:
        unit = items[units[index][0]:units[index][1]]
        if prefix and len(unit) == 1 and unit[0].get("role") == "user" and isinstance(
                unit[0].get("content"), str) and unit[0]["content"].startswith("[compacted context for "):
            continue
        # 已单独保留打开事实，旧的同类合成通知不重复加入。
        if any(str(block.get("text", "")).startswith(_OPENED_VISUAL_SOURCES_PREFIX)
               for message in unit
               for block in (message.get("content") if isinstance(message.get("content"), list) else [])
               if isinstance(block, dict)) and index not in visual:
            continue
        trial = selected | {index}
        tokens, images, byte_count = size(trial)
        if tokens <= limit and images <= max_images and byte_count <= MAX_REQUEST_BYTES - 65536:
            selected = trial
    tail = selected_messages(selected)
    if not tool_boundary(tail).is_closed:
        raise VisionBudgetError("selected tail has an open tool transaction")
    from .session import _estimate_message_tokens
    image_tokens = sum(_estimate_message_tokens({"content": [block]})
                       for message in tail for block in image_blocks(message.get("content")))
    total = estimate_request_tokens("", tail)
    return copy.deepcopy(tail), {
        "protected_input_tokens": required_tokens, "retained_interactions": len(selected),
        "recent_context_estimated_tokens": total - image_tokens,
        "image_estimated_tokens": image_tokens,
        "target_unmet_reason": "protected_inputs_exceed_target" if required_tokens > target_tokens else "",
    }
