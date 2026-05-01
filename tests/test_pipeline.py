from pathlib import Path

from viewforge3d.assembly import build_from_spec, write_project
from viewforge3d.connectivity import structural_connectivity
from viewforge3d.orbit import parse_obj, render_orbit_set
from viewforge3d.reference_planner import plan_reference_set


ROOT = Path(__file__).resolve().parents[1]


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
