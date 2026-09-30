"""输入与实测数据校验。

规则：
- 逐段的用电、光伏、购电上限必须为非负整数 Wh；购电单价为非负有限数（元/kWh）。
- 电池：容量为正整数；0 <= 保留电量 < 容量；保留 <= 初始电量 <= 容量；
  每段充/放电上限为 1..容量 的整数。缺项、负值、越界一律拒绝保存。
- 实测：按段顺序登记，须满足守恒、不送网、购电上限、电量边界、充放电上限。
"""

from __future__ import annotations

import math
from typing import Any

from .optimizer import PERIODS

MAX_WH = 100_000_000
MAX_PRICE = 1_000_000.0


class ValidationError(Exception):
    def __init__(self, message: str, field: str | None = None):
        super().__init__(message)
        self.message = message
        self.field = field


def parse_nonneg_int(value: Any, field: str, *, allow_zero: bool = True,
                     maximum: int = MAX_WH) -> int:
    """严格解析非负整数：拒绝 None、布尔、浮点字符串、负数。"""
    if value is None or isinstance(value, bool):
        raise ValidationError("必须填写整数", field)
    if isinstance(value, int):
        iv = value
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            raise ValidationError("不能为空", field)
        try:
            iv = int(s)
        except ValueError:
            raise ValidationError("必须是整数", field)
    elif isinstance(value, float) and value.is_integer():
        iv = int(value)
    else:
        raise ValidationError("必须是整数", field)
    if iv < 0 or (iv == 0 and not allow_zero):
        raise ValidationError("必须为非负整数" if allow_zero else "必须大于 0", field)
    if iv > maximum:
        raise ValidationError(f"不能超过 {maximum}", field)
    return iv


def parse_price(value: Any, field: str) -> float:
    if value is None or isinstance(value, bool) or isinstance(value, str) and not value.strip():
        raise ValidationError("购电单价不能为空", field)
    try:
        fv = float(value)
    except (TypeError, ValueError):
        raise ValidationError("购电单价必须是数字", field)
    if not math.isfinite(fv) or fv < 0:
        raise ValidationError("购电单价必须为非负数", field)
    if fv > MAX_PRICE:
        raise ValidationError(f"购电单价不能超过 {MAX_PRICE:g}", field)
    return round(fv, 4)


def validate_plan_payload(payload: Any) -> dict:
    """校验完整的计划提交，返回规范化字典。"""
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")

    battery = payload.get("battery")
    if not isinstance(battery, dict):
        raise ValidationError("缺少电池参数", "battery")

    capacity = parse_nonneg_int(battery.get("capacity"), "battery.capacity",
                                allow_zero=False)
    reserve = parse_nonneg_int(battery.get("reserve"), "battery.reserve")
    initial_soc = parse_nonneg_int(battery.get("initial_soc"),
                                   "battery.initial_soc")
    charge_limit = parse_nonneg_int(battery.get("charge_limit"),
                                    "battery.charge_limit", allow_zero=False)
    discharge_limit = parse_nonneg_int(battery.get("discharge_limit"),
                                       "battery.discharge_limit",
                                       allow_zero=False)

    if reserve >= capacity:
        raise ValidationError("最低保留电量必须小于电池容量", "battery.reserve")
    if not reserve <= initial_soc <= capacity:
        raise ValidationError("初始电量必须在保留电量与容量之间",
                              "battery.initial_soc")
    if charge_limit > capacity:
        raise ValidationError("每段充电上限不能超过电池容量",
                              "battery.charge_limit")
    if discharge_limit > capacity:
        raise ValidationError("每段放电上限不能超过电池容量",
                              "battery.discharge_limit")

    periods_raw = payload.get("periods")
    if not isinstance(periods_raw, list) or len(periods_raw) != PERIODS:
        raise ValidationError(f"必须提供 {PERIODS} 个时段数据", "periods")

    periods: list[dict] = []
    for i, row in enumerate(periods_raw):
        if not isinstance(row, dict):
            raise ValidationError(f"第 {i + 1} 段数据格式错误", f"periods.{i}")
        try:
            load = parse_nonneg_int(row.get("load"), f"periods.{i}.load")
            pv = parse_nonneg_int(row.get("pv"), f"periods.{i}.pv")
            grid_limit = parse_nonneg_int(row.get("grid_limit"),
                                          f"periods.{i}.grid_limit")
            price = parse_price(row.get("price"), f"periods.{i}.price")
        except ValidationError as e:
            raise ValidationError(f"第 {i + 1} 段（{_label(i)}）：{e.message}",
                                  e.field)
        periods.append({
            "load": load, "pv": pv, "grid_limit": grid_limit, "price": price,
        })

    return {
        "battery": {
            "capacity": capacity,
            "reserve": reserve,
            "initial_soc": initial_soc,
            "charge_limit": charge_limit,
            "discharge_limit": discharge_limit,
        },
        "periods": periods,
    }


