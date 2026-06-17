from datetime import date, datetime

from landwatch.filtering import matches_profile
from landwatch.models import AuctionItem
from landwatch.scoring import score_item


def profile():
    return {
        "name": "test",
        "regions": ["충북 충주시"],
        "statuses": ["유찰"],
        "usages": ["전"],
        "failed_count": {"min": 1, "max": 4},
        "min_price": {"min": 5_000_000, "max": 30_000_000},
        "land_area_m2": {"min": 330, "max": 3300},
        "appraisal_discount_percent": {"min": 20, "max": 90},
        "preferred_usages": {"전": 10},
        "exclude_keywords": ["지분", "맹지"],
    }


def test_filter_and_score():
    item = AuctionItem(
        auction_id="x", status="유찰", usage="전", address="충청북도 충주시 소태면",
        province="충북", city_county="충주시", min_price=18_000_000,
        appraisal_price=42_000_000, failed_count=2, land_area_m2=1180,
        auction_date=date(2026, 7, 3), market_estimate=36_000_000,
    )
    ok, reasons = matches_profile(item, profile(), today=date(2026, 6, 14))
    assert ok, reasons
    scored = score_item(item, profile())
    assert scored.score > 60


def test_risky_item_excluded():
    item = AuctionItem(
        auction_id="y", status="유찰", usage="전", address="충북 충주시",
        province="충북", city_county="충주시", min_price=10_000_000,
        appraisal_price=30_000_000, failed_count=2, land_area_m2=660,
        special_conditions=["지분매각"], auction_date=date(2026, 7, 3),
    )
    ok, reasons = matches_profile(item, profile(), today=date(2026, 6, 14))
    assert not ok
    assert any("지분" in r for r in reasons)


def test_report_contains_item_number_and_comma_formatting():
    from landwatch.report import format_dataframe_for_report, to_dataframe

    item = AuctionItem(
        auction_id="x-report",
        case_number="2025타경10101",
        item_number="2",
        min_price=18_000_000,
        appraisal_price=42_000_000,
        failed_count=2,
        land_area_m2=1180,
        market_estimate=36_000_000,
    )
    item.score = 90.8
    df = format_dataframe_for_report(to_dataframe([item], {"label": "실데이터", "is_sample": False}))
    assert df.loc[0, "물건번호"] == "2"
    assert df.loc[0, "최저매각가격"] == "18,000,000"
    assert df.loc[0, "감정평가액"] == "42,000,000"
    assert df.loc[0, "유찰횟수"] == "2"
    assert df.loc[0, "토지면적(㎡)"] == "1,180"


def test_sample_report_is_clearly_marked():
    from landwatch.report import to_dataframe

    item = AuctionItem(auction_id="sample", case_number="SAMPLE-CASE-001", item_number="TEST-1")
    df = to_dataframe([item], {"label": "기능 테스트용 샘플 데이터", "is_sample": True})
    assert df.loc[0, "데이터 출처"] == "기능 테스트용 샘플 데이터"
    assert df.loc[0, "사건번호 확인"] == "샘플(검색불가)"


def test_court_selenium_search_body_matches_site_shape():
    from landwatch.court_selenium import build_search_body

    p = profile()
    body = build_search_body(
        p,
        page=2,
        page_size=100,
        cfg={"sale_window_days": 13, "force_land_category": True},
        today=date(2026, 6, 15),
    )
    page_info = body["dma_pageInfo"]
    search = body["dma_srchGdsDtlSrchInfo"]
    assert page_info["pageNo"] == 2
    assert page_info["pageSize"] == 100
    assert search["lclDspslGdsLstUsgCd"] == "10000"
    assert search["lwsDspslPrcMin"] == "5000000"
    assert search["lwsDspslPrcMax"] == "30000000"
    assert search["flbdNcntMin"] == "1"
    assert search["objctArDtsMin"] == "330"
    assert search["lwsDspslPrcRateMin"] == "10"
    assert search["lwsDspslPrcRateMax"] == "80"
    assert search["bidBgngYmd"] == "20260615"
    assert search["bidEndYmd"] == "20260628"
    assert search["cortStDvs"] == "1"


def test_court_selenium_row_normalization():
    from landwatch.court_selenium import normalize_search_row

    raw = {
        "printCsNo": "2025 타경 12345",
        "mokmulSer": "2",
        "boCd": "B000123",
        "jiwonNm": "청주지방법원 충주지원",
        "hjguSido": "충청북도",
        "hjguSigu": "충주시",
        "hjguDong": "소태면",
        "daepyoLotno": "구룡리 158",
        "gamevalAmt": "42,000,000원",
        "minmaePrice": "18,000,000원",
        "yuchalCnt": "2",
        "maeGiil": "20260703",
        "jimokList": "전",
        "areaList": "토지 1,180㎡",
        "mulBigo": "농지취득자격증명 필요",
        "docid": "REAL-DOC-1",
        "lclsUtilCd": "10000",
    }
    item = normalize_search_row(raw)
    assert item.case_number == "2025타경12345"
    assert item.item_number == "2"
    assert item.min_price == 18_000_000
    assert item.appraisal_price == 42_000_000
    assert item.land_area_m2 == 1180
    assert item.failed_count == 2
    assert item.usage == "전"
    assert item.status == "진행/유찰"
    assert item.province == "충청북도"
    assert item.city_county == "충주시"
    assert "농지취득자격증명" in item.special_conditions[0]


def test_court_search_row_prefers_next_notified_price_over_previous_failed_price():
    from landwatch.court_selenium import normalize_search_row

    item = normalize_search_row({
        "printCsNo": "2025 타경 209",
        "mokmulSer": "1",
        "docid": "COURT-NEXT-PRICE",
        "hjguSido": "전북특별자치도",
        "hjguSigu": "부안군",
        "hjguDong": "행안면",
        "daepyoLotno": "역리 1117",
        "gamevalAmt": "1,188,697,150",
        "minmaePrice": "407,723,000",
        "notifyMinmaePrice1": "285,406,000",
        "notifyMinmaePrice2": "0",
        "yuchalCnt": "3",
        "maeGiil": "20260720",
        "lclsUtilCd": "20000",
    })

    assert item.min_price == 285_406_000
    assert item.raw["court_list_price_source"] == "notifyMinmaePrice1"
    assert item.raw["court_list_previous_min_price"] == 407_723_000


def test_region_alias_matches_real_court_address():
    item = AuctionItem(
        auction_id="real-region",
        status="유찰",
        usage="전",
        address="충청북도 충주시 소태면 구룡리 158",
        province="충청북도",
        city_county="충주시",
        min_price=18_000_000,
        appraisal_price=42_000_000,
        failed_count=2,
        land_area_m2=1180,
        auction_date=date(2026, 6, 20),
    )
    ok, reasons = matches_profile(item, profile(), today=date(2026, 6, 15))
    assert ok, reasons


def test_court_selenium_run_falls_back_from_100_to_20_on_http_400():
    from landwatch.court_selenium import (
        CourtAuctionHttpError,
        CourtAuctionSeleniumProvider,
    )

    provider = CourtAuctionSeleniumProvider({
        "page_size": 100,
        "max_pages": 1,
        "max_calls_per_run": 3,
    })
    requested_sizes = []

    def fake_post(body):
        size = body["dma_pageInfo"]["pageSize"]
        requested_sizes.append(size)
        if size == 100:
            raise CourtAuctionHttpError(400, "HTTP 400")
        return {
            "data": {
                "dma_pageInfo": {"pageNo": 1, "pageSize": 20, "totalCnt": 0},
                "dlt_srchResult": [],
            }
        }

    provider._post_json = fake_post
    result = provider.fetch(profile())
    assert result == []
    assert requested_sizes == [100, 20, 20]  # 3번째는 시·군·구 호환검색


