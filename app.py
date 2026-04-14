from flask import Flask, render_template_string, jsonify, request
import requests
import time
import re
import os
import threading
import concurrent.futures
from datetime import datetime
from math import radians, cos, sin, asin, sqrt

# ====================== 核心配置（从环境变量读取，无则用默认值）======================
# 高德地图 API Key 配置
FRONT_MAP_KEY = os.environ.get("FRONT_MAP_KEY", "1b8125f3888fd2d971977520e7baf756")
BACK_WEB_SERVICE_KEY_LIST = [
    os.environ.get("AMAP_KEY_1", "51ad0875d8cbd8af2b694be4d4643476"),
    os.environ.get("AMAP_KEY_2", "519aace43b162d480e247828e9fd3b0c"),
]
CURRENT_KEY_INDEX = 0
CURRENT_KEY_LOCK = threading.Lock()

# 全局缓存（统一缓存架构）
GLOBAL_CACHE = {
    "lock": threading.RLock(),
    "traffic": {"data": None, "time": 0, "lng": 0, "lat": 0},
    "weather": {"data": None, "time": 0},
    "geocode": {"data": None, "time": 0, "lng": 0, "lat": 0},
}

# 缓存过期时间（秒）
TRAFFIC_CACHE_TTL = 60
WEATHER_CACHE_TTL = 300
GEOCODE_CACHE_TTL = 600
MOVE_THRESHOLD = 500  # 移动阈值，米

REFRESH_INTERVAL = int(os.environ.get("REFRESH_INTERVAL", "30"))
DEFAULT_CENTER = [
    float(os.environ.get("DEFAULT_LNG", "118.8919")),
    float(os.environ.get("DEFAULT_LAT", "31.8810")),
]
DEFAULT_DISTRICT = os.environ.get("DEFAULT_DISTRICT", "江宁区")
DEFAULT_CITY = os.environ.get("DEFAULT_CITY", "南京市")

# 线程池（用于并发请求）
EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=6)

# ====================== 工具函数 ======================
def clean_road_name(name):
    if not name:
        return ""
    name = re.sub(r"\(.*?\)|（.*?）", "", name)
    for suffix in ["北向南", "南向北", "东向西", "西向东", "北段", "南段", "东段", "西段", "大道", "路", "街", "巷"]:
        name = name.replace(suffix, "")
    return name.strip()

def get_current_key():
    global CURRENT_KEY_INDEX
    with CURRENT_KEY_LOCK:
        return BACK_WEB_SERVICE_KEY_LIST[CURRENT_KEY_INDEX % len(BACK_WEB_SERVICE_KEY_LIST)]

def switch_key():
    global CURRENT_KEY_INDEX
    with CURRENT_KEY_LOCK:
        CURRENT_KEY_INDEX += 1
        print(f"[Key切换] 当前Key异常，切换到备用Key #{CURRENT_KEY_INDEX % len(BACK_WEB_SERVICE_KEY_LIST) + 1}")

def calculate_distance(lng1, lat1, lng2, lat2):
    lng1, lat1, lng2, lat2 = map(radians, [lng1, lat1, lng2, lat2])
    dlon = lng2 - lng1
    dlat = lat2 - lat1
    a = sin(dlat/2)**2 + cos(lat1) * cos(lat2) * sin(dlon/2)**2
    c = 2 * asin(sqrt(a))
    r = 6371393
    return c * r

def safe_int(value, default=0):
    if value is None:
        return default
    if isinstance(value, str):
        num_match = re.search(r"\d+", value)
        if num_match:
            value = num_match.group()
        else:
            return default
    try:
        return int(value)
    except:
        return default

def safe_float(value, default=0.0):
    if value is None:
        return default
    if isinstance(value, str):
        num_match = re.search(r"\d+\.?\d*", value)
        if num_match:
            value = num_match.group()
        else:
            return default
    try:
        return float(value)
    except:
        return default

def safe_get(d, *keys, default=None):
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k)
            if d is None:
                return default
        else:
            return default
    return d

# ====================== 统一缓存读写工具 ======================
def cache_get(cache_type, lng=None, lat=None):
    with GLOBAL_CACHE["lock"]:
        entry = GLOBAL_CACHE.get(cache_type, {})
        if entry.get("data") is None:
            return None, True
        if lng is not None and lat is not None:
            if cache_type == "traffic" and len(str(entry.get("lng", ""))) > 0:
                dist = calculate_distance(lng, lat, entry.get("lng", 0), entry.get("lat", 0))
                if dist > MOVE_THRESHOLD:
                    return None, True
        age = time.time() - entry.get("time", 0)
        ttl = {"traffic": TRAFFIC_CACHE_TTL, "weather": WEATHER_CACHE_TTL, "geocode": GEOCODE_CACHE_TTL}.get(cache_type, 60)
        return entry.get("data"), (age > ttl)

def cache_set(cache_type, data, lng=None, lat=None):
    with GLOBAL_CACHE["lock"]:
        entry = {"data": data, "time": time.time()}
        if lng is not None and lat is not None:
            entry["lng"] = lng
            entry["lat"] = lat
        GLOBAL_CACHE[cache_type] = entry

# ====================== 高德API接口（带缓存）======================
def get_nanjing_real_weather():
    cached, stale = cache_get("weather")
    if cached is not None and not stale:
        return cached, False

    try:
        url = f"https://restapi.amap.com/v3/weather/weatherInfo?city=320115&key={get_current_key()}"
        res = requests.get(url, timeout=5).json()
        if res.get("status") == "1" and res.get("lives"):
            weather_data = res["lives"][0]
            processed_data = {
                "weather": weather_data.get("weather", "阴"),
                "temperature": str(safe_float(weather_data.get("temperature", 14))),
                "winddirection": weather_data.get("winddirection", "东"),
                "windpower": str(safe_int(weather_data.get("windpower", 3))),
                "visibility": str(safe_float(weather_data.get("visibility", 10)))
            }
            cache_set("weather", processed_data)
            return processed_data, False
    except Exception as e:
        print(f"[天气接口] 调用异常：{e}")

    fallback = {
        "weather": "阴", "temperature": "14",
        "winddirection": "东", "windpower": "3", "visibility": "10"
    }
    if cached is not None:
        return cached, True
    return fallback, True

