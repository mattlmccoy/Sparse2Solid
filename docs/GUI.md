# Sparse2Solid Studio GUI

Sparse2Solid Studio is the local browser interface for people who should not need to run individual commands by hand.

## Launch

```bash
python scripts/run_gui.py
```

Default URL:

```text
http://127.0.0.1:8765
```

Do not open a browser automatically:

```bash
python scripts/run_gui.py --no-open
```

Use a custom project workspace:

```bash
python scripts/run_gui.py --workspace ~/Sparse2SolidProjects
```

## What The GUI Does

- Creates named local projects.
- Uploads sparse reference images into `projects/<slug>/images/`.
- Lets the user crop and label regions as useful structure or distractions to ignore.
- Writes `analysis/image_analysis.json` and `analysis/image_contact_sheet.jpg` from the current image pixels.
- Generates `reference_plan.json` from the current image set.
- Identifies likely reconstruction units in `components/component_plan.json` using image evidence, not fixed demo labels.
- Builds ready unit drafts into `outputs/units/<component>/`.
- Renders per-unit orbit contact sheets for visual QA.
- Assembles current unit drafts into `outputs/assembly/`.
- Shows model/report/orbit links and lightweight OBJ previews in the browser.

## Why This Matters

The pipeline has multiple steps: reference planning, component generation, assembly, orbit review, and validation. A new user should experience those as guided actions, not as a memorized command sequence.

The GUI is intentionally local-first. It does not upload images to a server. That makes it usable for private venues, homes, historic interiors, and other sensitive photo sets.

## Teaching The Detector

The "Teach The Detector" step gives the automatic crop proposal system a small amount of project-specific supervision. Draw boxes around representative structure, then choose a label such as `opening`, `vertical`, `band`, or `roofline`. Draw boxes around distractions such as trees, flowers, sky, people, signage, glare, or foreground clutter and choose `ignore`.

Those labels are saved to:

```text
projects/<slug>/components/training_examples.jsonl
projects/<slug>/components/training_crops/
```

Positive labels are promoted into manual unit candidates on the next discovery pass. Ignore labels suppress overlapping automatic candidates. This makes the workflow less brittle on buildings with gardens, crowds, water reflections, or decorative clutter.

## Current Limitation

The Studio now analyzes uploaded pixels and creates image-conditioned draft geometry, but it is still a scaffold rather than a full semantic vision model. It does not yet perform reliable object segmentation, camera calibration, or metric reconstruction from arbitrary images. The current role is to produce honest unit candidates, expose the evidence, and avoid reusing stale/template outputs when images change.

```text
projects/<slug>/
  images/
  analysis/
  reference_plan.json
  components/
    training_examples.jsonl
    training_crops/
    component_plan.json
  outputs/
    units/
    assembly/
  project.json
```
