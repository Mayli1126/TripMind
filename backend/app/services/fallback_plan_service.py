"""离线降级模板：与 Agent、工具连接和正常规划流程隔离。"""

from ..models.schemas import TripRequest, TripPlan, DayPlan, Attraction, WeatherInfo, Location, Hotel, Budget
from .trip_quality_service import trip_quality_service


def create_fallback_plan(request: TripRequest, reason: str = "外部工具或模型输出不可用") -> TripPlan:
    """生成明确标记的离线参考模板，不代表真实天气、酒店或报价。"""
    from datetime import datetime, timedelta

    # 解析日期
    start_date = datetime.strptime(request.start_date, "%Y-%m-%d")

    # 城市经纬度映射(常见城市)
    city_coords = {
        "北京": (116.407396, 39.904200),
        "上海": (121.473701, 31.230416),
        "广州": (113.264385, 23.129112),
        "深圳": (114.057865, 22.543096),
        "成都": (104.066541, 30.572268),
        "杭州": (120.155070, 30.274086),
        "西安": (108.939621, 34.343147),
        "重庆": (106.551556, 29.563009),
        "武汉": (114.305393, 30.593099),
        "南京": (118.796877, 32.060255),
        "长沙": (112.938814, 28.228209),
        "苏州": (120.585316, 31.298886),
        "天津": (117.190686, 39.125596),
        "青岛": (120.382639, 36.067082),
        "厦门": (118.089425, 24.479834),
        "昆明": (102.832899, 25.038898),
        "大连": (121.614687, 38.914003),
        "哈尔滨": (126.534967, 45.803775),
    }
    base_lng, base_lat = city_coords.get(request.city, (116.397128, 39.916527))

    # 景点模板(按城市提供)
    attraction_templates = {
        "北京": [
            ("故宫博物院", "北京市东城区景山前街4号", 60, "历史文化", "中国明清两代的皇家宫殿,世界上现存规模最大的木质结构古建筑群,占地72万平方米。"),
            ("颐和园", "北京市海淀区新建宫门路19号", 30, "历史文化", "中国古典园林之首,清朝帝王避暑行宫,以昆明湖、万寿山为基址,以杭州西湖为蓝本建造。"),
            ("天坛公园", "北京市东城区天坛内东里7号", 15, "历史文化", "明清两代皇帝祭天祈谷的场所,是中国古代建筑艺术的精华。"),
            ("八达岭长城", "北京市延庆区G6京藏高速62号出口", 40, "历史文化", "万里长城的精华段落,地势险要,城关坚固。"),
            ("圆明园遗址公园", "北京市海淀区清华西路28号", 10, "历史文化", "清代大型皇家园林,有'万园之园'之称。"),
        ],
        "上海": [
            ("外滩", "上海市黄浦区中山东一路", 0, "城市风光", "上海标志性景观,全长1.5公里,东侧有哥特式、罗马式等52幢风格迥异的古典复兴大楼。"),
            ("东方明珠", "上海市浦东新区世纪大道1号", 180, "现代建筑", "上海地标建筑之一,高约468米,集都市观光、时尚餐饮、购物娱乐于一体。"),
            ("豫园", "上海市黄浦区安仁街137号", 40, "历史文化", "始建于明代的古典园林,有'东南名园冠'之美誉。"),
            ("迪士尼乐园", "上海市浦东新区川沙镇", 475, "主题乐园", "中国大陆首个迪士尼主题乐园,拥有众多独特的游乐项目和精彩演出。"),
            ("朱家角古镇", "上海市青浦区朱家角镇", 0, "历史文化", "上海四大历史文化名镇之一,有'上海威尼斯'之称。"),
        ],
    }

    # 获取城市景点模板,无则使用通用模板
    city_attractions = attraction_templates.get(request.city)
    if not city_attractions:
        city_attractions = [
            (f"{request.city}博物馆", f"{request.city}市中心", 0, "历史文化", f"展示{request.city}历史文化的综合性博物馆。"),
            (f"{request.city}古城", f"{request.city}老城区", 30, "历史文化", f"{request.city}保存完好的历史古城区域。"),
            (f"{request.city}公园", f"{request.city}市区", 0, "自然风光", f"{request.city}市民休闲的大型城市公园。"),
            (f"{request.city}商业街", f"{request.city}繁华地段", 0, "城市风光", f"{request.city}最具特色的商业街区。"),
            (f"{request.city}美食街", f"{request.city}市区", 0, "美食", f"品尝{request.city}地道美食的热门街区。"),
        ]

    # 天气模拟(根据月份推测)
    month = start_date.month
    if month in [12, 1, 2]:
        weathers = [("多云", "晴", 5, -3, "北风", "3-4级"), ("晴", "晴", 6, -2, "西北风", "2-3级")]
    elif month in [3, 4, 5]:
        weathers = [("多云", "多云", 18, 10, "南风", "2-3级"), ("晴", "多云", 20, 12, "东南风", "1-2级")]
    elif month in [6, 7, 8]:
        weathers = [("晴", "晴", 32, 25, "南风", "1-2级"), ("多云", "雷阵雨", 30, 24, "东南风", "3-4级")]
    else:
        weathers = [("多云", "多云", 22, 14, "东风", "2-3级"), ("晴", "晴", 24, 16, "南风", "1-2级")]

    # 住宿价格映射
    hotel_prices = {
        "经济型酒店": (200, 350),
        "舒适型酒店": (350, 600),
        "高档型酒店": (600, 1200),
        "豪华型酒店": (1200, 3000),
        "豪华酒店": (1200, 3000),
        "民宿": (250, 600),
    }
    cost_range = hotel_prices.get(request.accommodation, (300, 500))

    days = []
    total_attractions_cost = 0
    total_meals_cost = 0
    total_hotel_cost = 0
    weather_info_list = []

    for i in range(request.travel_days):
        current_date = start_date + timedelta(days=i)
        date_str = current_date.strftime("%Y-%m-%d")

        # 降级模板也遵循用户选择的行程节奏。
        pace_counts = {"relaxed": 2, "balanced": 3, "intensive": 4}
        num_attractions = min(pace_counts[request.pace], len(city_attractions))
        # 优先保证跨天不重复。候选不足时宁可减少每日安排，也不循环复制景点。
        start_index = round(i * len(city_attractions) / request.travel_days)
        end_index = round((i + 1) * len(city_attractions) / request.travel_days)
        day_attractions_templates = city_attractions[start_index:end_index][:num_attractions]
        if not day_attractions_templates:
            day_attractions_templates = [(
                f"{request.city}自由探索区域（第{i + 1}天）",
                f"{request.city}市区",
                0,
                "休闲",
                "预留为自由活动，请结合实时地图选择附近已开放的场所。",
            )]

        attractions = []
        for j, (name, address, price, category, desc) in enumerate(day_attractions_templates):
            offset_lng = (i * 0.02 + j * 0.008) % 0.1
            offset_lat = (i * 0.015 + j * 0.01) % 0.08
            attractions.append(Attraction(
                name=name,
                address=address,
                location=Location(longitude=round(base_lng + offset_lng, 6), latitude=round(base_lat + offset_lat, 6)),
                visit_duration=120,
                description=desc,
                category=category,
                ticket_price=price
            ))
            total_attractions_cost += price

        # 酒店(每天推荐一个)
        hotel_cost = cost_range[0] + (i * 37) % (cost_range[1] - cost_range[0])
        total_hotel_cost += hotel_cost
        hotel = Hotel(
            name=f"{request.city}{request.accommodation}第{i+1}晚",
            address=f"{request.city}市中心区域",
            location=Location(longitude=round(base_lng + i * 0.01, 6), latitude=round(base_lat + i * 0.005, 6)),
            price_range=f"{cost_range[0]}-{cost_range[1]}元/晚",
            rating=str(round(4.3 + (i % 3) * 0.2, 1)),
            distance="距景点约1-3公里",
            type=request.accommodation,
            estimated_cost=hotel_cost,
        )

        # 天气信息
        weather_tuple = weathers[i % len(weathers)]
        weather_info_list.append(WeatherInfo(
            date=date_str,
            day_weather=weather_tuple[0],
            night_weather=weather_tuple[1],
            day_temp=weather_tuple[2],
            night_temp=weather_tuple[3],
            wind_direction=weather_tuple[4],
            wind_power=weather_tuple[5],
        ))

        day_plan = DayPlan(
            date=date_str,
            day_index=i,
            description=f"第{i+1}天: {request.city}精华景点游览,感受{request.city}独特的城市魅力。",
            transportation=request.transportation,
            accommodation=request.accommodation,
            hotel=hotel,
            attractions=attractions,
            meals=[],
        )
        days.append(day_plan)

    total_transportation = request.travel_days * 60  # 每天约60元交通费
    total_budget = total_attractions_cost + total_hotel_cost + total_meals_cost + total_transportation

    return TripPlan(
        city=request.city,
        start_date=request.start_date,
        end_date=request.end_date,
        days=days,
        weather_info=weather_info_list,
        overall_suggestions=f"这是系统降级生成的{request.city}{request.travel_days}日参考模板。景点、天气、酒店、票价和坐标并非实时查询结果，请在出行前逐项核实。",
        budget = Budget(
            total_attractions=total_attractions_cost,
            total_hotels=total_hotel_cost,
            total_meals=total_meals_cost,
            total_transportation=total_transportation,
            total=total_budget,
        ),
        data_quality="fallback",
        is_fallback=True,
        warnings=[
            f"降级原因：{reason}。当前展示的是内置参考模板。",
            "天气、价格、开放时间、酒店和交通班次均不是实时数据。",
        ],
        data_sources=trip_quality_service.default_sources(is_fallback=True),
    )
