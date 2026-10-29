# USD card scatter

`card_scatter.py` samples points inside closed meshes tagged with `custom bool ScatterGeo = 1` in `geo.usda` and writes a `UsdGeomPointInstancer` into `render.usda`. It creates a double-sided UV-mapped card prototype and binds it to `/Looks/CardMaterial` from `materials.usda`. `asset.usda` composes the render, proxy, and material layers; open **asset.usda** in a USD viewer and enable the render purpose to see the cards. The source meshes remain proxy-purpose geometry. The supplied tree's scatter meshes are `/Tree1/polySurface6` and `/Tree1/polySurface8`.

## Install and run

Use Python 3 with the OpenUSD Python bindings (`pxr`), NumPy and VTK. For example, in a virtual environment:

```sh
python -m pip install usd-core numpy vtk
python card_scatter.py geo.usda render.usda --count 500 --size 100 --seed 42
```

Run from the repository directory, or pass paths to your own files. Both positional paths default to `geo.usda` and `render.usda`. The count is **per marked mesh**. Card size is in the geometry stage's units; this example uses centimeters. Run `python card_scatter.py --help` for all options, including `--time` to evaluate animated geometry at a chosen time code and `--max-attempts` for thin volumes. The result is a static scatter sampled at that time, not an animation.

## Camera-facing cards

```sh
python card_scatter.py geo.usda render.usda --camera camera.usda --seed 42
```

Each card faces the camera's world-space position and gets a random spin around its own normal. The included `camera.usda` is an example camera; replace it or pass your own USD camera file. If the camera file contains more than one Camera prim, select one with `--camera-prim /path/to/Camera`. The camera and geometry must use the same coordinate space, `upAxis`, and `metersPerUnit`; parent camera transforms are included. Omitting `--camera` retains the original random 3D rotations.

Facing is baked at generation time. `--time` samples the camera and geometry at the same time code, but the cards do not automatically follow an animated camera. Rerun the script for a different camera position or time. The seed keeps scatter positions and scales the same when switching orientation modes.

## Size and fake shading

For example:

```sh
python card_scatter.py geo.usda render.usda --size 100 --min-scale 0.5 --max-scale 1.5 --center-darken 0.7 --height-darken 0.5 --seed 42
```

- `--min-scale` and `--max-scale` are the range of random uniform size multipliers (defaults: 0.75 and 1.25). Set both to 1 for identical sizes. `--seed` makes positions, rotations and scales repeatable.
- `--center-darken` sets darkness at the center of each mesh's world-space bounding box: 0 disables it, and 1 makes the exact center black. It fades out with distance, normalized by the box's half-size along each axis; it is not distance to the mesh surface or a lighting simulation.
- `--height-darken` sets darkness at the bottom of each mesh's world-space bounding box: 0 disables it, and 1 makes the bottom black. It fades to no darkening at the top, along the USD stage's Y or Z up axis. When both effects are enabled, their brightness factors multiply.
- Both shading controls default to 0. The checked-in `render.usda` demonstrates the example above with `--camera camera.usda` added. To regenerate it without shading, omit the two darkening options.

Shading uses discrete material and card prototype copies, each referencing the source material and scaling the texture RGB while leaving its alpha unchanged. `--shade-levels` controls the maximum number of brightness steps (2–256, default 32); more levels reduce visible banding but create more prototypes. With both effects off, the script uses a single prototype and the original material binding.

The output updates the existing render layer, keeps its other opinions and sublayers, and replaces the reserved `/ScatterCards` prim on every run. If the render layer doesn't exist, it is created and sublayers the geometry. Positions include the source meshes' local-to-world transforms. The mesh is treated as a closed polygonal volume using its authored control cage (not a subdivided or Houdini voxelized surface). Invalid, open or non-manifold meshes are rejected; thin or self-intersecting meshes may require cleanup or more attempts. Overlapping meshes are sampled independently.

`materials.usda` contains a portable `UsdPreviewSurface` network using `textures/brush.png` for color and opacity. The included image is a simple placeholder brush stroke; replace it with your own RGBA texture at that path, or edit the material's texture asset path. You can bind an existing material instead using `--material /Your/Material/Path`; that prim must be present in the composed asset. The texture from the Houdini example referred to a local Windows path and was not included with the example.

The material connects the brush texture's alpha channel to opacity and sets `opacityThreshold` to 0.001, so transparent texels are cut out instead of showing the black card rectangle in renderers supporting USD opacity masks. This is a binary cutout: values below the threshold are transparent and values at or above it are opaque. Set the threshold to 0 in `materials.usda` for soft, partially transparent edges if your renderer supports blended opacity. Use an RGBA image with the desired mask in its alpha channel; an RGB-only image is fully opaque. Renderer support and transparency settings vary. Custom materials selected with `--material` need their own opacity setup.

For darkening, the material must be in `materials.usda` next to the render output (or in the layer supplied with `--materials`) and have a `UsdUVTexture` child named `BrushTexture` whose RGB feeds the surface color and whose `inputs:scale` is not connected. The script references that material for each brightness level; it does not edit the source material. A custom material with a different shader layout can be used without darkening, or adapted to this layout. Renderers must support USD material references and `UsdUVTexture.inputs:scale`.

## Test

```sh
python -m unittest discover -s tests -v
```
