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


def scatter(geo_path, render_path, count, seed, size, min_scale, max_scale,
            material_path, time, max_attempts, center_darken=0.0, height_darken=0.0,
            materials_path=None, shade_levels=32):
    geo_path, render_path = Path(geo_path).resolve(), Path(render_path).resolve()
    if geo_path == render_path:
        raise ValueError("geometry input and render output must be different files")
    if count < 1 or size <= 0 or min_scale <= 0 or max_scale < min_scale or max_attempts < count:
        raise ValueError("count, size, and scales must be positive; max scale >= min scale and max attempts >= count")
    if not all(math.isfinite(v) for v in (size, min_scale, max_scale, center_darken, height_darken)):
        raise ValueError("size, scales and shading strengths must be finite")
    if not 0 <= center_darken <= 1 or not 0 <= height_darken <= 1 or not 2 <= shade_levels <= 256:
        raise ValueError("shading strengths must be between 0 and 1; shade levels must be between 2 and 256")
    stage = Usd.Stage.Open(str(geo_path))
    if not stage:
        raise ValueError(f"cannot open geometry stage: {geo_path}")
    meshes = [UsdGeom.Mesh(p) for p in stage.Traverse() if p.IsA(UsdGeom.Mesh)
              and p.GetAttribute('ScatterGeo') and p.GetAttribute('ScatterGeo').Get(time) is True]
    if not meshes:
        raise ValueError("no Mesh prims with bool ScatterGeo = true in the geometry stage")
    if not Sdf.Path.IsValidPathString(material_path) or not Sdf.Path(material_path).IsAbsolutePath() or not Sdf.Path(material_path).IsPrimPath():
        raise ValueError("material path must be an absolute USD prim path")
    rng = np.random.default_rng(seed)
    cache = UsdGeom.XformCache(time)
    positions = []
    shade_values = []
    shading_enabled = center_darken > 0 or height_darken > 0
    up_axis = 2 if UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.z else 1
    materials_path = Path(materials_path or render_path.parent / 'materials.usda').resolve()
    if shading_enabled:
        material_stage = Usd.Stage.Open(str(materials_path)) if materials_path.is_file() else None
        if not material_stage or not UsdShade.Material.Get(material_stage, material_path):
            raise ValueError(f"cannot find material {material_path} in {materials_path}")
        texture = UsdShade.Shader.Get(material_stage, Sdf.Path(material_path).AppendChild('BrushTexture'))
        if not texture or texture.GetIdAttr().Get() != 'UsdUVTexture':
            raise ValueError('shading requires a UsdUVTexture child named BrushTexture in the material')
        scale_input = texture.GetInput('scale')
        if scale_input and scale_input.HasConnectedSource():
            raise ValueError('BrushTexture.inputs:scale must be an unconnected value for shading')
        texture_scale = scale_input.Get() if scale_input else None
        texture_scale = texture_scale if texture_scale is not None else Gf.Vec4f(1, 1, 1, 1)
    for mesh in meshes:
        local = sample_mesh(mesh, count, rng, time, max_attempts)
        transform = cache.GetLocalToWorldTransform(mesh.GetPrim())
        world = [transform.Transform(Gf.Vec3d(*p)) for p in local]
        positions.extend(world)
        if shading_enabled:
            vertices = [transform.Transform(Gf.Vec3d(*p)) for p in mesh.GetPointsAttr().Get(time)]
            shade_values.extend(shade_factors(world, vertices, center_darken, height_darken, up_axis))
        print(f"{mesh.GetPath()}: {len(local)} points")
    # Random uniform 3D rotations, so cards are not all facing the same direction.
    u = rng.random((len(positions), 3))
    qx = np.sqrt(1-u[:, 0]) * np.sin(2*math.pi*u[:, 1])
    qy = np.sqrt(1-u[:, 0]) * np.cos(2*math.pi*u[:, 1])
    qz = np.sqrt(u[:, 0]) * np.sin(2*math.pi*u[:, 2])
    qw = np.sqrt(u[:, 0]) * np.cos(2*math.pi*u[:, 2])
    orientations = [Gf.Quath(float(w), Gf.Vec3h(float(x), float(y), float(z))) for x,y,z,w in zip(qx,qy,qz,qw)]
    scales = rng.uniform(min_scale, max_scale, len(positions))

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
        used_bins = sorted(set(bins.tolist()))
    else:
        bins = np.zeros(len(positions), dtype=int)
        used_bins = [0]
    prototype_paths = []
    variant_paths = []
    for shade_bin in used_bins:
        name = f'Card_{shade_bin:03d}' if shading_enabled else 'Card'
        proto = UsdGeom.Mesh.Define(output, '/ScatterCards/Prototypes/' + name)
        proto.CreatePointsAttr([(-half,-half,0), (half,-half,0), (half,half,0), (-half,half,0)])
        proto.CreateFaceVertexCountsAttr([4])
        proto.CreateFaceVertexIndicesAttr([0,1,2,3])
        proto.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
        proto.CreateDoubleSidedAttr(True)
        proto.CreateExtentAttr([(-half,-half,0), (half,half,0)])
        uv = UsdGeom.PrimvarsAPI(proto).CreatePrimvar('st', Sdf.ValueTypeNames.TexCoord2fArray, UsdGeom.Tokens.vertex)
        uv.Set([(0,0), (1,0), (1,1), (0,1)])
        target = Sdf.Path(material_path)
        if shading_enabled:
            target = Sdf.Path(f'/ScatterCards/Materials/Shade_{shade_bin:03d}')
            variant = UsdShade.Material.Define(output, target)
            variant.GetPrim().GetReferences().AddReference(str(materials_path), Sdf.Path(material_path))
            shader = UsdShade.Shader.Get(output, target.AppendChild('BrushTexture'))
            factor = shade_bin / (shade_levels - 1)
            shader.CreateInput('scale', Sdf.ValueTypeNames.Float4).Set(
                Gf.Vec4f(*(float(texture_scale[i]) * factor for i in range(3)), float(texture_scale[3])))
            variant_paths.append(target)
        UsdShade.MaterialBindingAPI.Apply(proto.GetPrim())
        proto.GetPrim().CreateRelationship('material:binding').SetTargets([target])
        prototype_paths.append(proto.GetPath())
    inst.CreatePrototypesRel().SetTargets(prototype_paths)
    inst.CreatePositionsAttr(Vt.Vec3fArray([Gf.Vec3f(p) for p in positions]))
    indices = {shade_bin: i for i, shade_bin in enumerate(used_bins)}
    inst.CreateProtoIndicesAttr([indices[int(shade_bin)] for shade_bin in bins])
    inst.CreateOrientationsAttr(Vt.QuathArray(orientations))
    inst.CreateScalesAttr(Vt.Vec3fArray([Gf.Vec3f(float(s)) for s in scales]))
    # Conservative bound enclosing each rotated card at its largest radius.
    radius = size * math.sqrt(2) / 2 * max_scale
    arr = np.asarray(positions)
    lo, hi = arr.min(axis=0) - radius, arr.max(axis=0) + radius
    inst.CreateExtentAttr([Gf.Vec3f(*lo), Gf.Vec3f(*hi)])
    if shading_enabled:
        del shader, variant
    del uv, proto, inst, output
    layer.subLayerPaths = authored_sublayers
    for path in variant_paths:
        layer.GetPrimAtPath(path).referenceList.explicitItems = [Sdf.Reference(material_relative, Sdf.Path(material_path))]
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
    p.add_argument('--materials', default=None, help='material layer path for shading (default: materials.usda next to render)')
    p.add_argument('--material', default='/Looks/CardMaterial', help='absolute material prim path in composed asset')
    p.add_argument('--time', type=float, default=None, help='sample geometry and transforms at this USD time code; default time otherwise')
    p.add_argument('--max-attempts', type=int, default=1000000, help='maximum candidate points per mesh')
    a = p.parse_args()
    try:
        scatter(a.geo, a.render, a.count, a.seed, a.size, a.min_scale, a.max_scale,
                a.material, Usd.TimeCode.Default() if a.time is None else Usd.TimeCode(a.time), a.max_attempts,
                a.center_darken, a.height_darken, a.materials, a.shade_levels)
    except (ValueError, OSError, RuntimeError) as exc:
        p.exit(1, f'error: {exc}\n')

if __name__ == '__main__':
    main()
