from __future__ import annotations

import base64
import html
import json
import mimetypes
import re
from dataclasses import asdict, is_dataclass
from functools import lru_cache
from pathlib import Path
from datetime import date, datetime
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlsplit


_IMAGE_KEY_WORDS = (
    "image", "img", "photo", "picture", "pic", "thumbnail", "thumb", "thnl", "poto",
)
_EXPLICIT_IMAGE_KEYS = (
    # 차세대 온비드 목록/상세
    "thnlImgUrlAdr", "THNL_IMG_URL_ADR", "thumbnailUrl", "thumbnail_url",
    "mainImgUrl", "mainImageUrl", "imageUrl", "image_url", "imgUrl", "img_url",
    "photoUrl", "photo_url", "picUrl", "pictureUrl", "potoUrlAdr",
    # 법원 목록/상세에서 사용될 수 있는 별칭
    "imgPath", "imagePath", "photoPath", "picPath", "thumImgUrl", "realEstImgUrl",
    "dspslGdsImgUrl", "gdsImgUrl", "mainPhotoUrl", "court_image_url",
)
_VIEW_KEYS = (
    "iqryCnt", "IQRY_CNT", "inqryCnt", "inqCnt", "viewCnt", "views", "hitCnt",
    "hits", "readCnt", "rdCnt", "inqrCnt", "searchCnt", "clickCnt",
)
_NOTE_KEYS = (
    "mulBigo", "utlzPscdCont", "UTLZ_PSCD", "locVntyPscdCont", "POSI_ENV_PSCD",
    "pytnMtrsCont", "icdlCdtnCont", "cltrEtcCont", "lowstBidPrcIndctCont",
    "pjbBuldList", "buldList", "GOODS_NM",
)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "to_dict"):
        mapped = value.to_dict()
        if isinstance(mapped, dict):
            return mapped
    return {}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        return ""
    text = str(value).strip()
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _first(mapping: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


@lru_cache(maxsize=256)
def _local_image_data_uri(path_text: str, mtime_ns: int) -> str:
    path = Path(path_text)
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return ""
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}"
    except OSError:
        return ""


def _safe_image_url(value: Any, *, sale_type: str = "") -> str:
    text = _text(value).strip("'\"")
    if not text:
        return ""
    if text.lower().startswith("data:image/"):
        return text
    # 법원 상세화면에서 캡처한 로컬 대표사진은 data URI로 변환해 카드에 삽입한다.
    if text.startswith(("/", "~", ".")) or re.match(r"^[A-Za-z]:[\\/]", text):
        path = Path(text).expanduser()
        try:
            if path.is_file():
                return _local_image_data_uri(str(path.resolve()), path.stat().st_mtime_ns)
        except OSError:
            pass
    # XML/JSON 문자열 속에 URL이 들어 있는 경우 첫 이미지 URL을 뽑는다.
    match = re.search(r"https?://[^\s<>'\"]+", text, flags=re.I)
    if match and not text.lower().startswith(("http://", "https://")):
        text = match.group(0)
    text = text.replace("&amp;", "&")
    if text.startswith("//"):
        text = "https:" + text
    elif text.startswith("/"):
        base = "https://www.onbid.co.kr" if sale_type == "공매" else "https://www.courtauction.go.kr"
        text = urljoin(base, text)
    elif not text.lower().startswith(("http://", "https://")):
        return ""
    try:
        parts = urlsplit(text)
    except Exception:
        return ""
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return ""
    return text


def _iter_image_candidates(value: Any, parent_key: str = "", depth: int = 0):
    if depth > 5:
        return
    if isinstance(value, dict):
        # 명시된 필드를 먼저 반환해 품질 좋은 대표 이미지를 우선한다.
        for key in _EXPLICIT_IMAGE_KEYS:
            if key in value:
                yield key, value.get(key)
        if any(word in parent_key.lower() for word in _IMAGE_KEY_WORDS):
            for nested_url_key in ("urlAdr", "URL_ADR", "fileUrl", "downloadUrl"):
                if nested_url_key in value:
                    yield nested_url_key, value.get(nested_url_key)
        for key, child in value.items():
            key_lower = str(key).lower()
            if any(word in key_lower for word in _IMAGE_KEY_WORDS):
                if isinstance(child, (str, int, float)):
                    yield str(key), child
                elif isinstance(child, (list, tuple, dict)):
                    yield from _iter_image_candidates(child, str(key), depth + 1)
            elif isinstance(child, (list, tuple, dict)):
                yield from _iter_image_candidates(child, str(key), depth + 1)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _iter_image_candidates(child, parent_key, depth + 1)
    elif isinstance(value, str) and any(word in parent_key.lower() for word in _IMAGE_KEY_WORDS):
        # XML 또는 구분자 문자열에서도 URL을 찾는다.
        for url in re.findall(r"https?://[^\s<>'\"]+", value, flags=re.I):
            yield parent_key, url


