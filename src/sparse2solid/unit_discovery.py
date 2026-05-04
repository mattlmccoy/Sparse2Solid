from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageOps


DISCOVERY_WIDTH = 900


def discover_visual_units(project_dir: Path, analysis: dict[str, Any], image_paths: list[Path]) -> list[dict[str, Any]]:
    """Find anonymous visual unit proposals from image pixels.

    This deliberately avoids architecture-specific names. A proposal is a visual
    repeat, band, opening-like patch, or envelope crop with source evidence.
    Semantic names should come later from user review or a stronger vision model.
    """

    crop_dir = project_dir / "components" / "crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    summary = analysis["summary"]
    candidates: list[dict[str, Any]] = []

    for image_record in sorted(
        [item for item in analysis["images"] if item.get("analyzable")],
        key=lambda item: item.get("image_quality_score", 0),
        reverse=True,
    )[:8]:
        path = next((candidate for candidate in image_paths if candidate.name == image_record["name"]), None)
        if not path:
            continue
        candidates.extend(image_candidates(path, image_record))

    candidates = select_diverse_candidates(dedupe_candidates(candidates))
    units: list[dict[str, Any]] = []
    units.append(reference_envelope_unit(summary))
    for candidate in candidates[:10]:
        index = len(units) + 1
        unit_id = f"visual_unit_{index:03d}_{candidate['kind']}"
        crop_path = crop_dir / f"{unit_id}.jpg"
        write_candidate_crop(candidate, crop_path)
        confidence = confidence_from_candidate(candidate, summary)
        units.append(
            {
                "component": unit_id,
                "label": label_for_kind(candidate["kind"], index),
                "why": "Detected directly from repeated edges, dark regions, or horizontal/vertical bands in uploaded images.",
                "role": "visual_proposal",
                "kind": candidate["kind"],
                "confidence": round(confidence, 2),
                "status": "ready_for_unit_draft" if confidence >= 0.50 else "needs_more_images",
                "evidence": candidate["evidence"],
                "source_image": candidate["source_image"],
                "bbox": candidate["bbox"],
                "bbox_normalized": candidate["bbox_normalized"],
                "crop_path": crop_path.relative_to(project_dir).as_posix(),
                "matched_images": [
                    {
                        "name": candidate["source_image"],
                        "quality": candidate["quality"],
                        "score": round(candidate["score"], 3),
                    }
                ],
                "needs": needs_for_kind(candidate["kind"]),
                "priority": index,
                "output_folder": f"outputs/units/{unit_id}",
            }
        )
    return units


