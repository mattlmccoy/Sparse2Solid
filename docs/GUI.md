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
- Generates `reference_plan.json` from the current image set.
- Identifies likely reconstruction units in `components/component_plan.json`.
- Builds ready unit drafts into `outputs/units/<component>/`.
- Renders per-unit orbit contact sheets for visual QA.
- Assembles current unit drafts into `outputs/assembly/`.
- Shows model/report/orbit links in the browser.

## Why This Matters

The pipeline has multiple steps: reference planning, component generation, assembly, orbit review, and validation. A new user should experience those as guided actions, not as a memorized command sequence.

The GUI is intentionally local-first. It does not upload images to a server. That makes it usable for private venues, homes, historic interiors, and other sensitive photo sets.

## Current Limitation

The first GUI release uses heuristic component discovery and the included architectural component generators. The next major step is to connect uploaded photos to richer image-conditioned component proposal/generation flows. The GUI API and project structure are designed for that:

```text
projects/<slug>/
  images/
  reference_plan.json
  components/component_plan.json
  outputs/
    units/
    assembly/
  project.json
```
