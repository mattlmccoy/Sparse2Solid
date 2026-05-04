# Sparse2Solid

Few 2D images to clean, solid architectural 3D reconstruction.

Sparse2Solid is an experimental pipeline for turning a modest set of ordinary 2D photos into a clean, editable, full 3D architectural model. It is not classical photogrammetry. Instead of trying to reconstruct every surface directly from hundreds of overlapping images, it uses sparse photos to infer repeatable semantic components, validates those components with orbit renders, and assembles them into a full building model with named geometry groups.

The result is closer to how a careful human modeler works:

1. Collect a small but diverse reference set.
2. Identify repeatable units: bays, columns, lamps, arches, windows, roof modules, stairs.
3. Build each unit as clean geometry with named parts and materials.
4. Render orbit/contact sheets to reveal errors from many angles.
5. Revise unit geometry before assembling the whole building.
6. Place units into a full-scale model using plan dimensions, facade rhythm, and image evidence.
7. Check that the assembly has support paths and no unintentional floating details.

This approach can produce clean results from around 15-30 useful images when the subject has architectural regularity. Traditional photogrammetry often wants 50-200 images because it needs dense feature overlap. Sparse2Solid needs enough images to understand structure, repetition, scale, and hidden sides.

## What This Is Good For

- Historic buildings and landmarks
- Wedding/event venues
- Facades with repeated bays
- Houses, porches, storefronts, churches, boathouses, courthouses
- Game/blockout models that need clean topology instead of noisy scans
- Fabrication prep where semantic parts matter
- Design review where a user needs to click on a part and say "fix this"

## What This Is Not

- A one-click neural reconstruction system
- A dense scanner for organic objects
- A replacement for photogrammetry when millimeter surface texture is required
- A guarantee of metric accuracy without at least one real scale reference
- A way to infer hidden geometry that never appears in references

## Why Sparse Photos Can Work

The central trick is that buildings are not random. A building usually contains repeated, constrained, human-designed geometry:

- A facade bay repeats six or eight times.
- Lamps are instances of one detailed unit.
- Arches share radii and trim profiles.
- Windows share mullion patterns.
- Roofs obey simple pitch and ridge logic.
- Balustrades, dentils, plinths, steps, and columns form rhythmic systems.

Photogrammetry treats every point as something to solve from image correspondence. Sparse2Solid treats images as evidence for a constrained model. One sharp closeup of a lamp can define a reusable lamp unit. One good oblique view can establish depth. One plan dimension can scale the entire footprint. Then orbit renders expose where the model is wrong.

That means the pipeline is not "2D image magically becomes 3D mesh." It is:

```text
photos -> observations -> component hypotheses -> unit tests -> reviewed components -> assembled model
```

The current code in this repository is the reusable public core of that idea. It excludes project-specific private photos and fabrication-specific LEGO logic.

## Recommended Input Images

Minimum workable set for a regular building: about 12-18 images.

Comfortable set: about 20-35 images.

More images still help, but the goal is diversity, not brute-force quantity.

### Capture Checklist

| View type | Minimum | Ideal | Why it matters |
| --- | ---: | ---: | --- |
| Front hero / main elevation | 1 | 3 | Facade rhythm, proportions, repeat count, major ornament hierarchy |
| Front-left oblique | 1 | 3 | Depth of columns, arches, stairs, lamps, side returns |
| Front-right oblique | 1 | 3 | Opposite-side occlusions and symmetry checks |
| Side elevation | 1 | 2 | Building depth, end pavilions, side windows, roof returns |
| Rear / terrace / back | 1 | 2 | Prevents a front-only model with a guessed back |
| Roofline / parapet | 1 | 3 | Roof pitch, gutters, chimneys, awnings, brackets |
| Repeatable unit closeups | 3 | 8 | Turns visual texture into real component geometry |
| Context / scale | 1 | 2 | Stairs, ground plane, waterline, plan dimensions, human scale |

### Image Quality Rules

- Prefer sharp images over high quantity.
- Avoid cropping off the roofline or ground contact.
- Include at least one image where vertical lines are nearly vertical.
- Include oblique angles so depth can be inferred.
- Get closeups of repeated details: lamps, windows, doors, railings, arches.
- Capture both ends of the building if the ends differ.
- Include one known dimension if possible: floorplan width, door height, stair tread, brick size, railing height.
- Separate "truth photos" from "synthetic or guessed aids."

### When 20 Images Beat 200 Images

Twenty good images can beat two hundred weak images if they cover:

- one front elevation,
- two opposing obliques,
- both ends,
- the roofline,
- the ground/base contact,
- and the important repeating details.

Traditional photogrammetry needs image overlap. Sparse2Solid needs architectural evidence.

## Pipeline

### 1. Plan the reference set

