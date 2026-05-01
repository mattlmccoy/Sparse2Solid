from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .connectivity import structural_connectivity
from .geometry import MeshPart, arch_ring_parts, box_part, cylinder_part, export_parts, sphere_part


@dataclass(frozen=True)
class Component:
    name: str
    purpose: str
    parts: tuple[MeshPart, ...]

    def placed(
        self,
        instance_id: str,
        translate: tuple[float, float, float],
        scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
        rotate_z_deg: float = 0.0,
    ) -> list[MeshPart]:
        return [
            part.transformed(translate=translate, scale=scale, rotate_z_deg=rotate_z_deg, name_prefix=f"{instance_id}__")
            for part in self.parts
        ]


def facade_bay_component() -> Component:
    parts: list[MeshPart] = [
        box_part("wall_panel", "limestone", (2.4, 0.26, 3.3), (0, 0, 1.65)),
        box_part("recess_shadow", "shadow", (1.34, 0.08, 2.15), (0, -0.18, 1.35)),
        box_part("door_left", "glass", (0.48, 0.05, 1.55), (-0.26, -0.24, 0.88)),
        box_part("door_right", "glass", (0.48, 0.05, 1.55), (0.26, -0.24, 0.88)),
        box_part("center_mullion", "limestone", (0.08, 0.08, 1.65), (0, -0.28, 0.9)),
        box_part("lower_sill", "stone", (2.65, 0.34, 0.22), (0, -0.02, 0.11)),
        box_part("entablature", "limestone", (2.85, 0.38, 0.34), (0, 0, 3.38)),
    ]
    parts.extend(arch_ring_parts("applied_arch_ring", "limestone", 0.0, -0.25, 1.42, 0.86, 0.12, 0.28))
    for x in (-1.18, 1.18):
        parts.extend(
            [
                cylinder_part(f"column_{x}_shaft", "limestone", 0.13, 2.65, (x, -0.34, 1.55), 24),
                cylinder_part(f"column_{x}_base", "stone", 0.18, 0.18, (x, -0.34, 0.27), 24),
                cylinder_part(f"column_{x}_cap", "limestone", 0.18, 0.16, (x, -0.34, 2.9), 24),
            ]
        )
    for idx, x in enumerate([-0.45, -0.22, 0.0, 0.22, 0.45]):
        parts.append(box_part(f"fanlight_muntin_{idx}", "limestone", (0.055, 0.075, 0.78), (x, -0.33, 2.02)).transformed(rotate_z_deg=x * 42.0))
    return Component("facade_bay", "repeatable arched facade bay from front elevation photos", tuple(parts))


def lamp_component() -> Component:
    parts: list[MeshPart] = [
        box_part("stone_plinth", "stone", (0.74, 0.74, 0.28), (0, 0, 0.14)),
        box_part("base_foot", "accent", (0.54, 0.54, 0.16), (0, 0, 0.36)),
        cylinder_part("base_round", "accent", 0.23, 0.14, (0, 0, 0.52), 32),
        cylinder_part("fluted_pole", "accent", 0.075, 2.25, (0, 0, 1.70), 28),
        cylinder_part("capital_lower_disc", "accent", 0.27, 0.08, (0, 0, 2.88), 32),
        cylinder_part("capital_upper_disc", "accent", 0.22, 0.08, (0, 0, 3.02), 32),
        cylinder_part("globe_socket", "accent", 0.13, 0.17, (0, 0, 3.17), 32),
        sphere_part("warm_glass_globe", "warm_glass", 0.34, (0, 0, 3.55), 14, 28),
        cylinder_part("internal_support_spine", "accent", 0.045, 3.35, (0, 0, 1.88), 20),
    ]
    return Component("lamp", "ornamental exterior lamp inferred from detail photos", tuple(parts))


def roof_component() -> Component:
    parts = [
        box_part("upper_level_wall", "limestone", (14.5, 3.5, 1.25), (0, 0, 0.62)),
        box_part("roof_bearing_plate", "limestone", (14.8, 3.8, 0.46), (0, 0, 1.43)),
        box_part("green_white_awning", "accent", (10.8, 2.1, 0.13), (0, -2.7, 0.55)).transformed(rotate_z_deg=0),
        box_part("awning_front_rail", "metal", (11.2, 0.08, 0.12), (0, -3.78, 0.42)),
        box_part("awning_back_anchor", "metal", (11.2, 0.08, 0.18), (0, -1.62, 0.72)),
        box_part("terracotta_roof_front_slope", "roof", (14.9, 4.4, 0.16), (0, -0.82, 1.72)).transformed(rotate_z_deg=0),
        box_part("terracotta_roof_rear_slope", "roof", (14.9, 4.4, 0.16), (0, 0.82, 1.72)).transformed(rotate_z_deg=0),
        box_part("roof_ridge", "roof", (14.9, 0.18, 0.18), (0, 0, 1.88)),
    ]
    for x in [-5.2, -2.6, 0, 2.6, 5.2]:
        parts.append(cylinder_part(f"awning_support_{x}", "metal", 0.035, 1.5, (x, -3.65, -0.18), 12))
    return Component("upper_level_roof_awning", "second level, awning, roof, and support logic", tuple(parts))


COMPONENT_LIBRARY = {
    "facade_bay": facade_bay_component,
    "lamp": lamp_component,
    "upper_level_roof_awning": roof_component,
}


def build_from_spec(spec: dict[str, Any]) -> list[MeshPart]:
    components = {name: factory() for name, factory in COMPONENT_LIBRARY.items()}
    parts: list[MeshPart] = []
    for placement in spec["placements"]:
        component = components[placement["component"]]
        parts.extend(
            component.placed(
                placement["id"],
                tuple(placement.get("translate", (0, 0, 0))),
                tuple(placement.get("scale", (1, 1, 1))),
                float(placement.get("rotate_z_deg", 0.0)),
            )
        )
    return parts


def write_project(spec_path: Path, out_dir: Path) -> dict[str, Any]:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    parts = build_from_spec(spec)
    assets = export_parts(out_dir, spec.get("model_id", "viewforge_model"), parts)
    connectivity = structural_connectivity(parts)
    report = {
        "model_id": spec.get("model_id", "viewforge_model"),
        "source_spec": str(spec_path),
        "outputs": assets,
        "part_count": len(parts),
        "connectivity": connectivity.__dict__,
        "notes": [
            "This demo model is component-guided: sparse image observations are translated into reusable semantic units.",
            "The OBJ preserves named groups so review/orbit tools can isolate problem areas.",
        ],
    }
    report_path = out_dir / f"{report['model_id']}_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    report["outputs"]["report"] = str(report_path)
    return report
