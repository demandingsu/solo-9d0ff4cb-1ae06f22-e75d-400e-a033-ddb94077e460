"""端到端冒烟测试：用 Flask 测试客户端驱动示例站点 + 核定应用。

仅在安装了两个项目的依赖（Flask/requests/bs4）后可运行：
    python -m unittest tests.test_smoke
"""

import sys
import unittest
from pathlib import Path

import requests
from werkzeug.serving import make_server

# 让示例站点模块可导入
EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "example-site"
sys.path.insert(0, str(EXAMPLE_DIR))
import app as example_app  # noqa: E402

from commuter.app import app as commuter_app  # noqa: E402


class _Server:
    def __init__(self, flask_app, port):
        self.httpd = make_server("127.0.0.1", port, flask_app)
        self.thread = None

    def start(self):
        import threading

        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()


class SmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _Server(example_app.app, 5051)
        cls.server.start()
        cls.base = "http://127.0.0.1:5051/"
        # 确认服务起来了
        requests.get(cls.base, timeout=5).raise_for_status()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def _post(self, **overrides):
        payload = {
            "home_url": self.base,
            "board_station": "滨河园区",
            "alight_station": "中央厂区",
            "earliest_depart": "2026-11-11T22:00",
            "latest_arrive": "2026-11-12T02:00",
        }
        payload.update(overrides)
        return commuter_app.test_client().post("/", data=payload)

    def test_full_query_finds_latest(self):
        html = self._post().get_data(as_text=True)
        self.assertIn("N205", html)
        # 主结论正常给出；N206 数据不全只出现在告警区，不阻断核定
        self.assertNotIn("⚠ 无法核定末班", html)
        self.assertIn("数据告警", html)
        self.assertIn("N206", html)
        self.assertIn("2026-11-12 00:40", html)
        self.assertIn("2026-11-12 01:50", html)

    def test_pagination_failure_station(self):
        html = self._post(
            board_station="故障北站",
            earliest_depart="2026-11-11T21:00",
            latest_arrive="2026-11-12T05:00",
        ).get_data(as_text=True)
        self.assertIn("无法核定末班", html)
        self.assertIn("抓取失败", html)

    def test_unknown_station(self):
        html = self._post(board_station="不存在的站").get_data(as_text=True)
        self.assertIn("找不到站点", html)

    def test_missing_clock_trip_warns_but_others_valid(self):
        # N206 在中央厂区完全没有停站记录，但 N205 等其他车次仍可核定
        html = self._post(
            board_station="滨河园区",
            alight_station="中央厂区",
            earliest_depart="2026-11-11T22:00",
            latest_arrive="2026-11-12T03:00",
        ).get_data(as_text=True)
        self.assertIn("数据告警", html)
        self.assertIn("N206", html)
        self.assertIn("无法核定该班次", html)

    def test_validation_errors(self):
        client = commuter_app.test_client()
        html = client.post("/", data={}).get_data(as_text=True)
        self.assertIn("请填写", html)


class VerifySmokeTests(unittest.TestCase):
    """实到核对端到端：示例站点（含分页实录与缺页实录）+ /verify 路由。"""

    @classmethod
    def setUpClass(cls):
        cls.server = _Server(example_app.app, 5052)
        cls.server.start()
        cls.base = "http://127.0.0.1:5052/"
        requests.get(cls.base, timeout=5).raise_for_status()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def _post(self, **overrides):
        payload = {
            "home_url": self.base,
            "log_url": self.base + "log/",
            "board_station": "滨河园区",
            "alight_station": "中央厂区",
            "verify_date": "2026-11-12",
        }
        payload.update(overrides)
        return commuter_app.test_client().post("/verify", data=payload)

    def test_confirmed_pairs_and_unmatched(self):
        html = self._post().get_data(as_text=True)
        self.assertNotIn("来源抓取不完整", html)
        # 确定配对：N203 晚点 70 分钟、N205 提前 10 分钟、N210 提前 2 分钟
        self.assertIn("确定配对", html)
        self.assertIn("N203", html)
        self.assertIn("晚点 70 分钟", html)
        self.assertIn("N205", html)
        self.assertIn("提前 10 分钟", html)
        self.assertIn("N210", html)
        self.assertIn("提前 2 分钟", html)
        # 来源链接：时刻表分页 + 实录分页
        self.assertIn("station/%E4%B8%AD%E5%A4%AE%E5%8E%82%E5%8C%BA", html)  # 中央厂区
        self.assertIn("log/page-2", html)
        # N207 每日运行但实录缺席 → 未见实到；X999 不在时刻表 → 无匹配计划
        self.assertIn("未见实到", html)
        self.assertIn("N207", html)
        self.assertIn("无匹配计划", html)
        self.assertIn("X999", html)
        # N206 在下车站无记录 → 数据告警，不阻断核对
        self.assertIn("数据告警", html)
        self.assertIn("N206", html)

    def test_adjacent_service_day_ambiguity(self):
        # 2026-11-14 凌晨 N210 同时存在周五（工作日 25:35）与周六（周末 01:30）
        # 两个计划，实到 01:40 与两者都可配对 → 待核实，不进确定配对
        html = self._post(verify_date="2026-11-14").get_data(as_text=True)
        self.assertIn("待核实", html)
        self.assertIn("N210", html)
        self.assertIn("2026-11-14 01:35", html)
        self.assertIn("2026-11-14 01:30", html)
        # N202、N205 当日仍可确定配对
        self.assertIn("确定配对", html)
        self.assertIn("晚点 3 分钟", html)

    def test_broken_log_shows_fetched_records_only(self):
        html = self._post(log_url=self.base + "log-broken/").get_data(as_text=True)
        # 实录第 2 页 500：只展示已抓取记录与失败页，不下晚点或缺车结论
        self.assertIn("不下晚点或缺车结论", html)
        self.assertIn("log-broken/page-2", html)
        self.assertIn("已抓取的实录记录", html)
        self.assertIn("2026-11-12 00:30", html)  # 第 1 页已抓到的记录
        self.assertIn("已抓取的时刻表记录", html)
        self.assertNotIn("确定配对", html)
        self.assertNotIn("未见实到", html)

    def test_validation_errors(self):
        client = commuter_app.test_client()
        html = client.post("/verify", data={}).get_data(as_text=True)
        self.assertIn("请填写", html)


if __name__ == "__main__":
    unittest.main()
