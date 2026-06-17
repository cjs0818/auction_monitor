from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any


@dataclass
class AuctionItem:
    auction_id: str
    sale_type: str = "경매"
    source_name: str = "대한민국 법원경매정보"
    case_number: str = ""
    item_number: str = ""
    court: str = ""
    status: str = ""
    usage: str = ""
    address: str = ""
    province: str = ""
    city_county: str = ""
    min_price: int = 0
    appraisal_price: int = 0
    failed_count: int = 0
    land_area_m2: float = 0.0
    building_area_m2: float = 0.0
    auction_date: date | None = None
    special_conditions: list[str] = field(default_factory=list)
    detail_url: str = ""
    market_estimate: int = 0
    nearby_avg_unit_price: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    score: float = 0.0
    grade: str = "미평가"
    score_reasons: list[str] = field(default_factory=list)
    risk_reasons: list[str] = field(default_factory=list)
    matched_profile: str = ""

    @property
    def discount_percent(self) -> float:
        if self.appraisal_price <= 0:
            return 0.0
        return max(0.0, (1 - self.min_price / self.appraisal_price) * 100)

    @property
    def unit_price(self) -> float:
        if self.land_area_m2 <= 0:
            return 0.0
        return self.min_price / self.land_area_m2

    @property
    def market_gap_percent(self) -> float:
        if self.market_estimate <= 0:
            return 0.0
        return (1 - self.min_price / self.market_estimate) * 100

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["auction_date"] = self.auction_date.isoformat() if self.auction_date else ""
        d["discount_percent"] = round(self.discount_percent, 1)
        d["unit_price"] = round(self.unit_price, 0)
        d["market_gap_percent"] = round(self.market_gap_percent, 1)
        return d
