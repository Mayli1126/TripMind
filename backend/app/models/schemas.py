"""数据模型定义"""

from datetime import datetime, timedelta
from typing import Any, Dict, List, Literal, Optional, Union
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator, ValidationInfo
from ..config import get_settings

# Location 已抽到 geo 模块（schemas -> candidate -> geo 单向依赖，避免循环导入）；
# 此处 re-export 保持既有 `from .schemas import Location` 用法兼容。
from .geo import Location


def get_app_today():
    """返回旅行产品业务时区中的今天，避免部署到 UTC 服务器后日期错位。"""
    timezone_name = get_settings().app_timezone
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("UTC")
    return datetime.now(timezone).date()


# ============ 请求模型 ============

class TripRequest(BaseModel):
    """旅行规划请求"""
    origin_city: str = Field(..., min_length=1, max_length=50, description="出发城市", example="上海")
    city: str = Field(..., min_length=1, max_length=50, description="目的地城市", example="北京")
    start_date: str = Field(..., description="开始日期 YYYY-MM-DD", example="2025-06-01")
    end_date: str = Field(..., description="结束日期 YYYY-MM-DD", example="2025-06-03")
    travel_days: int = Field(..., description="旅行天数", ge=1, le=30, example=3)
    transportation: Literal[
        "high_speed_train", "train", "plane", "bus", "自驾", "公共交通"
    ] = Field(..., description="跨城交通方式", example="high_speed_train")
    departure_time: Optional[str] = Field(
        default=None, description="已确认的跨城出发时间 HH:MM", example="08:30"
    )
    arrival_time: Optional[str] = Field(
        default=None, description="已确认的跨城到达时间 HH:MM", example="14:20"
    )
    trip_type: Literal["round_trip", "one_way"] = Field(
        default="round_trip", description="往返或单程"
    )
    return_departure_time: Optional[str] = Field(
        default=None, description="已确认的返程出发时间 HH:MM", example="16:30"
    )
    return_arrival_time: Optional[str] = Field(
        default=None, description="已确认的返程到达时间 HH:MM", example="21:20"
    )
    accommodation: Literal[
        "经济型酒店", "舒适型酒店", "高档型酒店", "豪华型酒店", "豪华酒店", "民宿"
    ] = Field(..., description="住宿偏好", example="经济型酒店")
    preferences: List[str] = Field(default_factory=list, max_length=10, description="旅行偏好标签")
    free_text_input: Optional[str] = Field(default="", max_length=1000, description="额外要求")
    travelers: int = Field(default=1, ge=1, le=20, description="出行人数")
    rooms: int = Field(default=1, ge=1, le=10, description="房间数")
    budget_limit: Optional[int] = Field(default=None, ge=0, le=1_000_000, description="总预算上限(元)")
    optimization_goal: Literal["balanced", "economy", "fastest"] = Field(
        default="balanced", description="优化目标: 综合平衡/经济优先/时间优先"
    )
    pace: Literal["relaxed", "balanced", "intensive"] = Field(
        default="balanced", description="行程节奏: 轻松/适中/紧凑"
    )
    # 阶段 2 候选池接入：攻略导入确认后的候选地点随请求进入规划，
    # required/preferred 优先安排；excluded 由前端过滤，通常不随请求发送
    candidate_places: List["CandidatePlace"] = Field(
        default_factory=list, max_length=100,
        description="攻略候选地点（CandidatePlace 契约），规划时优先采用",
    )
    # 多路线选择：开启后生成前先 interrupt 让用户在 2-3 个行程方向中选一个
    # （骨架先行：方案由确定性代码构造，不额外消耗 LLM token）
    ask_route_options: bool = Field(
        default=False, description="生成前是否先比较多个行程方向（需要 Checkpointer 支持）"
    )

    @field_validator("start_date", "end_date")
    @classmethod
    def validate_date_format(cls, value: str) -> str:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("日期必须使用 YYYY-MM-DD 格式") from exc
        return value

    @field_validator("origin_city", "city")
    @classmethod
    def strip_city(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("城市不能为空")
        return value

    @model_validator(mode="after")
    @classmethod
    def validate_trip_range(cls, data, info: ValidationInfo):
        start = datetime.strptime(data.start_date, "%Y-%m-%d").date()
        end = datetime.strptime(data.end_date, "%Y-%m-%d").date()
        # 从存储加载时跳过日期校验（历史行程日期可能已过去）
        from_storage = info.context.get("from_storage", False) if info.context else False
        if not from_storage:
            today = get_app_today()
            if start < today:
                raise ValueError("出发日期不能早于今天")
        expected_days = (end - start).days + 1
        if expected_days <= 0:
            raise ValueError("结束日期不能早于开始日期")
        if expected_days != data.travel_days:
            raise ValueError(f"travel_days 应为 {expected_days}，必须与日期区间一致")
        if data.rooms > data.travelers:
            raise ValueError("房间数不能超过出行人数")
        if bool(data.departure_time) != bool(data.arrival_time):
            raise ValueError("跨城出发时间和到达时间需要同时填写")
        if bool(data.return_departure_time) != bool(data.return_arrival_time):
            raise ValueError("返程出发时间和到达时间需要同时填写")
        for label, departure_value, arrival_value in (
            ("去程", data.departure_time, data.arrival_time),
            ("返程", data.return_departure_time, data.return_arrival_time),
        ):
            if not departure_value:
                continue
            try:
                datetime.strptime(departure_value, "%H:%M")
                datetime.strptime(arrival_value, "%H:%M")
            except ValueError as exc:
                raise ValueError(f"{label}交通时间必须使用 HH:MM 格式") from exc
        if data.trip_type == "one_way" and data.return_departure_time:
            raise ValueError("单程旅行不应填写返程时间")
        if data.departure_time and data.arrival_time:
            departure = datetime.strptime(data.departure_time, "%H:%M")
            arrival = datetime.strptime(data.arrival_time, "%H:%M")
            arrival_date = start + timedelta(days=int(arrival <= departure))
            if arrival_date > end:
                raise ValueError("去程到达日期超出旅行日期范围，请延长结束日期")
        return data
    
    class Config:
        json_schema_extra = {
            "example": {
                "origin_city": "上海",
                "city": "北京",
                "start_date": "2025-06-01",
                "end_date": "2025-06-03",
                "travel_days": 3,
                "transportation": "high_speed_train",
                "accommodation": "经济型酒店",
                "preferences": ["历史文化", "美食"],
                "free_text_input": "希望多安排一些博物馆"
            }
        }


class POISearchRequest(BaseModel):
    """POI搜索请求"""
    keywords: str = Field(..., description="搜索关键词", example="故宫")
    city: str = Field(..., description="城市", example="北京")
    citylimit: bool = Field(default=True, description="是否限制在城市范围内")


class RouteRequest(BaseModel):
    """路线规划请求"""
    origin_address: str = Field(..., description="起点地址", example="北京市朝阳区阜通东大街6号")
    destination_address: str = Field(..., description="终点地址", example="北京市海淀区上地十街10号")
    origin_city: Optional[str] = Field(default=None, description="起点城市")
    destination_city: Optional[str] = Field(default=None, description="终点城市")
    route_type: str = Field(default="walking", description="路线类型: walking/driving/transit")


class TransportationOption(BaseModel):
    """交通方式选项"""
    type: str = Field(..., description="交通方式类型: train/high_speed_train/plane/bus", example="high_speed_train")
    name: str = Field(..., description="交通方式名称", example="高铁")
    price: int = Field(..., description="预估费用(元)", example="553")
    duration: str = Field(..., description="预计耗时", example="4小时30分钟")
    departure_time: Optional[str] = Field(default=None, description="出发时间", example="08:30")
    arrival_time: Optional[str] = Field(default=None, description="到达时间", example="13:00")
    departure_station: Optional[str] = Field(default=None, description="出发站", example="北京南站")
    arrival_station: Optional[str] = Field(default=None, description="到达站", example="上海虹桥站")
    description: Optional[str] = Field(default=None, description="详细描述")
    is_estimated: bool = Field(default=True, description="是否为估算数据")
    source: str = Field(default="estimated", description="数据来源")


class TransportationRequest(BaseModel):
    """通勤费用查询请求"""
    origin_city: str = Field(..., description="出发城市", example="北京")
    destination_city: str = Field(..., description="目的城市", example="上海")
    date: Optional[str] = Field(default=None, description="出发日期 YYYY-MM-DD", example="2025-06-15")


# ============ 响应模型 ============

class Attraction(BaseModel):
    """景点信息"""
    stable_id: str = Field(default="", description="计划内稳定 ID，Patch 操作据此定位，为空时由 patch_service 补全")
    name: str = Field(..., description="景点名称")
    address: str = Field(..., description="地址")
    location: Location = Field(..., description="经纬度坐标")
    visit_duration: int = Field(..., ge=15, le=1440, description="建议游览时间(分钟)")
    description: str = Field(..., description="景点描述")
    category: Optional[str] = Field(default="景点", description="景点类别")
    rating: Optional[float] = Field(default=None, ge=0, le=5, description="评分")
    photos: Optional[List[str]] = Field(default_factory=list, description="景点图片URL列表")
    poi_id: Optional[str] = Field(default="", description="POI ID")
    image_url: Optional[str] = Field(default=None, description="图片URL")
    ticket_price: int = Field(default=0, ge=0, description="门票价格(元/人)")
    opening_time: Optional[str] = Field(default=None, description="开放时间 HH:MM")
    closing_time: Optional[str] = Field(default=None, description="闭馆时间 HH:MM")
    last_entry_time: Optional[str] = Field(default=None, description="停止入园时间 HH:MM")
    closed_weekdays: List[int] = Field(default_factory=list, description="闭馆星期，周一为0")
    intensity: Optional[str] = Field(default=None, description="体力强度: high/medium/low")


class Meal(BaseModel):
    """餐饮信息"""
    type: str = Field(..., description="餐饮类型: breakfast/lunch/dinner/snack")
    name: str = Field(..., description="餐饮名称")
    address: Optional[str] = Field(default=None, description="地址")
    location: Optional[Location] = Field(default=None, description="经纬度坐标")
    description: Optional[str] = Field(default=None, description="描述")
    estimated_cost: int = Field(default=0, ge=0, description="预估费用(元/人)")


class Hotel(BaseModel):
    """酒店信息"""
    name: str = Field(..., description="酒店名称")
    address: str = Field(default="", description="酒店地址")
    location: Optional[Location] = Field(default=None, description="酒店位置")
    price_range: str = Field(default="", description="价格范围")
    rating: str = Field(default="", description="评分")
    distance: str = Field(default="", description="距离景点距离")
    type: str = Field(default="", description="酒店类型")
    estimated_cost: int = Field(default=0, ge=0, description="预估费用(元/间/晚)")


class DailyRouteSummary(BaseModel):
    """每日景点顺序优化摘要。"""

    distance_before_km: float = 0
    distance_after_km: float = 0
    estimated_travel_minutes: int = 0
    saved_percent: float = 0
    method: str = "nearest_neighbor_haversine"
    start_anchor: str = ""
    end_anchor: str = ""
    start_transfer_km: float = 0
    end_transfer_km: float = 0


class ScheduleItem(BaseModel):
    """按时间排序的单日执行步骤。"""

    start_time: str
    end_time: str
    item_type: Literal["meal", "transport", "attraction", "hotel", "break"]
    title: str
    description: str = ""
    address: str = ""
    related_index: Optional[int] = None
    duration_minutes: int = Field(default=0, ge=0)
    notes: str = ""
    stable_id: str = ""
    start_day_offset: int = Field(default=0, ge=0, le=7)
    end_day_offset: int = Field(default=0, ge=0, le=7)
    suggested_transport: str = Field(default="", description="建议交通方式: 步行/地铁/公交/打车/专车")


class DayPlan(BaseModel):
    """单日行程"""
    date: str = Field(..., description="日期 YYYY-MM-DD")
    day_index: int = Field(..., description="第几天(从0开始)")
    description: str = Field(..., description="当日行程描述")
    transportation: str = Field(..., description="交通方式")
    accommodation: str = Field(..., description="住宿")
    hotel: Optional[Hotel] = Field(default=None, description="推荐酒店")
    attractions: List[Attraction] = Field(default_factory=list, description="景点列表")
    meals: List[Meal] = Field(default_factory=list, description="餐饮列表")
    route_summary: Optional[DailyRouteSummary] = Field(default=None, description="当日路线优化摘要")
    theme: str = Field(default="", description="当日主题")
    pace: str = Field(default="", description="当日节奏: relaxed/standard/intense（Patch 可修改）")
    preferred_start_time: str = Field(default="", description="当日偏好出发时间 HH:MM（Patch 可修改）")
    schedule: List[ScheduleItem] = Field(default_factory=list, description="按时间排序的精细行程")


class WeatherInfo(BaseModel):
    """天气信息"""
    date: str = Field(..., description="日期 YYYY-MM-DD")
    day_weather: str = Field(default="", description="白天天气")
    night_weather: str = Field(default="", description="夜间天气")
    day_temp: Union[int, str] = Field(default=0, description="白天温度")
    night_temp: Union[int, str] = Field(default=0, description="夜间温度")
    wind_direction: str = Field(default="", description="风向")
    wind_power: str = Field(default="", description="风力")

    @field_validator('day_temp', 'night_temp', mode='before')
    @classmethod
    def parse_temperature(cls, v):
        """解析温度,移除°C等单位"""
        if isinstance(v, str):
            # 移除°C, ℃等单位符号
            v = v.replace('°C', '').replace('℃', '').replace('°', '').strip()
            try:
                return int(v)
            except ValueError:
                return 0
        return v


class Budget(BaseModel):
    """预算信息"""
    total_attractions: int = Field(default=0, description="景点门票总费用")
    total_hotels: int = Field(default=0, description="酒店总费用")
    total_meals: int = Field(default=0, description="餐饮总费用")
    total_transportation: int = Field(default=0, description="交通总费用")
    total: int = Field(default=0, description="总费用")
    intercity_transportation: int = Field(default=0, description="跨城交通费用")
    local_transportation: int = Field(default=0, description="市内交通费用")
    per_person: int = Field(default=0, description="人均预估费用")
    budget_limit: Optional[int] = Field(default=None, description="用户预算上限")
    over_budget: bool = Field(default=False, description="是否超出预算")
    calculation_note: str = Field(default="", description="预算计算口径")


class DataSource(BaseModel):
    """计划所使用的数据来源。"""

    name: str
    category: Literal["poi", "weather", "hotel", "transportation", "generation", "fallback"]
    source_type: Literal["live", "estimated", "generated", "static"]
    note: str = ""


class OptimizationSummary(BaseModel):
    """整份行程的确定性优化收益。"""

    method: str = "nearest_neighbor_haversine"
    total_distance_before_km: float = 0
    total_distance_after_km: float = 0
    saved_distance_km: float = 0
    saved_percent: float = 0
    optimized_days: int = 0
    note: str = "基于直线距离优化访问顺序，不代表道路导航距离。"


class IntercityJourney(BaseModel):
    """占用首日时间轴的跨城交通硬约束。"""

    mode: str
    leg: Literal["outbound", "return"] = "outbound"
    name: str
    departure_date: str = ""
    arrival_date: str = ""
    departure_time: str
    arrival_time: str
    departure_station: str = "出发站"
    arrival_station: str = "到达站"
    check_in_buffer_minutes: int = Field(default=60, ge=0)
    station_transfer_minutes: int = Field(default=45, ge=0)
    arrival_transfer_minutes: int = Field(default=45, ge=0)
    is_estimated: bool = True
    source: str = "estimated"


class RevisionChange(BaseModel):
    """一次修改中的单个变更条目，供前端差异确认展示。"""

    op: str = Field(..., description="操作类型，同 PatchOperationType")
    day_index: Optional[int] = Field(default=None, description="受影响日（从0开始），全计划级为 None")
    title: str = Field(..., description="变更中文标题，如「移动景点」")
    detail: str = Field(default="", description="变更说明（含操作原因）")


class TripPlan(BaseModel):
    """旅行计划"""
    city: str = Field(..., description="目的地城市")
    start_date: str = Field(..., description="开始日期")
    end_date: str = Field(..., description="结束日期")
    days: List[DayPlan] = Field(..., description="每日行程")
    weather_info: List[WeatherInfo] = Field(default_factory=list, description="天气信息")
    overall_suggestions: str = Field(..., description="总体建议")
    budget: Optional[Budget] = Field(default=None, description="预算信息")
    data_quality: Literal["live", "mixed", "fallback"] = Field(default="mixed", description="数据质量等级")
    is_fallback: bool = Field(default=False, description="是否为降级计划")
    warnings: List[str] = Field(default_factory=list, description="数据与可执行性警告")
    data_sources: List[DataSource] = Field(default_factory=list, description="数据来源")
    revision_diff: List[RevisionChange] = Field(default_factory=list, description="最近一次 AI 修改的差异摘要（前端确认用）")
    optimization_summary: Optional[OptimizationSummary] = Field(default=None, description="路线优化摘要")
    planning_iterations: int = Field(default=1, ge=1, le=2, description="Planner执行轮次")
    intercity_journey: Optional[IntercityJourney] = Field(
        default=None, description="首日跨城交通与接驳时间约束"
    )
    return_journey: Optional[IntercityJourney] = Field(
        default=None, description="末日返程交通与接驳时间约束"
    )

    @model_validator(mode="after")
    def migrate_legacy_journey_dates(self):
        """让旧版已保存行程在新增交通日期字段后仍可正常打开。"""
        for journey, fallback_date in (
            (self.intercity_journey, self.start_date),
            (self.return_journey, self.end_date),
        ):
            if journey is None or journey.departure_date:
                continue
            journey.departure_date = fallback_date
            dep = datetime.strptime(journey.departure_time, "%H:%M")
            arr = datetime.strptime(journey.arrival_time, "%H:%M")
            offset = int(arr <= dep)
            journey.arrival_date = (
                datetime.strptime(fallback_date, "%Y-%m-%d").date()
                + timedelta(days=offset)
            ).isoformat()
        return self


class TripPlanResponse(BaseModel):
    """旅行计划响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: Optional[TripPlan] = Field(default=None, description="旅行计划数据")


class POIInfo(BaseModel):
    """POI信息"""
    id: str = Field(..., description="POI ID")
    name: str = Field(..., description="名称")
    type: str = Field(..., description="类型")
    address: str = Field(..., description="地址")
    location: Location = Field(..., description="经纬度坐标")
    tel: Optional[str] = Field(default=None, description="电话")


class POISearchResponse(BaseModel):
    """POI搜索响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: List[POIInfo] = Field(default_factory=list, description="POI列表")


class RouteInfo(BaseModel):
    """路线信息"""
    distance: float = Field(..., description="距离(米)")
    duration: int = Field(..., description="时间(秒)")
    route_type: str = Field(..., description="路线类型")
    description: str = Field(..., description="路线描述")


class RouteResponse(BaseModel):
    """路线规划响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: Optional[RouteInfo] = Field(default=None, description="路线信息")


class TransportationResponse(BaseModel):
    """通勤费用查询响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: List[TransportationOption] = Field(default_factory=list, description="交通方式列表")


class WeatherResponse(BaseModel):
    """天气查询响应"""
    success: bool = Field(..., description="是否成功")
    message: str = Field(default="", description="消息")
    data: List[WeatherInfo] = Field(default_factory=list, description="天气信息")


class AgentExecutionState(BaseModel):
    """单个资料 Agent 的可观测执行状态。"""

    status: Literal["running", "completed", "degraded"]
    message: str = ""
    duration_ms: Optional[int] = Field(default=None, ge=0)


class PlanningMetrics(BaseModel):
    """一次规划任务的轻量性能指标。"""

    queue_wait_ms: int = Field(default=0, ge=0)
    total_duration_ms: Optional[int] = Field(default=None, ge=0)
    agent_duration_ms: Dict[str, int] = Field(default_factory=dict)
    degraded_agents: List[str] = Field(default_factory=list)


class PlanningJobResponse(BaseModel):
    """异步规划任务状态。"""

    job_id: str
    status: Literal["queued", "running", "waiting_input", "completed", "failed"]
    progress: int = Field(default=0, ge=0, le=100)
    stage: str = "queued"
    message: str = ""
    data: Optional[TripPlan] = None
    error: Optional[str] = None
    agents: Dict[str, AgentExecutionState] = Field(default_factory=dict)
    metrics: PlanningMetrics = Field(default_factory=PlanningMetrics)
    # interrupt 载荷：需要用户确认的问题（类型/问题文本/选项），无等待时为 None
    waiting_input: Optional[Dict[str, Any]] = None


class JobResumeRequest(BaseModel):
    """恢复等待中的规划任务：resume_value 对应 waiting_input.options[].value。"""

    resume_value: str = Field(..., description="用户的选择，如 downgrade / keep")


class TripRevisionRequest(BaseModel):
    """使用自然语言修改已有行程。"""

    trip_request: TripRequest
    plan: TripPlan
    instruction: str = Field(..., min_length=2, max_length=1000)


# ============ 个人行程库 ============

class SavedTripCreate(BaseModel):
    """保存一份新生成的行程。"""

    request: TripRequest
    plan: TripPlan
    title: Optional[str] = Field(default=None, max_length=100)


class SavedTripUpdate(BaseModel):
    """保存行程的新版本。"""

    plan: TripPlan
    change_note: str = Field(default="手动调整行程", min_length=1, max_length=500)


class TripVersionSummary(BaseModel):
    version: int
    change_note: str
    created_at: str


class SavedTripSummary(BaseModel):
    id: str
    title: str
    city: str
    start_date: str
    end_date: str
    days: int
    estimated_budget: Optional[int] = None
    data_quality: str
    current_version: int
    created_at: str
    updated_at: str


class SavedTripDetail(SavedTripSummary):
    request: TripRequest
    plan: TripPlan
    versions: List[TripVersionSummary] = Field(default_factory=list)


class UserPreferences(BaseModel):
    """单用户本地版的常用规划偏好。"""

    origin_city: str = Field(default="", max_length=50)
    transportation: Literal[
        "high_speed_train", "train", "plane", "bus", "自驾", "公共交通"
    ] = "high_speed_train"
    accommodation: Literal[
        "经济型酒店", "舒适型酒店", "高档型酒店", "豪华型酒店", "豪华酒店", "民宿"
    ] = "经济型酒店"
    preferences: List[str] = Field(default_factory=list, max_length=10)
    travelers: int = Field(default=1, ge=1, le=20)
    rooms: int = Field(default=1, ge=1, le=10)
    budget_limit: Optional[int] = Field(default=None, ge=0, le=1_000_000)
    optimization_goal: Literal["balanced", "economy", "fastest"] = "balanced"
    pace: Literal["relaxed", "balanced", "intensive"] = "balanced"

    @model_validator(mode="after")
    def validate_rooms(self):
        if self.rooms > self.travelers:
            raise ValueError("房间数不能超过出行人数")
        return self


class TripProgressUpdate(BaseModel):
    completed: bool


class TripProgressItem(BaseModel):
    item_key: str
    completed: bool
    completed_at: Optional[str] = None


class TripProgressResponse(BaseModel):
    trip_id: str
    items: List[TripProgressItem] = Field(default_factory=list)


# ============ 错误响应 ============

class ErrorResponse(BaseModel):
    """错误响应"""
    success: bool = Field(default=False, description="是否成功")
    message: str = Field(..., description="错误消息")
    error_code: Optional[str] = Field(default=None, description="错误代码")

# ============ 候选池契约接线（阶段 2） ============
# schemas 顶部不能直接 import candidate（candidate 依赖本模块的 Location），
# 在模块尾部完成引入并重建 TripRequest 的前向引用 "CandidatePlace"。
from .candidate import CandidatePlace  # noqa: E402

TripRequest.model_rebuild()