def get_road_info_by_location(lng, lat):
    cached, stale = cache_get("geocode", lng, lat)
    if cached is not None and not stale:
        return cached, False

    try:
        url = f"https://restapi.amap.com/v3/geocode/regeo?location={lng},{lat}&key={get_current_key()}&extensions=all&radius=500"
        res = requests.get(url, timeout=5).json()
        if res.get("status") == "1":
            regeocode = res["regeocode"]
            addressComponent = regeocode["addressComponent"]

            road_name = "未识别道路"
            road_type = "城市道路"

            if regeocode.get("roads") and len(regeocode["roads"]) > 0:
                nearest_road = regeocode["roads"][0]
                road_name = nearest_road.get("name", road_name)
            elif addressComponent.get("road"):
                road_name = addressComponent.get("road")
            elif addressComponent.get("township"):
                road_name = addressComponent.get("township")

            if addressComponent.get("road_type"):
                road_type = addressComponent.get("road_type")

            processed_data = {
                "road_name": road_name,
                "road_name_clean": clean_road_name(road_name),
                "road_type": road_type,
                "district": addressComponent.get("district", DEFAULT_DISTRICT),
                "city": addressComponent.get("city", DEFAULT_CITY),
                "full_address": regeocode.get("formatted_address", f"{DEFAULT_CITY}{DEFAULT_DISTRICT}")
            }
            cache_set("geocode", processed_data, lng, lat)
            return processed_data, False
        else:
            switch_key()
    except Exception as e:
        print(f"[逆地理接口] 调用异常：{e}")
        switch_key()

    fallback = {
        "road_name": "未识别道路", "road_name_clean": "",
        "road_type": "城市道路", "district": DEFAULT_DISTRICT,
        "city": DEFAULT_CITY, "full_address": f"{DEFAULT_CITY}{DEFAULT_DISTRICT}"
    }
    if cached is not None:
        return cached, True
    return fallback, True

def get_around_road_traffic(lng, lat, radius=1500):
    cached, stale = cache_get("traffic", lng, lat)
    if cached is not None and not stale:
        return cached, False

    road_list = []
    for _ in range(len(BACK_WEB_SERVICE_KEY_LIST)):
        try:
            url = f"https://restapi.amap.com/v3/traffic/status/circle?key={get_current_key()}&extensions=all"
            params = {"location": f"{lng},{lat}", "radius": radius}
            res = requests.get(url, params=params, timeout=8).json()

            if res.get("info") in ["OVER_QUOTA", "INSUFFICIENT_PRIVILEGES", "INVALID_USER_KEY"] or res.get("status") == "0":
                switch_key()
                time.sleep(0.5)
                continue

            if res.get("status") == "1" and res.get("trafficinfo"):
                road_list = res["trafficinfo"]["roads"]
                for road in road_list:
                    road["status"] = safe_int(road.get("status", 0))
                cache_set("traffic", road_list, lng, lat)
                return road_list, False

        except Exception as e:
            print(f"[路况接口] 调用异常：{e}")
            switch_key()
            time.sleep(0.5)
            continue

    if cached is not None:
        return cached, True
    return [], True

def get_around_traffic_event(lng, lat, radius=2000):
    try:
        url = f"https://restapi.amap.com/v3/traffic/event/circle?key={get_current_key()}"
        params = {"location": f"{lng},{lat}", "radius": radius}
        res = requests.get(url, params=params, timeout=5).json()
        if res.get("status") == "1":
            event_list = res.get("events", [])
            return len(event_list), event_list
    except Exception as e:
        print(f"[交通事件接口] 调用异常：{e}")
    return 0, []

def get_around_traffic_light(lng, lat, radius=1000):
    try:
        url = f"https://restapi.amap.com/v3/place/around?key={get_current_key()}"
        params = {
            "location": f"{lng},{lat}", "keywords": "红绿灯",
            "types": "190301", "radius": radius, "offset": 20, "page": 1
        }
        res = requests.get(url, params=params, timeout=5).json()
        if res.get("status") == "1":
            count = safe_int(res.get("count", 0))
            return count
    except Exception as e:
        print(f"[红绿灯接口] 调用异常：{e}")
    return 5

# ====================== 并发数据获取 ======================
def fetch_all_data(lng, lat, location_accuracy):
    """
    并发获取所有第三方数据，返回 (结果字典, 降级信息字典)
    """
    degraded = {}
    results = {}

    def fetch_weather():
        return ("weather", *get_nanjing_real_weather())

    def fetch_road_info():
        return ("road_info", *get_road_info_by_location(lng, lat))

    def fetch_traffic():
        return ("traffic", *get_around_road_traffic(lng, lat))

    def fetch_events():
        return ("events", *get_around_traffic_event(lng, lat))

    def fetch_lights():
        return ("lights", get_around_traffic_light(lng, lat), True)

    futures = [
        EXECUTOR.submit(fetch_weather),
        EXECUTOR.submit(fetch_road_info),
        EXECUTOR.submit(fetch_traffic),
        EXECUTOR.submit(fetch_events),
        EXECUTOR.submit(fetch_lights),
    ]

    for future in concurrent.futures.as_completed(futures):
        try:
            key, data, is_degraded = future.result()
            results[key] = data
            if is_degraded:
                degraded[key] = True
        except Exception as e:
            print(f"[并发任务] 异常：{e}")

    # 补默认值
    defaults = {
        "weather": {"weather": "阴", "temperature": "14", "winddirection": "东", "windpower": "3", "visibility": "10"},
        "road_info": {"road_name": "未识别道路", "road_name_clean": "", "road_type": "城市道路",
                       "district": DEFAULT_DISTRICT, "city": DEFAULT_CITY, "full_address": f"{DEFAULT_CITY}{DEFAULT_DISTRICT}"},
        "traffic": [],
        "events": 0,
        "lights": 5,
    }
    for k, v in defaults.items():
        if k not in results:
            results[k] = v
            degraded[k] = True

    return results, degraded

