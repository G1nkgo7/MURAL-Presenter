#!/usr/bin/env python3
"""Final-output pre-audit helpers for ordered rendered decks."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .judge_transport_and_artifact_runtime import (
    RunnerError,
    configure_browser_library_path,
)


VISIBLE_CONTENT_CONTRACT_VERSION = "final_visible_content_v1"
FINAL_PREAUDIT_CONTRACT_VERSION = "final_output_preaudit_v2"
PREAUDIT_TYPES = {"content", "visual"}


_VISIBLE_CONTENT_SCRIPT = r"""
() => {
  const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
  const rectObject = value => ({
    x: Math.round(value.x * 10) / 10,
    y: Math.round(value.y * 10) / 10,
    width: Math.round(value.width * 10) / 10,
    height: Math.round(value.height * 10) / 10,
    right: Math.round(value.right * 10) / 10,
    bottom: Math.round(value.bottom * 10) / 10
  });
  const excluded = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'TEMPLATE']);
  const effectiveVisible = element => {
    let current = element;
    let opacity = 1;
    while (current && current.nodeType === Node.ELEMENT_NODE) {
      const style = getComputedStyle(current);
      if (style.display === 'none' || style.visibility === 'hidden' ||
          style.visibility === 'collapse') return false;
      opacity *= Number.parseFloat(style.opacity || '1');
      if (!(opacity > 0.001)) return false;
      current = current.parentElement;
    }
    return true;
  };
  const intersectsViewport = rect =>
    rect.width > 0 && rect.height > 0 &&
    rect.right > 0 && rect.bottom > 0 &&
    rect.left < innerWidth && rect.top < innerHeight;
  const clippedByAncestor = (element, rect) => {
    let current = element.parentElement;
    while (current && current !== document.documentElement) {
      const style = getComputedStyle(current);
      const clipsX = ['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowX);
      const clipsY = ['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowY);
      if (clipsX || clipsY) {
        const box = current.getBoundingClientRect();
        if ((clipsX && (rect.right <= box.left || rect.left >= box.right)) ||
            (clipsY && (rect.bottom <= box.top || rect.top >= box.bottom))) {
          return true;
        }
      }
      current = current.parentElement;
    }
    return false;
  };

  const blocks = [];
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const text = normalize(node.textContent);
    const parent = node.parentElement;
    if (!text || !parent || excluded.has(parent.tagName) || !effectiveVisible(parent)) continue;
    const range = document.createRange();
    range.selectNodeContents(node);
    const rects = Array.from(range.getClientRects())
      .filter(rect => intersectsViewport(rect) && !clippedByAncestor(parent, rect));
    if (!rects.length) continue;
    const left = Math.max(0, Math.min(...rects.map(rect => rect.left)));
    const top = Math.max(0, Math.min(...rects.map(rect => rect.top)));
    const right = Math.min(innerWidth, Math.max(...rects.map(rect => rect.right)));
    const bottom = Math.min(innerHeight, Math.max(...rects.map(rect => rect.bottom)));
    const style = getComputedStyle(parent);
    blocks.push({
      order: blocks.length + 1,
      text: text.slice(0, 1000),
      bbox: rectObject({x:left, y:top, width:right-left, height:bottom-top,
                        right:right, bottom:bottom}),
      font_size: style.fontSize,
      font_weight: style.fontWeight
    });
    if (blocks.length >= 500) break;
  }

  const clipping = [];
  for (const element of Array.from(document.querySelectorAll('body *'))) {
    if (excluded.has(element.tagName) || !effectiveVisible(element)) continue;
    const text = normalize(Array.from(element.childNodes)
      .filter(child => child.nodeType === Node.TEXT_NODE)
      .map(child => child.textContent).join(' '));
    if (!text) continue;
    const style = getComputedStyle(element);
    const clipsX = ['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowX);
    const clipsY = ['hidden', 'clip', 'scroll', 'auto'].includes(style.overflowY);
    const clipped = (clipsX && element.scrollWidth > element.clientWidth + 2) ||
                    (clipsY && element.scrollHeight > element.clientHeight + 2);
    if (!clipped) continue;
    clipping.push({
      text: text.slice(0, 500),
      bbox: rectObject(element.getBoundingClientRect()),
      client_size: {width: element.clientWidth, height: element.clientHeight},
      scroll_size: {width: element.scrollWidth, height: element.scrollHeight}
    });
    if (clipping.length >= 100) break;
  }

  return {
    viewport: {width: innerWidth, height: innerHeight},
    visible_text: blocks.map(block => block.text).join('\n').slice(0, 60000),
    visible_text_blocks: blocks,
    possible_clipped_text: clipping
  };
}
"""


def configure_visible_content_browser() -> list[str]:
    configured: list[str] = []
    primary = configure_browser_library_path()
    if primary:
        configured.append(primary)
    candidates: list[Path] = []
    explicit = os.environ.get("JUDGE_PLAYWRIGHT_LIBS", "")
    if explicit:
        candidates.extend(Path(item) for item in explicit.split(":") if item)
    local_lib = Path.home() / ".local" / "lib"
    candidates.extend(
        [
            local_lib / "usr" / "lib" / "x86_64-linux-gnu",
            local_lib / "nspr" / "usr" / "lib" / "x86_64-linux-gnu",
        ]
    )
    current = [
        item for item in os.environ.get("LD_LIBRARY_PATH", "").split(":") if item
    ]
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        value = str(candidate.resolve())
        if value not in configured:
            configured.append(value)
        if value not in current:
            current.append(value)
    if current:
        os.environ["LD_LIBRARY_PATH"] = ":".join(current)
    return configured


def extract_visible_final_content(
    slides: list[Any],
    *,
    width: int,
    height: int,
) -> dict[str, Any]:
    """Extract only runtime-visible text from canonical final HTML pages."""
    dependency_library_paths = configure_visible_content_browser()
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise RunnerError(f"playwright import failed for final content audit: {exc}") from exc

    pages: list[dict[str, Any]] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
            )
            for slide in slides:
                attempted_network: list[str] = []

                def route_handler(route: Any) -> None:
                    url = route.request.url
                    if url.startswith(("file:", "data:", "blob:", "about:")):
                        route.continue_()
                    else:
                        attempted_network.append(url)
                        route.abort()

                page.route("**/*", route_handler)
                page.goto(slide.html_path.as_uri(), wait_until="load", timeout=60000)
                page.wait_for_timeout(300)
                extracted = page.evaluate(_VISIBLE_CONTENT_SCRIPT)
                page.unroute("**/*", route_handler)
                if not isinstance(extracted, dict):
                    raise RunnerError(
                        f"visible-content extraction returned invalid data for slide {slide.index}"
                    )
                pages.append(
                    {
                        "page": int(slide.index),
                        "source": f"slides/slide_{int(slide.index):02d}.html",
                        "viewport": extracted.get("viewport"),
                        "visible_text": extracted.get("visible_text", ""),
                        "visible_text_blocks": extracted.get("visible_text_blocks", []),
                        "possible_clipped_text": extracted.get("possible_clipped_text", []),
                        "attempted_network_requests": sorted(set(attempted_network)),
                    }
                )
            browser.close()
    except RunnerError:
        raise
    except Exception as exc:
        raise RunnerError(
            f"final visible-content extraction failed: {type(exc).__name__}: {exc}"
        ) from exc

    return {
        "contract_version": VISIBLE_CONTENT_CONTRACT_VERSION,
        "source_policy": (
            "Runtime-visible text nodes from final slide HTML only. Raw HTML, CSS, "
            "attributes, alt text, aria labels, hidden nodes, and intermediate artifacts are excluded."
        ),
        "page_count": len(pages),
        "canonical_order": [item["page"] for item in pages],
        "runtime_dependencies_configured": bool(dependency_library_paths),
        "pages": pages,
    }


def _validate_page_numbers(
    value: Any,
    *,
    page_count: int,
    label: str,
    allow_empty: bool,
) -> list[int]:
    if (
        not isinstance(value, list)
        or (not allow_empty and not value)
        or any(
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 1 <= item <= page_count
            for item in value
        )
    ):
        empty_note = " or empty" if allow_empty else ""
        raise RunnerError(
            f"{label} pages must be unique integers from 1 to {page_count}{empty_note}"
        )
    normalized = sorted(set(value))
    if len(normalized) != len(value):
        raise RunnerError(f"{label} pages must not repeat page numbers")
    return normalized


def validate_final_preaudit(
    value: dict[str, Any],
    *,
    audit_type: str,
    page_count: int,
) -> dict[str, Any]:
    if audit_type not in PREAUDIT_TYPES:
        raise RunnerError(f"unsupported final pre-audit type: {audit_type}")
    if not isinstance(value, dict):
        raise RunnerError(f"final {audit_type} pre-audit must be an object")
    expected_top = {
        "contract_version",
        "audit_type",
        "pages",
        "deck_strengths",
        "deck_defects",
        "deck_unverifiable",
    }
    if audit_type == "visual":
        expected_top.add("visual_metrics")
    if set(value) != expected_top:
        raise RunnerError(
            f"final {audit_type} pre-audit fields mismatch: "
            f"missing={sorted(expected_top - set(value))}, "
            f"extra={sorted(set(value) - expected_top)}"
        )
    if value["contract_version"] != FINAL_PREAUDIT_CONTRACT_VERSION:
        raise RunnerError(
            f"final {audit_type} pre-audit contract_version must be "
            f"{FINAL_PREAUDIT_CONTRACT_VERSION}"
        )
    if value["audit_type"] != audit_type:
        raise RunnerError(
            f"final pre-audit type mismatch: expected {audit_type}, got {value['audit_type']!r}"
        )
    pages = value.get("pages")
    if not isinstance(pages, list) or len(pages) != page_count:
        raise RunnerError(
            f"final {audit_type} pre-audit must contain exactly {page_count} pages"
        )
    expected_page_fields = {
        "page",
        "summary",
        "strengths",
        "defects",
        "unverifiable",
    }
    observed_pages: list[int] = []
    for index, page in enumerate(pages, start=1):
        if not isinstance(page, dict) or set(page) != expected_page_fields:
            raise RunnerError(
                f"final {audit_type} pre-audit page {index} fields mismatch"
            )
        if page["page"] != index:
            raise RunnerError(
                f"final {audit_type} pre-audit pages must be canonical; "
                f"expected {index}, got {page['page']!r}"
            )
        observed_pages.append(index)
        if not isinstance(page["summary"], str) or not page["summary"].strip():
            raise RunnerError(
                f"final {audit_type} pre-audit page {index} summary must be non-empty"
            )
        for field in ("strengths", "defects", "unverifiable"):
            items = page[field]
            if not isinstance(items, list) or any(
                not isinstance(item, str) or not item.strip() for item in items
            ):
                raise RunnerError(
                    f"final {audit_type} pre-audit page {index}.{field} "
                    "must be a string array"
                )
    if observed_pages != list(range(1, page_count + 1)):
        raise RunnerError(f"final {audit_type} pre-audit page order is invalid")
    for field in ("deck_strengths", "deck_unverifiable"):
        items = value[field]
        if not isinstance(items, list) or any(
            not isinstance(item, str) or not item.strip() for item in items
        ):
            raise RunnerError(
                f"final {audit_type} pre-audit {field} must be a string array"
            )
    deck_defects = value.get("deck_defects")
    if not isinstance(deck_defects, list):
        raise RunnerError(
            f"final {audit_type} pre-audit deck_defects must be an array"
        )
    normalized_deck_defects: list[dict[str, Any]] = []
    seen_defect_ids: set[str] = set()
    for item in deck_defects:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "description",
            "impact_scope",
            "pages",
            "improvement",
        }:
            raise RunnerError(
                f"final {audit_type} pre-audit deck defect fields mismatch"
            )
        defect_id = item.get("id")
        description = item.get("description")
        impact_scope = item.get("impact_scope")
        improvement = item.get("improvement")
        if (
            not isinstance(defect_id, str)
            or not defect_id.strip()
            or defect_id in seen_defect_ids
        ):
            raise RunnerError(
                f"final {audit_type} pre-audit deck defect id is empty or repeated"
            )
        if not isinstance(description, str) or not description.strip():
            raise RunnerError(
                f"final {audit_type} pre-audit deck defect description must be non-empty"
            )
        if impact_scope not in {"local", "non_local"}:
            raise RunnerError(
                f"final {audit_type} pre-audit deck defect impact_scope must be local or non_local"
            )
        if not isinstance(improvement, str) or not improvement.strip():
            raise RunnerError(
                f"final {audit_type} pre-audit deck defect improvement must be non-empty"
            )
        defect_pages = _validate_page_numbers(
            item.get("pages"),
            page_count=page_count,
            label=f"final {audit_type} pre-audit deck defect {defect_id}",
            allow_empty=False,
        )
        if impact_scope == "non_local" and len(defect_pages) < 2:
            impact_scope = "local"
        seen_defect_ids.add(defect_id)
        normalized_deck_defects.append(
            {
                "id": defect_id.strip(),
                "description": description.strip(),
                "impact_scope": impact_scope,
                "pages": defect_pages,
                "improvement": improvement.strip(),
            }
        )

    result = {
        "contract_version": FINAL_PREAUDIT_CONTRACT_VERSION,
        "audit_type": audit_type,
        "pages": pages,
        "deck_strengths": value["deck_strengths"],
        "deck_defects": normalized_deck_defects,
        "deck_unverifiable": value["deck_unverifiable"],
    }
    if audit_type == "visual":
        metrics = value.get("visual_metrics")
        expected_metric_fields = {
            "real_scene_or_artifact_photo_pages",
            "generic_or_low_detail_core_visual_substitution_pages",
            "repeated_layout_skeleton_groups",
            "unreadable_key_label_pages",
        }
        if not isinstance(metrics, dict) or set(metrics) != expected_metric_fields:
            raise RunnerError(
                "final visual pre-audit visual_metrics fields mismatch"
            )
        repeated_groups = metrics.get("repeated_layout_skeleton_groups")
        if not isinstance(repeated_groups, list):
            raise RunnerError(
                "final visual pre-audit repeated_layout_skeleton_groups "
                "must be an array"
            )
        normalized_groups: list[dict[str, Any]] = []
        for index, group in enumerate(repeated_groups, start=1):
            if not isinstance(group, dict) or set(group) != {
                "pages",
                "shared_skeleton",
            }:
                raise RunnerError(
                    "final visual pre-audit repeated layout group fields mismatch"
                )
            shared_skeleton = group.get("shared_skeleton")
            if not isinstance(shared_skeleton, str) or not shared_skeleton.strip():
                raise RunnerError(
                    "final visual pre-audit repeated layout shared_skeleton "
                    "must be non-empty"
                )
            group_pages = _validate_page_numbers(
                group.get("pages"),
                page_count=page_count,
                label=f"final visual pre-audit repeated layout group {index}",
                allow_empty=False,
            )
            if len(group_pages) < 2:
                raise RunnerError(
                    "final visual pre-audit repeated layout group must contain "
                    "at least two pages"
                )
            normalized_groups.append(
                {
                    "pages": group_pages,
                    "shared_skeleton": shared_skeleton.strip(),
                }
            )
        result["visual_metrics"] = {
            "real_scene_or_artifact_photo_pages": _validate_page_numbers(
                metrics.get("real_scene_or_artifact_photo_pages"),
                page_count=page_count,
                label="final visual pre-audit real photo",
                allow_empty=True,
            ),
            "generic_or_low_detail_core_visual_substitution_pages": _validate_page_numbers(
                metrics.get(
                    "generic_or_low_detail_core_visual_substitution_pages"
                ),
                page_count=page_count,
                label="final visual pre-audit generic substitution",
                allow_empty=True,
            ),
            "repeated_layout_skeleton_groups": normalized_groups,
            "unreadable_key_label_pages": _validate_page_numbers(
                metrics.get("unreadable_key_label_pages"),
                page_count=page_count,
                label="final visual pre-audit unreadable key label",
                allow_empty=True,
            ),
        }
    return result


def mock_final_preaudit(audit_type: str, page_count: int) -> dict[str, Any]:
    result = {
        "contract_version": FINAL_PREAUDIT_CONTRACT_VERSION,
        "audit_type": audit_type,
        "pages": [
            {
                "page": page,
                "summary": f"mock {audit_type} pre-audit for slide {page}",
                "strengths": [],
                "defects": [],
                "unverifiable": [],
            }
            for page in range(1, page_count + 1)
        ],
        "deck_strengths": [],
        "deck_defects": [],
        "deck_unverifiable": [],
    }
    if audit_type == "visual":
        result["visual_metrics"] = {
            "real_scene_or_artifact_photo_pages": list(
                range(1, page_count + 1)
            ),
            "generic_or_low_detail_core_visual_substitution_pages": [],
            "repeated_layout_skeleton_groups": [],
            "unreadable_key_label_pages": [],
        }
    return result
