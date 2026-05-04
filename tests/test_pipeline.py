from pathlib import Path
from io import BytesIO

from PIL import Image, ImageDraw

from sparse2solid.assembly import build_from_spec, write_project
from sparse2solid.connectivity import structural_connectivity
from sparse2solid.gui import create_app
from sparse2solid.orbit import parse_obj, render_orbit_set
from sparse2solid.reference_planner import plan_reference_set


ROOT = Path(__file__).resolve().parents[1]


def sample_facade_image() -> BytesIO:
    image = Image.new("RGB", (1200, 720), (236, 232, 224))
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 190, 1120, 620), fill=(248, 246, 238), outline=(110, 104, 96), width=5)
    draw.polygon([(70, 190), (600, 70), (1130, 190)], fill=(156, 77, 45), outline=(92, 48, 34))
    for idx in range(9):
        x = 150 + idx * 110
        draw.rectangle((x, 260, x + 58, 560), fill=(86, 115, 130), outline=(60, 70, 76), width=3)
        draw.line((x + 29, 260, x + 29, 560), fill=(248, 246, 238), width=4)
        draw.line((x, 395, x + 58, 395), fill=(248, 246, 238), width=4)
        draw.line((x - 18, 205, x - 18, 620), fill=(142, 136, 126), width=6)
    draw.rectangle((60, 620, 1140, 660), fill=(150, 146, 138))
    buf = BytesIO()
    image.save(buf, format="JPEG", quality=92)
    buf.seek(0)
    return buf


def test_reference_plan_explains_sparse_capture():
    plan = plan_reference_set("test")
    assert plan["recommended_image_count"]["minimum_workable"] < 20
    assert any(item["view"] == "repeatable_unit_closeups" for item in plan["checklist"])


def test_demo_spec_builds_named_connected_parts():
    import json

    spec = json.loads((ROOT / "examples" / "classical_facade_project.json").read_text())
    parts = build_from_spec(spec)
    names = [part.name for part in parts]
    assert any("front_bay_01__applied_arch_ring" in name for name in names)
    assert any("upper_roof__green_white_awning" in name for name in names)
    report = structural_connectivity(parts, tolerance=0.08)
    assert report.grounded
    assert report.floating_count == 0


def test_project_exports_obj_and_orbit(tmp_path):
    project_report = write_project(ROOT / "examples" / "classical_facade_project.json", tmp_path)
    obj = Path(project_report["outputs"]["obj"])
    mtl = Path(project_report["outputs"]["mtl"])
    assert obj.exists()
    assert mtl.exists()
    vertices, faces = parse_obj(obj)
    assert len(vertices) > 100
    assert len(faces) > 100
    orbit = render_orbit_set(obj, mtl, tmp_path / "orbits", frame_count=4)
    assert Path(orbit["contact_sheet"]).exists()


def test_gui_guides_project_upload_plan_and_build(tmp_path):
    app = create_app(tmp_path / "workspace")
    client = app.test_client()

    created = client.post("/api/projects", json={"name": "Test Building"})
    assert created.status_code == 200
    slug = created.get_json()["slug"]

    upload = client.post(
        f"/api/projects/{slug}/images",
        data={"images": (sample_facade_image(), "front-view.jpg")},
        content_type="multipart/form-data",
    )
    assert upload.status_code == 200
    assert upload.get_json()["saved"] == ["front-view.jpg"]

    planned = client.post(f"/api/projects/{slug}/plan")
    assert planned.status_code == 200
    plan = planned.get_json()["plan"]
    assert plan["available_images"]
    assert any(item["view"] == "hero_front" for item in plan["checklist"])

    labeled = client.post(
        f"/api/projects/{slug}/training-examples",
        json={
            "image_name": "front-view.jpg",
            "label": "opening",
            "bbox_normalized": [0.12, 0.36, 0.21, 0.78],
            "notes": "keep one representative facade opening",
        },
    )
    assert labeled.status_code == 200
    examples = labeled.get_json()["examples"]
    assert examples[0]["label"] == "opening"
    assert (tmp_path / "workspace" / slug / examples[0]["crop_path"]).exists()

    ignored = client.post(
        f"/api/projects/{slug}/training-examples",
        json={
            "image_name": "front-view.jpg",
            "label": "ignore",
            "bbox_normalized": [0.0, 0.0, 0.08, 0.12],
            "notes": "ignore sky/background corner",
        },
    )
    assert ignored.status_code == 200

    discovered = client.post(f"/api/projects/{slug}/components")
    assert discovered.status_code == 200
    component_plan = discovered.get_json()["component_plan"]
    assert component_plan["analyzable_image_count"] == 1
    assert component_plan["image_analysis"]["summary"]["average_facade_rhythm"] > 0
    assert component_plan["strategy"] == "pixel_evidence_unit_discovery"
    assert any(component["component"].startswith("visual_unit_") for component in component_plan["components"])
    assert any(component.get("crop_path") for component in component_plan["components"])
    assert any(component.get("source") == "manual" for component in component_plan["components"])
    assert any(component.get("kind") in {"vertical_repeat", "horizontal_band", "upper_edge_or_roofline", "opening_or_shadow_region"} for component in component_plan["components"])

    units = client.post(f"/api/projects/{slug}/build-units")
    assert units.status_code == 200
    unit_report = units.get_json()["report"]
    assert unit_report["built_count"] >= 1
    assert unit_report["units"][0]["output_folder"].startswith("outputs/units/")

    assembled = client.post(f"/api/projects/{slug}/assemble")
    assert assembled.status_code == 200
    report = assembled.get_json()["report"]
    assert report["source"] == "uploaded_images_and_component_plan"
    assert report["outputs"]["obj"]["url"].endswith(".obj")
    assert report["connectivity"]["grounded"]
    assert report["orbit"]["contact_sheet"]["url"].endswith("contact_sheet.jpg")
