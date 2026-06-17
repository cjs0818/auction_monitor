from datetime import date
import json

import pytest

from landwatch.models import AuctionItem
from landwatch.onbid_openapi import (
    NEXT_BASE_URL,
    NEXT_LIST_OPERATION,
    NEXT_LIST_SERVICE,
    OnbidOpenApiError,
    OnbidOpenApiProvider,
    _parse_api_response,
    _parse_json_response,
    _parse_xml_response,
    _parcel_from_pnu,
    normalize_onbid_row,
)
from landwatch.providers import CombinedProvider, normalize_search_target
from landwatch.report import to_dataframe


SAMPLE_JSON = {
    "header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE"},
    "body": {
        "items": {
            "item": [
                {
                    "cltrMngNo": "2026-00123-001",
                    "cltrHstrNo": 987654,
                    "onbidCltrno": 777,
                    "onbidPbancNo": 123456,
                    "pbctNo": 654321,
                    "pbctCdtnNo": 4,
                    "prptDivNm": "압류재산",
                    "dspsMthodNm": "매각",
                    "bidMthodNm": "일반경쟁(최고가방식)",
                    "pbctStatNm": "진행",
                    "cltrUsgLclsCtgrNm": "부동산",
                    "cltrUsgMclsCtgrNm": "토지",
                    "cltrUsgSclsCtgrNm": "전",
                    "onbidCltrNm": "전북특별자치도 부안군 변산면 100 농지",
                    "lctnSdnm": "전북특별자치도",
                    "lctnSggnm": "부안군",
                    "lctnEmdNm": "변산면",
                    "orgNm": "한국자산관리공사 전북지역본부",
                    "cltrBidBgngDt": "20260701100000",
                    "cltrBidEndDt": "20260703170000",
                    "apslEvlAmt": 42000000,
                    "frstBidPrc": 42000000,
                    "lowstBidPrcIndctCont": "18,000,000원",
                    "landSqms": 1180.0,
                    "bidPrgnNft": 3,
                }
            ]
        },
        "numOfRows": "100",
        "pageNo": "1",
        "totalCount": "1",
    },
}

SAMPLE_XML = """<?xml version='1.0' encoding='UTF-8'?>
<response>
  <header><resultCode>00</resultCode><resultMsg>NORMAL SERVICE</resultMsg></header>
  <body><items><item>
    <PLNM_NO>123456</PLNM_NO><PBCT_NO>654321</PBCT_NO><CLTR_NO>777</CLTR_NO>
    <CLTR_MNMT_NO>2026-00123-001</CLTR_MNMT_NO><CTGR_FULL_NM>부동산 / 토지 / 전</CTGR_FULL_NM>
    <CLTR_NM>전북특별자치도 부안군 농지</CLTR_NM><LDNM_ADRS>전북특별자치도 부안군 변산면 100</LDNM_ADRS>
    <MIN_BID_PRC>18000000</MIN_BID_PRC><APSL_ASES_AVG_AMT>42000000</APSL_ASES_AVG_AMT>
    <PBCT_CLS_DTM>20260703170000</PBCT_CLS_DTM><PBCT_CLTR_STAT_NM>입찰준비중</PBCT_CLTR_STAT_NM>
    <USCBD_CNT>2</USCBD_CNT><GOODS_NM>전 1,180 ㎡</GOODS_NM>
  </item></items><totalCount>1</totalCount></body>
</response>"""


