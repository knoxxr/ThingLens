"""의미 사전 (계획서 §5.0 (2)). 사용자 확정 결과(semantic_rule)가 이 사전보다 우선한다.

각 의미(semantic)
  aliases : 키 이름 토큰과 일치하면 강한 근거 (한글 포함)
  weak    : 한 글자 등 약한 근거 (t, p, v …)
  units   : 단위 계열 (키 접미사 단위가 이 계열이면 가산, 다른 계열이면 감산)
  types   : 허용 데이터 종류
  typical : 흔한 값 범위 (샘플 5~95% 구간이 안에 들면 가산)
  physical: 물리적으로 가능한 범위 (벗어나면 의미 감산 + 품질 '범위 이탈')
"""

from __future__ import annotations

CONT, COUNTER, BINARY, CATEG = "continuous", "counter", "binary", "categorical"

SEMANTICS: dict[str, dict] = {
    "temperature": dict(ko="온도", aliases=["temperature", "temp", "tmp", "온도"], weak=["t"], units="temperature",
                        default_unit="°C", types=[CONT], typical=(-40, 125), physical=(-90, 1800)),
    "humidity": dict(ko="습도", aliases=["humidity", "humid", "hum", "rh", "습도"], weak=["h"], units="percent",
                     default_unit="%", types=[CONT], typical=(0, 100), physical=(0, 100)),
    "pressure": dict(ko="압력", aliases=["pressure", "press", "pres", "prs", "압력"], weak=["p"], units="pressure",
                     default_unit="bar", types=[CONT], typical=None, physical=None),
    "voltage": dict(ko="전압", aliases=["voltage", "volt", "volts", "전압"], weak=["v", "u"], units="voltage",
                    default_unit="V", types=[CONT], typical=None, physical=None),
    "current": dict(ko="전류", aliases=["current", "curr", "amp", "amps", "ampere", "전류"], weak=["i"],
                    units="current", default_unit="A", types=[CONT], typical=(0, 5000), physical=None),
    "power": dict(ko="전력", aliases=["power", "pwr", "watt", "watts", "전력"], weak=[], units="power",
                  default_unit="kW", types=[CONT], typical=None, physical=None),
    "energy": dict(ko="전력량", aliases=["energy", "kwh", "wh", "consumption", "전력량", "적산"], weak=[],
                   units="energy", default_unit="kWh", types=[COUNTER, CONT], typical=None, physical=(0, None)),
    "frequency": dict(ko="주파수", aliases=["frequency", "freq", "hz", "주파수"], weak=[], units="frequency",
                      default_unit="Hz", types=[CONT], typical=(0, 1000), physical=(0, None)),
    "battery": dict(ko="배터리", aliases=["battery", "batt", "bat", "배터리"], weak=[], units="percent",
                    default_unit="%", types=[CONT], typical=(0, 100), physical=(0, None)),
    "signal": dict(ko="신호 세기", aliases=["rssi", "rsrp", "rsrq", "signal", "dbm", "신호"], weak=[], units="dbm",
                   default_unit="dBm", types=[CONT], typical=(-140, 0), physical=(-200, 20)),
    "snr": dict(ko="신호대잡음비", aliases=["snr", "sinr"], weak=[], units="db", default_unit="dB", types=[CONT],
                typical=(-30, 50), physical=None),
    "co2": dict(ko="이산화탄소", aliases=["co2", "이산화탄소"], weak=[], units="ppm", default_unit="ppm", types=[CONT],
                typical=(300, 5000), physical=(0, 100000)),
    "pm25": dict(ko="초미세먼지", aliases=["pm25", "pm2_5", "초미세먼지"], weak=[], units="density",
                 default_unit="µg/m³", types=[CONT], typical=(0, 500), physical=(0, 5000)),
    "pm10": dict(ko="미세먼지", aliases=["pm10", "미세먼지"], weak=[], units="density", default_unit="µg/m³",
                 types=[CONT], typical=(0, 600), physical=(0, 5000)),
    "voc": dict(ko="휘발성유기화합물", aliases=["voc", "tvoc"], weak=[], units="ppm", default_unit="ppb",
                types=[CONT], typical=None, physical=(0, None)),
    "flow": dict(ko="유량", aliases=["flow", "flowrate", "유량"], weak=["q"], units="flow", default_unit="m³/h",
                 types=[CONT], typical=None, physical=None),
    "level": dict(ko="수위·레벨", aliases=["level", "lvl", "수위", "레벨"], weak=[], units="length",
                  default_unit="%", types=[CONT], typical=None, physical=None),
    "speed": dict(ko="속도·회전수", aliases=["rpm", "speed", "spd", "회전수", "속도"], weak=[], units="speed",
                  default_unit="rpm", types=[CONT], typical=None, physical=(0, None)),
    "vibration": dict(ko="진동", aliases=["vibration", "vib", "accel", "acceleration", "진동"], weak=[],
                      units="accel", default_unit="mm/s", types=[CONT], typical=None, physical=None),
    "light": dict(ko="조도", aliases=["lux", "light", "illuminance", "조도"], weak=[], units="lux",
                  default_unit="lx", types=[CONT], typical=(0, 100000), physical=(0, 200000)),
    "noise": dict(ko="소음", aliases=["noise", "sound", "소음"], weak=[], units="db", default_unit="dB",
                  types=[CONT], typical=(0, 140), physical=(0, 200)),
    "latitude": dict(ko="위도", aliases=["latitude", "lat", "위도"], weak=[], units=None, default_unit="°",
                     types=[CONT], typical=(-90, 90), physical=(-90, 90)),
    "longitude": dict(ko="경도", aliases=["longitude", "lon", "lng", "long", "경도"], weak=[], units=None,
                      default_unit="°", types=[CONT], typical=(-180, 180), physical=(-180, 180)),
    "status": dict(ko="상태", aliases=["status", "state", "mode", "상태", "모드"], weak=[], units=None,
                   default_unit=None, types=[BINARY, CATEG], typical=None, physical=None),
    "alarm": dict(ko="알람·고장", aliases=["alarm", "alert", "fault", "error", "err", "경보", "알람", "고장"], weak=[],
                  units=None, default_unit=None, types=[BINARY, CATEG], typical=None, physical=None),
    "door": dict(ko="개폐", aliases=["door", "open", "contact", "개폐", "문"], weak=[], units=None,
                 default_unit=None, types=[BINARY], typical=None, physical=None),
    "occupancy": dict(ko="재실·감지", aliases=["occupancy", "presence", "motion", "pir", "재실"], weak=[], units=None,
                      default_unit=None, types=[BINARY], typical=None, physical=None),
    "count": dict(ko="계수", aliases=["count", "cnt", "counter", "total", "카운트"], weak=[], units=None,
                  default_unit=None, types=[COUNTER, CONT, CATEG], typical=None, physical=(0, None)),
    "runtime": dict(ko="가동 시간", aliases=["runtime", "uptime", "operating", "가동시간"], weak=[], units="time",
                    default_unit="h", types=[COUNTER, CONT], typical=None, physical=(0, None)),
    "weight": dict(ko="무게", aliases=["weight", "wt", "mass", "무게", "중량"], weak=[], units="mass",
                   default_unit="kg", types=[CONT], typical=None, physical=None),
    "timestamp": dict(ko="시각", aliases=["timestamp", "ts", "time", "datetime", "lastseen", "시각", "시간"], weak=[],
                      units=None, default_unit=None, types=["timestamp"], typical=None, physical=None),
    "setpoint": dict(ko="설정값", aliases=["setpoint", "target", "설정값"], weak=["sp"], units=None,
                     default_unit=None, types=[CONT], typical=None, physical=None),
}