def test_court_selenium_non_400_error_is_not_retried():
    import pytest
    from landwatch.court_selenium import (
        CourtAuctionHttpError,
        CourtAuctionSeleniumProvider,
    )

    provider = CourtAuctionSeleniumProvider({
        "page_size": 100,
        "max_pages": 1,
        "max_calls_per_run": 3,
    })
    calls = []

    def fake_post(body):
        calls.append(body["dma_pageInfo"]["pageSize"])
        raise CourtAuctionHttpError(503, "HTTP 503")

    provider._post_json = fake_post
    with pytest.raises(CourtAuctionHttpError):
        provider.fetch(profile())
    assert calls == [100]


def test_region_gui_has_nationwide_city_county_options_and_codes():
    from landwatch.regions import municipality_names, province_names, resolve_region_codes

    assert len(province_names()) == 17
    options = municipality_names(["충청북도", "강원특별자치도"])
    assert "충청북도 충주시" in options
    assert "강원특별자치도 원주시" in options
    assert resolve_region_codes("충북 충주시") == {"sido": "43", "sigungu": "130", "dong": ""}
    assert resolve_region_codes("강원특별자치도 원주시") == {"sido": "51", "sigungu": "130", "dong": ""}


def test_gui_terms_and_risk_keywords_are_normalized():
    from landwatch.ui_config import exclusion_keywords, exclusion_labels

    labels = ["지분매각", "토지만 매각", "법정지상권 성립 가능성"]
    keywords = exclusion_keywords(labels, "급경사, 문화재")
    assert "지분" in keywords
    assert "토지만매각" in keywords
    assert "법정지상권" in keywords
    assert "급경사" in keywords
    assert "지분매각" in exclusion_labels(keywords)


def test_filter_matches_risk_keyword_even_when_spaces_differ():
    p = profile()
    p["exclude_keywords"] = ["토지만매각"]
    item = AuctionItem(
        auction_id="space-risk", status="유찰", usage="전", address="충북 충주시",
        province="충북", city_county="충주시", min_price=10_000_000,
        appraisal_price=30_000_000, failed_count=2, land_area_m2=660,
        special_conditions=["토지만 매각 대상"], auction_date=date(2026, 7, 3),
    )
    ok, reasons = matches_profile(item, p, today=date(2026, 6, 14))
    assert not ok
    assert any("토지만매각" in reason for reason in reasons)


def test_config_raw_load_preserves_environment_placeholders(tmp_path):
    from landwatch.config import load_config, save_config

    path = tmp_path / "config.yaml"
    path.write_text('''notifications:
  telegram:
    enabled: false
    bot_token: ${TEST_BOT_TOKEN}
    chat_id: ${TEST_CHAT_ID}
profiles: []
''', encoding="utf-8")
    cfg = load_config(path, expand_environment=False)
    assert cfg["notifications"]["telegram"]["bot_token"] == "${TEST_BOT_TOKEN}"
    save_config(cfg, path)
    assert "${TEST_BOT_TOKEN}" in path.read_text(encoding="utf-8")


def test_optional_appraisal_range_zero_means_no_limit():
    p = profile()
    p["appraisal_price"] = {"min": 0, "max": 0}
    item = AuctionItem(
        auction_id="no-appraisal-limit", status="유찰", usage="전",
        address="충북 충주시", province="충북", city_county="충주시",
        min_price=10_000_000, appraisal_price=99_000_000, failed_count=2,
        land_area_m2=660, auction_date=date(2026, 7, 3),
    )
    ok, reasons = matches_profile(item, p, today=date(2026, 6, 14))
    assert ok, reasons

    from landwatch.court_selenium import build_search_body
    body = build_search_body(p, page=1, page_size=20, today=date(2026, 6, 15))
    search = body["dma_srchGdsDtlSrchInfo"]
    assert search["aeeEvlAmtMin"] == ""
    assert search["aeeEvlAmtMax"] == ""


def test_selected_region_is_always_sent_to_court_server_even_when_legacy_flag_is_false():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    p = profile()
    provider = CourtAuctionSeleniumProvider({
        "page_size": 20,
        "max_pages": 1,
        "max_calls_per_run": 2,
        "server_side_region_filter": False,  # 이전 config.yaml 값도 안전하게 무시
    })
    captured = []

    def fake_post(body):
        captured.append(body)
        return {
            "data": {
                "dma_pageInfo": {"pageNo": 1, "pageSize": 20, "totalCnt": 0},
                "dlt_srchResult": [],
            }
        }

    provider._post_json = fake_post
    assert provider.fetch(p) == []
    search = captured[0]["dma_srchGdsDtlSrchInfo"]
    assert search["rprsAdongSdCd"] == "43"
    assert search["rprsAdongSggCd"] == "130"
    assert search["cortStDvs"] == "2"


def test_province_only_court_search_uses_nationwide_request_and_local_filter():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    p = profile()
    p["regions"] = ["경기도"]
    p["auction_within_days"] = 0
    provider = CourtAuctionSeleniumProvider({
        "page_size": 20,
        "max_pages": 1,
        "max_calls_per_run": 2,
    })
    captured = []

    def fake_post(body):
        captured.append(body)
        return {
            "data": {
                "dma_pageInfo": {"pageNo": 1, "pageSize": 100, "totalCnt": 2},
                "dlt_srchResult": [
                    {
                        "printCsNo": "2026 타경 1", "mokmulSer": "1", "docid": "GG-1",
                        "hjguSido": "경기도", "hjguSigu": "가평군",
                        "gamevalAmt": "30,000,000원", "minmaePrice": "20,000,000원",
                        "yuchalCnt": "1", "maeGiil": date.today().strftime("%Y%m%d"),
                        "jimokList": "전", "areaList": "토지 500㎡",
                    },
                    {
                        "printCsNo": "2026 타경 2", "mokmulSer": "1", "docid": "GW-1",
                        "hjguSido": "강원특별자치도", "hjguSigu": "춘천시",
                        "gamevalAmt": "30,000,000원", "minmaePrice": "20,000,000원",
                        "yuchalCnt": "1", "maeGiil": date.today().strftime("%Y%m%d"),
                        "jimokList": "전", "areaList": "토지 500㎡",
                    },
                ],
            }
        }

    provider._post_json = fake_post
    items = provider.fetch(p)
    search = captured[0]["dma_srchGdsDtlSrchInfo"]
    assert search["rprsAdongSdCd"] == ""
    assert search["rprsAdongSggCd"] == ""
    assert search["cortStDvs"] == "1"
    assert [item.province for item in items] == ["경기도"]
    assert provider.last_fetch_diagnostics[0]["검색방식"] == "전국 대체검색 후 시·도 주소검증"


def test_connection_check_uses_selected_region_instead_of_nationwide():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    p = profile()
    p["regions"] = ["제주특별자치도"]
    provider = CourtAuctionSeleniumProvider({"page_size": 20, "max_calls_per_run": 2})
    captured = []

    def fake_post(body):
        captured.append(body)
        return {
            "data": {
                "dma_pageInfo": {"pageNo": 1, "pageSize": 20, "totalCnt": 3},
                "dlt_srchResult": [],
            }
        }

    provider._post_json = fake_post
    result = provider.test_connection(p)
    search = captured[0]["dma_srchGdsDtlSrchInfo"]
    assert search["rprsAdongSdCd"] == "50"
    assert result["queried_region"] == "제주특별자치도"
    assert result["total_count"] == 3


def test_buan_region_has_current_and_legacy_code_variants():
    from landwatch.regions import resolve_region_code_variants

    assert resolve_region_code_variants("전북특별자치도 부안군") == [
        {"sido": "52", "sigungu": "800", "dong": ""},
        {"sido": "45", "sigungu": "800", "dong": ""},
    ]


def test_gangwon_municipality_has_current_and_legacy_code_variants():
    from landwatch.regions import resolve_region_code_variants

    assert resolve_region_code_variants("강원특별자치도 원주시") == [
        {"sido": "51", "sigungu": "130", "dong": ""},
        {"sido": "42", "sigungu": "130", "dong": ""},
    ]


