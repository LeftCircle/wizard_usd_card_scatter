#!/usr/bin/env python3
"""Scatter textured card instances inside closed USD meshes marked ScatterGeo."""
import argparse
import math
from pathlib import Path
import sys

import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade, Vt
import vtk
from vtk.util.numpy_support import numpy_to_vtk


def polydata_for_mesh(mesh, time):
    points = mesh.GetPointsAttr().Get(time)
    counts = mesh.GetFaceVertexCountsAttr().Get(time)
    indices = mesh.GetFaceVertexIndicesAttr().Get(time)
    holes = set(mesh.GetHoleIndicesAttr().Get(time) or [])
    if points is None or counts is None or indices is None or not points:
        raise ValueError(f"{mesh.GetPath()}: mesh has no topology or points at {time}")
    if sum(counts) != len(indices) or any(c < 3 for c in counts):
        raise ValueError(f"{mesh.GetPath()}: invalid polygon topology")
    if any(i < 0 or i >= len(points) for i in indices):
        raise ValueError(f"{mesh.GetPath()}: face index outside points array")
    poly = vtk.vtkPolyData()
    vtk_points = vtk.vtkPoints()
    vtk_points.SetData(numpy_to_vtk(np.asarray(points, dtype=np.float64), deep=True))
    poly.SetPoints(vtk_points)
    cells = vtk.vtkCellArray()
    offset = 0
    for face, count in enumerate(counts):
        if face not in holes:
            cells.InsertNextCell(count)
            for i in indices[offset:offset + count]:
                cells.InsertCellPoint(int(i))
        offset += count
    poly.SetPolys(cells)
    triangles = vtk.vtkTriangleFilter()
    triangles.SetInputData(poly)
    triangles.Update()
    poly = triangles.GetOutput()
    edges = vtk.vtkFeatureEdges()
    edges.SetInputData(poly)
    edges.BoundaryEdgesOn()
    edges.NonManifoldEdgesOn()
    edges.FeatureEdgesOff()
    edges.ManifoldEdgesOff()
    edges.Update()
    if not poly.GetNumberOfCells() or edges.GetOutput().GetNumberOfCells():
        raise ValueError(f"{mesh.GetPath()}: scatter mesh must be closed and manifold (including hole faces)")
    return poly


def sample_mesh(mesh, count, rng, time, max_attempts):
    poly = polydata_for_mesh(mesh, time)
    bounds = np.array(poly.GetBounds(), dtype=float).reshape(3, 2)
    if not np.isfinite(bounds).all() or np.any(bounds[:, 1] <= bounds[:, 0]):
        raise ValueError(f"{mesh.GetPath()}: mesh has empty or invalid volume bounds")
    selector = vtk.vtkSelectEnclosedPoints()
    selector.SetSurfaceData(poly)
    selector.CheckSurfaceOff()  # topology was checked above
    selected = []
    found = 0
    attempted = 0
    while found < count and attempted < max_attempts:
        n = min(max(1024, (count - found) * 4), 100000, max_attempts - attempted)
        candidates = rng.uniform(bounds[:, 0], bounds[:, 1], size=(n, 3))
        data = vtk.vtkPolyData()
        pts = vtk.vtkPoints()
        pts.SetData(numpy_to_vtk(candidates, deep=True))
        data.SetPoints(pts)
        selector.SetInputData(data)
        selector.Update()
        inside = np.fromiter((selector.IsInside(i) for i in range(n)), dtype=bool, count=n)
        chosen = candidates[inside][:count - found]
        selected.extend(chosen.tolist())
        found += len(chosen)
        attempted += n
    if found != count:
        raise ValueError(f"{mesh.GetPath()}: found only {found}/{count} points after {attempted} attempts; increase --max-attempts")
    return selected


def shade_factors(points, vertices, center_strength, height_strength, up_axis):
    """Compute an independent shade ramp from each mesh's world-space bounds."""
    points = np.asarray(points, dtype=float)
    vertices = np.asarray(vertices, dtype=float)
    low, high = vertices.min(axis=0), vertices.max(axis=0)
    if high[up_axis] <= low[up_axis]:
        raise ValueError('transformed mesh has zero height along the stage up axis')
    half = (high - low) / 2
    # Normalize each axis so elongated meshes still have a meaningful center.
    safe_half = np.where(half > 0, half, 1.0)
    distance = np.clip(np.linalg.norm((points - (low + high) / 2) / safe_half, axis=1), 0, 1)
    height = np.clip((points[:, up_axis] - low[up_axis]) / (high[up_axis] - low[up_axis]), 0, 1)
    return (1 - center_strength * (1 - distance)) * (1 - height_strength * (1 - height))


