from __future__ import annotations

import re
from typing import Any

from .models import AuctionItem
from .utils import get_path

DEFAULT_RISK_PENALTIES = {
    "지분": 35,
    "맹지": 25,
    "법정지상권": 30,
    "분묘기지권": 22,
    "유치권": 30,
    "토지만매각": 15,
    "건물만매각": 25,
    "선순위": 25,
    "대항력": 20,
    "농지취득자격증명": 3,
    "재매각": 8,
    "공유자우선매수": 12,
}


def score_item(item: AuctionItem, profile: dict[str, Any]) -> AuctionItem:
    weights = profile.get("scoring", {}) or {}
    score = 0.0
    positive: list[str] = []
    risks: list[str] = []

    # 1) 감정가 대비 할인율
    w = float(weights.get("discount", 30))
    discount_component = min(max(item.discount_percent / 60, 0), 1) * w
    score += discount_component
    if item.discount_percent >= 40:
        positive.append(f"감정평가액 대비 {item.discount_percent:.1f}% 할인")

    # 2) 예산 중심점 적합도
    w = float(weights.get("budget_fit", 12))
    pmin = float(get_path(profile, "min_price.min", 0) or 0)
    pmax = float(get_path(profile, "min_price.max", 0) or 0)
    score += triangular_fit(item.min_price, pmin, pmax) * w

    # 3) 면적 중심점 적합도
    w = float(weights.get("area_fit", 10))
    amin = float(get_path(profile, "land_area_m2.min", 0) or 0)
    amax = float(get_path(profile, "land_area_m2.max", 0) or 0)
    area_fit = triangular_fit(item.land_area_m2, amin, amax)
    score += area_fit * w
    if area_fit >= 0.7:
        positive.append("희망 면적대 적합")

    # 4) 유찰수: 1~3회 선호, 지나치게 많으면 위험 신호
    w = float(weights.get("failed_count", 8))
    failed_scores = {0: 0.35, 1: 0.8, 2: 1.0, 3: 0.85, 4: 0.55}
    ffit = failed_scores.get(item.failed_count, 0.25)
    score += ffit * w
    if item.failed_count in (1, 2, 3):
        positive.append(f"유찰 {item.failed_count}회")

    # 5) 선호 용도
    w = float(weights.get("usage_preference", 10))
    prefs = profile.get("preferred_usages", {}) or {}
    pref = max((float(v) for k, v in prefs.items() if k in item.usage), default=0.0)
    max_pref = max([float(v) for v in prefs.values()] or [10.0])
    score += min(pref / max_pref, 1) * w

    # 6) 시장가 대비 차이. 상세 API/비교시세가 있는 경우에만 적극 반영.
    w = float(weights.get("market_gap", 20))
    if item.market_estimate > 0:
        gap = item.market_gap_percent
        score += min(max(gap / 45, 0), 1) * w
        if gap >= 20:
            positive.append(f"추정시세 대비 {gap:.1f}% 낮음")
    elif item.nearby_avg_unit_price > 0 and item.land_area_m2 > 0:
        implied_market = item.nearby_avg_unit_price * item.land_area_m2
        gap = (1 - item.min_price / implied_market) * 100 if implied_market > 0 else 0
        score += min(max(gap / 45, 0), 1) * w * 0.8
        if gap >= 20:
            positive.append(f"인근 단가 대비 약 {gap:.1f}% 낮음")

    # 7) 데이터 품질
    w = float(weights.get("data_quality", 10))
    checks = [
        bool(item.case_number), bool(item.address), item.min_price > 0,
        item.appraisal_price > 0, item.land_area_m2 > 0, bool(item.auction_date),
        bool(item.detail_url), bool(item.market_estimate or item.nearby_avg_unit_price),
    ]
    score += sum(checks) / len(checks) * w

    # 위험 감점
    text = _compact(" ".join([item.address, item.usage, *item.special_conditions]))
    for keyword, penalty in DEFAULT_RISK_PENALTIES.items():
        if _compact(keyword) in text:
            score -= penalty
            risks.append(f"{keyword}(-{penalty})")

    score = max(0.0, min(100.0, score))
    item.score = round(score, 1)
    item.grade = grade(score, risks)
    item.score_reasons = positive
    item.risk_reasons = risks
    item.matched_profile = str(profile.get("name", ""))
    return item


def triangular_fit(value: float, minimum: float, maximum: float) -> float:
    if maximum <= minimum or maximum <= 0:
        return 0.5
    if value < minimum or value > maximum:
        return 0.0
    center = (minimum + maximum) / 2
    half = (maximum - minimum) / 2
    return max(0.0, 1 - abs(value - center) / half) if half else 1.0


def grade(score: float, risks: list[str]) -> str:
    severe = any(any(k in r for k in ("지분", "법정지상권", "유치권", "선순위")) for r in risks)
    if severe:
        return "주의"
    if score >= 75:
        return "우선검토"
    if score >= 60:
        return "관심"
    if score >= 45:
        return "보류"
    return "제외"


def _compact(value: str) -> str:
    return re.sub(r"[^가-힣A-Za-z0-9]", "", str(value or "")).lower()
