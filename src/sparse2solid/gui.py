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

from .assembly import write_project
from .demo import DEFAULT_SPEC
from .orbit import render_orbit_set
from .reference_planner import plan_reference_set


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

    @app.post("/api/projects/<slug>/build-demo")
    def build_demo(slug: str):
        project_dir = safe_project(project_root, slug)
        manifest = project_manifest(project_dir)
        out_dir = project_dir / "outputs" / "demo_model"
        report = write_project(DEFAULT_SPEC, out_dir)
        orbit_report = render_orbit_set(
            obj_path=Path(report["outputs"]["obj"]),
            mtl_path=Path(report["outputs"]["mtl"]),
            out_dir=out_dir / "orbits",
            title=f"{manifest.get('name', slug)} starter orbit",
        )
        report["orbit"] = orbit_report
        manifest.setdefault("outputs", {})["starter_model"] = relativize_report(project_dir, report)
        event(manifest, "Built starter semantic model and orbit contact sheet.")
        write_manifest(project_dir, manifest)
        return jsonify({"report": manifest["outputs"]["starter_model"], "project": manifest})

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
    .empty {
      min-height: 520px;
      display: grid;
      place-items: center;
      text-align: center;
      border-radius: var(--radius-xl);
      background: rgba(255,255,255,.45);
      border: 1px solid rgba(255,255,255,.8);
      box-shadow: var(--shadow);
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
        <p>Local studio for guiding sparse photo sets into clean semantic 3D reconstruction.</p>
      </section>
      <section class="card wide" style="padding:18px;min-height:auto;">
        <h3>New Project</h3>
        <div class="field">
          <label for="projectName">Building or object name</label>
          <input id="projectName" type="text" placeholder="Prospect Park Boathouse" />
        </div>
        <button class="blue" id="createProject">Create Project</button>
      </section>
      <h3 style="margin-top:26px;">Projects</h3>
      <div id="projectList" class="project-list"></div>
    </aside>
    <main>
      <section class="hero">
        <div>
          <div class="eyebrow">Guided Reconstruction Studio</div>
          <h2 id="heroTitle">Start with a project</h2>
          <p id="heroCopy">Create a project, upload 12-35 diverse images, generate a capture plan, then build and review the starter model.</p>
        </div>
        <div class="status-pill"><span class="loader"></span><span id="statusText">Ready</span></div>
      </section>
      <section id="emptyState" class="empty">
        <div>
          <h2>Make the workflow obvious.</h2>
          <p>Users should not need to know terminal commands. The studio turns the pipeline into guided steps.</p>
        </div>
      </section>
      <section id="projectPane" class="grid" hidden>
        <article class="card">
          <h3>1. Upload Sparse Reference Images</h3>
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
          <h3>2. Let Sparse2Solid Plan Missing Views</h3>
          <p>The planner does not pretend every image is equally useful. It tells the user what view types are needed and why.</p>
          <button id="planBtn" class="orange">Generate Capture Plan</button>
          <div id="planSummary" class="checklist" style="margin-top:16px;"></div>
        </article>
        <article class="card">
          <h3>3. Build Starter Semantic Model</h3>
          <p>V1 ships with a generic architectural starter model so new users can see the full pipeline immediately. Real image-conditioned component generation can slot behind this same button.</p>
          <button id="buildBtn" class="blue">Build Model + Orbit QA</button>
          <div id="modelLinks" style="margin-top:16px;"></div>
        </article>
        <article class="card">
          <h3>4. Review Orbit Contact Sheet</h3>
          <p>Orbit maps make geometry mistakes visible without opening Blender.</p>
          <div id="orbitPreview" class="preview"></div>
        </article>
        <article class="card third">
          <h3>What Images Should I Upload?</h3>
          <div class="step"><div class="badge">1</div><div><strong>Main elevation</strong><p class="small">Locks rhythm, proportions, and repeat count.</p></div></div>
          <div class="step"><div class="badge">2</div><div><strong>Two obliques</strong><p class="small">Shows depth and prevents flat facade guesses.</p></div></div>
          <div class="step"><div class="badge">3</div><div><strong>Closeups</strong><p class="small">Define reusable components like lamps, arches, windows.</p></div></div>
        </article>
        <article class="card third">
          <h3>Current Project</h3>
          <div id="projectStats"></div>
        </article>
        <article class="card third">
          <h3>Activity</h3>
          <div id="events" class="events"></div>
        </article>
      </section>
    </main>
  </div>
  <script>
    let state = { projects: [], active: null };
    const $ = (id) => document.getElementById(id);
    const setStatus = (text) => $("statusText").textContent = text;

    async function api(path, options = {}) {
      setStatus("Working");
      const res = await fetch(path, options);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Request failed");
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
      $("emptyState").hidden = true;
      $("projectPane").hidden = false;
      $("heroTitle").textContent = project.name;
      $("heroCopy").textContent = `${project.images.length} uploaded image(s). Generate a plan, then build a starter model and orbit QA sheet.`;
      renderProject(project);
      await refreshProjects();
    }

    function renderProject(project) {
      $("thumbs").innerHTML = project.images.map(image => `<div class="thumb"><img src="${image.url}" alt="${image.name}"></div>`).join("");
      $("projectStats").innerHTML = `
        <p><strong>Slug:</strong> ${project.slug}</p>
        <p><strong>Images:</strong> ${project.images.length}</p>
        <p><strong>Plan:</strong> ${project.reference_plan ? "generated" : "not yet"}</p>
      `;
      $("events").innerHTML = (project.events || []).map(e => `<div class="event">${e.at}<br>${e.message}</div>`).join("") || `<p class="small">No activity yet.</p>`;
      const starter = project.outputs && project.outputs.starter_model;
      if (starter) renderModelOutputs(starter);
    }

    function renderPlan(plan) {
      $("planSummary").innerHTML = plan.checklist.map(item => `
        <div class="check">
          <div><strong>${item.view.replaceAll("_", " ")}</strong><span>${item.purpose}</span></div>
          <span>${item.minimum}-${item.ideal}</span>
        </div>
      `).join("");
    }

    function renderModelOutputs(report) {
      const obj = report.outputs.obj;
      const mtl = report.outputs.mtl;
      const contact = report.orbit.contact_sheet;
      $("modelLinks").innerHTML = `
        <p><a href="${obj.url}" target="_blank">OBJ</a> · <a href="${mtl.url}" target="_blank">MTL</a> · <a href="${report.outputs.report.url}" target="_blank">Report</a></p>
        <p class="small">Connectivity: ${report.connectivity.grounded ? "grounded" : "floating pieces found"} · ${report.part_count} named parts</p>
      `;
      $("orbitPreview").innerHTML = `<img src="${contact.url}" alt="Orbit contact sheet">`;
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
      if (!state.active || !files.length) return;
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
      if (!state.active) return;
      const data = await api(`/api/projects/${state.active}/plan`, { method: "POST" });
      renderPlan(data.plan);
      await loadProject(state.active);
    });

    $("buildBtn").addEventListener("click", async () => {
      if (!state.active) return;
      const data = await api(`/api/projects/${state.active}/build-demo`, { method: "POST" });
      renderModelOutputs(data.report);
      await loadProject(state.active);
    });

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
