from __future__ import annotations

import argparse
import json
import re
import time
import webbrowser
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory
from PIL import Image, ImageOps
from werkzeug.datastructures import FileStorage

from .connectivity import structural_connectivity
from .geometry import MeshPart, box_part, cylinder_part, export_parts
from .image_analysis import write_analysis
from .orbit import render_orbit_set
from .reference_planner import plan_reference_set
from .unit_discovery import discover_visual_units


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic"}

def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or f"project-{int(time.time())}"


def project_manifest(project_dir: Path) -> dict[str, Any]:
    manifest_path = project_dir / "project.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "slug": project_dir.name,
        "name": project_dir.name.replace("-", " ").title(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "images": [],
        "outputs": {},
        "events": [],
    }


def write_manifest(project_dir: Path, manifest: dict[str, Any]) -> None:
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "project.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def event(manifest: dict[str, Any], message: str) -> None:
    manifest.setdefault("events", []).insert(
        0,
        {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "message": message,
        },
    )
    manifest["events"] = manifest["events"][:20]


def invalidate_derived_outputs(manifest: dict[str, Any], reason: str) -> None:
    manifest.pop("reference_plan", None)
    manifest.pop("component_plan", None)
    manifest["outputs"] = {}
    event(manifest, reason)


def invalidate_model_outputs(manifest: dict[str, Any], reason: str) -> None:
    manifest.pop("component_plan", None)
    manifest["outputs"] = {}
    event(manifest, reason)


def safe_project(root: Path, slug: str) -> Path:
    candidate = (root / slugify(slug)).resolve()
    if root.resolve() not in candidate.parents and candidate != root.resolve():
        raise ValueError("invalid project path")
    return candidate


def image_records(project_dir: Path) -> list[dict[str, str]]:
    image_dir = project_dir / "images"
    records = []
    if image_dir.exists():
        for path in sorted(image_dir.iterdir()):
            if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                records.append(
                    {
                        "name": path.name,
                        "path": f"images/{path.name}",
                        "url": f"/api/projects/{project_dir.name}/files/images/{path.name}",
                    }
                )
    return records


def save_uploads(files: list[FileStorage], image_dir: Path) -> list[str]:
    image_dir.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for uploaded in files:
        if not uploaded.filename:
            continue
        original = Path(uploaded.filename).name
        suffix = Path(original).suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            continue
        stem = slugify(Path(original).stem)
        target = image_dir / f"{stem}{suffix}"
        counter = 2
        while target.exists():
            target = image_dir / f"{stem}-{counter}{suffix}"
            counter += 1
        uploaded.save(target)
        saved.append(target.name)
    return saved


def training_examples(project_dir: Path) -> list[dict[str, Any]]:
    path = project_dir / "components" / "training_examples.jsonl"
    if not path.exists():
        return []
    examples: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            examples.append(json.loads(line))
        except ValueError:
            continue
    return examples


