from __future__ import annotations

import hashlib
import html
import json
import logging
import random
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .models import AuctionItem
from .filtering import item_matches_region
from .regions import (
    MUNICIPALITY_CODES,
    normalize_court_region_code_values,
    resolve_region_codes,
    resolve_region_code_variants,
    region_label,
    split_region_label,
)
from .utils import to_date, to_float, to_int

logger = logging.getLogger(__name__)

BASE_URL = "https://www.courtauction.go.kr"
WARMUP_PATH = "/pgj/index.on?w2xPath=/pgj/ui/pgj100/PGJ151F00.xml&pgjId=151F00"
SEARCH_PATH = "/pgj/pgjsearch/searchControllerMain.on"
COURTS_PATH = "/pgj/pgjComm/selectCortOfcCdLst.on"
CASE_DETAIL_PATH = "/pgj/pgj15A/selectAuctnCsSrchRslt.on"
CASE_SEARCH_URL = (
    "https://www.courtauction.go.kr/pgj/index.on?"
    "w2xPath=/pgj/ui/pgj100/PGJ159M00.xml&pgjId=159M00"
)
SUBMISSION_ID = "mf_wfm_mainFrame_sbm_selectGdsDtlSrch"
VALID_PAGE_SIZES = {10, 20, 50, 100}

# 지목/용도 판별용. 사이트 결과의 jimokList는 HTML 또는 쉼표 구분 문자열일 수 있다.
LAND_USAGES = [
    "전", "답", "과수원", "목장용지", "임야", "광천지", "염전", "대",
    "공장용지", "학교용지", "주차장", "주유소용지", "창고용지", "도로",
    "철도용지", "제방", "하천", "구거", "유지", "양어장", "수도용지",
    "공원", "체육용지", "유원지", "종교용지", "사적지", "묘지", "잡종지",
]

PROVINCE_ALIASES = {
    "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시",
    "인천": "인천광역시", "광주": "광주광역시", "대전": "대전광역시",
    "울산": "울산광역시", "세종": "세종특별자치시", "경기": "경기도",
    "강원": "강원특별자치도", "충북": "충청북도", "충남": "충청남도",
    "전북": "전북특별자치도", "전남": "전라남도", "경북": "경상북도",
    "경남": "경상남도", "제주": "제주특별자치도",
}

# 사건번호는 법원별로 중복될 수 있어 선택지역의 관할 법원을 우선 탐색한다.
# 법원 목록은 사이트에서 동적으로 받아오며, 아래 값은 이름 필터용 힌트다.
REGION_COURT_HINTS = {
    "전북특별자치도 부안군": ["정읍지원"],
    "전라북도 부안군": ["정읍지원"],
}
PROVINCE_COURT_HINTS = {
    "전북특별자치도": ["전주지방법원"],
    "전라북도": ["전주지방법원"],
}


class CourtAuctionSeleniumError(RuntimeError):
    pass


class CourtAuctionHttpError(CourtAuctionSeleniumError):
    """검색 endpoint가 HTTP 오류를 반환했을 때 상태/응답을 보존한다."""

    def __init__(self, status: int, message: str, response_text: str = ""):
        super().__init__(message)
        self.status = int(status or 0)
        self.response_text = response_text


