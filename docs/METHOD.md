# Method: Sparse Images to Clean Architectural 3D

Sparse2Solid is a hybrid reconstruction workflow. It borrows the discipline of photogrammetry, the interpretability of CAD, and the iteration loop of design review.

## The Core Insight

For buildings, a clean model usually does not require solving every pixel as geometry. It requires identifying the correct architectural system:

- the footprint,
- the repeated units,
- the hierarchy of masses,
- the surface details that actually affect silhouette,
- and the placement rules tying all of those together.

Photogrammetry asks:

> Where is this point in 3D?

Sparse2Solid asks:

> What component is this, where does it repeat, and what evidence constrains it?

That distinction is why the approach can work from far fewer photos.

## Step-by-Step

### 1. Reference Triage

Photos are sorted by what they prove:

- proportion,
- scale,
- depth,
- roof massing,
- repeated detail,
- material/color,
- hidden side/back conditions.

An image that proves a missing side depth is more useful than ten redundant front photos.

### 2. Unit Discovery

The building is decomposed into units:

- facade bay,
- lamp,
- column,
- stair/terrace,
- parapet segment,
- roof cap,
- awning,
- side pavilion,
- window grille.

These units become testable geometry files. Each unit can be rendered, inspected, revised, and reused.

### 3. Component Modeling

The current public implementation uses procedural geometry because it is clean, inspectable, and easy to revise. Future versions may plug in AI-assisted geometry suggestions, but the public contract remains the same:

```json
{
  "component": "facade_bay",
  "groups": ["wall_panel", "applied_arch_ring", "column", "glass", "entablature"],
  "evidence": ["front_hero", "detail_closeup", "left_oblique"],
  "confidence": "reviewed"
}
```

### 4. Orbit Review

Orbit rendering is the debugging trick.

A model can look correct from the one source image and fail immediately from an oblique view. Orbit maps expose:

- inverted roof slopes,
- missing second levels,
- unsupported awnings,
- floating lamps,
- facade details that only exist on part of the front,
- wrong end-cap massing,
- and bad component scale.

The orbit does not create geometry. It makes mistakes visible.

### 5. Assembly

Once units pass review, they are placed into a full model. Placement is constrained by:

- measured width/depth,
- facade repeat count,
- unit spacing,
- roof/ground contact,
- side and rear evidence,
- and known architectural symmetries.

The final OBJ remains semantically grouped. That is essential. A single fused anonymous mesh is hard to fix; a semantic assembly is editable.

### 6. Connectivity Validation

The pipeline checks whether parts have a route back to ground. This matters because many model formats allow visually convincing but physically impossible stacks of disconnected pieces.

Connectivity validation finds pieces that are not supported by any preceding geometry, even if they appear close.

## Why Not Pure AI?

AI-generated 3D can be useful for suggestions, but architecture needs accountability. If a roofline, window rhythm, or facade ornament is wrong, the model should tell you which evidence created it and which component owns it.

Sparse2Solid can use AI as an assistant, but not as untracked ground truth.

## Why Not Pure Photogrammetry?

Photogrammetry is excellent when:

- you have many overlapping images,
- the object has trackable texture,
- the geometry is visible from all sides,
- and scan noise is acceptable.

Architectural venue photos often violate those assumptions:

- glossy windows,
- white facades with low texture,
- trees and people occluding details,
- inaccessible backs/sides,
- rooflines visible in only a few photos,
- event/gallery images taken from inconsistent cameras.

Component-guided reconstruction handles these cases by encoding architectural regularity instead of relying only on feature matching.