def test_normal_municipality_uses_single_region_code():
    from landwatch.regions import resolve_region_code_variants

    assert resolve_region_code_variants("충청북도 충주시") == [
        {"sido": "43", "sigungu": "130", "dong": ""},
    ]


def test_buan_search_retries_legacy_code_when_current_code_returns_zero():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    p = profile()
    p["regions"] = ["전북특별자치도 부안군"]
    provider = CourtAuctionSeleniumProvider({
        "page_size": 20,
        "max_pages": 1,
        "max_calls_per_run": 4,
        "legacy_code_fallback_only": False,
    })
    requested = []

    def fake_post(body):
        search = body["dma_srchGdsDtlSrchInfo"]
        requested.append((search["rprsAdongSdCd"], search["rprsAdongSggCd"]))
        if search["rprsAdongSdCd"] == "52":
            return {"data": {"dma_pageInfo": {"totalCnt": 0}, "dlt_srchResult": []}}
        return {
            "data": {
                "dma_pageInfo": {"totalCnt": 1},
                "dlt_srchResult": [{
                    "printCsNo": "2025 타경 10001",
                    "mokmulSer": "1",
                    "hjguSido": "전라북도",
                    "hjguSigu": "부안군",
                    "hjguDong": "변산면",
                    "daepyoLotno": "도청리 1",
                    "gamevalAmt": "30,000,000원",
                    "minmaePrice": "20,000,000원",
                    "yuchalCnt": "1",
                    "maeGiil": "20260620",
                    "jimokList": "전",
                    "areaList": "토지 500㎡",
                    "docid": "BUAN-1",
                    "lclsUtilCd": "10000",
                }],
            }
        }

    provider._post_json = fake_post
    items = provider.fetch(p)
    assert requested == [("52", "800"), ("45", "800")]
    assert len(items) == 1
    assert items[0].city_county == "부안군"
    assert provider.last_fetch_diagnostics[0]["비고"].startswith("0건")
    assert provider.last_fetch_diagnostics[1]["코드구분"] == "과거코드 병행"


def test_buan_search_also_checks_legacy_code_when_current_code_has_results():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    p = profile()
    p["regions"] = ["전북특별자치도 부안군"]
    provider = CourtAuctionSeleniumProvider({
        "page_size": 20,
        "max_pages": 1,
        "max_calls_per_run": 4,
        "legacy_code_fallback_only": False,
    })
    requested = []

    def fake_post(body):
        search = body["dma_srchGdsDtlSrchInfo"]
        requested.append((search["rprsAdongSdCd"], search["rprsAdongSggCd"]))
        return {"data": {"dma_pageInfo": {"totalCnt": 1}, "dlt_srchResult": [{
            "printCsNo": "2025 타경 10002", "mokmulSer": "1", "docid": "BUAN-2",
            "hjguSido": "전북특별자치도", "hjguSigu": "부안군",
            "gamevalAmt": "30,000,000원", "minmaePrice": "20,000,000원",
            "yuchalCnt": "1", "maeGiil": "20260620", "jimokList": "전",
            "areaList": "토지 500㎡", "lclsUtilCd": "10000",
        }]}}

    provider._post_json = fake_post
    items = provider.fetch(p)
    assert requested == [("52", "800"), ("45", "800")]
    assert len(items) == 1  # 동일 docid는 자동 중복 제거


def test_90_day_range_is_split_into_all_court_safe_windows():
    from landwatch.court_selenium import build_sale_date_windows

    p = profile()
    p["auction_within_days"] = 90
    windows = build_sale_date_windows(p, {"sale_window_days": 13}, today=date(2026, 6, 15))
    assert len(windows) == 7
    assert windows[0] == (date(2026, 6, 15), date(2026, 6, 28))
    assert windows[-1] == (date(2026, 9, 7), date(2026, 9, 13))
    for previous, current in zip(windows, windows[1:]):
        assert (current[0] - previous[1]).days == 1


def test_buan_90_day_search_checks_every_window_and_both_code_generations(monkeypatch):
    from landwatch.court_selenium import CourtAuctionSeleniumProvider
    import landwatch.court_selenium as court_module

    monkeypatch.setattr(court_module, "date", type("FixedDate", (date,), {
        "today": classmethod(lambda cls: cls(2026, 6, 15))
    }))

    p = profile()
    p["regions"] = ["전북특별자치도 부안군"]
    p["auction_within_days"] = 90
    provider = CourtAuctionSeleniumProvider({
        "page_size": 20,
        "max_pages": 1,
        "max_calls_per_run": 10,  # 이전 GUI 기본값이어도 자동으로 14회까지 확장
        "hard_call_cap": 30,
        "sale_window_days": 13,
        "legacy_code_fallback_only": False,
    })
    requested = []

    def fake_post(body):
        search = body["dma_srchGdsDtlSrchInfo"]
        key = (
            search["rprsAdongSdCd"], search["rprsAdongSggCd"],
            search["bidBgngYmd"], search["bidEndYmd"],
        )
        requested.append(key)
        # 마지막 날짜 구간의 과거코드에서만 실제 물건이 있다고 가정한다.
        if key == ("45", "800", "20260907", "20260913"):
            return {"data": {"dma_pageInfo": {"totalCnt": 1}, "dlt_srchResult": [{
                "printCsNo": "2025 타경 20001", "mokmulSer": "1", "docid": "BUAN-LATE",
                "hjguSido": "전라북도", "hjguSigu": "부안군", "hjguDong": "변산면",
                "gamevalAmt": "30,000,000원", "minmaePrice": "20,000,000원",
                "yuchalCnt": "1", "maeGiil": "20260910", "jimokList": "전",
                "areaList": "토지 500㎡", "lclsUtilCd": "10000",
            }]}}
        return {"data": {"dma_pageInfo": {"totalCnt": 0}, "dlt_srchResult": []}}

    provider._post_json = fake_post
    items = provider.fetch(p)
    assert len(requested) == 14
    assert requested[0] == ("52", "800", "20260615", "20260628")
    assert requested[6] == ("52", "800", "20260907", "20260913")
    assert requested[-1] == ("45", "800", "20260907", "20260913")
    assert requested[7] == ("45", "800", "20260615", "20260628")
    assert requested[13] == ("45", "800", "20260907", "20260913")
    assert requested[-1] == ("45", "800", "20260907", "20260913")
    assert provider.call_limit == 14
    assert len(items) == 1
    assert items[0].auction_id == "BUAN-LATE"
    assert provider.last_fetch_diagnostics[0]["완료구간"] == "7/7"
    assert provider.last_fetch_diagnostics[1]["완료구간"] == "7/7"


def test_region_filter_accepts_legacy_jeonbuk_address_for_current_name():
    p = profile()
    p["regions"] = ["전북특별자치도 부안군"]
    p["land_area_m2"] = {"min": 0, "max": 0}
    p["min_price"] = {"min": 0, "max": 0}
    p["failed_count"] = {"min": 0, "max": 10}
    p["appraisal_discount_percent"] = {"min": 0, "max": 100}
    item = AuctionItem(
        auction_id="legacy-buan", status="유찰", usage="전",
        address="전라북도 부안군 변산면 도청리 1",
        province="전라북도", city_county="부안군",
        min_price=20_000_000, appraisal_price=30_000_000,
        failed_count=1, land_area_m2=500, auction_date=date(2026, 6, 20),
    )
    ok, reasons = matches_profile(item, p, today=date(2026, 6, 15))
    assert ok, reasons


