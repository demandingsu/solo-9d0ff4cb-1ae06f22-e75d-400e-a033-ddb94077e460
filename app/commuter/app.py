"""夜班接驳车末班核定 Web 应用。"""

from __future__ import annotations

from datetime import datetime

import requests
from flask import Flask, render_template, request

from .scraper import find_station_url, scrape_station_timetable
from .times import SERVICE_LABELS, determine_last_trip

app = Flask(__name__)


def _parse_local_dt(raw: str, field_label: str) -> datetime:
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_label}格式无效，请通过日期时间选择器填写。") from exc


@app.route("/", methods=["GET", "POST"])
def index():
    form = {
        "home_url": "",
        "board_station": "",
        "alight_station": "",
        "earliest_depart": "",
        "latest_arrive": "",
    }
    if request.method != "POST":
        return render_template("index.html", form=form)

    form.update({key: request.form.get(key, "").strip() for key in form})
    errors: list[str] = []       # 阻断性问题：无法核定末班
    warnings: list[str] = []     # 数据告警：不影响已核定班次，但需人工留意
    board_source = alight_source = None
    determination = None

    required = [
        ("home_url", "时刻表首页 URL"),
        ("board_station", "上车站"),
        ("alight_station", "下车站"),
        ("earliest_depart", "最早出门时间"),
        ("latest_arrive", "到岗截止时间"),
    ]
    for key, label in required:
        if not form[key]:
            errors.append(f"请填写{label}。")

    earliest_depart = latest_arrive = None
    if not errors:
        try:
            earliest_depart = _parse_local_dt(form["earliest_depart"], "最早出门时间")
            latest_arrive = _parse_local_dt(form["latest_arrive"], "到岗截止时间")
        except ValueError as exc:
            errors.append(str(exc))
        if earliest_depart and latest_arrive and latest_arrive <= earliest_depart:
            errors.append("到岗截止时间必须晚于最早出门时间。")
        if form["board_station"] == form["alight_station"]:
            errors.append("上车站与下车站不能相同。")

    if not errors:
        session = requests.Session()
        try:
            board_url = find_station_url(form["home_url"], form["board_station"], session)
            alight_url = find_station_url(form["home_url"], form["alight_station"], session)
        except (requests.RequestException, LookupError) as exc:
            board_url = alight_url = None
            errors.append(f"读取时刻表首页失败：{exc}")

        board_records = alight_records = None
        scrape_broken = False
        if board_url and alight_url:
            board_records, board_errors = scrape_station_timetable(
                board_url, form["board_station"], session
            )
            alight_records, alight_errors = scrape_station_timetable(
                alight_url, form["alight_station"], session
            )
            scrape_broken = bool(board_errors or alight_errors)
            errors.extend(board_errors)
            errors.extend(alight_errors)
            board_source, alight_source = board_url, alight_url

        if board_records is not None and alight_records is not None:
            determination = determine_last_trip(
                board_records,
                alight_records,
                form["board_station"],
                form["alight_station"],
                earliest_depart,
                latest_arrive,
            )
            if scrape_broken:
                # 分页不完整，任何配对结论都不可信，仅保留错误提示
                determination = None
            elif determination.latest is None:
                # 没有任何可信班次，所有问题升级为阻断性错误
                errors.extend(determination.problems)
                if not any("无法核定末班" in item for item in errors):
                    errors.append("在给定时间窗内没有符合条件的班次，无法核定末班。")
            else:
                # 已核定末班，其他车次的数据问题只作告警展示
                warnings.extend(determination.problems)

    return render_template(
        "index.html",
        form=form,
        errors=errors,
        warnings=warnings,
        determination=determination,
        board_source=board_source,
        alight_source=alight_source,
        service_labels=SERVICE_LABELS,
        now=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