def validate_actual_payload(payload: Any, next_period: int) -> dict:
    """校验实测提交的四个整数字段及段序号。"""
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是 JSON 对象")

    period = payload.get("period")
    if period is None:
        period = next_period
    try:
        t = int(period)
    except (TypeError, ValueError):
        raise ValidationError("时段序号必须是整数", "period")
    if t != next_period:
        raise ValidationError(
            f"必须按时段顺序登记，下一段应为第 {next_period + 1} 段", "period")

    fields = {}
    for key, label in (("load", "实际用电"), ("pv", "实际光伏"),
                       ("grid_buy", "实际购电"), ("soc_end", "期末电量")):
        fields[key] = parse_nonneg_int(payload.get(key), key)
        if fields[key] > MAX_WH:
            raise ValidationError(f"{label}超出允许范围", key)
    fields["period"] = t
    return fields


def check_actual_physics(t: int, actual: dict, *, soc_before: int,
                         battery: dict, grid_limit: int) -> list[str]:
    """对一条实测做物理校验，返回错误信息列表（空列表表示通过）。

    由期末电量反推净充入 c-d，再由能量守恒反推弃光 u：
        u = pv + grid_buy + (d - c) - load = pv + g + soc_before - soc_end - load
    要求 u >= 0（不缺供）、g <= 购电上限、
    reserve <= soc_end <= capacity、净充/净放各自不超过每段上限。
    """
    errors: list[str] = []
    load = actual["load"]
    pv = actual["pv"]
    g = actual["grid_buy"]
    soc_end = actual["soc_end"]

    reserve = battery["reserve"]
    capacity = battery["capacity"]
    charge_limit = battery["charge_limit"]
    discharge_limit = battery["discharge_limit"]

    if not reserve <= soc_end <= capacity:
        errors.append(
            f"期末电量 {soc_end} 超出允许范围 [{reserve}, {capacity}] Wh")

    net_charge = soc_end - soc_before  # c - d
    if net_charge > charge_limit:
        errors.append(f"该段净充电 {net_charge} Wh 超过充电上限 {charge_limit} Wh")
    if -net_charge > discharge_limit:
        errors.append(
            f"该段净放电 {-net_charge} Wh 超过放电上限 {discharge_limit} Wh")

    if g > grid_limit:
        errors.append(f"购电 {g} Wh 超过该段购电上限 {grid_limit} Wh")

    # 守恒反推弃光 u；弃光只能来自光伏，故要求 0 <= u <= pv。
    # u<0：用完购入电量与电池可放电量仍缺供（且不允许向电网送电）；
    # u>pv：有购入电量或电池电量凭空消失（既没供负荷也没留在电池里）。
    u = pv + g + soc_before - soc_end - load
    if u < 0:
        errors.append(
            f"能量不平衡：用电 {load} Wh，但光伏+购电+电池最多只能供应 "
            f"{load + u} Wh，缺口 {-u} Wh（且不允许向电网送电）")
    elif u > pv:
        wasted = u - pv
        errors.append(
            f"能量去向不明：有 {wasted} Wh 的购入电量或电池电量既未用于用电，"
            f"也未留在电池中（弃光最多只能等于实际光伏 {pv} Wh）；"
            f"请核对购电与期末电量。")
    return errors


def _label(t: int) -> str:
    total = t * 15
    return f"{total // 60:02d}:{total % 60:02d}"
