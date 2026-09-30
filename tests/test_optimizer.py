"""优化器单元测试：分层最优、不送网、边界、不可行缺口定位、取整完整性。"""

import random
import unittest

from app.optimizer import (SuffixInput, earliest_gap, solve, total_cost,
                           total_cycling, InfeasibleError)


def check_rows(r, inp: SuffixInput):
    soc = inp.initial_soc
    for t, row in enumerate(r):
        self_balance(inp, t, row)
        soc += row["charge"] - row["discharge"]
        assert soc == row["soc_end"]
        assert inp.reserve <= soc <= inp.capacity
        assert 0 <= row["grid_buy"] <= inp.grid_limit[t]
        assert 0 <= row["charge"] <= inp.charge_limit
        assert 0 <= row["discharge"] <= inp.discharge_limit
        assert not (row["charge"] > 0 and row["discharge"] > 0)
        if inp.pv[t] >= inp.load[t]:
            assert row["discharge"] == 0
            assert 0 <= row["curtail"] <= inp.pv[t] - inp.load[t]
        else:
            assert row["curtail"] == 0


def self_balance(inp, t, row):
    assert inp.load[t] == (inp.pv[t] + row["grid_buy"] + row["discharge"]
                           - row["charge"] - row["curtail"])


class OptimizerTest(unittest.TestCase):
    def test_flat_price_no_free_energy_no_cycling(self):
        inp = SuffixInput([1000] * 96, [0] * 96, [5000] * 96, [0.5] * 96,
                          500, 4000, 500, 1000, 1000)
        r = solve(inp)
        check_rows(r, inp)
        self.assertEqual(0, total_cycling(r))
        self.assertAlmostEqual(48.0, total_cost(r), places=6)

    def test_initial_surplus_energy_is_used(self):
        inp = SuffixInput([1000] * 96, [0] * 96, [5000] * 96, [0.5] * 96,
                          2000, 4000, 500, 1000, 1000)
        r = solve(inp)
        check_rows(r, inp)
        self.assertEqual(0, sum(x["charge"] for x in r))
        self.assertEqual(1500, sum(x["discharge"] for x in r))
        self.assertAlmostEqual(47.25, total_cost(r), places=6)

    def test_pv_shifting_saves_money(self):
        pv = [3000 if 28 <= t < 44 else 0 for t in range(96)]
        kw = dict(load=[1000] * 96, pv=pv, grid_limit=[5000] * 96,
                  price=[0.5] * 96, capacity=8000, reserve=500,
                  charge_limit=2000, discharge_limit=2000)
        r = solve(SuffixInput(initial_soc=500, **kw))
        check_rows(r, SuffixInput(initial_soc=500, **kw))
        r0 = solve(SuffixInput(initial_soc=500, capacity=500, reserve=500,
                               charge_limit=2000, discharge_limit=2000,
                               load=[1000] * 96, pv=pv,
                               grid_limit=[5000] * 96, price=[0.5] * 96))
        self.assertLess(total_cost(r), total_cost(r0))

    def test_peak_valley_arbitrage(self):
        price = [1.0] * 96
        for t in range(24):
            price[t] = 0.0
        inp = SuffixInput([1000] * 96, [0] * 96, [5000] * 96, price,
                          500, 8000, 500, 2000, 2000)
        r = solve(inp)
        check_rows(r, inp)
        self.assertGreater(sum(r[t]["charge"] for t in range(24)), 0)
        self.assertAlmostEqual(64.5, total_cost(r), places=6)

    def test_no_export_when_full_and_surplus(self):
        inp = SuffixInput([500] * 96, [3000] * 96, [2000] * 96, [0.3] * 96,
                          4000, 4000, 0, 2000, 2000)
        r = solve(inp)
        check_rows(r, inp)
        self.assertTrue(all(x["grid_buy"] == 0 for x in r))
        self.assertEqual(2500, r[0]["curtail"])

    def test_infeasible_gap_located(self):
        inp = SuffixInput([10000] * 96, [0] * 96, [100] * 96, [0.5] * 96,
                          2000, 4000, 0, 1000, 1000)
        with self.assertRaises(InfeasibleError) as cm:
            solve(inp)
        self.assertEqual(0, cm.exception.gap_index)
        # t0: 缺 9900 购电后，电池最多放 1000，缺口 8900
        self.assertEqual(8900, cm.exception.gap_wh)

    def test_gap_later_period(self):
        # 前段可行，靠电池+购电；后续段缺口
        load = [0] * 96
        load[50] = 9000
        inp = SuffixInput(load, [0] * 96, [1000] * 96, [0.5] * 96,
                          2000, 2000, 0, 1000, 1000)
        with self.assertRaises(InfeasibleError) as cm:
            solve(inp)
        self.assertEqual(50, cm.exception.gap_index)

    def test_random_feasible_instances(self):
        random.seed(2026)
        tested = 0
        for _ in range(120):
            L = [random.randint(0, 3000) for _ in range(96)]
            P = [random.randint(0, 3000) for _ in range(96)]
            G = [random.randint(0, 4000) for _ in range(96)]
            pr = [round(random.uniform(0, 2), 3) for _ in range(96)]
            cap = random.choice([2000, 4000, 8000, 12000])
            res = random.choice([0, 500])
            init = random.randint(res, cap)
            cl = random.choice([1000, 2000])
            inp = SuffixInput(L, P, G, pr, init, cap, res, cl, cl)
            if earliest_gap(inp) is not None:
                with self.assertRaises(InfeasibleError):
                    solve(inp)
                continue
            r = solve(inp)
            check_rows(r, inp)
            tested += 1
        self.assertGreater(tested, 20)

    def test_zero_price_tie_break_min_cycling(self):
        # 全零电价 + 全富余：费用恒为 0，应零充放
        inp = SuffixInput([0] * 96, [1000] * 96, [0] * 96, [0.0] * 96,
                          4000, 4000, 0, 1000, 1000)
        r = solve(inp)
        check_rows(r, inp)
        self.assertEqual(0, total_cycling(r))
        self.assertEqual(1000, r[0]["curtail"])

    def test_suffix_feasibility_with_grid_charge(self):
        # t2 负荷 3000 但该段购电上限为 0，须靠 t0/t1 从电网充电跨段供给
        grid = [3000] * 96
        grid[2] = 0
        load = [0] * 96
        load[2] = 3000
        inp = SuffixInput(load, [0] * 96, grid, [0.3] * 96,
                          0, 3000, 0, 3000, 3000)
        r = solve(inp)
        check_rows(r, inp)
        self.assertEqual(3000, r[2]["discharge"])
        self.assertEqual(0, r[2]["grid_buy"])


if __name__ == "__main__":
    unittest.main()