def test_next_onbid_json_and_row_normalization():
    rows, total, header = _parse_json_response(SAMPLE_JSON)
    assert total == 1
    assert header["resultCode"] == "00"
    item = normalize_onbid_row(rows[0])
    assert item.sale_type == "공매"
    assert item.source_name == "한국자산관리공사 차세대 온비드"
    assert item.case_number == "123456"
    assert item.item_number == "2026-00123-001"
    assert item.address.startswith("전북특별자치도 부안군")
    assert item.usage == "전"
    assert item.status == "유찰"
    assert item.min_price == 18_000_000
    assert item.appraisal_price == 42_000_000
    assert item.failed_count == 2
    assert item.land_area_m2 == 1180
    assert item.auction_date == date(2026, 7, 3)
    assert item.detail_url == (
        "https://www.onbid.co.kr/op/cta/cltrdtl/collateralDetailRealEstateList.do"
        "?searchCltrMnmtNo=2026-00123-001&cltrMnmtNo=2026-00123-001"
    )
    assert item.raw["onbid_link_mode"] == "management-number-search"
    assert "collateralRealEstateDetail.do" not in item.detail_url




def test_pnu_decodes_exact_parcel_number():
    assert _parcel_from_pnu("4580033021101000000") == "100"
    assert _parcel_from_pnu("4580033021200120003") == "산 12-3"
    assert _parcel_from_pnu("invalid") == ""


def test_next_onbid_list_address_uses_pnu_when_title_has_no_lot_number():
    row = dict(SAMPLE_JSON["body"]["items"]["item"][0])
    row["onbidCltrNm"] = "전북특별자치도 부안군 변산면 소재 농지"
    row["ltnoPnu"] = "4580033021101000000"
    item = normalize_onbid_row(row)
    assert item.address == "전북특별자치도 부안군 변산면 100"
    assert item.raw["address_source"] == "PNU 지번복원"
    assert item.raw["parcel_from_pnu"] == "100"
    assert item.raw["LTNO_PNU"] == "4580033021101000000"


def test_next_onbid_list_address_decodes_mountain_sub_lot():
    row = dict(SAMPLE_JSON["body"]["items"]["item"][0])
    row["onbidCltrNm"] = "전북특별자치도 부안군 변산면 임야"
    row["ltnoPnu"] = "4580033021200120003"
    item = normalize_onbid_row(row)
    assert item.address.endswith("변산면 산 12-3")


def test_detail_lot_address_is_preferred_over_road_address_and_pnu():
    row = dict(SAMPLE_JSON["body"]["items"]["item"][0])
    row.update({
        "zadrNm": "전북특별자치도 부안군 변산면 격포리 88-2",
        "cltrRadr": "전북특별자치도 부안군 변산면 해변로 1",
        "ltnoPnu": "4580033021100880002",
    })
    item = normalize_onbid_row(row)
    assert item.address == "전북특별자치도 부안군 변산면 격포리 88-2"
    assert item.raw["address_source"] == "상세 지번주소"


def test_old_xml_remains_parseable_for_saved_cache_compatibility():
    rows, total, _ = _parse_xml_response(SAMPLE_XML)
    assert total == 1
    item = normalize_onbid_row(rows[0])
    assert item.item_number == "2026-00123-001"
    assert item.min_price == 18_000_000


def test_api_response_accepts_json_content_type():
    rows, total, _ = _parse_api_response(json.dumps(SAMPLE_JSON), "application/json")
    assert len(rows) == 1
    assert total == 1


def test_next_onbid_json_error_is_explained():
    with pytest.raises(OnbidOpenApiError, match="등록되지 않은 서비스키") as exc:
        _parse_json_response({"header": {"resultCode": "30", "resultMsg": "등록되지 않은 서비스키"}, "body": {}})
    assert exc.value.code == "30"


def test_onbid_build_params_use_new_endpoint_fields(monkeypatch):
    provider = OnbidOpenApiProvider({"service_key": "test", "page_size": 100})
    params = provider._build_params({
        "appraisal_price": {"min": 10_000_000, "max": 200_000_000},
    }, "전북특별자치도", "부안군", 2)
    assert provider.base_url == NEXT_BASE_URL
    assert provider.service_path == NEXT_LIST_SERVICE
    assert provider.list_operation == NEXT_LIST_OPERATION
    assert params["resultType"] == "json"
    assert params["prptDivCd"]
    assert params["pvctTrgtYn"] == "N"
    assert params["dspsMthodCd"] == "0001"
    assert params["cltrUsgLclsCtgrId"] == "10000"
    assert params["lctnSdnm"] == "전북특별자치도"
    assert params["lctnSggnm"] == "부안군"
    assert params["apslEvlAmtFrom"] == 10_000_000
    assert params["apslEvlAmtTo"] == 200_000_000
    assert params["pageNo"] == 2
    assert "PBCT_BEGN_DTM" not in params
    assert "OPEN_PRICE_FROM" not in params


