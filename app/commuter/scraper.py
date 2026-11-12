"""时刻表抓取：从首页找到站点分页链接，逐页解析停站记录。

约定的示例网站结构（也适用于同构的真实时刻表站点）：
  * 首页含若干 ``a.station-link``，文本为站点名，href 指向该站时刻表第 1 页；
  * 站点页含一张表格，表头出现 车次号 / 停站顺序 / 站点 / 时刻 / 服务日 等关键词；
  * 页脚有 ``a.next-page`` 指向下一页；最后一页没有该链接。
"""

from __future__ import annotations

from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from .times import DAILY, WEEKDAY, WEEKEND, StopRecord

REQUEST_TIMEOUT = 10
MAX_PAGES = 50  # 防止异常站点造成无限翻页

_HEADER_ALIASES = {
    "trip_no": ("车次", "车次号", "班车", "trip", "bus"),
    "stop_seq": ("停站顺序", "站序", "顺序", "序号", "seq", "order"),
    "station": ("站点", "站名", "车站", "station", "stop"),
    "clock": ("时刻", "时间", "到时", "发时", "time", "clock"),
    "service_kind": ("服务日", "班次类型", "运行日", "类型", "service", "day"),
}

_KIND_MAP = {
    "工作日": WEEKDAY,
    "周一至周五": WEEKDAY,
    "平日": WEEKDAY,
    "weekday": WEEKDAY,
    "周末": WEEKEND,
    "周六周日": WEEKEND,
    "双休": WEEKEND,
    "weekend": WEEKEND,
    "每日": DAILY,
    "每天": DAILY,
    "daily": DAILY,
}


def _norm(text: str) -> str:
    return "".join((text or "").split()).lower()


def _header_index(headers: list[str]) -> dict[str, int]:
    index: dict[str, int] = {}
    normed = [_norm(h) for h in headers]
    for field_name, aliases in _HEADER_ALIASES.items():
        for alias in aliases:
            key = _norm(alias)
            for i, header in enumerate(normed):
                if header and (header == key or key in header):
                    index[field_name] = i
                    break
            if field_name in index:
                break
    return index


def _map_service_kind(text: str) -> str | None:
    key = _norm(text)
    for label, kind in _KIND_MAP.items():
        if _norm(label) in key:
            return kind
    return None


def _fetch(session: requests.Session, url: str) -> str:
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or resp.encoding
    return resp.text


def _parse_int(value: str) -> int | None:
    value = (value or "").strip()
    return int(value) if value.isdigit() else None


def find_station_url(home_url: str, station_name: str, session: requests.Session) -> str:
    """在时刻表首页按站点名查找该站时刻表第 1 页的绝对 URL。"""
    html = _fetch(session, home_url)
    soup = BeautifulSoup(html, "html.parser")
    links = soup.select("a.station-link") or soup.find_all("a")

    target = _norm(station_name)
    # 先精确匹配，再做包含匹配，避免“东门”误匹配“东门枢纽”之类的情况
    for exact in (True, False):
        for link in links:
            label = _norm(link.get_text())
            href = link.get("href")
            if not href or not label:
                continue
            matched = label == target if exact else (target in label or label in target)
            if matched:
                return urljoin(home_url, href)
    raise LookupError(f"首页上找不到站点「{station_name}」的时刻表链接。")


def scrape_station_timetable(
    first_page_url: str, expected_station: str, session: requests.Session | None = None
) -> tuple[list[StopRecord], list[str]]:
    """逐页抓取某站点的时刻表，返回 ``(记录列表, 错误列表)``。

    只要有一页抓取失败或解析不到表头，错误列表非空；
    调用方据此提示“无法核定末班”，因为分页未取全，结论可能不成立。
    """
    session = session or requests.Session()
    records: list[StopRecord] = []
    errors: list[str] = []
    seen_urls: set[str] = set()
    url: str | None = first_page_url
    page_no = 0
    chain_broken = False

    while url and page_no < MAX_PAGES:
        page_no += 1
        if url in seen_urls:
            errors.append(f"分页出现循环链接（{url}），时刻表未取全。")
            chain_broken = True
            break
        seen_urls.add(url)

        try:
            html = _fetch(session, url)
        except requests.RequestException as exc:
            errors.append(f"第 {page_no} 页抓取失败（{url}）：{exc}")
            chain_broken = True
            break

        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if table is None:
            errors.append(f"第 {page_no} 页（{url}）找不到时刻表表格。")
            chain_broken = True
            break

        header_row = table.find("tr")
        headers = [cell.get_text(strip=True) for cell in header_row.find_all(["th", "td"])]
        col = _header_index(headers)
        missing_cols = [name for name in ("trip_no", "stop_seq", "station", "clock", "service_kind")
                        if name not in col]
        if missing_cols:
            errors.append(f"第 {page_no} 页（{url}）表头缺少必要列：{'、'.join(missing_cols)}。")
            chain_broken = True
            break

        for row in table.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) <= max(col.values()):
                continue
            trip_no = cells[col["trip_no"]].get_text(strip=True)
            if not trip_no:
                continue
            kind = _map_service_kind(cells[col["service_kind"]].get_text(strip=True))
            if kind is None:
                kind = WEEKDAY  # 保守默认，问题由服务日筛选兜住
            records.append(
                StopRecord(
                    trip_no=trip_no,
                    stop_seq=_parse_int(cells[col["stop_seq"]].get_text()),
                    station=cells[col["station"]].get_text(strip=True) or expected_station,
                    clock=cells[col["clock"]].get_text(strip=True),
                    service_kind=kind,
                )
            )

        next_link = soup.select_one("a.next-page")
        url = urljoin(url, next_link["href"]) if next_link and next_link.get("href") else None

    if page_no >= MAX_PAGES and url:
        errors.append(f"分页超过 {MAX_PAGES} 页上限，时刻表可能未取全。")
        chain_broken = True
    if chain_broken:
        errors.append("分页时刻表未完整抓取，无法核定末班，请稍后重试或联系调度。")
    return records, errors