def test_exact_municipality_uses_minimal_server_filters_first():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    p = profile()
    p["regions"] = ["충청북도 충주시"]
    provider = CourtAuctionSeleniumProvider({
        "page_size": 20, "max_pages": 1, "max_calls_per_run": 2,
        "hard_call_cap": 60, "sale_window_days": 13,
    })
    captured = []

    def fake_post(body):
        search = body["dma_srchGdsDtlSrchInfo"]
        captured.append(dict(search))
        return {"data": {"dma_pageInfo": {"totalCnt": 1}, "dlt_srchResult": [{
            "printCsNo": "2025 타경 30001", "mokmulSer": "1", "docid": "RELAXED-1",
            "hjguSido": "충청북도", "hjguSigu": "충주시",
            "gamevalAmt": "30,000,000원", "minmaePrice": "20,000,000원",
            "yuchalCnt": "1", "maeGiil": "20260620", "jimokList": "전",
            "areaList": "토지 500㎡", "lclsUtilCd": "10000",
        }]}}

    provider._post_json = fake_post
    items = provider.fetch(p)
    assert len(captured) == 1
    assert captured[0]["rprsAdongSggCd"] == "130"
    assert captured[0]["flbdNcntMin"] == ""
    assert captured[0]["lwsDspslPrcMin"] == ""
    assert captured[0]["objctArDtsMin"] == "330"
    assert captured[0]["objctArDtsMax"] == "3300"
    assert captured[0]["lwsDspslPrcRateMin"] == ""
    assert len(items) == 1
    assert captured[0]["lclDspslGdsLstUsgCd"] == ""
    assert any(d["코드구분"] == "대체검색 생략" for d in provider.last_fetch_diagnostics)
    assert provider.last_fetch_diagnostics[0]["검색방식"] == "시·군·구 완전 최소조건검색"
    assert provider.last_fetch_diagnostics[-1]["검색방식"] == "시·도 대체검색 후 주소검증"


def test_exact_municipality_zero_falls_back_to_province_and_local_address_filter():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    p = profile()
    p["regions"] = ["전북특별자치도 부안군"]
    provider = CourtAuctionSeleniumProvider({
        "page_size": 20, "max_pages": 1, "max_calls_per_run": 4,
        "hard_call_cap": 60, "sale_window_days": 13,
    })
    captured = []

    def fake_post(body):
        search = body["dma_srchGdsDtlSrchInfo"]
        captured.append((search["rprsAdongSdCd"], search["rprsAdongSggCd"]))
        if search["rprsAdongSggCd"]:
            return {"data": {"dma_pageInfo": {"totalCnt": 0}, "dlt_srchResult": []}}
        return {"data": {"dma_pageInfo": {"totalCnt": 1}, "dlt_srchResult": [{
            "printCsNo": "2025 타경 40001", "mokmulSer": "1", "docid": "PROV-1",
            "hjguSido": "전북특별자치도", "hjguSigu": "부안군",
            "gamevalAmt": "30,000,000원", "minmaePrice": "20,000,000원",
            "yuchalCnt": "1", "maeGiil": "20260620", "jimokList": "전",
            "areaList": "토지 500㎡", "lclsUtilCd": "10000",
        }]}}

    provider._post_json = fake_post
    items = provider.fetch(p)
    assert captured[:2] == [("52", "800"), ("45", "800")]
    assert captured[2] == ("52", "")
    assert len(items) == 1
    assert items[0].city_county == "부안군"
    assert any(d["검색방식"] == "시·도 대체검색 후 주소검증" for d in provider.last_fetch_diagnostics)



def test_usage_parser_accepts_land_category_followed_immediately_by_digits():
    from landwatch.court_selenium import normalize_search_row
    item = normalize_search_row({
        "printCsNo": "2025 타경 385",
        "mokmulSer": "1",
        "docid": "CASE-385",
        "hjguSido": "전북특별자치도",
        "hjguSigu": "부안군",
        "jimokList": "전123㎡, 답456㎡",
        "areaList": "전123㎡ 답456㎡",
        "gamevalAmt": "50,000,000원",
        "minmaePrice": "40,000,000원",
        "yuchalCnt": "1",
        "maeGiil": "20260630",
    })
    assert "전" in item.usage
    assert "답" in item.usage


def test_case_schedule_parser_supports_current_and_history_field_names():
    from landwatch.court_selenium import _extract_case_schedule_rows

    rows = _extract_case_schedule_rows({
        "dlt_dspslGdsDspslObjctLst": [{
            "dspslGdsSeq": 1,
            "dspslDxdyYmd": "20260715",
            "fstDspslHm": "1030",
            "aeeEvlAmt": 100_000_000,
            "scndPbancLwsDspslPrc": 64_000_000,
            "auctnDxdyGdsStatCd": "00",
        }],
        "dlt_rletCsGdsDtsDxdyInf": [{
            "dspslGdsSeq": 1,
            "dxdyYmd": "20260610",
            "dxdyHm": "1000",
            "dxdyPlcNm": "경매법정",
            "auctnDxdyRsltCd": "002",
        }],
    })
    assert [x["매각기일"] for x in rows] == ["2026-07-15", "2026-06-10"]
    assert rows[0]["최저매각가격"] == 64_000_000
    assert rows[0]["기일구분"] == "현재 예정기일"
    assert rows[1]["결과코드"] == "002"


def test_lookup_case_prefers_buan_related_court_and_returns_schedule(monkeypatch):
    from landwatch.court_selenium import CourtAuctionSeleniumProvider, COURTS_PATH, CASE_DETAIL_PATH

    provider = CourtAuctionSeleniumProvider({"max_calls_per_run": 10, "hard_call_cap": 60})
    calls = []

    def fake_endpoint(path, body, **kwargs):
        calls.append((path, body))
        if path == COURTS_PATH:
            return {"data": {"result": [
                {"cortOfcCd": "B001", "cortOfcNm": "전주지방법원", "cortSptNm": "정읍지원"},
                {"cortOfcCd": "B002", "cortOfcNm": "전주지방법원", "cortSptNm": "군산지원"},
            ]}}
        assert path == CASE_DETAIL_PATH
        assert body["dma_srchCsDtlInf"]["cortOfcCd"] == "B001"
        return {"data": {
            "dma_csBasInf": {
                "cortOfcNm": "전주지방법원 정읍지원",
                "csNo": "2025타경385",
                "csNm": "부동산임의경매",
                "csProgStatCd": "진행",
            },
            "dlt_rletCsDspslObjctLst": [
                {"dspslObjctSeq": "1", "userSt": "전북특별자치도 부안군 변산면 예시리 1"}
            ],
            "dlt_dspslGdsDspslObjctLst": [
                {
                    "dspslGdsSeq": "1", "dspslDxdyYmd": "20260701",
                    "fstDspslHm": "1000", "aeeEvlAmt": "50,000,000",
                    "fstPbancLwsDspslPrc": "40,000,000",
                    "auctnDxdyGdsStatCd": "00"
                }
            ],
            "dlt_rletCsGdsDtsDxdyInf": [
                {
                    "dspslGdsSeq": "1", "dxdyYmd": "20260601",
                    "dxdyHm": "1000", "dxdyPlcNm": "정읍지원 경매법정",
                    "auctnDxdyRsltCd": "002"
                }
            ],
        }}

    monkeypatch.setattr(provider, "_post_json_endpoint", fake_endpoint)
    p = profile()
    p["regions"] = ["전북특별자치도 부안군"]
    p["auction_within_days"] = 90
    result = provider.lookup_case("2025 타경 385", p)
    assert result["found"] is True
    assert result["사건번호"] == "2025타경385"
    assert "정읍지원" in result["법원"]
    assert result["물건내역"][0]["소재지"].startswith("전북특별자치도 부안군")
    assert len(result["매각기일내역"]) == 2
    current = [x for x in result["매각기일내역"] if x["기일구분"].startswith("현재")][0]
    history = [x for x in result["매각기일내역"] if x["기일구분"] == "기일내역"][0]
    assert current["매각기일"] == "2026-07-01"
    assert current["매각시간"] == "10:00"
    assert current["최저매각가격"] == 40_000_000
    assert result["다음매각기일"] == "2026-07-01"
    assert history["매각기일"] == "2026-06-01"
    assert history["매각장소"] == "정읍지원 경매법정"
    assert len(calls) == 2


