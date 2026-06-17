from __future__ import annotations

import copy
import re
from typing import Any


STATUS_OPTIONS = ["신건", "유찰", "재매각", "진행", "수의계약", "입찰마감"]

# 「공간정보의 구축 및 관리 등에 관한 법률」상 대표 지목 용어를 중심으로 구성.
LAND_USE_OPTIONS = [
    "전", "답", "과수원", "목장용지", "임야", "광천지", "염전", "대",
    "공장용지", "학교용지", "주차장", "주유소용지", "창고용지", "도로",
    "철도용지", "제방", "하천", "구거", "유지", "양어장", "수도용지",
    "공원", "체육용지", "유원지", "종교용지", "사적지", "묘지", "잡종지",
]

RISK_OPTION_MAP: dict[str, str] = {
    "지분매각": "지분",
    "법정지상권 성립 가능성": "법정지상권",
    "분묘기지권 성립 가능성": "분묘기지권",
    "유치권 신고": "유치권",
    "토지만 매각": "토지만매각",
    "건물만 매각": "건물만매각",
    "선순위 권리": "선순위",
    "대항력 있는 임차인": "대항력",
    "공유자우선매수권": "공유자우선매수",
    "맹지 표시": "맹지",
}

SCORING_PRESETS: dict[str, dict[str, int]] = {
    "균형형": {
        "discount": 30,
        "budget_fit": 12,
        "area_fit": 10,
        "failed_count": 8,
        "usage_preference": 10,
        "market_gap": 20,
        "data_quality": 10,
    },
    "가격할인 중시형": {
        "discount": 40,
        "budget_fit": 15,
        "area_fit": 10,
        "failed_count": 10,
        "usage_preference": 5,
        "market_gap": 10,
        "data_quality": 10,
    },
    "농지·면적 중시형": {
        "discount": 25,
        "budget_fit": 15,
        "area_fit": 20,
        "failed_count": 8,
        "usage_preference": 17,
        "market_gap": 5,
        "data_quality": 10,
    },
}

DEFAULT_PROFILE: dict[str, Any] = {
    "name": "지방 소액 농지·임야",
    "enabled": True,
    "regions": ["충청북도 충주시", "충청북도 제천시", "강원특별자치도 원주시", "충청남도 공주시"],
    "statuses": ["신건", "유찰", "진행"],
    "usages": ["전", "답", "과수원", "임야"],
    "failed_count": {"min": 1, "max": 4},
    "min_price": {"min": 5_000_000, "max": 30_000_000},
    "appraisal_price": {"min": 0, "max": 0},
    "land_area_m2": {"min": 330, "max": 3300},
    "appraisal_discount_percent": {"min": 20, "max": 90},
    "auction_within_days": 90,
    "include_keywords": [],
    "exclude_keywords": [
        "지분", "법정지상권", "분묘기지권", "맹지", "유치권",
        "대항력", "선순위",
    ],
    "scoring_preset": "균형형",
    "scoring": copy.deepcopy(SCORING_PRESETS["균형형"]),
    "preferred_usages": {"전": 10, "답": 10, "과수원": 8, "임야": 4},
}