def test_legacy_config_is_automatically_migrated_to_next_api():
    provider = OnbidOpenApiProvider({
        "base_url": "http://openapi.onbid.co.kr/openapi/services",
        "service_path": "ThingInfoInquireSvc",
        "list_operation": "getUnifyUsageCltr",
        "service_key": "test",
    })
    assert provider.legacy_config_migrated is True
    assert provider.base_url == NEXT_BASE_URL
    assert provider.service_path == NEXT_LIST_SERVICE
    assert provider.list_operation == NEXT_LIST_OPERATION


def test_search_target_aliases():
    assert normalize_search_target("경매") == "경매"
    assert normalize_search_target("onbid") == "공매"
    assert normalize_search_target("경매+공매") == "경매 및 공매"


class _FakeProvider:
    def __init__(self, item, summary):
        self.item = item
        self.last_fetch_diagnostics = []
        self.last_fetch_summary = summary
        self.cache_enabled = True

    def fetch(self, profile):
        return [self.item]

    def fetch_detail(self, item):
        return item

    def close(self):
        pass


def test_combined_provider_keeps_auction_and_public_sale():
    auction = AuctionItem(auction_id="same", sale_type="경매", case_number="2025타경1")
    public = AuctionItem(auction_id="same", sale_type="공매", case_number="111")
    provider = CombinedProvider([
        _FakeProvider(auction, {"실제 법원요청": 1, "실제 공매요청": 0}),
        _FakeProvider(public, {"실제 법원요청": 0, "실제 공매요청": 1}),
    ])
    items = provider.fetch({})
    assert {x.sale_type for x in items} == {"경매", "공매"}
    assert provider.last_fetch_summary["실제 법원요청"] == 1
    assert provider.last_fetch_summary["실제 공매요청"] == 1


def test_combined_provider_safely_aggregates_dash_placeholders():
    auction = AuctionItem(auction_id="auction", sale_type="경매", case_number="2025타경1")
    public = AuctionItem(auction_id="public", sale_type="공매", case_number="111")
    provider = CombinedProvider([
        _FakeProvider(auction, {
            "총 소요시간(초)": 1.25,
            "실제 법원요청": 2,
            "실제 공매요청": "-",
            "캐시 재사용": "-",
            "요청대기시간(초)": 0.5,
            "서버응답시간(초)": 0.75,
            "브라우저준비시간(초)": 0.25,
        }),
        _FakeProvider(public, {
            "총 소요시간(초)": "2.5",
            "실제 법원요청": "-",
            "실제 공매요청": 3,
            "캐시 재사용": 1,
            "요청대기시간(초)": "-",
            "서버응답시간(초)": 2.0,
            "브라우저준비시간(초)": "-",
        }),
    ])

    items = provider.fetch({})

    assert len(items) == 2
    assert provider.last_fetch_summary == {
        "총 소요시간(초)": 3.75,
        "실제 법원요청": 2,
        "실제 공매요청": 3,
        "캐시 재사용": 1,
        "요청대기시간(초)": 0.5,
        "서버응답시간(초)": 2.75,
        "브라우저준비시간(초)": 0.25,
    }


def test_report_distinguishes_public_sale_fields():
    item = normalize_onbid_row(_parse_json_response(SAMPLE_JSON)[0][0])
    df = to_dataframe([item], {"is_sample": False})
    assert df.loc[0, "매각구분"] == "공매"
    assert df.loc[0, "진행기관"] == "한국자산관리공사 전북지역본부"
    assert df.loc[0, "사건/공고번호"] == "123456"
    assert df.loc[0, "물건번호/물건관리번호"] == "2026-00123-001"


