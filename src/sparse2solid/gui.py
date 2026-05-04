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

from .assembly import COMPONENT_LIBRARY, write_project
from .demo import DEFAULT_SPEC
from .geometry import export_parts
from .orbit import render_orbit_set
from .reference_planner import plan_reference_set


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic"}

COMPONENT_HINTS = [
    {
        "component": "facade_bay",
        "label": "Facade bay / repeated wall opening",
        "why": "Most architectural models are controlled by the repeating bay rhythm: arches, windows, doors, columns, and trim.",
        "keywords": ["front", "facade", "arch", "door", "window", "elevation", "bay"],
        "needs": ["straight-on front elevation", "one closeup of a representative bay", "left or right oblique showing depth"],
        "priority": 1,
    },
    {
        "component": "lamp",
        "label": "Freestanding lamp or small repeated ornament",
        "why": "Small repeated objects should be solved once as units, then instanced across the site.",
        "keywords": ["lamp", "lamppost", "light", "globe", "detail"],
        "needs": ["closeup with full height", "ground-contact view", "oblique view showing depth"],
        "priority": 2,
    },
    {
        "component": "upper_level_roof_awning",
        "label": "Upper level, roof, awning, or canopy",
        "why": "Roof geometry is often underconstrained from front photos and should be treated as its own unit.",
        "keywords": ["roof", "awning", "parapet", "terrace", "upper", "side", "rear"],
        "needs": ["roofline photo", "side or rear view", "oblique view showing where supports land"],
        "priority": 3,
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


def keyword_score(records: list[dict[str, str]], keywords: list[str]) -> int:
    text = " ".join(record["name"].lower() for record in records)
    return sum(1 for keyword in keywords if keyword in text)


def component_plan(project_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    images = image_records(project_dir)
    planned = []
    for hint in COMPONENT_HINTS:
        score = keyword_score(images, hint["keywords"])
        broad_bonus = 1 if images and hint["component"] == "facade_bay" else 0
        confidence = min(0.86, 0.24 + 0.14 * len(images) + 0.16 * score + 0.12 * broad_bonus)
        status = "ready_for_unit_draft" if confidence >= 0.66 else "needs_more_images"
        matched = [
            record
            for record in images
            if any(keyword in record["name"].lower() for keyword in hint["keywords"])
        ][:6]
        planned.append(
            {
                "component": hint["component"],
                "label": hint["label"],
                "why": hint["why"],
                "confidence": round(confidence, 2),
                "status": status,
                "matched_images": matched,
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
        "image_count": len(images),
        "strategy": "unit_first_reconstruction",
        "components": planned,
        "ignored_by_default": ignored,
        "next_step": "Build ready unit drafts, then request targeted images for low-confidence units before assembly.",
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
        factory = COMPONENT_LIBRARY.get(component["component"])
        if not factory:
            units.append({**component, "built": False, "reason": "No component generator exists yet."})
            continue
        unit = factory()
        out_dir = project_dir / "outputs" / "units" / component["component"]
        outputs = export_parts(out_dir, component["component"], list(unit.parts))
        orbit = render_orbit_set(Path(outputs["obj"]), Path(outputs["mtl"]), out_dir / "orbits", title=f"{component['label']} unit orbit", frame_count=8)
        units.append(
            {
                **component,
                "built": True,
                "purpose": unit.purpose,
                "outputs": relativize_report(project_dir, outputs),
                "orbit": relativize_report(project_dir, orbit),
                "part_count": len(unit.parts),
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
        event(manifest, f"Uploaded {len(saved)} image(s).")
        write_manifest(project_dir, manifest)
        return jsonify({"saved": saved, "project": manifest})

    @app.post("/api/projects/<slug>/plan")
    def build_plan(slug: str):
        project_dir = safe_project(project_root, slug)
        manifest = project_manifest(project_dir)
        images = [str(project_dir / record["path"]) for record in image_records(project_dir)]
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
        out_dir = project_dir / "outputs" / "assembly"
        report = write_project(DEFAULT_SPEC, out_dir)
        orbit_report = render_orbit_set(
            obj_path=Path(report["outputs"]["obj"]),
            mtl_path=Path(report["outputs"]["mtl"]),
            out_dir=out_dir / "orbits",
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
        out_dir = project_dir / "outputs" / "assembly"
        report = write_project(DEFAULT_SPEC, out_dir)
        orbit_report = render_orbit_set(Path(report["outputs"]["obj"]), Path(report["outputs"]["mtl"]), out_dir / "orbits", title=f"{manifest.get('name', slug)} assembly orbit")
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
      --ink: #17181c;
      --muted: #6d7280;
      --paper: rgba(255, 255, 255, 0.76);
      --paper-strong: rgba(255, 255, 255, 0.92);
      --line: rgba(40, 45, 58, 0.12);
      --blue: #007aff;
      --blue-2: #5ac8fa;
      --green: #34c759;
      --orange: #ff9f0a;
      --red: #ff453a;
      --purple: #af52de;
      --shadow: 0 24px 80px rgba(24, 32, 56, 0.14);
      --radius-xl: 32px;
      --radius: 20px;
      font-family: ui-rounded, "SF Pro Rounded", "SF Pro Display", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      background:
        radial-gradient(circle at 12% 8%, rgba(90, 200, 250, 0.30), transparent 28%),
        radial-gradient(circle at 86% 0%, rgba(255, 159, 10, 0.24), transparent 30%),
        linear-gradient(135deg, #f7f8fb 0%, #eef2f8 46%, #f8f2e9 100%);
    }
    button, input { font: inherit; }
    button {
      border: 0;
      border-radius: 999px;
      padding: 12px 18px;
      background: var(--ink);
      color: white;
      cursor: pointer;
      box-shadow: 0 12px 24px rgba(0, 0, 0, 0.10);
      transition: transform .18s ease, box-shadow .18s ease, background .18s ease;
    }
    button:hover { transform: translateY(-1px); box-shadow: 0 16px 28px rgba(0,0,0,.14); }
    button.secondary { background: rgba(255, 255, 255, 0.74); color: var(--ink); border: 1px solid var(--line); box-shadow: none; }
    button.blue { background: linear-gradient(135deg, var(--blue), var(--blue-2)); }
    button.orange { background: linear-gradient(135deg, var(--orange), #ff7a1a); color: #211100; }
    button:disabled { opacity: .45; cursor: not-allowed; transform: none; }
    .shell { display: grid; grid-template-columns: 360px 1fr; min-height: 100vh; }
    aside {
      position: sticky;
      top: 0;
      height: 100vh;
      padding: 28px;
      border-right: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.42);
      backdrop-filter: blur(30px);
      overflow: auto;
    }
    main { padding: 32px; }
    .brand {
      padding: 26px;
      border-radius: var(--radius-xl);
      background: rgba(255, 255, 255, 0.62);
      border: 1px solid rgba(255,255,255,.7);
      box-shadow: var(--shadow);
      margin-bottom: 24px;
    }
    .eyebrow { color: var(--blue); font-size: 12px; font-weight: 800; letter-spacing: .16em; text-transform: uppercase; }
    h1 { margin: 8px 0 6px; font-size: 54px; line-height: .88; letter-spacing: -0.06em; }
    h2 { margin: 0; font-size: 34px; letter-spacing: -0.045em; }
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
    .project-item.active { outline: 3px solid rgba(0, 122, 255, .25); background: rgba(255,255,255,.9); }
    .hero {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 24px;
      padding: 30px;
      background: var(--paper);
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
      background: rgba(52, 199, 89, .12);
      color: #176b31;
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
    .tutorial-row.done .badge { background: rgba(52,199,89,.14); color: #176b31; }
    .tutorial-row.current { outline: 3px solid rgba(0,122,255,.18); background: rgba(255,255,255,.88); }
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
      background: rgba(0, 122, 255, .12);
      color: var(--blue);
      font-weight: 900;
      flex: 0 0 auto;
    }
    .dropzone {
      border: 2px dashed rgba(0,122,255,.32);
      background: rgba(255,255,255,.48);
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
      background: rgba(0,122,255,.12);
      color: var(--blue);
      white-space: nowrap;
    }
    .confidence.low { background: rgba(255,159,10,.18); color: #8a4d00; }
    .needs { margin: 10px 0 0; padding-left: 18px; color: var(--muted); font-size: 13px; line-height: 1.4; }
    .preview img {
      width: 100%;
      border-radius: 22px;
      border: 1px solid var(--line);
      background: white;
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
      background: rgba(23, 24, 28, .90);
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
      background: rgba(255,255,255,.45);
      border: 1px solid rgba(255,255,255,.8);
      box-shadow: var(--shadow);
      padding: 42px;
    }
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
        <div class="eyebrow">Few Images To 3D</div>
        <h1>Sparse<br>2Solid</h1>
        <p>Build a clean 3D model by solving reusable parts first, then assembling the whole structure.</p>
      </section>
      <section class="card wide" style="padding:18px;min-height:auto;">
        <h3>Start Here</h3>
        <div class="field">
          <label for="projectName">Building or object name</label>
          <input id="projectName" type="text" placeholder="Prospect Park Boathouse" />
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
        </article>
        <article class="card">
          <h3>2. Check Image Coverage</h3>
          <p>Before geometry is drafted, Sparse2Solid checks whether the images cover the views needed for a trustworthy unit-first model.</p>
          <button id="planBtn" class="orange">Check Missing Views</button>
          <div id="planSummary" class="checklist" style="margin-top:16px;"></div>
        </article>
        <article class="card">
          <h3>3. Discover Geometry Units</h3>
          <p>The next pass identifies likely reusable units: facade bays, lamps, roofs, awnings, stairs, side pavilions, and other repeated elements. Trees and temporary objects stay ignored unless promoted.</p>
          <button id="componentsBtn" class="blue">Identify Units</button>
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
      $("projectStats").innerHTML = `
        <p><strong>Slug:</strong> ${project.slug}</p>
        <p><strong>Images:</strong> ${project.images.length}</p>
        <p><strong>Capture plan:</strong> ${project.reference_plan ? "complete" : "not yet"}</p>
        <p><strong>Unit plan:</strong> ${project.component_plan ? "complete" : "not yet"}</p>
        <p><strong>Unit drafts:</strong> ${project.outputs?.units ? "built" : "not yet"}</p>
      `;
      $("folderMap").innerHTML = `
        <div>projects/${project.slug}/</div>
        <div>  images/ <span class="small">uploaded references</span></div>
        <div>  reference_plan.json <span class="small">missing-view checklist</span></div>
        <div>  components/component_plan.json <span class="small">unit candidates</span></div>
        <div>  outputs/units/ <span class="small">per-unit OBJ/MTL/orbits</span></div>
        <div>  outputs/assembly/ <span class="small">full model preview</span></div>
      `;
      $("events").innerHTML = (project.events || []).map(e => `<div class="event">${e.at}<br>${e.message}</div>`).join("") || `<p class="small">No activity yet.</p>`;
      renderTutorial(project);
      updateLocks(project);
      if (project.reference_plan_data) renderPlan(project.reference_plan_data, false);
      else $("planSummary").innerHTML = `<p class="small">Upload images, then check coverage to see which views are strong or missing.</p>`;
      if (project.component_plan_data) renderComponents(project.component_plan_data, false);
      else $("componentSummary").innerHTML = `<p class="small">Coverage comes first. Unit discovery will appear here.</p>`;
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
          <p class="small"><strong>Status:</strong> ${component.status.replaceAll("_", " ")}</p>
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
          ${unit.built ? `<p><a href="${unit.outputs.obj.url}" target="_blank">OBJ</a> · <a href="${unit.outputs.mtl.url}" target="_blank">MTL</a> · <a href="${unit.orbit.contact_sheet.url}" target="_blank">Orbit sheet</a></p>` : ""}
        </div>
      `).join("");
    }

    function renderAssemblyOutputs(report) {
      const obj = report.outputs.obj;
      const mtl = report.outputs.mtl;
      const contact = report.orbit.contact_sheet;
      $("assemblyOutputs").innerHTML = `
        <p><a href="${obj.url}" target="_blank">OBJ</a> · <a href="${mtl.url}" target="_blank">MTL</a> · <a href="${report.outputs.report.url}" target="_blank">Report</a></p>
        <p class="small">Connectivity: ${report.connectivity.grounded ? "grounded" : "floating pieces found"} · ${report.part_count} named parts</p>
        <div class="preview"><img src="${contact.url}" alt="Assembly orbit contact sheet"></div>
      `;
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
