from __future__ import annotations

import argparse
import json
import re
import time
import webbrowser
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.datastructures import FileStorage

from .connectivity import structural_connectivity
from .geometry import MeshPart, box_part, cylinder_part, export_parts
from .image_analysis import write_analysis
from .orbit import render_orbit_set
from .reference_planner import plan_reference_set


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic"}

COMPONENT_HINTS = [
    {
        "component": "primary_massing_blockout",
        "label": "Primary massing blockout",
        "why": "A coarse volume is a measuring scaffold only: it anchors width, height, depth, and roof/base relationships before small units are assembled.",
        "needs": ["one wide exterior image", "one known scale or approximate dimensions", "at least one image with roof and ground visible"],
        "role": "scaffold",
        "priority": 1,
        "ready_threshold": 0.52,
    },
    {
        "component": "ground_base_unit",
        "label": "Ground/base or step unit",
        "why": "Base geometry should be solved separately so later supports, porch pieces, and repeated modules have an explicit ground chain.",
        "needs": ["image with ground contact visible", "wide elevation", "oblique view showing base depth"],
        "role": "structural_unit",
        "priority": 2,
        "ready_threshold": 0.50,
    },
    {
        "component": "vertical_support_unit",
        "label": "Single vertical support / column unit",
        "why": "Porches and long facades are usually controlled by one repeated post or column, not by one giant facade chunk.",
        "needs": ["straight-on view with repeated vertical rhythm", "oblique view showing support depth", "closeup if column/base shape matters"],
        "role": "repeatable_unit",
        "priority": 3,
        "ready_threshold": 0.56,
    },
    {
        "component": "opening_unit",
        "label": "Single opening / window-door unit",
        "why": "Openings should be drafted as one representative small unit, then repeated across the facade rather than baked into a large wall section.",
        "needs": ["front/elevation view", "one clear crop of a window or door", "oblique view if recess depth is visible"],
        "role": "repeatable_unit",
        "priority": 4,
        "ready_threshold": 0.56,
    },
    {
        "component": "horizontal_rail_band_unit",
        "label": "Porch rail / balcony band unit",
        "why": "Long railings, balconies, and porch edges are better as a short repeatable segment with rails and balusters.",
        "needs": ["image showing railing or porch band", "side/oblique for depth", "closeup if baluster pattern matters"],
        "role": "repeatable_unit",
        "priority": 5,
        "ready_threshold": 0.58,
    },
    {
        "component": "cornice_trim_unit",
        "label": "Cornice / trim strip unit",
        "why": "Dentils, gutters, belt courses, and trim bands are thin repeated pieces that should not be merged into the whole facade.",
        "needs": ["roofline or upper facade view", "image with horizontal trim visible", "oblique for projection depth"],
        "role": "repeatable_unit",
        "priority": 6,
        "ready_threshold": 0.56,
    },
    {
        "component": "roof_plane_unit",
        "label": "Roof plane / ridge slice unit",
        "why": "Roof massing needs its own small slice so pitch, overhang, ridge, and end returns can be adjusted independently.",
        "needs": ["roofline image", "side or oblique image", "image showing roof overhang and end condition"],
        "role": "repeatable_unit",
        "priority": 7,
        "ready_threshold": 0.58,
    },
    {
        "component": "detail_or_ornament_unit",
        "label": "Optional detail/ornament unit",
        "why": "Small details are only reconstructed when the images contain enough evidence or the user explicitly promotes them.",
        "needs": ["closeup of the object", "ground-contact or attachment view", "second angle if it is freestanding"],
        "role": "optional_detail",
        "priority": 8,
        "ready_threshold": 0.62,
    },
]


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