def test_onbid_service_key_candidates_accept_encoding_decoding_and_url():
    from landwatch.onbid_openapi import (
        _decode_service_key,
        _sanitize_service_key,
        _service_key_candidates,
        _service_key_fingerprint,
    )
    decoded = "abc+def/ghi="
    encoded = "abc%2Bdef%2Fghi%3D"
    assert _decode_service_key(encoded) == decoded
    assert _sanitize_service_key(f"https://x.test/?serviceKey={encoded}&pageNo=1") == decoded
    candidates = _service_key_candidates(encoded)
    assert any(x["mode"] == "params" and x["value"] == decoded for x in candidates)
    assert any(x["mode"] == "raw_url" and x["value"] == encoded for x in candidates)
    assert "abc" not in _service_key_fingerprint(encoded)


def test_connection_uses_get_rlst_cltr_list2(tmp_path):
    class FakeResponse:
        text = json.dumps(SAMPLE_JSON)
        headers = {"content-type": "application/json"}
        def raise_for_status(self): return None

    class FakeSession:
        def __init__(self): self.calls = []
        def get(self, url, params=None, timeout=None):
            self.calls.append((url, params))
            return FakeResponse()
        def close(self): pass

    provider = OnbidOpenApiProvider({
        "service_key": "test-key",
        "cache_enabled": False,
        "cache_dir": str(tmp_path),
    })
    provider.session = FakeSession()
    result = provider.test_connection({"regions": ["전북특별자치도 부안군"]})
    assert result["service"] == "OnbidRlstListSrvc2/getRlstCltrList2"
    called_url, called_params = provider.session.calls[0]
    assert called_url.endswith("/OnbidRlstListSrvc2/getRlstCltrList2")
    assert called_params["resultType"] == "json"
    assert called_params["lctnSggnm"] == "부안군"


def test_error30_message_identifies_next_generation_service(tmp_path):
    class FakeResponse:
        text = json.dumps({"header": {"resultCode": "30", "resultMsg": "SERVICE KEY IS NOT REGISTERED ERROR"}, "body": {}})
        headers = {"content-type": "application/json"}
        def raise_for_status(self): return None
    class FakeSession:
        def get(self, url, params=None, timeout=None): return FakeResponse()
        def close(self): pass

    provider = OnbidOpenApiProvider({"service_key": "test-key", "cache_enabled": False, "cache_dir": str(tmp_path)})
    provider.session = FakeSession()
    with pytest.raises(OnbidOpenApiError) as exc_info:
        provider._request(provider.list_operation, {"numOfRows": 1, "pageNo": 1, "resultType": "json"})
    message = str(exc_info.value)
    assert "차세대 온비드 부동산 물건목록 조회서비스" in message
    assert "OnbidRlstListSrvc2/getRlstCltrList2" in message
    assert "ThingInfoInquireSvc" not in message
    assert "test-key" not in message


def test_redact_sensitive_url_removes_service_key():
    from landwatch.onbid_openapi import _redact_sensitive_url
    safe = _redact_sensitive_url(
        "https://apis.data.go.kr/B010003/OnbidRlstListSrvc2/getRlstCltrList2?serviceKey=SECRET&pageNo=1"
    )
    assert safe == "https://apis.data.go.kr/B010003/OnbidRlstListSrvc2/getRlstCltrList2"
    assert "SECRET" not in safe