def extract_image_url(item: Any) -> str:
    """경매·공매 원시자료에서 대표사진 URL을 안전하게 찾는다."""
    data = _mapping(item)
    raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
    sale_type = _text(data.get("sale_type") or raw.get("source_type"))
    court_visual_source = _text(raw.get("court_photo_source"))
    allowed_court_cache = court_visual_source.startswith((
        "court-photo-", "court-현황사진", "court-물건사진", "court-사진보기",
        "court-사진정보", "court-물건이미지", "court-사진",
    ))
    has_court_cache = bool(raw.get("court_image_cache_path") or raw.get("court_image_url"))
    rejected_court_cache = sale_type == "경매" and has_court_cache and not allowed_court_cache

    # 정규화 필드가 추가되는 경우 가장 우선한다.
    for value in (
        data.get("image_url"), data.get("thumbnail_url"),
        None if rejected_court_cache else raw.get("court_image_cache_path"),
        None if rejected_court_cache else raw.get("court_image_url"),
        raw.get("thnlImgUrlAdr"), raw.get("THNL_IMG_URL_ADR"),
        raw.get("mainImgUrl"), raw.get("imageUrl"),
    ):
        safe = _safe_image_url(value, sale_type=sale_type)
        if safe:
            return safe

    searchable_raw = raw
    if rejected_court_cache:
        searchable_raw = dict(raw)
        searchable_raw.pop("court_image_cache_path", None)
        searchable_raw.pop("court_image_url", None)
    for _, candidate in _iter_image_candidates(searchable_raw):
        if isinstance(candidate, dict):
            candidate = _first(candidate, _EXPLICIT_IMAGE_KEYS)
        if isinstance(candidate, (list, tuple)):
            for each in candidate:
                safe = _safe_image_url(each, sale_type=sale_type)
                if safe:
                    return safe
        else:
            safe = _safe_image_url(candidate, sale_type=sale_type)
            if safe:
                return safe
    return ""


def extract_view_count(item: Any) -> int | None:
    data = _mapping(item)
    raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
    value = _first(data, ("view_count", "views"))
    if value in (None, ""):
        value = _first(raw, _VIEW_KEYS)
    if value in (None, ""):
        return None
    digits = re.sub(r"[^0-9-]", "", str(value))
    try:
        return max(0, int(digits))
    except (TypeError, ValueError):
        return None


def _amount(value: Any) -> str:
    try:
        number = int(round(float(value or 0)))
    except (TypeError, ValueError):
        return "-"
    return f"{number:,}원" if number else "-"


def _area(value: Any) -> tuple[str, str]:
    try:
        area = float(value or 0)
    except (TypeError, ValueError):
        area = 0.0
    if area <= 0:
        return "-", "-"
    sqm = f"{area:,.2f}".rstrip("0").rstrip(".") + "㎡"
    pyeong = f"{area / 3.305785:,.1f}평"
    return sqm, pyeong


