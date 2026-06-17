from __future__ import annotations

import base64
from datetime import date
from pathlib import Path

from landwatch.card_view import extract_image_url
from landwatch.court_photo import (
    _case_parts,
    _write_photo_source,
    is_cache_fresh,
    is_usable_photo_cache,
    photo_cache_path,
)
from landwatch.court_selenium import CourtAuctionSeleniumProvider
from landwatch.models import AuctionItem


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _item() -> AuctionItem:
    return AuctionItem(
        auction_id="court|B000520|2025타경385|1",
        sale_type="경매",
        case_number="2025타경385",
        item_number="1",
        court="전주지방법원 정읍지원",
        address="전북특별자치도 부안군 행안면 역리 1117",
        auction_date=date(2026, 7, 1),
        raw={"boCd": "B000520"},
    )


def test_case_number_parts():
    assert _case_parts("2025 타경 385") == ("2025", "385")
    assert _case_parts("2025타경385") == ("2025", "385")


def test_local_court_photo_is_rendered_as_data_uri(tmp_path: Path):
    image = tmp_path / "court.png"
    image.write_bytes(PNG_1X1)
    item = _item()
    item.raw["court_image_cache_path"] = str(image)
    item.raw["court_photo_source"] = "court-photo-현황사진"
    url = extract_image_url(item)
    assert url.startswith("data:image/png;base64,")


def test_photo_cache_key_is_stable(tmp_path: Path):
    first = photo_cache_path(_item(), tmp_path)
    second = photo_cache_path(_item(), tmp_path)
    assert first == second
    assert first.suffix == ".png"


