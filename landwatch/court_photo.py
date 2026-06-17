from __future__ import annotations

import hashlib
import logging
import re
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

from .models import AuctionItem

logger = logging.getLogger(__name__)

CASE_SEARCH_URL = (
    "https://www.courtauction.go.kr/pgj/index.on?"
    "w2xPath=/pgj/ui/pgj100/PGJ159M00.xml&pgjId=159M00"
)

_CASE_COURT_ID = "mf_wfm_mainFrame_sbx_auctnCsSrchCortOfc"
_CASE_YEAR_ID = "mf_wfm_mainFrame_sbx_auctnCsSrchCsYear"
_CASE_NUMBER_ID = "mf_wfm_mainFrame_ibx_auctnCsSrchCsNo"
_CASE_SEARCH_BUTTON_ID = "mf_wfm_mainFrame_btn_auctnCsSrchBtn"

_PHOTO_LABELS = (
    "현황사진",
    "물건사진",
    "사진보기",
    "사진정보",
    "물건이미지",
    "사진",
)
_ITEM_LABELS = (
    "물건내역",
    "매각물건",
    "물건상세",
)
_EXCLUDED_TOKENS = (
    "logo",
    "icon",
    "button",
    "btn",
    "captcha",
    "loading",
    "spinner",
    "banner",
    "header",
    "footer",
    "calendar",
    "close",
    "search",
    "arrow",
    "bullet",
    "blank",
    "home",
    "common",
    "websquare",
)

# 대표사진 후보에서 반드시 배제할 법원문서/지도 UI 토큰. 법원문서 미리보기는
# 큰 canvas로 표시되는 경우가 많아 단순 면적 점수만 사용하면 현황사진으로
# 오인될 수 있다.
_NON_PHOTO_TOKENS = (
    "감정평가서",
    "현황조사서",
    "매각물건명세서",
    "물건명세서",
    "감정서",
    "등기부",
    "문서뷰어",
    "document",
    "docviewer",
    "pdfviewer",
    "pdf.js",
    "application/pdf",
    "위치지도",
    "지도보기",
    "네이버지도",
    "카카오지도",
    "roadview",
    "streetview",
    "map_area",
    "map_wrap",
    "mapcontainer",
)

_PHOTO_CONTEXT_TOKENS = (
    "현황사진",
    "물건사진",
    "사진보기",
    "사진정보",
    "물건이미지",
    "photo",
    "auctionimage",
    "auction-image",
    "goodsimage",
    "goods-image",
)

_VALID_CACHE_SOURCE_PREFIXES = (
    "court-photo-",
    "court-현황사진",
    "court-물건사진",
    "court-사진보기",
    "court-사진정보",
    "court-물건이미지",
    "court-사진",
)


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _case_parts(case_number: str) -> tuple[str, str]:
    match = re.search(r"(\d{4})\s*타경\s*(\d+)", _clean(case_number))
    if not match:
        return "", ""
    return match.group(1), match.group(2)