def test_ssl_error_is_explained_and_key_is_not_exposed(tmp_path):
    import requests

    class FakeSession:
        verify = True
        def get(self, url, params=None, timeout=None):
            raise requests.exceptions.SSLError(
                "certificate verify failed for https://example/?serviceKey=SUPERSECRET"
            )
        def close(self): pass

    provider = OnbidOpenApiProvider({
        "service_key": "SUPERSECRET",
        "cache_enabled": False,
        "cache_dir": str(tmp_path),
        "ssl_trust_mode": "certifi",
    })
    provider.session = FakeSession()
    with pytest.raises(OnbidOpenApiError) as exc_info:
        provider._request(provider.list_operation, {"numOfRows": 1, "pageNo": 1, "resultType": "json"})
    message = str(exc_info.value)
    assert "HTTPS 연결에서 인증서 검증에 실패" in message
    assert "SUPERSECRET" not in message
    assert "SSL 검증을 끄는 방식은 사용하지 않습니다" in message


def test_connection_result_reports_ssl_mode(tmp_path, monkeypatch):
    class FakeResponse:
        text = json.dumps(SAMPLE_JSON)
        headers = {"content-type": "application/json"}
        def raise_for_status(self): return None

    class FakeSession:
        def __init__(self): self.calls = []
        def get(self, url, params=None, timeout=None):
            self.calls.append((url, params))
            return FakeResponse()
        def close(self): pass

    monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
    monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
    provider = OnbidOpenApiProvider({
        "service_key": "test-key",
        "cache_enabled": False,
        "cache_dir": str(tmp_path),
        "ssl_trust_mode": "certifi",
    })
    provider.session = FakeSession()
    result = provider.test_connection({"regions": ["전북특별자치도 부안군"]})
    assert result["ssl"]["active_mode"] == "requests-certifi"
    assert result["ssl"]["certificate_verification"] is True


def test_onbid_title_keeps_legal_ri_between_myeon_and_parcel():
    row = dict(SAMPLE_JSON["body"]["items"]["item"][0])
    row["onbidCltrNm"] = "전북특별자치도 부안군 변산면 도청리 100-2 소재 전"
    row["ltnoPnu"] = "4580033021101000002"
    item = normalize_onbid_row(row)
    assert item.address == "전북특별자치도 부안군 변산면 도청리 100-2"
    assert item.raw["address_source"] == "물건명 지번주소"
    assert item.raw["legal_village_from_title"] == "도청리"


def test_onbid_title_legal_ri_combines_with_pnu_when_title_has_no_parcel():
    row = dict(SAMPLE_JSON["body"]["items"]["item"][0])
    row["onbidCltrNm"] = "전북특별자치도 부안군 변산면 도청리 소재 농지"
    row["ltnoPnu"] = "4580033021101000002"
    item = normalize_onbid_row(row)
    assert item.address == "전북특별자치도 부안군 변산면 도청리 100-2"
    assert item.raw["address_source"] == "물건명 법정리 + PNU 지번복원"
    assert item.raw["legal_village_from_title"] == "도청리"


def test_onbid_title_legal_ri_combines_with_mountain_pnu():
    row = dict(SAMPLE_JSON["body"]["items"]["item"][0])
    row["onbidCltrNm"] = "전북특별자치도 부안군 변산면 격포리 소재 임야"
    row["ltnoPnu"] = "4580033021200120003"
    item = normalize_onbid_row(row)
    assert item.address == "전북특별자치도 부안군 변산면 격포리 산 12-3"


