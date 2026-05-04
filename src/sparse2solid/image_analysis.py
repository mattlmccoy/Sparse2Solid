from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageOps


IMAGE_SIZE = 320


def analyze_image(path: Path) -> dict[str, Any]:
    record: dict[str, Any] = {
        "name": path.name,
        "path": str(path),
        "analyzable": False,
    }
    try:
        image = Image.open(path)
        image = ImageOps.exif_transpose(image).convert("RGB")
    except Exception as exc:  # noqa: BLE001 - invalid user files should not crash a project.
        record["error"] = str(exc)
        return record

    width, height = image.size
    thumb = image.copy()
    thumb.thumbnail((IMAGE_SIZE, IMAGE_SIZE))
    gray = ImageOps.grayscale(thumb)
    arr = np.asarray(gray, dtype=np.float32) / 255.0
    rgb = np.asarray(thumb, dtype=np.float32) / 255.0

    gx = np.abs(np.diff(arr, axis=1))
    gy = np.abs(np.diff(arr, axis=0))
    vertical_profile = gx.mean(axis=0) if gx.size else np.asarray([0.0])
    horizontal_profile = gy.mean(axis=1) if gy.size else np.asarray([0.0])
    vertical_strength = float(vertical_profile.mean())
    horizontal_strength = float(horizontal_profile.mean())
    edge_density = float((gx > 0.12).mean()) if gx.size else 0.0
    vertical_peak_count = count_profile_peaks(vertical_profile)
    horizontal_peak_count = count_profile_peaks(horizontal_profile)
    saturation = float((rgb.max(axis=2) - rgb.min(axis=2)).mean())
    brightness = float(arr.mean())

    record.update(
        {
            "analyzable": True,
            "width": width,
            "height": height,
            "aspect_ratio": round(width / max(1, height), 3),
            "orientation": "landscape" if width >= height else "portrait",
            "brightness": round(brightness, 3),
            "saturation": round(saturation, 3),
            "edge_density": round(edge_density, 3),
            "vertical_edge_strength": round(vertical_strength, 3),
            "horizontal_edge_strength": round(horizontal_strength, 3),
            "vertical_peak_count": vertical_peak_count,
            "horizontal_peak_count": horizontal_peak_count,
            "facade_rhythm_score": round(min(1.0, vertical_peak_count / 18.0 + vertical_strength * 2.6), 3),
            "roofline_score": round(min(1.0, horizontal_peak_count / 12.0 + horizontal_strength * 2.0), 3),
            "image_quality_score": round(min(1.0, 0.25 + edge_density * 1.8 + min(width, height) / 2400.0), 3),
        }
    )
    return record


def count_profile_peaks(profile: np.ndarray) -> int:
    if profile.size < 5:
        return 0
    mean = float(profile.mean())
    std = float(profile.std())
    threshold = mean + std * 0.85
    peaks = 0
    cooldown = 0
    for idx in range(1, profile.size - 1):
        if cooldown:
            cooldown -= 1
            continue
        if profile[idx] > threshold and profile[idx] >= profile[idx - 1] and profile[idx] >= profile[idx + 1]:
            peaks += 1
            cooldown = 4
    return peaks


def analyze_images(paths: list[Path]) -> dict[str, Any]:
    images = [analyze_image(path) for path in paths]
    valid = [item for item in images if item.get("analyzable")]
    def avg(key: str) -> float:
        return round(float(np.mean([item[key] for item in valid])), 3) if valid else 0.0

    wide_count = sum(1 for item in valid if item.get("aspect_ratio", 1) >= 1.45)
    portrait_count = sum(1 for item in valid if item.get("orientation") == "portrait")
    summary = {
        "image_count": len(images),
        "analyzable_count": len(valid),
        "wide_count": wide_count,
        "portrait_count": portrait_count,
        "average_aspect_ratio": avg("aspect_ratio"),
        "average_quality": avg("image_quality_score"),
        "average_facade_rhythm": avg("facade_rhythm_score"),
        "average_roofline": avg("roofline_score"),
        "average_vertical_peaks": avg("vertical_peak_count"),
        "average_horizontal_peaks": avg("horizontal_peak_count"),
        "view_diversity_score": round(min(1.0, (wide_count > 0) * 0.25 + (portrait_count > 0) * 0.15 + min(len(valid), 10) * 0.06), 3),
    }
    return {"summary": summary, "images": images}


def write_analysis(project_dir: Path, image_paths: list[Path]) -> dict[str, Any]:
    analysis = analyze_images(image_paths)
    out_dir = project_dir / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    analysis_path = out_dir / "image_analysis.json"
    analysis_path.write_text(json.dumps(analysis, indent=2) + "\n", encoding="utf-8")
    contact_path = out_dir / "image_contact_sheet.jpg"
    make_contact_sheet([path for path in image_paths if path.exists()], contact_path)
    analysis["analysis_path"] = "analysis/image_analysis.json"
    analysis["contact_sheet"] = "analysis/image_contact_sheet.jpg"
    return analysis


def make_contact_sheet(paths: list[Path], out_path: Path) -> None:
    thumbs: list[Image.Image] = []
    labels: list[str] = []
    for path in paths[:48]:
        try:
            image = Image.open(path)
            image = ImageOps.exif_transpose(image).convert("RGB")
        except Exception:
            continue
        image.thumbnail((220, 150))
        thumbs.append(image.copy())
        labels.append(path.name[:28])
    if not thumbs:
        sheet = Image.new("RGB", (700, 220), (246, 241, 234))
        draw = ImageDraw.Draw(sheet)
        draw.text((28, 92), "No analyzable images yet", fill=(90, 82, 74))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(out_path)
        return
    columns = min(4, len(thumbs))
    rows = math.ceil(len(thumbs) / columns)
    cell_w, cell_h = 250, 190
    sheet = Image.new("RGB", (columns * cell_w, rows * cell_h), (246, 241, 234))
    draw = ImageDraw.Draw(sheet)
    for idx, image in enumerate(thumbs):
        x = (idx % columns) * cell_w + (cell_w - image.width) // 2
        y = (idx // columns) * cell_h + 18
        sheet.paste(image, (x, y))
        draw.text(((idx % columns) * cell_w + 18, (idx // columns) * cell_h + 158), labels[idx], fill=(68, 64, 60))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
