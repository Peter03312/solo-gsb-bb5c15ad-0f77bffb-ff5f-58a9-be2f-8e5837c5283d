"""端到端业务测试：校验拒绝、不可行不保存、实测锁定与重算、旧计划可查。"""

import os
import tempfile
import unittest

_tmp = tempfile.mkdtemp(prefix="ems-test-")
os.environ["DATABASE_PATH"] = os.path.join(_tmp, "test.db")
os.environ.setdefault("FLASK_ENV", "testing")

from app.web import create_app  # noqa: E402
from app.sample_data import sample_payload  # noqa: E402


def flat(load=1000, pv=0, grid=5000, price=0.5):
    return [{"load": load, "pv": pv, "grid_limit": grid, "price": price}
            for _ in range(96)]


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.client = cls.app.test_client()

    def setUp(self):
        self.client.post("/api/reset")

    def test_validation_missing_negative_battery(self):
        body = sample_payload()
        body["battery"]["reserve"] = -1
        r = self.client.post("/api/plans", json=body)
        self.assertEqual(r.status_code, 400)
        self.assertIn("非负", r.get_json()["message"])

        body = sample_payload()
        body["battery"]["initial_soc"] = body["battery"]["reserve"] - 1
        r = self.client.post("/api/plans", json=body)
        self.assertEqual(400, r.status_code)
        self.assertIn("初始电量", r.get_json()["message"])

        body = sample_payload()
        body["battery"]["charge_limit"] = body["battery"]["capacity"] + 1
        self.assertEqual(400, self.client.post("/api/plans", json=body).status_code)

        body = sample_payload()
        body["periods"][10]["load"] = None
        r = self.client.post("/api/plans", json=body)
        self.assertEqual(400, r.status_code)
        self.assertIn("第 11 段", r.get_json()["message"])

        body = sample_payload()
        body["periods"][0]["price"] = -0.1
        r = self.client.post("/api/plans", json=body)
        self.assertEqual(400, r.status_code)

        body = sample_payload()
        body["periods"][0]["load"] = 1.5
        self.assertEqual(400, self.client.post("/api/plans", json=body).status_code)

    def test_infeasible_not_saved(self):
        body = {
            "battery": {"capacity": 4000, "reserve": 0, "initial_soc": 1000,
                        "charge_limit": 1000, "discharge_limit": 1000},
            "periods": flat(load=10000, pv=0, grid=100, price=0.5),
        }
        r = self.client.post("/api/plans", json=body)
        self.assertEqual(422, r.status_code)
        data = r.get_json()
        self.assertTrue(data["infeasible"])
        self.assertEqual(0, data["gap_period"])
        self.assertGreater(data["gap_wh"], 0)
        # 未保存：state 中无计划
        st = self.client.get("/api/state").get_json()
        self.assertIsNone(st["plan"])

    def test_create_and_plan_constraints(self):
        body = sample_payload()
        r = self.client.post("/api/plans", json=body)
        self.assertEqual(200, r.status_code)
        plan_id = r.get_json()["plan_id"]
        d = self.client.get(f"/api/plans/{plan_id}").get_json()
        rows = d["periods"]
        self.assertEqual(96, len(rows))
        b = body["battery"]
        soc = b["initial_soc"]
        for i, (row, inp) in enumerate(zip(rows, body["periods"])):
            self.assertEqual(
                inp["load"],
                inp["pv"] + row["grid_buy"] + row["discharge"]
                - row["charge"] - row["curtail"])
            soc += row["charge"] - row["discharge"]
            self.assertEqual(soc, row["soc_end"])
            self.assertGreaterEqual(soc, b["reserve"])
            self.assertLessEqual(soc, b["capacity"])
            self.assertGreaterEqual(row["grid_buy"], 0)
            self.assertLessEqual(row["grid_buy"], inp["grid_limit"])
            self.assertFalse(row["charge"] > 0 and row["discharge"] > 0)

    def test_actuals_sequence_validation_lock_recompute(self):
        body = sample_payload()
        plan_id = self.client.post("/api/plans", json=body).get_json()["plan_id"]

        # 正常登记 t=0：与计划一致
        r0 = body["periods"][0]
        plan = self.client.get(f"/api/plans/{plan_id}").get_json()["periods"][0]
        r = self.client.post("/api/actuals", json={
            "load": r0["load"], "pv": r0["pv"],
            "grid_buy": plan["grid_buy"], "soc_end": plan["soc_end"]})
        self.assertEqual(200, r.status_code, r.get_data(as_text=True))
        new_id = r.get_json()["plan_id"]
        self.assertEqual(1, r.get_json()["executed_count"])
        self.assertNotEqual(plan_id, new_id)

        # 已执行段锁定：不能重复登记 t=0，服务端要求下一段 t=1
        r = self.client.post("/api/actuals", json={
            "period": 0, "load": 1, "pv": 0, "grid_buy": 0, "soc_end": 4000})
        self.assertEqual(400, r.status_code)
        self.assertIn("顺序", r.get_json()["message"])

        # 错误记录（超购电上限）不落库
        st = self.client.get("/api/state").get_json()
        self.assertEqual(1, len(st["actuals"]))
        bad = self.client.post("/api/actuals", json={
            "load": 1000, "pv": 0,
            "grid_buy": st["periods"][1]["grid_limit"] + 1,
            "soc_end": st["periods"][1]["soc_end"]})
        self.assertEqual(422, bad.status_code)
        self.assertIn("购电", bad.get_json()["errors"][0])
        st2 = self.client.get("/api/state").get_json()
        self.assertEqual(1, len(st2["actuals"]))  # 未入库

        # 能量缺口（不送网）拒绝：用电大于所有供给
        soc0 = st["actuals"][0]["soc_end"]
        bad2 = self.client.post("/api/actuals", json={
            "load": 9000, "pv": 0, "grid_buy": 0,
            "soc_end": max(0, soc0 - body["battery"]["discharge_limit"])})
        self.assertEqual(422, bad2.status_code)
        self.assertTrue(any("能量不平衡" in e for e in bad2.get_json()["errors"]))

        # 期末电量越界拒绝
        bad3 = self.client.post("/api/actuals", json={
            "load": 0, "pv": 0, "grid_buy": 0,
            "soc_end": body["battery"]["capacity"] + 1})
        self.assertEqual(422, bad3.status_code)

        # 正常登记 t=1，与计划一致
        p1 = st2["periods"][1]
        ok = self.client.post("/api/actuals", json={
            "load": body["periods"][1]["load"], "pv": body["periods"][1]["pv"],
            "grid_buy": p1["grid_buy"], "soc_end": p1["soc_end"]})
        self.assertEqual(200, ok.status_code)

        # 旧计划仍可查看
        old = self.client.get(f"/api/plans/{plan_id}")
        self.assertEqual(200, old.status_code)
        self.assertEqual(0, old.get_json()["plan"]["executed_count"])

    def test_recompute_changes_remaining_plan(self):
        # 简单场景：容量恒定电价，初始高于保留 -> t0 多放电，后续应重算
        body = {
            "battery": {"capacity": 4000, "reserve": 500, "initial_soc": 2000,
                        "charge_limit": 1000, "discharge_limit": 1000},
            "periods": flat(load=1000, pv=0, grid=5000, price=0.5),
        }
        self.client.post("/api/plans", json=body)
        # t0 实测：全部靠电池放电 1000，soc 1000；与原计划（放电量较少）不同
        r = self.client.post("/api/actuals", json={
            "load": 1000, "pv": 0, "grid_buy": 0, "soc_end": 1000})
        self.assertEqual(200, r.status_code)
        st = self.client.get("/api/state").get_json()
        self.assertEqual(0, st["actuals"][0]["grid_buy"])
        rows = st["periods"]
        self.assertEqual(0, rows[0]["grid_buy"])
        self.assertEqual(1000, rows[0]["discharge"])
        self.assertEqual(1, st["plan"]["executed_count"])
        # 余段守恒
        soc = 1000
        for row in rows[1:]:
            self.assertEqual(1000, row["grid_buy"] + row["discharge"]
                             - row["charge"] - row["curtail"])
            soc += row["charge"] - row["discharge"]
            self.assertEqual(soc, row["soc_end"])
            self.assertGreaterEqual(soc, 500)

    def test_no_plan_actual_rejected(self):
        r = self.client.post("/api/actuals", json={
            "load": 1, "pv": 0, "grid_buy": 0, "soc_end": 0})
        self.assertEqual(400, r.status_code)

    def test_actual_making_tail_infeasible_warns_and_keeps_plan(self):
        # t0/t1 各需 1000 且可大额购电充电；t2..t5 各需 1000 且购电为 0，
        # 原计划靠两段充满 4000 后逐段放电 -> 可行
        periods = []
        for t in range(96):
            grid = 5000 if t < 2 else 0
            load = 1000 if t <= 5 else 0
            periods.append({"load": load, "pv": 0, "grid_limit": grid, "price": 0.5})
        body = {
            "battery": {"capacity": 4000, "reserve": 0, "initial_soc": 2000,
                        "charge_limit": 2000, "discharge_limit": 2000},
            "periods": periods,
        }
        pid = self.client.post("/api/plans", json=body).get_json()["plan_id"]

        # t0 实测：用电 1000 全部由电池放电供给，未给电池补电 ->
        # 物理合法，但电池只剩 1000，t1 充满也仅 3000 < 4000，余段将缺 1000
        r = self.client.post("/api/actuals", json={
            "load": 1000, "pv": 0, "grid_buy": 0, "soc_end": 1000})
        self.assertEqual(200, r.status_code)
        data = r.get_json()
        self.assertIsNotNone(data["warning"])
        self.assertIn("最早供电缺口", data["warning"])
        # 缺口在 t5（末段只有 3000 电量供 4000 需求）
        self.assertIn("第 6 段", data["warning"])

        # 实测仍入库并锁定，旧计划可查，余段保留重算前旧计划行作参考
        st = self.client.get("/api/state").get_json()
        self.assertEqual(1, len(st["actuals"]))
        self.assertEqual(1, st["plan"]["executed_count"])
        self.assertEqual(200, self.client.get(f"/api/plans/{pid}").status_code)
        # 旧计划 t1：购电 3000（1000 供负荷 + 2000 充电），期末充满 4000
        self.assertEqual(3000, st["periods"][1]["grid_buy"])
        self.assertEqual(2000, st["periods"][1]["charge"])
        self.assertEqual(4000, st["periods"][1]["soc_end"])
        # 冻结段 t0 为实测：放电 1000、无弃光
        self.assertEqual(1000, st["periods"][0]["discharge"])
        self.assertEqual(0, st["periods"][0]["curtail"])

    def test_recover_to_feasible_after_warning(self):
        # 同上但 t2 起购电上限 1000 可直接兜底 -> t0 用电池供电后余段仍可行
        periods = []
        for t in range(96):
            grid = 5000 if t < 2 else 1000
            load = 1000 if t <= 5 else 0
            periods.append({"load": load, "pv": 0, "grid_limit": grid, "price": 0.5})
        body = {
            "battery": {"capacity": 4000, "reserve": 0, "initial_soc": 2000,
                        "charge_limit": 2000, "discharge_limit": 2000},
            "periods": periods,
        }
        self.client.post("/api/plans", json=body)
        r0 = self.client.post("/api/actuals", json={
            "load": 1000, "pv": 0, "grid_buy": 0, "soc_end": 1000})
        self.assertEqual(200, r0.status_code)
        self.assertIsNone(r0.get_json()["warning"])

    def test_energy_without_destination_rejected(self):
        # 放电 1000 但用电只有 500 且无光伏：多余能量去向不明，必须拒绝
        body = {
            "battery": {"capacity": 4000, "reserve": 0, "initial_soc": 4000,
                        "charge_limit": 2000, "discharge_limit": 2000},
            "periods": [{"load": 1000, "pv": 0, "grid_limit": 5000, "price": 0.5}
                        for _ in range(96)],
        }
        self.client.post("/api/plans", json=body)
        r = self.client.post("/api/actuals", json={
            "load": 500, "pv": 0, "grid_buy": 0, "soc_end": 3000})
        self.assertEqual(422, r.status_code)
        self.assertTrue(any("去向不明" in e for e in r.get_json()["errors"]))


if __name__ == "__main__":
    unittest.main()
