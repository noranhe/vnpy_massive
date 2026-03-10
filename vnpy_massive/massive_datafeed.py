import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, cast

import requests  # type: ignore[import-untyped]

from vnpy.trader.constant import Interval
from vnpy.trader.datafeed import BaseDatafeed
from vnpy.trader.database import DB_TZ
from vnpy.trader.object import BarData, HistoryRequest, TickData
from vnpy.trader.setting import SETTINGS


BASE_URL = "https://api.polygon.io"
INDEX_UNDERLYINGS = frozenset({"SPX", "NDX", "RUT", "DJX", "VIX"})
INTERVAL_VT2POLYGON = {
    Interval.MINUTE: "minute",
    Interval.HOUR: "hour",
    Interval.DAILY: "day",
}


def _to_polygon_ticker(symbol: str) -> str:
    """将 VeighNa symbol 转为 Massive API ticker 格式。"""
    s = symbol.strip()
    if s.startswith("O:"):
        return s
    if s in INDEX_UNDERLYINGS:
        return f"I:{s}"
    if len(s) > 10 and not s.startswith("O:"):
        return f"O:{s}"
    return s


class MassiveDatafeed(BaseDatafeed):
    """Massive REST API 数据服务"""

    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
        base_backoff: float = 1.0,
        limit: int = 50000,
    ) -> None:
        """初始化 MassiveDatafeed。"""
        self.api_key: str = SETTINGS["datafeed.password"]
        self.base_url = base_url.rstrip("/")

        self.timeout = timeout
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.limit = limit
        self.inited: bool = False

    def init(self, output: Callable[[str], Any] = print) -> bool:
        """初始化数据服务，校验 API 连通性。"""
        if self.inited:
            return True

        if not self.api_key:
            output("MassiveDatafeed 初始化失败：API 密钥为空，请配置 datafeed.password")
            return False

        try:
            resp = requests.get(
                f"{self.base_url}/v3/reference/exchanges",
                params={"apiKey": self.api_key},
                timeout=self.timeout,
            )
            if resp.status_code != 200:
                output(f"MassiveDatafeed 初始化失败：HTTP {resp.status_code}")
                return False

        except Exception as e:
            output(f"MassiveDatafeed 初始化失败：{e}")
            return False

        self.inited = True
        return True

    def _get(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """单次 GET 请求，带指数退避重试。"""
        url = f"{self.base_url}{path}"
        p = dict(params or {})
        p["apiKey"] = self.api_key

        for attempt in range(self.max_retries + 1):
            try:
                resp = requests.get(url, params=p, timeout=self.timeout)
                if resp.status_code == 200:
                    return cast(dict[str, Any], resp.json())
                if resp.status_code == 429 or resp.status_code >= 500:
                    wait = self.base_backoff * (2**attempt)
                    time.sleep(wait)
                    continue

                raise Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")

            except (requests.RequestException, requests.Timeout):
                if attempt < self.max_retries:
                    wait = self.base_backoff * (2**attempt)
                    time.sleep(wait)
                else:
                    raise

        raise Exception(f"Max retries exceeded for {path}")

    def _get_all_pages(
        self,
        path: str,
        params: dict[str, str] | None = None,
        max_pages: int = 500,
    ) -> list[dict]:
        """分页 GET，跟随 next_url 直至取完。"""
        all_results: list[dict] = []
        data = self._get(path, params)
        all_results.extend(data.get("results", []))

        page = 1

        while "next_url" in data and page < max_pages:
            next_url = data["next_url"]
            if "apiKey=" not in next_url:
                sep = "&" if "?" in next_url else "?"
                next_url = f"{next_url}{sep}apiKey={self.api_key}"
            else:
                next_url = re.sub(r"apiKey=[^&]*", f"apiKey={self.api_key}", next_url)

            for attempt in range(self.max_retries + 1):
                try:
                    resp = requests.get(next_url, timeout=self.timeout)
                    if resp.status_code == 200:
                        data = resp.json()
                        all_results.extend(data.get("results", []))
                        page += 1
                        break
                    if resp.status_code == 429 or resp.status_code >= 500:
                        wait = self.base_backoff * (2**attempt)
                        time.sleep(wait)
                        continue

                    break

                except (requests.RequestException, requests.Timeout):
                    if attempt < self.max_retries:
                        wait = self.base_backoff * (2**attempt)
                        time.sleep(wait)
                    else:
                        return all_results

        return all_results

    def query_bar_history(
        self,
        req: HistoryRequest,
        output: Callable[[str], Any] = print,
    ) -> list[BarData]:
        """查询 K 线数据，支持股票、指数、期权。"""
        if not self.inited:
            ok = self.init(output)
            if not ok:
                return []

        symbol = req.symbol
        interval = req.interval
        start = req.start
        end = req.end

        if end is None:
            end = datetime.now()

        polygon_interval = INTERVAL_VT2POLYGON.get(interval) if interval else None
        if not polygon_interval:
            output(f"MassiveDatafeed 不支持的时间周期: {interval}")
            return []

        ticker = _to_polygon_ticker(symbol)
        from_str = start.strftime("%Y-%m-%d")
        to_str = end.strftime("%Y-%m-%d")

        path = f"/v2/aggs/ticker/{ticker}/range/1/{polygon_interval}/{from_str}/{to_str}"
        params = {"limit": str(self.limit), "sort": "asc"}

        results = self._get_all_pages(path, params)

        bars: list[BarData] = []
        for r in results:
            ts_ms = r.get("t", 0)
            dt = datetime.fromtimestamp(ts_ms / 1000)
            if not (start <= dt <= end):
                continue

            vwap_val = r.get("vw")
            vol_val = r.get("v", 0)
            if vwap_val is None:
                vwap_val = 0.0
            turnover = float(vwap_val) * float(vol_val)

            bar = BarData(
                symbol=req.symbol,
                exchange=req.exchange,
                datetime=dt.replace(tzinfo=DB_TZ),
                interval=interval,
                volume=float(r.get("v", 0)),
                turnover=turnover,
                open_price=float(r.get("o", 0)),
                high_price=float(r.get("h", 0)),
                low_price=float(r.get("l", 0)),
                close_price=float(r.get("c", 0)),
                gateway_name="MASSIVE",
            )
            bars.append(bar)

        return bars

    def query_tick_history(
        self,
        req: HistoryRequest,
        output: Callable[[str], Any] = print,
    ) -> list[TickData]:
        """查询 Tick 数据"""
        return []
