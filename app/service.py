"""业务服务层：计划创建、实测登记与按实测电量重算余段。"""

from __future__ import annotations

from . import db
from .optimizer import (SuffixInput, solve, earliest_gap, total_cost,
                        total_cycling, period_label, PERIODS)
from .validation import check_actual_physics, ValidationError


def _battery_of(plan) -> dict:
    return {
        "capacity": plan["capacity"],
        "reserve": plan["reserve"],
        "initial_soc": plan["initial_soc"],
        "charge_limit": plan["charge_limit"],
        "discharge_limit": plan["discharge_limit"],
    }


def _suffix_input(periods: list, battery: dict,
                  start: int, initial_soc: int) -> SuffixInput:
    tail = periods[start:]
    return SuffixInput(
        load=[p["load"] for p in tail],
        pv=[p["pv"] for p in tail],
        grid_limit=[p["grid_limit"] for p in tail],
        price=[p["price"] for p in tail],
        initial_soc=initial_soc,
        capacity=battery["capacity"],
        reserve=battery["reserve"],
        charge_limit=battery["charge_limit"],
        discharge_limit=battery["discharge_limit"],
    )


def create_plan(data: dict) -> dict:
    """校验并求解新计划；不可行时不保存，返回缺口信息。"""
    battery = data["battery"]
    periods_in = data["periods"]

    inp = _suffix_input(periods_in, battery, 0, battery["initial_soc"])
    gap = earliest_gap(inp)
    if gap is not None:
        idx, gap_wh, detail = gap
        return {
            "ok": False,
            "infeasible": True,
            "gap_period": idx,
            "gap_period_label": period_label(idx),
            "gap_wh": gap_wh,
            "detail": detail,
            "message": (f"计划不可行，最早供电缺口在第 {idx + 1} 段"
                        f"（{period_label(idx)} 起），缺口 {gap_wh} Wh；"
                        f"该段用电 {detail['load']} Wh、光伏 {detail['pv']} Wh、"
                        f"购电上限 {detail['grid_limit']} Wh，"
                        f"购电后仍缺 {detail['deficit_after_grid']} Wh，"
                        f"电池最多再供 {detail['battery_available']} Wh。"
                        f"未保存计划。"),
        }

    with db.get_conn() as conn:
        if db.count_actuals(conn) > 0:
            raise ValidationError(
                "已有已执行时段，无法整体替换计划；如需重新规划请先清空数据。")
        results = solve(inp)
        cost = total_cost(results)
        plan_id = db.insert_plan(
            conn, source="new", battery=battery, periods_in=periods_in,
            results=results, executed_count=0, total_cost=cost)

    return {"ok": True, "plan_id": plan_id, "total_cost": cost,
            "total_cycling": total_cycling(results)}