class CourtAuctionBlockedError(CourtAuctionSeleniumError):
    pass


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<img\b[^>]*>", " ", text, flags=re.I)
    text = re.sub(r"<br\s*/?\s*>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _first_nonblank(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = _clean_text(row.get(key))
        if value:
            return value
    return ""


def _normalize_case_number(value: str) -> str:
    value = _clean_text(value).replace(" ", "")
    match = re.search(r"(\d{4})타경(\d+)", value)
    if match:
        return f"{match.group(1)}타경{match.group(2)}"
    return value


def _build_address(row: dict[str, Any]) -> str:
    parts = [
        _clean_text(row.get("hjguSido")),
        _clean_text(row.get("hjguSigu")),
        _clean_text(row.get("hjguDong")),
        _clean_text(row.get("hjguRd")),
        _clean_text(row.get("daepyoLotno")),
        _clean_text(row.get("buldNm")),
    ]
    parts = [x for x in parts if x]
    if parts:
        return " ".join(dict.fromkeys(parts))
    return _first_nonblank(row, "realSt", "printSt", "st", "userSt")


def _extract_area(row: dict[str, Any]) -> float:
    # 검색결과에 minArea/maxArea가 있으면 사이트가 계산한 면적값을 우선한다.
    max_area = to_float(row.get("maxArea"), 0)
    min_area = to_float(row.get("minArea"), 0)
    if max_area > 0:
        return max_area
    if min_area > 0:
        return min_area

    area_text = _clean_text(row.get("areaList"))
    # "1,180㎡", "330.5 m2" 등에서 면적을 합산한다.
    found = re.findall(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:㎡|m2|m²)", area_text, flags=re.I)
    values = [to_float(x, 0) for x in found]
    values = [x for x in values if x > 0]
    if values:
        return sum(values)
    return 0.0


def _extract_usage(row: dict[str, Any]) -> str:
    text = " ".join([
        _clean_text(row.get("jimokList")),
        _clean_text(row.get("areaList")),
        _clean_text(row.get("pjbBuldList")),
    ])
    matched: list[str] = []
    for usage in LAND_USAGES:
        # 법원 원문에는 "전123㎡", "답 456㎡"처럼 지목 뒤에 숫자가 바로 붙는 경우가 있다.
        # 숫자까지 단어문자로 막으면 정상 지목을 놓치므로 한글/영문만 경계로 사용한다.
        pattern = rf"(?<![가-힣A-Za-z]){re.escape(usage)}(?![가-힣A-Za-z])"
        if re.search(pattern, text) and usage not in matched:
            matched.append(usage)
    if matched:
        return "/".join(matched[:4])
    if str(row.get("lclsUtilCd", "")) == "10000":
        return "토지"
    return _first_nonblank(row, "usgNm", "mulBigo") or "토지"


def _normalize_status(row: dict[str, Any], failed_count: int) -> str:
    raw = " ".join(
        _clean_text(row.get(k))
        for k in ("mulStatcd", "mulStatNm", "jinstatCd", "jinstatNm", "status")
    )
    keywords = ["신건", "유찰", "진행", "재매각", "매각", "변경", "연기", "정지", "취하"]
    found = [k for k in keywords if k in raw]
    if found:
        return "/".join(dict.fromkeys(found))
    return "진행/유찰" if failed_count > 0 else "진행/신건"


def _first_int(row: dict[str, Any], *keys: str) -> int:
    """여러 금액 필드 중 첫 번째 유효한 정수를 반환한다."""
    for key in keys:
        value = to_int(row.get(key), 0)
        if value:
            return value
    return 0


def _first_date(row: dict[str, Any], *keys: str):
    """법원 사건상세 응답의 현재/과거 기일 날짜 필드명을 모두 지원한다."""
    for key in keys:
        value = to_date(row.get(key))
        if value:
            return value
    return None


def _sale_time(row: dict[str, Any]) -> str:
    raw = _first_nonblank(row, "dxdyHm", "fstDspslHm", "scndDspslHm", "thrdDspslHm", "fothDspslHm")
    digits = re.sub(r"[^0-9]", "", raw)
    if len(digits) == 3:
        digits = "0" + digits
    if len(digits) == 4:
        return f"{digits[:2]}:{digits[2:]}"
    return raw


def _current_minimum_price(row: dict[str, Any]) -> int:
    """현재 매각공고 행에서 최저매각가격을 찾는다.

    법원 응답은 화면/사건 상태에 따라 lwsDspslPrc 또는
    fst~fothPbancLwsDspslPrc 중 하나를 사용한다.
    """
    return _first_int(
        row,
        "lwsDspslPrc",
        "minmaePrice",
        "fothPbancLwsDspslPrc",
        "thrdPbancLwsDspslPrc",
        "scndPbancLwsDspslPrc",
        "fstPbancLwsDspslPrc",
    )


def _extract_case_schedule_rows(case_data: dict[str, Any]) -> list[dict[str, Any]]:
    """사건상세 응답에서 현재 예정기일과 과거 기일을 합쳐 정규화한다.

    현재 예정기일은 dlt_dspslGdsDspslObjctLst[].dspslDxdyYmd,
    과거 기일은 dlt_rletCsGdsDtsDxdyInf[].dxdyYmd에 들어온다.
    구버전 응답의 dspslDxdyYmd/lwsDspslPrc도 함께 지원한다.
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}

    current_rows = case_data.get("dlt_dspslGdsDspslObjctLst")
    current_rows = current_rows if isinstance(current_rows, list) else []
    for row in current_rows:
        if not isinstance(row, dict):
            continue
        d = _first_date(row, "dspslDxdyYmd", "dxdyYmd", "maeGiil")
        if not d:
            continue
        item_no = _first_nonblank(row, "dspslGdsSeq", "dspslObjctSeq", "mokmulSer")
        key = (item_no, d.isoformat())
        merged[key] = {
            "물건번호": item_no,
            "매각기일": d.isoformat(),
            "매각시간": _sale_time(row),
            "매각장소": _first_nonblank(row, "dxdyPlcNm", "dspslPlcNm"),
            "감정평가액": _first_int(row, "aeeEvlAmt", "gamevalAmt"),
            "최저매각가격": _current_minimum_price(row),
            "결과코드": _first_nonblank(row, "auctnDxdyGdsStatCd", "auctnGdsStatCd", "rsltCd"),
            "기일구분": "현재 예정기일",
        }

    history_rows = case_data.get("dlt_rletCsGdsDtsDxdyInf")
    history_rows = history_rows if isinstance(history_rows, list) else []
    for row in history_rows:
        if not isinstance(row, dict):
            continue
        d = _first_date(row, "dxdyYmd", "dspslDxdyYmd", "maeGiil")
        if not d:
            continue
        item_no = _first_nonblank(row, "dspslGdsSeq", "dspslObjctSeq", "mokmulSer")
        key = (item_no, d.isoformat())
        normalized = {
            "물건번호": item_no,
            "매각기일": d.isoformat(),
            "매각시간": _sale_time(row),
            "매각장소": _first_nonblank(row, "dxdyPlcNm", "dspslPlcNm"),
            "감정평가액": _first_int(row, "aeeEvlAmt", "gamevalAmt"),
            "최저매각가격": _first_int(row, "lwsDspslPrc", "minmaePrice"),
            "결과코드": _first_nonblank(row, "auctnDxdyRsltCd", "rsltCd", "auctnDxdyGdsStatCd"),
            "기일구분": "기일내역",
        }
        if key in merged:
            current = merged[key]
            for field in ("매각시간", "매각장소", "감정평가액", "최저매각가격", "결과코드"):
                if not current.get(field) and normalized.get(field):
                    current[field] = normalized[field]
            current["기일구분"] = "현재 예정기일/기일내역"
        else:
            merged[key] = normalized

    return sorted(
        merged.values(),
        key=lambda x: (
            0 if "현재 예정기일" in str(x.get("기일구분", "")) else 1,
            x.get("매각기일", ""),
            x.get("물건번호", ""),
        ),
    )




_COURT_TERMINAL_RESULT_WORDS = (
    "유찰", "매각", "낙찰", "취하", "변경", "연기", "정지", "불허가",
    "기각", "종결", "미납", "재매각", "입찰불능", "취소",
)


def _schedule_result_text(row: dict[str, Any]) -> str:
    return _first_nonblank(
        row,
        "결과코드", "rsltCd", "auctnDxdyRsltCd", "auctnDxdyGdsStatCd",
        "auctnGdsStatCd", "기일결과",
    )


def _is_terminal_schedule_result(value: Any) -> bool:
    text = _clean_text(value)
    if not text:
        return False
    # 숫자 코드만 있는 경우는 사이트별 코드표가 달라 단정하지 않는다. 화면에
    # 한글 결과가 병기된 행만 종료행으로 판정한다.
    if text.isdigit():
        return False
    return any(word in text for word in _COURT_TERMINAL_RESULT_WORDS)


def _select_actionable_court_schedule(
    case_data: dict[str, Any],
    item_number: str,
    *,
    today: date | None = None,
    preferred_date: date | None = None,
) -> dict[str, Any] | None:
    """사건상세 기일내역에서 실제 다음 매각기일과 그 회차 가격을 고른다.

    법원 목록의 ``minmaePrice``는 경우에 따라 직전 유찰회차 가격을 유지한다.
    사건상세의 ``dlt_rletCsGdsDtsDxdyInf``에는 과거 유찰행과 다음 예정행이 함께
    내려오므로, 물건번호가 일치하고 결과가 비어 있는 오늘 이후의 가장 가까운
    행을 대표값으로 사용한다.
    """
    today = today or date.today()
    wanted = _clean_text(item_number)
    rows = _extract_case_schedule_rows(case_data)
    filtered: list[dict[str, Any]] = []
    for row in rows:
        row_item = _clean_text(row.get("물건번호"))
        if wanted and row_item and row_item != wanted:
            continue
        d = to_date(row.get("매각기일"))
        if not d:
            continue
        normalized = dict(row)
        normalized["_date"] = d
        normalized["_terminal"] = _is_terminal_schedule_result(_schedule_result_text(row))
        filtered.append(normalized)
    if not filtered:
        return None

    # 목록의 예정일과 정확히 일치하는 사건상세 행이 있으면 가장 먼저 사용한다.
    if preferred_date:
        exact = [r for r in filtered if r["_date"] == preferred_date and not r["_terminal"]]
        if exact:
            exact.sort(key=lambda r: (0 if int(r.get("최저매각가격") or 0) > 0 else 1))
            return exact[0]

    future_open = [r for r in filtered if r["_date"] >= today and not r["_terminal"]]
    if future_open:
        future_open.sort(key=lambda r: (r["_date"], 0 if int(r.get("최저매각가격") or 0) > 0 else 1))
        return future_open[0]

    # 일부 법원 응답은 다음 예정행에도 상태코드를 숫자로만 넣는다. 날짜가 미래면
    # 가격이 있는 가장 가까운 행을 2차 후보로 사용한다.
    future_any = [r for r in filtered if r["_date"] >= today]
    if future_any:
        future_any.sort(key=lambda r: (r["_date"], 0 if int(r.get("최저매각가격") or 0) > 0 else 1))
        return future_any[0]

    # 종결/과거 사건은 가장 최근 기일을 진단용 대표값으로 남긴다.
    filtered.sort(key=lambda r: r["_date"], reverse=True)
    return filtered[0]


def _count_prior_failed_rounds(
    case_data: dict[str, Any], item_number: str, selected_date: date | None
) -> int:
    wanted = _clean_text(item_number)
    count = 0
    for row in _extract_case_schedule_rows(case_data):
        row_item = _clean_text(row.get("물건번호"))
        if wanted and row_item and row_item != wanted:
            continue
        d = to_date(row.get("매각기일"))
        if selected_date and d and d >= selected_date:
            continue
        result = _schedule_result_text(row)
        if "유찰" in result:
            count += 1
    return count


def _apply_case_schedule_to_item(
    item: AuctionItem,
    case_data: dict[str, Any],
    *,
    today: date | None = None,
) -> AuctionItem:
    selected = _select_actionable_court_schedule(
        case_data,
        item.item_number,
        today=today,
        preferred_date=item.auction_date,
    )
    raw = item.raw if isinstance(item.raw, dict) else {}
    item.raw = raw
    raw["COURT_SCHEDULE_ROWS"] = [
        {k: v for k, v in row.items() if not str(k).startswith("_")}
        for row in _extract_case_schedule_rows(case_data)
    ]
    if not selected:
        raw["court_price_source"] = "목록검색 응답(사건상세 예정행 없음)"
        return item

    selected_date = selected.get("_date") or to_date(selected.get("매각기일"))
    selected_price = to_int(selected.get("최저매각가격"), 0)
    selected_appraisal = to_int(selected.get("감정평가액"), 0)
    old_price = int(item.min_price or 0)
    old_date = item.auction_date

    if selected_date:
        item.auction_date = selected_date
    if selected_price > 0:
        item.min_price = selected_price
    if selected_appraisal > 0:
        item.appraisal_price = selected_appraisal
    failed_from_history = _count_prior_failed_rounds(case_data, item.item_number, selected_date)
    if failed_from_history > 0:
        item.failed_count = max(int(item.failed_count or 0), failed_from_history)

    raw["court_selected_schedule"] = {
        k: v for k, v in selected.items() if not str(k).startswith("_")
    }
    raw["court_price_source"] = "사건상세 다음 예정기일"
    raw["court_previous_list_min_price"] = old_price
    raw["court_previous_list_auction_date"] = old_date.isoformat() if old_date else ""
    raw["court_price_corrected"] = bool(
        (selected_price > 0 and selected_price != old_price)
        or (selected_date and selected_date != old_date)
    )
    return item


def _split_location(address: str) -> tuple[str, str]:
    tokens = address.split()
    province = tokens[0] if tokens else ""
    city_county = tokens[1] if len(tokens) > 1 else ""
    return province, city_county




def _search_result_next_minimum_price(row: dict[str, Any]) -> tuple[int, str]:
    """조건검색 행에서 다음 공고의 최저매각가격을 우선한다.

    ``minmaePrice``는 직전/현재 회차 가격으로 남는 사례가 있고, 법원 검색응답은
    다음 공고 가격을 ``notifyMinmaePrice1~4``에 별도로 제공한다. 양수인 첫 예정
    가격을 우선하고 없을 때만 ``minmaePrice``로 되돌아간다.
    """
    for index in range(1, 5):
        key = f"notifyMinmaePrice{index}"
        value = to_int(row.get(key), 0)
        if value > 0:
            return value, key
    value = to_int(row.get("minmaePrice"), 0)
    return value, "minmaePrice"


def normalize_search_row(row: dict[str, Any]) -> AuctionItem:
    case_number = _normalize_case_number(
        _first_nonblank(row, "srnSaNo", "printCsNo", "saNo", "csNo")
    )
    # 법원 검색응답에서 maemulSer는 입찰표에 기재하는 실제 '물건순번',
    # mokmulSer는 그 물건을 구성하는 토지·건물 등의 '목적물번호'다.
    # 일괄매각 사건에서 mokmulSer를 물건번호로 쓰면 동일 매각가격을 가진
    # 수십~수백 개의 가짜 독립 물건이 생성되므로 반드시 물건순번을 우선한다.
    item_number = _first_nonblank(
        row,
        "maemulSer",
        "Group_maemul_ser",
        "dspslGdsSeq",
        "dspslSeq",
        "itemNo",
        "mokmulSer",
    )
    object_number = _first_nonblank(row, "mokmulSer", "dspslObjctSeq")
    address = _build_address(row)
    province, city_county = _split_location(address)
    failed_count = to_int(row.get("yuchalCnt"), 0)
    remarks = _clean_text(row.get("mulBigo"))
    property_description = _clean_text(row.get("pjbBuldList"))
    building_list = _clean_text(row.get("buldList"))
    conditions = [x for x in (remarks, property_description, building_list) if x]
    court_code = _first_nonblank(row, "boCd", "cortOfcCd")
    court_name = _first_nonblank(row, "jiwonNm", "cortOfcNm", "courtName")
    document_id = _first_nonblank(row, "docid", "dspslRealId")
    canonical_lot_id = "|".join(
        x for x in ("court", court_code or court_name, case_number, item_number) if x
    )
    # 단일 목적물은 기존 docid를 유지해 DB 이력 호환성을 지킨다. 동일 물건순번에
    # 여러 목적물이 병합되는 순간 _merge_court_lot()에서 canonical_lot_id로 바꾼다.
    auction_id = document_id or canonical_lot_id
    if not auction_id:
        auction_id = json.dumps(row, ensure_ascii=False, sort_keys=True)[:200]

    min_price, min_price_source = _search_result_next_minimum_price(row)
    compatible_raw = dict(row)
    compatible_raw["court_list_price_source"] = min_price_source
    compatible_raw["court_list_previous_min_price"] = to_int(row.get("minmaePrice"), 0)
    compatible_raw["court_lot_number"] = item_number
    compatible_raw["court_object_number"] = object_number
    compatible_raw["court_document_id"] = document_id
    compatible_raw["court_canonical_lot_id"] = canonical_lot_id

    component = {
        "목적물번호": object_number,
        "소재지": address,
        "용도": _extract_usage(row),
        "토지면적(㎡)": _extract_area(row),
        "문서ID": document_id,
    }
    compatible_raw["COURT_COMPONENTS"] = [component]
    compatible_raw["court_component_count"] = 1

    return AuctionItem(
        auction_id=auction_id,
        sale_type="경매",
        source_name="대한민국 법원경매정보",
        case_number=case_number,
        item_number=item_number,
        court=court_name,
        status=_normalize_status(row, failed_count),
        usage=_extract_usage(row),
        address=address,
        province=province,
        city_county=city_county,
        min_price=min_price,
        appraisal_price=to_int(row.get("gamevalAmt"), 0),
        failed_count=failed_count,
        land_area_m2=_extract_area(row),
        building_area_m2=0,
        auction_date=to_date(row.get("maeGiil")),
        special_conditions=conditions,
        detail_url=CASE_SEARCH_URL,
        market_estimate=0,
        nearby_avg_unit_price=0,
        raw=compatible_raw,
    )


def _court_lot_key(item: AuctionItem) -> str:
    """법원 사건의 실제 매각물건 단위 식별자."""
    raw = item.raw if isinstance(item.raw, dict) else {}
    court_code = _first_nonblank(raw, "boCd", "cortOfcCd", "courtCode") or item.court
    lot_number = _first_nonblank(
        raw, "court_lot_number", "maemulSer", "Group_maemul_ser", "dspslGdsSeq"
    ) or item.item_number
    return "|".join(str(x or "").strip() for x in (court_code, item.case_number, lot_number))


def _component_identity(component: dict[str, Any]) -> tuple[str, str, str]:
    """반복 날짜구간·현행/과거코드 조회에서 목적물 중복 합산을 막는다."""
    object_number = _clean_text(component.get("목적물번호"))
    address = _clean_text(component.get("소재지"))
    document_id = _clean_text(component.get("문서ID"))
    # 현행/과거 지역코드나 날짜구간을 달리 조회하면 동일 목적물의 docid가
    # 달라질 수 있다. 목적물번호·주소가 있으면 그것만으로 중복을 판정한다.
    if object_number or address:
        return object_number, address, ""
    return "", "", document_id


def _merge_court_lot(existing: AuctionItem, incoming: AuctionItem) -> AuctionItem:
    """동일 물건순번에 속한 여러 목적물 행을 한 개의 경매물건으로 병합한다.

    법원 조건검색은 일괄매각 물건을 토지·건물 목적물별 행으로 반환하면서
    각 행에 물건 전체의 감정가와 최저매각가격을 반복한다. 가격을 합산하지 않고,
    목적물 주소·용도·면적만 중복 없이 합쳐 실제 입찰 단위 한 건으로 만든다.
    """
    existing_raw = existing.raw if isinstance(existing.raw, dict) else {}
    incoming_raw = incoming.raw if isinstance(incoming.raw, dict) else {}
    existing.raw = existing_raw

    components: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for source in (
        existing_raw.get("COURT_COMPONENTS", []),
        incoming_raw.get("COURT_COMPONENTS", []),
    ):
        if not isinstance(source, list):
            continue
        for component in source:
            if not isinstance(component, dict):
                continue
            key = _component_identity(component)
            if key in seen:
                continue
            seen.add(key)
            components.append(dict(component))

    if not components:
        return existing

    addresses = [
        _clean_text(component.get("소재지"))
        for component in components
        if _clean_text(component.get("소재지"))
    ]
    usages: list[str] = []
    for component in components:
        for usage in re.split(r"[/,·]", _clean_text(component.get("용도"))):
            usage = usage.strip()
            if usage and usage not in usages:
                usages.append(usage)

    total_area = sum(to_float(component.get("토지면적(㎡)"), 0) for component in components)
    if addresses:
        existing.address = addresses[0]
        if len(components) > 1:
            existing.address += f" 외 {len(components) - 1}건"
        existing.province, existing.city_county = _split_location(addresses[0])
    if usages:
        existing.usage = "/".join(usages[:4])
        if len(usages) > 4:
            existing.usage += " 등"
    if total_area > 0:
        existing.land_area_m2 = total_area

    # 금액은 물건 전체 금액이 반복된 것이므로 합산하지 않는다. 응답 간 차이가
    # 있으면 양수 값을 우선하되, 값 목록을 원자료에 남겨 진단 가능하게 한다.
    min_prices = sorted({x for x in (int(existing.min_price or 0), int(incoming.min_price or 0)) if x > 0})
    appraisal_prices = sorted({
        x for x in (int(existing.appraisal_price or 0), int(incoming.appraisal_price or 0)) if x > 0
    })
    if not existing.min_price and incoming.min_price:
        existing.min_price = incoming.min_price
    if not existing.appraisal_price and incoming.appraisal_price:
        existing.appraisal_price = incoming.appraisal_price

    existing.failed_count = max(int(existing.failed_count or 0), int(incoming.failed_count or 0))
    if existing.auction_date is None or (
        incoming.auction_date is not None and incoming.auction_date < existing.auction_date
    ):
        existing.auction_date = incoming.auction_date
    existing.special_conditions = list(dict.fromkeys([
        *existing.special_conditions,
        *incoming.special_conditions,
    ]))
    if len(components) > 1:
        canonical_lot_id = _clean_text(existing_raw.get("court_canonical_lot_id"))
        if canonical_lot_id:
            existing.auction_id = canonical_lot_id
        grouped_note = (
            f"법원 목록상 물건번호 {existing.item_number}에 목적물 {len(components)}건 포함"
        )
        if grouped_note not in existing.special_conditions:
            existing.special_conditions.append(grouped_note)

    existing_raw["COURT_COMPONENTS"] = components
    existing_raw["court_component_count"] = len(components)
    existing_raw["court_grouped_lot"] = len(components) > 1
    existing_raw["court_component_addresses"] = addresses
    existing_raw["court_object_numbers"] = [
        _clean_text(component.get("목적물번호")) for component in components
    ]
    existing_raw["court_price_scope"] = "매각물건 전체"
    existing_raw["court_observed_min_prices"] = min_prices
    existing_raw["court_observed_appraisal_prices"] = appraisal_prices
    return existing


def _range_text(
    spec: dict[str, Any] | None,
    key: str,
    *,
    zero_is_blank: bool = False,
) -> str:
    if not spec:
        return ""
    value = spec.get(key)
    if value in (None, ""):
        return ""
    if zero_is_blank:
        try:
            if float(value) == 0:
                return ""
        except (TypeError, ValueError):
            pass
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace(",", "").strip()


def _number_text(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _discount_to_price_rate(profile: dict[str, Any]) -> tuple[str, str]:
    spec = profile.get("appraisal_discount_percent", {}) or {}
    dmin = spec.get("min")
    dmax = spec.get("max")
    rate_min = "" if dmax in (None, "") else _number_text(max(0.0, 100.0 - float(dmax)))
    rate_max = "" if dmin in (None, "") else _number_text(min(100.0, 100.0 - float(dmin)))
    return rate_min, rate_max


def build_sale_date_windows(
    profile: dict[str, Any],
    cfg: dict[str, Any] | None = None,
    today: date | None = None,
) -> list[tuple[date, date]]:
    """GUI의 전체 매각기일 범위를 법원 허용 범위의 여러 구간으로 나눈다.

    법원 물건상세검색은 한 요청의 날짜 폭이 짧아야 안정적으로 동작한다.
    예를 들어 GUI에서 90일 이내를 선택하면 14일 단위(기본값: 시작일+13일)로
    분할하여 모든 구간을 순차 조회한다. 이전 버전은 첫 구간만 조회해 14일 이후
    물건이 누락되는 문제가 있었다.
    """
    cfg = cfg or {}
    today = today or date.today()
    requested_days = int(profile.get("auction_within_days", 13) or 13)
    requested_days = max(0, min(requested_days, 365))
    chunk_span_days = int(cfg.get("sale_window_days", 13) or 13)
    # 시작일과 종료일을 모두 포함하므로 13은 14일짜리 구간이다.
    chunk_span_days = max(1, min(chunk_span_days, 13))

    final_day = today + timedelta(days=requested_days)
    windows: list[tuple[date, date]] = []
    start = today
    while start <= final_day:
        end = min(start + timedelta(days=chunk_span_days), final_day)
        windows.append((start, end))
        start = end + timedelta(days=1)
    return windows


def build_search_body(
    profile: dict[str, Any],
    page: int,
    page_size: int,
    cfg: dict[str, Any] | None = None,
    today: date | None = None,
    region_codes: dict[str, str] | None = None,
    sale_from_date: date | None = None,
    sale_to_date: date | None = None,
    relaxed_server_filters: bool = False,
    omit_usage_filter: bool = False,
) -> dict[str, Any]:
    cfg = cfg or {}
    today = today or date.today()
    if page_size not in VALID_PAGE_SIZES:
        raise CourtAuctionSeleniumError(
            f"page_size는 {sorted(VALID_PAGE_SIZES)} 중 하나여야 합니다: {page_size}"
        )

    if sale_from_date is not None or sale_to_date is not None:
        if sale_from_date is None or sale_to_date is None:
            raise CourtAuctionSeleniumError("매각기일 시작일과 종료일을 함께 지정해야 합니다.")
        if sale_to_date < sale_from_date:
            raise CourtAuctionSeleniumError("매각기일 종료일은 시작일보다 빠를 수 없습니다.")
        if (sale_to_date - sale_from_date).days > 13:
            raise CourtAuctionSeleniumError("법원 검색요청의 매각기일 범위는 14일 이내여야 합니다.")
        sale_from = sale_from_date.strftime("%Y%m%d")
        sale_to = sale_to_date.strftime("%Y%m%d")
    else:
        # 단독 호출과 기존 테스트의 호환을 위해 첫 구간을 사용한다. 전체 기간은
        # fetch()가 build_sale_date_windows()로 나누어 모두 조회한다.
        first_window = build_sale_date_windows(profile, cfg, today)[0]
        sale_from = first_window[0].strftime("%Y%m%d")
        sale_to = first_window[1].strftime("%Y%m%d")

    raw_region_codes = region_codes or {}
    court_code_override = str(raw_region_codes.get("court_code", "") or "").strip()
    try:
        region_codes = normalize_court_region_code_values(raw_region_codes)
    except ValueError as exc:
        raise CourtAuctionSeleniumError(f"법원경매 지역코드 오류: {exc}") from exc
    sido = region_codes["sido"]
    sigungu = region_codes["sigungu"]
    dong = region_codes["dong"]
    has_region = bool(sido or sigungu or dong)

    discount_rate_min, discount_rate_max = _discount_to_price_rate(profile)
    force_land = bool(cfg.get("force_land_category", True))
    # 시·군·구 최소조건검색에서는 법원 내부 분류가 혼합용도/건물로 잡힌 토지까지
    # 놓치지 않도록 대분류 필터를 비운다. 최종 지목은 수집 후 다시 판정한다.
    large_usage_code = "" if omit_usage_filter else (
        "10000" if force_land else str(cfg.get("large_usage_code", ""))
    )

    # 일부 법원 검색환경에서는 가격비율·면적·유찰횟수 조합이 실제 화면과
    # 다르게 0건을 반환할 수 있다. 정확한 시·군·구에서 엄격검색이 0건이면
    # 지역·토지·날짜만 서버에 보내고 나머지는 로컬에서 동일 기준으로 거른다.
    if relaxed_server_filters:
        appraisal_min = appraisal_max = ""
        rate_min = rate_max = ""
        failed_min = failed_max = ""
        area_min = area_max = ""
        price_min = price_max = ""
    else:
        appraisal_min = _range_text(profile.get("appraisal_price"), "min", zero_is_blank=True)
        appraisal_max = _range_text(profile.get("appraisal_price"), "max", zero_is_blank=True)
        rate_min, rate_max = discount_rate_min, discount_rate_max
        failed_min = _range_text(profile.get("failed_count"), "min")
        failed_max = _range_text(profile.get("failed_count"), "max")
        area_min = _range_text(profile.get("land_area_m2"), "min", zero_is_blank=True)
        area_max = _range_text(profile.get("land_area_m2"), "max", zero_is_blank=True)
        price_min = _range_text(profile.get("min_price"), "min", zero_is_blank=True)
        price_max = _range_text(profile.get("min_price"), "max", zero_is_blank=True)

    return {
        "dma_pageInfo": {
            "pageNo": int(page),
            "pageSize": int(page_size),
            "bfPageNo": "",
            "startRowNo": "",
            "totalCnt": "",
            "totalYn": "Y",
            "groupTotalCount": "",
        },
        "dma_srchGdsDtlSrchInfo": {
            "rletDspslSpcCondCd": "",
            "bidDvsCd": str(cfg.get("bid_type_code", "000331")),
            "mvprpRletDvsCd": "00031R",
            "cortAuctnSrchCondCd": "0004601",
            "rprsAdongSdCd": sido,
            "rprsAdongSggCd": sigungu,
            "rprsAdongEmdCd": dong,
            "rdnmSdCd": "",
            "rdnmSggCd": "",
            "rdnmNo": "",
            "mvprpDspslPlcAdongSdCd": "",
            "mvprpDspslPlcAdongSggCd": "",
            "mvprpDspslPlcAdongEmdCd": "",
            "rdDspslPlcAdongSdCd": "",
            "rdDspslPlcAdongSggCd": "",
            "rdDspslPlcAdongEmdCd": "",
            "cortOfcCd": court_code_override or str(cfg.get("court_code", "")),
            "jdbnCd": "",
            "execrOfcDvsCd": "",
            "lclDspslGdsLstUsgCd": large_usage_code,
            "mclDspslGdsLstUsgCd": str(cfg.get("medium_usage_code", "")),
            "sclDspslGdsLstUsgCd": str(cfg.get("small_usage_code", "")),
            "cortAuctnMbrsId": "",
            "aeeEvlAmtMin": appraisal_min,
            "aeeEvlAmtMax": appraisal_max,
            "lwsDspslPrcRateMin": rate_min,
            "lwsDspslPrcRateMax": rate_max,
            "flbdNcntMin": failed_min,
            "flbdNcntMax": failed_max,
            "objctArDtsMin": area_min,
            "objctArDtsMax": area_max,
            "mvprpArtclKndCd": "",
            "mvprpArtclNm": "",
            "mvprpAtchmPlcTypCd": "",
            "notifyLoc": "off",
            "lafjOrderBy": str(cfg.get("order_by", "")),
            "pgmId": "PGJ151F01",
            "csNo": "",
            "cortStDvs": "2" if has_region else "1",
            "statNum": 1,
            "bidBgngYmd": sale_from,
            "bidEndYmd": sale_to,
            "dspslDxdyYmd": "",
            "fstDspslHm": "",
            "scndDspslHm": "",
            "thrdDspslHm": "",
            "fothDspslHm": "",
            "dspslPlcNm": "",
            "lwsDspslPrcMin": price_min,
            "lwsDspslPrcMax": price_max,
            "grbxTypCd": "",
            "gdsVendNm": "",
            "fuelKndCd": "",
            "carMdyrMax": "",
            "carMdyrMin": "",
            "carMdlNm": "",
            "sideDvsCd": "",
        },
    }


class CourtAuctionSeleniumProvider:
    """대한민국 법원경매정보를 Selenium 브라우저 세션으로 읽는 read-only 공급자.

    화면의 CSS selector를 직접 조작하지 않고, 공식 검색화면을 Selenium으로 연 뒤
    동일 출처의 검색 요청을 브라우저 내부 fetch로 실행한다. CAPTCHA/접근차단 우회는
    시도하지 않으며 차단 신호가 확인되면 즉시 중단한다.
    """

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.base_url = str(cfg.get("base_url", BASE_URL)).rstrip("/")
        self.headless = bool(cfg.get("headless", True))
        self.timeout = int(cfg.get("timeout_seconds", 45))
        self.warmup_wait = float(cfg.get("warmup_wait_seconds", 6))
        self.adaptive_warmup = bool(cfg.get("adaptive_warmup", True))
        self.warmup_settle = float(cfg.get("warmup_settle_seconds", 0.75))
        self.min_delay = float(cfg.get("min_delay_seconds", 3.0))
        self.jitter = float(cfg.get("jitter_seconds", 1.5))
        self.adaptive_throttle = bool(cfg.get("adaptive_throttle", True))
        self.min_delay_floor = max(0.2, float(cfg.get("min_delay_floor_seconds", 1.0) or 0.2))
        self.min_delay_floor = min(self.min_delay_floor, self.min_delay)
        self.delay_recover_step = max(0.0, float(cfg.get("delay_recover_step_seconds", 0.25) or 0.0))
        self.delay_backoff_step = max(0.0, float(cfg.get("delay_backoff_step_seconds", 0.8) or 0.0))
        self.delay_backoff_max = max(0.0, float(cfg.get("delay_backoff_max_seconds", 3.0) or 0.0))
        self.max_calls = int(cfg.get("max_calls_per_run", 10))
        self.call_limit = self.max_calls
        self.hard_call_cap = int(cfg.get("hard_call_cap", 60) or 60)
        self.page_size = int(cfg.get("page_size", 20))
        self.max_pages = int(cfg.get("max_pages", 8))
        # 시·군·구 정확검색은 대형 일괄매각의 목적물 행이 페이지를 독점할 수 있다.
        # 서버 totalCnt가 기본 페이지 한도를 넘으면 필요한 만큼 자동 확장하되 상한을 둔다.
        self.municipality_auto_max_pages = max(
            self.max_pages,
            min(100, int(cfg.get("municipality_auto_max_pages", 30) or 30)),
        )
        # 시·도 전체검색에서 시·군·구 수가 적은 지역은 전국검색보다
        # 각 시·군·구를 직접 조회하는 편이 정확하다. 제주(2개 시), 세종,
        # 광주·대전·울산 등에 적용하며 대규모 시·도는 기존 대체검색을 유지한다.
        self.province_fanout_max_municipalities = max(
            1, min(30, int(cfg.get("province_fanout_max_municipalities", 8) or 8))
        )
        self.legacy_fallback_only = bool(cfg.get("legacy_code_fallback_only", True))
        self.cache_enabled = bool(cfg.get("cache_enabled", True))
        self.cache_ttl_seconds = max(0, int(cfg.get("cache_ttl_minutes", 15) or 0) * 60)
        self.cache_dir = Path(str(cfg.get("cache_dir", "data/selenium_cache")))
        self.debug_dir = Path(str(cfg.get("debug_dir", "data/selenium_debug")))
        self.save_exchange_debug = bool(cfg.get("save_exchange_debug", False))
        self.fast_mode = bool(cfg.get("fast_mode", True))
        self.price_detail_policy = str(cfg.get("price_detail_policy", "smart") or "smart").strip().lower()
        if self.price_detail_policy not in {"always", "smart", "never"}:
            self.price_detail_policy = "smart"
        self.price_detail_max_per_run = max(0, int(cfg.get("price_detail_max_per_run", 6) or 0))
        self.detail_min_delay = max(0.0, float(cfg.get("detail_min_delay_seconds", 1.5) or 0.0))
        self.detail_jitter = max(0.0, float(cfg.get("detail_jitter_seconds", 0.5) or 0.0))
        self.photo_enabled = bool(cfg.get("photo_enabled", True))
        self.photo_cache_dir = Path(str(cfg.get("photo_cache_dir", "data/court_photo_cache")))
        self.photo_cache_days = max(0, int(cfg.get("photo_cache_days", 30) or 0))
        self.photo_max_per_run = max(0, int(cfg.get("photo_max_per_run", 6) or 0))
        self.photo_wait_seconds = max(0.15, float(cfg.get("photo_wait_seconds", 0.45) or 0.45))
        self.photo_capture_timeout = max(0.8, float(cfg.get("photo_capture_timeout_seconds", 2.5) or 2.5))
        self.photo_missing_cache_days = max(0, int(cfg.get("photo_missing_cache_days", 7) or 0))
        self.photo_debug_enabled = bool(cfg.get("photo_debug_enabled", False))
        # 위치지도는 주소 오차로 인해 서로 다른 물건이 같은 지도로 보일 수 있어 더 이상 사용하지 않는다.
        self.photo_map_fallback = False
        self.driver = None
        self.calls_so_far = 0
        self.network_calls = 0
        self.cache_hits = 0
        self.throttle_wait_seconds = 0.0
        self.request_elapsed_seconds = 0.0
        self.warmup_elapsed_seconds = 0.0
        self.last_call_at = 0.0
        self.warmed_up = False
        self.current_search_delay = self.min_delay
        self.last_fetch_diagnostics: list[dict[str, Any]] = []
        self.last_fetch_summary: dict[str, Any] = {}
        self.photo_network_attempts = 0
        self.photo_cache_hits = 0
        self.photo_new_count = 0
        self.photo_failure_count = 0
        self.case_detail_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self.price_detail_attempts = 0
        self.price_detail_success_count = 0
        self.price_detail_failure_count = 0
        self.price_detail_skipped_count = 0

    def _cache_path(self, body: dict[str, Any]) -> Path:
        raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _load_cache(self, body: dict[str, Any]) -> dict[str, Any] | None:
        if not self.cache_enabled or self.cache_ttl_seconds <= 0:
            return None
        path = self._cache_path(body)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            created_at = float(data.get("created_at", 0) or 0)
            payload = data.get("payload")
            if time.time() - created_at > self.cache_ttl_seconds:
                path.unlink(missing_ok=True)
                return None
            if isinstance(payload, dict):
                self.cache_hits += 1
                return payload
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None
        return None

    def _save_cache(self, body: dict[str, Any], payload: dict[str, Any]) -> None:
        if not self.cache_enabled or self.cache_ttl_seconds <= 0:
            return
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            path = self._cache_path(body)
            temp = path.with_suffix(".tmp")
            temp.write_text(
                json.dumps({"created_at": time.time(), "payload": payload}, ensure_ascii=False),
                encoding="utf-8",
            )
            temp.replace(path)
        except OSError:
            logger.debug("검색 캐시 저장 실패", exc_info=True)

    def _finish_fetch_summary(
        self,
        started_at: float,
        start_network_calls: int,
        start_cache_hits: int,
        start_wait_seconds: float,
        start_request_seconds: float,
        start_warmup_seconds: float,
        date_windows: list[tuple[date, date]],
    ) -> None:
        self.last_fetch_summary = {
            "총 소요시간(초)": round(time.monotonic() - started_at, 1),
            "실제 법원요청": self.network_calls - start_network_calls,
            "실제 공매요청": 0,
            "캐시 재사용": self.cache_hits - start_cache_hits,
            "요청대기시간(초)": round(self.throttle_wait_seconds - start_wait_seconds, 1),
            "서버응답시간(초)": round(self.request_elapsed_seconds - start_request_seconds, 1),
            "브라우저준비시간(초)": round(self.warmup_elapsed_seconds - start_warmup_seconds, 1),
            "검색기본대기(초)": round(self.current_search_delay, 2),
            "날짜구간수": len(date_windows),
            "경매사진 캐시": self.photo_cache_hits,
            "경매사진 신규수집": self.photo_new_count,
            "경매사진 실패": self.photo_failure_count,
        }

    def _record_request_success(self, request_kind: str) -> None:
        if not self.adaptive_throttle or request_kind != "search":
            return
        if self.delay_recover_step <= 0:
            return
        self.current_search_delay = max(
            self.min_delay_floor,
            self.current_search_delay - self.delay_recover_step,
        )

    def _record_request_failure(self, request_kind: str) -> None:
        if not self.adaptive_throttle or request_kind != "search":
            return
        if self.delay_backoff_step <= 0:
            return
        ceiling = max(self.min_delay, self.min_delay_floor) + self.delay_backoff_max
        self.current_search_delay = min(ceiling, self.current_search_delay + self.delay_backoff_step)

    def _query_plan(self, profile: dict[str, Any]) -> list[tuple[str, list[tuple[str, list[dict[str, str] | None]]], bool, bool]]:
        """검색 안정성을 높이기 위한 단계별 검색계획을 만든다.

        시·군·구 검색은 정확 코드 후 시·도 대체검색을 사용한다. 시·도 전체검색은
        시·도 코드만 넣은 브라우저 요청이 실패할 수 있어 전국 목록을 조회한 뒤
        주소로 해당 시·도만 남긴다. 가격·면적·유찰·할인율은 runner에서 다시 검증한다.
        """
        selected_regions = [str(x).strip() for x in profile.get("regions", []) or [] if str(x).strip()]
        groups = self._region_query_groups(profile) if selected_regions else [("전국", [None])]
        exact_municipality = bool(groups) and all(
            all(bool((codes or {}).get("sigungu")) for codes in variants)
            for _, variants in groups
        )
        province_only = bool(groups) and all(
            all(bool((codes or {}).get("sido")) and not bool((codes or {}).get("sigungu"))
                for codes in variants)
            for _, variants in groups
        )
        if province_only:
            # 제주특별자치도는 관할 법원이 제주지방법원 한 곳이므로 지역코드
            # 두 건을 연속 호출하지 않고 법원코드로 한 번 조회한다. 법원 사이트가
            # 제주시/서귀포시 연속 fetch 중 status 0을 반환하는 현상을 피하면서
            # 제주 전체 물건은 주소검증으로 안전하게 한정한다.
            if len(groups) == 1:
                province, municipality = split_region_label(groups[0][0])
                if province == "제주특별자치도" and not municipality:
                    return [(
                        "제주지방법원 전체검색 후 주소검증",
                        [(groups[0][0], [{
                            "sido": "", "sigungu": "", "dong": "",
                            "court_code": "B000530",
                        }])],
                        True,
                        True,
                    )]

            # 전국검색의 앞쪽 페이지만 읽고 주소로 거르는 방식은 제주처럼 전국
            # 물건 수에 비해 대상 물건이 적은 지역을 완전히 놓칠 수 있다.
            # 시·군·구 수가 적은 시·도는 각 시·군·구를 직접 조회해 누락을 막는다.
            fanout_groups: list[tuple[str, list[dict[str, str] | None]]] = []
            fanout_possible = True
            for label, _ in groups:
                province, municipality = split_region_label(label)
                municipalities = MUNICIPALITY_CODES.get(province, {})
                if municipality or not municipalities or (
                    len(municipalities) > self.province_fanout_max_municipalities
                ):
                    fanout_possible = False
                    break
                variants: list[dict[str, str] | None] = []
                for municipality_name in municipalities:
                    municipal_label = region_label(province, municipality_name)
                    variants.extend(resolve_region_code_variants(municipal_label))
                if not variants:
                    fanout_possible = False
                    break
                # 지역 표시는 원래 시·도명을 유지하고 요청만 하위 시·군·구로 분할한다.
                fanout_groups.append((label, variants))

            if fanout_possible and fanout_groups:
                return [
                    ("시·도 코드 직접검색", groups, True, False),
                    ("시·도 전체 시·군·구 분할검색", fanout_groups, True, True),
                ]

            # 법원 사이트는 시·도 코드만 지정한 요청에서 브라우저 fetch 자체가
            # ``TypeError: Failed to fetch``(status 0)로 실패하는 경우가 있다.
            # 시·군·구가 많은 시·도는 기존처럼 전국 검색 후 주소를 엄격히 검증한다.
            nationwide_groups = [(label + " (전국 대체검색)", [None]) for label, _ in groups]
            return [
                ("시·도 코드 직접검색", groups, True, False),
                ("전국 대체검색 후 시·도 주소검증", nationwide_groups, True, False),
            ]
        if not exact_municipality:
            return [("지역·날짜 우선검색", groups, True, False)]

        province_groups: list[tuple[str, list[dict[str, str] | None]]] = []
        for label, variants in groups:
            province_variants: list[dict[str, str] | None] = []
            seen: set[str] = set()
            for codes in variants:
                if not codes:
                    continue
                sido = str(codes.get("sido", ""))
                if not sido or sido in seen:
                    continue
                seen.add(sido)
                province_variants.append({"sido": sido, "sigungu": "", "dong": ""})
            if province_variants:
                province_groups.append((label + " (시·도 대체검색)", province_variants))

        # 정확한 시·군·구 검색은 용도 대분류까지 비워 누락 가능성을 최소화한다.
        # 시·도 대체검색은 데이터량을 통제하기 위해 토지 대분류를 유지한다.
        plan = [("시·군·구 완전 최소조건검색", groups, True, True)]
        if province_groups:
            plan.append(("시·도 대체검색 후 주소검증", province_groups, True, False))
        return plan

    def fetch(self, profile: dict[str, Any]) -> list[AuctionItem]:
        """선택지역을 서버 요청과 수집 단계에서 모두 우선 적용한다.

        1차는 시·군·구 코드로 직접 조회한다. 서버가 코드를 무시하거나 다른 지역
        행을 섞어 반환해도 선택지역 주소가 아닌 행은 즉시 버린다. 정확검색에서
        선택지역 물건을 한 건도 확보하지 못한 경우에만 시·도 대체검색을 수행하며,
        이때도 페이지를 읽는 즉시 시·군·구 주소를 검증해 해당 지역만 수집한다.
        """
        started_at = time.monotonic()
        start_network_calls = self.network_calls
        start_cache_hits = self.cache_hits
        start_wait_seconds = self.throttle_wait_seconds
        start_request_seconds = self.request_elapsed_seconds
        start_warmup_seconds = self.warmup_elapsed_seconds

        date_windows = build_sale_date_windows(profile, self.cfg)
        search_plan = self._query_plan(profile)

        first_groups = search_plan[0][1]
        baseline_calls = len(date_windows) * sum(len(variants) for _, variants in first_groups)
        self.call_limit = max(
            self.call_limit,
            min(self.hard_call_cap, self.calls_so_far + baseline_calls * self.max_pages),
        )

        self.last_fetch_diagnostics = []
        found: dict[str, AuctionItem] = {}
        effective_page_size = self.page_size
        exact_region_match_count: dict[str, int] = {}

        for mode_index, (mode_label, mode_groups, relaxed_filters, omit_usage_filter) in enumerate(search_plan):
            if mode_index > 0:
                mode_calls = len(date_windows) * sum(len(v) for _, v in mode_groups)
                self.call_limit = max(
                    self.call_limit,
                    min(self.hard_call_cap, self.calls_so_far + mode_calls * self.max_pages),
                )

            for region_label, variants in mode_groups:
                target_region = (
                    region_label.replace(" (시·도 대체검색)", "")
                    .replace(" (전국 대체검색)", "")
                    .strip()
                )
                # 정확한 시·군·구 검색에서 이미 대상지역 물건을 확보했다면
                # 시·도 전체 대체검색은 실행하지 않는다.
                if mode_index > 0 and exact_region_match_count.get(target_region, 0) > 0:
                    self.last_fetch_diagnostics.append({
                        "검색방식": mode_label,
                        "지역": target_region,
                        "조회코드": "-",
                        "코드구분": "대체검색 생략",
                        "조회기간": (
                            f"{date_windows[0][0].isoformat()}~{date_windows[-1][1].isoformat()}"
                            if date_windows else "-"
                        ),
                        "완료구간": "0/0",
                        "법원 전체건수": 0,
                        "수집건수": 0,
                        "지역일치건수": 0,
                        "지역불일치제외": 0,
                        "비고": "시·군·구 직접검색에서 대상지역 물건 확보",
                    })
                    continue

                group_match_count = 0
                for variant_index, region_codes in enumerate(variants):
                    province_direct_mode = "시·도 코드 직접검색" in mode_label
                    code_label = self._format_region_codes(region_codes)
                    variant_total = 0
                    variant_rows = 0
                    variant_region_matches = 0
                    variant_region_mismatches = 0
                    completed_windows = 0
                    limit_reached = False
                    variant_page_size = (
                        max(effective_page_size, 100)
                        if "전국 대체검색" in mode_label else effective_page_size
                    )

                    auto_expanded_page_limit = self.max_pages
                    required_pages_observed = 0
                    truncation_observed = False

                    for sale_from_date, sale_to_date in date_windows:
                        page = 1
                        page_limit = self.max_pages
                        while page <= page_limit:
                            if self.calls_so_far >= self.call_limit:
                                logger.warning(
                                    "법원경매 사이트 보호를 위한 실행당 호출 한도(%s회)에 도달해 "
                                    "추가 조회를 중단합니다.", self.call_limit,
                                )
                                limit_reached = True
                                break

                            request_page_size = variant_page_size
                            body = build_search_body(
                                profile, page, request_page_size, self.cfg,
                                region_codes=region_codes,
                                sale_from_date=sale_from_date,
                                sale_to_date=sale_to_date,
                                relaxed_server_filters=relaxed_filters,
                                omit_usage_filter=omit_usage_filter,
                            )
                            try:
                                payload = self._post_json(body)
                            except CourtAuctionHttpError as exc:
                                if exc.status == 0:
                                    logger.warning(
                                        "법원경매 브라우저 fetch가 끊겨 검색화면 세션을 복구한 뒤 1회 재시도합니다: %s",
                                        code_label,
                                    )
                                    self._recover_search_session()
                                    try:
                                        payload = self._post_json(body)
                                    except CourtAuctionHttpError as retry_exc:
                                        if (
                                            retry_exc.status in {0, 400}
                                            and page == 1
                                            and request_page_size > 20
                                        ):
                                            logger.warning(
                                                "세션 복구 후에도 페이지 크기 %s 요청이 실패하여 "
                                                "20건으로 자동 전환합니다.", request_page_size,
                                            )
                                            effective_page_size = 20
                                            variant_page_size = 20
                                            request_page_size = 20
                                            body = build_search_body(
                                                profile, page, request_page_size, self.cfg,
                                                region_codes=region_codes,
                                                sale_from_date=sale_from_date,
                                                sale_to_date=sale_to_date,
                                                relaxed_server_filters=relaxed_filters,
                                                omit_usage_filter=omit_usage_filter,
                                            )
                                            self._recover_search_session()
                                            payload = self._post_json(body)
                                        elif province_direct_mode:
                                            logger.warning(
                                                "시·도 코드 직접검색이 상태 %s로 실패해 대체검색으로 전환합니다: %s",
                                                retry_exc.status, code_label,
                                            )
                                            payload = {
                                                "data": {
                                                    "dma_pageInfo": {"totalCnt": 0},
                                                    "dlt_srchResult": [],
                                                }
                                            }
                                        else:
                                            raise
                                elif exc.status == 400 and page == 1 and request_page_size > 20:
                                    logger.warning(
                                        "법원경매 사이트가 페이지 크기 %s 요청을 상태 %s로 거부하여 "
                                        "20건으로 자동 전환합니다.", request_page_size, exc.status,
                                    )
                                    effective_page_size = 20
                                    variant_page_size = 20
                                    request_page_size = 20
                                    body = build_search_body(
                                        profile, page, request_page_size, self.cfg,
                                        region_codes=region_codes,
                                        sale_from_date=sale_from_date,
                                        sale_to_date=sale_to_date,
                                        relaxed_server_filters=relaxed_filters,
                                        omit_usage_filter=omit_usage_filter,
                                    )
                                    payload = self._post_json(body)
                                elif province_direct_mode:
                                    logger.warning(
                                        "시·도 코드 직접검색이 상태 %s로 실패해 대체검색으로 전환합니다: %s",
                                        exc.status, code_label,
                                    )
                                    payload = {
                                        "data": {
                                            "dma_pageInfo": {"totalCnt": 0},
                                            "dlt_srchResult": [],
                                        }
                                    }
                                else:
                                    raise

                            data = payload.get("data") if isinstance(payload, dict) else None
                            data = data if isinstance(data, dict) else {}
                            rows = data.get("dlt_srchResult")
                            rows = rows if isinstance(rows, list) else []
                            page_info = data.get("dma_pageInfo")
                            page_info = page_info if isinstance(page_info, dict) else {}
                            total_count = to_int(page_info.get("totalCnt"), len(rows))
                            if page == 1:
                                variant_total += total_count
                                # 정확한 시·군·구 조회에서는 일괄매각 한 건이 수백 개 목적물
                                # 행으로 펼쳐질 수 있다. 설정된 8페이지(160행)에서 자르면 다른
                                # 실제 물건이 뒤 페이지에 있어도 누락되므로 totalCnt에 맞춰 확장한다.
                                is_exact_municipality = bool((region_codes or {}).get("sigungu"))
                                if is_exact_municipality and total_count > request_page_size * page_limit:
                                    required_pages = (total_count + request_page_size - 1) // request_page_size
                                    required_pages_observed = max(required_pages_observed, required_pages)
                                    expanded_limit = min(required_pages, self.municipality_auto_max_pages)
                                    if expanded_limit > page_limit:
                                        extra_pages = expanded_limit - page_limit
                                        page_limit = expanded_limit
                                        auto_expanded_page_limit = max(auto_expanded_page_limit, page_limit)
                                        self.call_limit = max(
                                            self.call_limit,
                                            min(self.hard_call_cap, self.call_limit + extra_pages),
                                        )
                                    if required_pages > self.municipality_auto_max_pages:
                                        truncation_observed = True
                            variant_rows += len(rows)

                            for raw in rows:
                                if not isinstance(raw, dict):
                                    continue
                                item = normalize_search_row(raw)
                                if target_region and target_region != "전국" and not item_matches_region(item, target_region):
                                    variant_region_mismatches += 1
                                    continue
                                variant_region_matches += 1
                                group_match_count += 1
                                lot_key = _court_lot_key(item)
                                if lot_key in found:
                                    found[lot_key] = _merge_court_lot(found[lot_key], item)
                                else:
                                    found[lot_key] = item

                            logger.info(
                                "법원경매 검색: 방식=%s 프로필=%s 지역=%s 코드=%s 기간=%s~%s "
                                "페이지=%s 서버행=%s 지역일치=%s 전체=%s",
                                mode_label, profile.get("name", ""), target_region, code_label,
                                sale_from_date.isoformat(), sale_to_date.isoformat(), page,
                                len(rows), variant_region_matches, total_count,
                            )
                            if not rows or len(rows) < request_page_size:
                                break
                            if total_count and page * request_page_size >= total_count:
                                break
                            page += 1

                        if limit_reached:
                            break
                        completed_windows += 1

                    period_label = (
                        f"{date_windows[0][0].isoformat()}~{date_windows[-1][1].isoformat()}"
                        if date_windows else "-"
                    )
                    note = "호출한도 도달(일부 기간만 조회)" if limit_reached else (
                        "대상지역 검색 성공" if variant_region_matches else (
                            "서버행은 있으나 대상지역 0건" if variant_rows else "0건"
                        )
                    )
                    if auto_expanded_page_limit > self.max_pages:
                        note += f" · 대형 일괄매각 대응 {self.max_pages}→{auto_expanded_page_limit}페이지 자동확장"
                    if truncation_observed:
                        note += (
                            f" · 서버 필요 {required_pages_observed}페이지 중 "
                            f"안전상한 {self.municipality_auto_max_pages}페이지까지만 조회"
                        )
                    self.last_fetch_diagnostics.append({
                        "검색방식": mode_label,
                        "지역": target_region,
                        "조회코드": code_label,
                        "코드구분": "현행" if variant_index == 0 else "과거코드 병행",
                        "조회기간": period_label,
                        "완료구간": f"{completed_windows}/{len(date_windows)}",
                        "법원 전체건수": variant_total,
                        "수집건수": variant_rows,
                        "지역일치건수": variant_region_matches,
                        "지역불일치제외": variant_region_mismatches,
                        "비고": note
                        + (" · 가격/면적/유찰/할인율은 수집 후 적용" if relaxed_filters else "")
                        + (" · 물건 대분류도 수집 후 판정" if omit_usage_filter else ""),
                    })

                    if limit_reached:
                        self._finish_fetch_summary(
                            started_at, start_network_calls, start_cache_hits,
                            start_wait_seconds, start_request_seconds, start_warmup_seconds,
                            date_windows,
                        )
                        return list(found.values())

                    # 특별자치도는 현행 코드에서 대상지역 물건이 확인되면 과거 코드
                    # 조회를 생략한다. 과거 코드는 현행 코드가 0건일 때만 호환용으로 사용한다.
                    same_sigungu_variants = len({
                        str((candidate or {}).get("sigungu", "")) for candidate in variants
                    }) <= 1
                    if (
                        variant_index == 0
                        and variant_region_matches > 0
                        and self.legacy_fallback_only
                        and len(variants) > 1
                        and same_sigungu_variants
                    ):
                        self.last_fetch_diagnostics.append({
                            "검색방식": mode_label,
                            "지역": target_region,
                            "조회코드": "-",
                            "코드구분": "과거코드 조회 생략",
                            "조회기간": period_label,
                            "완료구간": "0/0",
                            "법원 전체건수": 0,
                            "수집건수": 0,
                            "지역일치건수": 0,
                            "지역불일치제외": 0,
                            "비고": "현행 지역코드에서 대상지역 물건을 확보하여 중복 조회를 생략",
                        })
                        break

                if mode_index == 0:
                    exact_region_match_count[target_region] = group_match_count

        self._finish_fetch_summary(
            started_at, start_network_calls, start_cache_hits,
            start_wait_seconds, start_request_seconds, start_warmup_seconds,
            date_windows,
        )
        return list(found.values())

    def _needs_price_detail(self, item: AuctionItem) -> bool:
        """목록응답만으로 다음 기일/가격을 신뢰할 수 있는지 판단한다.

        법원 목록이 ``notifyMinmaePrice1~4`` 중 하나와 매각기일을 제공하면 이미
        다음 공고 가격을 갖고 있으므로 사건상세 재조회 효과가 거의 없다. 고속모드에서는
        값이 없거나 구형 ``minmaePrice``만 남은 유찰물건처럼 교정 필요성이 높은 경우만
        상세조회한다.
        """
        if self.price_detail_policy == "never":
            return False
        if self.price_detail_policy == "always" or not self.fast_mode:
            return True
        raw = item.raw if isinstance(item.raw, dict) else {}
        source = str(raw.get("court_list_price_source", "") or "")
        has_notified_next_price = source.startswith("notifyMinmaePrice") and int(item.min_price or 0) > 0
        has_sale_date = item.auction_date is not None
        if has_notified_next_price and has_sale_date:
            return False
        if int(item.min_price or 0) <= 0 or not has_sale_date:
            return True
        # 신건은 목록의 minmaePrice가 현재 회차 가격이므로 별도 교정 필요성이 낮다.
        if int(item.failed_count or 0) <= 0:
            return False
        return source in {"", "minmaePrice"}

    def _enrich_current_schedule(self, item: AuctionItem) -> AuctionItem:
        """사건상세의 기일내역으로 다음 매각기일/최저가를 교정한다."""
        if item.sale_type == "공매":
            return item
        raw = item.raw if isinstance(item.raw, dict) else {}
        item.raw = raw
        if not self._needs_price_detail(item):
            raw["court_price_source"] = "목록검색 응답(고속검증 통과)"
            self.price_detail_skipped_count += 1
            return item
        if self.price_detail_attempts >= self.price_detail_max_per_run:
            raw["court_price_source"] = "목록검색 응답(상세조회 상한)"
            self.price_detail_skipped_count += 1
            return item
        court_code = _first_nonblank(raw, "boCd", "cortOfcCd", "courtCode")
        case_number = _normalize_case_number(item.case_number)
        if not court_code or not case_number:
            raw["court_price_source"] = "목록검색 응답(법원코드 없음)"
            self.price_detail_skipped_count += 1
            return item
        key = (court_code, case_number)
        try:
            case_data = self.case_detail_cache.get(key)
            if case_data is None:
                self.price_detail_attempts += 1
                # 교정 필요성이 높은 상위 후보만 제한적으로 상세 조회한다.
                self.call_limit = max(
                    self.call_limit,
                    min(self.hard_call_cap, self.calls_so_far + 1),
                )
                payload = self._post_json_endpoint(
                    CASE_DETAIL_PATH,
                    {"dma_srchCsDtlInf": {"cortOfcCd": court_code, "csNo": case_number}},
                    debug_name="case_detail_price",
                    request_kind="detail",
                )
                data = payload.get("data") if isinstance(payload, dict) else {}
                case_data = data if isinstance(data, dict) else {}
                self.case_detail_cache[key] = case_data
            _apply_case_schedule_to_item(item, case_data)
            self.price_detail_success_count += 1
        except Exception as exc:
            self.price_detail_failure_count += 1
            raw["court_price_source"] = f"목록검색 응답(상세교정 실패:{type(exc).__name__})"
            logger.warning("법원경매 다음 매각가격 교정 실패 %s: %s", item.auction_id, exc)
        return item

    def fetch_detail(self, item: AuctionItem) -> AuctionItem:
        """상위 표시 후보의 다음 회차 가격을 교정하고 대표 시각자료를 보강한다."""
        if item.sale_type == "공매":
            return item

        from .court_photo import (
            capture_court_photo,
            discard_unusable_photo_cache,
            is_recent_missing_photo_cache,
            is_usable_photo_cache,
            photo_cache_path,
            photo_cache_source,
        )

        raw = item.raw if isinstance(item.raw, dict) else {}
        item.raw = raw
        if not self.photo_enabled:
            # 사진 기능이 꺼진 경우에만 가격 교정을 시도한다.
            return self._enrich_current_schedule(item)

        cached_path = photo_cache_path(item, self.photo_cache_dir)
        if is_usable_photo_cache(cached_path, self.photo_cache_days):
            raw["court_image_cache_path"] = str(cached_path.resolve())
            raw["court_photo_source"] = photo_cache_source(cached_path) or "cache"
            raw.setdefault("court_price_source", "목록검색 응답(사진 캐시 우선)")
            self.photo_cache_hits += 1
            return item
        if cached_path.exists() or cached_path.with_suffix(cached_path.suffix + ".source").exists():
            discard_unusable_photo_cache(cached_path)
        if is_recent_missing_photo_cache(cached_path, self.photo_missing_cache_days):
            raw["court_photo_source"] = "photo-not-found-cached"
            raw.setdefault("court_price_source", "목록검색 응답(사진 미존재 캐시)")
            return item

        if self.photo_network_attempts >= self.photo_max_per_run:
            raw["court_photo_source"] = "run-limit"
            raw.setdefault("court_price_source", "목록검색 응답(사진수집 상한)")
            return item

        # 새 사진을 수집할 때만 브라우저 세션을 준비하므로, 사진 캐시/상한 경로는
        # 드라이버 기동 없이 매우 빠르게 종료된다.
        item = self._enrich_current_schedule(item)

        self.photo_network_attempts += 1
        try:
            self._ensure_driver()
            image_path, source = capture_court_photo(
                self.driver,
                item,
                cache_dir=self.photo_cache_dir,
                cache_days=self.photo_cache_days,
                timeout_seconds=min(float(self.timeout), 7.0),
                settle_seconds=self.photo_wait_seconds,
                capture_timeout_seconds=self.photo_capture_timeout,
                missing_cache_days=self.photo_missing_cache_days,
                debug_dir=(self.debug_dir / "court_photo") if self.photo_debug_enabled else None,
                map_fallback=self.photo_map_fallback,
            )
            raw["court_photo_source"] = source
            if image_path:
                raw["court_image_cache_path"] = image_path
                self.photo_new_count += 1
            else:
                self.photo_failure_count += 1
        except Exception as exc:
            logger.warning("법원경매 대표사진 보강 실패 %s: %s", item.auction_id, exc)
            raw["court_photo_source"] = f"error:{type(exc).__name__}"
            self.photo_failure_count += 1
        finally:
            # 상세화면으로 이동했으므로 다음 검색요청은 공식 검색화면에서 다시 준비한다.
            self.warmed_up = False
        return item

    def test_connection(self, profile: dict[str, Any]) -> dict[str, Any]:
        selected_regions = [str(x).strip() for x in profile.get("regions", []) or [] if str(x).strip()]
        groups = self._region_query_groups(profile) if selected_regions else [("전국", [None])]
        region_label, variants = groups[0]
        date_windows = build_sale_date_windows(profile, self.cfg)
        first_from, first_to = date_windows[0]
        attempts: list[dict[str, Any]] = []
        selected_result: dict[str, Any] | None = None

        for variant_index, region_codes in enumerate(variants):
            body = build_search_body(
                profile, 1, min(self.page_size, 20), self.cfg,
                region_codes=region_codes,
                sale_from_date=first_from,
                sale_to_date=first_to,
            )
            payload = self._post_json(body)
            data = payload.get("data") if isinstance(payload, dict) else {}
            data = data if isinstance(data, dict) else {}
            rows = data.get("dlt_srchResult") if isinstance(data.get("dlt_srchResult"), list) else []
            page_info = data.get("dma_pageInfo") if isinstance(data.get("dma_pageInfo"), dict) else {}
            total_count = to_int(page_info.get("totalCnt"), len(rows))
            attempt = {
                "code_type": "현행" if variant_index == 0 else "과거코드 병행",
                "region_codes": region_codes or {},
                "first_page_count": len(rows),
                "total_count": total_count,
                "tested_period": f"{first_from.isoformat()}~{first_to.isoformat()}",
            }
            attempts.append(attempt)
            if selected_result is None or total_count > 0 or rows:
                selected_result = {
                    "ok": True,
                    "page_title": self.driver.title if self.driver else "",
                    "cookies": len(self.driver.get_cookies()) if self.driver else 0,
                    "first_page_count": len(rows),
                    "total_count": total_count,
                    "first_case_number": normalize_search_row(rows[0]).case_number if rows else "",
                    "queried_region": region_label,
                    "region_codes": region_codes or {},
                    "code_type": attempt["code_type"],
                    "tested_period": attempt["tested_period"],
                    "full_search_period": (
                        f"{date_windows[0][0].isoformat()}~{date_windows[-1][1].isoformat()}"
                    ),
                    "window_count": len(date_windows),
                    "attempts": list(attempts),
                }
            # 연결 점검은 빠르게 끝내기 위해 결과가 확인되면 중단한다. 실제 검색은
            # 현행/과거 코드를 모두 병행하고 전체 날짜 구간을 조회한다.
            if total_count > 0 or rows:
                break

        return selected_result or {
            "ok": True, "queried_region": region_label, "region_codes": {},
            "first_page_count": 0, "total_count": 0, "first_case_number": "",
            "tested_period": f"{first_from.isoformat()}~{first_to.isoformat()}",
            "full_search_period": f"{date_windows[0][0].isoformat()}~{date_windows[-1][1].isoformat()}",
            "window_count": len(date_windows), "attempts": attempts,
        }

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
            self.warmed_up = False

    def _region_query_groups(
        self, profile: dict[str, Any]
    ) -> list[tuple[str, list[dict[str, str] | None]]]:
        mapping = self.cfg.get("region_code_map", {}) or {}
        groups: list[tuple[str, list[dict[str, str] | None]]] = []
        unresolved: list[str] = []

        for region in profile.get("regions", []) or []:
            key = str(region).strip()
            configured = mapping.get(key)
            canonical_variants = resolve_region_code_variants(key)

            # GUI가 제공하는 알려진 지역은 내장 표를 유일한 기준으로 사용한다.
            # 설정파일의 오래되거나 잘못된 5자리 값이 정확한 내장 코드를 덮어쓰지 못하게 한다.
            if canonical_variants:
                variants = canonical_variants
                if isinstance(configured, dict) and (configured.get("sido") or configured.get("sigungu")):
                    try:
                        normalized_config = normalize_court_region_code_values(configured)
                    except ValueError as exc:
                        logger.warning(
                            "설정파일의 지역코드가 잘못되어 내장 코드를 사용합니다: %s (%s)",
                            key, exc,
                        )
                    else:
                        canonical_primary = canonical_variants[0]
                        if normalized_config != canonical_primary:
                            logger.warning(
                                "설정파일 지역코드(%s)가 내장 표(%s)와 달라 내장 코드를 사용합니다: %s",
                                normalized_config, canonical_primary, key,
                            )
            elif isinstance(configured, dict) and (configured.get("sido") or configured.get("sigungu")):
                # 내장 표에 없는 사용자 정의 지역만 설정값을 허용하되 형식과 접두부를 검증한다.
                try:
                    variants = [normalize_court_region_code_values(configured)]
                except ValueError as exc:
                    logger.warning("사용자 정의 지역코드 무시: %s (%s)", key, exc)
                    variants = []
            else:
                variants = []

            cleaned: list[dict[str, str] | None] = []
            seen: set[tuple[str, str, str]] = set()
            for codes in variants:
                try:
                    normalized = normalize_court_region_code_values(codes)
                except ValueError as exc:
                    logger.warning("법원 지역코드 후보 무시: %s (%s)", key, exc)
                    continue
                marker = (
                    normalized["sido"], normalized["sigungu"], normalized["dong"]
                )
                if marker in seen:
                    continue
                seen.add(marker)
                cleaned.append({"sido": marker[0], "sigungu": marker[1], "dong": marker[2]})

            if cleaned:
                groups.append((key, cleaned))
            else:
                unresolved.append(key)

        if unresolved:
            logger.warning(
                "지역코드가 없는 지역(%s)은 전국검색 후 주소로 필터링합니다.",
                ", ".join(unresolved),
            )
            groups.append((", ".join(unresolved), [None]))
        return groups or [("전국", [None])]

    def _region_queries(self, profile: dict[str, Any]) -> list[dict[str, str] | None]:
        # 기존 테스트/호출과의 호환을 위해 평탄화된 목록도 제공한다.
        return [codes for _, variants in self._region_query_groups(profile) for codes in variants]

    @staticmethod
    def _format_region_codes(region_codes: dict[str, str] | None) -> str:
        if not region_codes:
            return "전국"
        sido = str(region_codes.get("sido", ""))
        sigungu = str(region_codes.get("sigungu", ""))
        dong = str(region_codes.get("dong", ""))
        court_code = str(region_codes.get("court_code", "") or "")
        if court_code:
            return f"법원:{court_code}"
        return "/".join(x for x in (sido, sigungu, dong) if x) or "전국"

    def _ensure_driver(self) -> None:
        if self.driver is not None:
            return
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
        except ImportError as exc:
            raise CourtAuctionSeleniumError(
                "selenium이 설치되지 않았습니다. ./scripts/setup_mac.sh 또는 "
                "pip install selenium을 실행하세요."
            ) from exc

        options = Options()
        if self.headless:
            options.add_argument("--headless=new")
        options.add_argument("--window-size=1440,1000")
        options.add_argument("--lang=ko-KR")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-notifications")
        options.add_argument(
            "--user-agent=" + str(self.cfg.get(
                "user_agent",
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            ))
        )
        chrome_binary = str(self.cfg.get("chrome_binary", "")).strip()
        if chrome_binary:
            options.binary_location = chrome_binary
        user_data_dir = str(self.cfg.get("user_data_dir", "")).strip()
        if user_data_dir:
            options.add_argument(f"--user-data-dir={Path(user_data_dir).expanduser()}")

        driver_path = str(self.cfg.get("driver_path", "")).strip()
        service = Service(executable_path=driver_path) if driver_path else Service()
        try:
            self.driver = webdriver.Chrome(service=service, options=options)
        except Exception as exc:
            raise CourtAuctionSeleniumError(
                "Chrome WebDriver 실행에 실패했습니다. Chrome을 설치하고 Selenium Manager가 "
                "드라이버를 내려받을 수 있는지 확인하세요. 필요하면 대시보드의 "
                "‘수집·운영 설정’에서 Chrome 실행파일/드라이버 경로를 지정하세요. 원인: " + str(exc)
            ) from exc
        self.driver.set_page_load_timeout(self.timeout)
        self.driver.set_script_timeout(self.timeout)

    def _warmup(self) -> None:
        if self.warmed_up:
            return
        warmup_started = time.monotonic()
        self._ensure_driver()
        url = f"{self.base_url}{WARMUP_PATH}"
        try:
            self.driver.get(url)
            deadline = time.monotonic() + max(0.5, self.warmup_wait)
            ready_since: float | None = None
            while time.monotonic() < deadline:
                state = self.driver.execute_script("return document.readyState")
                if state in {"interactive", "complete"}:
                    if not self.adaptive_warmup:
                        break
                    if ready_since is None:
                        ready_since = time.monotonic()
                    if time.monotonic() - ready_since >= max(0.1, self.warmup_settle):
                        break
                else:
                    ready_since = None
                time.sleep(0.1)
            if not self.adaptive_warmup:
                time.sleep(self.warmup_wait)
            page_text = _clean_text(self.driver.page_source)
            if any(x in page_text for x in ("자동입력방지", "보안문자", "접근이 차단", "IP가 차단")):
                self._save_debug("warmup_blocked")
                raise CourtAuctionBlockedError(
                    "법원경매정보 사이트가 자동입력방지 또는 접근차단 화면을 표시했습니다. "
                    "CAPTCHA 우회는 하지 않습니다. 대시보드에서 ‘브라우저 창 표시’를 켜 직접 확인한 뒤 다시 실행하세요."
                )
            self.warmed_up = True
        except CourtAuctionBlockedError:
            raise
        except Exception as exc:
            self._save_debug("warmup_error")
            raise CourtAuctionSeleniumError(f"법원경매 검색화면 접속 실패: {exc}") from exc
        finally:
            self.warmup_elapsed_seconds += time.monotonic() - warmup_started

    def _throttle(self, request_kind: str = "search") -> None:
        if self.last_call_at:
            if request_kind == "detail" and self.fast_mode:
                base_delay = self.detail_min_delay
                jitter = self.detail_jitter
            else:
                base_delay = self.current_search_delay if self.adaptive_throttle else self.min_delay
                jitter = self.jitter
            wait = base_delay + random.random() * max(0.0, jitter)
            elapsed = time.monotonic() - self.last_call_at
            if elapsed < wait:
                actual_wait = wait - elapsed
                time.sleep(actual_wait)
                self.throttle_wait_seconds += actual_wait

    def _recover_search_session(self) -> None:
        """status 0/Failed to fetch 뒤 검색화면 세션을 새로 준비한다."""
        self.warmed_up = False
        try:
            if self.driver is not None:
                self.driver.switch_to.default_content()
        except Exception:
            pass
        # 다음 _post_json()이 공식 검색화면을 다시 열어 쿠키와 WebSquare
        # 실행문맥을 복구하도록 한다. 기존 브라우저는 유지해 불필요한 재기동을 피한다.

    def _post_json(self, body: dict[str, Any]) -> dict[str, Any]:
        cached = self._load_cache(body)
        if cached is not None:
            return cached
        payload = self._post_json_endpoint(
            SEARCH_PATH, body, submission_id=SUBMISSION_ID, debug_name="search"
        )
        self._save_cache(body, payload)
        return payload

    def _post_json_endpoint(
        self,
        endpoint_path: str,
        body: dict[str, Any],
        *,
        submission_id: str = "",
        debug_name: str = "request",
        request_kind: str = "search",
    ) -> dict[str, Any]:
        self._warmup()
        if self.calls_so_far >= self.call_limit:
            raise CourtAuctionSeleniumError(
                f"사이트 보호를 위한 실행당 호출 한도({self.call_limit})를 초과했습니다."
            )
        self._throttle(request_kind=request_kind)
        self.calls_so_far += 1
        self.network_calls += 1
        self.last_call_at = time.monotonic()
        request_started = time.monotonic()

        script = r"""
            const targetUrl = arguments[0];
            const payload = arguments[1];
            const submissionId = arguments[2];
            const done = arguments[arguments.length - 1];
            const headers = {
                'Content-Type': 'application/json;charset=UTF-8',
                'Accept': 'application/json'
            };
            if (submissionId) {
                headers['submissionid'] = submissionId;
                headers['sc-userid'] = 'SYSTEM';
            }
            fetch(targetUrl, {
                method: 'POST',
                credentials: 'same-origin',
                headers: headers,
                body: JSON.stringify(payload)
            }).then(async (response) => {
                const text = await response.text();
                done({status: response.status, ok: response.ok, text: text});
            }).catch((error) => {
                done({status: 0, ok: false, error: String(error)});
            });
        """
        try:
            response = self.driver.execute_async_script(
                script, endpoint_path, body, submission_id
            )
        except Exception as exc:
            self._record_request_failure(request_kind)
            self._save_debug(f"{debug_name}_script_error")
            raise CourtAuctionSeleniumError(f"브라우저 내부 요청 실패: {exc}") from exc
        finally:
            self.request_elapsed_seconds += time.monotonic() - request_started

        self._save_exchange(body, response, debug_name=debug_name)
        if not isinstance(response, dict):
            self._record_request_failure(request_kind)
            self._save_debug(f"{debug_name}_empty_response")
            raise CourtAuctionSeleniumError("법원경매 사이트에서 응답을 받지 못했습니다.")
        if not response.get("ok"):
            self._record_request_failure(request_kind)
            response_text = str(response.get("text", "") or "")
            self._save_debug(f"{debug_name}_http_error", response_text)
            status = to_int(response.get("status"), 0)
            suffix = str(response.get("error", "") or "").strip()
            message = f"법원경매 요청 HTTP 오류: {status}"
            if suffix:
                message += f" {suffix}"
            raise CourtAuctionHttpError(status, message, response_text)
        try:
            payload = json.loads(response.get("text", ""))
        except json.JSONDecodeError as exc:
            self._record_request_failure(request_kind)
            self._save_debug(f"{debug_name}_json_error", response.get("text", ""))
            raise CourtAuctionSeleniumError("법원경매 응답 JSON 해석에 실패했습니다.") from exc

        self._save_exchange(body, response, debug_name=debug_name, payload=payload)
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if isinstance(errors, dict) and errors.get("errorMessage"):
            self._record_request_failure(request_kind)
            self._save_debug(f"{debug_name}_upstream_error", response.get("text", ""))
            raise CourtAuctionSeleniumError(str(errors.get("errorMessage")))
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, dict) and data.get("ipcheck") is False:
            self._record_request_failure(request_kind)
            self._save_debug(f"{debug_name}_blocked", response.get("text", ""))
            raise CourtAuctionBlockedError(
                "법원경매정보 사이트가 현재 IP의 자동조회 요청을 차단했습니다. "
                "자동 재시도하지 말고 최소 1시간 뒤 호출량을 줄여 다시 실행하세요."
            )
        self._record_request_success(request_kind)
        return payload

    def lookup_case(self, case_number: str, profile: dict[str, Any] | None = None) -> dict[str, Any]:
        """선택지역의 관할 법원을 우선해 사건번호를 직접 조회한다.

        물건 조건검색에 노출되지 않는 사건도 사건검색 endpoint에서 존재 여부와
        매각기일 내역을 확인할 수 있어 누락 원인을 분리하는 데 사용한다.
        """
        normalized = _normalize_case_number(case_number)
        if not re.fullmatch(r"\d{4}타경\d+", normalized):
            raise CourtAuctionSeleniumError("사건번호는 예: 2025타경385 형식으로 입력하십시오.")

        # 진단용 호출은 사용자가 버튼을 눌렀을 때만 실행하며 최대 12회로 제한한다.
        self.call_limit = max(self.call_limit, min(self.hard_call_cap, self.calls_so_far + 12))
        court_payload = self._post_json_endpoint(
            COURTS_PATH, {}, debug_name="courts"
        )
        data = court_payload.get("data") if isinstance(court_payload, dict) else {}
        data = data if isinstance(data, dict) else {}
        rows = data.get("result")
        rows = rows if isinstance(rows, list) else []
        courts = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = _first_nonblank(row, "cortOfcCd", "code")
            name = " ".join(x for x in (
                _first_nonblank(row, "cortOfcNm", "name"),
                _first_nonblank(row, "cortSptNm", "branchName"),
            ) if x)
            if code:
                courts.append({"code": code, "name": name.strip()})

        regions = [str(x).strip() for x in (profile or {}).get("regions", []) or [] if str(x).strip()]
        exact_hints: list[str] = []
        province_hints: list[str] = []
        for region in regions:
            exact_hints.extend(REGION_COURT_HINTS.get(region, []))
            province = region.split()[0] if region.split() else region
            province_hints.extend(PROVINCE_COURT_HINTS.get(province, []))

        selected = courts
        if exact_hints:
            exact = [c for c in courts if any(h in c["name"] for h in exact_hints)]
            if exact:
                selected = exact
        if selected is courts and province_hints:
            province_matches = [c for c in courts if any(h in c["name"] for h in province_hints)]
            if province_matches:
                selected = province_matches
        selected = selected[:10]

        attempts: list[dict[str, Any]] = []
        for court in selected:
            payload = self._post_json_endpoint(
                CASE_DETAIL_PATH,
                {"dma_srchCsDtlInf": {"cortOfcCd": court["code"], "csNo": normalized}},
                debug_name="case_detail",
            )
            case_data = payload.get("data") if isinstance(payload, dict) else {}
            case_data = case_data if isinstance(case_data, dict) else {}
            basis = case_data.get("dma_csBasInf")
            attempts.append({"법원": court["name"], "법원코드": court["code"], "발견": bool(basis)})
            if not isinstance(basis, dict):
                continue

            item_rows = case_data.get("dlt_rletCsDspslObjctLst")
            item_rows = item_rows if isinstance(item_rows, list) else []
            items = [{
                "물건번호": _first_nonblank(r, "dspslObjctSeq", "dspslGdsSeq"),
                "소재지": _first_nonblank(r, "userSt", "st", "printSt"),
            } for r in item_rows if isinstance(r, dict)]
            schedules = _extract_case_schedule_rows(case_data)

            today = date.today()
            within_days = int((profile or {}).get("auction_within_days", 0) or 0)
            future_dates = [to_date(r.get("매각기일")) for r in schedules]
            future_dates = [d for d in future_dates if d and d >= today]
            reason = "사건은 확인됐습니다."
            next_sale_date = min(future_dates) if future_dates else None
            if not future_dates:
                reason += " 현재 이후의 매각기일이 없어 물건 조건검색에 나타나지 않을 수 있습니다."
            elif within_days and next_sale_date > today + timedelta(days=within_days):
                reason += f" 다음 매각기일({next_sale_date.isoformat()})이 설정한 {within_days}일 조회범위 밖입니다."
            else:
                reason += f" 다음 매각기일은 {next_sale_date.isoformat()}이며 조회범위 안입니다. 목록검색 분류·입찰방식 또는 최종 필터를 확인해야 합니다."

            return {
                "found": True,
                "사건번호": _first_nonblank(basis, "csNo") or normalized,
                "법원": _first_nonblank(basis, "cortOfcNm") or court["name"],
                "사건명": _first_nonblank(basis, "csNm"),
                "진행상태코드": _first_nonblank(basis, "csProgStatCd"),
                "최종처분코드": _first_nonblank(basis, "ultmtDvsCd"),
                "다음매각기일": next_sale_date.isoformat() if next_sale_date else "",
                "물건내역": items,
                "매각기일내역": schedules,
                "진단": reason,
                "조회시도": attempts,
            }

        return {
            "found": False,
            "사건번호": normalized,
            "진단": "선택지역 관련 법원에서 사건을 찾지 못했습니다. 법원 또는 사건번호를 확인하십시오.",
            "조회시도": attempts,
        }

    def _save_exchange(
        self,
        body: dict[str, Any],
        response: Any,
        *,
        debug_name: str = "search",
        payload: Any | None = None,
    ) -> None:
        """가장 최근 요청/응답을 endpoint별 파일로 저장한다."""
        if not self.save_exchange_debug:
            return
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r"[^0-9A-Za-z_-]+", "_", debug_name or "search")
            request_text = json.dumps(body, ensure_ascii=False, indent=2, default=str)
            response_obj = payload if payload is not None else response
            response_text = json.dumps(response_obj, ensure_ascii=False, indent=2, default=str)
            (self.debug_dir / f"last_{safe_name}_request.json").write_text(
                request_text, encoding="utf-8"
            )
            (self.debug_dir / f"last_{safe_name}_response.json").write_text(
                response_text, encoding="utf-8"
            )
            # 기존 진단 도구와의 호환 파일도 유지한다.
            (self.debug_dir / "last_search_request.json").write_text(
                request_text, encoding="utf-8"
            )
            (self.debug_dir / "last_search_response.json").write_text(
                response_text, encoding="utf-8"
            )
        except Exception:
            logger.debug("최근 검색 요청/응답 저장 실패", exc_info=True)

    def _save_debug(self, name: str, response_text: str = "") -> None:
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            prefix = self.debug_dir / f"{stamp}_{name}"
            if self.driver is not None:
                self.driver.save_screenshot(str(prefix) + ".png")
                Path(str(prefix) + ".html").write_text(
                    self.driver.page_source, encoding="utf-8", errors="ignore"
                )
            if response_text:
                Path(str(prefix) + "_response.txt").write_text(
                    str(response_text), encoding="utf-8", errors="ignore"
                )
        except Exception:
            logger.debug("Selenium 디버그 자료 저장 실패", exc_info=True)
