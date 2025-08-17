from datetime import datetime
from collections.abc import Callable, Iterator

from polygon import RESTClient
from polygon.rest.aggs import Agg, HTTPResponse

from vnpy.trader.constant import Interval
from vnpy.trader.object import BarData, TickData, HistoryRequest
from vnpy.trader.datafeed import BaseDatafeed
from vnpy.trader.setting import SETTINGS
from vnpy.trader.database import DB_TZ


INTERVAL_VT2POLYGON = {
    Interval.MINUTE: "minute",
    Interval.HOUR: "hour",
    Interval.DAILY: "day",
}


class PolygonDatafeed(BaseDatafeed):
    """Polygon.io数据服务接口"""

    def __init__(self) -> None:
        """"""
        self.api_key: str = SETTINGS["datafeed.password"]

        self.client: RESTClient
        self.inited: bool = False

    def init(self, output: Callable = print) -> bool:
        """初始化"""
        if self.inited:
            return True

        if not self.api_key:
            output("Polygon.io数据服务初始化失败：API密钥为空！")
            return False

        try:
            self.client = RESTClient(self.api_key)

            self.client.get_exchanges(asset_class='options')
        except Exception as e:
            output(f"Polygon.io数据服务初始化失败：{e}")
            return False

        self.inited = True
        return True

    def query_bar_history(self, req: HistoryRequest, output: Callable = print) -> list[BarData] | None:
        """查询K线数据"""
        if not self.inited:
            n: bool = self.init(output)
            if not n:
                return []

        symbol = req.symbol
        exchange = req.exchange
        interval = req.interval
        start = req.start
        end = req.end

        polygon_interval = INTERVAL_VT2POLYGON.get(interval)
        if not polygon_interval:
            output(f"Polygon.io查询K线数据失败：不支持的时间周期{interval.value}")
            return []

        if len(symbol) > 10:
            symbol = "O:" + symbol  # Polygon要求期权代码前加O:前缀

        # polygon客户端的list_aggs方法返回一个处理分页的迭代器
        aggs: Iterator[Agg] | HTTPResponse = self.client.list_aggs(
            ticker=symbol,
            multiplier=1,
            timespan=polygon_interval,
            from_=start,
            to=end,
            limit=5000      # 每次查5000条
        )

        bars: list[BarData] = []
        for agg in aggs:
            # Polygon时间戳是毫秒，转换为datetime
            dt: datetime = datetime.fromtimestamp(agg.timestamp / 1000)

            # list_aggs可能返回超出请求范围的数据，所以需要过滤
            if not (start <= dt <= end):
                continue

            bar = BarData(
                symbol=req.symbol,
                exchange=exchange,
                datetime=dt.replace(tzinfo=DB_TZ),
                interval=interval,
                volume=agg.volume,
                open_price=agg.open,
                high_price=agg.high,
                low_price=agg.low,
                close_price=agg.close,
                turnover=agg.vwap * agg.volume,
                gateway_name="POLYGON"
            )
            bars.append(bar)

        return bars

    def query_tick_history(self, req: HistoryRequest, output: Callable = print) -> list[TickData] | None:
        """查询Tick数据"""
        return []
