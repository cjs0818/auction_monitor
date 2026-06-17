from __future__ import annotations

import copy
import csv
import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterable

import requests

from .models import AuctionItem
from .utils import get_path, to_date, to_float, to_int

logger = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    pass


class BaseProvider(ABC):
    @abstractmethod
    def fetch(self, profile: dict[str, Any]) -> list[AuctionItem]:
        raise NotImplementedError

    def fetch_detail(self, item: AuctionItem) -> AuctionItem:
        return item

    def close(self) -> None:
        return None


class CsvProvider(BaseProvider):
    def __init__(self, csv_path: str):
        self.csv_path = Path(csv_path)

    def fetch(self, profile: dict[str, Any]) -> list[AuctionItem]:
        if not self.csv_path.exists():
            raise ProviderError(f"CSV 파일이 없습니다: {self.csv_path}")
        items: list[AuctionItem] = []
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                items.append(_canonicalize(row, {}))
        return items


class GenericApiProvider(BaseProvider):
    """Config-driven connector for licensed APIs such as HYPHEN/경매다.

    It deliberately does not scrape commercial websites. Endpoint/body/field mapping
    are supplied from the API vendor's development guide after subscription.
    """

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.base_url = str(cfg.get("base_url", "")).rstrip("/")
        self.list_endpoint = str(cfg.get("list_endpoint", ""))
        self.detail_endpoint = str(cfg.get("detail_endpoint", ""))
        self.method = str(cfg.get("method", "POST")).upper()
        self.timeout = int(cfg.get("timeout_seconds", 30))
        self.headers = cfg.get("headers", {}) or {}
        self.field_map = cfg.get("field_map", {}) or {}
        self.list_path = str(cfg.get("response_list_path", "data.items"))
        self.detail_path = str(cfg.get("response_detail_path", "data"))
        self.pagination = cfg.get("pagination", {}) or {}

    def fetch(self, profile: dict[str, Any]) -> list[AuctionItem]:
        if not self.base_url or not self.list_endpoint:
            raise ProviderError(
                "generic_api의 base_url/list_endpoint가 비어 있습니다. "
                "API 상품 신청 후 개발가이드의 호출 경로를 config.yaml에 입력하세요."
            )
        regions = profile.get("regions") or [""]
        all_items: dict[str, AuctionItem] = {}
        for region in regions:
            province, city_county = split_region(str(region))
            for raw in self._fetch_region(profile, province, city_county):
                item = _canonicalize(raw, self.field_map)
                all_items[item.auction_id] = item
        return list(all_items.values())

    def _fetch_region(
        self, profile: dict[str, Any], province: str, city_county: str
    ) -> Iterable[dict[str, Any]]:
        pg = self.pagination
        enabled = bool(pg.get("enabled", False))
        page = int(pg.get("start_page", 1))
        max_pages = int(pg.get("max_pages", 1)) if enabled else 1
        page_size = int(pg.get("page_size", 100))

        for _ in range(max_pages):
            values = build_template_values(profile, province, city_county, page, page_size)
            body = render_template(copy.deepcopy(self.cfg.get("list_request_template", {})), values)
            payload = self._request(self.list_endpoint, body)
            rows = get_path(payload, self.list_path, [])
            if rows is None:
                rows = []
            if not isinstance(rows, list):
                raise ProviderError(f"response_list_path가 목록이 아닙니다: {self.list_path}")
            yield from rows

            if not enabled or not rows:
                break
            total_pages = to_int(get_path(payload, str(pg.get("total_pages_path", "")), 0))
            if total_pages and page >= total_pages:
                break
            if len(rows) < page_size and not total_pages:
                break
            page += 1

    def fetch_detail(self, item: AuctionItem) -> AuctionItem:
        if not self.detail_endpoint:
            return item
        values = {"auction_id": item.auction_id, "case_number": item.case_number}
        body = render_template(copy.deepcopy(self.cfg.get("detail_request_template", {})), values)
        payload = self._request(self.detail_endpoint, body)
        raw = get_path(payload, self.detail_path, {})
        if not isinstance(raw, dict):
            return item
        merged = dict(item.raw)
        merged.update(raw)
        return _canonicalize(merged, self.field_map)

    def _request(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        try:
            if self.method == "GET":
                resp = requests.get(url, headers=self.headers, params=body, timeout=self.timeout)
            else:
                resp = requests.request(
                    self.method, url, headers=self.headers, json=body, timeout=self.timeout
                )
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                raise ProviderError("API 응답이 JSON 객체가 아닙니다.")
            return data
        except requests.RequestException as exc:
            raise ProviderError(f"API 호출 실패: {url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ProviderError(f"API 응답 JSON 해석 실패: {url}") from exc



SEARCH_TARGET_OPTIONS = ("경매", "공매", "경매 및 공매")


def normalize_search_target(value: Any) -> str:
    text = str(value or "경매").strip()
    aliases = {
        "auction": "경매", "court": "경매", "경매": "경매",
        "public_sale": "공매", "onbid": "공매", "공매": "공매",
        "both": "경매 및 공매", "all": "경매 및 공매", "경매+공매": "경매 및 공매",
        "경매 및 공매": "경매 및 공매",
    }
    return aliases.get(text, "경매")


class CombinedProvider(BaseProvider):
    def __init__(self, providers: list[BaseProvider]):
        self.providers = providers
        self.last_fetch_diagnostics: list[dict[str, Any]] = []
        self.last_fetch_summary: dict[str, Any] = {}

    @property
    def cache_enabled(self) -> bool:
        return all(bool(getattr(p, "cache_enabled", False)) for p in self.providers)

    @cache_enabled.setter
    def cache_enabled(self, value: bool) -> None:
        for provider in self.providers:
            if hasattr(provider, "cache_enabled"):
                provider.cache_enabled = bool(value)

    def fetch(self, profile: dict[str, Any]) -> list[AuctionItem]:
        all_items: dict[str, AuctionItem] = {}
        diagnostics: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        for provider in self.providers:
            rows = provider.fetch(profile)
            for item in rows:
                all_items[f"{item.sale_type}|{item.auction_id}"] = item
            diagnostics.extend(getattr(provider, "last_fetch_diagnostics", []) or [])
            summary = getattr(provider, "last_fetch_summary", {}) or {}
            if summary:
                summaries.append(summary)
        self.last_fetch_diagnostics = diagnostics

        # 개별 공급자의 진단표에는 해당되지 않는 항목을 표시하기 위해 ``-``가
        # 들어올 수 있다. 통합검색에서는 이를 그대로 int/float로 변환하지 않고
        # 계산 시 0으로 정규화한다. 예: 공매 공급자의 ``실제 법원요청: -``.
        def _sum_float(name: str) -> float:
            return round(sum(to_float(s.get(name), 0.0) for s in summaries), 2)

        def _sum_int(name: str) -> int:
            return sum(to_int(s.get(name), 0) for s in summaries)

        self.last_fetch_summary = {
            "총 소요시간(초)": _sum_float("총 소요시간(초)"),
            "실제 법원요청": _sum_int("실제 법원요청"),
            "실제 공매요청": _sum_int("실제 공매요청"),
            "캐시 재사용": _sum_int("캐시 재사용"),
            "요청대기시간(초)": _sum_float("요청대기시간(초)"),
            "서버응답시간(초)": _sum_float("서버응답시간(초)"),
            "브라우저준비시간(초)": _sum_float("브라우저준비시간(초)"),
        }
        return list(all_items.values())

    def fetch_detail(self, item: AuctionItem) -> AuctionItem:
        for provider in self.providers:
            provider_name = provider.__class__.__name__
            if item.sale_type == "공매" and provider_name == "OnbidOpenApiProvider":
                return provider.fetch_detail(item)
            if item.sale_type == "경매" and provider_name == "CourtAuctionSeleniumProvider":
                return provider.fetch_detail(item)
        return item

    def test_connection(self, profile: dict[str, Any]) -> dict[str, Any]:
        results = []
        for provider in self.providers:
            if hasattr(provider, "test_connection"):
                results.append(provider.test_connection(profile))
        return {"source": "경매 및 공매", "results": results}

    def lookup_case(self, case_number: str, profile: dict[str, Any]) -> dict[str, Any]:
        for provider in self.providers:
            if hasattr(provider, "lookup_case"):
                return provider.lookup_case(case_number, profile)
        return {"found": False, "진단": "법원경매 공급자가 활성화되지 않았습니다."}

    def close(self) -> None:
        for provider in self.providers:
            try:
                provider.close()
            except Exception:
                logger.debug("공급자 종료 실패", exc_info=True)


def build_providers(config: dict[str, Any], search_target: str | None = None) -> list[BaseProvider]:
    target = normalize_search_target(
        search_target or (config.get("app", {}) or {}).get("search_target", "경매")
    )
    src = config.get("source", {}) or {}
    providers: list[BaseProvider] = []
    if target in {"경매", "경매 및 공매"}:
        kind = src.get("type", "court_selenium")
        if kind == "csv":
            providers.append(CsvProvider(str(src.get("csv_path", "sample_data/sample_auctions.csv"))))
        elif kind == "generic_api":
            providers.append(GenericApiProvider(src.get("generic_api", {}) or {}))
        else:
            from .court_selenium import CourtAuctionSeleniumProvider
            providers.append(CourtAuctionSeleniumProvider(src.get("court_selenium", {}) or {}))
    if target in {"공매", "경매 및 공매"}:
        from .onbid_openapi import OnbidOpenApiProvider
        providers.append(OnbidOpenApiProvider(src.get("onbid_openapi", {}) or {}))
    return providers

def build_provider(config: dict[str, Any], search_target: str | None = None) -> BaseProvider:
    providers = build_providers(config, search_target)
    if not providers:
        raise ProviderError("활성화된 데이터 공급자가 없습니다.")
    if len(providers) == 1:
        return providers[0]
    return CombinedProvider(providers)


def split_region(region: str) -> tuple[str, str]:
    parts = region.strip().split(maxsplit=1)
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def build_template_values(
    profile: dict[str, Any], province: str, city_county: str, page: int, page_size: int
) -> dict[str, Any]:
    return {
        "province": province,
        "city_county": city_county,
        "page": page,
        "page_size": page_size,
        "profile_name": profile.get("name", ""),
        "status": ",".join(profile.get("statuses", [])),
        "usage": ",".join(profile.get("usages", [])),
        "min_price": get_path(profile, "min_price.min", ""),
        "max_price": get_path(profile, "min_price.max", ""),
        "min_area": get_path(profile, "land_area_m2.min", ""),
        "max_area": get_path(profile, "land_area_m2.max", ""),
        "min_failed_count": get_path(profile, "failed_count.min", ""),
        "max_failed_count": get_path(profile, "failed_count.max", ""),
    }


def render_template(value: Any, values: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {k: render_template(v, values) for k, v in value.items()}
    if isinstance(value, list):
        return [render_template(v, values) for v in value]
    if isinstance(value, str):
        try:
            rendered = value.format(**values)
        except (KeyError, ValueError):
            rendered = value
        # Preserve numeric types when the whole value is a placeholder.
        if rendered.isdigit():
            return int(rendered)
        return rendered
    return value


def _canonicalize(raw: dict[str, Any], field_map: dict[str, str]) -> AuctionItem:
    def val(name: str, default: Any = "") -> Any:
        path = field_map.get(name, name)
        return get_path(raw, path, default)

    conditions = val("special_conditions", [])
    if isinstance(conditions, str):
        conditions = [x.strip() for x in conditions.replace("|", ";").split(";") if x.strip()]
    elif not isinstance(conditions, list):
        conditions = [str(conditions)] if conditions else []

    auction_id = str(val("auction_id", "")).strip()
    if not auction_id:
        auction_id = f"{val('case_number','')}-{val('item_number','')}"
    return AuctionItem(
        auction_id=auction_id,
        sale_type=str(val("sale_type", "경매")) or "경매",
        source_name=str(val("source_name", "외부 제공 데이터")) or "외부 제공 데이터",
        case_number=str(val("case_number", "")),
        item_number=str(val("item_number", "")),
        court=str(val("court", "")),
        status=str(val("status", "")),
        usage=str(val("usage", "")),
        address=str(val("address", "")),
        province=str(val("province", "")),
        city_county=str(val("city_county", "")),
        min_price=to_int(val("min_price", 0)),
        appraisal_price=to_int(val("appraisal_price", 0)),
        failed_count=to_int(val("failed_count", 0)),
        land_area_m2=to_float(val("land_area_m2", 0)),
        building_area_m2=to_float(val("building_area_m2", 0)),
        auction_date=to_date(val("auction_date", None)),
        special_conditions=conditions,
        detail_url=str(val("detail_url", "")),
        market_estimate=to_int(val("market_estimate", 0)),
        nearby_avg_unit_price=to_float(val("nearby_avg_unit_price", 0)),
        raw=raw,
    )
