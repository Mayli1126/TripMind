"""模型响应解析：只做 JSON 清洗、字段归一和结构校验，不调用模型或地图。"""

import json
import logging

from ..models.schemas import TripPlan, TripRequest

logger = logging.getLogger(__name__)


class PlanResponseError(ValueError):
    """响应不可用，由上层决定修复、重试或生成参考计划。"""


class PlanResponseParser:
    def _extract_json(self, response: str) -> str:
        """
        从LLM响应中提取JSON字符串,支持多种格式

        优先级:
        1. ```json ... ``` 代码块(支持多个,取最长的)
        2. ``` ... ``` 代码块
        3. 裸JSON对象 { ... }
        """
        import re

        # 策略1: 提取所有 ```json ... ``` 代码块,取最长的(避免截断)
        json_blocks = re.findall(r'```json\s*\n?(.*?)```', response, re.DOTALL)
        if json_blocks:
            return max(json_blocks, key=len).strip()

        # 策略2: 提取所有 ``` ... ``` 代码块
        code_blocks = re.findall(r'```\s*\n?(.*?)```', response, re.DOTALL)
        if code_blocks:
            return max(code_blocks, key=len).strip()

        # 策略3: 裸JSON - 使用括号匹配找到完整的JSON对象
        brace_count = 0
        start = -1
        for i, char in enumerate(response):
            if char == '{':
                if brace_count == 0:
                    start = i
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0 and start != -1:
                    return response[start:i+1]

        raise ValueError("响应中未找到有效的JSON数据")

    def _clean_json_string(self, json_str: str) -> str:
        """
        清理JSON字符串中的常见LLM输出错误

        处理:
        - 尾部逗号: [1, 2,] -> [1, 2]
        - 单引号: {'key': 'value'} -> {"key": "value"}
        - JavaScript注释: // ... 和 /* ... */
        - Markdown加粗符号: **text**
        - 多余的换行和空格
        - 字符串中的转义问题
        - 省略号或不完整值
        """
        import re

        # 移除JavaScript风格注释：字符串感知版——只剥字符串字面量之外的注释，
        # 否则值里的 URL（https://…）会被削成 "https:"（踩过）
        json_str = self._strip_comments_outside_strings(json_str)

        # 移除Markdown加粗符号 **
        json_str = re.sub(r'\*\*', '', json_str)

        # 移除对象/数组末尾的逗号 (尾部逗号)
        json_str = re.sub(r',\s*([}\]])', r'\1', json_str)

        # 将单引号替换为双引号(仅在键名和简单值中,避免影响字符串内容)
        # 注意: 这是一个简化处理,复杂情况可能失效
        json_str = re.sub(r"'([^']*)'\s*:", r'"\1":', json_str)  # 键名
        json_str = re.sub(r":\s*'([^']*)'", r': "\1"', json_str)  # 字符串值

        return json_str

    @staticmethod
    def _strip_comments_outside_strings(json_str: str) -> str:
        """剥除 JSON 字符串字面量之外的 JS 注释（// 行注释与 /* */ 块注释）。

        逐字符扫描并跟踪是否处于字符串内（处理 \\" 转义），
        保证 {"url": "https://a.com"} 中的 // 不被误删。
        字符串外：看到 " 就进字符串；看到 // 或 /* 就删注释；其他照抄
        字符串内：看到 \" 知道是转义别上当；看到裸 " 就出字符串；其他照抄
        """
        out: list[str] = []
        i, n = 0, len(json_str)
        in_string = False
        while i < n:
            ch = json_str[i]
            if in_string:
                out.append(ch)
                if ch == "\\":  # 转义：连同下一个字符一起原样保留
                    if i + 1 < n:
                        out.append(json_str[i + 1])
                        i += 1
                elif ch == '"':
                    in_string = False
                i += 1
                continue
            if ch == '"':
                in_string = True
                out.append(ch)
                i += 1
                continue
            # 字符串外：识别注释起点
            if ch == "/" and i + 1 < n:
                nxt = json_str[i + 1]
                if nxt == "/":  # 行注释：跳到行尾
                    while i < n and json_str[i] != "\n":
                        i += 1
                    continue
                if nxt == "*":  # 块注释：跳到 */
                    end = json_str.find("*/", i + 2)
                    i = n if end == -1 else end + 2
                    continue
            out.append(ch)
            i += 1
        return "".join(out)

    def _normalize_data(self, data: dict, request: TripRequest) -> dict:
        """
        清洗并规范化LLM返回的字典数据,使其符合TripPlan Pydantic模型

        处理:
        - 补全缺失的必填字段
        - 类型转换(如ticket_price "60元" -> 60)
        - location字符串 "116.3,39.9" -> {"longitude": 116.3, "latitude": 39.9}
        """
        import re
        from datetime import datetime, timedelta

        # 确保顶层必填字段存在
        data.setdefault('city', request.city)
        data.setdefault('start_date', request.start_date)
        data.setdefault('end_date', request.end_date)
        data.setdefault('overall_suggestions', f"这是为您规划的{request.city}{request.travel_days}日游行程。")

        # 规范化days
        days = data.get('days', [])
        if not isinstance(days, list):
            days = []
        for i, day in enumerate(days):
            if not isinstance(day, dict):
                continue
            expected_date = (
                datetime.strptime(request.start_date, "%Y-%m-%d") + timedelta(days=i)
            ).strftime("%Y-%m-%d")
            day.setdefault('date', expected_date)
            day.setdefault('day_index', i)
            day.setdefault('description', f"第{i+1}天行程")
            day.setdefault('transportation', request.transportation)
            day.setdefault('accommodation', request.accommodation)
            day.setdefault('attractions', [])
            day.setdefault('meals', [])
            day.pop('schedule', None)

            # 规范化attractions中的字段
            for attr in day.get('attractions', []):
                if not isinstance(attr, dict):
                    continue
                attr.setdefault('name', '未知景点')
                attr.setdefault('address', request.city)
                attr.setdefault('visit_duration', 120)
                attr.setdefault('description', '')
                attr.setdefault('category', '景点')
                attr.setdefault('ticket_price', 0)

                # 处理location: 字符串 -> 字典
                loc = attr.get('location')
                if isinstance(loc, str):
                    # 尝试解析 "116.397128, 39.916527" 格式
                    parts = re.findall(r'[\d.]+', loc)
                    if len(parts) >= 2:
                        attr['location'] = {
                            'longitude': float(parts[0]),
                            'latitude': float(parts[1])
                        }
                    else:
                        attr['location'] = {'longitude': 116.397128, 'latitude': 39.916527}
                elif isinstance(loc, dict):
                    # 确保键名正确
                    if 'lng' in loc and 'longitude' not in loc:
                        loc['longitude'] = float(loc.pop('lng'))
                    if 'lat' in loc and 'latitude' not in loc:
                        loc['latitude'] = float(loc.pop('lat'))
                    # 确保经纬度是数字
                    if 'longitude' in loc and isinstance(loc['longitude'], str):
                        nums = re.findall(r'[\d.]+', loc['longitude'])
                        loc['longitude'] = float(nums[0]) if nums else 116.397128
                    if 'latitude' in loc and isinstance(loc['latitude'], str):
                        nums = re.findall(r'[\d.]+', loc['latitude'])
                        loc['latitude'] = float(nums[0]) if nums else 39.916527
                elif loc is None or not isinstance(loc, dict):
                    attr['location'] = {'longitude': 116.397128, 'latitude': 39.916527}

                # 处理ticket_price: 字符串 "60元" -> int
                price = attr.get('ticket_price')
                if isinstance(price, str):
                    nums = re.findall(r'\d+', price)
                    attr['ticket_price'] = int(nums[0]) if nums else 0
                elif price is None:
                    attr['ticket_price'] = 0
                elif isinstance(price, (int, float)):
                    attr['ticket_price'] = int(price)

                # 处理visit_duration: 字符串/None -> int
                duration = attr.get('visit_duration')
                if isinstance(duration, str):
                    nums = re.findall(r'\d+', duration)
                    attr['visit_duration'] = max(15, int(nums[0])) if nums else 120
                elif duration is None or not isinstance(duration, (int, float)):
                    attr['visit_duration'] = 120
                else:
                    attr['visit_duration'] = max(15, min(1440, int(duration)))

                # rating 容错：LLM 常回 "4.7分"/"4.5/5"/"暂无"；非法或超 0-5 置 None，
                # 避免单字段校验失败把整份计划打成 fallback 模板
                rating = attr.get('rating')
                if isinstance(rating, str):
                    nums = re.findall(r'\d+(?:\.\d+)?', rating)
                    attr['rating'] = min(5.0, float(nums[0])) if nums else None
                elif isinstance(rating, (int, float)) and not 0 <= rating <= 5:
                    attr['rating'] = None
                elif rating is not None and not isinstance(rating, (int, float)):
                    attr['rating'] = None

                # photos 容错：要求字符串列表；非列表（字符串/null/对象）一律丢弃
                photos = attr.get('photos')
                if not isinstance(photos, list) or any(not isinstance(p, str) for p in photos):
                    attr['photos'] = []

                # closed_weekdays 容错：要求 int 列表（周一为 0）；LLM 回星期文字时丢弃
                closed = attr.get('closed_weekdays')
                if not isinstance(closed, list) or any(
                    not isinstance(c, int) or not 0 <= c <= 6 for c in closed
                ):
                    attr['closed_weekdays'] = []

                # 门票非负兜底
                if isinstance(attr.get('ticket_price'), (int, float)) and attr['ticket_price'] < 0:
                    attr['ticket_price'] = 0

            # 规范化meals
            for meal in day.get('meals', []):
                if not isinstance(meal, dict):
                    continue
                meal.setdefault('type', 'snack')
                meal.setdefault('name', '餐饮')
                meal.setdefault('description', '')
                meal.setdefault('estimated_cost', 0)
                cost = meal.get('estimated_cost')
                if isinstance(cost, str):
                    nums = re.findall(r'\d+', cost)
                    meal['estimated_cost'] = int(nums[0]) if nums else 0
                elif cost is None or not isinstance(cost, (int, float)):
                    meal['estimated_cost'] = 0
                else:
                    meal['estimated_cost'] = int(cost)

            # 规范化hotel
            hotel = day.get('hotel')
            if isinstance(hotel, dict):
                hotel.setdefault('name', '推荐酒店')
                hotel.setdefault('address', request.city)
                hotel.setdefault('price_range', '')
                hotel.setdefault('rating', '')
                hotel.setdefault('distance', '')
                hotel.setdefault('type', '')
                hotel.setdefault('estimated_cost', 0)
                h_loc = hotel.get('location')
                if isinstance(h_loc, str):
                    parts = re.findall(r'[\d.]+', h_loc)
                    if len(parts) >= 2:
                        hotel['location'] = {
                            'longitude': float(parts[0]),
                            'latitude': float(parts[1])
                        }
                elif isinstance(h_loc, dict):
                    if 'lng' in h_loc and 'longitude' not in h_loc:
                        h_loc['longitude'] = float(h_loc.pop('lng'))
                    if 'lat' in h_loc and 'latitude' not in h_loc:
                        h_loc['latitude'] = float(h_loc.pop('lat'))
                hotel_cost = hotel.get('estimated_cost')
                if isinstance(hotel_cost, str):
                    nums = re.findall(r'\d+', hotel_cost)
                    hotel['estimated_cost'] = int(nums[0]) if nums else 0
                elif hotel_cost is None or not isinstance(hotel_cost, (int, float)):
                    hotel['estimated_cost'] = 0
                else:
                    hotel['estimated_cost'] = int(hotel_cost)

        data['days'] = days

        # 规范化weather_info
        weather = data.get('weather_info', [])
        if not isinstance(weather, list):
            weather = []
        for i, w in enumerate(weather):
            if not isinstance(w, dict):
                continue
            expected_date = (
                datetime.strptime(request.start_date, "%Y-%m-%d") + timedelta(days=i)
            ).strftime("%Y-%m-%d")
            w.setdefault('date', expected_date)
            w.setdefault('day_weather', '晴')
            w.setdefault('night_weather', '多云')
            w.setdefault('day_temp', 25)
            w.setdefault('night_temp', 15)
            w.setdefault('wind_direction', '南风')
            w.setdefault('wind_power', '1-3级')
            # 确保温度是数字
            for temp_key in ('day_temp', 'night_temp'):
                tv = w.get(temp_key)
                if isinstance(tv, str):
                    nums = re.findall(r'-?\d+', tv)
                    w[temp_key] = int(nums[0]) if nums else 20
                elif tv is None or not isinstance(tv, (int, float)):
                    w[temp_key] = 20
                else:
                    w[temp_key] = int(tv)

        data['weather_info'] = weather

        # 汇总预算由确定性质量服务重新计算，丢弃模型中常见的“¥200”等非结构化汇总值。
        data.pop('budget', None)

        return data

    def parse(self, response: str, request: TripRequest) -> TripPlan:
        """
        解析Agent响应

        采用三层容错策略:
        1. 提取JSON字符串(支持代码块和裸JSON)
        2. 清理常见LLM输出错误(尾部逗号、注释等)
        3. 规范化数据(类型转换、字段补全)

        若全部失败则使用增强版备份计划

        Args:
            response: Agent响应文本
            request: 原始请求

        Returns:
            旅行计划
        """
        last_error = None

        # 尝试1: 原始提取 + 清理 + 解析
        for attempt, do_clean in enumerate([False, True]):
            try:
                json_str = self._extract_json(response)
                if do_clean:
                    json_str = self._clean_json_string(json_str)

                data = json.loads(json_str)
                data = self._normalize_data(data, request)
                trip_plan = TripPlan(**data)

                if do_clean:
                    logger.debug("模型响应经 JSON 清理后解析成功")
                else:
                    logger.debug("模型响应解析成功")
                return trip_plan

            except Exception as e:
                last_error = e
                if not do_clean:
                    logger.debug("首次解析失败，清理后重试：%s", type(e).__name__)
                continue

        # 尝试2: 使用正则提取最外层JSON对象(更暴力)
        try:
            import re
            # 尝试匹配整个最大的JSON对象
            match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', response, re.DOTALL)
            if match:
                json_str = self._clean_json_string(match.group())
                data = json.loads(json_str)
                data = self._normalize_data(data, request)
                trip_plan = TripPlan(**data)
                logger.debug("使用兼容解析策略成功")
                return trip_plan
        except Exception as e:
            last_error = e

        # 只报告解析错误；是否修复或降级由调用方决定。
        logger.warning("模型响应结构不可用：%s", type(last_error).__name__)
        appears_truncated = response.count("{") > response.count("}") or response.count("[") > response.count("]")
        if appears_truncated:
            reason = "模型输出疑似被长度限制截断，JSON未完整闭合"
        else:
            reason = f"模型输出无法通过结构校验：{type(last_error).__name__}"
        raise PlanResponseError(reason) from last_error



plan_response_parser = PlanResponseParser()
