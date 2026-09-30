"""家庭光伏储能调度优化器。

模型（所有能量单位均为瓦时 Wh，每个时段固定 15 分钟 = 0.25 小时）：

每个时段 t 的决策量（LP 松弛变量，约束矩阵为网络流全幺模结构，
整数输入下最优解取整后仍为整数可行最优）：
    g_t  从电网购电，满足 0 <= g_t <= grid_limit_t
    c_t  电池充电量（效率 100%），0 <= c_t <= charge_limit
    d_t  电池放电量，0 <= d_t <= discharge_limit
    u_t  弃光量，0 <= u_t

每段能量守恒（不向电网送电，负荷必须全部满足）：
    load_t = pv_t + g_t + d_t - c_t - u_t
即              g_t - c_t + d_t - u_t = load_t - pv_t

物理互斥（由目标函数自动保证，此处显式收紧，消除零电价退化）：
    缺负荷段（pv < load）：u_t = 0
    富余段  （pv >= load）：d_t = 0 且 u_t <= pv_t - load_t
    （后者保证购入的电不会被当作"弃光"浪费）

电池期末电量（含最后一段）：
    soc_t = soc_{t-1} + c_t - d_t，且 reserve <= soc_t <= capacity

分层优化：
    1) 购电总费用最小： sum(g_t * price_t / 1000)        （元）
    2) 同费用时充放电总量最小： sum(c_t + d_t)           （Wh）

不可行时用逐段贪心（每段尽力把可用能量存入电池）定位最早供电缺口。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import linprog

PERIODS = 96
HOURS_PER_PERIOD = 0.25


def period_label(t: int) -> str:
    """返回时段起始时间标签，t=0 -> '00:00'，t=95 -> '23:45'。"""
    total = t * 15
    return f"{total // 60:02d}:{total % 60:02d}"


class InfeasibleError(Exception):
    """计划不可行，携带最早供电缺口信息。"""

    def __init__(self, gap_index: int, gap_wh: int, detail: dict):
        self.gap_index = gap_index
        self.gap_wh = gap_wh
        self.detail = detail
        super().__init__(
            f"最早供电缺口在时段 {gap_index + 1}（{period_label(gap_index)} 起），"
            f"缺口 {gap_wh} Wh"
        )


@dataclass
class SuffixInput:
    """一次求解所需的逐段数据（96 段全量，或实测后重算的剩余段）。"""

    load: list[int]
    pv: list[int]
    grid_limit: list[int]
    price: list[float]
    initial_soc: int
    capacity: int
    reserve: int
    charge_limit: int
    discharge_limit: int

    def __post_init__(self):
        n = len(self.load)
        if not all(len(x) == n for x in (self.pv, self.grid_limit, self.price)):
            raise ValueError("逐段数组长度必须一致")


def _constraints(inp: SuffixInput):
    """构造 (A_eq, b_eq, A_ub, b_ub)。变量列序：每段 [g, c, d, u]。"""
    n = len(inp.load)
    nv = 4 * n

    ub_spec: list[dict] = []
    for t in range(n):
        base = 4 * t
        surplus = inp.pv[t] - inp.load[t]
        ub_spec.append({"col": base, "rhs": inp.grid_limit[t]})          # g
        ub_spec.append({"col": base + 1, "rhs": inp.charge_limit})      # c
        ub_spec.append({"col": base + 2, "rhs": inp.discharge_limit})   # d
        if surplus >= 0:
            ub_spec.append({"col": base + 2, "rhs": 0.0})               # 富余段 d=0
            ub_spec.append({"col": base + 3, "rhs": float(surplus)})    # u <= surplus
        else:
            ub_spec.append({"col": base + 3, "rhs": 0.0})               # 缺负荷段 u=0

    soc_count = 2 * n
    total_rows = len(ub_spec) + soc_count
    A_ub = np.zeros((total_rows, nv))
    b_ub = np.zeros(total_rows)

    for i, spec in enumerate(ub_spec):
        A_ub[i, spec["col"]] = 1.0
        b_ub[i] = spec["rhs"]

    r = len(ub_spec)
    cum = np.zeros(nv)
    for j in range(1, n + 1):
        t = j - 1
        cum[4 * t + 1] = 1.0    # +c
        cum[4 * t + 2] = -1.0   # -d
        # soc_j <= capacity
        A_ub[r] = cum
        b_ub[r] = inp.capacity - inp.initial_soc
        r += 1
        # soc_j >= reserve
        A_ub[r] = -cum
        b_ub[r] = inp.initial_soc - inp.reserve
        r += 1

    A_eq = np.zeros((n, nv))
    b_eq = np.zeros(n)
    for t in range(n):
        base = 4 * t
        A_eq[t, base] = 1.0       # g
        A_eq[t, base + 1] = -1.0  # c
        A_eq[t, base + 2] = 1.0   # d
        A_eq[t, base + 3] = -1.0  # u
        b_eq[t] = inp.load[t] - inp.pv[t]

    return A_eq, b_eq, A_ub, b_ub


def _solve_lp(inp: SuffixInput):
    """两层目标求解，返回 (变量矩阵 x[t]=(g,c,d,u), 第一层最小费用)。"""
    n = len(inp.load)
    A_eq, b_eq, A_ub, b_ub = _constraints(inp)
    bounds = [(0.0, None)] * (4 * n)

    # 第一层：最小购电费用（price 元/kWh × g Wh / 1000 = 元）
    c1 = np.zeros(4 * n)
    for t in range(n):
        c1[4 * t] = inp.price[t] / 1000.0

    res1 = linprog(c1, A_ub=A_ub, b_ub=b_ub, A_eq=A_eq, b_eq=b_eq,
                   bounds=bounds, method="highs")
    if not res1.success:
        return None, None

    # 第二层：在最小费用之上最小化充放电总量。
    tol = 1e-7 * max(1.0, abs(res1.fun))
    A_ub2 = np.vstack([A_ub, c1.reshape(1, -1)])
    b_ub2 = np.concatenate([b_ub, [res1.fun + tol]])

    c2 = np.zeros(4 * n)
    for t in range(n):
        c2[4 * t + 1] = 1.0  # c
        c2[4 * t + 2] = 1.0  # d

    res2 = linprog(c2, A_ub=A_ub2, b_ub=b_ub2, A_eq=A_eq, b_eq=b_eq,
                   bounds=bounds, method="highs")

    x_raw = res2.x if res2.success else res1.x
    x = x_raw.reshape(n, 4)
    x[np.abs(x) < 1e-7] = 0.0
    return x, float(res1.fun)


def earliest_gap(inp: SuffixInput) -> tuple[int, int, dict] | None:
    """逐段贪心判定可行性并定位最早供电缺口。

    每段在满足用电后尽量把可用能量（光伏富余、购电剩余额度）存入电池，
    使后续时段的供电灵活性最大；返回 (最早缺口段下标, 缺口 Wh, 诊断)，可行返回 None。
    """
    soc = inp.initial_soc
    for t in range(len(inp.load)):
        net = inp.load[t] - inp.pv[t]
        if net >= 0:
            # 缺负荷段：购电优先，不足部分由电池放电补足
            from_grid = min(inp.grid_limit[t], net)
            deficit = net - from_grid
            can_discharge = min(inp.discharge_limit, soc - inp.reserve)
            if deficit > can_discharge:
                return t, int(deficit - can_discharge), {
                    "load": inp.load[t],
                    "pv": inp.pv[t],
                    "grid_limit": inp.grid_limit[t],
                    "soc_before": soc,
                    "deficit_after_grid": int(deficit),
                    "battery_available": int(can_discharge),
                }
            soc -= min(deficit, can_discharge)
            # 购电额度仍有剩余时可为未来充电（净负荷为 0 时同样适用）
            spare_grid = inp.grid_limit[t] - from_grid
            charge = min(inp.charge_limit, inp.capacity - soc, spare_grid)
            soc += charge
        else:
            # 富余段：先充光伏富余，充不满且有购电额度时再从电网补充
            surplus = -net
            charge = min(inp.charge_limit, inp.capacity - soc, surplus)
            room = min(inp.charge_limit - charge, inp.capacity - soc - charge)
            charge += min(room, inp.grid_limit[t])
            soc += charge
    return None


def _repair_row(t: int, vals: np.ndarray, inp: SuffixInput, soc: int) -> tuple[dict, int]:
    """把一段 LP 浮点解取整为整数解并保持守恒与物理约束，返回 (计划行, 新 soc)。

    约束矩阵为全幺模网络流结构，整数输入下顶点解必为整数；此处只处理
    求解器数值误差（1e-6 量级），按购电→充放电的顺序微调。
    """
    g, c, d, u = (int(round(v)) for v in vals)
    net = inp.load[t] - inp.pv[t]  # 守恒：g - c + d - u = net

    if net <= 0:
        # 富余段：禁止放电，弃光吸收守恒
        d = 0
        u = g - c - net
        if u < 0:  # 充电偏多
            c += u
            u = 0
        if u > -net:  # 购电偏多
            g -= u + net
            u = -net
        g, c = max(0, g), max(0, c)
    else:
        # 缺负荷段：禁止弃光
        u = 0
        diff = net - (g - c + d)  # >0 供应略缺，<0 供应略多（取整偏差，通常 ±1）
        if diff > 0:
            add = min(diff, inp.grid_limit[t] - g)
            g += add
            diff -= add
            if diff > 0:
                add = min(diff, inp.discharge_limit - d,
                          max(0, soc - inp.reserve - d))
                d += add
                diff -= add
            if diff > 0:
                sub = min(diff, c)
                c -= sub
                diff -= sub
        elif diff < 0:
            sub = min(-diff, g)
            g -= sub
            diff += sub
            if diff < 0:
                sub = min(-diff, d)
                d -= sub
                diff += sub
            if diff < 0:
                add = min(-diff, inp.charge_limit - c,
                          max(0, inp.capacity - soc + d - c))
                c += add
                diff += add
        if diff != 0:
            raise RuntimeError(f"第 {t + 1} 段取整后无法保持守恒（偏差 {diff} Wh）")

    # 电池不得同时充放：去掉对冲量（c,d 同时为正时对消不改变守恒）
    common = min(c, d)
    c -= common
    d -= common

    new_soc = soc + c - d
    if not (inp.reserve <= new_soc <= inp.capacity):
        raise RuntimeError(
            f"第 {t + 1} 段取整后期末电量 {new_soc} 越界"
            f"[{inp.reserve}, {inp.capacity}]"
        )
    if inp.load[t] != inp.pv[t] + g + d - c - u:
        raise RuntimeError(f"第 {t + 1} 段取整后能量不守恒")
    if g < 0 or c < 0 or d < 0 or u < 0 or g > inp.grid_limit[t] \
            or c > inp.charge_limit or d > inp.discharge_limit:
        raise RuntimeError(f"第 {t + 1} 段取整后越界")

    row = {
        "grid_buy": g,
        "charge": c,
        "discharge": d,
        "curtail": u,
        "soc_end": new_soc,
        "cost": round(g * inp.price[t] / 1000.0, 6),
    }
    return row, new_soc


def solve(inp: SuffixInput) -> list[dict]:
    """求解并返回逐段计划（整数 Wh）。不可行时抛 InfeasibleError。"""
    gap = earliest_gap(inp)
    if gap is not None:
        idx, gap_wh, detail = gap
        raise InfeasibleError(idx, gap_wh, detail)

    x, _ = _solve_lp(inp)
    if x is None:  # 贪心已通过时 LP 不会失败，双保险
        raise RuntimeError("优化器求解失败")

    n = len(inp.load)
    out: list[dict] = []
    soc = inp.initial_soc
    for t in range(n):
        row, soc = _repair_row(t, x[t], inp, soc)
        out.append(row)
    return out


def total_cost(rows: list[dict]) -> float:
    return round(sum(r["cost"] for r in rows), 4)


def total_cycling(rows: list[dict]) -> int:
    return sum(r["charge"] + r["discharge"] for r in rows)