def ensure_config_defaults(cfg: dict[str, Any] | None) -> dict[str, Any]:
    cfg = copy.deepcopy(cfg or {})
    app = cfg.setdefault("app", {})
    app.setdefault("timezone", "Asia/Seoul")
    app.setdefault("database_path", "data/landwatch.db")
    app.setdefault("report_dir", "reports")
    app.setdefault("only_notify_new_or_changed", True)
    app.setdefault("top_n_per_profile", 20)
    app.setdefault("search_target", "경매")

    source = cfg.setdefault("source", {})
    source.setdefault("type", "court_selenium")
    source.setdefault("label", "대한민국 법원경매정보 Selenium 수집")
    source.setdefault("is_sample", False)
    selenium = source.setdefault("court_selenium", {})
    defaults = {
        "base_url": "https://www.courtauction.go.kr",
        "headless": True,
        "timeout_seconds": 45,
        "warmup_wait_seconds": 6,
        "min_delay_seconds": 3.0,
        "jitter_seconds": 1.5,
        "max_calls_per_run": 10,
        "hard_call_cap": 60,
        "page_size": 20,
        "max_pages": 8,
        "municipality_auto_max_pages": 30,
        "province_fanout_max_municipalities": 8,
        "sale_window_days": 13,
        "adaptive_warmup": True,
        "warmup_settle_seconds": 0.75,
        "legacy_code_fallback_only": True,
        "cache_enabled": True,
        "cache_ttl_minutes": 15,
        "cache_dir": "data/selenium_cache",
        "search_index_enabled": True,
        "search_index_ttl_minutes": 30,
        "fast_mode": True,
        "price_detail_policy": "smart",
        "price_detail_max_per_run": 6,
        "detail_min_delay_seconds": 1.5,
        "detail_jitter_seconds": 0.5,
        "photo_enabled": True,
        "photo_cache_dir": "data/court_photo_cache",
        "photo_cache_days": 30,
        "photo_max_per_run": 6,
        "photo_wait_seconds": 0.45,
        "photo_capture_timeout_seconds": 2.5,
        "photo_missing_cache_days": 7,
        "photo_map_fallback": False,
        "force_land_category": True,
        "bid_type_code": "000331",
        "order_by": "",
        "server_side_region_filter": True,
        "chrome_binary": "",
        "driver_path": "",
        "user_data_dir": "",
        "debug_dir": "data/selenium_debug",
        "save_exchange_debug": False,
        "photo_debug_enabled": False,
    }
    for key, value in defaults.items():
        selenium.setdefault(key, value)
    # GUI에서 선택한 지역은 항상 법원 서버 검색에 반영한다.
    selenium["server_side_region_filter"] = True
    selenium.setdefault("region_code_map", {})

    onbid = source.setdefault("onbid_openapi", {})
    onbid_defaults = {
        "api_generation": "차세대",
        "base_url": "https://apis.data.go.kr/B010003",
        "service_path": "OnbidRlstListSrvc2",
        "list_operation": "getRlstCltrList2",
        "detail_service_path": "OnbidRlstDtlSrvc2",
        "detail_operation": "getRlstDtlInf2",
        "detail_enabled": False,
        "service_key": "${KAMCO_API_KEY}",
        "service_name": "한국자산관리공사_차세대 온비드 부동산 물건목록 조회서비스",
        "key_encoding_mode": "auto",
        "ssl_trust_mode": "system",
        "ca_bundle_path": "",
        "generated_ca_bundle_path": "data/certs/macos-system-ca.pem",
        "property_division_codes": "0007,0010,0005,0002,0003,0006,0008,0011,0013",
        "private_contract_target": "N",
        "force_land_category": False,
        "timeout_seconds": 30,
        "page_size": 100,
        "max_pages": 10,
        "cache_enabled": True,
        "cache_ttl_minutes": 15,
        "cache_dir": "data/onbid_cache_v2",
    }
    # 기존 구 API 설정은 차세대 API로 자동 마이그레이션한다.
    if "openapi.onbid.co.kr" in str(onbid.get("base_url", "")) or str(onbid.get("service_path", "")) == "ThingInfoInquireSvc":
        onbid.update({k: v for k, v in onbid_defaults.items() if k != "service_key"})
    for key, value in onbid_defaults.items():
        onbid.setdefault(key, value)

    profiles = cfg.setdefault("profiles", [])
    if not profiles:
        profiles.append(copy.deepcopy(DEFAULT_PROFILE))
    else:
        cfg["profiles"] = [normalize_profile(p) for p in profiles]

    notifications = cfg.setdefault("notifications", {})
    telegram = notifications.setdefault("telegram", {})
    telegram.setdefault("enabled", False)
    telegram.setdefault("bot_token", "${TELEGRAM_BOT_TOKEN}")
    telegram.setdefault("chat_id", "${TELEGRAM_CHAT_ID}")
    email = notifications.setdefault("email", {})
    email.setdefault("enabled", False)
    email.setdefault("smtp_host", "smtp.gmail.com")
    email.setdefault("smtp_port", 587)
    email.setdefault("username", "${SMTP_USERNAME}")
    email.setdefault("password", "${SMTP_PASSWORD}")
    email.setdefault("from_address", "${SMTP_FROM}")
    email.setdefault("to_addresses", ["${SMTP_TO}"])
    return cfg


