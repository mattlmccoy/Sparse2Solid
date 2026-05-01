from __future__ import annotations

import argparse
import json
from pathlib import Path

from .assembly import write_project
from .orbit import render_orbit_set


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SPEC = ROOT / "examples" / "classical_facade_project.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the Sparse2Solid demo model and orbit contact sheet.")
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--out", type=Path, default=Path("outputs/demo"))
    args = parser.parse_args()
    report = write_project(args.spec, args.out)
    orbit_report = render_orbit_set(
        obj_path=Path(report["outputs"]["obj"]),
        mtl_path=Path(report["outputs"]["mtl"]),
        out_dir=args.out / "orbits",
        title="Sparse2Solid demo orbit",
    )
    report["orbit"] = orbit_report
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