# ====================== 多元化评分算法（含扣分排序、置信度、驾驶建议）======================
def calculate_multi_dimension_score(base_data):
    """
    返回完整的多维度评分结果，包含：
    - final_score, grade, label
    - dimension_scores（四维评分）
    - penalty_items（扣分项排序，Top3）
    - confidence（置信度）
    - driving_suggestion（驾驶建议）
    """
    dimension_scores = {
        "traffic_flow": {"score": 60, "weight": 45, "detail": {}, "label": "通行效率"},
        "road_infra":   {"score": 70, "weight": 25, "detail": {}, "label": "道路基础设施"},
        "environment":  {"score": 80, "weight": 20, "detail": {}, "label": "环境条件"},
        "location_map": {"score": 70, "weight": 10, "detail": {}, "label": "定位与地图"},
    }
    all_sub_scores = []

    traffic_info = base_data.get("traffic_info", {})
    event_count = base_data.get("event_count", 0)
    status_score_map = {1: 100, 2: 70, 3: 40, 4: 10, 0: 60}
    road_status = safe_int(traffic_info.get("status", 0))
    congestion_score = status_score_map.get(road_status, 60)
    congestion_label = ['未知', '畅通', '缓行', '拥堵', '严重拥堵'][road_status]
    dimension_scores["traffic_flow"]["detail"]["拥堵状态"] = congestion_score
    if congestion_score < 80:
        all_sub_scores.append({"name": "拥堵状态", "score": congestion_score,
                                 "reason": f"当前路况{congestion_label}"})

    speed = safe_float(traffic_info.get("speed", 40))
    if 60 <= speed <= 80:
        speed_score = 100
    elif 40 <= speed < 60 or 80 < speed <= 100:
        speed_score = 80
    elif 20 <= speed < 40 or 100 < speed <= 120:
        speed_score = 50
    else:
        speed_score = 30
    dimension_scores["traffic_flow"]["detail"]["行驶速度"] = speed_score
    if speed_score < 80:
        all_sub_scores.append({"name": "行驶速度", "score": speed_score,
                                 "reason": f"当前车速{speed}km/h不在最优区间"})

    if event_count == 0:
        event_score = 100
    elif event_count <= 2:
        event_score = 60
    elif event_count <= 5:
        event_score = 30
    else:
        event_score = 0
    dimension_scores["traffic_flow"]["detail"]["交通事件"] = event_score
    if event_score < 80:
        all_sub_scores.append({"name": "交通事件", "score": event_score,
                                 "reason": f"周边存在{event_count}起交通事件"})

    traffic_total = round(congestion_score * (25/45) + speed_score * (10/45) + event_score * (10/45), 1)
    dimension_scores["traffic_flow"]["score"] = traffic_total

    road_info = base_data.get("road_info", {})
    traffic_light_count = base_data.get("traffic_light_count", 5)
    road_type = road_info.get("road_type", "城市道路")
    road_name = road_info.get("road_name", "")

    road_type_score_map = {
        "高速公路": 100, "城市快速路": 90, "城市主干道": 80,
        "城市次干道": 60, "城市道路": 70, "县道": 50, "乡道": 30
    }
    road_level_score = road_type_score_map.get(road_type, 60)
    dimension_scores["road_infra"]["detail"]["道路等级"] = road_level_score
    if road_level_score < 70:
        all_sub_scores.append({"name": "道路等级", "score": road_level_score,
                                 "reason": f"当前道路为{road_type}"})

    if traffic_light_count <= 2:
        light_score = 100
    elif traffic_light_count <= 5:
        light_score = 80
    elif traffic_light_count <= 10:
        light_score = 50
    else:
        light_score = 30
    dimension_scores["road_infra"]["detail"]["红绿灯密度"] = light_score
    if light_score < 80:
        all_sub_scores.append({"name": "红绿灯密度", "score": light_score,
                                 "reason": f"周边{traffic_light_count}个红绿灯控流"})

    if road_type in ["高速公路", "城市快速路"]:
        isolate_score = 100
    elif "大道" in road_name or "主干道" in road_type:
        isolate_score = 80
    else:
        isolate_score = 50
    dimension_scores["road_infra"]["detail"]["道路隔离条件"] = isolate_score
    if isolate_score < 80:
        all_sub_scores.append({"name": "道路隔离", "score": isolate_score,
                                 "reason": "道路隔离条件一般"})

    infra_total = round(road_level_score * (10/25) + light_score * (8/25) + isolate_score * (7/25), 1)
    dimension_scores["road_infra"]["score"] = infra_total

    weather_info = base_data.get("weather_info", {})
    is_night = base_data.get("is_night", False)
    weather = weather_info.get("weather", "晴")
    visibility = safe_float(weather_info.get("visibility", 10))
    wind_power = safe_int(weather_info.get("windpower", 3))

    good_weather = ["晴", "多云", "阴"]
    normal_weather = ["小雨", "阵雨", "小雪", "阵雪"]
    bad_weather = ["中雨", "大雨", "暴雨", "中雪", "大雪", "暴雪", "雾", "霾", "沙尘暴"]

    if weather in good_weather:
        weather_score = 100
    elif weather in normal_weather:
        weather_score = 60
    elif weather in bad_weather:
        weather_score = 20
    else:
        weather_score = 80
    dimension_scores["environment"]["detail"]["天气状况"] = weather_score
    if weather_score < 80:
        all_sub_scores.append({"name": "天气状况", "score": weather_score,
                                 "reason": f"当前天气{weather}"})

    if visibility >= 10:
        visibility_score = 100
    elif 5 <= visibility < 10:
        visibility_score = 80
    elif 1 <= visibility < 5:
        visibility_score = 40
    else:
        visibility_score = 10
    dimension_scores["environment"]["detail"]["能见度"] = visibility_score
    if visibility_score < 80:
        all_sub_scores.append({"name": "能见度", "score": visibility_score,
                                 "reason": f"能见度{visibility}km"})

    if wind_power <= 3:
        wind_score = 100
    elif wind_power <= 5:
        wind_score = 70
    elif wind_power <= 6:
        wind_score = 40
    else:
        wind_score = 10
    dimension_scores["environment"]["detail"]["风力等级"] = wind_score
    if wind_score < 80:
        all_sub_scores.append({"name": "风力等级", "score": wind_score,
                                 "reason": f"当前风力{wind_power}级"})

    day_night_score = 100 if not is_night else 60
    dimension_scores["environment"]["detail"]["光照条件"] = day_night_score
    if day_night_score < 80:
        all_sub_scores.append({"name": "光照条件", "score": day_night_score,
                                 "reason": "夜间行驶"})

    env_total = round(weather_score * (8/20) + visibility_score * (5/20) + wind_score * (3/20) + day_night_score * (4/20), 1)
    dimension_scores["environment"]["score"] = env_total

    location_accuracy = safe_float(base_data.get("location_accuracy", 20))
    if location_accuracy <= 5:
        locate_score = 100
    elif location_accuracy <= 10:
        locate_score = 80
    elif location_accuracy <= 20:
        locate_score = 60
    elif location_accuracy <= 50:
        locate_score = 30
    else:
        locate_score = 10
    dimension_scores["location_map"]["detail"]["定位精度"] = locate_score
    if locate_score < 80:
        all_sub_scores.append({"name": "定位精度", "score": locate_score,
                                 "reason": f"定位精度{location_accuracy}米"})

    if road_type in ["高速公路", "城市快速路"]:
        hd_map_score = 100
    elif road_type in ["城市主干道", "城市次干道"]:
        hd_map_score = 70
    else:
        hd_map_score = 30
    dimension_scores["location_map"]["detail"]["高精地图覆盖"] = hd_map_score
    if hd_map_score < 70:
        all_sub_scores.append({"name": "高精地图", "score": hd_map_score,
                                 "reason": f"{road_type}高精地图覆盖一般"})

    locate_total = round(locate_score * (6/10) + hd_map_score * (4/10), 1)
    dimension_scores["location_map"]["score"] = locate_total

    final_score = round(traffic_total * 0.45 + infra_total * 0.25 + env_total * 0.2 + locate_total * 0.1, 1)

    if final_score >= 90:
        grade, label = "S", "非常适宜"
    elif final_score >= 80:
        grade, label = "A", "适宜"
    elif final_score >= 70:
        grade, label = "B", "一般适宜"
    elif final_score >= 60:
        grade, label = "C", "较不适宜"
    else:
        grade, label = "D", "不适宜"

    # 扣分项排序 Top3
    penalty_items = sorted(all_sub_scores, key=lambda x: x["score"])[:3]

    # 置信度计算
    degraded = base_data.get("_degraded", {})
    confidence = 100
    if degraded.get("traffic"):
        confidence -= 20
    if degraded.get("weather"):
        confidence -= 10
    if degraded.get("road_info"):
        confidence -= 15
    if degraded.get("events"):
        confidence -= 5
    if degraded.get("lights"):
        confidence -= 5
    confidence = max(confidence, 30)

    # 驾驶建议
    if final_score >= 85:
        suggestion = "建议保持自动驾驶，系统评估当前环境非常适合智能驾驶。"
    elif final_score >= 70:
        suggestion = "建议保持自动驾驶，但需关注前方路况变化，适时准备接管。"
    elif final_score >= 55:
        suggestion = "建议谨慎接管，当前环境存在一定风险，请密切注意道路状况。"
    elif final_score >= 40:
        suggestion = "建议手动驾驶并降低车速，当前路况复杂，不适合自动驾驶模式。"
    else:
        suggestion = "不适宜自动驾驶，建议立即切换为手动驾驶，注意行车安全。"

    # 优势项
    strengths = []
    for dim_key, dim in dimension_scores.items():
        if dim["score"] >= 85:
            strengths.append(dim["label"])

    return {
        "score": final_score,
        "grade": grade,
        "label": label,
        "dimension_scores": dimension_scores,
        "penalty_items": penalty_items,
        "confidence": confidence,
        "driving_suggestion": suggestion,
        "strengths": strengths,
    }

