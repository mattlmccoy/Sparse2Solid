#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from sparse2solid.gui import (
    build_assembly_preview,
    build_unit_outputs,
    event,
    project_manifest,
    relativize_report,
    write_component_plan,
    write_manifest,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Regenerate one Studio project from its current uploaded images.")
    parser.add_argument("project_dir", type=Path)
    args = parser.parse_args()

    project_dir = args.project_dir.resolve()
    manifest = project_manifest(project_dir)
    plan = write_component_plan(project_dir, manifest)
    units = build_unit_outputs(project_dir, manifest)
    message = (
        f"Regenerated image-conditioned plan with {plan['analyzable_image_count']} "
        f"analyzable image(s) and {units['built_count']} unit draft(s)."
    )
    event(manifest, message)
    if units["built_count"]:
        assembly = build_assembly_preview(project_dir, manifest)
        manifest.setdefault("outputs", {})["assembly"] = relativize_report(project_dir, assembly)
        event(manifest, "Regenerated image-conditioned assembly preview.")
    write_manifest(project_dir, manifest)
    print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