def component_plan(project_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    image_paths = [project_dir / record["path"] for record in image_records(project_dir)]
    analysis = write_analysis(project_dir, image_paths)
    summary = analysis["summary"]
    planned = []
    for hint in COMPONENT_HINTS:
        confidence, evidence = component_confidence(hint["component"], summary)
        status = "ready_for_unit_draft" if confidence >= hint.get("ready_threshold", 0.58) else "needs_more_images"
        planned.append(
            {
                "component": hint["component"],
                "label": hint["label"],
                "why": hint["why"],
                "role": hint["role"],
                "confidence": round(confidence, 2),
                "status": status,
                "evidence": evidence,
                "matched_images": evidence_images(analysis, hint["component"]),
                "needs": hint["needs"],
                "priority": hint["priority"],
                "output_folder": f"outputs/units/{hint['component']}",
            }
        )
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
        "strategy": "unit_first_reconstruction",
        "image_analysis": analysis,
        "components": planned,
        "ignored_by_default": ignored,
        "next_step": "Build ready unit drafts, then request targeted images for low-confidence units before assembly.",
    }


def component_confidence(component: str, summary: dict[str, Any]) -> tuple[float, list[str]]:
    n = summary["analyzable_count"]
    quality = summary["average_quality"]
    diversity = summary["view_diversity_score"]
    rhythm = summary["average_facade_rhythm"]
    roofline = summary["average_roofline"]
    wide = summary["wide_count"]
    evidence: list[str] = []
    if n:
        evidence.append(f"{n} analyzable image(s)")
    if wide:
        evidence.append(f"{wide} wide/context image(s)")
    if quality:
        evidence.append(f"average image quality {quality:.2f}")
    if component == "primary_massing_blockout":
        confidence = min(0.94, 0.12 + n * 0.075 + quality * 0.30 + diversity * 0.22 + min(wide, 3) * 0.075)
        evidence.append("uses image aspect ratios and broad edge structure")
    elif component == "ground_base_unit":
        confidence = min(0.88, 0.10 + n * 0.045 + quality * 0.24 + min(wide, 3) * 0.08)
        evidence.append("uses wide views to establish ground/base support")
    elif component == "vertical_support_unit":
        confidence = min(0.90, 0.08 + n * 0.035 + rhythm * 0.48 + quality * 0.16)
        evidence.append(f"facade rhythm score {rhythm:.2f}")
    elif component == "opening_unit":
        confidence = min(0.88, 0.08 + n * 0.032 + rhythm * 0.38 + quality * 0.20 + min(wide, 2) * 0.04)
        evidence.append(f"opening rhythm score {rhythm:.2f}")
    elif component == "horizontal_rail_band_unit":
        confidence = min(0.84, 0.06 + n * 0.030 + roofline * 0.26 + rhythm * 0.18 + quality * 0.16)
        evidence.append("uses combined horizontal bands and repeated vertical baluster evidence")
    elif component == "cornice_trim_unit":
        confidence = min(0.86, 0.07 + n * 0.032 + roofline * 0.44 + quality * 0.15)
        evidence.append(f"horizontal trim score {roofline:.2f}")
    elif component == "roof_plane_unit":
        confidence = min(0.82, 0.08 + n * 0.036 + roofline * 0.42 + diversity * 0.14)
        evidence.append(f"roofline/horizontal edge score {roofline:.2f}")
    else:
        confidence = min(0.54, 0.04 + n * 0.012 + quality * 0.14)
        evidence.append("details require explicit closeups or user promotion before drafting")
    if not n:
        evidence.append("no valid image pixels could be analyzed")
    return max(0.0, confidence), evidence


