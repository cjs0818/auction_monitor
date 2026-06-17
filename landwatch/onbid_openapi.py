from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from html import unescape as html_unescape
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

import requests
from requests.adapters import HTTPAdapter

from .filtering import item_matches_region
from .models import AuctionItem
from .providers import BaseProvider, ProviderError, split_region
from .regions import MUNICIPALITY_CODES
from .utils import to_date, to_float, to_int

logger = logging.getLogger(__name__)


class OnbidOpenApiError(ProviderError):
    def __init__(self, message: str, *, code: str = ""):
        super().__init__(message)
        self.code = str(code or "")


# 2026년 차세대 온비드 부동산 물건목록 API
NEXT_BASE_URL = "https://apis.data.go.kr/B010003"
NEXT_LIST_SERVICE = "OnbidRlstListSrvc2"
NEXT_LIST_OPERATION = "getRlstCltrList2"
NEXT_DETAIL_SERVICE = "OnbidRlstDtlSrvc2"
NEXT_DETAIL_OPERATION = "getRlstDtlInf2"

# 재산유형: 압류, 국유, 기타일반, 공유, 금융권담보, 유입, 수탁, 공공개발, 파산
DEFAULT_PROPERTY_DIVISIONS = "0007,0010,0005,0002,0003,0006,0008,0011,0013"


class _SSLContextAdapter(HTTPAdapter):
    """Requests adapter that uses an explicitly supplied SSLContext."""

    def __init__(self, ssl_context: ssl.SSLContext, *args: Any, **kwargs: Any):
        self._ssl_context = ssl_context
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, connections: int, maxsize: int, block: bool = False, **pool_kwargs: Any) -> None:
        pool_kwargs["ssl_context"] = self._ssl_context
        super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: Any):
        proxy_kwargs["ssl_context"] = self._ssl_context
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def _redact_sensitive_url(url: str) -> str:
    """Remove query strings so API keys never appear in errors or logs."""
    try:
        parts = urlsplit(str(url or ""))
        if not parts.scheme or not parts.netloc:
            return str(url or "").split("?", 1)[0]
        return f"{parts.scheme}://{parts.netloc}{parts.path}"
    except Exception:
        return str(url or "").split("?", 1)[0]


def _redact_exception_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"([?&]serviceKey=)[^&\s)'\"]+", r"\1***", text, flags=re.IGNORECASE)
    text = re.sub(r"(serviceKey(?:%3D|=))[^&\s)'\"]+", r"\1***", text, flags=re.IGNORECASE)
    return text


def _build_macos_ca_bundle(output_path: Path) -> Path | None:
    """Export public certificates from macOS Keychains into a PEM bundle.

    The bundle contains certificates only (no private keys). The certifi bundle is
    appended first so ordinary public roots remain available even when a custom
    enterprise root is only present in Keychain.
    """
    if sys.platform != "darwin":
        return None
    security = shutil.which("security") or "/usr/bin/security"
    if not Path(security).exists():
        return None

    chunks: list[bytes] = []
    try:
        import certifi
        certifi_path = Path(certifi.where())
        if certifi_path.exists():
            chunks.append(certifi_path.read_bytes())
    except Exception:
        pass

    commands = [
        [security, "find-certificate", "-a", "-p", "/System/Library/Keychains/SystemRootCertificates.keychain"],
        [security, "find-certificate", "-a", "-p", "/Library/Keychains/System.keychain"],
        [security, "find-certificate", "-a", "-p"],
    ]
    for command in commands:
        try:
            completed = subprocess.run(command, capture_output=True, check=False, timeout=20)
            if completed.returncode == 0 and b"BEGIN CERTIFICATE" in completed.stdout:
                chunks.append(completed.stdout)
        except Exception:
            continue

    if not chunks:
        return None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    combined = b"\n".join(chunk.rstrip() for chunk in chunks if chunk.strip()) + b"\n"
    output_path.write_bytes(combined)
    try:
        output_path.chmod(0o600)
    except OSError:
        pass
    return output_path


def _configure_requests_tls(session: requests.Session, cfg: dict[str, Any]) -> dict[str, Any]:
    """Configure safe TLS trust without ever disabling certificate verification."""
    requested_mode = str(cfg.get("ssl_trust_mode") or "system").strip().lower()
    custom_bundle = str(
        cfg.get("ca_bundle_path")
        or os.getenv("REQUESTS_CA_BUNDLE")
        or os.getenv("CURL_CA_BUNDLE")
        or ""
    ).strip()
    diagnostics: dict[str, Any] = {
        "requested_mode": requested_mode,
        "active_mode": "requests-certifi",
        "ca_bundle_path": "",
        "truststore_available": False,
        "certificate_verification": True,
    }

    if custom_bundle:
        bundle_path = Path(custom_bundle).expanduser().resolve()
        if not bundle_path.is_file():
            raise OnbidOpenApiError(
                f"온비드 SSL CA 인증서 파일을 찾을 수 없습니다: {bundle_path}. "
                "설정의 CA 인증서 번들 경로를 수정하거나 비워 두십시오."
            )
        session.verify = str(bundle_path)
        diagnostics.update(active_mode="custom-ca-bundle", ca_bundle_path=str(bundle_path))
        return diagnostics

    if requested_mode in {"system", "auto", "macos"}:
        try:
            import truststore

            context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            session.mount("https://", _SSLContextAdapter(context))
            diagnostics.update(active_mode="system-truststore", truststore_available=True)
            return diagnostics
        except Exception as exc:
            diagnostics["truststore_error"] = f"{type(exc).__name__}: {exc}"

        # Safe fallback for old/isolated Python builds on macOS.
        bundle = _build_macos_ca_bundle(Path(str(cfg.get("generated_ca_bundle_path") or "data/certs/macos-system-ca.pem")))
        if bundle is not None:
            session.verify = str(bundle.resolve())
            diagnostics.update(active_mode="macos-keychain-bundle", ca_bundle_path=str(bundle.resolve()))
            return diagnostics

    # Requests default: certifi. This remains verified TLS, never verify=False.
    diagnostics["active_mode"] = "requests-certifi"
    return diagnostics


def _clean_tag(tag: str) -> str:
    return str(tag).split("}")[-1]


def _element_to_dict(element: ET.Element) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for child in list(element):
        key = _clean_tag(child.tag)
        value: Any
        if list(child):
            value = _element_to_dict(child)
        else:
            value = (child.text or "").strip()
        if key in result:
            if not isinstance(result[key], list):
                result[key] = [result[key]]
            result[key].append(value)
        else:
            result[key] = value
    return result


def _raise_api_error(code: Any, message: Any) -> None:
    code_text = str(code or "").strip()
    message_text = str(message or "").strip()
    if code_text not in {"", "00", "000", "NORMAL_CODE"}:
        raise OnbidOpenApiError(
            f"온비드 OpenAPI 오류 {code_text}: {message_text or '상세 메시지 없음'}",
            code=code_text,
        )


