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
      "component": "vertical_support_unit",
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
  "strategy": "unit_first_reconstruction",
  "image_count": 12,
  "analyzable_image_count": 12,
  "image_analysis": {},
  "components": [
    {
      "component": "vertical_support_unit",
      "label": "Single vertical support / column unit",
      "role": "repeatable_unit",
      "confidence": 0.72,
      "status": "ready_for_unit_draft",
      "evidence": ["12 analyzable image(s)", "facade rhythm score 0.68"],
      "matched_images": [],
      "needs": ["straight-on view with repeated vertical rhythm", "oblique view showing support depth"],
      "output_folder": "outputs/units/vertical_support_unit"
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
  "name": "vertical_support_unit",
  "purpose": "one repeatable support, column, post, or pilaster primitive",
  "parts": [
    {
      "name": "support_shaft_single_repeat",
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
