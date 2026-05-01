from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont


@dataclass(frozen=True)
class ObjFace:
    indices: tuple[int, int, int]
    material: str
    group: str


def parse_mtl(path: Path) -> dict[str, tuple[int, int, int]]:
    materials = {"default": (210, 204, 190)}
    current: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        parts = raw.strip().split()
        if not parts:
            continue
        if parts[0] == "newmtl":
            current = " ".join(parts[1:])
        elif parts[0] == "Kd" and current:
            materials[current] = tuple(max(0, min(255, int(float(value) * 255))) for value in parts[1:4])
    return materials


def parse_obj(path: Path) -> tuple[np.ndarray, list[ObjFace]]:
    vertices = [[0.0, 0.0, 0.0]]
    faces: list[ObjFace] = []
    group = "default"
    material = "default"
    for raw in path.read_text(encoding="utf-8").splitlines():
        parts = raw.strip().split()
        if not parts or parts[0].startswith("#"):
            continue
        if parts[0] == "v":
            vertices.append([float(parts[1]), float(parts[2]), float(parts[3])])
        elif parts[0] in {"g", "o"}:
            group = " ".join(parts[1:]) or group
        elif parts[0] == "usemtl":
            material = " ".join(parts[1:]) or "default"
        elif parts[0] == "f":
            ids = [int(item.split("/")[0]) for item in parts[1:]]
            for idx in range(1, len(ids) - 1):
                faces.append(ObjFace((ids[0], ids[idx], ids[idx + 1]), material, group))
    return np.asarray(vertices, dtype=float), faces


def _normalize(vector: np.ndarray) -> np.ndarray:
    length = np.linalg.norm(vector)
    return vector / length if length > 1e-9 else vector


def _project(
    vertices: np.ndarray,
    eye: np.ndarray,
    target: np.ndarray,
    fov: float,
    width: int,
    height: int,
) -> tuple[np.ndarray, np.ndarray]:
    forward = _normalize(target - eye)
    right = _normalize(np.cross(forward, np.array([0.0, 0.0, 1.0])))
    up = _normalize(np.cross(right, forward))
    rel = vertices - eye
    x = rel @ right
    y = rel @ up
    z = rel @ forward
    focal = 0.5 * width / math.tan(math.radians(fov) / 2.0)
    sx = width * 0.5 + focal * x / np.maximum(z, 1e-4)
    sy = height * 0.54 - focal * y / np.maximum(z, 1e-4)
    return np.column_stack([sx, sy]), z


def _shade(color: tuple[int, int, int], points: np.ndarray, eye: np.ndarray, face_index: int) -> tuple[int, int, int]:
    normal = _normalize(np.cross(points[1] - points[0], points[2] - points[0]))
    light = _normalize(np.array([-0.35, -0.45, 0.82]))
    facing = abs(float(normal @ _normalize(eye - points.mean(axis=0))))
    diffuse = max(0.0, float(normal @ light))
    texture = 0.94 + 0.12 * (((face_index * 1103515245 + 12345) & 0xFFFF) / 0xFFFF)
    factor = (0.58 + 0.34 * diffuse + 0.16 * facing) * texture
    return tuple(max(0, min(255, int(channel * factor))) for channel in color)


