"""示例数据：典型家庭 24 小时曲线（96 段 × 15 分钟），用于一键填充。"""

from __future__ import annotations

from .optimizer import PERIODS


def _interpolate(points: list[tuple[int, int]]) -> list[int]:
    """按 (段下标, 值) 关键点线性插值生成 96 段整数序列。"""
    out = [0] * PERIODS
    for (t0, v0), (t1, v1) in zip(points, points[1:]):
        for t in range(t0, t1):
            ratio = (t - t0) / (t1 - t0)
            out[t] = round(v0 + (v1 - v0) * ratio)
    lt, lv = points[-1]
    if lt < PERIODS:
        out[lt] = lv
    return out


# 用电 Wh/15min：夜间 200，早高峰 800，午间 500，晚高峰 1400
LOAD_POINTS = [
    (0, 200), (24, 200), (28, 600), (36, 800), (44, 500),
    (60, 500), (68, 900), (76, 1400), (84, 1000), (92, 400), (96, 200),
]
# 光伏：中午峰值约 3000 Wh/段
PV_POINTS = [
    (0, 0), (24, 0), (30, 400), (40, 3000), (44, 3000),
    (52, 2400), (60, 600), (66, 0), (96, 0),
]
# 分时电价（元/kWh）：谷 0.3（00:00-06:00）、平 0.7、峰 1.2（18:00-23:00）
PRICE_TABLE = [
    (0, 0.3), (24, 0.7), (72, 1.2), (92, 0.7), (96, 0.7),
]


def sample_payload() -> dict:
    load = _interpolate(LOAD_POINTS)
    pv = _interpolate(PV_POINTS)
    price = [0.0] * PERIODS
    for (t0, v), (t1, _) in zip(PRICE_TABLE, PRICE_TABLE[1:]):
        for t in range(t0, min(t1, PERIODS)):
            price[t] = v
    return {
        "battery": {
            "capacity": 10000,
            "reserve": 1000,
            "initial_soc": 4000,
            "charge_limit": 2500,
            "discharge_limit": 2500,
        },
        "periods": [
            {"load": load[t], "pv": pv[t], "grid_limit": 5000,
             "price": price[t]}
            for t in range(PERIODS)
        ],
    }
