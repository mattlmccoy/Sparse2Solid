from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np


Color = tuple[float, float, float]


DEFAULT_MATERIALS: dict[str, Color] = {
    "limestone": (0.83, 0.81, 0.74),
    "shadow": (0.08, 0.12, 0.12),
    "glass": (0.55, 0.78, 0.86),
    "roof": (0.56, 0.11, 0.05),
    "metal": (0.06, 0.09, 0.09),
    "stone": (0.45, 0.45, 0.40),
    "accent": (0.0, 0.48, 0.35),
    "warm_glass": (1.0, 0.92, 0.72),
}


@dataclass(frozen=True)
class MeshPart:
    """A named, materialized watertight mesh fragment.

    ViewForge keeps semantic parts separate until late in the pipeline. That
    makes user review and revision precise: "move the left arch ring forward"
    is easier to satisfy than "fix the mesh."
    """

    name: str
    material: str
    vertices: np.ndarray
    faces: tuple[tuple[int, int, int], ...]

    @property
    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        return self.vertices.min(axis=0), self.vertices.max(axis=0)

    def transformed(
        self,
        *,
        translate: tuple[float, float, float] = (0.0, 0.0, 0.0),
        scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
        rotate_z_deg: float = 0.0,
        name_prefix: str = "",
    ) -> "MeshPart":
        angle = math.radians(rotate_z_deg)
        rot = np.array(
            [
                [math.cos(angle), -math.sin(angle), 0.0],
                [math.sin(angle), math.cos(angle), 0.0],
                [0.0, 0.0, 1.0],
            ],
            dtype=float,
        )
        verts = self.vertices * np.asarray(scale, dtype=float)
        verts = verts @ rot.T
        verts = verts + np.asarray(translate, dtype=float)
        return MeshPart(f"{name_prefix}{self.name}", self.material, verts, self.faces)


def box_part(name: str, material: str, size: tuple[float, float, float], center: tuple[float, float, float]) -> MeshPart:
    sx, sy, sz = (value / 2.0 for value in size)
    cx, cy, cz = center
    vertices = np.array(
        [
            [cx - sx, cy - sy, cz - sz],
            [cx + sx, cy - sy, cz - sz],
            [cx + sx, cy + sy, cz - sz],
            [cx - sx, cy + sy, cz - sz],
            [cx - sx, cy - sy, cz + sz],
            [cx + sx, cy - sy, cz + sz],
            [cx + sx, cy + sy, cz + sz],
            [cx - sx, cy + sy, cz + sz],
        ],
        dtype=float,
    )
    faces = (
        (1, 2, 3), (1, 3, 4),
        (5, 8, 7), (5, 7, 6),
        (1, 5, 6), (1, 6, 2),
        (2, 6, 7), (2, 7, 3),
        (3, 7, 8), (3, 8, 4),
        (4, 8, 5), (4, 5, 1),
    )
    return MeshPart(name, material, vertices, faces)


def cylinder_part(
    name: str,
    material: str,
    radius: float,
    height: float,
    center: tuple[float, float, float],
    segments: int = 32,
) -> MeshPart:
    cx, cy, cz = center
    vertices: list[list[float]] = [[cx, cy, cz - height / 2.0], [cx, cy, cz + height / 2.0]]
    for z in (cz - height / 2.0, cz + height / 2.0):
        for idx in range(segments):
            angle = 2.0 * math.pi * idx / segments
            vertices.append([cx + math.cos(angle) * radius, cy + math.sin(angle) * radius, z])
    faces: list[tuple[int, int, int]] = []
    bottom_start = 3
    top_start = 3 + segments
    for idx in range(segments):
        nxt = (idx + 1) % segments
        faces.append((1, bottom_start + nxt, bottom_start + idx))
        faces.append((2, top_start + idx, top_start + nxt))
        faces.append((bottom_start + idx, bottom_start + nxt, top_start + nxt))
        faces.append((bottom_start + idx, top_start + nxt, top_start + idx))
    return MeshPart(name, material, np.asarray(vertices, dtype=float), tuple(faces))


