from datetime import date
from pathlib import Path

from landwatch.card_view import (
    build_result_cards_html,
    card_image_diagnostics,
    extract_image_url,
    extract_view_count,
)
from landwatch.models import AuctionItem


def sample_item(**overrides):
    values = dict(
        auction_id="onbid:2025-12641-001",
        sale_type="공매",
        source_name="한국자산관리공사 차세대 온비드",
        case_number="123456",
        item_number="2025-12641-001",
        court="한국자산관리공사 전북지역본부",
        status="진행",
        usage="전",
        address="전북특별자치도 부안군 변산면 도청리 100-2",
        min_price=10_511_000,
        appraisal_price=17_518_000,
        failed_count=2,
        land_area_m2=820.5,
        auction_date=date(2026, 6, 17),
        special_conditions=["농지취득자격증명 제출 필요"],
        risk_reasons=["도로 접면 확인 필요"],
        raw={
            "thnlImgUrlAdr": "https://example.com/photo/land.jpg",
            "iqryCnt": 153,
        },
    )
    values.update(overrides)
    return AuctionItem(**values)


def test_card_result_contains_requested_information_and_new_tab_link():
    item = sample_item()
    html = build_result_cards_html([item], ["?lw_detail_page=1&lw_detail_case=123456"])
    assert "물건사진" in html
    assert "입찰마감일" in html
    assert "용도" in html
    assert "물건 2025-12641-001" in html
    assert "변산면 도청리 100-2" in html
    assert "820.5㎡" in html
    assert "농지취득자격증명" in html
    assert "감정평가액" in html
    assert "최저입찰가" in html
    assert "진행" in html
    assert "조회 153회" in html
    assert "상세조회 ↗" in html
    assert "target=\"_blank\"" in html
    assert "lw_detail_page=1" in html


def test_onbid_thumbnail_is_used_as_representative_photo():
    item = sample_item()
    assert extract_image_url(item) == "https://example.com/photo/land.jpg"
    assert card_image_diagnostics([item]) == {"total": 1, "available": 1, "missing": 0}


def test_nested_photo_url_and_relative_court_photo_are_supported():
    item = sample_item(
        sale_type="경매",
        raw={"photoList": [{"imgPath": "/file/auction/house01.jpg"}]},
    )
    assert extract_image_url(item) == "https://www.courtauction.go.kr/file/auction/house01.jpg"


def test_missing_photo_shows_placeholder_and_view_count_dash():
    item = sample_item(raw={})
    html = build_result_cards_html([item], ["?lw_detail_page=1"])
    assert "물건사진 없음" in html
    assert "조회 -" in html
    assert extract_view_count(item) is None


def test_app_defaults_current_search_results_to_photo_card_mode():
    app = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    assert '["사진 카드형", "상세 표형"]' in app
    assert "build_result_cards_html" in app
    assert "card_image_diagnostics" in app


def test_card_does_not_display_old_map_fallback_cache():
    from datetime import date
    from landwatch.card_view import build_result_cards_html, extract_image_url
    from landwatch.models import AuctionItem

    item = AuctionItem(
        auction_id="x", case_number="2025타경1", item_number="1", sale_type="경매",
        address="전북특별자치도 부안군 부안읍 봉덕리 1", auction_date=date(2026, 7, 1),
        raw={"court_image_cache_path": "/tmp/not-exist.png", "court_photo_source": "naver-map-fallback"},
    )
    html = build_result_cards_html([item], ["http://localhost/detail"])
    assert extract_image_url(item) == ""
    assert "위치지도" not in html
    assert "물건사진 없음" in html


def test_card_marks_official_court_photo_as_current_photo(tmp_path: Path):
    image = tmp_path / "court.png"
    image.write_bytes(b"x" * 600)
    item = sample_item(
        sale_type="경매",
        raw={
            "court_image_cache_path": str(image),
            "court_photo_source": "court-photo-현황사진",
        },
    )
    html = build_result_cards_html([item], ["http://localhost/detail"])
    assert "현황사진" in html
    assert "법원문서" not in html
    assert extract_image_url(item).startswith("data:image/png;base64,")


def test_old_court_document_cache_is_never_displayed(tmp_path: Path):
    image = tmp_path / "court-document.png"
    image.write_bytes(b"x" * 600)
    item = sample_item(
        sale_type="경매",
        raw={
            "court_image_cache_path": str(image),
            "court_photo_source": "court-document-감정평가서",
        },
    )
    html = build_result_cards_html([item], ["http://localhost/detail"])
    assert extract_image_url(item) == ""
    assert "법원문서" not in html
    assert "물건사진 없음" in html
