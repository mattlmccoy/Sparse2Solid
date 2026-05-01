from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


VIEW_CHECKLIST = [
    {
        "view": "hero_front",
        "minimum": 1,
        "ideal": 3,
        "purpose": "Establishes facade rhythm, repeat count, main proportions, and ornament hierarchy.",
        "capture": "Straight-on or slight telephoto front view with verticals as parallel as possible.",
    },
    {
        "view": "front_left_oblique",
        "minimum": 1,
        "ideal": 3,
        "purpose": "Reveals depth of columns, arch rings, stairs, lamps, and side return.",
        "capture": "30-45 degree angle from left, include roof and ground contact.",
    },
    {
        "view": "front_right_oblique",
        "minimum": 1,
        "ideal": 3,
        "purpose": "Balances occlusions from the opposite side and checks symmetry assumptions.",
        "capture": "30-45 degree angle from right, include roof and ground contact.",
    },
    {
        "view": "side_elevation",
        "minimum": 1,
        "ideal": 2,
        "purpose": "Locks building depth, end-pavilion massing, roof return, and window placement.",
        "capture": "As orthographic as possible; avoid cropping roof or base.",
    },
    {
        "view": "rear_or_terrace",
        "minimum": 1,
        "ideal": 2,
        "purpose": "Prevents a beautiful front-only model with a guessed back.",
        "capture": "Wide enough to include corners, railings, and roof edge.",
    },
    {
        "view": "roofline",
        "minimum": 1,
        "ideal": 3,
        "purpose": "Captures parapets, awnings, chimneys, gutters, brackets, and pitch.",
        "capture": "Use elevated/long-lens views when possible. Roof detail is often underdetermined from ground photos.",
    },
    {
        "view": "repeatable_unit_closeups",
        "minimum": 3,
        "ideal": 8,
        "purpose": "Turns visual texture into actual component geometry: arches, lamps, doors, columns, rails.",
        "capture": "Crop around one repeatable bay or object; keep one nearby known-size object in frame when possible.",
    },
    {
        "view": "context_scale",
        "minimum": 1,
        "ideal": 2,
        "purpose": "Connects model scale to site features, stairs, ground plane, waterline, or plan dimensions.",
        "capture": "Wide context shot, preferably with measurable elements.",
    },
]


def plan_reference_set(project_name: str, available_images: list[str] | None = None) -> dict[str, Any]:
    available = available_images or []
    total_minimum = sum(item["minimum"] for item in VIEW_CHECKLIST)
    total_ideal = sum(item["ideal"] for item in VIEW_CHECKLIST)
    return {
        "project_name": project_name,
        "recommended_image_count": {
            "minimum_workable": total_minimum,
            "comfortable": total_ideal,
            "notes": "This pipeline can work from fewer images than photogrammetry because images guide semantic components; they are not the only source of geometry.",
        },
        "available_images": available,
        "checklist": VIEW_CHECKLIST,
        "triage_rules": [
            "Prefer sharp images with visible ground contact and uncropped roofline.",
            "One excellent orthographic/front image is more valuable than ten casual duplicates.",
            "Closeups are for component construction; wide views are for placement and scale.",
            "If a region is missing, create a low-confidence synthetic/request card rather than silently guessing.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a sparse-photo capture plan for component-guided reconstruction.")
    parser.add_argument("project_name")
    parser.add_argument("--images", type=Path, help="Optional folder of current reference images.")
    parser.add_argument("--out", type=Path, default=Path("reference_plan.json"))
    args = parser.parse_args()
    images = sorted(str(path) for path in args.images.glob("*") if path.is_file()) if args.images else []
    plan = plan_reference_set(args.project_name, images)
    args.out.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(plan, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
