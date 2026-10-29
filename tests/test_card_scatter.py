import tempfile
import unittest
import shutil
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade
from card_scatter import scatter, shade_factors, camera_position, camera_orientations


class ScatterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.geo = self.root / 'geo.usda'
        self.render = self.root / 'render.usda'
        stage = Usd.Stage.CreateNew(str(self.geo))
        root = UsdGeom.Xform.Define(stage, '/Model')
        root.AddTranslateOp().Set(Gf.Vec3d(10, 20, 30))
        mesh = UsdGeom.Mesh.Define(stage, '/Model/Volume')
        mesh.CreatePointsAttr([(-1,-1,-1),(1,-1,-1),(1,1,-1),(-1,1,-1),
                               (-1,-1,1),(1,-1,1),(1,1,1),(-1,1,1)])
        mesh.CreateFaceVertexCountsAttr([4]*6)
        mesh.CreateFaceVertexIndicesAttr([0,3,2,1,4,5,6,7,0,1,5,4,
                                          1,2,6,5,2,3,7,6,3,0,4,7])
        mesh.GetPrim().CreateAttribute('ScatterGeo', Sdf.ValueTypeNames.Bool, custom=True).Set(True)
        stage.GetRootLayer().Save()
        self.render.write_text('#usda 1.0\n( subLayers = [@./geo.usda@] )\n\nover "Model" {\n custom string retained = "yes"\n}\n')

    def run_scatter(self, seed=4):
        scatter(self.geo, self.render, 50, seed, 2, 0.5, 1.0,
                '/Looks/CardMaterial', Usd.TimeCode.Default(), 10000)

    def test_transformed_volume_and_repeatable_render(self):
        self.run_scatter()
        output = Usd.Stage.Open(str(self.render))
        inst = UsdGeom.PointInstancer.Get(output, '/ScatterCards')
        positions = list(inst.GetPositionsAttr().Get())
        self.assertEqual(len(positions), 50)
        for p in positions:
            self.assertTrue(9 <= p[0] <= 11 and 19 <= p[1] <= 21 and 29 <= p[2] <= 31, p)
        self.assertEqual(len(inst.GetProtoIndicesAttr().Get()), len(positions))
        self.assertEqual(len(inst.GetOrientationsAttr().Get()), len(positions))
        self.assertEqual(len(inst.GetScalesAttr().Get()), len(positions))
        self.assertEqual(output.GetPrimAtPath('/Model').GetAttribute('retained').Get(), 'yes')
        self.assertEqual(inst.GetPurposeAttr().Get(), 'render')
        proto = output.GetPrimAtPath('/ScatterCards/Prototypes/Card')
        self.assertEqual(proto.GetRelationship('material:binding').GetTargets(), [Sdf.Path('/Looks/CardMaterial')])
        del output, inst, proto
        self.run_scatter()
        output = Usd.Stage.Open(str(self.render))
        output.Reload()
        self.assertEqual(positions, list(UsdGeom.PointInstancer.Get(output, '/ScatterCards').GetPositionsAttr().Get()))
        self.assertEqual(list(output.GetRootLayer().subLayerPaths), ['./geo.usda'])

    def test_open_mesh_fails_without_modifying_output(self):
        stage = Usd.Stage.Open(str(self.geo))
        mesh = UsdGeom.Mesh.Get(stage, '/Model/Volume')
        mesh.CreateFaceVertexCountsAttr([4]*5)
        mesh.CreateFaceVertexIndicesAttr(list(mesh.GetFaceVertexIndicesAttr().Get())[:20])
        stage.GetRootLayer().Save()
        before = self.render.read_bytes()
        with self.assertRaisesRegex(ValueError, 'closed and manifold'):
            self.run_scatter()
        self.assertEqual(before, self.render.read_bytes())

    def test_no_marked_mesh_fails_without_modifying_output(self):
        stage = Usd.Stage.Open(str(self.geo))
        stage.GetPrimAtPath('/Model/Volume').GetAttribute('ScatterGeo').Set(False)
        stage.GetRootLayer().Save()
        before = self.render.read_bytes()
        with self.assertRaisesRegex(ValueError, 'no Mesh prims'):
            self.run_scatter()
        self.assertEqual(before, self.render.read_bytes())

    def test_shade_ramps_and_up_axis(self):
        vertices = [(-2, -2, -2), (2, 2, 2)]
        center, edge = shade_factors([(0, 0, 0), (2, 0, 0)], vertices, 0.8, 0, 1)
        self.assertAlmostEqual(center, 0.2)
        self.assertAlmostEqual(edge, 1)
        bottom, top = shade_factors([(0, 0, -2), (0, 0, 2)], vertices, 0, 0.7, 2)
        self.assertAlmostEqual(bottom, 0.3)
        self.assertAlmostEqual(top, 1)
        combined = shade_factors([(0, 0, 0)], vertices, 0.8, 0.7, 2)[0]
        self.assertAlmostEqual(combined, 0.2 * 0.65)

    def test_shading_variants_resolve_and_preserve_texture_and_scale(self):
        shutil.copy(Path(__file__).resolve().parents[1] / 'materials.usda', self.root)
        shutil.copytree(Path(__file__).resolve().parents[1] / 'textures', self.root / 'textures')
        self.run_scatter()
        stage = Usd.Stage.Open(str(self.render))
        original = UsdGeom.PointInstancer.Get(stage, '/ScatterCards')
        original_positions = list(original.GetPositionsAttr().Get())
        original_scales = list(original.GetScalesAttr().Get())
        self.assertGreater(len(set(float(s[0]) for s in original_scales)), 1)
        self.assertTrue(all(0.5 <= s[0] <= 1.0 for s in original_scales))
        del original, stage
        scatter(self.geo, self.render, 50, 4, 2, 0.5, 1.0, '/Looks/CardMaterial',
                Usd.TimeCode.Default(), 10000, 0.8, 0.7, shade_levels=8)
        stage = Usd.Stage.Open(str(self.render))
        stage.Reload()
        inst = UsdGeom.PointInstancer.Get(stage, '/ScatterCards')
        self.assertEqual(list(inst.GetPositionsAttr().Get()), original_positions)
        self.assertEqual(list(inst.GetScalesAttr().Get()), original_scales)
        prototypes = inst.GetPrototypesRel().GetTargets()
        indices = list(inst.GetProtoIndicesAttr().Get())
        self.assertGreater(len(prototypes), 1)
        self.assertTrue(all(0 <= i < len(prototypes) for i in indices))
        for path in prototypes:
            material, _ = UsdShade.MaterialBindingAPI(stage.GetPrimAtPath(path)).ComputeBoundMaterial()
            self.assertTrue(material)
            texture = UsdShade.Shader.Get(stage, material.GetPath().AppendChild('BrushTexture'))
            value = texture.GetInput('scale').Get()
            self.assertTrue(0 <= value[0] <= 1)
            self.assertEqual(value[3], 1)  # shading must not change opacity
            self.assertTrue(texture.GetInput('file').Get().resolvedPath.endswith('textures/brush.png'))
            self.assertTrue(material.GetSurfaceOutput().HasConnectedSource())

    def test_invalid_shading_leaves_render_untouched(self):
        before = self.render.read_bytes()
        with self.assertRaisesRegex(ValueError, 'shading strengths'):
            scatter(self.geo, self.render, 50, 4, 2, 0.5, 1.0,
                    '/Looks/CardMaterial', Usd.TimeCode.Default(), 10000, 1.1)
        self.assertEqual(before, self.render.read_bytes())

    def test_camera_facing_random_roll_and_retained_scales(self):
        camera_file = self.root / 'camera.usda'
        camera_stage = Usd.Stage.CreateNew(str(camera_file))
        parent = UsdGeom.Xform.Define(camera_stage, '/Rig')
        parent.AddTranslateOp().Set(Gf.Vec3d(4, 7, 10))
        camera = UsdGeom.Camera.Define(camera_stage, '/Rig/Camera')
        camera.AddTranslateOp().Set(Gf.Vec3d(9, 13, 25))
        camera_stage.GetRootLayer().Save()
        self.run_scatter()
        stage = Usd.Stage.Open(str(self.render))
        inst = UsdGeom.PointInstancer.Get(stage, '/ScatterCards')
        positions, scales = list(inst.GetPositionsAttr().Get()), list(inst.GetScalesAttr().Get())
        del stage, inst
        scatter(self.geo, self.render, 50, 4, 2, .5, 1, '/Looks/CardMaterial',
                Usd.TimeCode.Default(), 10000, camera_file=camera_file)
        stage = Usd.Stage.Open(str(self.render))
        stage.Reload()
        inst = UsdGeom.PointInstancer.Get(stage, '/ScatterCards')
        self.assertEqual(positions, list(inst.GetPositionsAttr().Get()))
        self.assertEqual(scales, list(inst.GetScalesAttr().Get()))
        camera_world = Gf.Vec3d(13, 20, 35)
        ups = []
        for point, orientation in zip(positions, inst.GetOrientationsAttr().Get()):
            rotation = Gf.Rotation(Gf.Quatd(orientation))
            normal = rotation.TransformDir(Gf.Vec3d(0, 0, 1)).GetNormalized()
            expected = (camera_world - Gf.Vec3d(point)).GetNormalized()
            self.assertGreater(Gf.Dot(normal, expected), .99999)
            ups.append(rotation.TransformDir(Gf.Vec3d(0, 1, 0)))
        self.assertLess(Gf.Dot(ups[0], ups[1]), .99)
        with self.assertRaisesRegex(ValueError, 'coincides'):
            camera_orientations([camera_world], camera_world, [0.5])

    def test_camera_selection_time_and_validation(self):
        camera_file = self.root / 'camera.usda'
        stage = Usd.Stage.CreateNew(str(camera_file))
        first = UsdGeom.Camera.Define(stage, '/First')
        first.AddTranslateOp().Set(Gf.Vec3d(1, 2, 3), Usd.TimeCode(10))
        UsdGeom.Camera.Define(stage, '/Second')
        stage.GetRootLayer().Save()
        geometry = Usd.Stage.Open(str(self.geo))
        with self.assertRaisesRegex(ValueError, '2 cameras'):
            camera_position(camera_file, None, Usd.TimeCode(10), geometry)
        self.assertEqual(camera_position(camera_file, '/First', Usd.TimeCode(10), geometry), Gf.Vec3d(1, 2, 3))
        with self.assertRaisesRegex(ValueError, 'camera prim not found'):
            camera_position(camera_file, '/Missing', Usd.TimeCode(10), geometry)
        UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
        stage.GetRootLayer().Save()
        with self.assertRaisesRegex(ValueError, 'same up axis'):
            camera_position(camera_file, '/First', Usd.TimeCode(10), geometry)

    def test_opacity_mask_survives_shaded_material_references(self):
        shutil.copy(Path(__file__).resolve().parents[1] / 'materials.usda', self.root)
        scatter(self.geo, self.render, 10, 4, 2, .5, 1, '/Looks/CardMaterial',
                Usd.TimeCode.Default(), 10000, center_darken=.5)
        stage = Usd.Stage.Open(str(self.render))
        inst = UsdGeom.PointInstancer.Get(stage, '/ScatterCards')
        for path in inst.GetPrototypesRel().GetTargets():
            material, _ = UsdShade.MaterialBindingAPI(stage.GetPrimAtPath(path)).ComputeBoundMaterial()
            shader = UsdShade.Shader.Get(stage, material.GetPath().AppendChild('PreviewSurface'))
            self.assertGreater(shader.GetInput('opacityThreshold').Get(), 0)
            src, name, _ = shader.GetInput('opacity').GetConnectedSource()
            self.assertEqual(name, 'a')
            self.assertEqual(src.GetPrim().GetPath(), material.GetPath().AppendChild('BrushTexture'))


if __name__ == '__main__':
    unittest.main()