def camera_position(camera_file, camera_prim, time, geometry_stage):
    path = Path(camera_file).resolve()
    if not path.is_file():
        raise ValueError(f'camera file does not exist: {path}')
    stage = Usd.Stage.Open(str(path))
    if not stage:
        raise ValueError(f'cannot open camera stage: {path}')
    if (UsdGeom.GetStageUpAxis(stage) != UsdGeom.GetStageUpAxis(geometry_stage) or
            not math.isclose(UsdGeom.GetStageMetersPerUnit(stage), UsdGeom.GetStageMetersPerUnit(geometry_stage), rel_tol=1e-9)):
        raise ValueError('camera and geometry stages must use the same up axis and metersPerUnit')
    if camera_prim:
        if (not Sdf.Path.IsValidPathString(camera_prim) or
                not Sdf.Path(camera_prim).IsAbsolutePath() or not Sdf.Path(camera_prim).IsPrimPath()):
            raise ValueError('--camera-prim must be an absolute USD prim path')
        prim = stage.GetPrimAtPath(camera_prim)
        if not prim or not prim.IsA(UsdGeom.Camera):
            raise ValueError(f'camera prim not found: {camera_prim}')
    else:
        cameras = [p for p in stage.Traverse() if p.IsA(UsdGeom.Camera)]
        if len(cameras) != 1:
            raise ValueError(f'camera stage contains {len(cameras)} cameras; specify --camera-prim when it has more than one')
        prim = cameras[0]
    transform = UsdGeom.XformCache(time).GetLocalToWorldTransform(prim)
    position = transform.Transform(Gf.Vec3d(0, 0, 0))
    if not all(math.isfinite(x) for x in position):
        raise ValueError('camera position must be finite')
    print(f'Camera {prim.GetPath()} at {position}')
    return position


def camera_orientations(positions, camera, rolls):
    """Point the local +Z normal at the camera and roll around that normal."""
    rotations = []
    for point, roll in zip(positions, rolls):
        direction = Gf.Vec3d(camera) - Gf.Vec3d(point)
        if direction.GetLength() < 1e-10:
            raise ValueError('camera coincides with a scatter point; move the camera')
        direction.Normalize()
        face = Gf.Rotation(Gf.Vec3d(0, 0, 1), direction).GetQuat()
        spin = Gf.Rotation(Gf.Vec3d(0, 0, 1), float(roll) * 360).GetQuat()
        result = face * spin
        rotations.append(Gf.Quath(float(result.GetReal()), Gf.Vec3h(*result.GetImaginary())))
    return rotations


def brush_info(stage, path):
    """Locate a preview surface and its shared color/alpha texture."""
    material = UsdShade.Material.Get(stage, path)
    if not material:
        raise ValueError(f'brush material not found: {path}')
    surface_output = material.GetSurfaceOutput()
    source = surface_output.GetConnectedSource() if surface_output else None
    surface = UsdShade.Shader(source[0].GetPrim()) if source else None
    if not surface or surface.GetIdAttr().Get() != 'UsdPreviewSurface':
        raise ValueError(f'{path}: expected a connected UsdPreviewSurface')
    color = surface.GetInput('diffuseColor')
    alpha = surface.GetInput('opacity')
    color_src = color.GetConnectedSource() if color else None
    alpha_src = alpha.GetConnectedSource() if alpha else None
    if not color_src or not alpha_src or color_src[0].GetPrim() != alpha_src[0].GetPrim() or color_src[1] != 'rgb' or alpha_src[1] != 'a':
        raise ValueError(f'{path}: diffuseColor and opacity must use the rgb and a outputs of the same UsdUVTexture')
    texture = UsdShade.Shader(color_src[0].GetPrim())
    if not texture or texture.GetIdAttr().Get() != 'UsdUVTexture':
        raise ValueError(f'{path}: expected a UsdUVTexture')
    if not texture.GetPath().HasPrefix(path) or not surface.GetPath().HasPrefix(path):
        raise ValueError(f'{path}: texture and surface shaders must be children of the material')
    values = []
    for name, fallback in [('scale', Gf.Vec4f(1)), ('bias', Gf.Vec4f(0))]:
        inp = texture.GetInput(name)
        if inp and inp.HasConnectedSource():
            raise ValueError(f'{path}: texture inputs:{name} must not be connected')
        value = inp.Get() if inp else None
        values.append(value if value is not None else fallback)
    for name in ('opacityThreshold', 'opacityMode'):
        inp = surface.GetInput(name)
        if inp and inp.HasConnectedSource():
            raise ValueError(f'{path}: surface inputs:{name} must not be connected')
    return (texture.GetPath().MakeRelativePath(path), surface.GetPath().MakeRelativePath(path), *values)