def test_province_fallback_discards_other_municipalities_before_runner():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    p = profile()
    p["regions"] = ["전북특별자치도 부안군"]
    p["auction_within_days"] = 0
    provider = CourtAuctionSeleniumProvider({
        "page_size": 20, "max_pages": 1, "max_calls_per_run": 8,
        "hard_call_cap": 20, "sale_window_days": 13,
    })

    def fake_post(body):
        search = body["dma_srchGdsDtlSrchInfo"]
        # 정확 시군구 검색은 0건, 시도 대체검색은 부안군+군산시 혼합 반환
        if search["rprsAdongSggCd"]:
            return {"data": {"dma_pageInfo": {"totalCnt": 0}, "dlt_srchResult": []}}
        return {"data": {"dma_pageInfo": {"totalCnt": 2}, "dlt_srchResult": [
            {
                "printCsNo": "2025 타경 385", "mokmulSer": "1", "docid": "BUAN-385",
                "hjguSido": "전북특별자치도", "hjguSigu": "부안군",
                "gamevalAmt": "50,000,000원", "minmaePrice": "40,000,000원",
                "yuchalCnt": "1", "maeGiil": date.today().strftime("%Y%m%d"),
                "jimokList": "전", "areaList": "토지 500㎡",
            },
            {
                "printCsNo": "2025 타경 999", "mokmulSer": "1", "docid": "GUNSAN-999",
                "hjguSido": "전북특별자치도", "hjguSigu": "군산시",
                "gamevalAmt": "50,000,000원", "minmaePrice": "40,000,000원",
                "yuchalCnt": "1", "maeGiil": date.today().strftime("%Y%m%d"),
                "jimokList": "전", "areaList": "토지 500㎡",
            },
        ]}}

    provider._post_json = fake_post
    items = provider.fetch(p)
    assert [item.case_number for item in items] == ["2025타경385"]
    assert all(item.city_county == "부안군" for item in items)
    fallback = [d for d in provider.last_fetch_diagnostics if "대체검색" in d["검색방식"]]
    assert fallback
    assert sum(d["지역일치건수"] for d in fallback) >= 1
    assert sum(d["지역불일치제외"] for d in fallback) >= 1


def test_exact_municipality_match_skips_province_fallback():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    p = profile()
    p["regions"] = ["전북특별자치도 부안군"]
    p["auction_within_days"] = 0
    provider = CourtAuctionSeleniumProvider({
        "page_size": 20, "max_pages": 1, "max_calls_per_run": 8,
        "hard_call_cap": 20, "sale_window_days": 13,
    })
    captured = []

    def fake_post(body):
        search = body["dma_srchGdsDtlSrchInfo"]
        captured.append((search["rprsAdongSdCd"], search["rprsAdongSggCd"]))
        return {"data": {"dma_pageInfo": {"totalCnt": 1}, "dlt_srchResult": [{
            "printCsNo": "2025 타경 385", "mokmulSer": "1", "docid": "BUAN-EXACT",
            "hjguSido": "전북특별자치도", "hjguSigu": "부안군",
            "gamevalAmt": "50,000,000원", "minmaePrice": "40,000,000원",
            "yuchalCnt": "1", "maeGiil": date.today().strftime("%Y%m%d"),
            "jimokList": "전", "areaList": "토지 500㎡",
        }]}}

    provider._post_json = fake_post
    items = provider.fetch(p)
    assert len(items) == 1
    assert captured == [("52", "800")]
    assert any(d["코드구분"] == "과거코드 조회 생략" for d in provider.last_fetch_diagnostics)
    assert any(d["코드구분"] == "대체검색 생략" for d in provider.last_fetch_diagnostics)


def test_detail_popup_payload_and_map_urls():
    from landwatch.detail_view import build_detail_payload, build_map_urls

    item = AuctionItem(
        auction_id="detail-1",
        case_number="2025타경385",
        item_number="1",
        court="전주지방법원 정읍지원",
        status="유찰",
        usage="답",
        address="전북특별자치도 부안군 변산면 대항리 123",
        min_price=24_000_000,
        appraisal_price=40_000_000,
        failed_count=2,
        land_area_m2=991.74,
        auction_date=date(2026, 7, 20),
        special_conditions=["농지취득자격증명 필요"],
        raw={"jimokList": "답", "areaList": "991.74㎡", "maePlace": "정읍지원 경매법정"},
    )
    item.score = 78.5
    item.grade = "관심"
    item.score_reasons = ["감정평가액 대비 40% 할인"]
    payload = build_detail_payload(item)
    assert payload["사건번호"] == "2025타경385"
    assert payload["최저매각가격"] == "24,000,000원"
    assert payload["토지면적"] == "991.74㎡"
    assert payload["감정평가액 대비 할인율"] == "40%"
    assert "농지취득자격증명" in payload["특수조건·비고"][0]

    urls = build_map_urls(payload["소재지"])
    assert urls["primary"] == urls["naver"]
    assert "%EC%A0%84%EB%B6%81" in urls["naver"]
    assert urls["kakao"].startswith("https://map.kakao.com/link/search/")


def test_naver_map_is_primary_and_google_is_secondary():
    from landwatch.detail_view import build_map_urls

    urls = build_map_urls("전북특별자치도 부안군 부안읍")
    assert urls["primary"] == urls["naver"]
    assert "map.naver.com" in urls["naver"]
    assert "google.com/maps" in urls["google"]
    assert "map.kakao.com" in urls["kakao"]


def test_naver_map_html_requires_client_id_and_uses_ncp_key_id():
    from landwatch.detail_view import build_naver_map_html

    assert build_naver_map_html("전북특별자치도 부안군", "") == ""
    content = build_naver_map_html("전북특별자치도 부안군", "sample-client-id")
    assert "ncpKeyId=sample-client-id" in content
    assert "submodules=geocoder" in content
    assert "전북특별자치도 부안군" in content


def _buan_profile_for_speed_test():
    p = profile()
    p.update({
        "regions": ["전북특별자치도 부안군"],
        "auction_within_days": 1,
    })
    return p


def _buan_row_for_speed_test():
    return {
        "printCsNo": "2025타경385",
        "mokmulSer": "1",
        "jiwonNm": "전주지방법원 정읍지원",
        "hjguSido": "전북특별자치도",
        "hjguSigu": "부안군",
        "hjguDong": "부안읍",
        "daepyoLotno": "봉덕리 1",
        "gamevalAmt": "30,000,000",
        "minmaePrice": "20,000,000",
        "yuchalCnt": "1",
        "maeGiil": "20260616",
        "jimokList": "전",
        "areaList": "330㎡",
        "docid": "BUAN-1",
    }


def test_current_special_province_code_success_skips_legacy_and_province_fallback():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    provider = CourtAuctionSeleniumProvider({
        "page_size": 20,
        "max_pages": 1,
        "max_calls_per_run": 20,
        "legacy_code_fallback_only": True,
        "cache_enabled": False,
    })
    calls = []

    def fake_post(body):
        search = body["dma_srchGdsDtlSrchInfo"]
        calls.append((search["rprsAdongSdCd"], search["rprsAdongSggCd"]))
        return {
            "data": {
                "dma_pageInfo": {"pageNo": 1, "pageSize": 20, "totalCnt": 1},
                "dlt_srchResult": [_buan_row_for_speed_test()],
            }
        }

    provider._post_json = fake_post
    items = provider.fetch(_buan_profile_for_speed_test())
    assert len(items) == 1
    assert calls == [("52", "800")]
    assert any(d.get("코드구분") == "과거코드 조회 생략" for d in provider.last_fetch_diagnostics)