def render_view(
    vertices: np.ndarray,
    faces: list[ObjFace],
    materials: dict[str, tuple[int, int, int]],
    out_path: Path,
    *,
    title: str,
    angle_deg: float,
    target: tuple[float, float, float],
    radius: float,
    eye_z: float,
    fov: float,
    width: int = 1200,
    height: int = 800,
) -> Path:
    angle = math.radians(angle_deg)
    eye = np.array([target[0] + math.sin(angle) * radius, target[1] - math.cos(angle) * radius, eye_z], dtype=float)
    center = np.asarray(target, dtype=float)
    projected, depth = _project(vertices, eye, center, fov, width, height)

    image = Image.new("RGB", (width, height), (226, 229, 225))
    draw = ImageDraw.Draw(image)
    for grid_x in range(0, width, 80):
        draw.line([(grid_x, 0), (grid_x, height)], fill=(208, 214, 211))
    for grid_y in range(0, height, 80):
        draw.line([(0, grid_y), (width, grid_y)], fill=(208, 214, 211))

    render_faces: list[tuple[float, int, ObjFace, np.ndarray]] = []
    for face_index, face in enumerate(faces):
        z = depth[list(face.indices)]
        if np.any(z <= 0.05):
            continue
        points_2d = projected[list(face.indices)]
        if np.all((points_2d[:, 0] < -200) | (points_2d[:, 0] > width + 200) | (points_2d[:, 1] < -200) | (points_2d[:, 1] > height + 200)):
            continue
        render_faces.append((float(z.mean()), face_index, face, points_2d))
    render_faces.sort(key=lambda item: item[0], reverse=True)

    for _depth, face_index, face, points_2d in render_faces:
        points_3d = vertices[list(face.indices)]
        color = _shade(materials.get(face.material, materials["default"]), points_3d, eye, face_index)
        polygon = [(float(x), float(y)) for x, y in points_2d]
        draw.polygon(polygon, fill=color)
        if face_index % 4 == 0:
            draw.line(polygon + [polygon[0]], fill=(38, 49, 47), width=1)

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Avenir Next.ttc", 26)
    except OSError:
        font = ImageFont.load_default()
    draw.rounded_rectangle((20, 20, width - 20, 70), radius=14, fill=(20, 32, 31), outline=(255, 132, 54), width=2)
    draw.text((38, 30), f"{title} | {angle_deg:.1f} deg", fill=(255, 248, 225), font=font)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)
    return out_path


def make_contact_sheet(paths: list[Path], out_path: Path, columns: int = 6) -> Path:
    thumb = (320, 214)
    rows = math.ceil(len(paths) / columns)
    sheet = Image.new("RGB", (columns * thumb[0], rows * thumb[1]), (31, 42, 40))
    for idx, path in enumerate(paths):
        image = Image.open(path).convert("RGB")
        image.thumbnail((thumb[0] - 10, thumb[1] - 10))
        x = (idx % columns) * thumb[0] + (thumb[0] - image.width) // 2
        y = (idx // columns) * thumb[1] + (thumb[1] - image.height) // 2
        sheet.paste(image, (x, y))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out_path)
    return out_path


def render_orbit_set(
    obj_path: Path,
    mtl_path: Path,
    out_dir: Path,
    *,
    title: str = "Review orbit",
    frame_count: int = 18,
    radius: float | None = None,
    fov: float = 38.0,
) -> dict[str, object]:
    vertices, faces = parse_obj(obj_path)
    materials = parse_mtl(mtl_path)
    mn = vertices[1:].min(axis=0)
    mx = vertices[1:].max(axis=0)
    center = tuple(((mn + mx) / 2.0).tolist())
    model_radius = float(max(mx[0] - mn[0], mx[1] - mn[1]))
    orbit_radius = radius or model_radius * 1.35
    eye_z = float(mx[2] + max(1.2, (mx[2] - mn[2]) * 0.55))
    frame_paths: list[Path] = []
    for idx, angle in enumerate(np.linspace(-165, 165, frame_count), start=1):
        frame_paths.append(
            render_view(
                vertices,
                faces,
                materials,
                out_dir / f"orbit_{idx:03d}.png",
                title=title,
                angle_deg=float(angle),
                target=center,
                radius=orbit_radius,
                eye_z=eye_z,
                fov=fov,
            )
        )
    contact_sheet = make_contact_sheet(frame_paths, out_dir / "contact_sheet.jpg")
    report = {
        "obj": str(obj_path),
        "mtl": str(mtl_path),
        "frame_count": len(frame_paths),
        "frames": [str(path) for path in frame_paths],
        "contact_sheet": str(contact_sheet),
        "notes": "Orbit renders are used as fast QA: they reveal bad silhouette, floating geometry, missing backs/sides, and wrong component placement.",
    }
    (out_dir / "orbit_manifest.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Render an orbit contact sheet for a semantic OBJ/MTL model.")
    parser.add_argument("obj", type=Path)
    parser.add_argument("--mtl", type=Path)
    parser.add_argument("--out", type=Path, default=Path("outputs/orbit"))
    parser.add_argument("--title", default="Review orbit")
    args = parser.parse_args()
    mtl = args.mtl or args.obj.with_suffix(".mtl")
    print(json.dumps(render_orbit_set(args.obj, mtl, args.out, title=args.title), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