def sphere_part(
    name: str,
    material: str,
    radius: float,
    center: tuple[float, float, float],
    rings: int = 12,
    segments: int = 24,
) -> MeshPart:
    cx, cy, cz = center
    vertices: list[list[float]] = [[cx, cy, cz + radius], [cx, cy, cz - radius]]
    for ring in range(1, rings):
        phi = math.pi * ring / rings
        z = cz + math.cos(phi) * radius
        r = math.sin(phi) * radius
        for idx in range(segments):
            theta = 2.0 * math.pi * idx / segments
            vertices.append([cx + math.cos(theta) * r, cy + math.sin(theta) * r, z])
    faces: list[tuple[int, int, int]] = []
    first_ring = 3
    for idx in range(segments):
        faces.append((1, first_ring + idx, first_ring + ((idx + 1) % segments)))
    for ring in range(rings - 2):
        a = 3 + ring * segments
        b = a + segments
        for idx in range(segments):
            nxt = (idx + 1) % segments
            faces.append((a + idx, b + idx, b + nxt))
            faces.append((a + idx, b + nxt, a + nxt))
    last_ring = 3 + (rings - 2) * segments
    for idx in range(segments):
        faces.append((2, last_ring + ((idx + 1) % segments), last_ring + idx))
    return MeshPart(name, material, np.asarray(vertices, dtype=float), tuple(faces))


def arch_ring_parts(
    name: str,
    material: str,
    center_x: float,
    y: float,
    base_z: float,
    radius: float,
    thickness: float,
    depth: float,
    segments: int = 18,
) -> list[MeshPart]:
    """Approximate an applied arch ring with small blocks.

    The blocks intentionally remain named pieces. Review tooling can select a
    single voussoir-like block or the entire group.
    """

    parts: list[MeshPart] = []
    for idx in range(segments + 1):
        t = math.pi * idx / segments
        x = center_x + math.cos(t) * radius
        z = base_z + math.sin(t) * radius
        tangent = math.degrees(t) - 90.0
        block = box_part(f"{name}_{idx:02d}", material, (thickness, depth, thickness * 1.35), (0.0, 0.0, 0.0))
        parts.append(block.transformed(translate=(x, y, z), rotate_z_deg=tangent))
    parts.append(box_part(f"{name}_left_leg", material, (thickness, depth, radius), (center_x - radius, y, base_z / 2.0)))
    parts.append(box_part(f"{name}_right_leg", material, (thickness, depth, radius), (center_x + radius, y, base_z / 2.0)))
    return parts


def write_mtl(path: Path, materials: dict[str, Color] | None = None) -> None:
    material_map = materials or DEFAULT_MATERIALS
    lines: list[str] = []
    for name, (r, g, b) in material_map.items():
        lines.extend([f"newmtl {name}", f"Kd {r:.4f} {g:.4f} {b:.4f}", "Ka 0.05 0.05 0.05", "Ks 0.15 0.15 0.15", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def write_obj(path: Path, mtl_name: str, object_name: str, parts: Iterable[MeshPart]) -> None:
    lines = [f"mtllib {mtl_name}", f"o {object_name}"]
    vertex_offset = 0
    for part in parts:
        lines.append(f"g {part.name}")
        lines.append(f"usemtl {part.material}")
        for x, y, z in part.vertices:
            lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
        for a, b, c in part.faces:
            lines.append(f"f {a + vertex_offset} {b + vertex_offset} {c + vertex_offset}")
        vertex_offset += len(part.vertices)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def export_parts(out_dir: Path, stem: str, parts: list[MeshPart]) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    mtl_path = out_dir / f"{stem}.mtl"
    obj_path = out_dir / f"{stem}.obj"
    write_mtl(mtl_path)
    write_obj(obj_path, mtl_path.name, stem, parts)
    return {"obj": str(obj_path), "mtl": str(mtl_path)}