def test_legacy_code_is_used_only_when_current_code_returns_no_target_rows():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    provider = CourtAuctionSeleniumProvider({
        "page_size": 20,
        "max_pages": 1,
        "max_calls_per_run": 20,
        "legacy_code_fallback_only": True,
        "cache_enabled": False,
    })
    calls = []

    def fake_post(body):
        search = body["dma_srchGdsDtlSrchInfo"]
        sido = search["rprsAdongSdCd"]
        calls.append((sido, search["rprsAdongSggCd"]))
        rows = [] if sido == "52" else [_buan_row_for_speed_test()]
        return {
            "data": {
                "dma_pageInfo": {"pageNo": 1, "pageSize": 20, "totalCnt": len(rows)},
                "dlt_srchResult": rows,
            }
        }

    provider._post_json = fake_post
    items = provider.fetch(_buan_profile_for_speed_test())
    assert len(items) == 1
    assert calls == [("52", "800"), ("45", "800")]


def test_search_response_disk_cache_avoids_second_network_call(tmp_path):
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    cfg = {
        "cache_enabled": True,
        "cache_ttl_minutes": 15,
        "cache_dir": str(tmp_path / "cache"),
    }
    body = {"dma_pageInfo": {"pageNo": 1}, "dma_srchGdsDtlSrchInfo": {"x": "y"}}
    payload = {"data": {"dlt_srchResult": []}}
    calls = []

    first = CourtAuctionSeleniumProvider(cfg)
    first._post_json_endpoint = lambda *args, **kwargs: (calls.append(1) or payload)
    assert first._post_json(body) == payload
    assert len(calls) == 1

    second = CourtAuctionSeleniumProvider(cfg)
    second._post_json_endpoint = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network called"))
    assert second._post_json(body) == payload
    assert second.cache_hits == 1


def test_next_court_schedule_uses_future_blank_result_not_previous_failed_price():
    from datetime import date
    from landwatch.court_selenium import _select_actionable_court_schedule, _apply_case_schedule_to_item
    from landwatch.models import AuctionItem

    case_data = {
        "dlt_rletCsGdsDtsDxdyInf": [
            {
                "dspslGdsSeq": "1",
                "dspslDxdyYmd": "20260427",
                "lwsDspslPrc": "1200000000",
                "aeeEvlAmt": "1500000000",
                "rsltCd": "유찰",
            },
            {
                "dspslGdsSeq": "1",
                "dspslDxdyYmd": "20260630",
                "lwsDspslPrc": "960000000",
                "aeeEvlAmt": "1500000000",
                "rsltCd": "",
            },
        ]
    }
    selected = _select_actionable_court_schedule(
        case_data, "1", today=date(2026, 6, 16), preferred_date=date(2026, 6, 30)
    )
    assert selected["최저매각가격"] == 960000000
    assert selected["매각기일"] == "2026-06-30"

    item = AuctionItem(
        auction_id="x", case_number="2024타경100001", item_number="1",
        min_price=1200000000, appraisal_price=1500000000,
        auction_date=date(2026, 6, 30), raw={},
    )
    _apply_case_schedule_to_item(item, case_data, today=date(2026, 6, 16))
    assert item.min_price == 960000000
    assert item.auction_date == date(2026, 6, 30)
    assert item.failed_count == 1
    assert item.raw["court_price_corrected"] is True
    assert item.raw["court_price_source"] == "사건상세 다음 예정기일"


def test_court_row_prefers_auction_lot_number_over_object_number():
    from landwatch.court_selenium import normalize_search_row

    item = normalize_search_row({
        "printCsNo": "2025 타경 50039",
        "maemulSer": "1",       # 실제 물건순번
        "mokmulSer": "155",     # 일괄물건 안의 목적물번호
        "boCd": "B000513",
        "jiwonNm": "순천지원",
        "hjguSido": "전라남도",
        "hjguSigu": "고흥군",
        "hjguDong": "동강면",
        "daepyoLotno": "장덕리 345-5",
        "gamevalAmt": "18,598,941,000",
        "minmaePrice": "8,326,233,000",
        "maeGiil": "20260622",
        "jimokList": "답",
        "areaList": "1,893㎡",
        "docid": "OBJECT-155",
    })

    assert item.item_number == "1"
    assert item.raw["court_lot_number"] == "1"
    assert item.raw["court_object_number"] == "155"


def test_court_provider_merges_components_of_same_auction_lot_without_multiplying_price():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    p = profile()
    p["regions"] = ["전남 고흥군"]
    p["auction_within_days"] = 0
    provider = CourtAuctionSeleniumProvider({
        "page_size": 20,
        "max_pages": 1,
        "max_calls_per_run": 4,
        "cache_enabled": False,
    })

    rows = []
    for object_no, lotno, usage, area in (
        ("1", "장덕리 산77-2", "임야", "15,769㎡"),
        ("2", "장덕리 870-4", "전", "10,896㎡"),
        ("3", "매곡리 154-2", "도로", "26㎡"),
    ):
        rows.append({
            "printCsNo": "2025 타경 50039",
            "maemulSer": "1",
            "mokmulSer": object_no,
            "boCd": "B000513",
            "jiwonNm": "순천지원",
            "hjguSido": "전라남도",
            "hjguSigu": "고흥군",
            "hjguDong": "동강면",
            "daepyoLotno": lotno,
            "gamevalAmt": "18,598,941,000",
            "minmaePrice": "8,326,233,000",
            "yuchalCnt": "3",
            "maeGiil": date.today().strftime("%Y%m%d"),
            "jimokList": usage,
            "areaList": area,
            "docid": f"OBJECT-{object_no}",
        })

    def fake_post(body):
        return {
            "data": {
                "dma_pageInfo": {"pageNo": 1, "pageSize": 20, "totalCnt": len(rows)},
                "dlt_srchResult": rows,
            }
        }

    provider._post_json = fake_post
    items = provider.fetch(p)

    assert len(items) == 1
    item = items[0]
    assert item.case_number == "2025타경50039"
    assert item.item_number == "1"
    assert item.min_price == 8_326_233_000
    assert item.appraisal_price == 18_598_941_000
    assert item.land_area_m2 == 26_691
    assert item.address == "전라남도 고흥군 동강면 장덕리 산77-2 외 2건"
    assert item.raw["court_component_count"] == 3
    assert item.raw["court_grouped_lot"] is True
    assert item.raw["court_price_scope"] == "매각물건 전체"
    assert "목적물 3건 포함" in " ".join(item.special_conditions)


def test_court_provider_keeps_different_auction_lot_numbers_separate():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    p = profile()
    p["regions"] = ["전남 고흥군"]
    p["auction_within_days"] = 0
    provider = CourtAuctionSeleniumProvider({
        "page_size": 20,
        "max_pages": 1,
        "max_calls_per_run": 4,
        "cache_enabled": False,
    })
    rows = []
    for lot_number, object_number, price, lotno in (
        ("1", "1", "20,000,000", "강산리 123-1"),
        ("2", "2", "30,000,000", "강산리 123-2"),
    ):
        rows.append({
            "printCsNo": "2025 타경 654",
            "maemulSer": lot_number,
            "mokmulSer": object_number,
            "boCd": "B000513",
            "jiwonNm": "순천지원",
            "hjguSido": "전라남도",
            "hjguSigu": "고흥군",
            "hjguDong": "점암면",
            "daepyoLotno": lotno,
            "gamevalAmt": price,
            "minmaePrice": price,
            "maeGiil": date.today().strftime("%Y%m%d"),
            "jimokList": "전",
            "areaList": "500㎡",
            "docid": f"LOT-{lot_number}",
        })

    provider._post_json = lambda body: {
        "data": {
            "dma_pageInfo": {"pageNo": 1, "pageSize": 20, "totalCnt": len(rows)},
            "dlt_srchResult": rows,
        }
    }
    items = provider.fetch(p)

    assert len(items) == 2
    assert {item.item_number for item in items} == {"1", "2"}



def test_goheung_uses_court_three_digit_sigungu_request_code():
    from landwatch.regions import resolve_region_codes

    assert resolve_region_codes("전남 고흥군") == {
        "sido": "46", "sigungu": "770", "dong": ""
    }