def _coerce_items(value: Any) -> list[dict[str, Any]]:
    if value in (None, ""):
        return []
    if isinstance(value, dict):
        # body.items.item 또는 바로 item 객체
        if "item" in value:
            return _coerce_items(value.get("item"))
        return [value]
    if isinstance(value, list):
        return [dict(x) for x in value if isinstance(x, dict)]
    return []


def _parse_json_response(payload: Any) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise OnbidOpenApiError("온비드 OpenAPI JSON 응답 형식이 올바르지 않습니다.")
    # 차세대는 header/body가 최상위다. 일부 게이트웨이는 response로 한 번 감싼다.
    root = payload.get("response") if isinstance(payload.get("response"), dict) else payload
    header = root.get("header") if isinstance(root.get("header"), dict) else {}
    _raise_api_error(header.get("resultCode"), header.get("resultMsg"))
    body = root.get("body") if isinstance(root.get("body"), dict) else {}
    items = body.get("items")
    if items is None and "item" in body:
        items = body.get("item")
    rows = _coerce_items(items)
    total_count = to_int(body.get("totalCount"))
    if total_count <= 0:
        total_count = len(rows)
    return rows, total_count, dict(header)


def _parse_xml_response(text: str) -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        preview = re.sub(r"\s+", " ", text[:500])
        raise OnbidOpenApiError(f"온비드 OpenAPI XML 해석 실패: {preview}") from exc

    header: dict[str, Any] = {}
    header_element = root.find(".//header")
    if header_element is not None:
        header = _element_to_dict(header_element)
    _raise_api_error(header.get("resultCode"), header.get("resultMsg"))

    body = root.find(".//body")
    if body is None:
        return [], 0, header
    total_element = body.find("totalCount")
    total_count = to_int(total_element.text if total_element is not None else 0)
    item_elements = body.findall("./items/item") or body.findall("./item")
    rows = [_element_to_dict(x) for x in item_elements]
    if total_count <= 0:
        total_count = len(rows)
    return rows, total_count, header


def _parse_api_response(text: str, content_type: str = "") -> tuple[list[dict[str, Any]], int, dict[str, Any]]:
    stripped = str(text or "").lstrip()
    if "json" in str(content_type).lower() or stripped.startswith(("{", "[")):
        try:
            return _parse_json_response(json.loads(text))
        except json.JSONDecodeError:
            # 공공데이터 오류 응답이 XML로 내려오는 경우가 있어 XML로 다시 시도한다.
            pass
    return _parse_xml_response(text)


def _sanitize_service_key(value: str) -> str:
    key = html_unescape(str(value or "")).strip().strip('"\'')
    key = key.replace("\u200b", "").replace("\ufeff", "")
    key = re.sub(r"\s+", "", key)
    if not key:
        return ""
    if "serviceKey=" in key:
        candidate = key
        if "://" not in candidate:
            candidate = "https://example.invalid/?" + candidate.lstrip("?&")
        try:
            values = parse_qs(urlsplit(candidate).query, keep_blank_values=True).get("serviceKey") or []
            if values and values[0]:
                key = values[0]
        except Exception:
            match = re.search(r"(?:^|[?&])serviceKey=([^&]+)", key)
            if match:
                key = match.group(1)
    return key.strip()


def _decode_service_key(value: str) -> str:
    key = _sanitize_service_key(value)
    if not key:
        return ""
    decoded = key
    for _ in range(3):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def _service_key_candidates(value: str) -> list[dict[str, str]]:
    raw = _sanitize_service_key(value)
    decoded = _decode_service_key(raw)
    if not decoded:
        return []
    encoded = quote(decoded, safe="")
    result = [
        {"label": "디코딩키 자동인코딩", "mode": "params", "value": decoded},
        {"label": "인코딩키 1회전달", "mode": "raw_url", "value": encoded},
    ]
    if "%" in raw and raw != encoded:
        result.append({"label": "입력 인코딩키 원문전달", "mode": "raw_url", "value": raw})
    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in result:
        marker = (item["mode"], item["value"])
        if marker not in seen:
            seen.add(marker)
            deduped.append(item)
    return deduped


def _service_key_fingerprint(value: str) -> str:
    decoded = _decode_service_key(value)
    if not decoded:
        return "미입력"
    digest = hashlib.sha256(decoded.encode("utf-8")).hexdigest()[:10]
    return f"길이 {len(decoded)}, SHA256 {digest}"


def _normalize_service_key(value: str) -> str:
    return _decode_service_key(value)


def _parse_onbid_datetime(value: Any) -> date | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) >= 8:
        try:
            return datetime.strptime(digits[:8], "%Y%m%d").date()
        except ValueError:
            return None
    return to_date(value)


def _parse_area(value: Any) -> float:
    text = str(value or "")
    if not text:
        return 0.0
    candidates: list[float] = []
    for match in re.finditer(r"([0-9][0-9,]*(?:\.\d+)?)\s*(?:㎡|m2|M2|제곱미터)", text):
        candidates.append(to_float(match.group(1)))
    if candidates:
        return max(candidates)
    return to_float(text)


def _parse_money(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "비공개"}:
        return 0
    # 억/만원 단위 안내문구도 처리한다.
    total = 0.0
    matched_unit = False
    for number, unit in re.findall(r"([0-9]+(?:\.\d+)?)\s*(억|만)", text):
        matched_unit = True
        total += float(number) * (100_000_000 if unit == "억" else 10_000)
    if matched_unit:
        return int(total)
    match = re.search(r"[0-9]+(?:\.\d+)?", text)
    return int(float(match.group(0))) if match else 0




def _parse_onbid_round_price(row: dict[str, Any], appraisal_price: int) -> tuple[int, str]:
    """현재 입찰회차와 같은 행에 있는 최저입찰가를 고른다.

    차세대 목록은 회차별 행을 반환한다. 따라서 다른 회차의 가격을 계산하거나
    최초입찰가를 무조건 쓰지 않고, 선택된 행의 명시적 최저가 → 안내문구 →
    최초입찰가 → 감정가 대비 비율 순으로 해석한다.
    """
    explicit_fields = (
        "lowstBidPrc", "lowstBidAmt", "minBidPrc", "minBidAmt",
        "MIN_BID_PRC", "LWS_BID_PRC",
    )
    for key in explicit_fields:
        price = _parse_money(row.get(key))
        if price > 0:
            return price, key

    guide = _first(row, "lowstBidPrcIndctCont", "LOWST_BID_PRC_INDCT_CONT")
    guide_price = _parse_money(guide)
    # 비율(예: 80%)이나 회차 숫자를 가격으로 잘못 읽는 것을 막는다.
    if guide_price >= 10_000:
        return guide_price, "lowstBidPrcIndctCont"

    first_price = _parse_money(_first(row, "frstBidPrc", "FRST_BID_PRC"))
    if first_price > 0:
        return first_price, "frstBidPrc"

    ratio = to_float(_first(row, "apslPrcCtrsLowstBidRto", "APSL_PRC_CTRS_LOWST_BID_RTO"), 0.0)
    if appraisal_price > 0 and ratio > 0:
        computed = int(round(appraisal_price * ratio / 100.0))
        if computed > 0:
            return computed, "apslPrcCtrsLowstBidRto 계산"
    return 0, "미확인"