# ====================== 路由配置 ======================
app = Flask(__name__)

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE,
                                 front_map_key=FRONT_MAP_KEY,
                                 refresh_interval=REFRESH_INTERVAL,
                                 default_center=DEFAULT_CENTER)

@app.route('/api/get_score')
def get_score():
    try:
        lng = safe_float(request.args.get('lng'), DEFAULT_CENTER[0])
        lat = safe_float(request.args.get('lat'), DEFAULT_CENTER[1])
        location_accuracy = safe_float(request.args.get('accuracy', 20.0))

        # 并发获取所有数据
        raw_results, degraded = fetch_all_data(lng, lat, location_accuracy)

        road_info = raw_results.get("road_info", {})
        weather_info = raw_results.get("weather", {})
        traffic_road_list = raw_results.get("traffic", [])
        event_count = raw_results.get("events", 0)
        traffic_light_count = raw_results.get("lights", 5)

        # 取最近的道路交通信息
        traffic_info = traffic_road_list[0] if traffic_road_list else {}

        # 判断是否夜间
        hour = datetime.now().hour
        is_night = hour < 6 or hour > 18

        # 组装基础数据
        base_data = {
            "traffic_info": traffic_info,
            "event_count": event_count,
            "road_info": road_info,
            "weather_info": weather_info,
            "is_night": is_night,
            "traffic_light_count": traffic_light_count,
            "location_accuracy": location_accuracy,
            "_degraded": degraded,
        }

        # 计算多维度评分
        score_result = calculate_multi_dimension_score(base_data)

        # 降级信息汇总
        degraded_items = []
        if degraded.get("traffic"):
            degraded_items.append("路况")
        if degraded.get("weather"):
            degraded_items.append("天气")
        if degraded.get("road_info"):
            degraded_items.append("地理")
        if degraded.get("events"):
            degraded_items.append("事件")
        if degraded.get("lights"):
            degraded_items.append("红绿灯")

        is_degraded = len(degraded_items) > 0
        degraded_message = f"以下数据来自缓存：{', '.join(degraded_items)}。" if is_degraded else None

        result = {
            "score": score_result["score"],
            "grade": score_result["grade"],
            "label": score_result["label"],
            "road_info": road_info,
            "weather_info": weather_info,
            "traffic_info": traffic_info,
            "event_count": event_count,
            "location_accuracy": location_accuracy,
            "dimension_scores": score_result["dimension_scores"],
            "penalty_items": score_result["penalty_items"],
            "confidence": score_result["confidence"],
            "driving_suggestion": score_result["driving_suggestion"],
            "strengths": score_result["strengths"],
            "is_degraded": is_degraded,
            "degraded_message": degraded_message,
            "is_night": is_night,
            "current_hour": hour,
            "traffic_light_count": traffic_light_count,
        }
        return jsonify(result)

    except Exception as e:
        print(f"[评分API] 异常：{e}")
        return jsonify({
            "score": 60, "grade": "C", "label": "数据获取异常",
            "road_info": {"road_name": "未识别道路", "road_type": "城市道路",
                           "district": DEFAULT_DISTRICT, "city": DEFAULT_CITY},
            "weather_info": {"weather": "阴", "temperature": "14"},
            "traffic_info": {}, "event_count": 0,
            "location_accuracy": 50.0, "dimension_scores": {},
            "penalty_items": [], "confidence": 30,
            "driving_suggestion": "数据获取异常，建议手动驾驶。",
            "strengths": [], "is_degraded": True,
            "degraded_message": "所有接口均不可用，显示默认数据。",
            "is_night": datetime.now().hour < 6 or datetime.now().hour > 18,
            "current_hour": datetime.now().hour,
            "traffic_light_count": 5,
        })