def add_actual(actual: dict) -> dict:
    """登记一条实测，校验失败不落库；通过后冻结该段并重算余段。"""
    with db.get_conn() as conn:
        plan = db.get_active_plan(conn)
        if plan is None:
            raise ValidationError("尚无可执行的计划，请先创建计划。")
        periods = [dict(p) for p in db.get_plan_periods(conn, plan["id"])]
        actuals = db.get_actuals(conn)
        k = len(actuals)
        if k >= PERIODS:
            raise ValidationError("96 个时段均已登记完毕。")

        if actual["period"] != k:
            raise ValidationError(
                f"必须按时段顺序登记，下一段应为第 {k + 1} 段"
                f"（{period_label(k)} 起）。")

        battery = _battery_of(plan)
        soc_before = actuals[-1]["soc_end"] if actuals else battery["initial_soc"]
        frozen = periods[k]

        errors = check_actual_physics(
            k, actual, soc_before=soc_before, battery=battery,
            grid_limit=frozen["grid_limit"])
        if errors:
            return {"ok": False, "errors": errors,
                    "period": k,
                    "period_label": period_label(k)}

        # 由守恒反推冻结段的充/放/弃光（校验已保证 0<=u<=pv，记录物理真实）
        # 守恒：load = pv + grid_buy + discharge - charge - curtail
        net_charge = actual["soc_end"] - soc_before
        charge = max(net_charge, 0)
        discharge = max(-net_charge, 0)
        curtail = (actual["pv"] + actual["grid_buy"] + soc_before
                   - actual["soc_end"] - actual["load"])
        frozen_cost = round(
            actual["grid_buy"] * frozen["price"] / 1000.0, 6)

        # 余段重算（输入预测沿用计划）
        start = k + 1
        warning = None
        suffix_results: list | None = None
        if start < PERIODS:
            inp = _suffix_input(periods, battery, start, actual["soc_end"])
            gap = earliest_gap(inp)
            if gap is not None:
                idx, gap_wh, detail = gap
                abs_idx = start + idx
                warning = (
                    f"警告：按实测电量重算余段不可行，最早供电缺口在第 "
                    f"{abs_idx + 1} 段（{period_label(abs_idx)} 起），"
                    f"缺口 {gap_wh} Wh（该段用电 {detail['load']} Wh、"
                    f"光伏 {detail['pv']} Wh、购电上限 {detail['grid_limit']} Wh，"
                    f"购电后仍缺 {detail['deficit_after_grid']} Wh，"
                    f"电池最多再供 {detail['battery_available']} Wh）。"
                    f"当前实测已保存并锁定；为避免误导，第 {start + 1} 段及以后"
                    f"仍展示重算前的旧计划数值供参考（已不再可行），"
                    f"旧计划版本也仍可查看。请调整后续购电/用电安排，"
                    f"登记下一可行实测后计划将自动恢复。")
            else:
                suffix_results = solve(inp)

        # 组装全量 96 段：已执行段（含本段）用实测冻结；
        # 余段可行时用重算结果，不可行时保留旧计划行作只读参考
        results: list[dict] = []
        periods_in: list[dict] = []
        for t in range(PERIODS):
            p = periods[t]
            if t < k:
                results.append({
                    "grid_buy": p["grid_buy"], "charge": p["charge"],
                    "discharge": p["discharge"], "curtail": p["curtail"],
                    "soc_end": p["soc_end"], "cost": p["cost"],
                })
            elif t == k:
                results.append({
                    "grid_buy": actual["grid_buy"], "charge": charge,
                    "discharge": discharge, "curtail": curtail,
                    "soc_end": actual["soc_end"], "cost": frozen_cost,
                })
            elif suffix_results is not None:
                results.append(suffix_results[t - start])
            else:
                # 余段不可行：沿用重算前计划行作为参考
                results.append({
                    "grid_buy": p["grid_buy"], "charge": p["charge"],
                    "discharge": p["discharge"], "curtail": p["curtail"],
                    "soc_end": p["soc_end"], "cost": p["cost"],
                })
            if t <= k:
                # 冻结段输入改为实测
                periods_in.append({
                    "load": actuals[t]["load"] if t < k else actual["load"],
                    "pv": actuals[t]["pv"] if t < k else actual["pv"],
                    "grid_limit": p["grid_limit"], "price": p["price"],
                })
            else:
                periods_in.append({
                    "load": p["load"], "pv": p["pv"],
                    "grid_limit": p["grid_limit"], "price": p["price"],
                })

        db.insert_actual(conn, k, actual)
        cost = total_cost(results)
        new_id = db.insert_plan(
            conn, source="actuals", battery=battery, periods_in=periods_in,
            results=results, executed_count=start, total_cost=cost,
            warning=warning)

    return {"ok": True, "plan_id": new_id, "executed_count": start,
            "total_cost": cost, "warning": warning,
            "frozen": {"charge": charge, "discharge": discharge,
                       "curtail": curtail, "cost": frozen_cost}}


def plan_detail(plan_id: int) -> dict | None:
    with db.get_conn() as conn:
        plan = db.get_plan(conn, plan_id)
        if plan is None:
            return None
        rows = db.get_plan_periods(conn, plan_id)
        return {
            "plan": dict(plan),
            "periods": [dict(r) for r in rows],
            "total_cycling": total_cycling([dict(r) for r in rows]),
        }


def active_dashboard() -> dict:
    with db.get_conn() as conn:
        plan = db.get_active_plan(conn)
        actuals = db.get_actuals(conn)
        plans = db.list_plans(conn)
        data = {
            "plan": dict(plan) if plan else None,
            "actuals": [dict(a) for a in actuals],
            "plans": [dict(p) for p in plans],
            "plan_count": len(plans),
        }
        if plan:
            rows = db.get_plan_periods(conn, plan["id"])
            data["periods"] = [dict(r) for r in rows]
        return data
