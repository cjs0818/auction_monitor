from __future__ import annotations

import html
import json
from dataclasses import asdict, is_dataclass
from typing import Any
from urllib.parse import quote, quote_plus


def _to_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "to_dict"):
        result = value.to_dict()
        if isinstance(result, dict):
            return result
    return {}


def _number(value: Any, digits: int = 0) -> str:
    if value in (None, ""):
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if digits <= 0:
        return f"{int(round(number)):,}"
    return f"{number:,.{digits}f}".rstrip("0").rstrip(".")


def build_detail_payload(item: Any) -> dict[str, Any]:
    data = _to_mapping(item)
    appraisal = int(float(data.get("appraisal_price") or 0))
    minimum = int(float(data.get("min_price") or 0))
    discount = float(data.get("discount_percent") or 0)
    if not discount and appraisal > 0:
        discount = max(0.0, (1 - minimum / appraisal) * 100)
    area = float(data.get("land_area_m2") or 0)
    unit_price = float(data.get("unit_price") or 0)
    if not unit_price and area > 0:
        unit_price = minimum / area

    raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
    conditions = data.get("special_conditions") or []
    if isinstance(conditions, str):
        conditions = [conditions]
    score_reasons = data.get("score_reasons") or []
    risk_reasons = data.get("risk_reasons") or []

    result = {
        "검색조건": data.get("matched_profile") or data.get("profile_name") or "-",
        "검토등급": data.get("grade") or "-",
        "투자검토점수": _number(data.get("score"), 1),
        "매각구분": data.get("sale_type") or raw.get("source_type") or "경매",
        "데이터출처": data.get("source_name") or ("한국자산관리공사 온비드" if data.get("sale_type") == "공매" else "대한민국 법원경매정보"),
        "진행기관": data.get("court") or raw.get("orgNm") or raw.get("rqstOrgNm") or raw.get("ORG_NM") or raw.get("jiwonNm") or raw.get("cortOfcNm") or "-",
        "사건/공고번호": data.get("case_number") or raw.get("onbidPbancNo") or raw.get("PLNM_NO") or raw.get("printCsNo") or raw.get("srnSaNo") or "-",
        "물건번호/물건관리번호": data.get("item_number") or raw.get("cltrMngNo") or raw.get("CLTR_MNMT_NO") or raw.get("mokmulSer") or raw.get("maemulSer") or "-",
        "진행상태": data.get("status") or "-",
        "물건용도": data.get("usage") or "-",
        "소재지": data.get("address") or raw.get("realSt") or raw.get("printSt") or "-",
        "감정평가액": _number(appraisal) + ("원" if appraisal else ""),
        "최저매각가격/최저입찰가": _number(minimum) + ("원" if minimum else ""),
        "감정평가액 대비 할인율": _number(discount, 1) + "%",
        "유찰횟수": _number(data.get("failed_count")) + "회",
        "토지면적": _number(area, 2) + ("㎡" if area else ""),
        "평환산": _number(area / 3.305785, 1) + ("평" if area else ""),
        "㎡당 최저매각가격": _number(unit_price) + ("원" if unit_price else ""),
        "매각기일/입찰마감일": str(data.get("auction_date") or raw.get("cltrBidEndDt") or raw.get("PBCT_CLS_DTM") or raw.get("maeGiil") or "-"),
        "검토근거": [str(x) for x in score_reasons if str(x).strip()],
        "주의사항": [str(x) for x in risk_reasons if str(x).strip()],
        "특수조건·비고": [str(x) for x in conditions if str(x).strip()],
        "상세URL": data.get("detail_url") or "",
        "원시정보": {
            "지목목록": raw.get("jimokList") or "",
            "면적내역": raw.get("areaList") or "",
            "건물내역": raw.get("buldList") or "",
            "물건설명": raw.get("pjbBuldList") or "",
            "비고": raw.get("mulBigo") or raw.get("utlzPscdCont") or raw.get("locVntyPscdCont") or raw.get("UTLZ_PSCD") or raw.get("POSI_ENV_PSCD") or "",
            "매각/개찰장소": raw.get("maePlace") or raw.get("dspslPlcNm") or raw.get("opbdPlcCont") or raw.get("OPBD_PLC_CNTN") or "",
            "담당부서": raw.get("jpDeptNm") or raw.get("cortAuctnJdbnNm") or raw.get("RGST_DEPT_NM") or "",
            "공매번호": raw.get("pbctNo") or raw.get("PBCT_NO") or "",
            "물건번호": raw.get("onbidCltrno") or raw.get("CLTR_NO") or "",
            "입찰방식": raw.get("bidMthodNm") or raw.get("BID_MTD_NM") or "",
            "지번 PNU": raw.get("ltnoPnu") or raw.get("LTNO_PNU") or "",
            "복원 법정리": raw.get("legal_village_from_title") or "",
            "복원 지번": raw.get("parcel_from_pnu") or "",
            "주소 확인 방식": raw.get("address_source") or "",
            "병합된 입찰회차 수": raw.get("onbid_merged_round_count") or "",
            "대표 회차 선택 기준": raw.get("onbid_selected_round_reason") or "",
            "입찰회차 목록": raw.get("ONBID_SCHEDULE_ROWS") or [],
        },
    }
    # 기존 화면·테스트와의 하위 호환 키
    result["법원"] = result["진행기관"]
    result["사건번호"] = result["사건/공고번호"]
    result["물건번호"] = result["물건번호/물건관리번호"]
    result["최저매각가격"] = result["최저매각가격/최저입찰가"]
    result["매각기일"] = result["매각기일/입찰마감일"]
    return result