def test_provider_uses_cached_photo_without_starting_driver(tmp_path: Path):
    provider = CourtAuctionSeleniumProvider({
        "photo_enabled": True,
        "photo_cache_dir": str(tmp_path),
        "photo_cache_days": 30,
        "photo_max_per_run": 20,
    })
    item = _item()
    cache = photo_cache_path(item, tmp_path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(PNG_1X1 * 20)  # freshness check minimum size > 500 bytes
    _write_photo_source(cache, "court-photo-현황사진")
    assert is_cache_fresh(cache, 30)

    result = provider.fetch_detail(item)
    assert result.raw["court_image_cache_path"] == str(cache.resolve())
    assert result.raw["court_photo_source"] == "court-photo-현황사진"
    assert provider.driver is None
    assert provider.photo_cache_hits == 1


def test_photo_run_limit_does_not_break_item(tmp_path: Path):
    provider = CourtAuctionSeleniumProvider({
        "photo_enabled": True,
        "photo_cache_dir": str(tmp_path),
        "photo_max_per_run": 0,
    })
    item = provider.fetch_detail(_item())
    assert item.raw["court_photo_source"] == "run-limit"
    assert provider.driver is None


def test_photo_source_sidecar_preserves_saved_source(tmp_path):
    from landwatch.court_photo import _write_photo_source, _read_photo_source

    image = tmp_path / "sample.png"
    image.write_bytes(b"x" * 600)
    _write_photo_source(image, "court-photo-현황사진")
    assert _read_photo_source(image) == "court-photo-현황사진"


def test_only_official_photo_cache_is_reused(tmp_path: Path):
    image = tmp_path / "sample.png"
    image.write_bytes(b"x" * 600)

    _write_photo_source(image, "court-photo-현황사진")
    assert is_usable_photo_cache(image, 30)

    for invalid_source in (
        "court-document-감정평가서",
        "court-case-page",
        "court-item-row",
        "court-item-detail",
        "address-fallback",
        "naver-map-fallback",
        "",
    ):
        _write_photo_source(image, invalid_source)
        assert not is_usable_photo_cache(image, 30)


def test_court_photo_flow_does_not_open_court_documents():
    source = (Path(__file__).resolve().parents[1] / "landwatch" / "court_photo.py").read_text(
        encoding="utf-8"
    )
    assert "_DOCUMENT_LABELS" not in source
    assert "for label in _DOCUMENT_LABELS" not in source
    assert 'source = f"court-document-' not in source


def test_photo_candidate_uses_page_photo_tab_but_not_page_document_tabs():
    from landwatch.court_photo import _candidate_score

    class Driver:
        @staticmethod
        def execute_script(script, element):
            return {
                "w": 640,
                "h": 480,
                "text": "img goods-viewer",
                "pageText": "현황사진 감정평가서 현황조사서",
                "tag": "IMG",
                "src": "https://example.com/goods/1.jpg",
            }

    score, _ = _candidate_score(Driver(), object(), require_photo_context=True)
    assert score > 0


def test_photo_candidate_rejects_document_and_map_elements():
    from landwatch.court_photo import _candidate_score

    class Driver:
        meta = {}

        @classmethod
        def execute_script(cls, script, element):
            return cls.meta

    base = {
        "w": 640,
        "h": 480,
        "pageText": "현황사진",
        "tag": "CANVAS",
        "src": "",
    }
    for local_text in ("pdfviewer document", "map_area roadview"):
        Driver.meta = {**base, "text": local_text}
        score, _ = _candidate_score(Driver(), object(), require_photo_context=True)
        assert score < 0


def test_invalid_case_number_leaves_photo_blank_and_marks_missing_cache(tmp_path, monkeypatch):
    import landwatch.court_photo as module

    item = _item()
    item.case_number = "사건번호 미확인"

    def fake_map(driver, item_arg, destination, **kwargs):
        raise AssertionError("위치지도 fallback should not be called")

    monkeypatch.setattr(module, "_capture_naver_map_fallback", fake_map)
    path, source = module.capture_court_photo(
        object(), item, cache_dir=tmp_path, map_fallback=True
    )
    assert path == ""
    assert source == "invalid-case-number"
    assert module.is_recent_missing_photo_cache(module.photo_cache_path(item, tmp_path), 7)


def test_provider_discards_old_map_cache_before_retry(tmp_path: Path):
    from landwatch.court_photo import _write_photo_source

    provider = CourtAuctionSeleniumProvider({
        "photo_enabled": True,
        "photo_cache_dir": str(tmp_path),
        "photo_cache_days": 30,
        "photo_max_per_run": 0,
    })
    item = _item()
    cache = photo_cache_path(item, tmp_path)
    cache.write_bytes(b"x" * 600)
    _write_photo_source(cache, "naver-map-fallback")

    result = provider.fetch_detail(item)
    assert result.raw["court_photo_source"] == "run-limit"
    assert "court_image_cache_path" not in result.raw
    assert not cache.exists()


def test_fast_mode_skips_price_detail_when_list_has_next_price_and_date():
    provider = CourtAuctionSeleniumProvider({
        "fast_mode": True,
        "price_detail_policy": "smart",
        "price_detail_max_per_run": 6,
        "photo_enabled": False,
    })
    item = _item()
    item.min_price = 12_000_000
    item.failed_count = 2
    item.raw["court_list_price_source"] = "notifyMinmaePrice1"

    assert provider._needs_price_detail(item) is False
    result = provider.fetch_detail(item)
    assert result.raw["court_price_source"] == "목록검색 응답(고속검증 통과)"
    assert provider.price_detail_skipped_count == 1
    assert provider.driver is None


def test_fast_mode_requests_price_detail_only_for_incomplete_list_data():
    provider = CourtAuctionSeleniumProvider({
        "fast_mode": True,
        "price_detail_policy": "smart",
        "price_detail_max_per_run": 6,
        "photo_enabled": False,
    })
    item = _item()
    item.min_price = 0
    item.raw["court_list_price_source"] = "minmaePrice"
    assert provider._needs_price_detail(item) is True


def test_zero_price_detail_limit_uses_list_value_without_driver():
    provider = CourtAuctionSeleniumProvider({
        "fast_mode": True,
        "price_detail_policy": "smart",
        "price_detail_max_per_run": 0,
        "photo_enabled": False,
    })
    item = _item()
    item.min_price = 0
    result = provider.fetch_detail(item)
    assert result.raw["court_price_source"] == "목록검색 응답(상세조회 상한)"
    assert provider.driver is None


def test_missing_photo_negative_cache_prevents_repeat_attempt(tmp_path: Path):
    from landwatch.court_photo import _write_missing_photo_cache, is_recent_missing_photo_cache

    item = _item()
    cache = photo_cache_path(item, tmp_path)
    _write_missing_photo_cache(cache, "photo-not-found")
    assert is_recent_missing_photo_cache(cache, 7)

    provider = CourtAuctionSeleniumProvider({
        "photo_enabled": True,
        "photo_cache_dir": str(tmp_path),
        "photo_missing_cache_days": 7,
        "price_detail_max_per_run": 0,
    })
    result = provider.fetch_detail(item)
    assert result.raw["court_photo_source"] == "photo-not-found-cached"
    assert provider.photo_network_attempts == 0
    assert provider.driver is None