# ====================== 前端HTML模板（五区域布局）======================
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>南京市智能驾驶适宜性实时监测系统</title>
  <script type="text/javascript" src="https://webapi.amap.com/maps?v=2.0&key={{ front_map_key }}"></script>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-primary: #0d1117;
      --bg-secondary: #161b22;
      --bg-card: #1c2128;
      --border-color: #30363d;
      --text-primary: #e6edf3;
      --text-secondary: #8b949e;
      --text-muted: #6e7681;
      --accent-green: #3fb950;
      --accent-blue: #58a6ff;
      --accent-yellow: #d29922;
      --accent-red: #f85149;
      --accent-purple: #a371f7;
      --accent-orange: #db6d28;
    }

    * { margin: 0; padding: 0; box-sizing: border-box; }

    body {
      font-family: "Inter", "Microsoft YaHei", -apple-system, sans-serif;
      background: var(--bg-primary);
      color: var(--text-primary);
      height: 100vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }

    /* ===== 顶部区域 ===== */
    .header {
      height: 56px;
      background: var(--bg-secondary);
      border-bottom: 1px solid var(--border-color);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 20px;
      flex-shrink: 0;
    }
    .header-left { display: flex; align-items: center; gap: 12px; }
    .header-logo {
      width: 32px; height: 32px;
      background: linear-gradient(135deg, var(--accent-green), var(--accent-blue));
      border-radius: 8px;
      display: flex; align-items: center; justify-content: center;
      font-weight: 700; font-size: 14px;
    }
    .header-title { font-size: 16px; font-weight: 600; letter-spacing: -0.3px; }
    .header-right { display: flex; align-items: center; gap: 16px; }
    .header-time {
      font-size: 13px; color: var(--text-secondary);
      font-variant-numeric: tabular-nums;
    }
    .header-status { display: flex; align-items: center; gap: 6px; font-size: 13px; }
    .status-dot { width: 8px; height: 8px; border-radius: 50%; }
    .status-dot.ok { background: var(--accent-green); box-shadow: 0 0 6px var(--accent-green); }
    .status-dot.warn { background: var(--accent-yellow); box-shadow: 0 0 6px var(--accent-yellow); }
    .status-dot.err { background: var(--accent-red); box-shadow: 0 0 6px var(--accent-red); }
    .btn {
      padding: 6px 14px; border: 1px solid var(--border-color); border-radius: 6px;
      cursor: pointer; font-size: 13px; font-weight: 500; transition: all 0.2s;
      background: var(--bg-card); color: var(--text-primary);
    }
    .btn:hover { border-color: var(--accent-blue); color: var(--accent-blue); }
    .btn-primary { background: var(--accent-blue); border-color: var(--accent-blue); color: #fff; }
    .btn-primary:hover { background: #79b8ff; border-color: #79b8ff; color: #fff; }

    /* ===== 主体区域（五区域）====== */
    .main-container {
      display: grid;
      grid-template-columns: 280px 1fr 300px;
      grid-template-rows: auto 1fr auto;
      gap: 0;
      flex: 1;
      overflow: hidden;
    }

    /* ===== 左侧区域 ===== */
    .left-panel {
      grid-row: 1 / 3;
      background: var(--bg-secondary);
      border-right: 1px solid var(--border-color);
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 14px;
      overflow-y: auto;
    }

    .card {
      background: var(--bg-card);
      border: 1px solid var(--border-color);
      border-radius: 10px;
      padding: 16px;
      transition: border-color 0.2s;
    }
    .card:hover { border-color: #3b4048; }

    .card-label {
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.8px;
      color: var(--text-muted);
      margin-bottom: 10px;
    }

    .score-hero {
      text-align: center;
      padding: 20px 16px;
    }
    .score-number {
      font-size: 64px;
      font-weight: 700;
      line-height: 1;
      margin-bottom: 6px;
      transition: color 0.3s;
    }
    .score-grade {
      display: inline-block;
      font-size: 20px;
      font-weight: 700;
      padding: 4px 16px;
      border-radius: 20px;
      margin-bottom: 8px;
    }
    .score-label-text {
      font-size: 15px;
      color: var(--text-secondary);
    }
    .confidence-bar {
      margin-top: 12px;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .confidence-track {
      flex: 1;
      height: 4px;
      background: var(--border-color);
      border-radius: 2px;
      overflow: hidden;
    }
    .confidence-fill {
      height: 100%;
      background: var(--accent-blue);
      border-radius: 2px;
      transition: width 0.5s ease;
    }
    .confidence-text {
      font-size: 11px;
      color: var(--text-muted);
      white-space: nowrap;
    }

    .suggestion-card {
      background: linear-gradient(135deg, rgba(63, 185, 80, 0.08), rgba(88, 166, 255, 0.08));
      border-color: rgba(63, 185, 80, 0.3);
    }
    .suggestion-icon { font-size: 18px; margin-bottom: 8px; }
    .suggestion-text {
      font-size: 13px;
      line-height: 1.6;
      color: var(--text-secondary);
    }

    .strengths-list { display: flex; flex-wrap: wrap; gap: 6px; }
    .strength-tag {
      font-size: 12px;
      padding: 4px 10px;
      border-radius: 12px;
      background: rgba(63, 185, 80, 0.12);
      color: var(--accent-green);
      border: 1px solid rgba(63, 185, 80, 0.2);
    }

    /* ===== 中间地图区域 ===== */
    .map-area {
      grid-column: 2;
      grid-row: 1 / 3;
      position: relative;
      background: var(--bg-card);
    }
    #map-container {
      width: 100%; height: 100%;
      cursor: crosshair;
    }
    .map-overlay-info {
      position: absolute;
      top: 12px;
      left: 12px;
      background: rgba(13, 17, 23, 0.85);
      backdrop-filter: blur(8px);
      border: 1px solid var(--border-color);
      border-radius: 8px;
      padding: 10px 14px;
      font-size: 12px;
      color: var(--text-secondary);
      display: none;
    }
    .map-overlay-info.show { display: block; }
    .map-overlay-info span { color: var(--accent-blue); font-weight: 500; }

    /* ===== 右侧区域 ===== */
    .right-panel {
      grid-row: 1 / 3;
      background: var(--bg-secondary);
      border-left: 1px solid var(--border-color);
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 14px;
      overflow-y: auto;
    }

    /* 四维评分 */
    .dimension-item {
      margin-bottom: 14px;
    }
    .dimension-item:last-child { margin-bottom: 0; }
    .dimension-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 6px;
    }
    .dimension-name {
      font-size: 13px;
      font-weight: 500;
      color: var(--text-primary);
    }
    .dimension-score {
      font-size: 13px;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
    }
    .dimension-track {
      height: 6px;
      background: var(--border-color);
      border-radius: 3px;
      overflow: hidden;
    }
    .dimension-fill {
      height: 100%;
      border-radius: 3px;
      transition: width 0.6s ease, background 0.3s;
    }
    .dimension-subs {
      margin-top: 6px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 2px 8px;
    }
    .dim-sub-item { font-size: 11px; color: var(--text-muted); }
    .dim-sub-score { font-size: 11px; font-weight: 600; font-variant-numeric: tabular-nums; }

    /* 扣分项 */
    .penalty-item {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 8px 0;
      border-bottom: 1px solid var(--border-color);
    }
    .penalty-item:last-child { border-bottom: none; }
    .penalty-rank {
      width: 22px; height: 22px;
      border-radius: 50%;
      display: flex; align-items: center; justify-content: center;
      font-size: 11px; font-weight: 700;
      flex-shrink: 0;
    }
    .penalty-info { flex: 1; min-width: 0; }
    .penalty-name { font-size: 13px; font-weight: 500; }
    .penalty-reason { font-size: 11px; color: var(--text-muted); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .penalty-score { font-size: 16px; font-weight: 700; flex-shrink: 0; }

    /* ===== 底部区域 ===== */
    .footer {
      grid-column: 1 / 4;
      background: var(--bg-secondary);
      border-top: 1px solid var(--border-color);
      padding: 10px 20px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      flex-shrink: 0;
    }
    .footer-left { display: flex; align-items: center; gap: 20px; }
    .footer-stat {
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 12px;
      color: var(--text-secondary);
    }
    .footer-stat-val { font-weight: 600; font-variant-numeric: tabular-nums; }
    .footer-right { font-size: 12px; color: var(--text-muted); }
    .degraded-banner {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      padding: 3px 10px;
      border-radius: 12px;
      font-size: 11px;
      font-weight: 500;
      background: rgba(210, 153, 34, 0.12);
      color: var(--accent-yellow);
      border: 1px solid rgba(210, 153, 34, 0.3);
    }

    /* 趋势图 */
    .trend-chart {
      display: flex;
      align-items: flex-end;
      gap: 3px;
      height: 28px;
      flex: 1;
      max-width: 200px;
    }
    .trend-bar {
      flex: 1;
      border-radius: 2px 2px 0 0;
      transition: height 0.4s ease, background 0.3s;
      min-height: 3px;
      cursor: pointer;
      position: relative;
    }
    .trend-bar:hover { filter: brightness(1.3); }
    .trend-bar::after {
      content: attr(data-score);
      position: absolute;
      bottom: 100%;
      left: 50%;
      transform: translateX(-50%);
      font-size: 9px;
      color: var(--text-muted);
      white-space: nowrap;
      opacity: 0;
      transition: opacity 0.2s;
    }
    .trend-bar:hover::after { opacity: 1; }

    /* 等级颜色 */
    .grade-S { color: var(--accent-green); }
    .grade-A { color: #69f0ae; }
    .grade-B { color: var(--accent-yellow); }
    .grade-C { color: var(--accent-orange); }
    .grade-D { color: var(--accent-red); }

    .grade-bg-S { background: var(--accent-green); }
    .grade-bg-A { background: #69f0ae; }
    .grade-bg-B { background: var(--accent-yellow); }
    .grade-bg-C { background: var(--accent-orange); }
    .grade-bg-D { background: var(--accent-red); }

    .dim-traffic_flow { background: var(--accent-blue); }
    .dim-road_infra { background: var(--accent-purple); }
    .dim-environment { background: var(--accent-green); }
    .dim-location_map { background: var(--accent-orange); }

    /* 加载动画 */
    @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
    .loading { animation: pulse 1.5s ease infinite; }

    @media (max-width: 900px) {
      .main-container {
        grid-template-columns: 1fr;
        grid-template-rows: auto auto 300px auto auto;
      }
      .left-panel { grid-row: 1; grid-column: 1; flex-direction: row; flex-wrap: wrap; }
      .right-panel { grid-row: 2; grid-column: 1; }
      .map-area { grid-row: 3; grid-column: 1; }
      .footer { grid-row: 5; grid-column: 1; }
    }
  </style>
</head>
<body>

  <!-- 顶部 -->
  <div class="header">
    <div class="header-left">
      <div class="header-logo">宁</div>
      <div class="header-title">南京市智能驾驶适宜性实时监测系统</div>
    </div>
    <div class="header-right">
      <div class="header-time" id="current-time"></div>
      <div class="header-status">
        <div class="status-dot ok" id="status-dot"></div>
        <span id="location-tip">正在获取定位...</span>
      </div>
      <button class="btn" onclick="refreshScore()">手动刷新</button>
      <button class="btn" onclick="clearMarker()">清除标记</button>
    </div>
  </div>

  <!-- 主体五区域 -->
  <div class="main-container">

    <!-- 左侧：综合评分 + 驾驶建议 -->
    <div class="left-panel">
      <div class="card score-hero">
        <div class="card-label">综合适宜性评分</div>
        <div class="score-number grade-S loading" id="total-score">--</div>
        <div id="grade-badge">
          <span class="score-grade grade-S" id="total-grade">--</span>
        </div>
        <div class="score-label-text" id="score-label">未加载</div>
        <div class="confidence-bar">
          <div class="confidence-track">
            <div class="confidence-fill" id="confidence-fill" style="width:0%"></div>
          </div>
          <span class="confidence-text">置信度 <span id="confidence-val">--</span>%</span>
        </div>
      </div>

      <div class="card suggestion-card" id="suggestion-card">
        <div class="card-label">驾驶建议</div>
        <div class="suggestion-icon" id="suggestion-icon">[ AUTO ]</div>
        <div class="suggestion-text" id="suggestion-text">加载中...</div>
      </div>

      <div class="card" id="strengths-card" style="display:none">
        <div class="card-label">当前优势</div>
        <div class="strengths-list" id="strengths-list"></div>
      </div>
    </div>

    <!-- 中间：地图 -->
    <div class="map-area">
      <div id="map-container"></div>
      <div class="map-overlay-info" id="map-overlay">
        定位精度：<span id="overlay-accuracy">--</span> 米 | 交通事件：<span id="overlay-events">0</span> 起
      </div>
    </div>

    <!-- 右侧：四维评分 + 扣分项 -->
    <div class="right-panel">
      <div class="card">
        <div class="card-label">四维评分拆解</div>

        <div class="dimension-item">
          <div class="dimension-header">
            <span class="dimension-name">通行效率</span>
            <span class="dimension-score" id="dim-traffic-score">--</span>
          </div>
          <div class="dimension-track">
            <div class="dimension-fill dim-traffic_flow" id="dim-traffic-fill" style="width:0%"></div>
          </div>
          <div class="dimension-subs" id="dim-traffic-subs"></div>
        </div>

        <div class="dimension-item">
          <div class="dimension-header">
            <span class="dimension-name">道路基础设施</span>
            <span class="dimension-score" id="dim-road-score">--</span>
          </div>
          <div class="dimension-track">
            <div class="dimension-fill dim-road_infra" id="dim-road-fill" style="width:0%"></div>
          </div>
          <div class="dimension-subs" id="dim-road-subs"></div>
        </div>

        <div class="dimension-item">
          <div class="dimension-header">
            <span class="dimension-name">环境条件</span>
            <span class="dimension-score" id="dim-env-score">--</span>
          </div>
          <div class="dimension-track">
            <div class="dimension-fill dim-environment" id="dim-env-fill" style="width:0%"></div>
          </div>
          <div class="dimension-subs" id="dim-env-subs"></div>
        </div>

        <div class="dimension-item">
          <div class="dimension-header">
            <span class="dimension-name">定位与地图</span>
            <span class="dimension-score" id="dim-locate-score">--</span>
          </div>
          <div class="dimension-track">
            <div class="dimension-fill dim-location_map" id="dim-locate-fill" style="width:0%"></div>
          </div>
          <div class="dimension-subs" id="dim-locate-subs"></div>
        </div>
      </div>

      <div class="card" id="penalty-card">
        <div class="card-label">扣分原因</div>
        <div id="penalty-list">
          <div style="font-size:13px;color:var(--text-muted);text-align:center;padding:10px 0;">
            当前无明显扣分项
          </div>
        </div>
      </div>
    </div>

    <!-- 底部 -->
    <div class="footer">
      <div class="footer-left">
        <div class="footer-stat">
          <span>道路</span>
          <span class="footer-stat-val" id="footer-road">--</span>
        </div>
        <div class="footer-stat">
          <span>天气</span>
          <span class="footer-stat-val" id="footer-weather">--</span>
        </div>
        <div class="footer-stat">
          <span>红绿灯</span>
          <span class="footer-stat-val" id="footer-lights">--</span>
        </div>
        <div class="footer-stat">
          <span>交通事件</span>
          <span class="footer-stat-val" id="footer-events">0</span>
        </div>
        <div id="degraded-info" style="display:none">
          <div class="degraded-banner">
            <span>[!]</span>
            <span id="degraded-msg">使用缓存数据</span>
          </div>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:12px;">
        <div class="trend-chart" id="trend-chart"></div>
        <div class="footer-right" id="footer-right">
          数据刷新：<span>{{ refresh_interval }}</span>秒
        </div>
      </div>
    </div>

  </div>

  <script>
    // ===== 全局变量 =====
    let map = null;
    let currentMarker = null;
    let currentLng = {{ default_center[0] }};
    let currentLat = {{ default_center[1] }};
    let refreshTimer = null;
    let scoreHistory = []; // 最多存20条
    const MAX_HISTORY = 20;

    // ===== 时间更新 =====
    function updateTime() {
      const now = new Date();
      document.getElementById('current-time').textContent =
        now.toLocaleString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit',
          hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    }
    setInterval(updateTime, 1000);
    updateTime();

    // ===== 地图初始化 =====
    function initMap() {
      map = new AMap.Map('map-container', {
        zoom: 15,
        center: [currentLng, currentLat],
        mapStyle: 'amap://styles/dark',
        resizeEnable: true
      });

      map.on('click', function(e) {
        currentLng = e.lnglat.getLng();
        currentLat = e.lnglat.getLat();
        document.getElementById('location-tip').textContent = '已选择新位置';
        addMarker(currentLng, currentLat);
        refreshScore();
      });

      getCurrentLocation();
      initAutoRefresh();
    }

    // ===== 地理定位 =====
    function getCurrentLocation() {
      if (!navigator.geolocation) {
        document.getElementById('location-tip').textContent = '浏览器不支持定位，使用默认位置';
        map.setCenter([currentLng, currentLat]);
        addMarker(currentLng, currentLat);
        refreshScore();
        return;
      }

      document.getElementById('location-tip').textContent = '正在获取您的位置...';
      navigator.geolocation.getCurrentPosition(
        function(pos) {
          currentLng = pos.coords.longitude;
          currentLat = pos.coords.latitude;
          document.getElementById('location-tip').textContent = '定位成功';
          document.getElementById('status-dot').className = 'status-dot ok';
          map.setCenter([currentLng, currentLat]);
          addMarker(currentLng, currentLat);
          refreshScore(pos.coords.accuracy);
        },
        function(err) {
          let msg = '定位失败，使用默认位置';
          if (err.code === 1) msg = '已拒绝定位，使用默认位置';
          else if (err.code === 2) msg = '位置不可用，使用默认位置';
          else if (err.code === 3) msg = '定位超时，使用默认位置';
          document.getElementById('location-tip').textContent = msg;
          document.getElementById('status-dot').className = 'status-dot warn';
          map.setCenter([currentLng, currentLat]);
          addMarker(currentLng, currentLat);
          refreshScore();
        },
        { enableHighAccuracy: true, timeout: 10000, maximumAge: 30000 }
      );
    }

    // ===== 地图标记 =====
    function addMarker(lng, lat) {
      if (currentMarker) map.remove(currentMarker);
      currentMarker = new AMap.Marker({
        position: [lng, lat],
        icon: new AMap.Icon({
          size: new AMap.Size(30, 30),
          image: 'https://webapi.amap.com/theme/v1.3/markers/n/mark_b.png',
          imageSize: new AMap.Size(30, 30)
        }),
        anchor: 'bottom-center'
      });
      currentMarker.setMap(map);
    }

    function clearMarker() {
      if (currentMarker) { map.remove(currentMarker); currentMarker = null; }
      document.getElementById('location-tip').textContent = '标记已清除';
    }

    // ===== 刷新评分 =====
    function refreshScore(accuracy) {
      document.getElementById('status-dot').className = 'status-dot warn';
      let url = `/api/get_score?lng=${currentLng}&lat=${currentLat}`;
      if (accuracy !== undefined) url += `&accuracy=${accuracy}`;

      fetch(url)
        .then(r => { if (!r.ok) throw new Error('接口请求失败'); return r.json(); })
        .then(data => updateDashboard(data))
        .catch(err => {
          console.error('刷新失败：', err);
          document.getElementById('status-dot').className = 'status-dot err';
          document.getElementById('location-tip').textContent = '接口异常';
        });
    }

    // ===== 更新仪表盘 =====
    function updateDashboard(data) {
      document.getElementById('status-dot').className = 'status-dot ok';

      // 综合评分
      const scoreEl = document.getElementById('total-score');
      scoreEl.textContent = data.score;
      scoreEl.className = `score-number grade-${data.grade} loading`;

      document.getElementById('total-grade').textContent = data.grade;
      document.getElementById('total-grade').className = `score-grade grade-${data.grade}`;
      document.getElementById('score-label').textContent = data.label;

      setTimeout(() => scoreEl.classList.remove('loading'), 600);

      // 置信度
      const conf = data.confidence || 50;
      document.getElementById('confidence-fill').style.width = conf + '%';
      document.getElementById('confidence-val').textContent = conf;

      // 驾驶建议
      const sugText = data.driving_suggestion || '数据加载中...';
      document.getElementById('suggestion-text').textContent = sugText;
      let icon = '[AUTO]';
      if (data.score >= 85) icon = '[BEST]';
      else if (data.score >= 70) icon = '[CAUTION]';
      else if (data.score >= 55) icon = '[WARN]';
      else icon = '[STOP]';
      document.getElementById('suggestion-icon').textContent = icon;

      // 优势项
      const strengths = data.strengths || [];
      if (strengths.length > 0) {
        document.getElementById('strengths-card').style.display = 'block';
        document.getElementById('strengths-list').innerHTML =
          strengths.map(s => `<span class="strength-tag">✓ ${s}</span>`).join('');
      } else {
        document.getElementById('strengths-card').style.display = 'none';
      }

      // 四维评分
      const dims = data.dimension_scores || {};
      const dimMap = {
        traffic_flow: { scoreEl: 'dim-traffic-score', fillEl: 'dim-traffic-fill', subsEl: 'dim-traffic-subs' },
        road_infra:   { scoreEl: 'dim-road-score',    fillEl: 'dim-road-fill',    subsEl: 'dim-road-subs' },
        environment:  { scoreEl: 'dim-env-score',     fillEl: 'dim-env-fill',     subsEl: 'dim-env-subs' },
        location_map: { scoreEl: 'dim-locate-score',  fillEl: 'dim-locate-fill',  subsEl: 'dim-locate-subs' },
      };

      const dimColors = {
        traffic_flow: 'var(--accent-blue)',
        road_infra: 'var(--accent-purple)',
        environment: 'var(--accent-green)',
        location_map: 'var(--accent-orange)',
      };

      Object.entries(dimMap).forEach(([key, els]) => {
        const dim = dims[key] || {};
        const score = dim.score || 0;
        document.getElementById(els.scoreEl).textContent = score;
        document.getElementById(els.scoreEl).style.color = dimColors[key];
        const fill = document.getElementById(els.fillEl);
        fill.style.width = score + '%';
        fill.style.background = dimColors[key];

        // 子项
        const subs = dim.detail || {};
        const subsEl = document.getElementById(els.subsEl);
        subsEl.innerHTML = Object.entries(subs).map(([k, v]) => `
          <span class="dim-sub-item">${k}</span>
          <span class="dim-sub-score" style="color:${v >= 80 ? 'var(--accent-green)' : v >= 60 ? 'var(--accent-yellow)' : 'var(--accent-red)'}">${v}</span>
        `).join('');
      });

      // 扣分项
      const penalties = data.penalty_items || [];
      if (penalties.length > 0) {
        document.getElementById('penalty-list').innerHTML = penalties.map((p, i) => {
          const rankColors = ['#f85149', '#db6d28', '#d29922'];
          return `
          <div class="penalty-item">
            <div class="penalty-rank" style="background:${rankColors[i] || rankColors[2]};color:#fff">${i + 1}</div>
            <div class="penalty-info">
              <div class="penalty-name">${p.name}</div>
              <div class="penalty-reason">${p.reason || ''}</div>
            </div>
            <div class="penalty-score" style="color:${rankColors[i] || rankColors[2]}">${p.score}</div>
          </div>`;
        }).join('');
      } else {
        document.getElementById('penalty-list').innerHTML =
          '<div style="font-size:13px;color:var(--text-muted);text-align:center;padding:10px 0;">✓ 当前无明显扣分项，环境优良</div>';
      }

      // 降级信息
      if (data.is_degraded) {
        document.getElementById('degraded-info').style.display = 'block';
        document.getElementById('degraded-msg').textContent = data.degraded_message || '使用缓存数据';
        document.getElementById('status-dot').className = 'status-dot warn';
      } else {
        document.getElementById('degraded-info').style.display = 'none';
      }

      // 地图悬浮信息
      document.getElementById('overlay-accuracy').textContent =
        (data.location_accuracy || 0).toFixed(1);
      document.getElementById('overlay-events').textContent = data.event_count || 0;
      document.getElementById('map-overlay').classList.add('show');

      // 底部信息
      document.getElementById('footer-road').textContent =
        `${data.road_info?.road_type || '--'} ${data.road_info?.road_name || ''}`;
      document.getElementById('footer-weather').textContent =
        `${data.weather_info?.weather || '--'} ${data.weather_info?.temperature || '--'}°C`;
      document.getElementById('footer-lights').textContent =
        (data.traffic_light_count ?? '--') + '个';
      document.getElementById('footer-events').textContent =
        data.event_count ?? 0;

      // 更新趋势图
      scoreHistory.push(data.score);
      if (scoreHistory.length > MAX_HISTORY) scoreHistory.shift();
      renderTrendChart();

      // 定位提示
      if (!data.is_degraded) {
        document.getElementById('location-tip').textContent = '数据已更新';
      }
    }

    // ===== 趋势图 =====
    function renderTrendChart() {
      const container = document.getElementById('trend-chart');
      if (scoreHistory.length === 0) return;

      container.innerHTML = scoreHistory.map(score => {
        let color = 'var(--accent-green)';
        if (score < 60) color = 'var(--accent-red)';
        else if (score < 75) color = 'var(--accent-yellow)';
        else if (score < 85) color = 'var(--accent-blue)';
        const height = Math.max(score / 100 * 28, 3);
        return `<div class="trend-bar" style="height:${height}px;background:${color}" data-score="${score}"></div>`;
      }).join('');
    }

    // ===== 自动刷新 =====
    function initAutoRefresh() {
      refreshScore();
      if (refreshTimer) clearInterval(refreshTimer);
      refreshTimer = setInterval(refreshScore, {{ refresh_interval }} * 1000);
    }

    // ===== 启动 =====
    window.onload = initMap;
  </script>
</body>
</html>
"""

# ====================== 启动应用 ======================
if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
