# Data Contracts

These are the stable interfaces the pipeline is moving toward.

## Project Spec

```json
{
  "model_id": "example_building",
  "scale_notes": "Known measured width or other scale source.",
  "placements": [
    {
      "id": "front_repeat_01",
      "component": "visual_unit_003_opening_or_shadow_region",
      "translate": [-3.6, 0.0, 0.0],
      "scale": [1.0, 1.0, 1.0],
      "rotate_z_deg": 0.0
    }
  ]
}
```

## GUI Project Workspace

```text
projects/<slug>/
  project.json
  images/
  analysis/
  reference_plan.json
  components/
  outputs/
```

`project.json` stores:

```json
{
  "slug": "my-building",
  "name": "My Building",
  "created_at": "2026-05-03T12:00:00",
  "images": [
    {
      "name": "front-view.jpg",
      "path": "images/front-view.jpg",
      "url": "/api/projects/my-building/files/images/front-view.jpg"
    }
  ],
  "outputs": {},
  "events": []
}
```

`analysis/image_analysis.json` stores lightweight measurements from the uploaded images:

```json
{
  "summary": {
    "image_count": 12,
    "analyzable_count": 12,
    "average_aspect_ratio": 1.62,
    "average_quality": 0.73,
    "average_facade_rhythm": 0.68,
    "average_roofline": 0.52,
    "view_diversity_score": 0.7
  },
  "images": []
}
```

`analysis/image_contact_sheet.jpg` is a local visual proof that the current project is using the user's uploaded images.

## Component Plan

```json
{
  "strategy": "pixel_evidence_unit_discovery",
  "image_count": 12,
  "analyzable_image_count": 12,
  "image_analysis": {},
  "components": [
    {
      "component": "visual_unit_003_opening_or_shadow_region",
      "label": "Visual unit: opening or shadow region #003",
      "role": "visual_proposal",
      "kind": "opening_or_shadow_region",
      "confidence": 0.72,
      "status": "ready_for_unit_draft",
      "evidence": ["source image front.jpg", "bbox 120,220,240,520", "visual kind opening or shadow region"],
      "matched_images": [],
      "crop_path": "components/crops/visual_unit_003_opening_or_shadow_region.jpg",
      "bbox_normalized": [0.1, 0.31, 0.2, 0.72],
      "needs": ["one clearer crop of this opening/detail", "confirm this crop is permanent structure"],
      "output_folder": "outputs/units/visual_unit_003_opening_or_shadow_region"
    }
  ],
  "ignored_by_default": [
    {
      "category": "vegetation / people / temporary decor",
      "reason": "Usually context, not reconstruction targets."
    }
  ]
}
```

## Component

```json
{
  "name": "visual_unit_003_opening_or_shadow_region",
  "purpose": "anonymous crop-derived 2.5D draft pending semantic review",
  "parts": [
    {
      "name": "crop_frame_left",
      "material": "limestone",
      "geometry": "mesh"
    }
  ]
}
```

## Orbit Manifest

```json
{
  "obj": "outputs/demo/classical_facade_demo.obj",
  "mtl": "outputs/demo/classical_facade_demo.mtl",
  "frame_count": 18,
  "frames": ["outputs/demo/orbits/orbit_001.png"],
  "contact_sheet": "outputs/demo/orbits/contact_sheet.jpg",
  "notes": "Orbit renders are used as fast QA."
}
```

## Connectivity Report

```json
{
  "grounded": true,
  "floating_count": 0,
  "grounded_count": 120,
  "total_count": 120,
  "floating_names": []
}
```

## Future Review Annotation

```json
{
  "selected_groups": [
    {
      "groupName": "upper_roof__green_white_awning",
      "material": "accent",
      "hitPoint": [0.1, -2.4, 4.2],
      "file": "model.obj"
    }
  ],
  "note": "Awning needs front support posts at every stripe break.",
  "camera": {
    "position": [3.0, -7.0, 5.2],
    "target": [0.0, 0.0, 3.0]
  },
  "status": "queued"
}
```