def test_onbid_round_rows_merge_to_one_current_property():
    from landwatch.onbid_openapi import _merge_onbid_round_items

    base = dict(SAMPLE_JSON["body"]["items"]["item"][0])
    base.update({
        "cltrMngNo": "2025-12641-001",
        "onbidPbancNo": 866695,
        "onbidCltrNm": "전북특별자치도 부안군 부안읍 행중리 239-10 답",
        "apslEvlAmt": 17_516_500,
        "landSqms": 330.5,
        "bidPrgnNft": 1,
    })
    rows = []
    for condition_no, end_date, price, status in [
        (1, "20260617170000", 10_511_000, "진행"),
        (2, "20260624170000", 8_759_000, "입찰준비중"),
        (3, "20260701170000", 7_007_000, "입찰준비중"),
        (4, "20260708170000", 5_256_000, "입찰준비중"),
    ]:
        row = dict(base)
        row.update({
            "pbctCdtnNo": condition_no,
            "cltrBidEndDt": end_date,
            "lowstBidPrcIndctCont": str(price),
            "pbctStatNm": status,
        })
        rows.append(normalize_onbid_row(row))

    merged = None
    for item in rows:
        merged = _merge_onbid_round_items(merged, item, today=date(2026, 6, 16))

    assert merged is not None
    assert merged.auction_id == "onbid:2025-12641-001"
    assert merged.item_number == "2025-12641-001"
    assert merged.auction_date == date(2026, 6, 17)
    assert merged.min_price == 10_511_000
    assert merged.status == "진행"
    assert merged.raw["onbid_merged_round_count"] == 4
    assert merged.raw["onbid_duplicate_rows_merged"] == 3
    assert len(merged.raw["ONBID_SCHEDULE_ROWS"]) == 4


def test_onbid_provider_deduplicates_same_management_number(monkeypatch, tmp_path):
    base = dict(SAMPLE_JSON["body"]["items"]["item"][0])
    base.update({
        "cltrMngNo": "2025-12641-001",
        "onbidPbancNo": 866695,
        "onbidCltrNm": "전북특별자치도 부안군 부안읍 행중리 239-10 답",
        "lctnEmdNm": "부안읍",
        "apslEvlAmt": 17_516_500,
        "landSqms": 330.5,
        "bidPrgnNft": 1,
    })
    raw_rows = []
    for condition_no, end_date, price in [
        (1, "20990101170000", 10_511_000),
        (2, "20990108170000", 8_759_000),
        (3, "20990115170000", 7_007_000),
        (4, "20990122170000", 5_256_000),
    ]:
        row = dict(base)
        row.update({
            "pbctCdtnNo": condition_no,
            "cltrBidEndDt": end_date,
            "lowstBidPrcIndctCont": str(price),
            "pbctStatNm": "입찰준비중" if condition_no > 1 else "진행",
        })
        raw_rows.append(row)

    provider = OnbidOpenApiProvider({
        "service_key": "test",
        "cache_enabled": False,
        "cache_dir": str(tmp_path),
        "max_pages": 1,
    })
    monkeypatch.setattr(provider, "_request", lambda *args, **kwargs: (raw_rows, len(raw_rows)))
    items = provider.fetch({"regions": ["전북특별자치도 부안군"]})
    assert len(items) == 1
    assert items[0].item_number == "2025-12641-001"
    assert items[0].auction_id == "onbid:2025-12641-001"
    assert items[0].raw["onbid_duplicate_rows_merged"] == 3
    assert provider.last_fetch_diagnostics[0]["중복회차병합"] == 3


def test_database_removes_legacy_onbid_round_duplicates(tmp_path):
    from landwatch.db import Database

    db_path = tmp_path / "landwatch.db"
    db = Database(str(db_path))
    profile = "지방 소액 농지·임야"
    for round_no, day, price in [
        (1, 17, 10_511_000),
        (2, 24, 8_759_000),
        (3, 1, 7_007_000),
        (4, 8, 5_256_000),
    ]:
        month = 6 if round_no <= 2 else 7
        item = AuctionItem(
            auction_id=f"onbid:2025-12641-001:{round_no}",
            sale_type="공매",
            item_number="2025-12641-001",
            case_number="866695",
            status="진행" if round_no == 1 else "신건",
            auction_date=date(2026, month, day),
            min_price=price,
            appraisal_price=17_516_500,
            matched_profile=profile,
        )
        db.upsert(item)
    # Legacy upsert cleanup keeps only one physical property per profile.
    rows = db.recent_items(20)
    assert len(rows) == 1
    payload = json.loads(rows[0]["payload_json"])
    assert payload["item_number"] == "2025-12641-001"