```bash
python -m sparse2solid.reference_planner "my-building" --out reference_plan.json
```

This produces a checklist describing which missing views are likely to hurt the model.

### 2. Build semantic units

A unit is a reusable component such as:

- facade bay,
- lamp,
- column,
- stair run,
- roof cap,
- awning,
- parapet segment,
- side pavilion.

Each unit exports named OBJ groups. Those names matter because review feedback can be precise.

Example group names:

```text
front_bay_02__applied_arch_ring_07
left_lamp__warm_glass_globe
upper_roof__green_white_awning
upper_roof__awning_support_2.6
```

### 3. Render orbit maps

Orbit maps are deterministic review renders from the current OBJ/MTL. They are deliberately cheap and fast. They let you inspect the model from angles you did not explicitly model against.

```bash
python -m sparse2solid.orbit outputs/demo/classical_facade_demo.obj \
  --mtl outputs/demo/classical_facade_demo.mtl \
  --out outputs/demo/orbits \
  --title "Demo review orbit"
```

The contact sheet is often the fastest way to spot:

- floating trim,
- wrong roof pitch,
- missing side depth,
- inverted geometry,
- ungrounded decorative parts,
- bad scale,
- components that look good from front but fail from the side.

### 4. Assemble the full model

The assembly stage places reviewed components into a complete structure. Placement should be based on:

- floorplan or measured footprint,
- repeated bay count,
- door/window rhythm,
- scale reference,
- oblique photos for depth,
- roofline photos for vertical massing.

### 5. Validate support chains

A model can be watertight and still be wrong for physical interpretation. Sparse2Solid includes structural connectivity checks that ask:

> Does every semantic piece have some chain of contact back to the ground or a parent structure?

This catches details that are technically present in the OBJ but are visually/physically floating.

## Quick Start

### Local GUI

Most users should start with the browser studio:

```bash
git clone https://github.com/mattlmccoy/Sparse2Solid.git
cd Sparse2Solid
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python scripts/run_gui.py
```

Then open:

```text
http://127.0.0.1:8765
```

The GUI guides a new user through:

- creating a project,
- uploading sparse 2D reference images,
- writing an image analysis report and contact sheet from those exact uploads,
- generating a missing-view capture plan,
- identifying likely reusable geometry units with evidence-based confidence,
- building per-unit OBJ/MTL drafts and orbit QA sheets,
- assembling the current unit drafts into a full model preview,
- previewing OBJ outputs directly in the browser,
- opening OBJ/MTL/report outputs when needed.

The current GUI is local-first. Uploaded images stay in the local `projects/` folder.

The intended reconstruction order is:

```text
project -> images -> coverage plan -> unit discovery -> unit drafts -> assembly preview
```

This is intentionally different from one-shot reconstruction. The app should identify the important unique/repeated units first, ask for more images when a unit is underconstrained, and only then assemble the full structure.

### Generalization Guardrails

Sparse2Solid should not silently reuse an old template model for a new building. The Studio now follows these rules:

- Uploading new images marks prior analysis, unit plans, and model outputs as stale in the project manifest.
- Unit confidence is computed from the current uploaded images; it is not a fixed demo percentage.
- The image contact sheet is shown beside the project so users can verify which photos are being analyzed.
- Unit drafts and assembly previews are generated into the active project folder under `projects/<slug>/outputs/`.
- Browser OBJ preview is available for unit drafts and assembly outputs without downloading files first.
- If no units are confidently drafted, the Studio refuses to assemble a misleading full model.

On macOS, you can also double-click:

```text
Launch Sparse2Solid Studio.command
```

### Command Line

```bash
git clone https://github.com/mattlmccoy/Sparse2Solid.git
cd Sparse2Solid
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python scripts/build_demo_model.py
```

Expected outputs:

```text
outputs/demo/classical_facade_demo.obj
outputs/demo/classical_facade_demo.mtl
outputs/demo/classical_facade_demo_report.json
outputs/demo/orbits/contact_sheet.jpg
```

Run tests:

```bash
pytest
```

## Current Status

This is an early public extraction of a working private pipeline. The included demo is intentionally small and generic. The method has already been used on a real venue model with:

- sparse real photos,
- unit-level procedural reconstruction,
- component orbit review,
- full assembly placement,
- and support-chain validation.

The public repo intentionally does not include private venue photos or fabrication-specific downstream logic.

## Roadmap

- Browser-based review UI for selecting OBJ groups and attaching feedback
- More guided GUI steps for real image-conditioned component generation
- More component templates: roofs, dormers, columns, railings, stairs, lamps
- Guided annotation schema for turning user notes into unit revisions
- Optional AI-assisted novel-view planning, with strict synthetic/real provenance
- Optional Blender export helpers
- Better physical connectivity and watertightness checks
- Example projects using open/public-domain reference sets

## License

MIT.
