"""Flask Web 应用：页面路由 + JSON API。"""

from __future__ import annotations

import json

from flask import Flask, jsonify, render_template, request
from markupsafe import Markup

from . import db, service
from .optimizer import InfeasibleError, PERIODS, period_label
from .sample_data import sample_payload
from .validation import ValidationError, validate_plan_payload


def create_app() -> Flask:
    app = Flask(__name__)
    app.json.ensure_ascii = False
    db.init_db()

    # ---------------- 页面 ----------------

    @app.route("/")
    def page_dashboard():
        return render_template("dashboard.html", periods=PERIODS)

    @app.route("/input")
    def page_input():
        return render_template("input.html", periods=PERIODS)

    @app.route("/actuals")
    def page_actuals():
        return render_template("actuals.html", periods=PERIODS)

    @app.route("/plans/<int:plan_id>")
    def page_plan(plan_id: int):
        detail = service.plan_detail(plan_id)
        if detail is None:
            return render_template("notfound.html"), 404
        # 预序列化并转义 < 防止 </script> 注入（数值型数据，但稳妥起见）
        rows_json = json.dumps(detail["periods"], ensure_ascii=False).replace(
            "<", "\\u003c").replace(">", "\\u003e").replace(
            "&", "\\u0026")
        return render_template("plan.html", detail=detail, periods=PERIODS,
                               rows_json=Markup(rows_json))

    @app.route("/plans")
    def page_plans():
        with db.get_conn() as conn:
            plans = [dict(r) for r in db.list_plans(conn)]
        return render_template("plans.html", plans=plans)

    # ---------------- API ----------------

    @app.get("/api/state")
    def api_state():
        return jsonify(service.active_dashboard())

    @app.get("/api/sample")
    def api_sample():
        return jsonify(sample_payload())

    @app.get("/api/plans/<int:plan_id>")
    def api_plan(plan_id: int):
        detail = service.plan_detail(plan_id)
        if detail is None:
            return jsonify({"ok": False, "message": "计划不存在"}), 404
        return jsonify({"ok": True, **detail})

    @app.post("/api/plans")
    def api_create_plan():
        try:
            payload = validate_plan_payload(request.get_json(silent=True))
        except ValidationError as e:
            return jsonify({"ok": False, "message": e.message,
                            "field": e.field}), 400
        result = service.create_plan(payload)
        if not result.get("ok"):
            return jsonify(result), 422
        return jsonify(result)

    @app.post("/api/actuals")
    def api_add_actual():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({"ok": False, "message": "请求体必须是 JSON 对象"}), 400

        from .validation import validate_actual_payload
        with db.get_conn() as conn:
            nxt = db.count_actuals(conn)

        # 若客户端显式带了 period，必须与下一段一致（已执行段锁定，顺序登记）
        if body.get("period") is not None:
            try:
                given = int(body.get("period"))
            except (TypeError, ValueError):
                given = -1
            if given != nxt:
                return jsonify({
                    "ok": False,
                    "message": (f"必须按时段顺序登记且已执行时段不可修改，"
                                f"下一段应为第 {nxt + 1} 段"
                                f"（{period_label(nxt)} 起）。"),
                    "field": "period", "period": nxt,
                    "period_label": period_label(nxt),
                }), 400

        clean = {k: body.get(k) for k in ("load", "pv", "grid_buy", "soc_end")}
        try:
            actual = validate_actual_payload({**clean, "period": nxt}, nxt)
        except ValidationError as e:
            return jsonify({"ok": False, "message": e.message,
                            "field": e.field,
                            "period": nxt,
                            "period_label": period_label(nxt)}), 400

        result = service.add_actual(actual)
        if not result.get("ok"):
            return jsonify(result), 422
        return jsonify(result)

    @app.post("/api/reset")
    def api_reset():
        db.reset_db()
        return jsonify({"ok": True})

    @app.errorhandler(ValidationError)
    def handle_validation(e):
        return jsonify({"ok": False, "message": e.message,
                        "field": e.field}), 400

    @app.errorhandler(InfeasibleError)
    def handle_infeasible(e):
        return jsonify({"ok": False, "infeasible": True,
                        "message": str(e),
                        "gap_period": e.gap_index,
                        "gap_period_label": period_label(e.gap_index),
                        "gap_wh": e.gap_wh}), 422

    @app.errorhandler(500)
    def handle_500(e):
        return jsonify({"ok": False, "message": f"服务器内部错误：{e}"}), 500

    return app