def image_candidates(path: Path, image_record: dict[str, Any]) -> list[dict[str, Any]]:
    image = Image.open(path)
    image = ImageOps.exif_transpose(image).convert("RGB")
    original_w, original_h = image.size
    scale = min(1.0, DISCOVERY_WIDTH / max(1, original_w))
    work = image.resize((max(1, int(original_w * scale)), max(1, int(original_h * scale))))
    arr = np.asarray(ImageOps.grayscale(work), dtype=np.float32) / 255.0
    gx = np.abs(np.diff(arr, axis=1))
    gy = np.abs(np.diff(arr, axis=0))
    vertical_profile = gx.mean(axis=0) if gx.size else np.asarray([0.0])
    horizontal_profile = gy.mean(axis=1) if gy.size else np.asarray([0.0])
    candidates: list[dict[str, Any]] = []

    for idx, peak in enumerate(top_peaks(vertical_profile, limit=5, min_gap=max(16, work.width // 18))):
        width = max(42, int(work.width * 0.075))
        bbox = clamp_bbox((peak - width // 2, int(work.height * 0.16), peak + width // 2, int(work.height * 0.88)), work.size)
        candidates.append(make_candidate(path, image, bbox, scale, "vertical_repeat", image_record, vertical_profile[peak], idx))

    for idx, peak in enumerate(top_peaks(horizontal_profile, limit=5, min_gap=max(14, work.height // 18))):
        height = max(34, int(work.height * 0.07))
        kind = "upper_edge_or_roofline" if peak < work.height * 0.36 else "horizontal_band"
        bbox = clamp_bbox((int(work.width * 0.06), peak - height // 2, int(work.width * 0.94), peak + height // 2), work.size)
        candidates.append(make_candidate(path, image, bbox, scale, kind, image_record, horizontal_profile[peak], idx))

    for idx, bbox in enumerate(dark_region_boxes(arr, work.size)[:5]):
        candidates.append(make_candidate(path, image, bbox, scale, "opening_or_shadow_region", image_record, 0.55, idx))

    return candidates


def make_candidate(
    path: Path,
    image: Image.Image,
    work_bbox: tuple[int, int, int, int],
    scale: float,
    kind: str,
    image_record: dict[str, Any],
    score: float,
    index: int,
) -> dict[str, Any]:
    source_bbox = tuple(int(round(value / max(scale, 1e-6))) for value in work_bbox)
    width, height = image.size
    x1, y1, x2, y2 = source_bbox
    return {
        "kind": kind,
        "source_image": path.name,
        "source_path": path,
        "bbox": [x1, y1, x2, y2],
        "bbox_normalized": [round(x1 / width, 4), round(y1 / height, 4), round(x2 / width, 4), round(y2 / height, 4)],
        "quality": image_record.get("image_quality_score", 0),
        "score": float(score) + image_record.get("image_quality_score", 0) * 0.25,
        "sort_key": (kind, round((x2 - x1) / max(1, width), 2), round((y2 - y1) / max(1, height), 2)),
        "evidence": [
            f"source image {path.name}",
            f"bbox {x1},{y1},{x2},{y2}",
            f"visual kind {kind.replace('_', ' ')}",
        ],
        "index": index,
    }


def top_peaks(profile: np.ndarray, limit: int, min_gap: int) -> list[int]:
    if profile.size < 5:
        return []
    threshold = float(profile.mean() + profile.std() * 0.75)
    peaks = [
        idx
        for idx in range(1, profile.size - 1)
        if profile[idx] > threshold and profile[idx] >= profile[idx - 1] and profile[idx] >= profile[idx + 1]
    ]
    peaks = sorted(peaks, key=lambda idx: float(profile[idx]), reverse=True)
    selected: list[int] = []
    for peak in peaks:
        if all(abs(peak - existing) >= min_gap for existing in selected):
            selected.append(peak)
        if len(selected) >= limit:
            break
    return sorted(selected)


def dark_region_boxes(arr: np.ndarray, size: tuple[int, int]) -> list[tuple[int, int, int, int]]:
    width, height = size
    mask = arr < max(0.38, float(arr.mean() - arr.std() * 0.25))
    visited = np.zeros(mask.shape, dtype=bool)
    boxes: list[tuple[int, int, int, int, int]] = []
    for y in range(int(height * 0.18), int(height * 0.90), 4):
        for x in range(int(width * 0.06), int(width * 0.94), 4):
            if visited[y, x] or not mask[y, x]:
                continue
            x1, y1, x2, y2, area = flood_box(mask, visited, x, y)
            bw, bh = x2 - x1, y2 - y1
            if area < 80 or bw < 18 or bh < 24:
                continue
            aspect = bh / max(1, bw)
            if 0.8 <= aspect <= 5.5 and bh < height * 0.62 and bw < width * 0.28:
                boxes.append((x1, y1, x2, y2, area))
    boxes = sorted(boxes, key=lambda item: item[4], reverse=True)
    return [clamp_bbox(item[:4], size) for item in boxes[:8]]


def flood_box(mask: np.ndarray, visited: np.ndarray, start_x: int, start_y: int) -> tuple[int, int, int, int, int]:
    height, width = mask.shape
    stack = [(start_x, start_y)]
    visited[start_y, start_x] = True
    x1 = x2 = start_x
    y1 = y2 = start_y
    area = 0
    while stack and area < 20000:
        x, y = stack.pop()
        area += 1
        x1, x2 = min(x1, x), max(x2, x)
        y1, y2 = min(y1, y), max(y2, y)
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height and not visited[ny, nx] and mask[ny, nx]:
                visited[ny, nx] = True
                stack.append((nx, ny))
    return x1, y1, x2 + 1, y2 + 1, area


def clamp_bbox(bbox: tuple[int, int, int, int], size: tuple[int, int]) -> tuple[int, int, int, int]:
    width, height = size
    x1, y1, x2, y2 = bbox
    x1 = max(0, min(width - 2, x1))
    y1 = max(0, min(height - 2, y1))
    x2 = max(x1 + 2, min(width, x2))
    y2 = max(y1 + 2, min(height, y2))
    return x1, y1, x2, y2


def dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    best: dict[tuple[Any, ...], dict[str, Any]] = {}
    for candidate in sorted(candidates, key=lambda item: item["score"], reverse=True):
        key = candidate["sort_key"]
        if key not in best:
            best[key] = candidate
    return sorted(best.values(), key=lambda item: -item["score"])


def select_diverse_candidates(candidates: list[dict[str, Any]], limit: int = 10) -> list[dict[str, Any]]:
    quotas = {
        "opening_or_shadow_region": 3,
        "vertical_repeat": 3,
        "horizontal_band": 2,
        "upper_edge_or_roofline": 2,
    }
    selected: list[dict[str, Any]] = []
    for kind, quota in quotas.items():
        selected.extend([candidate for candidate in candidates if candidate["kind"] == kind][:quota])
    if len(selected) < limit:
        seen = {id(candidate) for candidate in selected}
        selected.extend([candidate for candidate in candidates if id(candidate) not in seen][: limit - len(selected)])
    kind_order = {kind: idx for idx, kind in enumerate(quotas)}
    return sorted(selected[:limit], key=lambda item: (kind_order.get(item["kind"], 9), -item["score"]))


def write_candidate_crop(candidate: dict[str, Any], crop_path: Path) -> None:
    image = Image.open(candidate["source_path"])
    image = ImageOps.exif_transpose(image).convert("RGB")
    crop = image.crop(tuple(candidate["bbox"]))
    max_side = 520
    scale = min(1.0, max_side / max(crop.size))
    if scale < 1.0:
        crop = crop.resize((max(1, int(crop.width * scale)), max(1, int(crop.height * scale))))
    crop_path.parent.mkdir(parents=True, exist_ok=True)
    crop.save(crop_path, quality=92)


def reference_envelope_unit(summary: dict[str, Any]) -> dict[str, Any]:
    n = summary.get("analyzable_count", 0)
    confidence = min(0.82, 0.15 + n * 0.04 + summary.get("view_diversity_score", 0) * 0.24 + summary.get("average_quality", 0) * 0.22)
    return {
        "component": "visual_unit_000_reference_envelope",
        "label": "Reference envelope / massing scaffold",
        "why": "A neutral bounding scaffold derived from image proportions; not a semantic building part.",
        "role": "scaffold",
        "kind": "reference_envelope",
        "confidence": round(confidence, 2),
        "status": "ready_for_unit_draft" if confidence >= 0.48 else "needs_more_images",
        "evidence": [
            f"{n} analyzable image(s)",
            f"average aspect ratio {summary.get('average_aspect_ratio', 0)}",
            f"view diversity {summary.get('view_diversity_score', 0)}",
        ],
        "matched_images": [],
        "needs": ["known scale or approximate dimensions", "one image with full height and base visible"],
        "priority": 0,
        "output_folder": "outputs/units/visual_unit_000_reference_envelope",
    }


def confidence_from_candidate(candidate: dict[str, Any], summary: dict[str, Any]) -> float:
    x1, y1, x2, y2 = candidate["bbox_normalized"]
    area = max(0.001, (x2 - x1) * (y2 - y1))
    repeat_bonus = min(0.18, summary.get("average_facade_rhythm", 0) * 0.12)
    quality_bonus = candidate["quality"] * 0.22
    score_bonus = min(0.18, candidate["score"] * 1.4)
    size_bonus = min(0.14, math.sqrt(area) * 0.35)
    return min(0.86, 0.26 + repeat_bonus + quality_bonus + score_bonus + size_bonus)


def label_for_kind(kind: str, index: int) -> str:
    labels = {
        "vertical_repeat": "Visual unit: repeated vertical strip",
        "horizontal_band": "Visual unit: horizontal band",
        "upper_edge_or_roofline": "Visual unit: upper edge / roofline band",
        "opening_or_shadow_region": "Visual unit: opening or shadow region",
    }
    return f"{labels.get(kind, 'Visual unit proposal')} #{index:03d}"


def needs_for_kind(kind: str) -> list[str]:
    common = ["confirm this crop is permanent structure", "add oblique/side view if depth matters"]
    if kind == "opening_or_shadow_region":
        return ["one clearer crop of this opening/detail", *common]
    if kind == "vertical_repeat":
        return ["one full-height crop of this repeated element", *common]
    if kind == "upper_edge_or_roofline":
        return ["side or oblique image showing roof/edge depth", *common]
    return ["closer crop showing profile/depth", *common]