def test_configured_full_or_three_digit_sigungu_is_normalized_to_court_request_code():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    p = profile()
    p["regions"] = ["전남 고흥군"]
    provider = CourtAuctionSeleniumProvider({
        "region_code_map": {
            "전남 고흥군": {"sido": "46", "sigungu": "770", "dong": ""}
        }
    })
    assert provider._region_query_groups(p) == [
        ("전남 고흥군", [{"sido": "46", "sigungu": "770", "dong": ""}])
    ]


def test_goheung_request_code_collects_multiple_date_windows_instead_of_partial_rows():
    from datetime import timedelta
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    p = profile()
    p["regions"] = ["전남 고흥군"]
    p["auction_within_days"] = 27
    provider = CourtAuctionSeleniumProvider({
        "page_size": 20,
        "max_pages": 2,
        "max_calls_per_run": 4,
        "hard_call_cap": 20,
        "sale_window_days": 13,
        "cache_enabled": False,
    })
    requested = []

    def row(case_no, date_text, lot_no="1"):
        return {
            "printCsNo": case_no,
            "maemulSer": lot_no,
            "mokmulSer": lot_no,
            "boCd": "B000513",
            "jiwonNm": "순천지원",
            "hjguSido": "전라남도",
            "hjguSigu": "고흥군",
            "hjguDong": "도화면",
            "daepyoLotno": f"봉룡리 {lot_no}",
            "gamevalAmt": "50,000,000",
            "minmaePrice": "30,000,000",
            "maeGiil": date_text,
            "jimokList": "답",
            "areaList": "500㎡",
            "docid": f"{case_no}-{lot_no}",
        }

    def fake_post(body):
        search = body["dma_srchGdsDtlSrchInfo"]
        requested.append((search["rprsAdongSggCd"], search["bidBgngYmd"], search["bidEndYmd"]))
        assert search["rprsAdongSggCd"] == "770"
        if search["bidBgngYmd"] == date.today().strftime("%Y%m%d"):
            rows = [row("2025 타경 654", date.today().strftime("%Y%m%d"))]
        else:
            later = (date.today() + timedelta(days=20)).strftime("%Y%m%d")
            rows = [row("2026 타경 17", later)]
        return {"data": {"dma_pageInfo": {"totalCnt": len(rows)}, "dlt_srchResult": rows}}

    provider._post_json = fake_post
    items = provider.fetch(p)
    assert {item.case_number for item in items} == {"2025타경654", "2026타경17"}
    assert len(requested) == 2
    assert all(code == "770" for code, _, _ in requested)



def test_exact_municipality_auto_expands_beyond_160_component_rows():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    p = profile()
    p["regions"] = ["전남 고흥군"]
    p["auction_within_days"] = 0
    provider = CourtAuctionSeleniumProvider({
        "page_size": 20,
        "max_pages": 8,
        "municipality_auto_max_pages": 30,
        "max_calls_per_run": 10,
        "hard_call_cap": 30,
        "cache_enabled": False,
    })

    rows = []
    # 대형 일괄매각 155개 목적물: 병합 후 실제 물건 1건
    for object_no in range(1, 156):
        rows.append({
            "printCsNo": "2025 타경 50039",
            "maemulSer": "1",
            "mokmulSer": str(object_no),
            "boCd": "B000513",
            "jiwonNm": "순천지원",
            "hjguSido": "전라남도",
            "hjguSigu": "고흥군",
            "hjguDong": "동강면",
            "daepyoLotno": f"장덕리 {object_no}",
            "gamevalAmt": "18,598,941,000",
            "minmaePrice": "8,326,233,000",
            "maeGiil": date.today().strftime("%Y%m%d"),
            "jimokList": "임야",
            "areaList": "100㎡",
            "docid": f"BULK-{object_no}",
        })
    # 기본 8페이지(160행) 뒤쪽에도 다른 실제 물건이 존재하는 상황
    for index in range(1, 11):
        rows.append({
            "printCsNo": f"2026 타경 {1000 + index}",
            "maemulSer": "1",
            "mokmulSer": "1",
            "boCd": "B000513",
            "jiwonNm": "순천지원",
            "hjguSido": "전라남도",
            "hjguSigu": "고흥군",
            "hjguDong": "도화면",
            "daepyoLotno": f"봉룡리 {index}",
            "gamevalAmt": "50,000,000",
            "minmaePrice": "30,000,000",
            "maeGiil": date.today().strftime("%Y%m%d"),
            "jimokList": "답",
            "areaList": "500㎡",
            "docid": f"NORMAL-{index}",
        })

    requested_pages = []

    def fake_post(body):
        page_no = body["dma_pageInfo"]["pageNo"]
        page_size = body["dma_pageInfo"]["pageSize"]
        search = body["dma_srchGdsDtlSrchInfo"]
        assert search["rprsAdongSggCd"] == "770"
        requested_pages.append(page_no)
        start = (page_no - 1) * page_size
        chunk = rows[start:start + page_size]
        return {"data": {"dma_pageInfo": {"totalCnt": len(rows)}, "dlt_srchResult": chunk}}

    provider._post_json = fake_post
    items = provider.fetch(p)

    assert requested_pages == list(range(1, 10))
    assert len(items) == 11  # 일괄매각 1건 + 일반 물건 10건
    assert any(item.case_number == "2026타경1010" for item in items)
    assert any("8→9페이지 자동확장" in d["비고"] for d in provider.last_fetch_diagnostics)


def test_all_builtin_municipalities_keep_unique_full_codes_and_three_digit_request_codes():
    from landwatch.regions import (
        EXPECTED_MUNICIPALITY_COUNT,
        MUNICIPALITY_CODES,
        PROVINCE_CODES,
        REGION_CODE_AUDIT,
        region_label,
        resolve_region_codes,
    )

    seen_full = set()
    labels = []
    for province, municipalities in MUNICIPALITY_CODES.items():
        sido = PROVINCE_CODES[province]
        for municipality, full_code in municipalities.items():
            label = region_label(province, municipality)
            labels.append(label)
            codes = resolve_region_codes(label)
            assert codes is not None
            assert codes["sido"] == sido
            assert codes["sigungu"] == full_code[2:]
            assert len(codes["sigungu"]) == 3
            assert codes["sigungu"].isdigit()
            assert full_code.startswith(sido)
            assert full_code not in seen_full
            seen_full.add(full_code)

    assert len(labels) == EXPECTED_MUNICIPALITY_COUNT == 229
    assert REGION_CODE_AUDIT == {
        "province_count": 17,
        "municipality_count": 229,
        "unique_sigungu_code_count": 229,
        "request_sigungu_length": 3,
        "errors": [],
        "ok": True,
    }


def test_all_builtin_municipality_codes_are_converted_to_three_digit_court_request():
    from landwatch.court_selenium import build_search_body
    from landwatch.regions import MUNICIPALITY_CODES, PROVINCE_CODES, region_label, resolve_region_codes

    p = profile()
    for province, municipalities in MUNICIPALITY_CODES.items():
        for municipality, full_code in municipalities.items():
            label = region_label(province, municipality)
            codes = resolve_region_codes(label)
            body = build_search_body(
                p, page=1, page_size=20, today=date(2026, 6, 16), region_codes=codes
            )
            search = body["dma_srchGdsDtlSrchInfo"]
            assert search["rprsAdongSdCd"] == PROVINCE_CODES[province]
            assert search["rprsAdongSggCd"] == full_code[2:]
            assert len(search["rprsAdongSggCd"]) == 3
            assert search["cortStDvs"] == "2"

def test_known_region_ignores_wrong_configured_five_digit_override():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    provider = CourtAuctionSeleniumProvider({
        "region_code_map": {
            # 형식은 5자리지만 경기도 가평군 코드를 고흥군에 잘못 저장한 경우
            "전남 고흥군": {"sido": "41", "sigungu": "41820", "dong": ""}
        }
    })
    assert provider._region_query_groups({"regions": ["전남 고흥군"]}) == [
        ("전남 고흥군", [{"sido": "46", "sigungu": "770", "dong": ""}])
    ]




