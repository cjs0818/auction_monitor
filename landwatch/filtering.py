from __future__ import annotations

from datetime import date
import re
from typing import Any

from .models import AuctionItem
from .utils import get_path


REGION_ALIASES = {
    "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시",
    "인천": "인천광역시", "광주": "광주광역시", "대전": "대전광역시",
    "울산": "울산광역시", "세종": "세종특별자치시", "경기": "경기도",
    "강원": "강원특별자치도", "강원도": "강원특별자치도",
    "충북": "충청북도", "충남": "충청남도",
    "전북": "전북특별자치도", "전라북도": "전북특별자치도",
    "전남": "전라남도", "경북": "경상북도", "경남": "경상남도",
    "제주": "제주특별자치도",
}


def _normalize_region_text(text: str) -> str:
    tokens = str(text).strip().split()
    if tokens and tokens[0] in REGION_ALIASES:
        tokens[0] = REGION_ALIASES[tokens[0]]
    normalized = " ".join(tokens)
    # 과거/현행 도 명칭이 혼용되는 경우를 같은 값으로 비교한다.
    normalized = normalized.replace("강원도", "강원특별자치도")
    normalized = normalized.replace("전라북도", "전북특별자치도")
    return normalized


def item_matches_region(item: AuctionItem, region: str) -> bool:
    """물건이 선택한 시·도 또는 시·군·구에 속하는지 판정한다.

    법원 검색결과는 전북특별자치도/전라북도, 강원특별자치도/강원도처럼
    신·구 명칭이 섞여 들어올 수 있으므로 같은 명칭으로 정규화한다.
    """
    target = _normalize_region_text(region)
    if not target:
        return True
    hay = _normalize_region_text(
        " ".join(str(x or "") for x in (item.province, item.city_county, item.address))
    )
    return all(token in hay for token in target.split())


def matches_profile(item: AuctionItem, profile: dict[str, Any], today: date | None = None) -> tuple[bool, list[str]]:
    today = today or date.today()
    reasons: list[str] = []

    regions = [str(x).strip() for x in profile.get("regions", []) if str(x).strip()]
    if regions and not any(item_matches_region(item, region) for region in regions):
        reasons.append("지역 불일치")

    statuses = profile.get("statuses", [])
    if statuses and not any(s in item.status for s in statuses):
        reasons.append("진행상태 불일치")

    usages = profile.get("usages", [])
    if usages and not any(u in item.usage for u in usages):
        reasons.append("물건용도 불일치")

    _check_range(item.failed_count, profile.get("failed_count", {}), "유찰횟수", reasons)
    _check_range(item.min_price, profile.get("min_price", {}), "최저매각가격", reasons, zero_max_is_unbounded=True)
    _check_range(item.appraisal_price, profile.get("appraisal_price", {}), "감정평가액", reasons, zero_max_is_unbounded=True)
    _check_range(item.land_area_m2, profile.get("land_area_m2", {}), "토지면적", reasons, zero_max_is_unbounded=True)
    _check_range(item.discount_percent, profile.get("appraisal_discount_percent", {}), "감정평가액 대비 할인율", reasons)

    within_days = profile.get("auction_within_days")
    if within_days and item.auction_date:
        delta = (item.auction_date - today).days
        if delta < 0 or delta > int(within_days):
            reasons.append("매각기일 범위 밖")

    text = " ".join([item.address, item.usage, *item.special_conditions])
    compact_text = _compact(text)
    includes = profile.get("include_keywords", [])
    if includes and not any(_compact(k) in compact_text for k in includes if _compact(k)):
        reasons.append("포함 키워드 없음")

    excludes = profile.get("exclude_keywords", [])
    found = [k for k in excludes if _compact(k) and _compact(k) in compact_text]
    if found:
        reasons.append("제외 키워드: " + ", ".join(found))

    return not reasons, reasons


def _check_range(
    value: float,
    spec: dict[str, Any],
    label: str,
    reasons: list[str],
    *,
    zero_max_is_unbounded: bool = False,
) -> None:
    if not spec:
        return
    min_v = get_path(spec, "min", None)
    max_v = get_path(spec, "max", None)
    if min_v not in (None, "") and value < float(min_v):
        reasons.append(f"{label} 최소 미달")
    if max_v not in (None, "") and not (zero_max_is_unbounded and float(max_v) == 0) and value > float(max_v):
        reasons.append(f"{label} 최대 초과")


def _compact(value: str) -> str:
    return re.sub(r"[^가-힣A-Za-z0-9]", "", str(value or "")).lower()