def test_onbid_round_price_ignores_percentage_text_and_uses_same_row_price():
    from landwatch.onbid_openapi import _parse_onbid_round_price

    price, source = _parse_onbid_round_price({
        "lowstBidPrcIndctCont": "감정가의 80% 이상",
        "frstBidPrc": 80_000_000,
    }, 100_000_000)
    assert price == 80_000_000
    assert source == "frstBidPrc"


def test_onbid_round_price_prefers_explicit_current_lowest_price():
    from landwatch.onbid_openapi import _parse_onbid_round_price

    price, source = _parse_onbid_round_price({
        "lowstBidPrc": 64_000_000,
        "frstBidPrc": 100_000_000,
        "lowstBidPrcIndctCont": "최저입찰가 64,000,000원",
    }, 100_000_000)
    assert price == 64_000_000
    assert source == "lowstBidPrc"


def _gyeonggi_onbid_row(*, management_no: str, municipality: str, parcel: str = "1"):
    row = dict(SAMPLE_JSON["body"]["items"]["item"][0])
    row.update({
        "cltrMngNo": management_no,
        "onbidPbancNo": int("".join(ch for ch in management_no if ch.isdigit())[-6:] or "1"),
        "onbidCltrNm": f"경기도 {municipality} 가평읍 읍내리 {parcel} 전",
        "lctnSdnm": "경기도",
        "lctnSggnm": municipality,
        "lctnEmdNm": "가평읍",
        "ltnoPnu": "4182025021100010000",
        "cltrBidEndDt": "20990101170000",
        "pbctStatNm": "진행",
    })
    return row


def test_onbid_province_only_zero_fans_out_all_municipalities(tmp_path, monkeypatch):
    provider = OnbidOpenApiProvider({
        "service_key": "test",
        "cache_enabled": False,
        "cache_dir": str(tmp_path),
        "max_pages": 1,
    })
    calls = []
    gyeonggi = _gyeonggi_onbid_row(management_no="2026-90001-001", municipality="가평군")

    def fake_request(operation, params, **kwargs):
        calls.append(dict(params))
        province = params.get("lctnSdnm")
        municipality = params.get("lctnSggnm")
        if province == "경기도" and not municipality:
            return [], 0
        if province == "경기도" and municipality == "가평군":
            return [gyeonggi], 1
        return [], 0

    monkeypatch.setattr(provider, "_request", fake_request)
    items = provider.fetch({"regions": ["경기도"]})

    assert len(items) == 1
    assert items[0].province == "경기도"
    assert "가평군" in items[0].city_county
    assert calls[0]["lctnSdnm"] == "경기도"
    assert calls[0]["lctnSggnm"] == "수원시"
    assert any(x.get("lctnSggnm") == "가평군" for x in calls)
    assert not any(not x.get("lctnSdnm") and not x.get("lctnSggnm") for x in calls)
    fallback = provider.last_fetch_diagnostics[-1]
    assert fallback["검색방식"] == "차세대 온비드 시·군·구 전수 분할검색"
    assert fallback["지역일치건수"] == 1
    assert "31개" in fallback["비고"]

def test_onbid_province_search_always_uses_municipality_fanout(tmp_path, monkeypatch):
    provider = OnbidOpenApiProvider({
        "service_key": "test",
        "cache_enabled": False,
        "cache_dir": str(tmp_path),
        "max_pages": 1,
    })
    calls = []
    gyeonggi = _gyeonggi_onbid_row(management_no="2026-90003-001", municipality="가평군")

    def fake_request(operation, params, **kwargs):
        calls.append(dict(params))
        if params.get("lctnSggnm") == "가평군":
            return [gyeonggi], 1
        return [], 0

    monkeypatch.setattr(provider, "_request", fake_request)
    items = provider.fetch({"regions": ["경기도"]})

    assert len(items) == 1
    assert len(calls) == 31
    assert all(call.get("lctnSggnm") for call in calls)
    assert not any(call.get("lctnSggnm") == "" for call in calls)