def build_map_urls(address: str) -> dict[str, str]:
    """Return external map links with NAVER Map as the primary provider."""
    address = str(address or "").strip()
    if not address or address == "-":
        return {"primary": "", "naver": "", "kakao": "", "google": ""}
    naver = f"https://map.naver.com/p/search/{quote(address, safe='')}"
    return {
        "primary": naver,
        "naver": naver,
        "kakao": f"https://map.kakao.com/link/search/{quote(address, safe='')}",
        "google": f"https://www.google.com/maps/search/?api=1&query={quote_plus(address)}",
    }


def build_naver_map_html(address: str, client_id: str, *, height: int = 440) -> str:
    """Build an interactive NAVER Map component.

    The client ID is the NAVER Cloud Maps JavaScript API ``ncpKeyId``.  When it
    is not configured, callers should show the external NAVER Map link instead.
    """
    address = str(address or "").strip()
    client_id = str(client_id or "").strip()
    if not address or address == "-" or not client_id:
        return ""

    safe_address_text = html.escape(address)
    js_address = json.dumps(address, ensure_ascii=False)
    js_client_id = quote(client_id, safe="")
    safe_height = max(260, min(int(height), 800))

    return f"""<!doctype html>
<html lang=\"ko\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width,initial-scale=1\" />
  <style>
    html, body, #map {{ width:100%; height:100%; margin:0; padding:0; }}
    body {{ font-family:-apple-system,BlinkMacSystemFont,\"Apple SD Gothic Neo\",\"Noto Sans KR\",sans-serif; }}
    #map {{ height:{safe_height}px; border-radius:10px; overflow:hidden; background:#f3f4f6; }}
    #status {{ position:absolute; z-index:10; left:14px; right:14px; top:14px; padding:10px 12px;
      border-radius:8px; background:rgba(255,255,255,.94); box-shadow:0 2px 10px rgba(0,0,0,.15);
      color:#374151; font-size:13px; }}
  </style>
  <script src=\"https://oapi.map.naver.com/openapi/v3/maps.js?ncpKeyId={js_client_id}&submodules=geocoder\"></script>
</head>
<body>
  <div id=\"status\">네이버 지도에서 주소를 찾고 있습니다: {safe_address_text}</div>
  <div id=\"map\"></div>
  <script>
    (function () {{
      var statusBox = document.getElementById('status');
      function fail(message) {{
        statusBox.textContent = message;
        statusBox.style.color = '#b42318';
      }}
      if (!window.naver || !naver.maps) {{
        fail('네이버 지도 API를 불러오지 못했습니다. Client ID와 Web 서비스 URL 등록을 확인하십시오.');
        return;
      }}
      var map = new naver.maps.Map('map', {{
        center: new naver.maps.LatLng(36.5, 127.8),
        zoom: 7,
        mapTypeControl: true,
        zoomControl: true,
        zoomControlOptions: {{ position: naver.maps.Position.TOP_RIGHT }}
      }});
      if (!naver.maps.Service || !naver.maps.Service.geocode) {{
        fail('네이버 지도 Geocoder 모듈을 사용할 수 없습니다. Maps API 설정을 확인하십시오.');
        return;
      }}
      naver.maps.Service.geocode({{ query: {js_address} }}, function(status, response) {{
        if (status !== naver.maps.Service.Status.OK || !response.v2 || !response.v2.addresses.length) {{
          fail('주소의 좌표를 찾지 못했습니다. 아래 네이버 지도 열기 버튼에서 직접 확인하십시오.');
          return;
        }}
        var result = response.v2.addresses[0];
        var position = new naver.maps.LatLng(Number(result.y), Number(result.x));
        map.setCenter(position);
        map.setZoom(17);
        var marker = new naver.maps.Marker({{ position: position, map: map }});
        var info = new naver.maps.InfoWindow({{
          content: '<div style="padding:10px 12px;max-width:300px;font-size:13px;line-height:1.45">' +
                   '<strong>매각물건 소재지</strong><br>' +
                   {json.dumps(safe_address_text, ensure_ascii=False)} + '</div>'
        }});
        info.open(map, marker);
        naver.maps.Event.addListener(marker, 'click', function() {{ info.open(map, marker); }});
        statusBox.style.display = 'none';
      }});
    }})();
  </script>
</body>
</html>"""