def normalize_profile(profile: dict[str, Any] | None) -> dict[str, Any]:
    base = copy.deepcopy(DEFAULT_PROFILE)
    profile = copy.deepcopy(profile or {})
    base.update(profile)
    for key in ("failed_count", "min_price", "appraisal_price", "land_area_m2", "appraisal_discount_percent"):
        merged = copy.deepcopy(DEFAULT_PROFILE.get(key, {}))
        value = profile.get(key)
        if isinstance(value, dict):
            merged.update(value)
        base[key] = merged
    preset = str(base.get("scoring_preset") or detect_scoring_preset(base.get("scoring", {})))
    if preset not in SCORING_PRESETS:
        preset = "직접 설정"
    base["scoring_preset"] = preset
    if not isinstance(base.get("scoring"), dict):
        base["scoring"] = copy.deepcopy(SCORING_PRESETS["균형형"])
    if not isinstance(base.get("preferred_usages"), dict):
        base["preferred_usages"] = {str(x): 10 for x in base.get("usages", [])}
    for list_key in ("regions", "statuses", "usages", "include_keywords", "exclude_keywords"):
        value = base.get(list_key, [])
        if isinstance(value, str):
            value = parse_keywords(value)
        base[list_key] = list(dict.fromkeys(str(x).strip() for x in value if str(x).strip()))
    return base


def new_profile(existing_names: list[str] | None = None) -> dict[str, Any]:
    profile = copy.deepcopy(DEFAULT_PROFILE)
    profile["name"] = unique_profile_name("새 검색조건", existing_names or [])
    profile["regions"] = []
    return profile


def unique_profile_name(base: str, existing_names: list[str]) -> str:
    existing = {str(x).strip() for x in existing_names}
    if base not in existing:
        return base
    number = 2
    while f"{base} {number}" in existing:
        number += 1
    return f"{base} {number}"


def duplicate_profile(profile: dict[str, Any], existing_names: list[str]) -> dict[str, Any]:
    result = normalize_profile(profile)
    result["name"] = unique_profile_name(f"{result['name']} 복사본", existing_names)
    return result


def parse_keywords(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(dict.fromkeys(str(x).strip() for x in value if str(x).strip()))
    return list(dict.fromkeys(x.strip() for x in re.split(r"[,;\n]", str(value)) if x.strip()))


def exclusion_labels(keywords: list[str] | None) -> list[str]:
    normalized = {_compact(x) for x in keywords or []}
    labels = []
    for label, keyword in RISK_OPTION_MAP.items():
        if _compact(keyword) in normalized:
            labels.append(label)
    return labels


def exclusion_keywords(labels: list[str] | None, custom_keywords: str | list[str] | None = None) -> list[str]:
    result = [RISK_OPTION_MAP[label] for label in labels or [] if label in RISK_OPTION_MAP]
    result.extend(parse_keywords(custom_keywords))
    return list(dict.fromkeys(result))


def custom_exclusion_keywords(keywords: list[str] | None) -> list[str]:
    standard = {_compact(x) for x in RISK_OPTION_MAP.values()}
    return [x for x in keywords or [] if _compact(x) not in standard]


def manwon_to_won(value: int | float) -> int:
    return int(round(float(value) * 10_000))


def won_to_manwon(value: int | float | None) -> int:
    return int(round(float(value or 0) / 10_000))


def sqm_to_pyeong(value: int | float | None) -> float:
    return round(float(value or 0) / 3.305785, 1)


def detect_scoring_preset(scoring: dict[str, Any] | None) -> str:
    scoring = scoring or {}
    for name, preset in SCORING_PRESETS.items():
        if all(int(scoring.get(k, -1)) == int(v) for k, v in preset.items()):
            return name
    return "직접 설정"


def profile_summary(profile: dict[str, Any]) -> str:
    p = normalize_profile(profile)
    regions = p.get("regions") or ["전국"]
    prices = p.get("min_price", {})
    areas = p.get("land_area_m2", {})
    return (
        f"지역 {len(regions)}곳 · 물건용도 {len(p.get('usages', []))}개 · "
        f"최저매각가격 {won_to_manwon(prices.get('min')):,}~{won_to_manwon(prices.get('max')):,}만원 · "
        f"토지면적 {float(areas.get('min') or 0):,.0f}~{float(areas.get('max') or 0):,.0f}㎡"
    )


def _compact(value: str) -> str:
    return re.sub(r"[^가-힣A-Za-z0-9]", "", str(value or "")).lower()
