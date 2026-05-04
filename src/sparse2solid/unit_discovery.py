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

    candidates = [candidate for candidate in candidates if candidate_passes_structural_filter(candidate)]
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
                "crop_metrics": candidate["crop_metrics"],
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
    if len(units) == 1:
        units[0]["evidence"].append("No crop proposals passed the structural filter; add clearer facade/detail images.")
    return units


def image_candidates(path: Path, image_record: dict[str, Any]) -> list[dict[str, Any]]:
    image = Image.open(path)
    image = ImageOps.exif_transpose(image).convert("RGB")
    original_w, original_h = image.size
    scale = min(1.0, DISCOVERY_WIDTH / max(1, original_w))
    work = image.resize((max(1, int(original_w * scale)), max(1, int(original_h * scale))))
    arr = np.asarray(ImageOps.grayscale(work), dtype=np.float32) / 255.0
    rgb = np.asarray(work, dtype=np.float32) / 255.0
    roi = estimate_building_roi(rgb, arr)
    gx = np.abs(np.diff(arr, axis=1))
    gy = np.abs(np.diff(arr, axis=0))
    vertical_profile = gx.mean(axis=0) if gx.size else np.asarray([0.0])
    horizontal_profile = gy.mean(axis=1) if gy.size else np.asarray([0.0])
    candidates: list[dict[str, Any]] = []

    for idx, peak in enumerate(top_peaks(vertical_profile, limit=5, min_gap=max(16, work.width // 18))):
        width = max(42, int(work.width * 0.075))
        bbox = clamp_bbox((peak - width // 2, int(work.height * 0.16), peak + width // 2, int(work.height * 0.88)), work.size)
        if overlaps_roi(bbox, roi, work.size):
            candidates.append(make_candidate(path, image, bbox, scale, "vertical_repeat", image_record, vertical_profile[peak], idx))

    for idx, peak in enumerate(top_peaks(horizontal_profile, limit=5, min_gap=max(14, work.height // 18))):
        height = max(34, int(work.height * 0.07))
        kind = "upper_edge_or_roofline" if peak < work.height * 0.36 else "horizontal_band"
        bbox = clamp_bbox((int(work.width * 0.06), peak - height // 2, int(work.width * 0.94), peak + height // 2), work.size)
        if overlaps_roi(bbox, roi, work.size):
            candidates.append(make_candidate(path, image, bbox, scale, kind, image_record, horizontal_profile[peak], idx))

    for idx, bbox in enumerate(dark_region_boxes(arr, work.size)[:5]):
        if overlaps_roi(bbox, roi, work.size):
            candidates.append(make_candidate(path, image, bbox, scale, "opening_or_shadow_region", image_record, 0.55, idx))

    return candidates


def estimate_building_roi(rgb: np.ndarray, gray: np.ndarray) -> tuple[int, int, int, int]:
    height, width = gray.shape
    maxc = rgb.max(axis=2)
    minc = rgb.min(axis=2)
    saturation = maxc - minc
    red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    green_bias = (green - red) + (green - blue)
    vegetation = (green >= red) & (green >= blue) & (green_bias > 0.045) & (saturation > 0.035)
    warm_flowers = (red > green * 1.03) & (red > blue * 1.03) & (saturation > 0.12)
    neutral_or_dark = (saturation < 0.24) & (gray > 0.16)
    structural = neutral_or_dark & ~(vegetation | warm_flowers)
    row_score = structural.mean(axis=1)
    col_score = structural.mean(axis=0)
    row_threshold = max(0.18, float(np.percentile(row_score, 68)))
    col_threshold = max(0.10, float(np.percentile(col_score, 58)))
    rows = np.where(row_score >= row_threshold)[0]
    cols = np.where(col_score >= col_threshold)[0]
    if rows.size < max(8, height * 0.08) or cols.size < max(8, width * 0.12):
        return (0, 0, width, height)
    y1, y2 = int(rows.min()), int(rows.max() + 1)
    x1, x2 = int(cols.min()), int(cols.max() + 1)
    pad_y = max(6, int(height * 0.05))
    pad_x = max(6, int(width * 0.04))
    return clamp_bbox((x1 - pad_x, y1 - pad_y, x2 + pad_x, y2 + pad_y), (width, height))


def overlaps_roi(bbox: tuple[int, int, int, int], roi: tuple[int, int, int, int], size: tuple[int, int]) -> bool:
    width, height = size
    x1, y1, x2, y2 = bbox
    rx1, ry1, rx2, ry2 = roi
    inter_x = max(0, min(x2, rx2) - max(x1, rx1))
    inter_y = max(0, min(y2, ry2) - max(y1, ry1))
    overlap = inter_x * inter_y / max(1, (x2 - x1) * (y2 - y1))
    cx = (x1 + x2) / 2
    cy = (y1 + y2) / 2
    center_inside = rx1 <= cx <= rx2 and ry1 <= cy <= ry2
    not_foreground_strip = cy < height * 0.88 or (y2 - y1) > height * 0.20
    meaningful_width = (x2 - x1) > width * 0.025
    meaningful_height = (y2 - y1) > height * 0.025
    return overlap >= 0.50 and center_inside and not_foreground_strip and meaningful_width and meaningful_height


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
    metrics = crop_metrics(image.crop(source_bbox))
    return {
        "kind": kind,
        "source_image": path.name,
        "source_path": path,
        "bbox": [x1, y1, x2, y2],
        "bbox_normalized": [round(x1 / width, 4), round(y1 / height, 4), round(x2 / width, 4), round(y2 / height, 4)],
        "quality": image_record.get("image_quality_score", 0),
        "score": float(score) + image_record.get("image_quality_score", 0) * 0.20 + metrics["structural_score"] * 0.45,
        "structural_score": metrics["structural_score"],
        "crop_metrics": metrics,
        "sort_key": (kind, round((x1 + x2) / max(1, width) / 2, 1), round((y1 + y2) / max(1, height) / 2, 1), round((x2 - x1) / max(1, width), 2), round((y2 - y1) / max(1, height), 2)),
        "evidence": [
            f"source image {path.name}",
            f"bbox {x1},{y1},{x2},{y2}",
            f"visual kind {kind.replace('_', ' ')}",
            f"structural score {metrics['structural_score']:.2f}",
        ],
        "index": index,
    }


def crop_metrics(crop: Image.Image) -> dict[str, float]:
    crop = crop.convert("RGB")
    crop.thumbnail((220, 220))
    rgb = np.asarray(crop, dtype=np.float32) / 255.0
    gray = np.asarray(ImageOps.grayscale(crop), dtype=np.float32) / 255.0
    if rgb.size == 0 or gray.size == 0:
        return {"structural_score": 0.0}
    maxc = rgb.max(axis=2)
    minc = rgb.min(axis=2)
    saturation = maxc - minc
    brightness = gray
    red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    green_bias = (green - red) + (green - blue)
    vegetation = (green >= red) & (green >= blue) & (green_bias > 0.045) & (saturation > 0.035)
    flower_warm = (red > green * 1.03) & (red > blue * 1.03) & (saturation > 0.12)
    flower_pink = (red > 0.38) & (blue > 0.30) & (green < 0.62) & (saturation > 0.10)
    grass = vegetation & (brightness > 0.18) & (brightness < 0.78)
    neutral_or_masonry = (saturation < 0.22) & (brightness > 0.18)
    dark_opening = (brightness < 0.30) & (saturation < 0.20)
    gx = np.abs(np.diff(gray, axis=1))
    gy = np.abs(np.diff(gray, axis=0))
    edge_density = float(((gx > 0.10).mean() + (gy > 0.10).mean()) / 2.0) if gx.size and gy.size else 0.0
    vertical_strength = float(gx.mean()) if gx.size else 0.0
    horizontal_strength = float(gy.mean()) if gy.size else 0.0
    axis_score = min(1.0, (vertical_strength + horizontal_strength) * 8.0)
    organic_ratio = float((vegetation | flower_warm | flower_pink | grass).mean())
    structural_color_ratio = float((neutral_or_masonry | dark_opening).mean())
    color_score = max(0.0, min(1.0, structural_color_ratio * 1.25 - organic_ratio * 1.45))
    edge_score = min(1.0, edge_density * 5.0 + axis_score * 0.35)
    structural_score = max(0.0, min(1.0, color_score * 0.58 + edge_score * 0.42))
    return {
        "structural_score": round(structural_score, 3),
        "organic_ratio": round(organic_ratio, 3),
        "structural_color_ratio": round(structural_color_ratio, 3),
        "edge_density": round(edge_density, 3),
        "axis_score": round(axis_score, 3),
        "mean_saturation": round(float(saturation.mean()), 3),
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


def candidate_passes_structural_filter(candidate: dict[str, Any]) -> bool:
    metrics = candidate["crop_metrics"]
    kind = candidate["kind"]
    if metrics["structural_score"] < 0.58:
        return False
    organic_limit = {
        "opening_or_shadow_region": 0.46,
        "vertical_repeat": 0.30,
        "horizontal_band": 0.34,
        "upper_edge_or_roofline": 0.42,
    }.get(kind, 0.34)
    if metrics["organic_ratio"] > organic_limit:
        return False
    x1, y1, x2, y2 = candidate["bbox_normalized"]
    width = x2 - x1
    height = y2 - y1
    if width < 0.018 or height < 0.025:
        return False
    if kind == "vertical_repeat" and width > 0.18:
        return False
    if kind == "opening_or_shadow_region" and (height < 0.08 or width < 0.035):
        return False
    return True


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
