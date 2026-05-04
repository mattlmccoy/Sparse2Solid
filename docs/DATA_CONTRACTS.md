# Data Contracts

These are the stable interfaces the pipeline is moving toward.

## Project Spec

```json
{
  "model_id": "classical_facade_demo",
  "scale_notes": "Known measured width or other scale source.",
  "placements": [
    {
      "id": "front_bay_01",
      "component": "facade_bay",
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
  reference_plan.json
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

## Component

```json
{
  "name": "facade_bay",
  "purpose": "repeatable arched facade bay",
  "parts": [
    {
      "name": "applied_arch_ring_07",
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