def _safe_filename(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", value).strip("_")[:60]


def photo_cache_path(item: AuctionItem, cache_dir: Path) -> Path:
    identity = "|".join(
        [item.court or "", item.case_number or "", item.item_number or "", item.auction_id or ""]
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    readable = _safe_filename(f"{item.case_number}_{item.item_number}") or "court_photo"
    return cache_dir / f"{readable}_{digest}.png"




def _source_sidecar(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".source")


def _missing_sidecar(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".missing")


def is_recent_missing_photo_cache(path: Path, cache_days: int = 7) -> bool:
    """현황사진·지도 생성에 실패한 물건을 짧게 음성 캐시해 반복 탐색을 막는다."""
    marker = _missing_sidecar(path)
    try:
        if not marker.is_file():
            return False
        if cache_days <= 0:
            return True
        return (time.time() - marker.stat().st_mtime) <= cache_days * 86400
    except OSError:
        return False


def _write_missing_photo_cache(path: Path, reason: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _missing_sidecar(path).write_text(str(reason or "photo-not-found"), encoding="utf-8")
    except OSError:
        pass


def _clear_missing_photo_cache(path: Path) -> None:
    try:
        _missing_sidecar(path).unlink(missing_ok=True)
    except OSError:
        pass


def _write_photo_source(path: Path, source: str) -> None:
    try:
        _source_sidecar(path).write_text(str(source or ""), encoding="utf-8")
    except OSError:
        pass


def _read_photo_source(path: Path) -> str:
    try:
        return _source_sidecar(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def photo_cache_source(path: Path) -> str:
    """Return the persisted visual source for a cached court image."""
    return _read_photo_source(path)


def is_usable_photo_cache(path: Path, cache_days: int) -> bool:
    """현황사진 계열의 캐시만 재사용한다.

    이전 버전은 사건화면/법원문서의 큰 canvas를 대표사진으로 저장하거나
    주소 기반 위치지도를 대체 시각자료로 저장할 수 있었다. 이런 캐시는 모두
    자동 무효화하여 다음 검색 때 공식 현황사진만 다시 찾도록 한다.
    """
    if not is_cache_fresh(path, cache_days):
        return False
    source = _read_photo_source(path)
    return bool(source) and source.startswith(_VALID_CACHE_SOURCE_PREFIXES)


def discard_unusable_photo_cache(path: Path) -> None:
    """잘못된 구버전 대표사진 캐시와 출처 sidecar를 함께 삭제한다."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        _source_sidecar(path).unlink(missing_ok=True)
    except OSError:
        pass
    _clear_missing_photo_cache(path)


def is_cache_fresh(path: Path, cache_days: int) -> bool:
    if not path.is_file() or path.stat().st_size < 500:
        return False
    if cache_days <= 0:
        return True
    return (time.time() - path.stat().st_mtime) <= cache_days * 86400


def _switch_to_frame_path(driver, frame_path: tuple[int, ...]) -> bool:
    """프레임 인덱스 경로로 이동한다. 동적 페이지에서 프레임이 사라지면 False."""
    from selenium.webdriver.common.by import By

    driver.switch_to.default_content()
    try:
        for index in frame_path:
            frames = driver.find_elements(By.CSS_SELECTOR, "iframe,frame")
            if index >= len(frames):
                driver.switch_to.default_content()
                return False
            driver.switch_to.frame(frames[index])
        return True
    except Exception:
        driver.switch_to.default_content()
        return False


def _frame_paths(driver, max_depth: int = 3) -> list[tuple[int, ...]]:
    """현재 문서에서 접근 가능한 iframe/frame 경로를 수집한다."""
    from selenium.webdriver.common.by import By

    paths: list[tuple[int, ...]] = [()]

    def walk(path: tuple[int, ...], depth: int) -> None:
        if depth >= max_depth or not _switch_to_frame_path(driver, path):
            return
        try:
            count = len(driver.find_elements(By.CSS_SELECTOR, "iframe,frame"))
        except Exception:
            count = 0
        for index in range(count):
            child = path + (index,)
            paths.append(child)
            walk(child, depth + 1)

    walk((), 0)
    driver.switch_to.default_content()
    # 동적 frame 변경으로 중복될 수 있어 순서를 유지하며 제거
    return list(dict.fromkeys(paths))


def _find_by_id(driver, element_id: str):
    from selenium.webdriver.common.by import By

    for path in _frame_paths(driver):
        if not _switch_to_frame_path(driver, path):
            continue
        try:
            elements = driver.find_elements(By.ID, element_id)
            if elements:
                return path, elements[0]
        except Exception:
            continue
    driver.switch_to.default_content()
    return None, None


def _select_option(element, *, value: str = "", text: str = "") -> bool:
    from selenium.webdriver.support.ui import Select

    select = Select(element)
    if value:
        try:
            select.select_by_value(value)
            return True
        except Exception:
            pass
    text_norm = _clean(text)
    if text_norm:
        for option in select.options:
            option_text = _clean(option.text)
            if option_text == text_norm or text_norm in option_text or option_text in text_norm:
                try:
                    option.click()
                    return True
                except Exception:
                    pass
    return False


def _wait_for_case_result(driver, case_number: str, timeout: float) -> None:
    deadline = time.monotonic() + max(2.0, timeout)
    compact = re.sub(r"\s+", "", case_number)
    while time.monotonic() < deadline:
        try:
            if compact and compact in re.sub(r"\s+", "", driver.page_source):
                return
        except Exception:
            pass
        time.sleep(0.2)


def _click_text(driver, labels: Iterable[str], *, exact_first: bool = True) -> bool:
    """모든 프레임에서 짧은 텍스트 버튼/링크를 찾아 클릭한다."""
    script = r"""
      const labels = arguments[0];
      const exactFirst = arguments[1];
      const nodes = Array.from(document.querySelectorAll(
        'button,a,[role="button"],input[type="button"],input[type="submit"],span,div,li,td'
      ));
      const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
      const candidates = [];
      for (const el of nodes) {
        const text = norm(el.innerText || el.textContent || el.value || el.title || el.getAttribute('aria-label'));
        if (!text || text.length > 45) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width < 2 || rect.height < 2) continue;
        for (let i = 0; i < labels.length; i++) {
          const label = labels[i];
          const exact = text === label;
          const contains = text.includes(label);
          if (exact || contains) {
            candidates.push({el, score: (exact ? 10000 : 1000) - i * 20 - text.length});
            break;
          }
        }
      }
      candidates.sort((a,b) => b.score - a.score);
      if (!candidates.length) return false;
      const el = candidates[0].el;
      el.scrollIntoView({block:'center', inline:'center'});
      try { el.click(); } catch (e) {
        el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
      }
      return true;
    """
    label_list = list(labels)
    for path in _frame_paths(driver):
        if not _switch_to_frame_path(driver, path):
            continue
        try:
            if driver.execute_script(script, label_list, exact_first):
                return True
        except Exception:
            continue
    driver.switch_to.default_content()
    return False


def _click_item_row(driver, item_number: str) -> bool:
    item_no = _clean(item_number)
    if not item_no:
        return False
    script = r"""
      const itemNo = arguments[0];
      const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
      const rows = Array.from(document.querySelectorAll('tr,[role="row"],li,.w2grid_body_row'));
      let candidates = [];
      for (const row of rows) {
        const text = norm(row.innerText || row.textContent);
        if (!text) continue;
        const patterns = [
          '물건번호 ' + itemNo, '물건 ' + itemNo, '(' + itemNo + ')',
          '물건번호' + itemNo, '물건' + itemNo
        ];
        if (!patterns.some(p => text.includes(p))) continue;
        const clickable = row.querySelector('a,button,[role="button"],input[type="button"]') || row;
        candidates.push({el:clickable, score:text.length});
      }
      candidates.sort((a,b) => a.score-b.score);
      if (!candidates.length) return false;
      const el = candidates[0].el;
      el.scrollIntoView({block:'center'});
      try { el.click(); } catch(e) {
        el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
      }
      return true;
    """
    for path in _frame_paths(driver):
        if not _switch_to_frame_path(driver, path):
            continue
        try:
            if driver.execute_script(script, item_no):
                return True
        except Exception:
            continue
    driver.switch_to.default_content()
    return False


def _candidate_score(
    driver,
    element,
    *,
    require_photo_context: bool = False,
) -> tuple[float, dict[str, Any]]:
    try:
        meta = driver.execute_script(
            r"""
            const el = arguments[0];
            const r = el.getBoundingClientRect();
            let src = '';
            if (el.tagName === 'IMG') src = el.currentSrc || el.src || '';
            const style = getComputedStyle(el);
            const ancestry = [];
            let node = el;
            for (let i = 0; node && i < 6; i++, node = node.parentElement) {
              ancestry.push(node.id, node.className, node.getAttribute && node.getAttribute('aria-label'));
              if (i <= 2) ancestry.push(node.innerText || node.textContent || '');
            }
            const pageMeta = [document.title, location.href].join(' ');
            const pageText = document.body ? (document.body.innerText || '').slice(0,2500) : '';
            const text = [el.id, el.className, el.alt, el.title, el.getAttribute('aria-label'), src,
                          style.backgroundImage, ...ancestry, pageMeta]
                         .filter(Boolean).join(' ').toLowerCase();
            return {
              w:r.width, h:r.height, text:text.slice(0,4000),
              pageText:String(pageText || '').toLowerCase(), tag:el.tagName, src,
              naturalW:Number(el.naturalWidth || 0), naturalH:Number(el.naturalHeight || 0)
            };
            """,
            element,
        )
    except Exception:
        return -1.0, {}
    if not isinstance(meta, dict):
        return -1.0, {}
    try:
        width = float(meta.get("w") or 0)
        height = float(meta.get("h") or 0)
    except (TypeError, ValueError):
        return -1.0, meta
    text = _clean(meta.get("text")).lower()
    page_text = _clean(meta.get("pageText")).lower()
    if width < 100 or height < 65:
        return -1.0, meta
    if any(token in text for token in _EXCLUDED_TOKENS):
        return -1.0, meta
    if any(token in text for token in _NON_PHOTO_TOKENS):
        return -1.0, meta
    has_photo_context = any(
        token in text or token in page_text for token in _PHOTO_CONTEXT_TOKENS
    )
    if require_photo_context and not has_photo_context:
        return -1.0, meta
    # 지나치게 가늘거나 화면 전체를 덮는 UI 배경은 제외한다.
    ratio = width / max(height, 1.0)
    if ratio > 8.0 or ratio < 0.15:
        return -1.0, meta
    tag = str(meta.get("tag", "")).upper()
    # A4 문서뷰어 canvas의 전형적인 세로 비율은 사진 탭 안에서도 오검출될 수
    # 있으므로, 명확한 사진 문맥이 없는 세로 canvas는 제외한다.
    if tag == "CANVAS" and 0.58 <= ratio <= 0.83 and not has_photo_context:
        return -1.0, meta
    score = width * height
    if has_photo_context:
        score *= 2.5
    if tag == "IMG":
        score *= 1.25
    return score, meta


def _capture_best_visual(
    driver,
    destination: Path,
    *,
    require_photo_context: bool = False,
) -> bool:
    from selenium.webdriver.common.by import By

    best_score = -1.0
    best_temp: Path | None = None
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_suffix(".candidate.png")
    temp.unlink(missing_ok=True)

    selectors = "img,canvas,video,[style*='background-image'],[class*='photo'],[class*='image'],[id*='photo'],[id*='image']"
    for path in _frame_paths(driver):
        if not _switch_to_frame_path(driver, path):
            continue
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, selectors)
        except Exception:
            continue
        for element in elements:
            try:
                if not element.is_displayed():
                    continue
            except Exception:
                continue
            score, _ = _candidate_score(
                driver, element, require_photo_context=require_photo_context
            )
            if score <= best_score:
                continue
            try:
                element.scroll_into_view if False else None
                driver.execute_script("arguments[0].scrollIntoView({block:'center',inline:'center'});", element)
                time.sleep(0.08)
                if element.screenshot(str(temp)) and temp.is_file() and temp.stat().st_size >= 500:
                    best_score = score
                    best_temp = temp
            except Exception:
                continue

    driver.switch_to.default_content()
    if best_temp and best_temp.is_file():
        best_temp.replace(destination)
        return True
    temp.unlink(missing_ok=True)
    return False


def _capture_hidden_image_clone(
    driver,
    destination: Path,
    *,
    require_photo_context: bool = False,
) -> bool:
    """갤러리에 미리 로드됐지만 숨겨진 IMG가 있으면 복제해 캡처한다."""
    best_score = -1.0
    temp = destination.with_suffix(".hidden.png")
    temp.unlink(missing_ok=True)
    script = r"""
      const excluded = arguments[0];
      const nonPhoto = arguments[1];
      const photoTokens = arguments[2];
      const requirePhotoContext = arguments[3];
      const imgs = Array.from(document.images || []);
      let best = null;
      let bestScore = -1;
      for (const img of imgs) {
        const src = String(img.currentSrc || img.src || '');
        const ancestry = [];
        let node = img;
        for (let i = 0; node && i < 6; i++, node = node.parentElement) {
          ancestry.push(node.id, node.className, node.getAttribute && node.getAttribute('aria-label'));
          if (i <= 2) ancestry.push(node.innerText || node.textContent || '');
        }
        const text = [img.id, img.className, img.alt, img.title, src, ...ancestry,
                      document.title, location.href].join(' ').toLowerCase();
        const pageText = document.body ? (document.body.innerText || '').slice(0,2500).toLowerCase() : '';
        if (!src || excluded.some(x => text.includes(x))) continue;
        if (nonPhoto.some(x => text.includes(x))) continue;
        const hasPhotoContext = photoTokens.some(x => text.includes(x) || pageText.includes(x));
        if (requirePhotoContext && !hasPhotoContext) continue;
        const w = Number(img.naturalWidth || img.width || 0);
        const h = Number(img.naturalHeight || img.height || 0);
        if (w < 100 || h < 65 || w / Math.max(h,1) > 8) continue;
        let score = w * h;
        if (hasPhotoContext) score *= 2.0;
        if (score > bestScore) { best = img; bestScore = score; }
      }
      if (!best) return null;
      const clone = best.cloneNode(true);
      clone.removeAttribute('id');
      clone.style.cssText = 'position:fixed!important;left:20px!important;top:20px!important;display:block!important;visibility:visible!important;opacity:1!important;z-index:2147483647!important;max-width:900px!important;max-height:720px!important;width:auto!important;height:auto!important;background:white!important;';
      document.body.appendChild(clone);
      return [clone, bestScore];
    """
    for path in _frame_paths(driver):
        if not _switch_to_frame_path(driver, path):
            continue
        clone = None
        try:
            result = driver.execute_script(
                script,
                list(_EXCLUDED_TOKENS),
                list(_NON_PHOTO_TOKENS),
                list(_PHOTO_CONTEXT_TOKENS),
                bool(require_photo_context),
            )
            if not isinstance(result, list) or len(result) != 2:
                continue
            clone, score = result
            score = float(score or 0)
            if score <= best_score:
                driver.execute_script("arguments[0].remove();", clone)
                continue
            time.sleep(0.15)
            if clone.screenshot(str(temp)) and temp.is_file() and temp.stat().st_size >= 500:
                best_score = score
            driver.execute_script("arguments[0].remove();", clone)
        except Exception:
            try:
                if clone is not None:
                    driver.execute_script("arguments[0].remove();", clone)
            except Exception:
                pass
            continue
    driver.switch_to.default_content()
    if best_score > 0 and temp.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp.replace(destination)
        return True
    temp.unlink(missing_ok=True)
    return False




def _wait_and_capture_visual(
    driver,
    destination: Path,
    timeout_seconds: float,
    *,
    require_photo_context: bool = False,
) -> bool:
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    while time.monotonic() < deadline:
        if _capture_best_visual(
            driver, destination, require_photo_context=require_photo_context
        ) or _capture_hidden_image_clone(
            driver, destination, require_photo_context=require_photo_context
        ):
            return True
        time.sleep(0.25)
    return False


def _capture_naver_map_fallback(
    driver,
    item: AuctionItem,
    destination: Path,
    *,
    timeout_seconds: float,
) -> bool:
    """법원 원문 사진이 없을 때 소재지 지도 화면을 대표 시각자료로 저장한다."""
    from selenium.webdriver.common.by import By

    address = _clean(item.address)
    if not address:
        return False
    try:
        driver.switch_to.default_content()
        driver.get(f"https://map.naver.com/p/search/{quote(address, safe='')}")
        deadline = time.monotonic() + max(4.0, timeout_seconds)
        while time.monotonic() < deadline:
            try:
                if driver.execute_script("return document.readyState") == "complete":
                    break
            except Exception:
                pass
            time.sleep(0.2)
        time.sleep(0.8)
        # 지도 canvas 또는 지도 컨테이너를 우선 캡처한다.
        selectors = (
            "canvas", "#map", "[class*='map_area']", "[class*='map_wrap']", "[class*='Map']"
        )
        best = None
        best_area = 0.0
        for selector in selectors:
            try:
                for el in driver.find_elements(By.CSS_SELECTOR, selector):
                    if not el.is_displayed():
                        continue
                    size = el.size or {}
                    area = float(size.get("width") or 0) * float(size.get("height") or 0)
                    if area > best_area and area >= 120_000:
                        best, best_area = el, area
            except Exception:
                continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if best is not None:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", best)
            if best.screenshot(str(destination)) and destination.stat().st_size >= 500:
                return True
        # DOM 구조가 바뀌어 지도 컨테이너를 못 찾더라도 검색결과 전체 화면을 보존한다.
        if driver.save_screenshot(str(destination)) and destination.stat().st_size >= 500:
            return True
    except Exception:
        pass
    destination.unlink(missing_ok=True)
    return False


def _save_photo_debug(driver, debug_dir: Path | None, item: AuctionItem, label: str) -> str:
    if debug_dir is None:
        return ""
    try:
        debug_dir.mkdir(parents=True, exist_ok=True)
        identity = hashlib.sha256(
            f"{item.case_number}|{item.item_number}|{label}".encode("utf-8")
        ).hexdigest()[:12]
        stem = _safe_filename(f"{item.case_number}_{item.item_number}_{label}") or identity
        stem = f"{stem}_{identity}"
        driver.switch_to.default_content()
        png = debug_dir / f"{stem}.png"
        html_path = debug_dir / f"{stem}.html"
        driver.save_screenshot(str(png))
        html_path.write_text(driver.page_source, encoding="utf-8")
        return str(png.resolve())
    except Exception:
        return ""


def _switch_to_latest_window(driver, original_handle: str, known_handles: set[str]) -> str:
    try:
        current = set(driver.window_handles)
    except Exception:
        return original_handle
    new_handles = [h for h in current if h not in known_handles]
    target = new_handles[-1] if new_handles else original_handle
    try:
        driver.switch_to.window(target)
    except Exception:
        target = original_handle
    return target


def capture_court_photo(
    driver,
    item: AuctionItem,
    *,
    cache_dir: Path,
    cache_days: int = 30,
    timeout_seconds: float = 12.0,
    settle_seconds: float = 0.8,
    capture_timeout_seconds: float = 2.5,
    missing_cache_days: int = 7,
    debug_dir: Path | None = None,
    map_fallback: bool = False,
) -> tuple[str, str]:
    """법원경매 사건 상세화면에서 대표사진을 캡처한다.

    반환값은 ``(파일경로, 출처상태)``다. 사진이 없거나 UI가 변경된 경우 예외 대신
    빈 경로와 상태문자열을 반환해 전체 검색이 중단되지 않게 한다.
    """
    path = photo_cache_path(item, cache_dir)
    if is_usable_photo_cache(path, cache_days):
        return str(path.resolve()), _read_photo_source(path) or "cache"
    if path.exists() or _source_sidecar(path).exists():
        discard_unusable_photo_cache(path)
    if is_recent_missing_photo_cache(path, missing_cache_days):
        return "", "photo-not-found-cached"

    def fallback(reason: str) -> tuple[str, str]:
        """현황사진이 없으면 빈 상태로 두고 음성 캐시만 남긴다."""
        _write_missing_photo_cache(path, reason)
        return "", reason

    year, serial = _case_parts(item.case_number)
    if not year or not serial:
        return fallback("invalid-case-number")

    original_handle = ""
    known_handles: set[str] = set()
    try:
        original_handle = driver.current_window_handle
        known_handles = set(driver.window_handles)
        # 직전 물건 처리 후 사건검색 폼이 남아 있으면 페이지를 다시 열지 않는다.
        # 상위 후보 여러 건을 연속 보강할 때 전체 페이지 로드를 크게 줄인다.
        court_path, court_el = _find_by_id(driver, _CASE_COURT_ID)
        if court_el is None:
            driver.get(CASE_SEARCH_URL)
            deadline = time.monotonic() + max(3.0, timeout_seconds)
            while time.monotonic() < deadline:
                try:
                    if driver.execute_script("return document.readyState") in {"interactive", "complete"}:
                        break
                except Exception:
                    pass
                time.sleep(0.15)
            court_path, court_el = _find_by_id(driver, _CASE_COURT_ID)
        if court_el is None:
            return fallback("case-search-form-not-found")
        court_code = _clean((item.raw or {}).get("boCd") or (item.raw or {}).get("cortOfcCd"))
        if not _select_option(court_el, value=court_code, text=item.court):
            logger.debug("법원사진: 법원 선택값을 찾지 못함 court=%s code=%s", item.court, court_code)

        year_path, year_el = _find_by_id(driver, _CASE_YEAR_ID)
        if year_el is None:
            return fallback("case-year-not-found")
        _select_option(year_el, value=year, text=year)

        number_path, number_el = _find_by_id(driver, _CASE_NUMBER_ID)
        if number_el is None:
            return fallback("case-number-input-not-found")
        number_el.clear()
        number_el.send_keys(serial)

        button_path, button_el = _find_by_id(driver, _CASE_SEARCH_BUTTON_ID)
        if button_el is None:
            return fallback("case-search-button-not-found")
        button_el.click()
        _wait_for_case_result(driver, item.case_number, timeout_seconds)
        time.sleep(max(0.2, settle_seconds))

        # 다물건 사건이면 대상 물건 행을 먼저 선택한다.
        if _click_item_row(driver, item.item_number):
            time.sleep(max(0.2, settle_seconds))
            _switch_to_latest_window(driver, original_handle, known_handles)

        # 물건내역 탭으로 이동한 뒤 현황사진/물건사진 탭만 단계적으로 누른다.
        # 사건화면이나 물건내역의 큰 이미지·canvas를 바로 캡처하지 않는다.
        # 해당 영역에는 위치지도나 법원문서 미리보기가 포함될 수 있기 때문이다.
        if _click_text(driver, _ITEM_LABELS):
            time.sleep(max(0.2, settle_seconds))
            _switch_to_latest_window(driver, original_handle, known_handles)

        # 모든 사진 라벨을 한 번에 탐색한다. 기존 방식은 라벨마다 전체 프레임과 DOM을
        # 다시 훑어 사진이 없는 사건에서 최대 6회의 불필요한 탐색이 발생했다.
        if _click_text(driver, _PHOTO_LABELS):
            time.sleep(max(0.2, settle_seconds))
            _switch_to_latest_window(driver, original_handle, known_handles)
            if _wait_and_capture_visual(
                driver,
                path,
                min(float(capture_timeout_seconds), timeout_seconds),
                require_photo_context=True,
            ):
                source = "court-photo-priority"
                _clear_missing_photo_cache(path)
                _write_photo_source(path, source)
                return str(path.resolve()), source

        _save_photo_debug(driver, debug_dir, item, "official_photo_not_found")
        return fallback("photo-not-found")
    except Exception as exc:
        logger.warning("법원경매 대표사진 수집 실패 %s: %s", item.auction_id, exc)
        _save_photo_debug(driver, debug_dir, item, f"error_{type(exc).__name__}")
        return fallback(f"error:{type(exc).__name__}")
    finally:
        # 사진 팝업이 열렸으면 닫고 원래 탭으로 돌아간다.
        try:
            for handle in list(driver.window_handles):
                if original_handle and handle != original_handle:
                    driver.switch_to.window(handle)
                    driver.close()
            if original_handle:
                driver.switch_to.window(original_handle)
        except Exception:
            pass
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