def test_onbid_municipality_zero_does_not_expand_to_nationwide(tmp_path, monkeypatch):
    provider = OnbidOpenApiProvider({
        "service_key": "test",
        "cache_enabled": False,
        "cache_dir": str(tmp_path),
        "max_pages": 1,
    })
    calls = []

    def fake_request(operation, params, **kwargs):
        calls.append(dict(params))
        return [], 0

    monkeypatch.setattr(provider, "_request", fake_request)
    items = provider.fetch({"regions": ["경기도 가평군"]})

    assert items == []
    assert len(calls) == 1
    assert calls[0]["lctnSdnm"] == "경기도"
    assert calls[0]["lctnSggnm"] == "가평군"


def test_onbid_empty_nationwide_response_cannot_block_municipality_fanout(tmp_path, monkeypatch):
    provider = OnbidOpenApiProvider({
        "service_key": "test",
        "cache_enabled": False,
        "cache_dir": str(tmp_path),
        "max_pages": 1,
        "page_size": 100,
    })
    gyeonggi = _gyeonggi_onbid_row(management_no="2026-90004-001", municipality="가평군")
    other = dict(gyeonggi)
    other.update({
        "cltrMngNo": "2026-90005-001",
        "onbidCltrNm": "강원특별자치도 춘천시 동면 만천리 1 전",
        "lctnSdnm": "강원특별자치도",
        "lctnSggnm": "춘천시",
        "lctnEmdNm": "동면",
    })
    calls = []

    def fake_request(operation, params, **kwargs):
        calls.append(dict(params))
        province = params.get("lctnSdnm")
        municipality = params.get("lctnSggnm")
        if province == "경기도" and not municipality:
            return [], 0
        if not province and not municipality:
            return [], 0  # 전국조회가 비어도 시·군·구 전수검색은 반드시 실행되어야 함
        if province == "경기도" and municipality == "가평군":
            return [gyeonggi], 1
        return [], 0

    monkeypatch.setattr(provider, "_request", fake_request)
    items = provider.fetch({"regions": ["경기도"]})

    assert len(items) == 1
    assert "가평군" in items[0].address
    assert any(x.get("lctnSggnm") == "가평군" for x in calls)
    assert provider.last_fetch_diagnostics[-1]["검색방식"] == "차세대 온비드 시·군·구 전수 분할검색"


def test_onbid_detail_url_uses_management_number_search_for_next_api_rows():
    from landwatch.onbid_openapi import _detail_url

    url = _detail_url({"cltrMngNo": "2025-12641-001", "pbctCdtnNo": 1})
    assert url == (
        "https://www.onbid.co.kr/op/cta/cltrdtl/collateralDetailRealEstateList.do"
        "?searchCltrMnmtNo=2025-12641-001&cltrMnmtNo=2025-12641-001"
    )
    assert "collateralRealEstateDetail.do" not in url


def test_onbid_detail_url_does_not_fabricate_legacy_web_link_from_api_ids():
    from landwatch.onbid_openapi import _detail_url

    url = _detail_url({
        "CLTR_HSTR_NO": "5447179",
        "CLTR_NO": "1873550",
        "PLNM_NO": "818693",
        "PBCT_NO": "9921904",
        "PBCT_CDTN_NO": "5315787",
    })
    assert url == (
        "https://www.onbid.co.kr/op/cta/cltrdtl/collateralDetailRealEstateList.do"
    )


def test_onbid_detail_url_accepts_only_explicit_official_web_url():
    from landwatch.onbid_openapi import _detail_url

    explicit = (
        "https://www.onbid.co.kr/op/cta/cltrdtl/"
        "someVerifiedDetail.do?token=abc"
    )
    assert _detail_url({"cltrMngNo": "2025-12641-001", "detailUrl": explicit}) == explicit
    assert "evil.example" not in _detail_url({
        "cltrMngNo": "2025-12641-001",
        "detailUrl": "https://evil.example/redirect",
    })