def test_goheung_regression_five_digit_request_would_return_zero_but_three_digit_collects():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    p = profile()
    p["regions"] = ["전남 고흥군"]
    p["auction_within_days"] = 0
    provider = CourtAuctionSeleniumProvider({
        "page_size": 20,
        "max_pages": 1,
        "max_calls_per_run": 2,
        "hard_call_cap": 10,
        "cache_enabled": False,
    })
    requested = []

    def fake_post(body):
        search = body["dma_srchGdsDtlSrchInfo"]
        code = search["rprsAdongSggCd"]
        requested.append(code)
        if code == "46770":
            return {"data": {"dma_pageInfo": {"totalCnt": 0}, "dlt_srchResult": []}}
        assert code == "770"
        return {"data": {"dma_pageInfo": {"totalCnt": 1}, "dlt_srchResult": [{
            "printCsNo": "2025 타경 654", "maemulSer": "1", "mokmulSer": "1",
            "boCd": "B000513", "jiwonNm": "순천지원",
            "hjguSido": "전라남도", "hjguSigu": "고흥군", "hjguDong": "점암면",
            "daepyoLotno": "강산리 1", "gamevalAmt": "50,000,000",
            "minmaePrice": "30,000,000", "maeGiil": date.today().strftime("%Y%m%d"),
            "jimokList": "전", "areaList": "500㎡", "docid": "GOHEUNG-1",
        }]}}

    provider._post_json = fake_post
    items = provider.fetch(p)
    assert requested == ["770"]
    assert len(items) == 1
    assert items[0].city_county == "고흥군"


def test_court_request_accepts_three_or_five_digit_input_but_sends_three_digits():
    import pytest
    from landwatch.court_selenium import CourtAuctionSeleniumError, build_search_body

    p = profile()
    for supplied in ("770", "46770"):
        body = build_search_body(
            p,
            page=1,
            page_size=20,
            today=date(2026, 6, 16),
            region_codes={"sido": "46", "sigungu": supplied, "dong": ""},
        )
        search = body["dma_srchGdsDtlSrchInfo"]
        assert search["rprsAdongSggCd"] == "770"

    with pytest.raises(CourtAuctionSeleniumError, match="앞 2자리가 일치하지 않습니다"):
        build_search_body(
            p,
            page=1,
            page_size=20,
            today=date(2026, 6, 16),
            region_codes={"sido": "46", "sigungu": "41820", "dong": ""},
        )


def test_jeju_province_only_uses_jeju_court_once_instead_of_region_fanout():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    p = profile()
    p["regions"] = ["제주특별자치도"]
    p["auction_within_days"] = 0
    provider = CourtAuctionSeleniumProvider({
        "page_size": 20,
        "max_pages": 1,
        "max_calls_per_run": 20,
        "hard_call_cap": 60,
        "province_fanout_max_municipalities": 8,
    })
    captured = []

    def row(case_no: str, docid: str, city: str):
        return {
            "printCsNo": case_no,
            "maemulSer": "1",
            "mokmulSer": "1",
            "docid": docid,
            "hjguSido": "제주특별자치도",
            "hjguSigu": city,
            "printSt": f"제주특별자치도 {city} 테스트리 1",
            "gamevalAmt": "30,000,000원",
            "minmaePrice": "20,000,000원",
            "yuchalCnt": "1",
            "maeGiil": date.today().strftime("%Y%m%d"),
            "jimokList": "전",
            "areaList": "토지 500㎡",
        }

    def fake_post(body):
        search = body["dma_srchGdsDtlSrchInfo"]
        captured.append((
            search["cortOfcCd"], search["rprsAdongSdCd"],
            search["rprsAdongSggCd"], search["cortStDvs"],
        ))
        rows = [
            row("2026 타경 110", "JEJU-110", "제주시"),
            row("2026 타경 130", "JEJU-130", "서귀포시"),
        ]
        return {
            "data": {
                "dma_pageInfo": {"pageNo": 1, "pageSize": 20, "totalCnt": len(rows)},
                "dlt_srchResult": rows,
            }
        }

    provider._post_json = fake_post
    items = provider.fetch(p)

    assert captured == [("B000530", "", "", "1")]
    assert {item.city_county for item in items} == {"제주시", "서귀포시"}
    assert all(item.province == "제주특별자치도" for item in items)
    assert provider.last_fetch_diagnostics[0]["검색방식"] == "제주지방법원 전체검색 후 주소검증"


def test_jeju_status_zero_recovers_session_and_retries_once():
    from landwatch.court_selenium import (
        CourtAuctionHttpError, CourtAuctionSeleniumProvider,
    )

    p = profile()
    p["regions"] = ["제주특별자치도"]
    p["auction_within_days"] = 0
    provider = CourtAuctionSeleniumProvider({
        "page_size": 20, "max_pages": 1,
        "max_calls_per_run": 20, "hard_call_cap": 60,
    })
    attempts = []
    recoveries = []

    def fake_post(body):
        attempts.append(body)
        if len(attempts) == 1:
            raise CourtAuctionHttpError(0, "법원경매 요청 HTTP 오류: 0 TypeError: Failed to fetch")
        return {
            "data": {
                "dma_pageInfo": {"pageNo": 1, "pageSize": 20, "totalCnt": 0},
                "dlt_srchResult": [],
            }
        }

    provider._post_json = fake_post
    provider._recover_search_session = lambda: recoveries.append(True)
    assert provider.fetch(p) == []
    assert len(attempts) == 2
    assert recoveries == [True]
    search = attempts[0]["dma_srchGdsDtlSrchInfo"]
    assert search["cortOfcCd"] == "B000530"
    assert search["rprsAdongSdCd"] == ""
    assert search["rprsAdongSggCd"] == ""


def test_large_province_still_uses_nationwide_fallback_plan():
    from landwatch.court_selenium import CourtAuctionSeleniumProvider

    p = profile()
    p["regions"] = ["경기도"]
    provider = CourtAuctionSeleniumProvider({"province_fanout_max_municipalities": 8})
    plan = provider._query_plan(p)
    assert plan[0][0] == "전국 대체검색 후 시·도 주소검증"
    assert plan[0][1][0][1] == [None]


def test_court_search_index_roundtrip(tmp_path):
    from landwatch.db import Database

    db = Database(str(tmp_path / "landwatch.db"))
    p = profile()
    item = AuctionItem(
        auction_id="IDX-1",
        sale_type="경매",
        source_name="대한민국 법원경매정보",
        case_number="2026타경100",
        item_number="1",
        court="청주지방법원",
        status="유찰",
        usage="전",
        address="충청북도 충주시 소태면",
        province="충청북도",
        city_county="충주시",
        min_price=20_000_000,
        appraisal_price=30_000_000,
        failed_count=1,
        land_area_m2=500,
        auction_date=date(2026, 7, 3),
        raw={"docid": "IDX-1"},
    )

    db.save_court_search_index(p, [item])
    cached = db.get_court_search_index(p, max_age_minutes=30)

    assert cached is not None
    assert len(cached) == 1
    assert cached[0].auction_id == "IDX-1"
    assert cached[0].province == "충청북도"
    assert cached[0].auction_date == date(2026, 7, 3)


def test_court_search_index_expired_returns_none(tmp_path):
    from datetime import timedelta

    from landwatch.db import Database

    db = Database(str(tmp_path / "landwatch.db"))
    p = profile()
    item = AuctionItem(
        auction_id="IDX-2",
        province="충청북도",
        city_county="충주시",
    )
    db.save_court_search_index(p, [item])

    old = (datetime.now() - timedelta(minutes=120)).isoformat(timespec="seconds")
    key = db._profile_index_key(p)
    db.conn.execute("UPDATE court_search_index SET fetched_at=? WHERE profile_key=?", (old, key))
    db.conn.commit()

    assert db.get_court_search_index(p, max_age_minutes=30) is None