def _date(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    text = _text(value)
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    return text or "미정"


def _notes(item: Any, limit: int = 3) -> list[str]:
    data = _mapping(item)
    raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
    values: list[Any] = []
    for key in ("special_conditions", "risk_reasons"):
        current = data.get(key) or []
        if isinstance(current, str):
            current = [current]
        values.extend(current)
    values.extend(raw.get(key) for key in _NOTE_KEYS)

    notes: list[str] = []
    for value in values:
        text = _text(value)
        if not text or text in notes:
            continue
        # 카드가 지나치게 길어지지 않도록 문장 단위로 요약한다.
        if len(text) > 120:
            text = text[:117].rstrip() + "…"
        notes.append(text)
        if len(notes) >= limit:
            break
    return notes


def _discount(data: dict[str, Any]) -> float:
    try:
        explicit = float(data.get("discount_percent") or 0)
    except (TypeError, ValueError):
        explicit = 0.0
    if explicit:
        return max(0.0, explicit)
    try:
        appraisal = float(data.get("appraisal_price") or 0)
        minimum = float(data.get("min_price") or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, (1 - minimum / appraisal) * 100) if appraisal > 0 else 0.0


def _esc(value: Any) -> str:
    return html.escape(_text(value) or "-", quote=True)


def build_result_cards_html(
    items: list[Any],
    detail_links: list[str],
    *,
    title: str = "검색 결과",
) -> str:
    """경매사이트형 사진 카드 목록 HTML을 생성한다."""
    cards: list[str] = []
    for index, item in enumerate(items):
        data = _mapping(item)
        raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
        sale_type = _text(data.get("sale_type") or raw.get("source_type")) or "경매"
        number = _text(data.get("case_number") or raw.get("onbidPbancNo") or raw.get("PLNM_NO")) or "번호 없음"
        item_number = _text(data.get("item_number") or raw.get("cltrMngNo") or raw.get("CLTR_MNMT_NO")) or "-"
        court = _text(data.get("court") or raw.get("orgNm") or raw.get("rqstOrgNm")) or "-"
        status = _text(data.get("status")) or "-"
        usage = _text(data.get("usage")) or "-"
        address = _text(data.get("address")) or "-"
        auction_date = _date(data.get("auction_date") or raw.get("cltrBidEndDt") or raw.get("maeGiil"))
        sqm, pyeong = _area(data.get("land_area_m2"))
        appraisal = _amount(data.get("appraisal_price"))
        minimum = _amount(data.get("min_price"))
        discount = _discount(data)
        failed = int(float(data.get("failed_count") or 0)) if str(data.get("failed_count") or "0").replace(".", "", 1).isdigit() else 0
        view_count = extract_view_count(item)
        notes = _notes(item)
        image_url = extract_image_url(item)
        visual_source = _text(raw.get("court_photo_source"))
        if visual_source.startswith("court-photo-") or visual_source.startswith((
            "court-현황사진", "court-물건사진", "court-사진보기", "court-사진정보",
            "court-물건이미지", "court-사진",
        )):
            visual_badge = "현황사진"
        else:
            visual_badge = ""
        detail_link = detail_links[index] if index < len(detail_links) else "#"
        detail_link = html.escape(detail_link, quote=True)

        source_badge = "공매" if sale_type == "공매" else "경매"
        date_label = "입찰마감일" if sale_type == "공매" else "매각기일"
        price_label = "최저입찰가" if sale_type == "공매" else "최저매각가격"
        notes_html = "".join(f"<li>{_esc(note)}</li>" for note in notes)
        if not notes_html:
            notes_html = "<li>별도 특이사항 없음</li>"
        image_html = ""
        if image_url:
            image_html = (
                f"<img src='{html.escape(image_url, quote=True)}' loading='lazy' "
                f"alt='{_esc(address)} 물건사진' referrerpolicy='no-referrer' />"
            )
        views = f"{view_count:,}회" if view_count is not None else "-"
        discount_text = f"감정가 대비 {discount:.1f}%↓" if discount > 0 else "감정가 대비 -"
        failed_text = f"유찰 {failed}회" if failed > 0 else "유찰 없음"

        cards.append(f"""
<a class="lw-result-card-link" href="{detail_link}" target="_blank" rel="noopener noreferrer" aria-label="{_esc(number)} 상세보기">
  <article class="lw-result-card">
    <div class="lw-media">
      <div class="lw-photo-placeholder"><span>🏞️</span><b>물건사진 없음</b></div>
      {image_html}
      <span class="lw-source-badge {source_badge}">{source_badge}</span>
      {f'<span class="lw-visual-badge">{_esc(visual_badge)}</span>' if visual_badge else ''}
    </div>
    <section class="lw-date-usage">
      <div class="lw-label">{date_label}</div>
      <div class="lw-date">{_esc(auction_date)}</div>
      <div class="lw-label usage-label">용도</div>
      <div class="lw-usage">{_esc(usage)}</div>
      <div class="lw-agency">{_esc(court)}</div>
    </section>
    <section class="lw-basic">
      <div class="lw-number-row">
        <strong>{_esc(number)}</strong>
        <span>물건 { _esc(item_number) }</span>
      </div>
      <div class="lw-address">📍 {_esc(address)}</div>
      <div class="lw-area">면적 <b>{_esc(sqm)}</b> <span>({ _esc(pyeong) })</span></div>
      <div class="lw-note-title">특이사항</div>
      <ul class="lw-notes">{notes_html}</ul>
    </section>
    <section class="lw-price">
      <div class="lw-label">감정평가액</div>
      <div class="lw-appraisal">{_esc(appraisal)}</div>
      <div class="lw-label price-gap">{price_label}</div>
      <div class="lw-minimum">{_esc(minimum)}</div>
      <div class="lw-discount">{_esc(discount_text)}</div>
    </section>
    <section class="lw-state">
      <span class="lw-status">{_esc(status)}</span>
      <span class="lw-failed">{_esc(failed_text)}</span>
      <span class="lw-views">조회 {views}</span>
      <span class="lw-detail-button">상세조회 ↗</span>
    </section>
  </article>
</a>
""")

    return f"""
<style>
.lw-card-list{{display:flex;flex-direction:column;gap:12px;margin:6px 0 16px;}}
.lw-result-card-link{{display:block;color:inherit;text-decoration:none!important;border-radius:14px;}}
.lw-result-card{{display:grid;grid-template-columns:220px 132px minmax(350px,1fr) 205px 128px;min-height:185px;background:#fff;border:1px solid #dfe3e8;border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(16,24,40,.06);transition:box-shadow .16s ease,border-color .16s ease,transform .16s ease;}}
.lw-result-card-link:hover .lw-result-card{{border-color:#7aa7e8;box-shadow:0 7px 22px rgba(16,24,40,.12);transform:translateY(-1px);}}
.lw-media{{position:relative;min-height:185px;background:#eef2f6;overflow:hidden;}}
.lw-media img{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;background:#eef2f6;}}
.lw-photo-placeholder{{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:7px;color:#667085;font-size:13px;}}
.lw-photo-placeholder span{{font-size:34px;filter:grayscale(.25);}}
.lw-source-badge{{position:absolute;left:10px;top:10px;z-index:2;padding:4px 9px;border-radius:999px;color:#fff;font-size:12px;font-weight:750;box-shadow:0 1px 4px rgba(0,0,0,.18);}}
.lw-source-badge.경매{{background:#3448a4;}} .lw-source-badge.공매{{background:#087f5b;}}
.lw-visual-badge{{position:absolute;right:10px;top:10px;z-index:2;padding:4px 8px;border-radius:999px;background:rgba(17,24,39,.78);color:#fff;font-size:11px;font-weight:700;}}
.lw-date-usage,.lw-basic,.lw-price,.lw-state{{padding:17px 16px;border-left:1px solid #edf0f2;}}
.lw-label{{font-size:12px;color:#667085;margin-bottom:4px;}}
.lw-date{{font-size:17px;font-weight:750;color:#1d2939;line-height:1.3;}}
.usage-label{{margin-top:17px;}}
.lw-usage{{display:inline-block;padding:5px 11px;border-radius:7px;background:#edf4ff;color:#1849a9;font-weight:750;font-size:14px;}}
.lw-agency{{margin-top:14px;color:#667085;font-size:12px;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}}
.lw-number-row{{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:9px;}}
.lw-number-row strong{{color:#175cd3;font-size:16px;text-decoration:underline;text-underline-offset:3px;}}
.lw-number-row span{{padding:3px 7px;border:1px solid #d0d5dd;border-radius:5px;color:#475467;font-size:12px;}}
.lw-address{{font-size:15px;font-weight:650;color:#101828;line-height:1.45;margin-bottom:7px;}}
.lw-area{{color:#344054;font-size:13px;}} .lw-area span{{color:#667085;}}
.lw-note-title{{font-size:12px;color:#667085;margin-top:13px;margin-bottom:3px;}}
.lw-notes{{margin:0;padding-left:17px;color:#475467;font-size:12px;line-height:1.45;}}
.lw-notes li{{margin:1px 0;}}
.lw-appraisal{{font-size:15px;color:#475467;text-decoration:none;}}
.price-gap{{margin-top:21px;}}
.lw-minimum{{font-size:20px;line-height:1.25;font-weight:800;color:#c11574;word-break:keep-all;}}
.lw-discount{{display:inline-block;margin-top:8px;padding:4px 7px;border-radius:6px;background:#fff1f3;color:#c01048;font-size:12px;font-weight:650;}}
.lw-state{{display:flex;flex-direction:column;align-items:stretch;justify-content:center;gap:9px;text-align:center;background:#fbfcfd;}}
.lw-status{{padding:7px 8px;border-radius:7px;background:#fef0c7;color:#93370d;font-size:13px;font-weight:750;}}
.lw-failed,.lw-views{{font-size:12px;color:#667085;}}
.lw-detail-button{{margin-top:5px;padding:9px 7px;border-radius:8px;background:#175cd3;color:#fff;font-size:13px;font-weight:750;}}
@media(max-width:1150px){{
 .lw-result-card{{grid-template-columns:190px 125px minmax(300px,1fr) 180px;}}
 .lw-state{{grid-column:2/5;flex-direction:row;justify-content:flex-end;align-items:center;padding:9px 14px;border-top:1px solid #edf0f2;}}
 .lw-status,.lw-detail-button{{padding:6px 10px;margin:0;}}
}}
@media(max-width:760px){{
 .lw-result-card{{grid-template-columns:1fr;}}
 .lw-media{{min-height:220px;}}
 .lw-date-usage,.lw-basic,.lw-price,.lw-state{{border-left:0;border-top:1px solid #edf0f2;}}
 .lw-state{{grid-column:auto;justify-content:flex-start;flex-wrap:wrap;}}
}}
</style>
<div class="lw-card-list" aria-label="{html.escape(title, quote=True)}">{''.join(cards)}</div>
"""


def card_image_diagnostics(items: list[Any]) -> dict[str, int]:
    """화면에 대표사진이 얼마나 표시되는지 요약한다."""
    total = len(items)
    available = sum(bool(extract_image_url(item)) for item in items)
    return {"total": total, "available": available, "missing": max(0, total - available)}