def evidence_images(analysis: dict[str, Any], component: str) -> list[dict[str, Any]]:
    images = [item for item in analysis["images"] if item.get("analyzable")]
    if component in {"vertical_support_unit", "opening_unit", "horizontal_rail_band_unit"}:
        images = sorted(images, key=lambda item: item.get("facade_rhythm_score", 0), reverse=True)
    elif component in {"roof_plane_unit", "cornice_trim_unit"}:
        images = sorted(images, key=lambda item: item.get("roofline_score", 0), reverse=True)
    elif component in {"primary_massing_blockout", "ground_base_unit"}:
        images = sorted(images, key=lambda item: (item.get("aspect_ratio", 0), item.get("image_quality_score", 0)), reverse=True)
    else:
        images = sorted(images, key=lambda item: item.get("image_quality_score", 0), reverse=True)
    return [
        {
            "name": item["name"],
            "aspect_ratio": item.get("aspect_ratio"),
            "facade_rhythm_score": item.get("facade_rhythm_score"),
            "roofline_score": item.get("roofline_score"),
            "quality": item.get("image_quality_score"),
        }
        for item in images[:6]
    ]


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
    rhythm = max(2, min(18, int(round(float(summary.get("average_vertical_peaks") or 5)))))
    key = component["component"]
    if key == "primary_massing_blockout":
        width = 4.0 + aspect * 2.2
        height = 2.6 + min(2.2, float(summary.get("average_roofline") or 0.3) * 1.7)
        depth = 1.6 + float(summary.get("view_diversity_score") or 0.2) * 2.6
        return [
            box_part("scaffold_main_volume_low_detail", "limestone", (width, depth, height), (0, 0, height / 2)),
            box_part("scaffold_front_reference_plane", "shadow", (width * 0.94, 0.05, height * 0.72), (0, -depth / 2 - 0.04, height * 0.45)),
        ]
    if key == "ground_base_unit":
        return [
            box_part("base_ground_contact_slab", "stone", (1.8, 0.9, 0.16), (0, 0, 0.08)),
            box_part("base_step_lower", "stone", (1.65, 0.72, 0.14), (0, -0.08, 0.23)),
            box_part("base_step_upper", "stone", (1.35, 0.52, 0.12), (0, -0.08, 0.36)),
            box_part("base_attachment_plinth", "limestone", (0.72, 0.42, 0.18), (0, -0.08, 0.51)),
        ]
    if key == "vertical_support_unit":
        parts: list[MeshPart] = [
            box_part("support_foot_plate", "stone", (0.52, 0.44, 0.12), (0, 0, 0.06)),
            cylinder_part("support_base_round", "limestone", 0.18, 0.16, (0, 0, 0.20), 24),
            cylinder_part("support_shaft_single_repeat", "limestone", 0.085, 2.05, (0, 0, 1.30), 24),
            cylinder_part("support_capital_round", "limestone", 0.20, 0.16, (0, 0, 2.40), 24),
            box_part("support_top_bearing_block", "limestone", (0.44, 0.38, 0.18), (0, 0, 2.57)),
        ]
        return parts
    if key == "opening_unit":
        mullion_count = max(2, min(4, rhythm // 5))
        parts = [
            box_part("opening_recess_shadow", "shadow", (0.92, 0.08, 1.48), (0, -0.08, 0.92)),
            box_part("opening_outer_frame_left", "limestone", (0.08, 0.16, 1.62), (-0.50, 0, 0.96)),
            box_part("opening_outer_frame_right", "limestone", (0.08, 0.16, 1.62), (0.50, 0, 0.96)),
            box_part("opening_outer_frame_top", "limestone", (1.08, 0.16, 0.08), (0, 0, 1.73)),
            box_part("opening_outer_frame_bottom", "limestone", (1.08, 0.16, 0.08), (0, 0, 0.15)),
            box_part("opening_glass_panel", "glass", (0.82, 0.04, 1.34), (0, -0.11, 0.92)),
            box_part("opening_midrail", "limestone", (0.82, 0.08, 0.045), (0, -0.14, 0.92)),
        ]
        for idx, x in enumerate(np_linspace(-0.28, 0.28, mullion_count)):
            parts.append(box_part(f"opening_mullion_{idx:02d}", "limestone", (0.04, 0.08, 1.32), (x, -0.14, 0.92)))
        return parts
    if key == "horizontal_rail_band_unit":
        parts = [
            box_part("rail_bottom_bar", "limestone", (1.6, 0.16, 0.10), (0, 0, 0.18)),
            box_part("rail_top_bar", "limestone", (1.6, 0.16, 0.12), (0, 0, 0.88)),
        ]
        for idx, x in enumerate(np_linspace(-0.66, 0.66, 5)):
            parts.append(cylinder_part(f"rail_baluster_{idx:02d}", "limestone", 0.045, 0.62, (x, 0, 0.54), 14))
        return parts
    if key == "cornice_trim_unit":
        parts = [
            box_part("cornice_backing_strip", "limestone", (1.8, 0.18, 0.22), (0, 0, 0.42)),
            box_part("cornice_projecting_lip", "limestone", (1.9, 0.34, 0.10), (0, -0.08, 0.58)),
            box_part("cornice_lower_shadow_line", "shadow", (1.78, 0.04, 0.05), (0, -0.20, 0.27)),
        ]
        for idx, x in enumerate(np_linspace(-0.72, 0.72, 7)):
            parts.append(box_part(f"cornice_dentil_{idx:02d}", "limestone", (0.08, 0.16, 0.16), (x, -0.10, 0.18)))
        return parts
    if key == "roof_plane_unit":
        width = min(3.2, 1.2 + aspect * 0.34)
        return [
            box_part("roof_slice_bearing_plate", "limestone", (width, 0.34, 0.16), (0, 0, 0.08)),
            box_part("roof_slice_sloped_plane_proxy", "roof", (width * 1.04, 1.15, 0.14), (0, 0.18, 0.45)),
            box_part("roof_slice_front_eave", "roof", (width * 1.08, 0.16, 0.18), (0, -0.43, 0.30)),
            box_part("roof_slice_ridge_marker", "roof", (width * 0.94, 0.10, 0.18), (0, 0.72, 0.59)),
        ]
    if key == "detail_or_ornament_unit":
        return [
            box_part("detail_attachment_pad", "stone", (0.46, 0.30, 0.10), (0, 0, 0.05)),
            cylinder_part("detail_vertical_axis_placeholder", "accent", 0.045, 0.65, (0, 0, 0.42), 14),
            box_part("detail_cap_placeholder", "accent", (0.24, 0.18, 0.12), (0, 0, 0.80)),
        ]
    return []


def np_linspace(start: float, stop: float, count: int) -> list[float]:
    if count <= 1:
        return [(start + stop) / 2]
    step = (stop - start) / (count - 1)
    return [start + step * idx for idx in range(count)]


def build_assembly_preview(project_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    units = manifest.get("outputs", {}).get("units", {}).get("units", [])
    built_components = {unit["component"]: unit for unit in units if unit.get("built")}
    analysis = {}
    component_path = project_dir / str(manifest.get("component_plan", "components/component_plan.json"))
    if component_path.exists():
        analysis = json.loads(component_path.read_text(encoding="utf-8")).get("image_analysis", {})
    summary = analysis.get("summary", {})
    width = 8.0 + float(summary.get("average_aspect_ratio") or 2.0) * 2.0
    bay_count = max(4, min(16, int(round(float(summary.get("average_vertical_peaks") or 8) / 2))))
    spacing = width / max(1, bay_count)
    parts = [
        box_part("assembly_ground_slab", "stone", (width + 1.0, 4.2, 0.16), (0, 0, 0.08)),
        box_part("assembly_front_structural_backing", "limestone", (width, 0.24, 3.55), (0, -2.14, 1.92)),
    ]
    if "primary_massing_blockout" in built_components:
        unit_parts = generic_unit_parts(built_components["primary_massing_blockout"], summary)
        parts.extend([part.transformed(name_prefix="assembly_scaffold__", translate=(0, 0, 0.12)) for part in unit_parts])
    if "ground_base_unit" in built_components:
        unit_parts = generic_unit_parts(built_components["ground_base_unit"], summary)
        for idx in range(bay_count):
            px = -width / 2 + spacing * (idx + 0.5)
            parts.extend([part.transformed(name_prefix=f"assembly_base_{idx:02d}__", translate=(px, -1.98, 0.12), scale=(min(1.0, spacing / 1.8), 1, 1)) for part in unit_parts])
    if "opening_unit" in built_components:
        unit_parts = generic_unit_parts(built_components["opening_unit"], summary)
        for idx in range(bay_count):
            px = -width / 2 + spacing * (idx + 0.5)
            parts.extend([part.transformed(name_prefix=f"assembly_opening_{idx:02d}__", translate=(px, -2.14, 0.70), scale=(min(1.1, spacing / 1.25), 1, 1.08)) for part in unit_parts])
    if "vertical_support_unit" in built_components:
        unit_parts = generic_unit_parts(built_components["vertical_support_unit"], summary)
        for idx in range(bay_count + 1):
            px = -width / 2 + spacing * idx
            parts.extend([part.transformed(name_prefix=f"assembly_support_{idx:02d}__", translate=(px, -2.28, 0.55), scale=(1, 1, 1.12)) for part in unit_parts])
    if "horizontal_rail_band_unit" in built_components:
        unit_parts = generic_unit_parts(built_components["horizontal_rail_band_unit"], summary)
        for idx in range(bay_count):
            px = -width / 2 + spacing * (idx + 0.5)
            parts.extend([part.transformed(name_prefix=f"assembly_rail_{idx:02d}__", translate=(px, -2.34, 2.70), scale=(min(1.1, spacing / 1.55), 1, 1)) for part in unit_parts])
    if "cornice_trim_unit" in built_components:
        unit_parts = generic_unit_parts(built_components["cornice_trim_unit"], summary)
        for idx in range(bay_count):
            px = -width / 2 + spacing * (idx + 0.5)
            parts.extend([part.transformed(name_prefix=f"assembly_cornice_{idx:02d}__", translate=(px, -2.16, 3.35), scale=(min(1.1, spacing / 1.7), 1, 1)) for part in unit_parts])
    if "roof_plane_unit" in built_components:
        unit_parts = generic_unit_parts(built_components["roof_plane_unit"], summary)
        roof_segments = max(2, min(8, bay_count // 2))
        roof_spacing = width / roof_segments
        for idx in range(roof_segments):
            px = -width / 2 + roof_spacing * (idx + 0.5)
            parts.extend([part.transformed(name_prefix=f"assembly_roof_{idx:02d}__", translate=(px, -0.15, 3.82), scale=(min(1.4, roof_spacing / 2.2), 1.35, 1.15)) for part in unit_parts])
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
    button, input { font: inherit; }
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
    input[type="text"] {
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
        <article class="card">
          <h3>2. Check Image Coverage</h3>
          <p>Before geometry is drafted, Sparse2Solid checks whether the images cover the views needed for a trustworthy unit-first model.</p>
          <button id="planBtn" class="orange">Check Missing Views</button>
          <div id="planSummary" class="checklist" style="margin-top:16px;"></div>
        </article>
        <article class="card">
          <h3>3. Discover Small Geometry Units</h3>
          <p>The next pass proposes small reusable primitives: base/steps, one support, one opening, one rail segment, one cornice strip, and one roof slice. The coarse mass is only a measuring scaffold.</p>
          <button id="componentsBtn" class="blue">Identify Small Units</button>
          <div id="componentSummary" class="unit-list"></div>
        </article>
        <article class="card">
          <h3>4. Draft Unit Geometry</h3>
          <p>Ready units are drafted independently and get their own OBJ/MTL plus orbit sheet. Low-confidence units ask for targeted images first.</p>
          <button id="unitsBtn" class="blue">Build Unit Drafts</button>
          <div id="unitOutputs" class="unit-list"></div>
        </article>
        <article class="card third">
          <h3>5. Assemble Preview</h3>
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
    let state = { projects: [], active: null, project: null, plan: null, components: null };
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
        <div>  reference_plan.json <span class="small">missing-view checklist</span></div>
        <div>  components/component_plan.json <span class="small">small-unit candidates</span></div>
        <div>  outputs/units/ <span class="small">per-unit OBJ/MTL/orbits</span></div>
        <div>  outputs/assembly/ <span class="small">full model preview</span></div>
      `;
      $("events").innerHTML = (project.events || []).map(e => `<div class="event">${e.at}<br>${e.message}</div>`).join("") || `<p class="small">No activity yet.</p>`;
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
          <p class="small"><strong>Role:</strong> ${(component.role || "unit").replaceAll("_", " ")}</p>
          <p class="small"><strong>Status:</strong> ${component.status.replaceAll("_", " ")}</p>
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