def save_training_example(project_dir: Path, payload: dict[str, Any]) -> dict[str, Any]:
    images = {record["name"]: project_dir / record["path"] for record in image_records(project_dir)}
    image_name = str(payload.get("image_name") or "")
    if image_name not in images:
        raise ValueError("Choose one of the uploaded images.")
    label = str(payload.get("label") or "structure").strip().lower()
    allowed_labels = {"structure", "opening", "window", "door", "vertical", "support", "column", "band", "rail", "roofline", "roof", "ignore"}
    if label not in allowed_labels:
        raise ValueError("Unsupported crop label.")
    bbox = payload.get("bbox_normalized")
    if not isinstance(bbox, list) or len(bbox) != 4:
        raise ValueError("Draw a crop box before saving.")
    try:
        x1, y1, x2, y2 = [max(0.0, min(1.0, float(value))) for value in bbox]
    except (TypeError, ValueError) as exc:
        raise ValueError("Crop box coordinates must be numeric.") from exc
    if x2 - x1 < 0.015 or y2 - y1 < 0.015:
        raise ValueError("Crop box is too small to teach the detector.")
    examples = training_examples(project_dir)
    example_id = f"example_{len(examples) + 1:04d}"
    example = {
        "id": example_id,
        "image_name": image_name,
        "label": label,
        "bbox_normalized": [round(x1, 5), round(y1, 5), round(x2, 5), round(y2, 5)],
        "notes": str(payload.get("notes") or "").strip(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    crop_path = write_training_crop(project_dir, images[image_name], example)
    example["crop_path"] = crop_path.relative_to(project_dir).as_posix()
    out_path = project_dir / "components" / "training_examples.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(example, sort_keys=True) + "\n")
    return example


def write_training_crop(project_dir: Path, image_path: Path, example: dict[str, Any]) -> Path:
    image = Image.open(image_path)
    image = ImageOps.exif_transpose(image).convert("RGB")
    width, height = image.size
    x1, y1, x2, y2 = example["bbox_normalized"]
    crop = image.crop((int(x1 * width), int(y1 * height), int(x2 * width), int(y2 * height)))
    crop.thumbnail((420, 420))
    crop_dir = project_dir / "components" / "training_crops"
    crop_dir.mkdir(parents=True, exist_ok=True)
    crop_path = crop_dir / f"{example['id']}_{example['label']}.jpg"
    crop.save(crop_path, quality=92)
    return crop_path


def component_plan(project_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    image_paths = [project_dir / record["path"] for record in image_records(project_dir)]
    analysis = write_analysis(project_dir, image_paths)
    summary = analysis["summary"]
    planned = discover_visual_units(project_dir, analysis, image_paths)
    ignored = [
        {
            "category": "vegetation / people / temporary decor",
            "reason": "Usually treated as context, not reconstruction targets, unless the user explicitly promotes them.",
        }
    ]
    return {
        "project": manifest.get("name") or project_dir.name,
        "image_count": summary["image_count"],
        "analyzable_image_count": summary["analyzable_count"],
        "strategy": "pixel_evidence_unit_discovery",
        "image_analysis": analysis,
        "components": planned,
        "ignored_by_default": ignored,
        "next_step": "Review visual crop proposals, build draft 2.5D units only for accepted/ready crops, then assemble from their image positions.",
    }


def write_component_plan(project_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    plan = component_plan(project_dir, manifest)
    out_dir = project_dir / "components"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "component_plan.json"
    path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    manifest["component_plan"] = "components/component_plan.json"
    return plan


def build_unit_outputs(project_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    plan_path = project_dir / str(manifest.get("component_plan", "components/component_plan.json"))
    if not plan_path.exists():
        plan = write_component_plan(project_dir, manifest)
    else:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    units = []
    for component in plan["components"]:
        if component["status"] != "ready_for_unit_draft":
            units.append({**component, "built": False, "reason": "Needs more targeted images before a useful unit draft."})
            continue
        parts = generic_unit_parts(component, plan.get("image_analysis", {}).get("summary", {}))
        if not parts:
            units.append({**component, "built": False, "reason": "No generic component generator exists yet."})
            continue
        out_dir = project_dir / "outputs" / "units" / component["component"]
        outputs = export_parts(out_dir, component["component"], parts)
        orbit = render_orbit_set(Path(outputs["obj"]), Path(outputs["mtl"]), out_dir / "orbits", title=f"{component['label']} unit orbit", frame_count=8)
        units.append(
            {
                **component,
                "built": True,
                "purpose": "Generic image-conditioned draft unit; refine after user review and targeted references.",
                "outputs": relativize_report(project_dir, outputs),
                "orbit": relativize_report(project_dir, orbit),
                "part_count": len(parts),
            }
        )
    report = {
        "unit_count": len(units),
        "built_count": sum(1 for unit in units if unit.get("built")),
        "units": units,
        "notes": "Units are drafted independently first. Assembly should wait until important units have enough image evidence.",
    }
    report_dir = project_dir / "outputs" / "units"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "unit_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    manifest.setdefault("outputs", {})["units"] = relativize_report(project_dir, {**report, "report": str(report_path)})
    return manifest["outputs"]["units"]


def generic_unit_parts(component: dict[str, Any], summary: dict[str, Any]) -> list[MeshPart]:
    aspect = max(1.2, min(6.0, float(summary.get("average_aspect_ratio") or 2.2)))
    kind = component.get("kind", "")
    if kind == "reference_envelope":
        width = 4.0 + aspect * 2.2
        height = 2.6 + min(2.2, float(summary.get("average_roofline") or 0.3) * 1.7)
        depth = 1.6 + float(summary.get("view_diversity_score") or 0.2) * 2.6
        return [
            box_part("scaffold_main_volume_low_detail", "limestone", (width, depth, height), (0, 0, height / 2)),
            box_part("scaffold_front_reference_plane", "shadow", (width * 0.94, 0.05, height * 0.72), (0, -depth / 2 - 0.04, height * 0.45)),
        ]
    bbox = component.get("bbox_normalized") or [0.25, 0.25, 0.75, 0.75]
    bw = max(0.04, float(bbox[2]) - float(bbox[0]))
    bh = max(0.04, float(bbox[3]) - float(bbox[1]))
    unit_w = max(0.35, min(2.8, bw * 5.5))
    unit_h = max(0.28, min(3.2, bh * 4.5))
    depth = max(0.08, min(0.55, 0.12 + bw * 0.55))
    if kind == "opening_or_shadow_region":
        mullion_count = max(1, min(4, int(round(unit_w * 2.0))))
        parts = [
            box_part("crop_backing_shadow", "shadow", (unit_w, 0.05, unit_h), (0, -depth / 2, unit_h / 2)),
            box_part("crop_frame_left", "limestone", (unit_w * 0.08, depth, unit_h), (-unit_w / 2, 0, unit_h / 2)),
            box_part("crop_frame_right", "limestone", (unit_w * 0.08, depth, unit_h), (unit_w / 2, 0, unit_h / 2)),
            box_part("crop_frame_top", "limestone", (unit_w * 1.08, depth, unit_h * 0.07), (0, 0, unit_h)),
            box_part("crop_frame_bottom", "limestone", (unit_w * 1.08, depth, unit_h * 0.07), (0, 0, 0.04)),
        ]
        for idx, x in enumerate(np_linspace(-unit_w * 0.32, unit_w * 0.32, mullion_count)):
            parts.append(box_part(f"crop_internal_divider_{idx:02d}", "limestone", (unit_w * 0.035, depth * 0.7, unit_h * 0.86), (x, -depth * 0.15, unit_h * 0.50)))
        return parts
    if kind == "vertical_repeat":
        parts = [
            box_part("vertical_crop_foot", "stone", (unit_w * 1.4, depth * 1.4, unit_h * 0.05), (0, 0, unit_h * 0.025)),
            box_part("vertical_crop_shaft_proxy", "limestone", (unit_w * 0.42, depth, unit_h * 0.86), (0, 0, unit_h * 0.48)),
            box_part("vertical_crop_cap", "limestone", (unit_w * 1.15, depth * 1.1, unit_h * 0.07), (0, 0, unit_h * 0.93)),
        ]
        return parts
    if kind in {"horizontal_band", "upper_edge_or_roofline"}:
        material = "roof" if kind == "upper_edge_or_roofline" else "limestone"
        tick_count = max(3, min(9, int(round(unit_w * 3.2))))
        parts = [
            box_part("band_main_crop_proxy", material, (unit_w, depth, unit_h), (0, 0, unit_h / 2)),
            box_part("band_lower_shadow_from_crop", "shadow", (unit_w * 0.96, depth * 0.35, unit_h * 0.12), (0, -depth * 0.45, unit_h * 0.18)),
        ]
        for idx, x in enumerate(np_linspace(-unit_w * 0.42, unit_w * 0.42, tick_count)):
            parts.append(box_part(f"band_tick_from_edge_{idx:02d}", "limestone", (unit_w * 0.035, depth * 0.8, unit_h * 0.35), (x, -depth * 0.1, unit_h * 0.48)))
        return parts
    return [
        box_part("visual_crop_volume_proxy", "limestone", (unit_w, depth, unit_h), (0, 0, unit_h / 2)),
    ]


def np_linspace(start: float, stop: float, count: int) -> list[float]:
    if count <= 1:
        return [(start + stop) / 2]
    step = (stop - start) / (count - 1)
    return [start + step * idx for idx in range(count)]


def build_assembly_preview(project_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    units = manifest.get("outputs", {}).get("units", {}).get("units", [])
    built_units = [unit for unit in units if unit.get("built")]
    analysis = {}
    component_path = project_dir / str(manifest.get("component_plan", "components/component_plan.json"))
    if component_path.exists():
        analysis = json.loads(component_path.read_text(encoding="utf-8")).get("image_analysis", {})
    summary = analysis.get("summary", {})
    width = 8.0 + float(summary.get("average_aspect_ratio") or 2.0) * 2.0
    height = 3.2 + min(2.0, float(summary.get("average_roofline") or 0.3) * 1.5)
    parts = [
        box_part("assembly_ground_slab", "stone", (width + 1.0, 3.2, 0.16), (0, 0, 0.08)),
        box_part("assembly_image_plane_backing", "limestone", (width, 0.18, height), (0, -1.55, height / 2 + 0.10)),
    ]
    for unit in built_units:
        unit_parts = generic_unit_parts(unit, summary)
        if unit.get("kind") == "reference_envelope":
            parts.extend([part.transformed(name_prefix="assembly_envelope__", translate=(0, 0, 0.12)) for part in unit_parts])
            continue
        bbox = unit.get("bbox_normalized") or [0.3, 0.3, 0.7, 0.7]
        cx = (float(bbox[0]) + float(bbox[2])) / 2.0
        cy = (float(bbox[1]) + float(bbox[3])) / 2.0
        bw = max(0.04, float(bbox[2]) - float(bbox[0]))
        bh = max(0.04, float(bbox[3]) - float(bbox[1]))
        px = (cx - 0.5) * width
        pz = (1.0 - cy) * height + 0.10
        sx = max(0.45, bw * width / 1.2)
        sz = max(0.45, bh * height / 1.0)
        parts.extend(
            [
                part.transformed(
                    name_prefix=f"assembly_{unit['component']}__",
                    translate=(px, -1.70, pz),
                    scale=(sx, 1, sz),
                )
                for part in unit_parts
            ]
        )
    out_dir = project_dir / "outputs" / "assembly"
    outputs = export_parts(out_dir, "image_conditioned_assembly_preview", parts)
    connectivity = structural_connectivity(parts, tolerance=0.08)
    report = {
        "model_id": "image_conditioned_assembly_preview",
        "source": "uploaded_images_and_component_plan",
        "outputs": outputs,
        "part_count": len(parts),
        "connectivity": connectivity.__dict__,
        "notes": [
            "This is an image-conditioned assembly preview derived from uploaded image analysis and unit candidates.",
            "It is not a final reconstruction. Low-confidence units should request more images before detail work.",
        ],
    }
    report_path = out_dir / "image_conditioned_assembly_preview_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report["outputs"]["report"] = str(report_path)
    return report


def create_app(workspace: Path | None = None) -> Flask:
    app = Flask(__name__)
    project_root = (workspace or Path.cwd() / "projects").resolve()
    project_root.mkdir(parents=True, exist_ok=True)

    @app.get("/")
    def index():
        return INDEX_HTML

    @app.get("/api/projects")
    def list_projects():
        projects = []
        for path in sorted(project_root.iterdir()) if project_root.exists() else []:
            if path.is_dir():
                manifest = project_manifest(path)
                manifest["images"] = image_records(path)
                projects.append(manifest)
        return jsonify({"workspace": str(project_root), "projects": projects})

    @app.post("/api/projects")
    def create_project():
        payload = request.get_json(silent=True) or {}
        name = str(payload.get("name") or "Untitled Project")
        slug = slugify(payload.get("slug") or name)
        project_dir = safe_project(project_root, slug)
        manifest = project_manifest(project_dir)
        manifest.update({"slug": slug, "name": name})
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "images").mkdir(exist_ok=True)
        (project_dir / "outputs").mkdir(exist_ok=True)
        event(manifest, "Project created.")
        write_manifest(project_dir, manifest)
        manifest["images"] = image_records(project_dir)
        return jsonify(manifest)

    @app.get("/api/projects/<slug>")
    def get_project(slug: str):
        project_dir = safe_project(project_root, slug)
        if not project_dir.exists():
            return jsonify({"error": "unknown project"}), 404
        manifest = project_manifest(project_dir)
        manifest["images"] = image_records(project_dir)
        manifest["training_examples"] = training_examples(project_dir)
        if (project_dir / "analysis" / "image_analysis.json").exists():
            manifest["image_analysis_url"] = f"/api/projects/{project_dir.name}/files/analysis/image_analysis.json"
            manifest["image_contact_sheet_url"] = f"/api/projects/{project_dir.name}/files/analysis/image_contact_sheet.jpg"
            manifest["image_analysis_data"] = json.loads((project_dir / "analysis" / "image_analysis.json").read_text(encoding="utf-8"))
        manifest["reference_plan_url"] = f"/api/projects/{project_dir.name}/files/reference_plan.json" if (project_dir / "reference_plan.json").exists() else None
        if (project_dir / "reference_plan.json").exists():
            manifest["reference_plan_data"] = json.loads((project_dir / "reference_plan.json").read_text(encoding="utf-8"))
        manifest["component_plan_url"] = f"/api/projects/{project_dir.name}/files/{manifest['component_plan']}" if manifest.get("component_plan") else None
        if manifest.get("component_plan") and (project_dir / manifest["component_plan"]).exists():
            manifest["component_plan_data"] = json.loads((project_dir / manifest["component_plan"]).read_text(encoding="utf-8"))
        return jsonify(manifest)

    @app.get("/api/projects/<slug>/training-examples")
    def get_training_examples(slug: str):
        project_dir = safe_project(project_root, slug)
        if not project_dir.exists():
            return jsonify({"error": "unknown project"}), 404
        return jsonify({"examples": training_examples(project_dir)})

    @app.post("/api/projects/<slug>/training-examples")
    def create_training_example(slug: str):
        project_dir = safe_project(project_root, slug)
        manifest = project_manifest(project_dir)
        payload = request.get_json(silent=True) or {}
        try:
            example = save_training_example(project_dir, payload)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        action = "ignore" if example["label"] == "ignore" else "keep"
        invalidate_model_outputs(manifest, f"Added {action} crop label {example['id']}; unit discovery outputs were marked stale.")
        write_manifest(project_dir, manifest)
        manifest["images"] = image_records(project_dir)
        manifest["training_examples"] = training_examples(project_dir)
        return jsonify({"example": example, "examples": manifest["training_examples"], "project": manifest})

    @app.post("/api/projects/<slug>/images")
    def upload_images(slug: str):
        project_dir = safe_project(project_root, slug)
        manifest = project_manifest(project_dir)
        files = request.files.getlist("images")
        saved = save_uploads(files, project_dir / "images")
        manifest["images"] = image_records(project_dir)
        if saved:
            invalidate_derived_outputs(manifest, f"Uploaded {len(saved)} image(s); previous analysis and model outputs were marked stale.")
        else:
            event(manifest, "No supported image files were uploaded.")
        write_manifest(project_dir, manifest)
        return jsonify({"saved": saved, "project": manifest})

    @app.post("/api/projects/<slug>/plan")
    def build_plan(slug: str):
        project_dir = safe_project(project_root, slug)
        manifest = project_manifest(project_dir)
        images = [str(project_dir / record["path"]) for record in image_records(project_dir)]
        write_analysis(project_dir, [project_dir / record["path"] for record in image_records(project_dir)])
        plan = plan_reference_set(manifest.get("name") or slug, images)
        plan_path = project_dir / "reference_plan.json"
        plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        manifest["reference_plan"] = "reference_plan.json"
        event(manifest, f"Generated reference plan from {len(images)} image(s).")
        write_manifest(project_dir, manifest)
        return jsonify({"plan": plan, "project": manifest})

    @app.post("/api/projects/<slug>/components")
    def discover_components(slug: str):
        project_dir = safe_project(project_root, slug)
        manifest = project_manifest(project_dir)
        plan = write_component_plan(project_dir, manifest)
        event(manifest, f"Identified {len(plan['components'])} reconstruction unit candidate(s).")
        write_manifest(project_dir, manifest)
        return jsonify({"component_plan": plan, "project": manifest})

    @app.post("/api/projects/<slug>/build-units")
    def build_units(slug: str):
        project_dir = safe_project(project_root, slug)
        manifest = project_manifest(project_dir)
        report = build_unit_outputs(project_dir, manifest)
        event(manifest, f"Built {report['built_count']} unit draft(s).")
        write_manifest(project_dir, manifest)
        return jsonify({"report": report, "project": manifest})

    @app.post("/api/projects/<slug>/assemble")
    def assemble(slug: str):
        project_dir = safe_project(project_root, slug)
        manifest = project_manifest(project_dir)
        if not manifest.get("outputs", {}).get("units"):
            return jsonify({"error": "Build unit drafts before assembling the full structure."}), 400
        if not manifest.get("outputs", {}).get("units", {}).get("built_count"):
            return jsonify({"error": "No units were confidently drafted yet. Add more targeted images before assembly."}), 400
        report = build_assembly_preview(project_dir, manifest)
        orbit_report = render_orbit_set(
            obj_path=Path(report["outputs"]["obj"]),
            mtl_path=Path(report["outputs"]["mtl"]),
            out_dir=project_dir / "outputs" / "assembly" / "orbits",
            title=f"{manifest.get('name', slug)} assembly orbit",
        )
        report["orbit"] = orbit_report
        manifest.setdefault("outputs", {})["assembly"] = relativize_report(project_dir, report)
        event(manifest, "Assembled current unit drafts into a full model preview.")
        write_manifest(project_dir, manifest)
        return jsonify({"report": manifest["outputs"]["assembly"], "project": manifest})

    @app.post("/api/projects/<slug>/build-demo")
    def build_demo(slug: str):
        project_dir = safe_project(project_root, slug)
        manifest = project_manifest(project_dir)
        if not manifest.get("component_plan"):
            plan = write_component_plan(project_dir, manifest)
            event(manifest, f"Identified {len(plan['components'])} reconstruction unit candidate(s).")
        if not manifest.get("outputs", {}).get("units"):
            report = build_unit_outputs(project_dir, manifest)
            event(manifest, f"Built {report['built_count']} unit draft(s).")
        write_manifest(project_dir, manifest)
        report = build_assembly_preview(project_dir, manifest)
        orbit_report = render_orbit_set(Path(report["outputs"]["obj"]), Path(report["outputs"]["mtl"]), project_dir / "outputs" / "assembly" / "orbits", title=f"{manifest.get('name', slug)} assembly orbit")
        report["orbit"] = orbit_report
        manifest.setdefault("outputs", {})["assembly"] = relativize_report(project_dir, report)
        event(manifest, "Ran the full guided demo: units first, then assembly.")
        write_manifest(project_dir, manifest)
        return jsonify({"report": manifest["outputs"]["assembly"], "project": manifest})

    @app.get("/api/projects/<slug>/files/<path:subpath>")
    def project_file(slug: str, subpath: str):
        project_dir = safe_project(project_root, slug)
        return send_from_directory(project_dir, subpath)

    return app


def relativize_report(project_dir: Path, value: Any) -> Any:
    if isinstance(value, dict):
        return {key: relativize_report(project_dir, child) for key, child in value.items()}
    if isinstance(value, list):
        return [relativize_report(project_dir, child) for child in value]
    if isinstance(value, str):
        path = Path(value)
        try:
            resolved = path.resolve()
            if project_dir.resolve() in resolved.parents or resolved == project_dir.resolve():
                rel = resolved.relative_to(project_dir.resolve())
                return {
                    "path": str(rel),
                    "url": f"/api/projects/{project_dir.name}/files/{rel.as_posix()}",
                }
        except OSError:
            pass
    return value


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Sparse2Solid Studio</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #1c1917;
      --muted: #746d66;
      --paper: rgba(255, 255, 255, 0.68);
      --paper-strong: rgba(255, 255, 255, 0.92);
      --line: rgba(68, 64, 60, 0.13);
      --stone: #e7dfd4;
      --blue: #2563eb;
      --blue-2: #06b6d4;
      --green: #10b981;
      --orange: #f59e0b;
      --coral: #f9735b;
      --red: #ff453a;
      --purple: #8b5cf6;
      --shadow: 0 26px 70px rgba(66, 44, 22, 0.13);
      --radius-xl: 32px;
      --radius: 20px;
      font-family: "SF Pro Text", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      background:
        radial-gradient(circle at 12% 8%, rgba(6, 182, 212, 0.23), transparent 28%),
        radial-gradient(circle at 88% 3%, rgba(249, 115, 91, 0.26), transparent 30%),
        radial-gradient(circle at 70% 92%, rgba(139, 92, 246, 0.18), transparent 31%),
        linear-gradient(135deg, #fbfaf7 0%, #f4eee6 45%, #e8ddd1 100%);
      -webkit-font-smoothing: antialiased;
    }
    button, input, select { font: inherit; }
    button {
      border: 0;
      border-radius: 999px;
      padding: 12px 18px;
      background: linear-gradient(135deg, #1c1917, #44403c);
      color: white;
      cursor: pointer;
      box-shadow: 0 12px 24px rgba(0, 0, 0, 0.10);
      transition: transform .18s ease, box-shadow .18s ease, background .18s ease;
    }
    button:hover { transform: translateY(-1px); box-shadow: 0 16px 28px rgba(0,0,0,.14); }
    button.secondary { background: rgba(255, 255, 255, 0.74); color: var(--ink); border: 1px solid var(--line); box-shadow: none; }
    button.blue { background: linear-gradient(135deg, var(--blue), var(--blue-2)); }
    button.orange { background: linear-gradient(135deg, var(--orange), var(--coral)); color: #211100; }
    button:disabled { opacity: .45; cursor: not-allowed; transform: none; }
    .shell { display: grid; grid-template-columns: 360px 1fr; min-height: 100vh; }
    aside {
      position: sticky;
      top: 0;
      height: 100vh;
      padding: 28px;
      border-right: 1px solid rgba(120, 113, 108, 0.20);
      background: rgba(250, 250, 249, 0.66);
      backdrop-filter: blur(30px);
      overflow: auto;
    }
    main { padding: 32px; }
    .brand {
      padding: 26px;
      border-radius: var(--radius-xl);
      background:
        linear-gradient(155deg, rgba(255,255,255,.78), rgba(255,255,255,.54)),
        radial-gradient(circle at 82% 20%, rgba(6,182,212,.24), transparent 32%);
      border: 1px solid rgba(255,255,255,.7);
      box-shadow: var(--shadow);
      margin-bottom: 24px;
    }
    .brand-mark {
      width: 46px;
      height: 46px;
      border-radius: 50%;
      border: 1px solid rgba(68,64,60,.18);
      background:
        radial-gradient(circle at center, #1c1917 0 8%, transparent 9%),
        radial-gradient(circle at center, transparent 0 28%, rgba(28,25,23,.72) 29% 31%, transparent 32%),
        radial-gradient(circle at center, transparent 0 48%, rgba(6,182,212,.75) 49% 51%, transparent 52%),
        radial-gradient(circle at center, transparent 0 68%, rgba(249,115,91,.72) 69% 71%, transparent 72%),
        rgba(255,255,255,.72);
      box-shadow: inset 0 0 24px rgba(255,255,255,.8), 0 10px 30px rgba(66,44,22,.12);
      margin-bottom: 16px;
    }
    .eyebrow { color: #78716c; font-size: 11px; font-weight: 800; letter-spacing: .26em; text-transform: uppercase; }
    h1, h2 { font-family: Georgia, "Times New Roman", serif; font-weight: 500; }
    h1 { margin: 8px 0 6px; font-size: 55px; line-height: .9; letter-spacing: -0.055em; }
    h2 { margin: 0; font-size: 39px; letter-spacing: -0.045em; }
    h3 { margin: 0 0 10px; font-size: 18px; letter-spacing: -0.02em; }
    p { color: var(--muted); line-height: 1.5; }
    .field { display: grid; gap: 8px; margin: 16px 0; }
    .field label { font-size: 13px; font-weight: 800; color: #424754; }
    input[type="text"], select {
      width: 100%;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.72);
      border-radius: 16px;
      padding: 14px 15px;
      outline: none;
    }
    input[type="file"] { width: 100%; }
    .project-list { display: grid; gap: 10px; margin-top: 18px; }
    .project-item {
      text-align: left;
      border-radius: 18px;
      background: rgba(255,255,255,.58);
      border: 1px solid var(--line);
      padding: 14px;
      cursor: pointer;
    }
    .project-item.active { outline: 3px solid rgba(6, 182, 212, .22); background: rgba(255,255,255,.92); }
    .hero {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 24px;
      padding: 34px;
      background:
        linear-gradient(135deg, rgba(255,255,255,.78), rgba(255,255,255,.54)),
        radial-gradient(circle at 100% 0%, rgba(249,115,91,.20), transparent 32%);
      border: 1px solid rgba(255,255,255,.8);
      border-radius: var(--radius-xl);
      box-shadow: var(--shadow);
      backdrop-filter: blur(28px);
      margin-bottom: 24px;
    }
    .status-pill {
      display: inline-flex;
      gap: 8px;
      align-items: center;
      padding: 10px 14px;
      border-radius: 999px;
      background: rgba(16, 185, 129, .12);
      color: #047857;
      font-weight: 800;
    }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 18px; }
    .card {
      grid-column: span 6;
      border-radius: var(--radius-xl);
      background: var(--paper);
      border: 1px solid rgba(255,255,255,.78);
      box-shadow: var(--shadow);
      backdrop-filter: blur(28px);
      padding: 22px;
      min-height: 220px;
      position: relative;
      overflow: hidden;
    }
    .card::before {
      content: "";
      position: absolute;
      inset: 0 0 auto;
      height: 5px;
      background: linear-gradient(90deg, var(--blue-2), var(--green), var(--orange), var(--coral), var(--purple));
      opacity: .72;
    }
    .card.wide { grid-column: span 12; }
    .card.third { grid-column: span 4; }
    .card.locked { opacity: .62; }
    .tutorial {
      display: grid;
      gap: 10px;
      margin-top: 18px;
    }
    .tutorial-row {
      display: grid;
      grid-template-columns: 34px 1fr;
      gap: 12px;
      padding: 12px;
      border-radius: 18px;
      background: rgba(255,255,255,.58);
      border: 1px solid var(--line);
    }
    .tutorial-row.done .badge { background: rgba(16,185,129,.14); color: #047857; }
    .tutorial-row.current { outline: 3px solid rgba(6,182,212,.18); background: rgba(255,255,255,.9); }
    .step {
      display: flex;
      align-items: flex-start;
      gap: 14px;
      padding: 14px 0;
      border-top: 1px solid var(--line);
    }
    .step:first-of-type { border-top: 0; }
    .badge {
      width: 34px; height: 34px; border-radius: 12px;
      display: grid; place-items: center;
      background: rgba(6, 182, 212, .13);
      color: #0e7490;
      font-weight: 900;
      flex: 0 0 auto;
    }
    .dropzone {
      border: 2px dashed rgba(6,182,212,.35);
      background:
        linear-gradient(135deg, rgba(255,255,255,.60), rgba(255,255,255,.35)),
        radial-gradient(circle at 10% 12%, rgba(6,182,212,.16), transparent 34%),
        radial-gradient(circle at 90% 80%, rgba(249,115,91,.14), transparent 34%);
      border-radius: 24px;
      padding: 28px;
      min-height: 180px;
      display: grid;
      place-items: center;
      text-align: center;
    }
    .thumbs { display: grid; grid-template-columns: repeat(auto-fill, minmax(110px, 1fr)); gap: 10px; margin-top: 16px; }
    .thumb {
      aspect-ratio: 1.15;
      border-radius: 16px;
      overflow: hidden;
      background: #e5e7ef;
      border: 1px solid rgba(255,255,255,.7);
      position: relative;
    }
    .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
    .roi-trainer {
      display: grid;
      grid-template-columns: minmax(0, 1.3fr) minmax(260px, .7fr);
      gap: 18px;
      align-items: start;
    }
    .roi-canvas-wrap {
      border-radius: 24px;
      border: 1px solid var(--line);
      background: rgba(28, 25, 23, .06);
      overflow: hidden;
      min-height: 360px;
    }
    .roi-canvas {
      width: 100%;
      height: 540px;
      display: block;
      cursor: crosshair;
      background: #f5f0e8;
    }
    .roi-controls { display: grid; gap: 12px; }
    .example-chip {
      display: grid;
      grid-template-columns: 76px 1fr;
      gap: 10px;
      align-items: center;
      padding: 10px;
      border-radius: 16px;
      border: 1px solid var(--line);
      background: rgba(255,255,255,.58);
    }
    .example-chip img {
      width: 76px;
      height: 58px;
      border-radius: 12px;
      object-fit: cover;
      background: #eee7dd;
    }
    .label-pill {
      display: inline-block;
      padding: 5px 8px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 900;
      color: #0f172a;
      background: rgba(6,182,212,.16);
    }
    .label-pill.ignore { background: rgba(255,69,58,.14); color: #b42318; }
    .checklist { display: grid; gap: 10px; }
    .check {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 12px;
      padding: 13px;
      border-radius: 18px;
      background: rgba(255,255,255,.58);
      border: 1px solid var(--line);
    }
    .check strong { display: block; margin-bottom: 4px; }
    .check span, .small { color: var(--muted); font-size: 13px; line-height: 1.35; }
    .unit-list { display: grid; gap: 12px; margin-top: 16px; }
    .unit-card {
      padding: 14px;
      border-radius: 20px;
      background: rgba(255,255,255,.62);
      border: 1px solid var(--line);
    }
    .unit-top { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
    .confidence {
      font-weight: 900;
      font-size: 13px;
      padding: 8px 10px;
      border-radius: 999px;
      background: rgba(6,182,212,.13);
      color: #0e7490;
      white-space: nowrap;
    }
    .confidence.low { background: rgba(245,158,11,.19); color: #92400e; }
    .needs { margin: 10px 0 0; padding-left: 18px; color: var(--muted); font-size: 13px; line-height: 1.4; }
    .preview img {
      width: 100%;
      border-radius: 22px;
      border: 1px solid var(--line);
      background: white;
    }
    .obj-viewer {
      width: 100%;
      min-height: 320px;
      border-radius: 22px;
      border: 1px solid rgba(68,64,60,.16);
      background:
        radial-gradient(circle at 20% 18%, rgba(6,182,212,.14), transparent 30%),
        linear-gradient(135deg, #fbfaf7, #ded3c6);
      margin-top: 12px;
    }
    .events { display: grid; gap: 8px; }
    .event {
      padding: 10px 12px;
      border-radius: 14px;
      background: rgba(255,255,255,.55);
      color: #4b5361;
      font-size: 13px;
    }
    .folder-map {
      display: grid;
      gap: 10px;
      font-family: ui-monospace, "SF Mono", Menlo, monospace;
      font-size: 13px;
      color: #3d4652;
      background: rgba(255,255,255,.58);
      border: 1px solid var(--line);
      border-radius: 20px;
      padding: 16px;
    }
    .toast {
      position: fixed;
      right: 24px;
      bottom: 24px;
      max-width: 360px;
      padding: 14px 16px;
      border-radius: 18px;
      background: rgba(28, 25, 23, .92);
      color: white;
      box-shadow: var(--shadow);
      transform: translateY(24px);
      opacity: 0;
      pointer-events: none;
      transition: opacity .18s ease, transform .18s ease;
      z-index: 10;
    }
    .toast.show { opacity: 1; transform: translateY(0); }
    .empty {
      min-height: 520px;
      display: grid;
      align-items: center;
      border-radius: var(--radius-xl);
      background:
        radial-gradient(circle at 12% 12%, rgba(6,182,212,.24), transparent 28%),
        radial-gradient(circle at 86% 10%, rgba(249,115,91,.22), transparent 30%),
        linear-gradient(135deg, #1c1917, #292524 58%, #44403c);
      border: 1px solid rgba(255,255,255,.12);
      box-shadow: 0 30px 90px rgba(28,25,23,.22);
      padding: 42px;
    }
    .empty h2 { color: #fffaf0; }
    .empty p { color: rgba(255,250,240,.72); max-width: 740px; }
    .empty .tutorial-row {
      background: rgba(255,255,255,.08);
      border-color: rgba(255,255,255,.12);
      color: #fffaf0;
    }
    .empty .small { color: rgba(255,250,240,.62); }
    .loader {
      width: 12px;
      height: 12px;
      border-radius: 50%;
      background: var(--green);
      box-shadow: 0 0 0 8px rgba(52,199,89,.14);
    }
    @media (max-width: 980px) {
      .shell { grid-template-columns: 1fr; }
      aside { position: relative; height: auto; }
      .card, .card.third { grid-column: span 12; }
      .hero { align-items: flex-start; flex-direction: column; }
      .roi-trainer { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside>
      <section class="brand">
        <div class="brand-mark" aria-hidden="true"></div>
        <div class="eyebrow">Few Images To 3D</div>
        <h1>Sparse<br>2Solid</h1>
        <p>Build a clean 3D model by solving reusable parts first, then assembling the whole structure.</p>
      </section>
      <section class="card wide" style="padding:18px;min-height:auto;">
        <h3>Start Here</h3>
        <div class="field">
          <label for="projectName">Building or object name</label>
          <input id="projectName" type="text" placeholder="The Grand Hotel" />
        </div>
        <button class="blue" id="createProject">Create Project</button>
      </section>
      <section class="tutorial" id="tutorial"></section>
      <h3 style="margin-top:26px;">Projects</h3>
      <div id="projectList" class="project-list"></div>
    </aside>
    <main>
      <section class="hero">
        <div>
          <div class="eyebrow">Unit-First Reconstruction Studio</div>
          <h2 id="heroTitle">Create a project to begin</h2>
          <p id="heroCopy">Sparse2Solid works by identifying reusable geometry units from your images, drafting those units, asking for missing views when needed, and only then assembling the full model.</p>
        </div>
        <div class="status-pill"><span class="loader"></span><span id="statusText">Ready</span></div>
      </section>
      <section id="emptyState" class="empty">
        <div>
          <div class="eyebrow">Onboarding</div>
          <h2>Your first model starts with a named project.</h2>
          <p>After you create a project, the studio will unlock one step at a time: upload images, check missing views, discover reusable geometry units, draft each unit, then assemble the structure.</p>
          <div class="tutorial" style="max-width:720px;">
            <div class="tutorial-row current"><div class="badge">1</div><div><strong>Create a project</strong><br><span class="small">Give the building a name so images and outputs have a home.</span></div></div>
            <div class="tutorial-row"><div class="badge">2</div><div><strong>Upload 12-35 images</strong><br><span class="small">Use front, oblique, side/rear, roofline, detail, and scale/context views.</span></div></div>
            <div class="tutorial-row"><div class="badge">3</div><div><strong>Solve units before assembly</strong><br><span class="small">The model is built from repeated components, not guessed in one giant mesh.</span></div></div>
          </div>
        </div>
      </section>
      <section id="projectPane" class="grid" hidden>
        <article class="card">
          <h3>1. Upload Images</h3>
          <div class="dropzone" id="dropzone">
            <div>
              <p><strong>Drop images here</strong> or choose files.</p>
              <p class="small">Best first set: front, left/right obliques, sides/rear, roofline, closeups, and one scale/context shot.</p>
              <input id="imageInput" type="file" multiple accept="image/*" />
            </div>
          </div>
          <div id="thumbs" class="thumbs"></div>
          <div id="imageAnalysis" class="preview" style="margin-top:16px;"></div>
        </article>
        <article class="card wide">
          <h3>2. Teach The Detector</h3>
          <p>Draw a few boxes around useful structure and obvious distractions. These labels become guardrails: kept crops are promoted into unit candidates, ignored crops suppress noisy automatic proposals.</p>
          <div class="roi-trainer">
            <div>
              <div class="field">
                <label for="roiImageSelect">Training image</label>
                <select id="roiImageSelect"></select>
              </div>
              <div class="roi-canvas-wrap">
                <canvas id="roiCanvas" class="roi-canvas"></canvas>
              </div>
              <p class="small">Drag on the image to mark a region. After you release, Sparse2Solid suggests a label from the crop shape, but you can override it.</p>
            </div>
            <div class="roi-controls">
              <div class="field">
                <label for="roiLabel">Crop label</label>
                <select id="roiLabel">
                  <option value="structure">Structure / useful detail</option>
                  <option value="opening">Opening / window / door</option>
                  <option value="vertical">Vertical repeat / support</option>
                  <option value="band">Horizontal band / rail</option>
                  <option value="roofline">Roofline / upper edge</option>
                  <option value="ignore">Ignore: tree, sky, person, flower, sign, glare</option>
                </select>
              </div>
              <div class="field">
                <label for="roiNotes">Optional note</label>
                <input id="roiNotes" type="text" placeholder="e.g. keep one porch column, ignore flowers" />
              </div>
              <button id="saveRoiBtn" class="blue">Save Crop Label</button>
              <div>
                <h3 style="margin-top:8px;">Training Labels</h3>
                <div id="roiExamples" class="unit-list"></div>
              </div>
            </div>
          </div>
        </article>
        <article class="card">
          <h3>3. Check Image Coverage</h3>
          <p>Before geometry is drafted, Sparse2Solid checks whether the images cover the views needed for a trustworthy unit-first model.</p>
          <button id="planBtn" class="orange">Check Missing Views</button>
          <div id="planSummary" class="checklist" style="margin-top:16px;"></div>
        </article>
        <article class="card">
          <h3>4. Discover Small Geometry Units</h3>
          <p>The next pass proposes small reusable primitives: base/steps, one support, one opening, one rail segment, one cornice strip, and one roof slice. The coarse mass is only a measuring scaffold.</p>
          <button id="componentsBtn" class="blue">Identify Small Units</button>
          <div id="componentSummary" class="unit-list"></div>
        </article>
        <article class="card">
          <h3>5. Draft Unit Geometry</h3>
          <p>Ready units are drafted independently and get their own OBJ/MTL plus orbit sheet. Low-confidence units ask for targeted images first.</p>
          <button id="unitsBtn" class="blue">Build Unit Drafts</button>
          <div id="unitOutputs" class="unit-list"></div>
        </article>
        <article class="card third">
          <h3>6. Assemble Preview</h3>
          <p>The full model should come after unit drafts. Assembly places reviewed pieces into one structure and renders orbit QA.</p>
          <button id="assembleBtn" class="orange">Assemble Current Units</button>
          <div id="assemblyOutputs" style="margin-top:16px;"></div>
        </article>
        <article class="card third">
          <h3>Project State</h3>
          <div id="projectStats"></div>
        </article>
        <article class="card third">
          <h3>Working Folders</h3>
          <div class="folder-map" id="folderMap"></div>
        </article>
        <article class="card wide">
          <h3>Activity</h3>
          <div id="events" class="events"></div>
        </article>
      </section>
    </main>
  </div>
  <div id="toast" class="toast"></div>
  <script>
    let state = {
      projects: [],
      active: null,
      project: null,
      plan: null,
      components: null,
      roi: { image: null, imageName: null, rect: null, dragStart: null, drawBounds: null }
    };
    const $ = (id) => document.getElementById(id);
    const setStatus = (text) => $("statusText").textContent = text;
    const steps = [
      ["Project", p => !!p],
      ["Images", p => (p?.images || []).length > 0],
      ["Coverage", p => !!p?.reference_plan],
      ["Units", p => !!p?.component_plan],
      ["Drafts", p => !!p?.outputs?.units],
      ["Assembly", p => !!p?.outputs?.assembly],
    ];

    function toast(message) {
      $("toast").textContent = message;
      $("toast").classList.add("show");
      setTimeout(() => $("toast").classList.remove("show"), 2800);
    }

    async function api(path, options = {}) {
      setStatus("Working");
      const res = await fetch(path, options);
      const data = await res.json();
      if (!res.ok) {
        setStatus("Needs attention");
        toast(data.error || "That step is not ready yet.");
        throw new Error(data.error || "Request failed");
      }
      setStatus("Ready");
      return data;
    }

    async function refreshProjects(selectSlug) {
      const data = await api("/api/projects");
      state.projects = data.projects;
      $("projectList").innerHTML = state.projects.map(project => `
        <div class="project-item ${state.active === project.slug ? "active" : ""}" data-slug="${project.slug}">
          <strong>${project.name}</strong><br>
          <span class="small">${project.images.length} image(s)</span>
        </div>
      `).join("");
      document.querySelectorAll(".project-item").forEach(item => {
        item.addEventListener("click", () => loadProject(item.dataset.slug));
      });
      if (selectSlug) await loadProject(selectSlug);
    }

    async function loadProject(slug) {
      const project = await api(`/api/projects/${slug}`);
      state.active = project.slug;
      state.project = project;
      $("emptyState").hidden = true;
      $("projectPane").hidden = false;
      $("heroTitle").textContent = project.name;
      $("heroCopy").textContent = `${project.images.length} uploaded image(s). Continue through the unit-first checklist: coverage, unit discovery, unit drafts, then assembly.`;
      renderProject(project);
      await refreshProjects();
    }

    function renderProject(project) {
      $("thumbs").innerHTML = project.images.map(image => `<div class="thumb"><img src="${image.url}" alt="${image.name}"></div>`).join("");
      if (project.image_analysis_data) {
        const s = project.image_analysis_data.summary;
        $("imageAnalysis").innerHTML = `
          <p class="small"><strong>Image analysis:</strong> ${s.analyzable_count}/${s.image_count} analyzable · rhythm ${s.average_facade_rhythm} · roofline ${s.average_roofline} · diversity ${s.view_diversity_score}</p>
          ${project.image_contact_sheet_url ? `<img src="${project.image_contact_sheet_url}" alt="Uploaded image contact sheet">` : ""}
        `;
      } else {
        $("imageAnalysis").innerHTML = `<p class="small">Image analysis will appear after checking coverage or identifying units.</p>`;
      }
      $("projectStats").innerHTML = `
        <p><strong>Slug:</strong> ${project.slug}</p>
        <p><strong>Images:</strong> ${project.images.length}</p>
        <p><strong>Capture plan:</strong> ${project.reference_plan ? "complete" : "not yet"}</p>
        <p><strong>Small-unit plan:</strong> ${project.component_plan ? "complete" : "not yet"}</p>
        <p><strong>Unit drafts:</strong> ${project.outputs?.units ? "built" : "not yet"}</p>
      `;
      $("folderMap").innerHTML = `
        <div>projects/${project.slug}/</div>
        <div>  images/ <span class="small">uploaded references</span></div>
        <div>  analysis/ <span class="small">image metrics + contact sheet from your photos</span></div>
        <div>  components/training_examples.jsonl <span class="small">manual keep/ignore crop labels</span></div>
        <div>  components/training_crops/ <span class="small">saved classifier evidence crops</span></div>
        <div>  reference_plan.json <span class="small">missing-view checklist</span></div>
        <div>  components/component_plan.json <span class="small">small-unit candidates</span></div>
        <div>  outputs/units/ <span class="small">per-unit OBJ/MTL/orbits</span></div>
        <div>  outputs/assembly/ <span class="small">full model preview</span></div>
      `;
      $("events").innerHTML = (project.events || []).map(e => `<div class="event">${e.at}<br>${e.message}</div>`).join("") || `<p class="small">No activity yet.</p>`;
      renderRoiTrainer(project);
      renderTutorial(project);
      updateLocks(project);
      if (project.reference_plan_data) renderPlan(project.reference_plan_data, false);
      else $("planSummary").innerHTML = `<p class="small">Upload images, then check coverage to see which views are strong or missing.</p>`;
      if (project.component_plan_data) renderComponents(project.component_plan_data, false);
      else $("componentSummary").innerHTML = `<p class="small">Coverage comes first. Small-unit discovery will appear here.</p>`;
      if (project.outputs?.units) renderUnitOutputs(project.outputs.units);
      else $("unitOutputs").innerHTML = `<p class="small">Ready unit drafts and their OBJ/MTL/orbit outputs will appear here.</p>`;
      if (project.outputs?.assembly) renderAssemblyOutputs(project.outputs.assembly);
      else $("assemblyOutputs").innerHTML = `<p class="small">Assembly unlocks after unit drafts are built.</p>`;
    }

    function renderRoiTrainer(project) {
      const select = $("roiImageSelect");
      const current = select.value || state.roi.imageName || project.images[0]?.name || "";
      select.innerHTML = project.images.length
        ? project.images.map(image => `<option value="${image.name}" ${image.name === current ? "selected" : ""}>${image.name}</option>`).join("")
        : `<option value="">Upload images first</option>`;
      state.roi.imageName = select.value || project.images[0]?.name || null;
      $("roiExamples").innerHTML = (project.training_examples || []).length
        ? project.training_examples.slice().reverse().map(example => `
          <div class="example-chip">
            ${example.crop_path ? `<img src="/api/projects/${project.slug}/files/${example.crop_path}" alt="${example.label} crop">` : `<div></div>`}
            <div>
              <span class="label-pill ${example.label === "ignore" ? "ignore" : ""}">${example.label}</span>
              <div class="small">${example.image_name}</div>
              ${example.notes ? `<div class="small">${example.notes}</div>` : ""}
            </div>
          </div>
        `).join("")
        : `<p class="small">No crop labels yet. Add 3-8 useful structure labels and a few ignore labels for trees, flowers, people, sky, signs, or reflections.</p>`;
      loadRoiImage(project);
    }

    function loadRoiImage(project) {
      const canvas = $("roiCanvas");
      const ctx = canvas.getContext("2d");
      const selected = project.images.find(image => image.name === state.roi.imageName) || project.images[0];
      if (!selected) {
        const rect = canvas.getBoundingClientRect();
        canvas.width = Math.max(520, rect.width || 520);
        canvas.height = 360;
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = "#f5f0e8";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = "#746d66";
        ctx.font = "16px SF Pro Text, system-ui";
        ctx.fillText("Upload images to train the detector.", 24, 42);
        state.roi.image = null;
        state.roi.rect = null;
        return;
      }
      const image = new Image();
      image.onload = () => {
        state.roi.image = image;
        state.roi.rect = null;
        drawRoiCanvas();
      };
      image.src = selected.url;
    }

    function drawRoiCanvas() {
      const canvas = $("roiCanvas");
      const rect = canvas.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      const cssWidth = Math.max(520, rect.width || 760);
      const cssHeight = Math.max(360, Math.min(560, cssWidth * 0.62));
      canvas.width = cssWidth * ratio;
      canvas.height = cssHeight * ratio;
      canvas.style.height = `${cssHeight}px`;
      const ctx = canvas.getContext("2d");
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.clearRect(0, 0, cssWidth, cssHeight);
      ctx.fillStyle = "#f5f0e8";
      ctx.fillRect(0, 0, cssWidth, cssHeight);
      if (!state.roi.image) return;
      const img = state.roi.image;
      const scale = Math.min(cssWidth / img.naturalWidth, cssHeight / img.naturalHeight);
      const drawW = img.naturalWidth * scale;
      const drawH = img.naturalHeight * scale;
      const dx = (cssWidth - drawW) / 2;
      const dy = (cssHeight - drawH) / 2;
      state.roi.drawBounds = { x: dx, y: dy, w: drawW, h: drawH };
      ctx.drawImage(img, dx, dy, drawW, drawH);
      ctx.fillStyle = "rgba(28,25,23,.28)";
      ctx.fillRect(0, 0, cssWidth, dy);
      ctx.fillRect(0, dy + drawH, cssWidth, cssHeight - dy - drawH);
      ctx.fillRect(0, dy, dx, drawH);
      ctx.fillRect(dx + drawW, dy, cssWidth - dx - drawW, drawH);
      if (state.roi.rect) {
        const r = normalizedRect(state.roi.rect);
        ctx.save();
        ctx.strokeStyle = $("roiLabel").value === "ignore" ? "rgba(255,69,58,.95)" : "rgba(37,99,235,.95)";
        ctx.fillStyle = $("roiLabel").value === "ignore" ? "rgba(255,69,58,.13)" : "rgba(6,182,212,.14)";
        ctx.lineWidth = 3;
        ctx.fillRect(r.x, r.y, r.w, r.h);
        ctx.strokeRect(r.x, r.y, r.w, r.h);
        ctx.restore();
      }
    }

    function normalizedRect(rect) {
      const x = Math.min(rect.x1, rect.x2);
      const y = Math.min(rect.y1, rect.y2);
      return { x, y, w: Math.abs(rect.x2 - rect.x1), h: Math.abs(rect.y2 - rect.y1) };
    }

    function canvasPoint(event) {
      const bounds = $("roiCanvas").getBoundingClientRect();
      return { x: event.clientX - bounds.left, y: event.clientY - bounds.top };
    }

    function roiBBoxNormalized() {
      if (!state.roi.rect || !state.roi.drawBounds) return null;
      const r = normalizedRect(state.roi.rect);
      const d = state.roi.drawBounds;
      const x1 = Math.max(0, Math.min(1, (r.x - d.x) / d.w));
      const y1 = Math.max(0, Math.min(1, (r.y - d.y) / d.h));
      const x2 = Math.max(0, Math.min(1, (r.x + r.w - d.x) / d.w));
      const y2 = Math.max(0, Math.min(1, (r.y + r.h - d.y) / d.h));
      if (x2 - x1 < 0.015 || y2 - y1 < 0.015) return null;
      return [x1, y1, x2, y2].map(value => Math.round(value * 100000) / 100000);
    }

    function suggestRoiLabel() {
      const bbox = roiBBoxNormalized();
      if (!bbox) return;
      const w = bbox[2] - bbox[0];
      const h = bbox[3] - bbox[1];
      const cy = (bbox[1] + bbox[3]) / 2;
      let label = "structure";
      if (cy < 0.24 && w > h * 2.2) label = "roofline";
      else if (w > h * 3.0) label = "band";
      else if (h > w * 2.2 && w < 0.18) label = "vertical";
      else if (h > 0.12 && w > 0.04 && w < 0.32) label = "opening";
      $("roiLabel").value = label;
    }

    function renderTutorial(project) {
      const firstIncomplete = steps.findIndex(([_label, done]) => !done(project));
      $("tutorial").innerHTML = steps.map(([label, done], index) => {
        const complete = done(project);
        const current = !complete && index === firstIncomplete;
        return `<div class="tutorial-row ${complete ? "done" : ""} ${current ? "current" : ""}">
          <div class="badge">${complete ? "✓" : index + 1}</div>
          <div><strong>${label}</strong><br><span class="small">${stepHelp(label)}</span></div>
        </div>`;
      }).join("");
    }

    function stepHelp(label) {
      return {
        Project: "Create a workspace for images and outputs.",
        Images: "Upload diverse 2D references.",
        Coverage: "Check what views are missing.",
        Units: "Identify reusable geometry pieces.",
        Drafts: "Build each confident unit separately.",
        Assembly: "Place units into a full model preview.",
      }[label];
    }

    function updateLocks(project) {
      $("planBtn").disabled = project.images.length === 0;
      $("componentsBtn").disabled = !project.reference_plan;
      $("unitsBtn").disabled = !project.component_plan;
      $("assembleBtn").disabled = !project.outputs?.units;
    }

    function renderPlan(plan, showToast = true) {
      $("planSummary").innerHTML = plan.checklist.map(item => `
        <div class="check">
          <div><strong>${item.view.replaceAll("_", " ")}</strong><span>${item.purpose}</span></div>
          <span>${item.minimum}-${item.ideal}</span>
        </div>
      `).join("");
      if (showToast) toast("Coverage checklist updated.");
    }

    function renderComponents(plan, showToast = true) {
      state.components = plan;
      $("componentSummary").innerHTML = plan.components.map(component => `
        <div class="unit-card">
          <div class="unit-top">
            <div><strong>${component.label}</strong><br><span class="small">${component.why}</span></div>
            <div class="confidence ${component.confidence < .66 ? "low" : ""}">${Math.round(component.confidence * 100)}%</div>
          </div>
          ${component.crop_path ? `<div class="preview" style="margin-top:10px;"><img src="/api/projects/${state.active}/files/${component.crop_path}" alt="${component.label} evidence crop"></div>` : ""}
          <p class="small"><strong>Role:</strong> ${(component.role || "unit").replaceAll("_", " ")}</p>
          <p class="small"><strong>Kind:</strong> ${(component.kind || "unknown").replaceAll("_", " ")}</p>
          <p class="small"><strong>Source:</strong> ${(component.source || "automatic").replaceAll("_", " ")}</p>
          <p class="small"><strong>Status:</strong> ${component.status.replaceAll("_", " ")}</p>
          ${component.crop_metrics ? `<p class="small"><strong>Crop metrics:</strong> structural ${component.crop_metrics.structural_score} · organic ${component.crop_metrics.organic_ratio} · edges ${component.crop_metrics.edge_density}</p>` : ""}
          <p class="small"><strong>Evidence:</strong> ${(component.evidence || []).join(" · ")}</p>
          <ul class="needs">${component.needs.map(need => `<li>${need}</li>`).join("")}</ul>
        </div>
      `).join("");
      if (showToast) toast("Unit candidates identified.");
    }

    function renderUnitOutputs(report) {
      $("unitOutputs").innerHTML = report.units.map(unit => `
        <div class="unit-card">
          <div class="unit-top">
            <div><strong>${unit.label}</strong><br><span class="small">${unit.built ? `${unit.part_count} named parts drafted` : unit.reason}</span></div>
            <div class="confidence ${unit.built ? "" : "low"}">${unit.built ? "Drafted" : "Needs views"}</div>
          </div>
          ${unit.built ? `<p><a href="${unit.outputs.obj.url}" target="_blank">OBJ</a> · <a href="${unit.outputs.mtl.url}" target="_blank">MTL</a> · <a href="${unit.orbit.contact_sheet.url}" target="_blank">Orbit sheet</a> · <button class="secondary" onclick="viewObj('${unit.outputs.obj.url}', 'unit-viewer-${unit.component}')">View OBJ</button></p><canvas class="obj-viewer" id="unit-viewer-${unit.component}"></canvas>` : ""}
        </div>
      `).join("");
    }

    function renderAssemblyOutputs(report) {
      const obj = report.outputs.obj;
      const mtl = report.outputs.mtl;
      const contact = report.orbit.contact_sheet;
      $("assemblyOutputs").innerHTML = `
        <p><a href="${obj.url}" target="_blank">OBJ</a> · <a href="${mtl.url}" target="_blank">MTL</a> · <a href="${report.outputs.report.url}" target="_blank">Report</a> · <button class="secondary" onclick="viewObj('${obj.url}', 'assemblyObjCanvas')">View OBJ</button></p>
        <p class="small">Connectivity: ${report.connectivity.grounded ? "grounded" : "floating pieces found"} · ${report.part_count} named parts</p>
        <canvas class="obj-viewer" id="assemblyObjCanvas"></canvas>
        <div class="preview"><img src="${contact.url}" alt="Assembly orbit contact sheet"></div>
      `;
      viewObj(obj.url, "assemblyObjCanvas");
    }

    async function viewObj(url, canvasId) {
      const canvas = document.getElementById(canvasId);
      if (!canvas) return;
      const text = await fetch(url).then(r => r.text());
      const mesh = parseObj(text);
      drawObj(mesh, canvas, 0.74);
    }

    function parseObj(text) {
      const vertices = [[0,0,0]];
      const faces = [];
      for (const raw of text.split(/\n/)) {
        const line = raw.trim();
        if (!line || line.startsWith("#")) continue;
        const parts = line.split(/\s+/);
        if (parts[0] === "v") vertices.push(parts.slice(1,4).map(Number));
        if (parts[0] === "f") {
          const ids = parts.slice(1).map(part => parseInt(part.split("/")[0], 10));
          for (let i = 1; i < ids.length - 1; i++) faces.push([ids[0], ids[i], ids[i + 1]]);
        }
      }
      return {vertices, faces};
    }

    function drawObj(mesh, canvas, angle) {
      const rect = canvas.getBoundingClientRect();
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.max(420, rect.width) * ratio;
      canvas.height = Math.max(320, rect.height) * ratio;
      const ctx = canvas.getContext("2d");
      ctx.scale(ratio, ratio);
      const width = canvas.width / ratio;
      const height = canvas.height / ratio;
      ctx.clearRect(0, 0, width, height);
      ctx.fillStyle = "rgba(255,255,255,.32)";
      ctx.fillRect(0, 0, width, height);
      const verts = mesh.vertices.slice(1);
      if (!verts.length) return;
      const xs = verts.map(v => v[0]), ys = verts.map(v => v[1]), zs = verts.map(v => v[2]);
      const center = [(Math.min(...xs)+Math.max(...xs))/2, (Math.min(...ys)+Math.max(...ys))/2, (Math.min(...zs)+Math.max(...zs))/2];
      const span = Math.max(Math.max(...xs)-Math.min(...xs), Math.max(...ys)-Math.min(...ys), Math.max(...zs)-Math.min(...zs), 1);
      const scale = Math.min(width, height) * 0.62 / span;
      const ca = Math.cos(angle), sa = Math.sin(angle);
      const projected = mesh.vertices.map(v => {
        const x = v[0] - center[0], y = v[1] - center[1], z = v[2] - center[2];
        const rx = x * ca - y * sa;
        const ry = x * sa + y * ca;
        return [width/2 + rx * scale, height/2 + (z * -0.86 + ry * 0.26) * scale, ry];
      });
      const renderFaces = mesh.faces.map((face, idx) => {
        const d = (projected[face[0]][2] + projected[face[1]][2] + projected[face[2]][2]) / 3;
        return {face, idx, d};
      }).sort((a,b) => a.d - b.d).slice(0, 9000);
      for (const item of renderFaces) {
        const pts = item.face.map(id => projected[id]);
        const shade = 190 + (item.idx % 37);
        ctx.beginPath();
        ctx.moveTo(pts[0][0], pts[0][1]);
        ctx.lineTo(pts[1][0], pts[1][1]);
        ctx.lineTo(pts[2][0], pts[2][1]);
        ctx.closePath();
        ctx.fillStyle = `rgb(${shade}, ${Math.max(130, shade-18)}, ${Math.max(110, shade-35)})`;
        ctx.fill();
        if (item.idx % 4 === 0) {
          ctx.strokeStyle = "rgba(28,25,23,.18)";
          ctx.stroke();
        }
      }
      ctx.fillStyle = "rgba(28,25,23,.70)";
      ctx.font = "12px SF Pro Text, system-ui";
      ctx.fillText(`${mesh.faces.length.toLocaleString()} faces · browser OBJ preview`, 16, 24);
    }

    $("createProject").addEventListener("click", async () => {
      const name = $("projectName").value || "Untitled Building";
      const project = await api("/api/projects", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({name})
      });
      await refreshProjects(project.slug);
    });

    async function upload(files) {
      if (!state.active) return toast("Create or choose a project before uploading images.");
      if (!files.length) return;
      const form = new FormData();
      [...files].forEach(file => form.append("images", file));
      await api(`/api/projects/${state.active}/images`, { method: "POST", body: form });
      await loadProject(state.active);
    }

    $("imageInput").addEventListener("change", (e) => upload(e.target.files));
    $("dropzone").addEventListener("dragover", (e) => { e.preventDefault(); e.currentTarget.style.borderColor = "var(--blue)"; });
    $("dropzone").addEventListener("dragleave", (e) => { e.currentTarget.style.borderColor = "rgba(0,122,255,.32)"; });
    $("dropzone").addEventListener("drop", (e) => {
      e.preventDefault();
      e.currentTarget.style.borderColor = "rgba(0,122,255,.32)";
      upload(e.dataTransfer.files);
    });

    $("roiImageSelect").addEventListener("change", (e) => {
      state.roi.imageName = e.target.value;
      state.roi.rect = null;
      if (state.project) loadRoiImage(state.project);
    });

    $("roiLabel").addEventListener("change", drawRoiCanvas);

    $("roiCanvas").addEventListener("mousedown", (e) => {
      if (!state.roi.image) return;
      const point = canvasPoint(e);
      state.roi.dragStart = point;
      state.roi.rect = { x1: point.x, y1: point.y, x2: point.x, y2: point.y };
      drawRoiCanvas();
    });

    $("roiCanvas").addEventListener("mousemove", (e) => {
      if (!state.roi.dragStart || !state.roi.rect) return;
      const point = canvasPoint(e);
      state.roi.rect.x2 = point.x;
      state.roi.rect.y2 = point.y;
      drawRoiCanvas();
    });

    window.addEventListener("mouseup", () => {
      if (!state.roi.dragStart) return;
      state.roi.dragStart = null;
      suggestRoiLabel();
      drawRoiCanvas();
    });

    $("saveRoiBtn").addEventListener("click", async () => {
      if (!state.active) return toast("Create or choose a project before saving crop labels.");
      const bbox = roiBBoxNormalized();
      if (!bbox) return toast("Draw a larger crop box first.");
      const data = await api(`/api/projects/${state.active}/training-examples`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
          image_name: $("roiImageSelect").value,
          label: $("roiLabel").value,
          bbox_normalized: bbox,
          notes: $("roiNotes").value
        })
      });
      state.project = data.project;
      state.roi.rect = null;
      $("roiNotes").value = "";
      await loadProject(state.active);
      toast("Crop label saved. Re-run unit discovery to use it.");
    });

    $("planBtn").addEventListener("click", async () => {
      if (!state.active) return toast("Create a project first.");
      if (!state.project?.images?.length) return toast("Upload images before checking coverage.");
      const data = await api(`/api/projects/${state.active}/plan`, { method: "POST" });
      renderPlan(data.plan);
      await loadProject(state.active);
    });

    $("componentsBtn").addEventListener("click", async () => {
      if (!state.active) return toast("Create a project first.");
      if (!state.project?.reference_plan) return toast("Check image coverage before identifying units.");
      const data = await api(`/api/projects/${state.active}/components`, { method: "POST" });
      renderComponents(data.component_plan);
      await loadProject(state.active);
    });

    $("unitsBtn").addEventListener("click", async () => {
      if (!state.active) return toast("Create a project first.");
      if (!state.project?.component_plan) return toast("Identify units before drafting geometry.");
      const data = await api(`/api/projects/${state.active}/build-units`, { method: "POST" });
      renderUnitOutputs(data.report);
      await loadProject(state.active);
    });

    $("assembleBtn").addEventListener("click", async () => {
      if (!state.active) return toast("Create a project first.");
      if (!state.project?.outputs?.units) return toast("Build unit drafts before assembly.");
      const data = await api(`/api/projects/${state.active}/assemble`, { method: "POST" });
      renderAssemblyOutputs(data.report);
      await loadProject(state.active);
    });

    renderTutorial(null);
    refreshProjects().catch(err => { setStatus("Error"); console.error(err); });
  </script>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch the local Sparse2Solid Studio GUI.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", type=Path, default=Path("projects"))
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically.")
    args = parser.parse_args()
    app = create_app(args.workspace)
    url = f"http://{args.host}:{args.port}"
    if not args.no_open:
        webbrowser.open(url)
    print(f"Sparse2Solid Studio running at {url}")
    app.run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
