import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pxr import Gf, Sdf, Usd, UsdGeom, UsdShade
from card_scatter import scatter


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


if __name__ == '__main__':
    unittest.main()