UNKNOWN = "unknown"

# 키 접미사 단위 → (단위 계열, 표시 단위)
UNIT_TOKENS: dict[str, tuple[str, str]] = {
    "c": ("temperature", "°C"), "degc": ("temperature", "°C"), "celsius": ("temperature", "°C"),
    "f": ("temperature", "°F"), "degf": ("temperature", "°F"),
    "pct": ("percent", "%"), "percent": ("percent", "%"), "perc": ("percent", "%"),
    "v": ("voltage", "V"), "mv": ("voltage", "mV"), "kv": ("voltage", "kV"),
    "a": ("current", "A"), "ma": ("current", "mA"),
    "w": ("power", "W"), "kw": ("power", "kW"), "mw": ("power", "MW"),
    "wh": ("energy", "Wh"), "kwh": ("energy", "kWh"), "mwh": ("energy", "MWh"),
    "hz": ("frequency", "Hz"),
    "pa": ("pressure", "Pa"), "kpa": ("pressure", "kPa"), "hpa": ("pressure", "hPa"), "mpa": ("pressure", "MPa"),
    "bar": ("pressure", "bar"), "mbar": ("pressure", "mbar"), "psi": ("pressure", "psi"),
    "ppm": ("ppm", "ppm"), "ppb": ("ppm", "ppb"),
    "lux": ("lux", "lx"), "lx": ("lux", "lx"),
    "db": ("db", "dB"), "dbm": ("dbm", "dBm"),
    "rpm": ("speed", "rpm"),
    "kg": ("mass", "kg"), "g": ("mass", "g"),
    "lpm": ("flow", "L/min"), "m3h": ("flow", "m³/h"),
    "mm": ("length", "mm"), "cm": ("length", "cm"), "m": ("length", "m"),
    "sec": ("time", "s"), "min": ("time", "min"), "hr": ("time", "h"), "hours": ("time", "h"),
}

# 속성 역할 (계획서 §5.0 (3))
ATTRIBUTE_ROLES: dict[str, dict] = {
    "location": dict(ko="위치", aliases=["location", "loc", "site", "zone", "area", "floor", "building", "room",
                                         "address", "addr", "latitude", "lat", "longitude", "lng", "lon",
                                         "위치", "설치위치", "구역", "층"]),
    "identity": dict(ko="식별", aliases=["serial", "sn", "serialnumber", "imei", "imsi", "iccid", "mac", "ip",
                                         "model", "manufacturer", "vendor", "deviceid", "uid", "uuid", "시리얼", "모델"]),
    "firmware": dict(ko="버전", aliases=["fw", "firmware", "version", "ver", "sw", "software", "펌웨어", "버전"]),
    "config": dict(ko="설정", aliases=["interval", "period", "threshold", "setpoint", "config", "cfg", "enable",
                                       "enabled", "limit", "timeout", "rate", "설정", "임계"]),
    "status": dict(ko="상태", aliases=["active", "status", "state", "online", "lastseen", "connected", "상태"]),
    "metadata": dict(ko="관리 정보", aliases=["installdate", "installed", "date", "owner", "manager", "description",
                                             "desc", "note", "memo", "설치일", "담당"]),
}

# TB가 자동으로 관리하는 동적 속성
TB_DYNAMIC_ATTRIBUTES = {"active", "lastActivityTime", "lastConnectTime", "lastDisconnectTime",
                         "inactivityAlarmTime"}

SCOPE_DEFAULT_ROLE = {"SHARED_SCOPE": "config", "CLIENT_SCOPE": "identity", "SERVER_SCOPE": "metadata"}