def _first(row: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def _normalize_usage(raw_usage: str, goods_name: str = "", title: str = "") -> str:
    text = " / ".join(x for x in (raw_usage, goods_name, title) if x)
    aliases = {
        "대지": "대", "전": "전", "답": "답", "과수원": "과수원", "목장용지": "목장용지",
        "임야": "임야", "광천지": "광천지", "염전": "염전", "공장용지": "공장용지",
        "학교용지": "학교용지", "주차장": "주차장", "주유소용지": "주유소용지",
        "창고용지": "창고용지", "도로": "도로", "철도용지": "철도용지", "제방": "제방",
        "하천": "하천", "구거": "구거", "유지": "유지", "양어장": "양어장",
        "수도용지": "수도용지", "공원": "공원", "체육용지": "체육용지", "유원지": "유원지",
        "종교용지": "종교용지", "사적지": "사적지", "묘지": "묘지", "잡종지": "잡종지",
    }
    for token in sorted(aliases, key=len, reverse=True):
        if re.search(rf"(?:^|[/,\s(]){re.escape(token)}(?:$|[/,\s)0-9])", text):
            return aliases[token]
    if "토지" in text:
        return "토지"
    return raw_usage.strip() or "토지"


def _normalize_status(raw_status: str, failed_count: int) -> str:
    text = str(raw_status or "").strip()
    if "수의" in text:
        return "수의계약"
    if "유찰" in text or failed_count > 0:
        return "유찰"
    if "마감" in text or "종료" in text or "낙찰" in text or "취소" in text:
        return "입찰마감"
    if "준비" in text:
        return "신건"
    return "진행"


def _normalize_pnu(value: Any) -> str:
    """Return a 19-digit parcel number (PNU) when the value is valid."""
    digits = re.sub(r"\D", "", str(value or ""))
    return digits if len(digits) == 19 else ""


def _parcel_from_pnu(value: Any) -> str:
    """Decode the mountain flag, main lot and sub-lot from a 19-digit PNU.

    PNU layout: 10-digit legal-dong code + 1-digit land flag +
    4-digit main lot + 4-digit sub-lot.  The land flag is ``2`` for
    mountain parcels and ``1`` for ordinary parcels.
    """
    pnu = _normalize_pnu(value)
    if not pnu:
        return ""
    land_flag = pnu[10]
    main_no = int(pnu[11:15])
    sub_no = int(pnu[15:19])
    if main_no <= 0:
        return ""
    prefix = "산 " if land_flag == "2" else ""
    return f"{prefix}{main_no}-{sub_no}" if sub_no > 0 else f"{prefix}{main_no}"


def _normalize_title_text(title: str) -> str:
    text = re.sub(r"[(),\[\]{}]", " ", str(title or ""))
    return re.sub(r"\s+", " ", text).strip()


def _extract_address_from_title(title: str) -> str:
    """Extract a cadastral address, including an optional legal-ri name.

    Rural Onbid titles commonly use ``면 + 리 + 지번`` (for example,
    ``변산면 도청리 100-2``).  The previous expression accepted only one
    locality token before the parcel number, so it stopped at ``변산면`` and
    failed to retain ``도청리``.  The locality expression below explicitly
    accepts both ``읍/면 + 리`` and ordinary ``동/가/리`` addresses.
    """
    text = _normalize_title_text(title)
    municipality = r"\S+(?:시|군|구)(?:\s+\S+구)?"
    locality = r"(?:\S+(?:읍|면)\s+\S+리|\S+(?:동|가|리|읍|면))"
    parcel = r"(?:산\s*)?\d+(?:-\d+)?"
    patterns = [
        re.compile(
            rf"(?P<addr>\S+(?:특별시|광역시|특별자치시|특별자치도|도)\s+"
            rf"{municipality}\s+{locality}\s+{parcel})"
        ),
        re.compile(rf"(?P<addr>{municipality}\s+{locality}\s+{parcel})"),
        # 제목이 읍·면부터 시작하는 축약형도 허용한다.
        re.compile(rf"(?P<addr>\S+(?:읍|면)\s+\S+리\s+{parcel})"),
    ]
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group("addr").strip()
    return ""


def _extract_legal_village_from_title(title: str, town_name: str = "") -> str:
    """Return the legal-ri token from an Onbid title when available.

    The list API exposes a PNU and usually an 읍/면 name, but not a separate
    legal-ri field.  Even when the title omits the parcel number, it frequently
    contains ``읍/면 + 리``.  Combining that ri name with the parcel decoded
    from PNU produces an address suitable for map lookup without another API.
    """
    text = _normalize_title_text(title)
    town = str(town_name or "").strip()
    if town:
        match = re.search(
            rf"(?:^|\s){re.escape(town)}\s+(?P<village>[^\s,;/]+리)(?=\s|$)",
            text,
        )
        if match:
            return match.group("village").strip()

    # Fallback only when a rural 읍/면 and a following 리 are clearly adjacent.
    match = re.search(r"(?:^|\s)\S+(?:읍|면)\s+(?P<village>[^\s,;/]+리)(?=\s|$)", text)
    return match.group("village").strip() if match else ""


def _build_onbid_address(row: dict[str, Any], title: str) -> tuple[str, str, str]:
    """Build the most precise address available from list/detail data.

    The next-generation list API commonly exposes only province/city/town
    names, but also includes ``ltnoPnu``.  We decode the exact lot number
    from that PNU, avoiding an additional detail-API approval and request.
    """
    # Prefer cadastral/lot address over road address for land investment.
    direct_lot = str(_first(row, "zadrNm", "LDNM_ADRS")).strip()
    if direct_lot:
        return direct_lot, "상세 지번주소", _parcel_from_pnu(_first(row, "ltnoPnu", "LTNO_PNU"))

    title_address = _extract_address_from_title(title)
    if title_address:
        return title_address, "물건명 지번주소", _parcel_from_pnu(_first(row, "ltnoPnu", "LTNO_PNU"))

    province_name = str(_first(row, "lctnSdnm")).strip()
    municipality_name = str(_first(row, "lctnSggnm")).strip()
    town_name = str(_first(row, "lctnEmdNm")).strip()
    base = " ".join(x for x in (province_name, municipality_name, town_name) if x).strip()
    parcel = _parcel_from_pnu(_first(row, "ltnoPnu", "LTNO_PNU"))
    village_name = _extract_legal_village_from_title(title, town_name)
    if base and village_name and parcel:
        return f"{base} {village_name} {parcel}", "물건명 법정리 + PNU 지번복원", parcel
    if base and parcel:
        return f"{base} {parcel}", "PNU 지번복원", parcel

    road_address = str(_first(row, "cltrRadr", "NMRD_ADRS")).strip()
    if road_address:
        return road_address, "도로명주소", parcel
    return base, "행정구역주소", parcel


ONBID_REAL_ESTATE_SEARCH_URL = (
    "https://www.onbid.co.kr/op/cta/cltrdtl/collateralDetailRealEstateList.do"
)


def _safe_explicit_onbid_url(row: dict[str, Any]) -> str:
    """Return an explicit Onbid web URL only when the API actually supplied it.

    The next-generation OpenAPI identifiers are API lookup keys and are not the
    legacy web page's internal identifiers.  Reconstructing the old detail URL
    from them can therefore open Onbid's 'page not found' screen.
    """
    for key in (
        "onbidWebUrl", "onbidUrl", "detailUrl", "cltrDtlUrl",
        "WEB_URL", "DETAIL_URL", "LINK_URL",
    ):
        value = str(row.get(key) or "").strip()
        if not value:
            continue
        try:
            parts = urlsplit(value)
        except Exception:
            continue
        host = (parts.hostname or "").lower()
        if parts.scheme in {"http", "https"} and (host == "onbid.co.kr" or host.endswith(".onbid.co.kr")):
            return value
    return ""


def _detail_url(row: dict[str, Any]) -> str:
    """Build a reliable Onbid web destination.

    New Onbid list/detail APIs expose ``cltrMngNo`` and ``pbctCdtnNo`` for API
    detail lookup.  They do *not* guarantee the legacy website-only identifiers
    required by ``collateralRealEstateDetail.do``.  Unless an explicit web URL
    is supplied, open the current official real-estate search page and pass the
    management number as search hints.  This avoids broken fabricated links and
    lets the user verify the exact item by its stable management number.
    """
    explicit = _safe_explicit_onbid_url(row)
    if explicit:
        return explicit

    management_no = str(
        _first(row, "cltrMngNo", "CLTR_MNMT_NO", "cltrMnmtNo")
    ).strip()
    if not management_no:
        return ONBID_REAL_ESTATE_SEARCH_URL

    # ``searchCltrMnmtNo`` is retained for compatibility with the existing
    # Onbid search form; unknown query values are harmless and the official
    # list page still opens even when Onbid changes its form implementation.
    params = urlencode({
        "searchCltrMnmtNo": management_no,
        "cltrMnmtNo": management_no,
    })
    return f"{ONBID_REAL_ESTATE_SEARCH_URL}?{params}"


def normalize_onbid_row(row: dict[str, Any]) -> AuctionItem:
    """차세대 camelCase와 구 API 대문자 필드를 모두 정규화한다."""
    plnm_no = str(_first(row, "onbidPbancNo", "PLNM_NO", "pbancMngNo", "pbctNo", "PBCT_NO")).strip()
    pbct_no = str(_first(row, "pbctNo", "PBCT_NO")).strip()
    cltr_no = str(_first(row, "onbidCltrno", "CLTR_NO")).strip()
    condition_no = str(
        _first(row, "pbctCdtnNo", "PBCT_CDTN_NO", default="0")
    ).strip()
    cltr_history_no = str(_first(row, "cltrHstrNo", "CLTR_HSTR_NO")).strip()
    management_no = str(_first(row, "cltrMngNo", "CLTR_MNMT_NO", default=cltr_no)).strip()

    title = str(_first(row, "onbidCltrNm", "CLTR_NM")).strip()
    province_name = str(_first(row, "lctnSdnm")).strip()
    municipality_name = str(_first(row, "lctnSggnm")).strip()
    town_name = str(_first(row, "lctnEmdNm")).strip()
    address, address_source, parcel_from_pnu = _build_onbid_address(row, title)
    province, city_county = split_region(address)
    province = province or province_name
    city_county = city_county or municipality_name

    bid_round = to_int(_first(row, "bidPrgnNft", "PBCT_SEQ", "PBCT_DGR"))
    failed_count = to_int(_first(row, "USCBD_CNT"))
    if failed_count <= 0 and bid_round > 0:
        failed_count = max(0, bid_round - 1)

    raw_status = str(_first(row, "pbctStatNm", "PBCT_CLTR_STAT_NM")).strip()
    goods_name = str(_first(row, "GOODS_NM", "dtlCltrNm", "sqmsCont"))
    usage_text = " / ".join(
        str(x).strip() for x in (
            _first(row, "cltrUsgLclsCtgrNm"),
            _first(row, "cltrUsgMclsCtgrNm"),
            _first(row, "cltrUsgSclsCtgrNm"),
            _first(row, "CTGR_FULL_NM"),
        ) if str(x).strip()
    )
    usage = _normalize_usage(usage_text, goods_name, title)
    land_area = _parse_area(_first(row, "landSqms", "LAND_SQMS")) or _parse_area(goods_name)
    building_area = _parse_area(_first(row, "bldSqms", "BLD_SQMS"))
    appraisal_price = _parse_money(_first(row, "apslEvlAmt", "APSL_ASES_AVG_AMT"))
    min_price, min_price_source = _parse_onbid_round_price(row, appraisal_price)

    special = [
        str(x).strip() for x in (
            _first(row, "prptDivNm", "PRPT_DVSN_NM"),
            _first(row, "dspsMthodNm", "DPSL_MTD_NM"),
            _first(row, "bidMthodNm", "BID_MTD_NM"),
            _first(row, "bidDivNm"),
            _first(row, "lowstBidPrcIndctCont"),
            _first(row, "icdlCdtnCont"),
            _first(row, "utlzPscdCont", "UTLZ_PSCD"),
            _first(row, "locVntyPscdCont", "POSI_ENV_PSCD"),
            _first(row, "pytnMtrsCont"),
        ) if str(x).strip()
    ]

    # 기존 상세화면과의 호환을 위한 별칭도 raw에 함께 저장한다.
    compatible_raw = dict(row)
    compatible_raw.update({
        "PLNM_NO": plnm_no,
        "PBCT_NO": pbct_no,
        "CLTR_NO": cltr_no,
        "CLTR_MNMT_NO": management_no,
        "PBCT_CLTR_STAT_NM": raw_status,
        "ORG_NM": str(_first(row, "orgNm", "rqstOrgNm", "ORG_NM")),
        "DPSL_MTD_NM": str(_first(row, "dspsMthodNm", "DPSL_MTD_NM")),
        "BID_MTD_NM": str(_first(row, "bidMthodNm", "BID_MTD_NM")),
        "PBCT_CLS_DTM": str(_first(row, "cltrBidEndDt", "PBCT_CLS_DTM")),
        "source_type": "공매",
        "raw_status": raw_status,
        "onbid_api_generation": "차세대",
        "address_source": address_source,
        "parcel_from_pnu": parcel_from_pnu,
        "legal_village_from_title": _extract_legal_village_from_title(title, town_name),
        "LTNO_PNU": str(_first(row, "ltnoPnu", "LTNO_PNU")),
        "CLTR_HSTR_NO": cltr_history_no,
        "PBCT_CDTN_NO": condition_no,
        "onbid_round_id": condition_no or pbct_no or "0",
        "onbid_pbct_cdtn_no": condition_no,
        "onbid_cltr_hstr_no": cltr_history_no,
        "onbid_price_source": min_price_source,
        "onbid_selected_round_end": str(_first(row, "cltrBidEndDt", "PBCT_CLS_DTM")),
        "onbid_link_mode": (
            "explicit-web-detail" if _safe_explicit_onbid_url(row)
            else "management-number-search"
        ),
        "onbid_search_management_no": management_no,
    })

    return AuctionItem(
        # 물건관리번호는 물건 자체의 안정적인 식별자다. pbctCdtnNo는 동일 물건의
        # 입찰 회차/조건 번호이므로 auction_id에 포함하면 한 필지가 회차별로 중복된다.
        auction_id=f"onbid:{management_no or cltr_no}",
        sale_type="공매",
        source_name="한국자산관리공사 차세대 온비드",
        case_number=plnm_no or pbct_no,
        item_number=management_no or cltr_no,
        court=str(_first(row, "orgNm", "rqstOrgNm", "ORG_NM", default="한국자산관리공사(온비드)")),
        status=_normalize_status(raw_status, failed_count),
        usage=usage,
        address=address,
        province=province,
        city_county=city_county,
        min_price=min_price,
        appraisal_price=appraisal_price,
        failed_count=failed_count,
        land_area_m2=land_area,
        building_area_m2=building_area,
        auction_date=_parse_onbid_datetime(_first(row, "cltrBidEndDt", "PBCT_CLS_DTM")),
        special_conditions=list(dict.fromkeys(special)),
        detail_url=_detail_url(row),
        raw=compatible_raw,
    )


_ONBID_TERMINAL_STATUS_WORDS = ("낙찰", "취소", "종료", "마감", "매각완료")


def _onbid_management_key(item: AuctionItem) -> str:
    """Return a stable property key, independent of auction round/condition."""
    raw = item.raw or {}
    management_no = str(
        _first(raw, "cltrMngNo", "CLTR_MNMT_NO", default=item.item_number)
    ).strip()
    if management_no:
        return management_no
    return str(item.item_number or item.auction_id or "").strip()


def _onbid_schedule_entry(item: AuctionItem) -> dict[str, Any]:
    raw = item.raw or {}
    return {
        "물건관리번호": item.item_number,
        "공고번호": item.case_number,
        "공매조건번호": str(_first(raw, "pbctCdtnNo", "PBCT_CDTN_NO", "onbid_pbct_cdtn_no")),
        "회차": str(_first(raw, "pbctNsq", "pbctsn", "bidPrgnNft", "PBCT_SEQ", "PBCT_DGR")),
        "진행상태": item.status,
        "입찰마감일": item.auction_date.isoformat() if item.auction_date else "",
        "최저입찰가": int(item.min_price or 0),
        "감정평가액": int(item.appraisal_price or 0),
    }


def _onbid_schedule_rank(item: AuctionItem, today: date | None = None) -> tuple[Any, ...]:
    """Rank duplicate round rows so one currently actionable row represents the property.

    1) non-terminal rows with today/future closing date, 2) any future row,
    3) non-terminal past row, 4) terminal past row. Within a bucket, the
    nearest date wins. This selects the 2026-06-17 active row rather than
    pre-announced 06-24/07-01/07-08 future price steps for the same property.
    """
    today = today or date.today()
    auction_date = item.auction_date
    status = str(item.status or "")
    terminal = any(word in status for word in _ONBID_TERMINAL_STATUS_WORDS)
    future = auction_date is not None and auction_date >= today
    if future and not terminal:
        bucket = 0
    elif future:
        bucket = 1
    elif not terminal:
        bucket = 2
    else:
        bucket = 3

    if auction_date is None:
        distance = 999_999
        date_tiebreak = "9999-12-31"
    else:
        distance = abs((auction_date - today).days)
        date_tiebreak = auction_date.isoformat()

    # 진행 중인 행을 신건/준비 행보다 우선한다.
    if "진행" in status or "입찰" in status:
        status_rank = 0
    elif "신건" in status or "준비" in status or "공고" in status:
        status_rank = 1
    elif "유찰" in status:
        status_rank = 2
    else:
        status_rank = 3

    round_no = to_int(_first(item.raw or {}, "pbctCdtnNo", "PBCT_CDTN_NO", "pbctNsq", "bidPrgnNft"))
    return (bucket, distance, status_rank, date_tiebreak, -round_no)


def _merge_onbid_round_items(
    current: AuctionItem | None,
    candidate: AuctionItem,
    *,
    today: date | None = None,
) -> AuctionItem:
    """Merge rows that differ only by Onbid auction round into one property."""
    if current is None:
        selected = candidate
        entries = [_onbid_schedule_entry(candidate)]
    else:
        selected = min((current, candidate), key=lambda x: _onbid_schedule_rank(x, today))
        entries = []
        for source in (current, candidate):
            old_entries = (source.raw or {}).get("ONBID_SCHEDULE_ROWS")
            if isinstance(old_entries, list):
                entries.extend(x for x in old_entries if isinstance(x, dict))
            else:
                entries.append(_onbid_schedule_entry(source))

    unique_entries: dict[tuple[Any, ...], dict[str, Any]] = {}
    for entry in entries:
        key = (
            str(entry.get("공매조건번호") or ""),
            str(entry.get("입찰마감일") or ""),
            int(entry.get("최저입찰가") or 0),
            str(entry.get("진행상태") or ""),
        )
        unique_entries[key] = entry
    schedules = sorted(
        unique_entries.values(),
        key=lambda x: (str(x.get("입찰마감일") or "9999-12-31"), int(x.get("최저입찰가") or 0)),
    )

    selected.raw = dict(selected.raw or {})
    selected.raw["ONBID_SCHEDULE_ROWS"] = schedules
    selected.raw["onbid_merged_round_count"] = len(schedules)
    selected.raw["onbid_duplicate_rows_merged"] = max(0, len(schedules) - 1)
    selected.raw["onbid_selected_round_reason"] = "현재 또는 가장 가까운 유효 입찰마감일 우선"
    selected.auction_id = f"onbid:{_onbid_management_key(selected)}"
    return selected


class OnbidOpenApiProvider(BaseProvider):
    """공공데이터포털 차세대 온비드 부동산 물건목록 공급자."""

    SERVICE_PATH = NEXT_LIST_SERVICE
    LIST_OPERATION = NEXT_LIST_OPERATION
    DETAIL_SERVICE_PATH = NEXT_DETAIL_SERVICE
    DETAIL_OPERATION = NEXT_DETAIL_OPERATION

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = dict(cfg or {})
        configured_base = str(self.cfg.get("base_url") or "").rstrip("/")
        configured_service = str(self.cfg.get("service_path") or "").strip("/")
        self.legacy_config_migrated = (
            not configured_base
            or "openapi.onbid.co.kr" in configured_base
            or configured_service in {"", "ThingInfoInquireSvc"}
        )
        # 기존 config.yaml에 구 API가 남아 있어도 차세대 승인키와 일치하도록 자동 전환한다.
        self.base_url = NEXT_BASE_URL if self.legacy_config_migrated else configured_base
        self.service_path = NEXT_LIST_SERVICE if self.legacy_config_migrated else configured_service
        self.list_operation = str(self.cfg.get("list_operation") or NEXT_LIST_OPERATION)
        if self.list_operation in {"getUnifyUsageCltr", "getRlstCltrList"}:
            self.list_operation = NEXT_LIST_OPERATION
        self.detail_service_path = str(self.cfg.get("detail_service_path") or NEXT_DETAIL_SERVICE)
        self.detail_operation = str(self.cfg.get("detail_operation") or NEXT_DETAIL_OPERATION)
        self.detail_enabled = bool(self.cfg.get("detail_enabled", False))

        raw_key = str(self.cfg.get("service_key") or os.getenv("KAMCO_API_KEY") or "")
        if raw_key.startswith("${") and raw_key.endswith("}"):
            raw_key = os.getenv(raw_key[2:-1], "")
        self.raw_service_key = _sanitize_service_key(raw_key)
        self.service_key = _normalize_service_key(self.raw_service_key)
        self.key_fingerprint = _service_key_fingerprint(self.raw_service_key)
        self.last_key_attempts: list[dict[str, Any]] = []
        self.timeout = int(self.cfg.get("timeout_seconds", 30))
        self.page_size = max(10, min(1000, int(self.cfg.get("page_size", 100))))
        self.max_pages = max(1, min(100, int(self.cfg.get("max_pages", 10))))
        self.cache_enabled = bool(self.cfg.get("cache_enabled", True))
        self.cache_ttl_minutes = max(1, int(self.cfg.get("cache_ttl_minutes", 15)))
        self.cache_dir = Path(str(self.cfg.get("cache_dir", "data/onbid_cache_v2")))
        self.property_divisions = str(
            self.cfg.get("property_division_codes") or DEFAULT_PROPERTY_DIVISIONS
        )
        self.private_contract_target = str(self.cfg.get("private_contract_target") or "N")
        self.force_land_category = bool(self.cfg.get("force_land_category", False))
        self.session = requests.Session()
        self.ssl_diagnostics = _configure_requests_tls(self.session, self.cfg)
        self.last_fetch_diagnostics: list[dict[str, Any]] = []
        self.last_fetch_summary: dict[str, Any] = {}
        self._request_count = 0
        self._cache_hits = 0
        self._request_seconds = 0.0

    def _require_key(self) -> None:
        if not self.service_key:
            raise OnbidOpenApiError(
                "공매 검색에는 공공데이터포털의 ‘차세대 온비드 부동산 물건목록 조회서비스’ "
                "인증키가 필요합니다. 대시보드의 ‘수집·알림 설정 → 온비드 공매 설정’에서 입력하십시오."
            )

    def _cache_path(self, service_path: str, operation: str, params: dict[str, Any]) -> Path:
        payload = json.dumps(
            {"service": service_path, "operation": operation, "params": params},
            sort_keys=True,
            ensure_ascii=False,
        )
        return self.cache_dir / f"{hashlib.sha256(payload.encode('utf-8')).hexdigest()}.json"

    def _request(
        self,
        operation: str,
        params: dict[str, Any],
        *,
        service_path: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        self._require_key()
        service_path = str(service_path or self.service_path).strip("/")
        clean_params = {k: v for k, v in params.items() if v not in (None, "")}
        cache_path = self._cache_path(service_path, operation, clean_params)
        if self.cache_enabled and cache_path.exists():
            age = time.time() - cache_path.stat().st_mtime
            if age <= self.cache_ttl_minutes * 60:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                self._cache_hits += 1
                return list(payload.get("rows") or []), int(payload.get("total_count") or 0)

        url = f"{self.base_url}/{service_path}/{operation}"
        candidates = _service_key_candidates(self.raw_service_key)
        self.last_key_attempts = []
        last_error: Exception | None = None

        for candidate in candidates:
            started = time.monotonic()
            try:
                if candidate["mode"] == "raw_url":
                    query = urlencode(clean_params, doseq=True)
                    request_url = f"{url}?{query}&serviceKey={candidate['value']}"
                    response = self.session.get(request_url, timeout=self.timeout)
                else:
                    request_params = dict(clean_params)
                    request_params["serviceKey"] = candidate["value"]
                    response = self.session.get(url, params=request_params, timeout=self.timeout)
                response.raise_for_status()
                self._request_count += 1
                try:
                    rows, total_count, header = _parse_api_response(
                        response.text,
                        str(getattr(response, "headers", {}).get("content-type", "")),
                    )
                except OnbidOpenApiError as exc:
                    self.last_key_attempts.append({
                        "전송방식": candidate["label"],
                        "결과코드": exc.code or "응답오류",
                        "결과메시지": str(exc),
                    })
                    last_error = exc
                    if exc.code == "30":
                        continue
                    raise

                self.last_key_attempts.append({
                    "전송방식": candidate["label"],
                    "결과코드": str(header.get("resultCode") or "00"),
                    "결과메시지": str(header.get("resultMsg") or "정상"),
                })
                if self.cache_enabled:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(
                        json.dumps({"rows": rows, "total_count": total_count}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                return rows, total_count
            except requests.exceptions.SSLError as exc:
                last_error = exc
                safe_url = _redact_sensitive_url(url)
                mode = str(self.ssl_diagnostics.get("active_mode") or "unknown")
                bundle = str(self.ssl_diagnostics.get("ca_bundle_path") or "미지정")
                self.last_key_attempts.append({
                    "전송방식": candidate["label"],
                    "결과코드": "SSL",
                    "결과메시지": f"TLS 인증서 검증 실패 (방식={mode})",
                })
                raise OnbidOpenApiError(
                    "온비드 서버와의 HTTPS 연결에서 인증서 검증에 실패했습니다. "
                    "이는 인증키나 온비드 API 승인 문제가 아니라 이 Mac의 인증서 신뢰 저장소 또는 "
                    "VPN·보안프로그램·기관 프록시의 TLS 검사 인증서 문제입니다. "
                    f"호출={safe_url}, TLS방식={mode}, CA번들={bundle}. "
                    "수정본은 macOS 시스템 키체인을 우선 사용하며, 계속 실패하면 설정의 "
                    "‘사용자 CA 인증서 번들 경로’에 기관 루트 인증서 PEM 파일을 지정하십시오. "
                    "보안을 위해 SSL 검증을 끄는 방식은 사용하지 않습니다."
                ) from exc
            except requests.RequestException as exc:
                last_error = exc
                safe_url = _redact_sensitive_url(url)
                self.last_key_attempts.append({
                    "전송방식": candidate["label"],
                    "결과코드": "HTTP",
                    "결과메시지": f"{type(exc).__name__}: {_redact_exception_text(exc)}",
                })
                raise OnbidOpenApiError(
                    f"온비드 OpenAPI 호출 실패: {safe_url}: {_redact_exception_text(exc)}"
                ) from exc
            finally:
                self._request_seconds += time.monotonic() - started

        attempts = ", ".join(
            f"{x.get('전송방식')}={x.get('결과코드')}" for x in self.last_key_attempts
        ) or "시도 없음"
        raise OnbidOpenApiError(
            "온비드 OpenAPI 오류 30: SERVICE KEY IS NOT REGISTERED ERROR. "
            "차세대 온비드 승인키를 두 전송방식으로 시험했지만 인증 서버가 거부했습니다. "
            f"키 진단({self.key_fingerprint}), 호출 서비스={service_path}/{operation}, 시도={attempts}. "
            "공공데이터포털에서 ‘한국자산관리공사_차세대 온비드 부동산 물건목록 조회서비스’가 "
            "승인 상태인지 확인하십시오. 이 프로그램이 사용해야 하는 엔드포인트는 "
            "https://apis.data.go.kr/B010003/OnbidRlstListSrvc2/getRlstCltrList2 입니다.",
            code="30",
        ) from last_error

    def _region_variants(self, region: str) -> list[tuple[str, str, str]]:
        province, municipality = split_region(region)
        variants = [(province, municipality, "현행")]
        if province == "전북특별자치도":
            variants.append(("전라북도", municipality, "과거명칭 호환"))
        elif province == "강원특별자치도":
            variants.append(("강원도", municipality, "과거명칭 호환"))
        return variants

    def _build_params(
        self,
        profile: dict[str, Any],
        province: str,
        municipality: str,
        page: int,
    ) -> dict[str, Any]:
        appraisal = profile.get("appraisal_price", {}) or {}
        params: dict[str, Any] = {
            "pageNo": page,
            "numOfRows": self.page_size,
            "resultType": "json",
            "prptDivCd": self.property_divisions,
            "pvctTrgtYn": self.private_contract_target,
            "dspsMthodCd": "0001",  # 매각
            "cltrUsgLclsCtgrId": "10000",  # 부동산
            "lctnSdnm": province,
            "lctnSggnm": municipality,
            # 차세대 공식 목록 API가 지원하는 감정가 범위
            "apslEvlAmtFrom": int(appraisal.get("min") or 0) or "",
            "apslEvlAmtTo": int(appraisal.get("max") or 0) or "",
        }
        # 토지 중분류 ID는 포털 코드표가 변동될 수 있어 기본은 로컬 용도 필터로 처리한다.
        if self.force_land_category:
            params["cltrUsgMclsCtgrId"] = str(self.cfg.get("land_category_id") or "10100")
        return params

    def fetch(self, profile: dict[str, Any]) -> list[AuctionItem]:
        started = time.monotonic()
        self.last_fetch_diagnostics = []
        self._request_count = self._cache_hits = 0
        self._request_seconds = 0.0
        regions = list(profile.get("regions") or [""])
        unique: dict[str, AuctionItem] = {}

        for region in regions:
            target_province, target_municipality = split_region(str(region)) if region else ("", "")
            province_only = bool(target_province and not target_municipality)
            # 시·도 단독 조건은 온비드 API가 0건 또는 일부 결과만 반환하는 사례가 있어
            # 신뢰하지 않는다. 시·도 전체는 GUI의 시·군·구 개별검색과 동일한 요청을
            # 전수 실행해 병합한다.
            variants = [] if province_only else (
                self._region_variants(str(region)) if region else [("", "", "전국")]
            )
            region_found = 0
            for province, municipality, variant_label in variants:
                variant_found = total_seen = pages_done = 0
                region_mismatch = 0
                duplicate_rounds_merged = 0
                for page in range(1, self.max_pages + 1):
                    rows, total_count = self._request(
                        self.list_operation,
                        self._build_params(profile, province, municipality, page),
                    )
                    pages_done += 1
                    total_seen = max(total_seen, total_count)
                    if not rows:
                        break
                    for raw in rows:
                        item = normalize_onbid_row(raw)
                        if region and not item_matches_region(item, str(region)):
                            region_mismatch += 1
                            continue
                        property_key = _onbid_management_key(item)
                        previous = unique.get(property_key)
                        if previous is not None:
                            duplicate_rounds_merged += 1
                        unique[property_key] = _merge_onbid_round_items(previous, item)
                        variant_found += 1
                    if len(rows) < self.page_size:
                        break
                    if total_count and page >= math.ceil(total_count / self.page_size):
                        break
                region_found += variant_found
                self.last_fetch_diagnostics.append({
                    "검색대상": "공매",
                    "검색방식": "차세대 온비드 지역우선검색",
                    "지역": region or "전국",
                    "조회코드": f"{province}/{municipality}".strip("/"),
                    "코드구분": variant_label,
                    "조회기간": f"현재 진행 물건 중 GUI 매각기일 조건은 수집 후 적용",
                    "완료구간": f"{pages_done}페이지",
                    "법원 전체건수": total_seen,
                    "수집건수": variant_found,
                    "지역일치건수": variant_found,
                    "지역불일치제외": region_mismatch,
                    "중복회차병합": duplicate_rounds_merged,
                    "비고": (
                        f"{self.service_path}/{self.list_operation} · "
                        f"동일 물건 회차 {duplicate_rounds_merged:,}건 병합"
                    ),
                })
                if variant_found > 0:
                    break
            # 차세대 온비드 목록 API는 시·군·구 없이 시·도만 전달할 때
            # 일부 지역(예: 경기도)에서 0건을 반환한다. 이전 구현은 전국 대체조회가
            # 0건이어도 totalCount=0을 '완전 조회'로 간주하여 시·군·구 분할검색을
            # 생략할 수 있었다. 시·도 단독조회가 0건이면 해당 시·도의 공식 시·군·구를
            # 직접 순회하는 방식이 가장 재현성 높으므로 전국조회와 무관하게 항상 실행한다.
            if province_only:
                fanout_found = fanout_pages = fanout_total_seen = 0
                fanout_mismatches = fanout_duplicates = 0
                municipalities_done = municipalities_with_results = 0
                municipality_names = list(MUNICIPALITY_CODES.get(target_province, {}))

                for municipality_name in municipality_names:
                    municipality_region = f"{target_province} {municipality_name}"
                    municipality_found = 0
                    municipality_total = 0
                    for query_province, query_municipality, _ in self._region_variants(
                        municipality_region
                    ):
                        query_found = 0
                        for page in range(1, self.max_pages + 1):
                            rows, total_count = self._request(
                                self.list_operation,
                                self._build_params(
                                    profile, query_province, query_municipality, page
                                ),
                            )
                            fanout_pages += 1
                            municipality_total = max(municipality_total, total_count)
                            fanout_total_seen += len(rows)
                            if not rows:
                                break
                            for raw in rows:
                                item = normalize_onbid_row(raw)
                                # 시·도 전체검색의 최종 목표는 해당 도의 모든 물건이다.
                                # API가 시·군·구 명칭을 생략하거나 행정구 명칭으로 반환해도
                                # 도 단위 주소가 일치하면 포함하고, 타 시·도만 제외한다.
                                if not item_matches_region(item, target_province):
                                    fanout_mismatches += 1
                                    continue
                                property_key = _onbid_management_key(item)
                                previous = unique.get(property_key)
                                if previous is not None:
                                    fanout_duplicates += 1
                                unique[property_key] = _merge_onbid_round_items(previous, item)
                                query_found += 1
                            if len(rows) < self.page_size:
                                break
                            if total_count and page >= math.ceil(total_count / self.page_size):
                                break
                        municipality_found += query_found
                        # 현행 명칭으로 결과가 있으면 과거명칭 호환조회는 생략한다.
                        if query_found > 0:
                            break
                    if municipality_found > 0:
                        municipalities_with_results += 1
                    fanout_found += municipality_found
                    municipalities_done += 1

                region_found += fanout_found
                self.last_fetch_diagnostics.append({
                    "검색대상": "공매",
                    "검색방식": "차세대 온비드 시·군·구 전수 분할검색",
                    "지역": region,
                    "조회코드": f"{target_province} 전 시·군·구",
                    "코드구분": "시·도 전체 전수검색",
                    "조회기간": "현재 진행 물건 중 GUI 매각기일 조건은 수집 후 적용",
                    "완료구간": (
                        f"{municipalities_done}/{len(municipality_names)}개 시·군·구 · "
                        f"결과 보유 {municipalities_with_results}개 · {fanout_pages}페이지"
                    ),
                    "법원 전체건수": fanout_total_seen,
                    "수집건수": fanout_found,
                    "지역일치건수": fanout_found,
                    "지역불일치제외": fanout_mismatches,
                    "중복회차병합": fanout_duplicates,
                    "비고": (
                        f"{self.service_path}/{self.list_operation} · "
                        f"시·도 단독 API 결과를 신뢰하지 않고 GUI 개별검색과 동일하게 "
                        f"공식 시·군·구 {len(municipality_names)}개를 직접 조회"
                    ),
                })

            if region_found == 0:
                logger.info("차세대 온비드 공매 검색 결과 없음: %s", region or "전국")

        self.last_fetch_summary = {
            "총 소요시간(초)": round(time.monotonic() - started, 2),
            "실제 법원요청": 0,
            "실제 공매요청": self._request_count,
            "캐시 재사용": self._cache_hits,
            "요청대기시간(초)": 0,
            "서버응답시간(초)": round(self._request_seconds, 2),
            "브라우저준비시간(초)": 0,
        }
        return list(unique.values())

    def fetch_detail(self, item: AuctionItem) -> AuctionItem:
        if not self.detail_enabled:
            return item
        raw = dict(item.raw or {})
        management_no = str(_first(raw, "cltrMngNo", "CLTR_MNMT_NO", default=item.item_number)).strip()
        if not management_no:
            return item
        try:
            rows, _ = self._request(
                self.detail_operation,
                {"resultType": "json", "cltrMngNo": management_no},
                service_path=self.detail_service_path,
            )
            if rows:
                # 상세는 동일 물건의 여러 입찰 회차를 배열로 반환한다. 가장 먼 미래 회차가
                # 아니라 현재 또는 가장 가까운 유효 회차를 대표값으로 선택한다.
                detail_items = [normalize_onbid_row(row) for row in rows]
                merged_detail: AuctionItem | None = None
                for detail_candidate in detail_items:
                    merged_detail = _merge_onbid_round_items(merged_detail, detail_candidate)
                detail_item = merged_detail or item
                selected = dict(detail_item.raw or {})
                item.address = detail_item.address or item.address
                item.land_area_m2 = detail_item.land_area_m2 or item.land_area_m2
                item.building_area_m2 = detail_item.building_area_m2 or item.building_area_m2
                item.appraisal_price = detail_item.appraisal_price or item.appraisal_price
                item.min_price = detail_item.min_price or item.min_price
                item.auction_date = detail_item.auction_date or item.auction_date
                item.special_conditions = list(dict.fromkeys(item.special_conditions + detail_item.special_conditions))
                item.detail_url = detail_item.detail_url or _detail_url(selected) or item.detail_url
                raw.update(selected)
                raw["ONBID_DETAIL_ROWS"] = rows
                raw["ONBID_SCHEDULE_ROWS"] = (detail_item.raw or {}).get("ONBID_SCHEDULE_ROWS", [])
                raw["onbid_merged_round_count"] = (detail_item.raw or {}).get("onbid_merged_round_count", len(rows))
                raw["onbid_duplicate_rows_merged"] = (detail_item.raw or {}).get("onbid_duplicate_rows_merged", max(0, len(rows) - 1))
        except Exception as exc:
            logger.warning("차세대 온비드 상세조회 실패 %s: %s", item.auction_id, exc)
        item.raw = raw
        return item

    def test_connection(self, profile: dict[str, Any]) -> dict[str, Any]:
        region = str((profile.get("regions") or [""])[0])
        province, municipality = split_region(region)
        rows, total = self._request(
            self.list_operation,
            self._build_params(profile, province, municipality, 1),
        )
        first = normalize_onbid_row(rows[0]) if rows else None
        return {
            "source": "공매",
            "queried_region": region or "전국",
            "region_codes": {"sido": province, "sigungu": municipality},
            "code_type": "차세대 온비드 주소명",
            "service": f"{self.service_path}/{self.list_operation}",
            "endpoint": f"{self.base_url}/{self.service_path}/{self.list_operation}",
            "legacy_config_migrated": self.legacy_config_migrated,
            "key_fingerprint": self.key_fingerprint,
            "ssl": dict(self.ssl_diagnostics),
            "first_page_count": len(rows),
            "total_count": total,
            "first_case_number": first.case_number if first else "",
            "attempts": list(self.last_key_attempts),
        }

    def close(self) -> None:
        self.session.close()
