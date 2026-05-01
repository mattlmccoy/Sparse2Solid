from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geometry import MeshPart


@dataclass(frozen=True)
class ConnectivityReport:
    grounded: bool
    floating_count: int
    grounded_count: int
    total_count: int
    floating_names: tuple[str, ...]


def _overlaps(a_min: np.ndarray, a_max: np.ndarray, b_min: np.ndarray, b_max: np.ndarray, tolerance: float) -> bool:
    return bool(np.all(a_max + tolerance >= b_min) and np.all(b_max + tolerance >= a_min))


def structural_connectivity(parts: list[MeshPart], ground_z: float = 0.0, tolerance: float = 0.015) -> ConnectivityReport:
    """Check whether every semantic part has a support path to ground.

    This is stricter than "the OBJ file exists" and closer to what a physical
    model needs. Parts can be stacked or bridged, but every part should connect
    through overlaps/touches to something that eventually reaches ground.
    """

    if not parts:
        return ConnectivityReport(True, 0, 0, 0, ())
    bounds = [part.bounds for part in parts]
    grounded = {idx for idx, (mn, _mx) in enumerate(bounds) if mn[2] <= ground_z + tolerance}
    changed = True
    while changed:
        changed = False
        for idx, (mn, mx) in enumerate(bounds):
            if idx in grounded:
                continue
            for parent_idx in grounded:
                parent_min, parent_max = bounds[parent_idx]
                touches_vertically = mn[2] <= parent_max[2] + tolerance and mx[2] >= parent_min[2] - tolerance
                if touches_vertically and _overlaps(mn[:2], mx[:2], parent_min[:2], parent_max[:2], tolerance):
                    grounded.add(idx)
                    changed = True
                    break
    floating = [parts[idx].name for idx in range(len(parts)) if idx not in grounded]
    return ConnectivityReport(
        grounded=not floating,
        floating_count=len(floating),
        grounded_count=len(grounded),
        total_count=len(parts),
        floating_names=tuple(floating),
    )