def scatter(geo_path, render_path, count, seed, size, min_scale, max_scale,
            material_path, time, max_attempts, center_darken=0.0, height_darken=0.0,
            materials_path=None, shade_levels=32, camera_file=None, camera_prim=None, opacity=1.0):
    geo_path, render_path = Path(geo_path).resolve(), Path(render_path).resolve()
    if geo_path == render_path:
        raise ValueError("geometry input and render output must be different files")
    if count < 1 or size <= 0 or min_scale <= 0 or max_scale < min_scale or max_attempts < count:
        raise ValueError("count, size, and scales must be positive; max scale >= min scale and max attempts >= count")
    if not all(math.isfinite(v) for v in (size, min_scale, max_scale, center_darken, height_darken, opacity)):
        raise ValueError("size, scales, opacity and shading strengths must be finite")
    if not 0 <= center_darken <= 1 or not 0 <= height_darken <= 1 or not 2 <= shade_levels <= 256:
        raise ValueError("shading strengths must be between 0 and 1; shade levels must be between 2 and 256")
    if not 0 <= opacity <= 1:
        raise ValueError('opacity must be between 0 and 1')
    stage = Usd.Stage.Open(str(geo_path))
    if not stage:
        raise ValueError(f"cannot open geometry stage: {geo_path}")
    if camera_prim and not camera_file:
        raise ValueError('--camera-prim requires --camera')
    camera = camera_position(camera_file, camera_prim, time, stage) if camera_file else None
    meshes = [UsdGeom.Mesh(p) for p in stage.Traverse() if p.IsA(UsdGeom.Mesh)
              and p.GetAttribute('ScatterGeo') and p.GetAttribute('ScatterGeo').Get(time) is True]
    if not meshes:
        raise ValueError("no Mesh prims with bool ScatterGeo = true in the geometry stage")
    rng = np.random.default_rng(seed)
    cache = UsdGeom.XformCache(time)
    positions = []
    shade_values = []
    shading_enabled = center_darken > 0 or height_darken > 0
    up_axis = 2 if UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.z else 1
    materials_path = Path(materials_path or render_path.parent / 'materials.usda').resolve()
    selected = [material_path] if isinstance(material_path, str) else material_path
    for p in selected or []:
        if not Sdf.Path.IsValidPathString(p) or not Sdf.Path(p).IsAbsolutePath() or not Sdf.Path(p).IsPrimPath():
            raise ValueError('material paths must be absolute USD prim paths')
    material_stage = Usd.Stage.Open(str(materials_path)) if materials_path.is_file() else None
    if selected is None:
        if not material_stage:
            raise ValueError(f'cannot open material stage: {materials_path}')
        selected = [str(p.GetPath()) for p in material_stage.Traverse() if p.IsA(UsdShade.Material)]
    selected = sorted(set(selected))
    if not selected:
        raise ValueError(f'no brush materials found in {materials_path}')
    needs_variants = shading_enabled or opacity < 1 or len(selected) > 1
    if needs_variants:
        if not material_stage:
            raise ValueError(f'cannot open material stage: {materials_path}')
        brush_infos = [brush_info(material_stage, Sdf.Path(p)) for p in selected]
    else:
        brush_infos = []
    for mesh in meshes:
        local = sample_mesh(mesh, count, rng, time, max_attempts)
        transform = cache.GetLocalToWorldTransform(mesh.GetPrim())
        world = [transform.Transform(Gf.Vec3d(*p)) for p in local]
        positions.extend(world)
        if shading_enabled:
            vertices = [transform.Transform(Gf.Vec3d(*p)) for p in mesh.GetPointsAttr().Get(time)]
            shade_values.extend(shade_factors(world, vertices, center_darken, height_darken, up_axis))
        print(f"{mesh.GetPath()}: {len(local)} points")
    # Draw the same random numbers in both modes so the seed keeps scales stable.
    u = rng.random((len(positions), 3))
    if camera is not None:
        orientations = camera_orientations(positions, camera, u[:, 0])
    else:
        qx = np.sqrt(1-u[:, 0]) * np.sin(2*math.pi*u[:, 1])
        qy = np.sqrt(1-u[:, 0]) * np.cos(2*math.pi*u[:, 1])
        qz = np.sqrt(u[:, 0]) * np.sin(2*math.pi*u[:, 2])
        qw = np.sqrt(u[:, 0]) * np.cos(2*math.pi*u[:, 2])
        orientations = [Gf.Quath(float(w), Gf.Vec3h(float(x), float(y), float(z))) for x,y,z,w in zip(qx,qy,qz,qw)]
    scales = rng.uniform(min_scale, max_scale, len(positions))
    brush_rng = np.random.default_rng(np.random.SeedSequence([seed, 0x42525553]))
    brush_indices = brush_rng.integers(0, len(selected), size=len(positions))
    for i, path in enumerate(selected):
        print(f'{path}: {int(np.count_nonzero(brush_indices == i))} cards')

    # Work in memory until everything is validated, then atomically replace the output.
    if render_path.exists():
        source = Sdf.Layer.FindOrOpen(str(render_path))
        if not source:
            raise ValueError(f"cannot open render layer {render_path}")
        layer = Sdf.Layer.CreateAnonymous('render.usda')
        layer.TransferContent(source)
    else:
        layer = Sdf.Layer.CreateAnonymous('render.usda')
        layer.pseudoRoot.SetInfo('metersPerUnit', UsdGeom.GetStageMetersPerUnit(stage))
        layer.pseudoRoot.SetInfo('upAxis', UsdGeom.GetStageUpAxis(stage))
    import os
    geo_relative = os.path.relpath(geo_path, render_path.parent).replace(os.sep, '/')
    existing = list(layer.subLayerPaths)
    if not any((render_path.parent / p).resolve() == geo_path for p in existing if not p.startswith('anon:')):
        layer.subLayerPaths = existing + [geo_relative]
    # Relative sublayers in an anonymous layer do not resolve; add a temporary absolute sublayer
    # during authoring, then restore the intended paths before export.
    authored_sublayers = list(layer.subLayerPaths)
    layer.subLayerPaths = [str((render_path.parent / p).resolve()) if not p.startswith('anon:') else p for p in authored_sublayers]
    output = Usd.Stage.Open(layer)
    output.SetEditTarget(layer)
    output.RemovePrim('/ScatterCards')
    inst = UsdGeom.PointInstancer.Define(output, '/ScatterCards')
    inst.CreatePurposeAttr(UsdGeom.Tokens.render)
    half = size / 2
    material_relative = os.path.relpath(materials_path, render_path.parent).replace(os.sep, '/')
    if shading_enabled:
        bins = np.rint(np.clip(shade_values, 0, 1) * (shade_levels - 1)).astype(int)
    else:
        bins = np.full(len(positions), shade_levels - 1, dtype=int)
    used_pairs = sorted(set(zip(brush_indices.tolist(), bins.tolist())))
    prototype_paths = []
    variant_paths = []
    for brush_index, shade_bin in used_pairs:
        name = f'Card_{brush_index:03d}_{shade_bin:03d}' if needs_variants else 'Card'
        proto = UsdGeom.Mesh.Define(output, '/ScatterCards/Prototypes/' + name)
        proto.CreatePointsAttr([(-half,-half,0), (half,-half,0), (half,half,0), (-half,half,0)])
        proto.CreateFaceVertexCountsAttr([4])
        proto.CreateFaceVertexIndicesAttr([0,1,2,3])
        proto.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
        proto.CreateDoubleSidedAttr(True)
        proto.CreateExtentAttr([(-half,-half,0), (half,half,0)])
        uv = UsdGeom.PrimvarsAPI(proto).CreatePrimvar('st', Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex)
        uv.Set([(0,0), (1,0), (1,1), (0,1)])
        source_path = Sdf.Path(selected[brush_index])
        target = source_path
        if needs_variants:
            target = Sdf.Path(f'/ScatterCards/Materials/Brush_{brush_index:03d}_Shade_{shade_bin:03d}')
            variant = UsdShade.Material.Define(output, target)
            variant.GetPrim().GetReferences().AddReference(str(materials_path), source_path)
            texture_path, surface_path, texture_scale, texture_bias = brush_infos[brush_index]
            shader = UsdShade.Shader.Get(output, target.AppendPath(texture_path))
            factor = shade_bin / (shade_levels - 1)
            shader.CreateInput('scale', Sdf.ValueTypeNames.Float4).Set(
                Gf.Vec4f(*(float(texture_scale[i]) * factor for i in range(3)), float(texture_scale[3]) * opacity))
            shader.CreateInput('bias', Sdf.ValueTypeNames.Float4).Set(
                Gf.Vec4f(*(float(texture_bias[i]) * factor for i in range(3)), float(texture_bias[3]) * opacity))
            if opacity < 1:
                surface = UsdShade.Shader.Get(output, target.AppendPath(surface_path))
                surface.CreateInput('opacityThreshold', Sdf.ValueTypeNames.Float).Set(0)
                surface.CreateInput('opacityMode', Sdf.ValueTypeNames.Token).Set('presence')
            variant_paths.append((target, source_path))
        UsdShade.MaterialBindingAPI.Apply(proto.GetPrim())
        proto.GetPrim().CreateRelationship('material:binding').SetTargets([target])
        prototype_paths.append(proto.GetPath())
    inst.CreatePrototypesRel().SetTargets(prototype_paths)
    inst.CreatePositionsAttr(Vt.Vec3fArray([Gf.Vec3f(p) for p in positions]))
    indices = {pair: i for i, pair in enumerate(used_pairs)}
    inst.CreateProtoIndicesAttr([indices[(int(brush), int(shade))] for brush, shade in zip(brush_indices, bins)])
    inst.CreateOrientationsAttr(Vt.QuathArray(orientations))
    inst.CreateScalesAttr(Vt.Vec3fArray([Gf.Vec3f(float(s)) for s in scales]))
    # Conservative bound enclosing each rotated card at its largest radius.
    radius = size * math.sqrt(2) / 2 * max_scale
    arr = np.asarray(positions)
    lo, hi = arr.min(axis=0) - radius, arr.max(axis=0) + radius
    inst.CreateExtentAttr([Gf.Vec3f(*lo), Gf.Vec3f(*hi)])
    if needs_variants:
        del shader, variant
        if opacity < 1:
            del surface
    del uv, proto, inst, output
    layer.subLayerPaths = authored_sublayers
    for path, source_path in variant_paths:
        layer.GetPrimAtPath(path).referenceList.explicitItems = [Sdf.Reference(material_relative, source_path)]
    render_path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(dir=render_path.parent, suffix='.usda', delete=False) as temp:
            temp_path = temp.name
        if not layer.Export(temp_path):
            raise ValueError(f"could not export {render_path}")
        os.replace(temp_path, render_path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
    print(f"Wrote {len(positions)} card instances to {render_path}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('geo', nargs='?', default='geo.usda', help='input USD geometry layer')
    p.add_argument('render', nargs='?', default='render.usda', help='render layer to update/create')
    p.add_argument('--count', type=int, default=500, help='points per marked mesh (default: 500)')
    p.add_argument('--seed', type=int, default=42)
    p.add_argument('--size', type=float, default=100, help='card width and height in stage units (default: 100)')
    p.add_argument('--min-scale', type=float, default=0.75, help='minimum random size multiplier (default: 0.75)')
    p.add_argument('--max-scale', type=float, default=1.25, help='maximum random size multiplier (default: 1.25)')
    p.add_argument('--center-darken', type=float, default=0, help='center darkness 0..1 per mesh (default: 0)')
    p.add_argument('--height-darken', type=float, default=0, help='bottom darkness 0..1 per mesh, using stage up axis (default: 0)')
    p.add_argument('--shade-levels', type=int, default=32, help='number of discrete shading levels, 2..256 (default: 32)')
    p.add_argument('--materials', default=None, help='brush material layer (default: materials.usda next to render)')
    p.add_argument('--material', action='append', default=None, help='brush material prim path; repeat to select multiple (default: all materials)')
    p.add_argument('--opacity', type=float, default=1.0, help='base opacity multiplier 0..1 (default: 1)')
    p.add_argument('--camera', default=None, help='USD camera file; orient card normals toward its camera position')
    p.add_argument('--camera-prim', default=None, help='absolute camera prim path (required if the camera file has multiple cameras)')
    p.add_argument('--time', type=float, default=None, help='sample geometry and transforms at this USD time code; default time otherwise')
    p.add_argument('--max-attempts', type=int, default=1000000, help='maximum candidate points per mesh')
    a = p.parse_args()
    try:
        scatter(a.geo, a.render, a.count, a.seed, a.size, a.min_scale, a.max_scale,
                a.material, Usd.TimeCode.Default() if a.time is None else Usd.TimeCode(a.time), a.max_attempts,
                a.center_darken, a.height_darken, a.materials, a.shade_levels, a.camera, a.camera_prim, a.opacity)
    except (ValueError, OSError, RuntimeError) as exc:
        p.exit(1, f'error: {exc}\n')

if __name__ == '__main__':
    main()
