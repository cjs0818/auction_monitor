from __future__ import annotations

import copy
import html as html_lib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from landwatch.card_view import build_result_cards_html, card_image_diagnostics
from landwatch.config import load_config, save_config
from landwatch.db import Database
from landwatch.detail_view import build_detail_payload, build_map_urls, build_naver_map_html
from landwatch.onbid_openapi import ONBID_REAL_ESTATE_SEARCH_URL
from landwatch.providers import SEARCH_TARGET_OPTIONS, build_provider
from landwatch.regions import (
    REGION_CODE_AUDIT,
    municipality_names,
    province_names,
    region_defaults_from_profile,
)
from landwatch.report import format_dataframe_for_report, to_dataframe
from landwatch.runner import RunResult, run_once
from landwatch.ui_config import (
    DEFAULT_PROFILE,
    LAND_USE_OPTIONS,
    RISK_OPTION_MAP,
    SCORING_PRESETS,
    STATUS_OPTIONS,
    custom_exclusion_keywords,
    duplicate_profile,
    exclusion_keywords,
    exclusion_labels,
    manwon_to_won,
    new_profile,
    normalize_profile,
    parse_keywords,
    profile_summary,
    sqm_to_pyeong,
    unique_profile_name,
    won_to_manwon,
)

st.set_page_config(
    page_title="토지 경매·공매 투자후보 모니터",
    page_icon="🏞️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
.block-container {padding-top: 1.6rem; padding-bottom: 3rem;}
[data-testid="stMetricValue"] {font-variant-numeric: tabular-nums;}
.small-note {color:#667085; font-size:0.9rem;}
.condition-card {border:1px solid #e4e7ec; border-radius:12px; padding:14px 16px; background:#fafafa;}
.profile-summary {padding:10px 12px; border-left:4px solid #4f46e5; background:#f5f3ff; border-radius:6px;}
div[data-testid="stButton"] > button {border-radius:8px;}
</style>
""",
    unsafe_allow_html=True,
)

PROJECT_ROOT = Path(__file__).resolve().parent
os.chdir(PROJECT_ROOT)
CONFIG_PATH = PROJECT_ROOT / "config/config.yaml"
EXAMPLE_PATH = PROJECT_ROOT / "config/config.example.yaml"
if not CONFIG_PATH.exists():
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(EXAMPLE_PATH.read_text(encoding="utf-8"), encoding="utf-8")


def load_ui_config() -> dict[str, Any]:
    return load_config(CONFIG_PATH, expand_environment=False)


def persist(cfg: dict[str, Any]) -> None:
    save_config(cfg, CONFIG_PATH)


def safe_key(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]+", "_", str(value))[:60]


def show_flash() -> None:
    flash = st.session_state.pop("flash_message", None)
    if flash:
        kind, text = flash
        getattr(st, kind)(text)


def validate_profile(profile: dict[str, Any], profiles: list[dict[str, Any]], index: int) -> list[str]:
    errors: list[str] = []
    name = str(profile.get("name", "")).strip()
    if not name:
        errors.append("검색조건 이름을 입력하십시오.")
    other_names = {
        str(p.get("name", "")).strip() for i, p in enumerate(profiles) if i != index
    }
    if name in other_names:
        errors.append("같은 이름의 검색조건이 이미 있습니다.")
    for key, label in (
        ("failed_count", "유찰횟수"),
        ("min_price", "최저매각가격"),
        ("appraisal_price", "감정평가액"),
        ("land_area_m2", "토지면적"),
        ("appraisal_discount_percent", "감정평가액 대비 할인율"),
    ):
        spec = profile.get(key, {}) or {}
        low = float(spec.get("min") or 0)
        high = float(spec.get("max") or 0)
        if high and low > high:
            errors.append(f"{label}의 최소값이 최대값보다 큽니다.")
    if not profile.get("usages"):
        errors.append("물건용도를 하나 이상 선택하십시오.")
    return errors


def profile_consistency_warnings(profile: dict[str, Any]) -> list[str]:
    """첫 화면에서는 조건 조합 안내 문구를 별도로 표시하지 않는다."""
    return []


def _compact_case_number(value: str) -> str:
    return re.sub(r"[^0-9가-힣]", "", str(value or ""))


def show_excluded_items(result: RunResult) -> None:
    excluded = getattr(result, "excluded_items", []) or []
    if not excluded:
        return
    st.markdown("##### 수집됐지만 조건에서 제외된 물건")
    case_query = st.text_input(
        "누락 사건번호·공고번호 확인",
        placeholder="예: 2025타경385 또는 온비드 공고번호",
        help="경매·공매 목록에는 수집됐지만 최종 조건에서 제외됐는지 확인합니다.",
        key="excluded_case_query",
    )
    display_rows = excluded
    if case_query.strip():
        needle = _compact_case_number(case_query)
        display_rows = [
            row for row in excluded
            if needle and needle in _compact_case_number(row.get("사건/공고번호", row.get("사건번호", "")))
        ]
        if display_rows:
            st.warning(
                f"{case_query}는 원문 데이터에서 수집됐지만 아래 조건 때문에 투자후보에서 제외됐습니다."
            )
        else:
            st.info(
                f"{case_query}는 이번 목록 수집자료에서 확인되지 않았습니다. "
                "매각기일 범위, 입찰방식, 법원의 물건 대분류 또는 사건 상태를 확인해야 합니다."
            )
    with st.expander(f"제외 물건 보기 ({len(display_rows):,}건)", expanded=bool(case_query.strip())):
        if display_rows:
            df = pd.DataFrame(display_rows)
            for col in ("최저매각가격", "감정평가액"):
                if col in df.columns:
                    df[col] = df[col].map(lambda x: f"{int(x or 0):,}")
            st.dataframe(df, use_container_width=True, hide_index=True, height=360)
        else:
            st.caption("일치하는 제외 물건이 없습니다.")



def _query_param(name: str) -> str:
    """Streamlit 버전에 관계없이 단일 query parameter 값을 읽는다."""
    try:
        value = st.query_params.get(name, "")
        if isinstance(value, (list, tuple)):
            return str(value[0] if value else "")
        return str(value or "")
    except Exception:
        try:
            values = st.experimental_get_query_params().get(name, [])
            return str(values[0] if values else "")
        except Exception:
            return ""


def _clear_detail_query() -> None:
    """상세 팝업을 연 내부 링크 query parameter를 제거한다."""
    keys = (
        "lw_detail_source", "lw_detail_index", "lw_detail_nonce",
        "lw_detail_case", "lw_detail_item", "lw_detail_profile",
        "lw_detail_auction", "lw_detail_court", "lw_detail_page",
    )
    try:
        for key in keys:
            try:
                del st.query_params[key]
            except (KeyError, AttributeError):
                pass
        return
    except Exception:
        pass
    try:
        params = st.experimental_get_query_params()
        for key in keys:
            params.pop(key, None)
        st.experimental_set_query_params(**params)
    except Exception:
        pass


def _display_cell(value: Any) -> str:
    try:
        if pd.isna(value):
            return "-"
    except Exception:
        pass
    text = str(value if value not in (None, "") else "-")
    return html_lib.escape(text)


def _item_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_dict"):
        mapped = value.to_dict()
        if isinstance(mapped, dict):
            return mapped
    return {}


def _detail_identity(value: Any) -> dict[str, str]:
    data = _item_mapping(value)
    raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
    return {
        "case": str(data.get("case_number") or raw.get("PLNM_NO") or raw.get("printCsNo") or raw.get("srnSaNo") or "").strip(),
        "item": str(data.get("item_number") or raw.get("CLTR_MNMT_NO") or raw.get("mokmulSer") or raw.get("maemulSer") or "").strip(),
        "profile": str(data.get("matched_profile") or data.get("profile_name") or "").strip(),
        "auction": str(data.get("auction_id") or data.get("document_id") or raw.get("docid") or "").strip(),
        "court": str(data.get("court") or raw.get("ORG_NM") or raw.get("jiwonNm") or raw.get("cortOfcNm") or "").strip(),
    }


def _detail_query(identity: dict[str, str], source: str) -> str:
    params = {
        "lw_detail_page": "1",
        "lw_detail_source": source,
        "lw_detail_case": identity.get("case", ""),
        "lw_detail_item": identity.get("item", ""),
        "lw_detail_profile": identity.get("profile", ""),
        "lw_detail_auction": identity.get("auction", ""),
        "lw_detail_court": identity.get("court", ""),
    }
    return "?" + urlencode({k: v for k, v in params.items() if v})


def _render_case_hyperlink_table(
    df: pd.DataFrame,
    *,
    source: str,
    key: str,
    height: int,
    detail_items: list[Any] | None = None,
) -> None:
    """사건번호를 안정적인 사건·물건 식별자 링크로 표시한다.

    링크 클릭으로 Streamlit 세션이 새로 만들어져도 DB에서 동일 물건을 다시
    찾을 수 있도록 행 인덱스가 아니라 사건번호·물건번호·검색조건·auction_id를
    query parameter에 넣는다.
    """
    if df.empty:
        st.info("표시할 결과가 없습니다.")
        return

    rows = list(detail_items or [])
    columns = list(df.columns)
    html_parts = [
        "<style>",
        ".lw-table-wrap{height:%dpx;max-height:%dpx;overflow-x:scroll;overflow-y:scroll;scrollbar-gutter:stable both-edges;border:1px solid #e5e7eb;border-radius:8px;background:white;scrollbar-width:auto;scrollbar-color:#98a2b3 #f1f5f9;}" % (max(220, height-8), max(220, height-8)),
        ".lw-table-wrap::-webkit-scrollbar{width:14px;height:14px;}",
        ".lw-table-wrap::-webkit-scrollbar-track{background:#f1f5f9;border-radius:8px;}",
        ".lw-table-wrap::-webkit-scrollbar-thumb{background:#98a2b3;border:3px solid #f1f5f9;border-radius:10px;}",
        ".lw-table-wrap::-webkit-scrollbar-thumb:hover{background:#667085;}",
        ".lw-table{border-collapse:separate;border-spacing:0;min-width:2100px;width:max-content;font-size:13px;color:#1f2937;}",
        ".lw-table th{position:sticky;top:0;z-index:2;background:#f8fafc;color:#344054;font-weight:600;border-bottom:1px solid #d0d5dd;padding:9px 10px;text-align:left;white-space:nowrap;}",
        ".lw-table td{border-bottom:1px solid #eaecf0;padding:8px 10px;vertical-align:middle;white-space:nowrap;}",
        ".lw-table tr:hover td{background:#f9fafb;}",
        ".lw-table td.address{max-width:390px;white-space:normal;line-height:1.35;}",
        ".lw-table a.case-link{color:#0969da;text-decoration:underline;font-weight:650;cursor:pointer;}",
        ".lw-table a.case-link:hover{color:#0550ae;text-decoration-thickness:2px;}",
        ".lw-table a.external-link{color:#475467;text-decoration:underline;}",
        "</style>",
        "<div class='lw-table-wrap'><table class='lw-table'><thead><tr>",
    ]
    for col in columns:
        html_parts.append(f"<th>{html_lib.escape(str(col))}</th>")
    html_parts.append("</tr></thead><tbody>")

    for row_index, (_, row) in enumerate(df.reset_index(drop=True).iterrows()):
        html_parts.append("<tr>")
        identity = _detail_identity(rows[row_index]) if row_index < len(rows) else {
            "case": str(row.get("사건/공고번호", row.get("사건번호", "")) or ""),
            "item": str(row.get("물건번호/물건관리번호", row.get("물건번호", "")) or ""),
            "profile": str(row.get("검색조건", "") or ""),
            "auction": "",
            "court": str(row.get("진행기관", row.get("법원", "")) or ""),
        }
        for col in columns:
            raw_value = row.get(col, "")
            text = _display_cell(raw_value)
            css_class = "address" if col == "소재지" else ""
            if col in {"사건번호", "사건/공고번호"} and text != "-":
                query = _detail_query(identity, source)
                html_parts.append(
                    f"<td class='{css_class}'><a class='case-link' target='_blank' rel='noopener noreferrer' "
                    f"href='{html_lib.escape(query, quote=True)}'>{text}</a></td>"
                )
            elif col in {"법원경매정보 URL", "원문 URL"} and str(raw_value or "").strip():
                url = html_lib.escape(str(raw_value), quote=True)
                html_parts.append(
                    f"<td><a class='external-link' href='{url}' target='_blank' rel='noopener'>원문 검색</a></td>"
                )
            else:
                html_parts.append(f"<td class='{css_class}'>{text}</td>")
        html_parts.append("</tr>")

    html_parts.append("</tbody></table></div>")
    table_html = "".join(html_parts)
    if hasattr(st, "html"):
        st.html(table_html)
    else:
        st.markdown(table_html, unsafe_allow_html=True)


def _history_detail_items(cfg: dict[str, Any], limit: int = 300) -> list[dict[str, Any]]:
    """저장된 결과를 상세 팝업에서 사용할 payload 목록으로 복원한다."""
    db_path = Path(str(cfg.get("app", {}).get("database_path", "data/landwatch.db")))
    if not db_path.exists():
        return []
    rows = Database(str(db_path)).recent_items(limit)
    items: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row.get("payload_json") or "{}")
        payload.setdefault("matched_profile", row.get("profile_name", ""))
        payload.setdefault("grade", row.get("grade", ""))
        payload.setdefault("score", row.get("score", 0))
        payload.setdefault("status", row.get("status", ""))
        payload.setdefault("usage", row.get("usage", ""))
        payload.setdefault("address", row.get("address", ""))
        payload.setdefault("min_price", row.get("min_price", 0))
        payload.setdefault("appraisal_price", row.get("appraisal_price", 0))
        payload.setdefault("failed_count", row.get("failed_count", 0))
        payload.setdefault("land_area_m2", row.get("land_area_m2", 0))
        payload.setdefault("auction_date", row.get("auction_date", ""))
        payload.setdefault("detail_url", row.get("detail_url", ""))
        items.append(payload)
    return items


def _same_text(left: Any, right: Any) -> bool:
    return str(left or "").strip() == str(right or "").strip()


def _find_detail_item(candidates: list[Any], wanted: dict[str, str]) -> Any | None:
    """Stable identifiers first; case/item/profile/court as fallback."""
    wanted_case = _compact_case_number(wanted.get("case", ""))
    wanted_auction = wanted.get("auction", "").strip()
    wanted_item = wanted.get("item", "").strip()
    wanted_profile = wanted.get("profile", "").strip()
    wanted_court = wanted.get("court", "").strip()

    if wanted_auction:
        for candidate in candidates:
            identity = _detail_identity(candidate)
            if identity.get("auction") == wanted_auction:
                if not wanted_profile or identity.get("profile") == wanted_profile:
                    return candidate

    matches: list[Any] = []
    for candidate in candidates:
        identity = _detail_identity(candidate)
        if wanted_case and _compact_case_number(identity.get("case", "")) != wanted_case:
            continue
        if wanted_item and not _same_text(identity.get("item"), wanted_item):
            continue
        if wanted_profile and not _same_text(identity.get("profile"), wanted_profile):
            continue
        if wanted_court and identity.get("court") and not _same_text(identity.get("court"), wanted_court):
            continue
        matches.append(candidate)
    return matches[0] if matches else None


def _resolve_linked_detail_item(cfg: dict[str, Any]) -> Any | None:
    """Query parameter의 영구 식별자로 상세 물건을 복원한다."""
    source = _query_param("lw_detail_source")
    if source not in {"current", "history"}:
        return None

    wanted = {
        "case": _query_param("lw_detail_case"),
        "item": _query_param("lw_detail_item"),
        "profile": _query_param("lw_detail_profile"),
        "auction": _query_param("lw_detail_auction"),
        "court": _query_param("lw_detail_court"),
    }

    current_items: list[Any] = []
    last_result = st.session_state.get("last_result")
    if last_result:
        current_items = list(getattr(last_result, "items", []) or [])

    # 새 탭은 별도 Streamlit 세션이므로 DB 저장결과를 넉넉히 읽어 복원한다.
    history_items = _history_detail_items(cfg, limit=5000)
    item = _find_detail_item(current_items, wanted)
    if item is None:
        item = _find_detail_item(history_items, wanted)

    # 구버전 인덱스 링크도 같은 세션에서는 임시 호환한다.
    if item is None and not any(wanted.values()):
        try:
            row_index = int(_query_param("lw_detail_index"))
        except (TypeError, ValueError):
            row_index = -1
        legacy_items = current_items if source == "current" else history_items
        if 0 <= row_index < len(legacy_items):
            item = legacy_items[row_index]
    return item


def _dispatch_linked_detail(cfg: dict[str, Any]) -> None:
    """구버전 동일 탭 링크를 상세 모달로 처리한다."""
    if _query_param("lw_detail_page") == "1":
        return
    source = _query_param("lw_detail_source")
    if source not in {"current", "history"}:
        return
    item = _resolve_linked_detail_item(cfg)
    if item is None:
        st.warning(
            "선택한 물건의 상세정보를 저장결과에서 찾지 못했습니다. "
            "사건번호 링크를 다시 눌러 주십시오."
        )
        return
    show_item_detail_dialog(item, cfg)


def _render_linked_detail_page(cfg: dict[str, Any]) -> None:
    """새 브라우저 탭에서 독립적인 사건 상세페이지를 렌더링한다."""
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"]{display:none;}
        .block-container{max-width:1500px;padding-top:1.2rem;}
        </style>
        """,
        unsafe_allow_html=True,
    )
    item = _resolve_linked_detail_item(cfg)
    if item is None:
        st.title("매각물건 상세정보")
        st.error("선택한 물건의 상세정보를 저장결과에서 찾지 못했습니다.")
        st.caption(
            "원래 검색 탭에서 검색을 다시 실행한 뒤 사건번호를 눌러 주십시오. "
            "현재 탭은 닫아도 됩니다."
        )
        st.link_button("검색 대시보드 열기", "/", use_container_width=True)
        return
    st.title("매각물건 상세정보")
    st.caption("검색 결과에서 선택한 경매·공매 물건의 상세정보입니다. 이 페이지는 별도 탭에서 열렸습니다.")
    _detail_dialog_body(item, cfg, standalone=True)


def _selected_rows(event: Any) -> list[int]:
    if event is None:
        return []
    selection = getattr(event, "selection", None)
    rows = getattr(selection, "rows", None)
    if rows is not None:
        return [int(x) for x in rows]
    if isinstance(event, dict):
        selected = event.get("selection", {})
        if isinstance(selected, dict):
            return [int(x) for x in selected.get("rows", [])]
    return []


def _selectable_dataframe(
    df: pd.DataFrame,
    *,
    key: str,
    height: int,
    column_config: dict[str, Any] | None = None,
) -> list[int]:
    """Streamlit 버전이 지원하면 행 선택을 활성화한다."""
    kwargs = dict(
        use_container_width=True,
        hide_index=True,
        height=height,
        column_config=column_config or {},
    )
    try:
        event = st.dataframe(
            df,
            key=key,
            on_select="rerun",
            selection_mode="single-row",
            **kwargs,
        )
        return _selected_rows(event)
    except TypeError:
        # Streamlit 1.36 일부 빌드에서는 on_select가 없을 수 있다.
        st.dataframe(df, **kwargs)
        st.caption("행 선택 팝업을 사용하려면 Streamlit 1.37 이상으로 업데이트하십시오.")
        return []


def _profile_for_detail(cfg: dict[str, Any], profile_name: str) -> dict[str, Any]:
    profiles = cfg.get("profiles", []) or []
    for profile in profiles:
        if str(profile.get("name", "")) == str(profile_name or ""):
            return profile
    return profiles[0] if profiles else {}


def _render_case_lookup(detail: dict[str, Any], item_number: str) -> None:
    if not detail:
        return
    if not detail.get("found"):
        st.warning(detail.get("진단", "법원 사건상세정보를 찾지 못했습니다."))
        return
    st.success(detail.get("진단", "법원 사건상세정보를 확인했습니다."))
    summary = {
        "법원": detail.get("법원", ""),
        "사건번호": detail.get("사건번호", ""),
        "사건명": detail.get("사건명", ""),
        "진행상태코드": detail.get("진행상태코드", ""),
        "다음매각기일": detail.get("다음매각기일", ""),
    }
    st.dataframe(pd.DataFrame([summary]), use_container_width=True, hide_index=True)

    object_rows = detail.get("물건내역", []) or []
    if item_number and item_number != "-":
        matched = [x for x in object_rows if str(x.get("물건번호", "")) == str(item_number)]
        if matched:
            object_rows = matched
    if object_rows:
        st.markdown("**법원 물건내역**")
        st.dataframe(pd.DataFrame(object_rows), use_container_width=True, hide_index=True)

    schedule_rows = detail.get("매각기일내역", []) or []
    if item_number and item_number != "-":
        matched = [x for x in schedule_rows if str(x.get("물건번호", "")) == str(item_number)]
        if matched:
            schedule_rows = matched
    if schedule_rows:
        schedule_df = pd.DataFrame(schedule_rows)
        for col in ("감정평가액", "최저매각가격"):
            if col in schedule_df.columns:
                schedule_df[col] = schedule_df[col].map(lambda x: f"{int(x or 0):,}")
        st.markdown("**매각기일내역**")
        st.dataframe(schedule_df, use_container_width=True, hide_index=True)


def _detail_dialog_body(item: Any, cfg: dict[str, Any], standalone: bool = False) -> None:
    detail = build_detail_payload(item)
    sale_type = detail.get("매각구분", "경매")
    number = detail.get("사건/공고번호", "-")
    item_number = detail.get("물건번호/물건관리번호", "-")
    number_label = "공고번호" if sale_type == "공매" else "사건번호"
    item_label = "물건관리번호" if sale_type == "공매" else "물건번호"
    price_label = "최저입찰가" if sale_type == "공매" else "최저매각가격"
    date_label = "입찰마감일" if sale_type == "공매" else "매각기일"

    st.markdown(f"### [{sale_type}] {number_label} {number} · {item_label} {item_number}")
    st.caption(detail["소재지"])

    m1, m2, m3, m4 = st.columns(4)
    m1.metric(price_label, detail["최저매각가격/최저입찰가"])
    m2.metric("감정평가액", detail["감정평가액"])
    m3.metric("할인율", detail["감정평가액 대비 할인율"])
    m4.metric("투자검토점수", detail["투자검토점수"])

    source_tab_label = "온비드 공매정보" if sale_type == "공매" else "법원 사건정보"
    tab_info, tab_map, tab_source, tab_raw = st.tabs([
        "상세정보", "네이버 지도", source_tab_label, "원시정보",
    ])

    with tab_info:
        info_rows = [
            ("매각구분", sale_type),
            ("데이터 출처", detail.get("데이터출처", "-")),
            ("검색조건", detail["검색조건"]),
            ("검토등급", detail["검토등급"]),
            ("진행기관", detail["진행기관"]),
            ("진행상태", detail["진행상태"]),
            ("물건용도", detail["물건용도"]),
            ("유찰횟수", detail["유찰횟수"]),
            ("토지면적", detail["토지면적"]),
            ("평환산", detail["평환산"]),
            ("㎡당 최저가격", detail["㎡당 최저매각가격"]),
            (date_label, detail["매각기일/입찰마감일"]),
        ]
        st.dataframe(
            pd.DataFrame(info_rows, columns=["항목", "내용"]),
            use_container_width=True,
            hide_index=True,
        )
        if detail["검토근거"]:
            st.markdown("**검토근거**")
            st.write("\n".join(f"• {x}" for x in detail["검토근거"]))
        if detail["주의사항"]:
            st.markdown("**주의사항**")
            st.warning("\n".join(f"• {x}" for x in detail["주의사항"]))
        if detail["특수조건·비고"]:
            st.markdown("**특수조건·비고**")
            st.write("\n".join(f"• {x}" for x in detail["특수조건·비고"]))
        if detail["상세URL"]:
            if sale_type == "공매":
                data = _item_mapping(item)
                raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
                management_no = str(
                    raw.get("onbid_search_management_no")
                    or raw.get("cltrMngNo")
                    or raw.get("CLTR_MNMT_NO")
                    or data.get("item_number")
                    or ""
                ).strip()
                link_mode = str(raw.get("onbid_link_mode") or "management-number-search")
                if link_mode == "explicit-web-detail":
                    st.link_button("온비드 원문 열기", detail["상세URL"], use_container_width=True)
                else:
                    search_params = urlencode({
                        "searchCltrMnmtNo": management_no,
                        "cltrMnmtNo": management_no,
                    }) if management_no else ""
                    search_url = (
                        f"{ONBID_REAL_ESTATE_SEARCH_URL}?{search_params}"
                        if search_params else ONBID_REAL_ESTATE_SEARCH_URL
                    )
                    st.info(
                        "차세대 온비드 OpenAPI의 식별자는 웹 상세페이지 내부번호와 달라 "
                        "구 상세 URL을 직접 만들 수 없습니다. 공식 부동산 물건검색 화면을 연 뒤 "
                        "아래 물건관리번호로 조회하십시오."
                    )
                    if management_no:
                        st.code(management_no, language=None)
                    st.link_button(
                        "온비드 부동산 물건검색 열기",
                        search_url,
                        use_container_width=True,
                    )
            else:
                st.link_button(
                    "대한민국 법원경매정보 원문 열기",
                    detail["상세URL"],
                    use_container_width=True,
                )

    with tab_map:
        map_urls = build_map_urls(detail["소재지"])
        if map_urls["naver"]:
            maps_cfg = cfg.get("maps", {}) or {}
            naver_cfg = maps_cfg.get("naver", {}) or {}
            configured_client_id = str(naver_cfg.get("client_id", "") or "").strip()
            if configured_client_id.startswith("${") and configured_client_id.endswith("}"):
                configured_client_id = ""
            naver_client_id = os.getenv("NAVER_MAP_CLIENT_ID", "").strip() or configured_client_id
            naver_html = build_naver_map_html(detail["소재지"], naver_client_id)
            if naver_html:
                components.html(naver_html, height=460, scrolling=False)
            else:
                st.info("네이버 지도 API Client ID가 설정되지 않아 외부 지도 열기 버튼을 표시합니다.")
                st.markdown(f"**소재지**  \n{detail['소재지']}")
            st.link_button("네이버 지도에서 위치 열기", map_urls["naver"], type="primary", use_container_width=True)
            st.caption("주소검색 결과입니다. 지적 경계·도로접면·실제 진입로는 원문과 현장조사로 재확인하십시오.")
            mc1, mc2 = st.columns(2)
            mc1.link_button("카카오맵으로 보기", map_urls["kakao"], use_container_width=True)
            mc2.link_button("Google 지도로 보기", map_urls["google"], use_container_width=True)
        else:
            st.info("표시할 소재지 정보가 없습니다.")

    with tab_source:
        data = _item_mapping(item)
        raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
        if sale_type == "공매":
            st.caption("공공데이터포털의 온비드 OpenAPI에서 조회한 공매 기본정보와 입찰일정입니다.")
            summary = {
                "공고번호": raw.get("onbidPbancNo") or raw.get("PLNM_NO") or number,
                "공매번호": raw.get("pbctNo") or raw.get("PBCT_NO") or "-",
                "물건번호": raw.get("onbidCltrno") or raw.get("CLTR_NO") or "-",
                "물건관리번호": raw.get("cltrMngNo") or raw.get("CLTR_MNMT_NO") or item_number,
                "처분방식": raw.get("DPSL_MTD_NM") or "매각",
                "입찰방식": raw.get("BID_MTD_NM") or "-",
                "원래 물건상태": raw.get("raw_status") or raw.get("pbctStatNm") or raw.get("PBCT_CLTR_STAT_NM") or "-",
                "입찰주최기관": raw.get("orgNm") or raw.get("rqstOrgNm") or raw.get("ORG_NM") or detail["진행기관"],
            }
            st.dataframe(pd.DataFrame([summary]), use_container_width=True, hide_index=True)
            schedules = raw.get("BID_DATE_ROWS") or []
            if isinstance(schedules, dict):
                schedules = [schedules]
            if schedules:
                rows = []
                for r in schedules:
                    rows.append({
                        "회차": r.get("PBCT_SEQ") or r.get("PBCT_DGR") or "-",
                        "입찰구분": r.get("BID_DVSN_NM") or "-",
                        "입찰시작": r.get("PBCT_BEGN_DTM") or "-",
                        "입찰마감": r.get("PBCT_CLS_DTM") or "-",
                        "개찰일시": r.get("PBCT_EXCT_DTM") or "-",
                        "개찰장소": r.get("OPBD_PLC_CNTN") or "-",
                        "최저입찰가": f"{int(float(r.get('MIN_BID_PRC') or 0)):,}",
                    })
                st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
            else:
                st.info("목록 또는 상세 응답에 입찰일정 세부자료가 없습니다.")
        else:
            st.caption("법원 사건상세 조회는 사용자가 버튼을 누를 때만 실행해 사이트 호출량을 줄입니다.")
            cache_key = f"{number}|{detail['진행기관']}"
            cache = st.session_state.setdefault("case_detail_popup_cache", {})
            button_key = "load_case_" + safe_key(cache_key)
            if st.button("법원 사건 상세정보 불러오기", key=button_key, use_container_width=True):
                provider = None
                try:
                    with st.spinner("법원 사건상세정보를 조회하고 있습니다..."):
                        runtime_cfg = load_config(CONFIG_PATH)
                        provider = build_provider(runtime_cfg, search_target="경매")
                        if not hasattr(provider, "lookup_case"):
                            raise RuntimeError("현재 데이터 공급자는 사건상세 조회를 지원하지 않습니다.")
                        profile = _profile_for_detail(runtime_cfg, detail["검색조건"])
                        cache[cache_key] = provider.lookup_case(number, profile)
                        st.session_state["case_detail_popup_cache"] = cache
                except Exception as exc:
                    cache[cache_key] = {"found": False, "진단": f"사건상세 조회 실패: {exc}"}
                finally:
                    if provider is not None:
                        try:
                            provider.close()
                        except Exception:
                            pass
            _render_case_lookup(cache.get(cache_key, {}), item_number)

    with tab_raw:
        raw_rows = [
            {"항목": key, "내용": value}
            for key, value in detail["원시정보"].items()
            if str(value or "").strip()
        ]
        if raw_rows:
            st.dataframe(pd.DataFrame(raw_rows), use_container_width=True, hide_index=True)
        else:
            st.info("목록 응답에 추가 원시정보가 없습니다.")

    st.divider()
    if standalone:
        c1, c2 = st.columns([1, 1])
        c1.link_button("검색 대시보드 열기", "/", use_container_width=True)
        c2.caption("상세 확인을 마쳤으면 이 브라우저 탭을 닫으십시오.")
    elif st.button("닫기", key="close_detail_" + safe_key(f"{number}_{item_number}"), use_container_width=True):
        _clear_detail_query()
        st.rerun()


if hasattr(st, "dialog"):
    show_item_detail_dialog = st.dialog("매각물건 상세정보", width="large")(_detail_dialog_body)
elif hasattr(st, "experimental_dialog"):
    show_item_detail_dialog = st.experimental_dialog("매각물건 상세정보", width="large")(_detail_dialog_body)
else:
    show_item_detail_dialog = _detail_dialog_body

def result_table(result: RunResult, cfg: dict[str, Any]) -> None:
    m1, m2, m3, m4, m5, m6 = st.columns(6)
    m1.metric("전체 후보", f"{len(result.items):,}건")
    m2.metric("경매", f"{sum(x.sale_type == '경매' for x in result.items):,}건")
    m3.metric("공매", f"{sum(x.sale_type == '공매' for x in result.items):,}건")
    m4.metric("신규", f"{len(result.new_items):,}건")
    m5.metric("변경", f"{len(result.changed_items):,}건")
    m6.metric("우선검토", f"{sum(x.grade == '우선검토' for x in result.items):,}건")

    diagnostics = getattr(result, "diagnostics", []) or []
    if diagnostics:
        st.markdown("##### 검색 진단")
        st.dataframe(pd.DataFrame(diagnostics), use_container_width=True, hide_index=True)
        st.caption(
            "‘원천 수집건수’가 0이면 지역·날짜·입찰방식 단계에서 미수집된 것이고, "
            "수집건수는 있으나 ‘조건 통과건수’가 0이면 아래 제외사유를 확인하십시오. "
            "법원 원본 요청/응답은 `data/selenium_debug/`, 온비드 응답 캐시는 `data/onbid_cache/`에 저장됩니다. "
            "소요시간은 브라우저 준비·요청 대기·서버 응답으로 나누어 표시됩니다."
        )

    show_excluded_items(result)

    if not result.items:
        st.info("현재 저장된 조건에 맞는 물건이 없습니다.")
        return

    raw_df = to_dataframe(result.items, cfg.get("source", {}))
    display_df = format_dataframe_for_report(raw_df)

    st.markdown("##### 투자후보 물건")
    view_mode = st.radio(
        "검색결과 표시방식",
        ["사진 카드형", "상세 표형"],
        horizontal=True,
        key="current_result_view_mode",
        label_visibility="collapsed",
    )
    if view_mode == "사진 카드형":
        detail_links = [
            _detail_query(_detail_identity(item), "current")
            for item in result.items
        ]
        card_html = build_result_cards_html(
            list(result.items),
            detail_links,
            title="경매·공매 투자후보",
        )
        if hasattr(st, "html"):
            st.html(card_html)
        else:
            st.markdown(card_html, unsafe_allow_html=True)
        image_diag = card_image_diagnostics(list(result.items))
        if image_diag["missing"]:
            st.caption(
                f"대표사진 제공 {image_diag['available']:,}건 · 사진 미제공 {image_diag['missing']:,}건. "
                "공매는 API 썸네일을 사용하고, 경매는 공식 사건상세 화면에서 캡처한 대표사진을 캐시해 표시합니다. "
                "법원 원문에도 사진이 없거나 화면구조가 달라 찾지 못한 물건은 자리표시자로 표시됩니다."
            )
        st.caption("카드 전체 또는 사건·공고번호 영역을 클릭하면 새 탭에서 상세정보와 네이버 지도가 열립니다.")
    else:
        st.caption("사건/공고번호를 클릭하면 새 탭에서 물건 상세정보와 지도가 열립니다.")
        _render_case_hyperlink_table(
            display_df,
            source="current",
            key="current_search_result_table",
            height=560,
            detail_items=list(result.items),
        )
    d1, d2, _ = st.columns([1, 1, 3])
    d1.download_button(
        "CSV 내려받기",
        display_df.to_csv(index=False).encode("utf-8-sig"),
        "토지_경매공매_투자후보.csv",
        "text/csv",
        use_container_width=True,
    )
    html_path = Path(result.report_html)
    if html_path.exists():
        d2.download_button(
            "HTML 보고서",
            html_path.read_bytes(),
            html_path.name,
            "text/html",
            use_container_width=True,
        )


def historical_table(cfg: dict[str, Any]) -> None:
    db_path = Path(str(cfg.get("app", {}).get("database_path", "data/landwatch.db")))
    if not db_path.exists():
        st.info("아직 저장된 검색결과가 없습니다.")
        return
    db = Database(str(db_path))
    rows = db.recent_items(300)
    if not rows:
        st.info("아직 저장된 검색결과가 없습니다.")
        return
    historical_rows: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row.get("payload_json") or "{}")
        area = float(row.get("land_area_m2") or 0)
        appraisal = int(row.get("appraisal_price") or 0)
        minimum = int(row.get("min_price") or 0)
        discount = (1 - minimum / appraisal) * 100 if appraisal > 0 else 0
        historical_rows.append({
            "검색조건": row.get("profile_name", ""),
            "검토등급": row.get("grade", ""),
            "투자검토점수": row.get("score", 0),
            "매각구분": payload.get("sale_type") or "경매",
            "데이터 출처": payload.get("source_name") or "",
            "진행기관": payload.get("court") or "",
            "사건/공고번호": payload.get("case_number") or "-",
            "물건번호/물건관리번호": payload.get("item_number") or "-",
            "진행상태": row.get("status", ""),
            "물건용도": row.get("usage", ""),
            "소재지": row.get("address", ""),
            "최저매각가격/최저입찰가": minimum,
            "감정평가액": appraisal,
            "감정평가액 대비 할인율(%)": round(discount, 1),
            "유찰횟수": row.get("failed_count", 0),
            "토지면적(㎡)": area,
            "토지면적(평)": sqm_to_pyeong(area),
            "매각기일/입찰마감일": row.get("auction_date", ""),
            "원문 URL": row.get("detail_url", ""),
        })
    display = format_dataframe_for_report(pd.DataFrame(historical_rows))
    history_payloads = _history_detail_items(cfg)
    st.caption("사건/공고번호를 클릭하면 새 탭에서 저장된 물건의 상세정보와 지도가 열립니다.")
    _render_case_hyperlink_table(
        display,
        source="history",
        key="historical_result_table",
        height=520,
        detail_items=history_payloads,
    )


def recent_runs(cfg: dict[str, Any]) -> None:
    db_path = Path(str(cfg.get("app", {}).get("database_path", "data/landwatch.db")))
    if not db_path.exists():
        return
    db = Database(str(db_path))
    runs = db.recent_runs(20)
    if not runs:
        return
    labels = {
        "success": "성공",
        "error": "오류",
        "running": "실행 중",
    }
    df = pd.DataFrame([
        {
            "시작일시": r.get("started_at", ""),
            "종료일시": r.get("finished_at", ""),
            "실행결과": labels.get(r.get("status"), r.get("status")),
            "투자후보": r.get("found_count", 0),
            "신규": r.get("new_count", 0),
            "변경": r.get("changed_count", 0),
            "법원요청": r.get("court_request_count", 0),
            "공매요청": r.get("public_sale_request_count", 0),
            "캐시": r.get("cache_hit_count", 0),
            "요청대기(초)": r.get("throttle_wait_seconds", 0),
            "서버응답(초)": r.get("server_response_seconds", 0),
            "브라우저준비(초)": r.get("browser_warmup_seconds", 0),
            "상세·사진(초)": r.get("detail_photo_seconds", 0),
            "사진 신규/실패": (
                f"{int(r.get('court_photo_new_count', 0) or 0)}/"
                f"{int(r.get('court_photo_failure_count', 0) or 0)}"
            ),
            "가격상세 교정/생략": (
                f"{int(r.get('court_price_detail_success_count', 0) or 0)}/"
                f"{int(r.get('court_price_detail_skipped_count', 0) or 0)}"
            ),
            "메시지": r.get("message", ""),
        }
        for r in runs
    ])
    st.dataframe(df, use_container_width=True, hide_index=True)


cfg = load_ui_config()
show_flash()

if _query_param("lw_detail_page") == "1":
    _render_linked_detail_page(cfg)
    st.stop()

st.title("🏞️ 토지 경매·공매 투자후보 모니터")
st.sidebar.caption(f"실행 설정파일: `{CONFIG_PATH}`")
active_regions = []
for _p in cfg.get("profiles", []):
    if _p.get("enabled", True):
        active_regions.extend(_p.get("regions", []) or ["전국"])
st.sidebar.info("현재 실행지역: " + ", ".join(dict.fromkeys(active_regions or ["전국"])))
st.sidebar.info("기본 검색대상: " + str((cfg.get("app", {}) or {}).get("search_target", "경매")))
st.caption(
    "대한민국 법원경매정보와 한국자산관리공사 온비드에서 토지 물건을 조회하고, "
    "저장한 검색조건에 따라 투자후보를 선별합니다. 설정은 대시보드에서 선택합니다."
)

profiles = [normalize_profile(p) for p in cfg.get("profiles", [])]
cfg["profiles"] = profiles

_dispatch_linked_detail(cfg)

tab_dashboard, tab_profile, tab_results, tab_system, tab_guide = st.tabs([
    "🔎 검색·대시보드",
    "⚙️ 검색조건 관리",
    "📋 저장된 결과",
    "🛠 수집·알림 설정",
    "ℹ️ 이용안내",
])

with tab_dashboard:
    st.subheader("토지 경매·공매 물건 검색")
    current_target = str((cfg.get("app", {}) or {}).get("search_target", "경매"))
    if current_target not in SEARCH_TARGET_OPTIONS:
        current_target = "경매"
    search_target = st.radio(
        "검색대상",
        SEARCH_TARGET_OPTIONS,
        index=SEARCH_TARGET_OPTIONS.index(current_target),
        horizontal=True,
        help="경매는 대한민국 법원경매정보, 공매는 한국자산관리공사 온비드 OpenAPI를 조회합니다.",
        key="search_target_selector",
    )
    if search_target != current_target:
        cfg.setdefault("app", {})["search_target"] = search_target
        persist(cfg)
    if search_target in {"공매", "경매 및 공매"}:
        onbid_cfg = (cfg.get("source", {}) or {}).get("onbid_openapi", {}) or {}
        configured_key = str(onbid_cfg.get("service_key", "") or "")
        has_runtime_key = bool(os.getenv("KAMCO_API_KEY")) or (configured_key and not configured_key.startswith("${"))
        if not has_runtime_key:
            st.warning(
                "공매 검색에는 공공데이터포털의 ‘한국자산관리공사_차세대 온비드 부동산 물건목록 조회서비스’ 활용승인 인증키가 필요합니다. "
                "‘수집·알림 설정 → 온비드 공매 설정’에서 입력하십시오."
            )
    enabled_profiles = [p for p in profiles if p.get("enabled", True)]
    enabled_names = [p["name"] for p in enabled_profiles]
    if not enabled_names:
        st.warning("활성화된 검색조건이 없습니다. ‘검색조건 관리’에서 검색조건을 활성화하십시오.")
    selected_names = st.multiselect(
        "실행할 검색조건",
        options=enabled_names,
        default=enabled_names,
        help="여러 검색조건을 한 번에 실행할 수 있습니다.",
    )
    if selected_names:
        for p in enabled_profiles:
            if p["name"] in selected_names:
                st.markdown(
                    f"<div class='profile-summary'><b>{p['name']}</b><br>{profile_summary(p)}</div>",
                    unsafe_allow_html=True,
                )
                for warning in profile_consistency_warnings(p):
                    st.warning(warning)

    c1, c2, c3 = st.columns([1, 1, 3])
    notify_now = c1.checkbox("신규·변경 알림 발송", value=False)
    check_clicked = c1.button("연결 점검", use_container_width=True)
    force_refresh = c3.checkbox(
        "검색 캐시를 무시하고 새로 조회",
        value=False,
        help="같은 조건을 다시 실행할 때도 법원 사이트에서 최신 데이터를 다시 가져옵니다.",
    )
    run_clicked = c2.button(
        "지금 검색 실행",
        type="primary",
        use_container_width=True,
        disabled=not selected_names,
    )

    if search_target != "공매":
        with st.expander("특정 법원경매 사건번호 직접 확인", expanded=False):
          st.caption(
              "조건검색에 보이지 않는 사건을 법원 사건검색으로 직접 확인합니다. "
              "사건번호는 법원별로 중복될 수 있어 선택지역 관련 법원을 우선 조회합니다."
          )
          dc1, dc2 = st.columns([3, 1])
          direct_case_number = dc1.text_input(
              "사건번호", value="2025타경385", placeholder="예: 2025타경385",
              key="direct_case_number",
          )
          direct_case_clicked = dc2.button("사건 직접 확인", use_container_width=True)
          if direct_case_clicked:
              if not selected_names:
                  st.warning("사건 확인에 사용할 검색조건을 하나 이상 선택하십시오.")
              else:
                  with st.spinner("법원 사건검색에서 직접 확인하고 있습니다..."):
                      provider = None
                      try:
                          runtime_cfg = load_config(CONFIG_PATH)
                          provider = build_provider(runtime_cfg, search_target="경매")
                          selected_profile = next(
                              p for p in runtime_cfg["profiles"] if p["name"] == selected_names[0]
                          )
                          if not hasattr(provider, "lookup_case"):
                              raise RuntimeError("현재 데이터 공급자는 사건번호 직접 확인을 지원하지 않습니다.")
                          case_result = provider.lookup_case(direct_case_number, selected_profile)
                          if case_result.get("found"):
                              st.success(
                                  f"사건 확인: {case_result.get('법원', '')} "
                                  f"{case_result.get('사건번호', direct_case_number)}"
                              )
                              st.info(case_result.get("진단", ""))
                              cmeta = {
                                  "사건명": case_result.get("사건명", ""),
                                  "진행상태코드": case_result.get("진행상태코드", ""),
                                  "최종처분코드": case_result.get("최종처분코드", ""),
                                  "다음매각기일": case_result.get("다음매각기일", ""),
                              }
                              st.json(cmeta)
                              if case_result.get("물건내역"):
                                  st.markdown("**물건내역**")
                                  st.dataframe(pd.DataFrame(case_result["물건내역"]), use_container_width=True, hide_index=True)
                              if case_result.get("매각기일내역"):
                                  st.markdown("**매각기일내역**")
                                  schedule_df = pd.DataFrame(case_result["매각기일내역"])
                                  for col in ("감정평가액", "최저매각가격"):
                                      if col in schedule_df.columns:
                                          schedule_df[col] = schedule_df[col].map(lambda x: f"{int(x or 0):,}")
                                  st.dataframe(schedule_df, use_container_width=True, hide_index=True)
                              else:
                                  st.warning(
                                      "법원 사건은 확인됐지만 매각기일 데이터가 없습니다. "
                                      "기일 미지정·연기·변경 상태이거나 법원 응답 구조가 달라졌을 수 있습니다."
                                  )
                          else:
                              st.warning(case_result.get("진단", "사건을 찾지 못했습니다."))
                          attempts = case_result.get("조회시도", []) or []
                          if attempts:
                              with st.expander("법원별 조회시도"):
                                  st.dataframe(pd.DataFrame(attempts), use_container_width=True, hide_index=True)
                      except Exception as exc:
                          st.error(f"사건 직접 확인 실패: {exc}")
                      finally:
                          if provider is not None:
                              try:
                                  provider.close()
                              except Exception:
                                  pass

    if check_clicked:
        if not selected_names:
            st.warning("연결 점검에 사용할 검색조건을 하나 이상 선택하십시오.")
        else:
            with st.spinner(f"{search_target} 데이터 연결을 점검하고 있습니다..."):
                provider = None
                try:
                    runtime_cfg = load_config(CONFIG_PATH)
                    provider = build_provider(runtime_cfg, search_target=search_target)
                    selected_profile = next(p for p in runtime_cfg["profiles"] if p["name"] == selected_names[0])
                    result = provider.test_connection(selected_profile)
                    if result.get("source") == "경매 및 공매":
                        for source_result in result.get("results", []):
                            st.success(
                                f"{source_result.get('source', '데이터')} 연결 성공 · "
                                f"조회지역 {source_result.get('queried_region', '전국')} · "
                                f"첫 페이지 {source_result.get('first_page_count', 0):,}건 · "
                                f"전체 {source_result.get('total_count', 0):,}건"
                            )
                        result = (result.get("results") or [{}])[0]
                    code_type = result.get("code_type", "")
                    code_text = "/".join(
                        str(x) for x in (
                            result.get("region_codes", {}).get("sido", ""),
                            result.get("region_codes", {}).get("sigungu", ""),
                        ) if x
                    )
                    code_note = f" · {code_type} 코드 {code_text}" if code_text else ""
                    st.success(
                        f"연결 성공 · 조회지역 {result.get('queried_region', '전국')}{code_note} · "
                        f"첫 페이지 {result.get('first_page_count', 0):,}건 · "
                        f"전체 {result.get('total_count', 0):,}건"
                    )
                    attempts = result.get("attempts", []) or []
                    if len(attempts) > 1:
                        st.caption(
                            "현행 지역코드가 0건이어서 과거 행정구역 코드로 자동 재조회했습니다."
                        )
                    if result.get("first_case_number"):
                        st.caption(f"확인된 첫 사건/공고번호: {result['first_case_number']}")
                except Exception as exc:
                    st.error(f"연결 점검 실패: {exc}")
                finally:
                    if provider is not None:
                        try:
                            provider.close()
                        except Exception:
                            pass

    if run_clicked:
        with st.spinner(f"{search_target} 물건을 수집하고 투자후보를 평가하고 있습니다..."):
            try:
                result = run_once(
                    str(CONFIG_PATH), notify=notify_now,
                    profile_names=selected_names, force_refresh=force_refresh,
                    search_target=search_target,
                )
                st.session_state["last_result"] = result
                st.success(
                    f"검색 완료: 투자후보 {len(result.items):,}건, "
                    f"신규 {len(result.new_items):,}건, 변경 {len(result.changed_items):,}건"
                )
            except Exception as exc:
                st.error(f"검색 실행 중 오류가 발생했습니다: {exc}")
                debug_dir = cfg.get("source", {}).get("court_selenium", {}).get("debug_dir", "data/selenium_debug")
                st.caption(
                    f"법원경매 오류자료는 `{debug_dir}`, 공매 캐시는 `data/onbid_cache/`에서 확인할 수 있습니다."
                )

    last_result = st.session_state.get("last_result")
    if last_result:
        st.divider()
        result_table(last_result, cfg)
    else:
        st.info("‘지금 검색 실행’을 누르면 이 화면에 투자후보가 표시됩니다.")

with tab_profile:
    st.subheader("검색조건 관리")
    st.caption("경매·공매에 공통 적용할 지역·진행상태·물건용도·가격·유찰횟수·면적을 화면에서 선택하고 저장합니다.")

    names = [p["name"] for p in profiles]
    pending_profile = st.session_state.pop("pending_profile_name", None)
    if pending_profile in names:
        st.session_state["profile_selector"] = pending_profile
    elif st.session_state.get("profile_selector") not in names:
        st.session_state["profile_selector"] = names[0]
    selected_name = st.selectbox("편집할 검색조건", names, key="profile_selector")
    profile_index = names.index(selected_name)
    profile = normalize_profile(profiles[profile_index])
    prefix = f"profile_{profile_index}_{safe_key(selected_name)}"

    action1, action2, action3, _ = st.columns([1, 1, 1, 3])
    if action1.button("새 검색조건", use_container_width=True):
        fresh = new_profile(names)
        cfg["profiles"].append(fresh)
        persist(cfg)
        st.session_state["pending_profile_name"] = fresh["name"]
        st.session_state["flash_message"] = ("success", f"‘{fresh['name']}’을 만들었습니다.")
        st.rerun()
    if action2.button("복제", use_container_width=True):
        copied = duplicate_profile(profile, names)
        cfg["profiles"].append(copied)
        persist(cfg)
        st.session_state["pending_profile_name"] = copied["name"]
        st.session_state["flash_message"] = ("success", f"‘{copied['name']}’을 만들었습니다.")
        st.rerun()
    if action3.button("삭제", use_container_width=True, disabled=len(profiles) <= 1):
        deleted = cfg["profiles"].pop(profile_index)
        persist(cfg)
        remaining_names = [p.get("name", "") for p in cfg.get("profiles", [])]
        if remaining_names:
            st.session_state["pending_profile_name"] = remaining_names[min(profile_index, len(remaining_names) - 1)]
        st.session_state["flash_message"] = ("success", f"‘{deleted.get('name', '')}’을 삭제했습니다.")
        st.rerun()

    st.markdown("#### 1. 기본 정보")
    b1, b2 = st.columns([3, 1])
    profile_name = b1.text_input("검색조건 이름", value=profile["name"], key=f"{prefix}_name")
    enabled = b2.checkbox("자동검색에 사용", value=bool(profile.get("enabled", True)), key=f"{prefix}_enabled")

    st.markdown("#### 2. 지역")
    existing_provinces, existing_municipalities = region_defaults_from_profile(profile)
    profile_regions = profile.get("regions") or []
    if not profile_regions:
        default_region_mode = "전국"
    elif existing_municipalities:
        default_region_mode = "시·군·구 선택"
    else:
        default_region_mode = "시·도 전체"
    region_mode = st.radio(
        "지역 선택방식",
        ["전국", "시·도 전체", "시·군·구 선택"],
        index=["전국", "시·도 전체", "시·군·구 선택"].index(default_region_mode),
        horizontal=True,
        key=f"{prefix}_region_mode",
    )
    selected_provinces: list[str] = []
    selected_regions: list[str] = []
    if region_mode == "시·도 전체":
        province_defaults = [x for x in profile_regions if x in province_names()] or existing_provinces
        selected_provinces = st.multiselect(
            "시·도",
            province_names(),
            default=[x for x in province_defaults if x in province_names()],
            key=f"{prefix}_province_all",
        )
        selected_regions = selected_provinces
    elif region_mode == "시·군·구 선택":
        selected_provinces = st.multiselect(
            "시·도",
            province_names(),
            default=[x for x in existing_provinces if x in province_names()],
            key=f"{prefix}_provinces",
        )
        region_options = municipality_names(selected_provinces)
        municipality_key = f"{prefix}_municipalities"
        if municipality_key in st.session_state:
            st.session_state[municipality_key] = [
                x for x in st.session_state[municipality_key] if x in region_options
            ]
        selected_regions = st.multiselect(
            "시·군·구",
            region_options,
            default=[x for x in existing_municipalities if x in region_options],
            key=municipality_key,
            placeholder="시·도 선택 후 시·군·구를 선택하십시오",
        )
    else:
        st.caption("전국 물건을 조회한 뒤 아래 조건으로 선별합니다.")

    st.markdown("#### 3. 물건 조건")
    c1, c2 = st.columns(2)
    statuses = c1.multiselect(
        "진행상태",
        STATUS_OPTIONS,
        default=[x for x in profile.get("statuses", []) if x in STATUS_OPTIONS],
        key=f"{prefix}_statuses",
        help="신건, 유찰, 재매각, 수의계약 등 경매·공매 진행상태를 선택합니다.",
    )
    usages = c2.multiselect(
        "물건용도(지목)",
        LAND_USE_OPTIONS,
        default=[x for x in profile.get("usages", []) if x in LAND_USE_OPTIONS],
        key=f"{prefix}_usages",
        help="전·답·과수원·임야·대 등 토지의 지목을 선택합니다.",
    )

    fmin = int((profile.get("failed_count") or {}).get("min") or 0)
    fmax = int((profile.get("failed_count") or {}).get("max") or 10)
    failed_range = st.slider(
        "유찰횟수",
        min_value=0,
        max_value=10,
        value=(max(0, min(fmin, 10)), max(0, min(fmax, 10))),
        key=f"{prefix}_failed",
    )

    p1, p2 = st.columns(2)
    price_min = p1.number_input(
        "최저매각가격 최소(만원)", min_value=0, step=100,
        value=won_to_manwon((profile.get("min_price") or {}).get("min")),
        key=f"{prefix}_price_min",
    )
    price_max = p2.number_input(
        "최저매각가격 최대(만원)", min_value=0, step=100,
        value=won_to_manwon((profile.get("min_price") or {}).get("max")),
        key=f"{prefix}_price_max",
    )

    appraisal_spec = profile.get("appraisal_price") or {}
    use_appraisal = st.checkbox(
        "감정평가액 범위도 지정",
        value=bool((appraisal_spec.get("min") or 0) or (appraisal_spec.get("max") or 0)),
        key=f"{prefix}_use_appraisal",
    )
    a1, a2 = st.columns(2)
    appraisal_min = a1.number_input(
        "감정평가액 최소(만원)", min_value=0, step=100,
        value=won_to_manwon(appraisal_spec.get("min")), disabled=not use_appraisal,
        key=f"{prefix}_appraisal_min",
    )
    appraisal_max = a2.number_input(
        "감정평가액 최대(만원)", min_value=0, step=100,
        value=won_to_manwon(appraisal_spec.get("max")), disabled=not use_appraisal,
        key=f"{prefix}_appraisal_max",
    )

    area_spec = profile.get("land_area_m2") or {}
    ar1, ar2 = st.columns(2)
    area_min = ar1.number_input(
        "토지면적 최소(㎡)", min_value=0.0, step=33.0,
        value=float(area_spec.get("min") or 0), key=f"{prefix}_area_min",
    )
    area_max = ar2.number_input(
        "토지면적 최대(㎡)", min_value=0.0, step=33.0,
        value=float(area_spec.get("max") or 0), key=f"{prefix}_area_max",
    )
    st.caption(f"평 환산: 약 {sqm_to_pyeong(area_min):,.1f}평 ~ {sqm_to_pyeong(area_max):,.1f}평")

    discount_spec = profile.get("appraisal_discount_percent") or {}
    dmin = int(discount_spec.get("min") or 0)
    dmax = int(discount_spec.get("max") or 100)
    discount_range = st.slider(
        "감정평가액 대비 할인율(%)",
        min_value=0,
        max_value=100,
        value=(max(0, min(dmin, 100)), max(0, min(dmax, 100))),
        key=f"{prefix}_discount",
        help="예: 할인율 40%는 최저매각가격이 감정평가액의 60%라는 뜻입니다.",
    )

    auction_within_days = st.number_input(
        "매각기일 조회범위(오늘부터 며칠 이내)",
        min_value=1,
        max_value=365,
        value=int(profile.get("auction_within_days") or 90),
        step=1,
        key=f"{prefix}_days",
    )
    chunk_days = int((cfg.get("source", {}).get("court_selenium", {}) or {}).get("sale_window_days", 13)) + 1
    estimated_windows = max(1, (int(auction_within_days) + chunk_days - 1) // chunk_days)
    st.caption(
        f"경매는 선택한 {int(auction_within_days)}일 전체를 약 {chunk_days}일 단위 "
        f"{estimated_windows}개 구간으로 나누어 조회하며, 공매는 같은 기간을 온비드 API에 직접 전달합니다."
    )

    st.markdown("#### 4. 키워드·특수조건")
    include_text = st.text_input(
        "반드시 포함할 키워드(선택)",
        value=", ".join(profile.get("include_keywords", [])),
        key=f"{prefix}_include",
        placeholder="예: 계획관리, 도로접면",
    )
    selected_risk_labels = st.multiselect(
        "자동 제외할 특수조건",
        list(RISK_OPTION_MAP),
        default=exclusion_labels(profile.get("exclude_keywords", [])),
        key=f"{prefix}_risks",
    )
    custom_exclude_text = st.text_input(
        "추가 제외 키워드(선택)",
        value=", ".join(custom_exclusion_keywords(profile.get("exclude_keywords", []))),
        key=f"{prefix}_custom_exclude",
        placeholder="쉼표로 구분",
    )

    with st.expander("투자검토점수 설정", expanded=False):
        stored_preset = str(profile.get("scoring_preset") or "균형형")
        preset_options = [*SCORING_PRESETS, "직접 설정"]
        if stored_preset not in preset_options:
            stored_preset = "직접 설정"
        scoring_preset = st.selectbox(
            "평가방식",
            preset_options,
            index=preset_options.index(stored_preset),
            key=f"{prefix}_preset",
        )
        existing_scoring = profile.get("scoring", {}) or {}
        if scoring_preset == "직접 설정":
            labels = [
                ("discount", "감정평가액 대비 할인"),
                ("budget_fit", "희망 예산 적합도"),
                ("area_fit", "희망 면적 적합도"),
                ("failed_count", "유찰횟수"),
                ("usage_preference", "선호 물건용도"),
                ("market_gap", "추정시세 대비 가격차이"),
                ("data_quality", "데이터 완성도"),
            ]
            scoring: dict[str, int] = {}
            weight_cols = st.columns(2)
            for i, (key, label) in enumerate(labels):
                scoring[key] = weight_cols[i % 2].number_input(
                    f"{label} 배점", min_value=0, max_value=100,
                    value=int(existing_scoring.get(key, DEFAULT_PROFILE["scoring"].get(key, 0))),
                    key=f"{prefix}_weight_{key}",
                )
            total_weight = sum(scoring.values())
            if total_weight != 100:
                st.warning(f"배점 합계가 {total_weight}점입니다. 비교하기 쉽도록 100점으로 맞추는 것을 권장합니다.")
        else:
            scoring = copy.deepcopy(SCORING_PRESETS[scoring_preset])
            st.json({
                "감정평가액 대비 할인": scoring["discount"],
                "희망 예산 적합도": scoring["budget_fit"],
                "희망 면적 적합도": scoring["area_fit"],
                "유찰횟수": scoring["failed_count"],
                "선호 물건용도": scoring["usage_preference"],
                "추정시세 대비 가격차이": scoring["market_gap"],
                "데이터 완성도": scoring["data_quality"],
            })
        preferred_default = [
            x for x, weight in (profile.get("preferred_usages") or {}).items()
            if float(weight or 0) >= 8 and x in usages
        ]
        preferred_key = f"{prefix}_preferred_usages"
        if preferred_key in st.session_state:
            st.session_state[preferred_key] = [x for x in st.session_state[preferred_key] if x in usages]
        preferred_usages = st.multiselect(
            "우선 선호할 물건용도",
            usages,
            default=preferred_default,
            key=preferred_key,
        )

    updated_profile = normalize_profile(profile)
    updated_profile.update({
        "name": profile_name.strip(),
        "enabled": enabled,
        "regions": [] if region_mode == "전국" else selected_regions,
        "statuses": statuses,
        "usages": usages,
        "failed_count": {"min": failed_range[0], "max": failed_range[1]},
        "min_price": {"min": manwon_to_won(price_min), "max": manwon_to_won(price_max)},
        "appraisal_price": {
            "min": manwon_to_won(appraisal_min) if use_appraisal else 0,
            "max": manwon_to_won(appraisal_max) if use_appraisal else 0,
        },
        "land_area_m2": {"min": area_min, "max": area_max},
        "appraisal_discount_percent": {"min": discount_range[0], "max": discount_range[1]},
        "auction_within_days": int(auction_within_days),
        "include_keywords": parse_keywords(include_text),
        "exclude_keywords": exclusion_keywords(selected_risk_labels, custom_exclude_text),
        "scoring_preset": scoring_preset,
        "scoring": scoring,
        "preferred_usages": {
            usage: (10 if usage in preferred_usages else 5) for usage in usages
        },
    })

    save_col, run_col, _ = st.columns([1, 1.3, 3])
    save_clicked = save_col.button("검색조건 저장", type="primary", use_container_width=True)
    save_and_run_clicked = run_col.button("저장 후 이 조건으로 검색", use_container_width=True)
    if save_clicked or save_and_run_clicked:
        errors = validate_profile(updated_profile, profiles, profile_index)
        if region_mode != "전국" and not selected_regions:
            errors.append("선택한 지역방식에 맞는 시·도 또는 시·군·구를 하나 이상 선택하십시오.")
        if errors:
            for error in errors:
                st.error(error)
        else:
            cfg["profiles"][profile_index] = updated_profile
            persist(cfg)
            if save_and_run_clicked:
                with st.spinner("저장한 검색조건으로 경매·공매 물건을 조회하고 있습니다..."):
                    try:
                        result = run_once(
                            str(CONFIG_PATH), notify=False,
                            profile_names=[updated_profile["name"]],
                            search_target=str((cfg.get("app", {}) or {}).get("search_target", "경매")),
                        )
                        st.session_state["last_result"] = result
                        st.success(f"저장 및 검색 완료: 투자후보 {len(result.items):,}건")
                        result_table(result, load_ui_config())
                    except Exception as exc:
                        st.error(f"검색 실행 중 오류가 발생했습니다: {exc}")
            else:
                st.session_state["pending_profile_name"] = updated_profile["name"]
                st.session_state["flash_message"] = (
                    "success", f"검색조건을 저장했습니다. 실행 파일: {CONFIG_PATH}"
                )
                st.rerun()

with tab_results:
    st.subheader("저장된 경매·공매 투자후보")
    historical_table(cfg)
    st.divider()
    st.subheader("최근 실행 내역")
    recent_runs(cfg)

with tab_system:
    st.subheader("수집·운영 설정")
    source_cfg = cfg.setdefault("source", {})
    selenium_cfg = source_cfg.setdefault("court_selenium", {})

    s1, s2, s3 = st.columns(3)
    hide_browser = s1.checkbox(
        "브라우저 창 숨기기(백그라운드 실행)",
        value=bool(selenium_cfg.get("headless", True)),
    )
    page_size_options = [10, 20]
    current_page_size = int(selenium_cfg.get("page_size", 20))
    if current_page_size not in page_size_options:
        current_page_size = 20
    page_size = s2.selectbox(
        "페이지당 조회건수",
        page_size_options,
        index=page_size_options.index(current_page_size),
        help="20건을 권장합니다. 법원 사이트는 50·100건 요청을 HTTP 400으로 거부할 수 있습니다.",
    )
    top_n = s3.number_input(
        "검색조건별 보고서 최대 건수",
        min_value=1, max_value=200,
        value=int(cfg.get("app", {}).get("top_n_per_profile", 20)),
    )

    s4, s5, s6 = st.columns(3)
    max_pages = s4.number_input(
        "검색조건별 최대 페이지",
        min_value=1, max_value=30,
        value=int(selenium_cfg.get("max_pages", 8)),
        help=(
            "일반적인 페이지 한도입니다. 시·군·구 직접검색에서 대형 일괄매각 목적물이 "
            "이 한도를 채우면 서버 전체건수에 맞춰 최대 30페이지까지 자동 확장합니다."
        ),
    )
    max_calls = s5.number_input(
        "1회 실행 기본 요청횟수",
        min_value=1, max_value=50,
        value=int(selenium_cfg.get("max_calls_per_run", 10)),
        help=(
            "90일·특별자치도처럼 여러 날짜구간과 신·구 지역코드 조회가 필요한 경우에는 "
            "프로그램이 필요한 1페이지 기준 횟수만큼 자동 확장하되, 30회를 넘지 않습니다."
        ),
    )
    sale_window = s6.number_input(
        "법원 1회 요청 날짜폭(추가일수)",
        min_value=1, max_value=13,
        value=min(13, int(selenium_cfg.get("sale_window_days", 13))),
        help="13은 시작일을 포함해 14일 범위를 뜻합니다. 전체 조회기간은 검색조건에서 별도로 지정합니다.",
    )

    s7, s8 = st.columns(2)
    min_delay = s7.number_input(
        "요청 간 최소 대기시간(초)",
        min_value=2.0, max_value=30.0, step=0.5,
        value=float(selenium_cfg.get("min_delay_seconds", 3.0)),
    )
    jitter = s8.number_input(
        "추가 무작위 대기시간(초)",
        min_value=0.0, max_value=10.0, step=0.5,
        value=float(selenium_cfg.get("jitter_seconds", 1.5)),
    )
    st.caption(
        "법원 지역코드 자체점검: "
        f"시·도 {REGION_CODE_AUDIT['province_count']}개 / "
        f"시·군·구 {REGION_CODE_AUDIT['municipality_count']}개 / "
        f"고유 5자리 기준코드 {REGION_CODE_AUDIT['unique_sigungu_code_count']}개 / "
        f"법원 요청 {REGION_CODE_AUDIT['request_sigungu_length']}자리 / "
        f"오류 {len(REGION_CODE_AUDIT['errors'])}건"
    )
    st.info(
        "지역을 선택한 검색조건은 법원 서버에 해당 시·도/시·군·구 코드를 자동 적용합니다. "
        "전국 결과 일부를 받은 뒤 지역을 거르는 방식은 사용하지 않습니다."
    )
    server_region = True

    st.markdown("##### 검색속도 최적화")
    mode_labels = ["빠른 검색", "누락 최소화 검색"]
    current_search_mode = str(selenium_cfg.get("search_mode", "fast") or "fast")
    if current_search_mode not in {"fast", "complete"}:
        current_search_mode = "fast"
    court_search_mode_label = st.radio(
        "법원경매 검색 모드",
        mode_labels,
        index=0 if current_search_mode == "fast" else 1,
        horizontal=True,
        help=(
            "빠른 검색은 가격·면적·유찰·용도 조건을 법원 서버에 먼저 보내 호출량을 줄입니다. "
            "누락 최소화 검색은 조건 일부를 로컬 필터로 미루어 더 넓게 조회합니다."
        ),
    )
    o1, o2, o3 = st.columns(3)
    adaptive_warmup = o1.checkbox(
        "브라우저 준비시간 자동 단축",
        value=bool(selenium_cfg.get("adaptive_warmup", True)),
        help="페이지가 준비되면 고정 6초를 모두 기다리지 않고 바로 검색을 시작합니다.",
    )
    legacy_fallback_only = o2.checkbox(
        "과거 지역코드는 결과가 없을 때만 조회",
        value=bool(selenium_cfg.get("legacy_code_fallback_only", True)),
        help=(
            "강원·전북 특별자치도에서 현행 코드로 대상지역 물건이 확인되면 "
            "과거 도 코드는 생략합니다. 결과가 0건일 때만 호환 조회합니다."
        ),
    )
    cache_enabled = o3.checkbox(
        "동일조건 검색 캐시 사용",
        value=bool(selenium_cfg.get("cache_enabled", True)),
        help="같은 조건을 짧은 시간 안에 다시 실행하면 법원 재접속 없이 저장된 응답을 사용합니다.",
    )
    cache_ttl = st.number_input(
        "검색 캐시 유지시간(분)",
        min_value=1, max_value=120,
        value=max(1, int(selenium_cfg.get("cache_ttl_minutes", 15) or 15)),
        disabled=not cache_enabled,
        help="경매정보의 신선도와 반복검색 속도를 함께 고려하면 10~20분을 권장합니다.",
    )
    f1, f2 = st.columns(2)
    court_fast_mode = f1.checkbox(
        "법원경매 고속 상세조회",
        value=bool(selenium_cfg.get("fast_mode", True)),
        help=(
            "목록에 다음 회차 가격과 매각기일이 이미 있으면 사건상세 가격조회를 생략합니다. "
            "값이 없거나 교정 가능성이 큰 물건만 제한적으로 확인합니다."
        ),
    )
    price_detail_max = f2.number_input(
        "가격 상세교정 신규조회 상한",
        min_value=0, max_value=100,
        value=max(0, int(selenium_cfg.get("price_detail_max_per_run", 6) or 0)),
        disabled=not court_fast_mode,
        help="고속모드에서 목록가격의 추가 확인이 필요한 물건만 이 수량까지 사건상세를 조회합니다.",
    )
    st.caption(
        f"{max(1, int(profiles[0].get('auction_within_days', 14) if profiles else 14))}일 검색은 "
        "14일 단위로 나누어 조회합니다. 목록에 다음 회차 가격이 있으면 상위 후보별 사건상세 "
        "재조회를 생략하고, 사진은 기존 캐시를 모두 사용하면서 신규 수집만 설정한 상한만큼 누적합니다."
    )
    if st.button("저장된 검색 캐시 비우기", disabled=not cache_enabled):
        cache_path = PROJECT_ROOT / str(selenium_cfg.get("cache_dir", "data/selenium_cache"))
        shutil.rmtree(cache_path, ignore_errors=True)
        st.success("검색 캐시를 비웠습니다. 다음 검색은 법원에서 새로 조회합니다.")

    st.markdown("##### 법원 경매 대표사진")
    ph1, ph2, ph3 = st.columns(3)
    court_photo_enabled = ph1.checkbox(
        "법원 상세화면에서 대표사진 자동수집",
        value=bool(selenium_cfg.get("photo_enabled", True)),
        help=(
            "법원 목록 JSON에는 사진이 없으므로 최종 표시할 상위 후보만 공식 사건검색 화면에서 "
            "물건사진을 찾아 로컬에 저장합니다."
        ),
    )
    court_photo_max = ph2.number_input(
        "1회 실행 사진 신규수집 상한",
        min_value=0, max_value=100,
        value=max(0, int(selenium_cfg.get("photo_max_per_run", 6) or 0)),
        disabled=not court_photo_enabled,
        help="이미 캐시에 있는 사진은 이 횟수에 포함되지 않습니다.",
    )
    court_photo_cache_days = ph3.number_input(
        "사진 캐시 유지일수",
        min_value=0, max_value=3650,
        value=max(0, int(selenium_cfg.get("photo_cache_days", 30) or 0)),
        disabled=not court_photo_enabled,
        help="0일은 기존 사진을 기간 제한 없이 사용합니다.",
    )
    court_photo_map_fallback = False
    st.caption(
        "법원 조건검색·사건조회 JSON에는 물건사진 URL이 포함되지 않습니다. 사건 상세화면의 "
        "현황사진·물건사진 탭에서 사진을 찾고, 현황사진이 없으면 대표 이미지는 비워 둡니다. "
        "소재지 위치지도와 감정평가서·현황조사서 등 법원문서는 대표 이미지로 사용하지 않습니다. "
        "이후 검색에서는 `data/court_photo_cache/`의 현황사진만 즉시 재사용합니다."
    )
    if st.button("저장된 법원 대표사진 캐시 비우기", disabled=not court_photo_enabled):
        photo_cache_path = PROJECT_ROOT / str(selenium_cfg.get("photo_cache_dir", "data/court_photo_cache"))
        shutil.rmtree(photo_cache_path, ignore_errors=True)
        st.success("법원 대표사진 캐시를 비웠습니다. 다음 검색에서 사진을 다시 수집합니다.")

    st.divider()
    st.markdown("##### 온비드 공매 설정")
    onbid_cfg = source_cfg.setdefault("onbid_openapi", {})
    stored_onbid_key = str(onbid_cfg.get("service_key", "") or "")
    if stored_onbid_key.startswith("${") and stored_onbid_key.endswith("}"):
        stored_onbid_key = ""
    onbid_service_key = st.text_input(
        "공공데이터포털 서비스 인증키",
        value=stored_onbid_key,
        type="password",
        placeholder="차세대 온비드 부동산 물건목록 조회서비스 인증키",
        help="공공데이터포털에서 ‘한국자산관리공사_차세대 온비드 부동산 물건목록 조회서비스’를 활용 신청·승인한 뒤 발급받은 인증키를 입력합니다. 호출 오퍼레이션은 getRlstCltrList2입니다.",
    )
    ssl1, ssl2 = st.columns([1, 2])
    use_system_ssl_store = ssl1.checkbox(
        "macOS 시스템 인증서 사용",
        value=str(onbid_cfg.get("ssl_trust_mode", "system")).lower() in {"system", "auto", "macos"},
        help=(
            "Python의 기본 certifi 대신 macOS 키체인의 신뢰 인증서를 사용합니다. "
            "기관망·VPN·보안프로그램이 HTTPS를 검사하는 환경에서는 이 설정이 필요할 수 있습니다."
        ),
    )
    onbid_ca_bundle_path = ssl2.text_input(
        "사용자 CA 인증서 번들 경로 (선택)",
        value=str(onbid_cfg.get("ca_bundle_path", "") or ""),
        placeholder="예: /Users/jschoi/certs/company-root-ca.pem",
        help=(
            "기관 또는 보안프로그램에서 제공한 루트 인증서 PEM 번들이 있을 때만 지정합니다. "
            "값을 입력하면 macOS 시스템 인증서보다 우선 적용됩니다."
        ),
    )
    generated_ca_path = PROJECT_ROOT / str(onbid_cfg.get("generated_ca_bundle_path", "data/certs/macos-system-ca.pem"))
    if st.button("macOS 인증서 번들 다시 생성", help="자동 생성된 키체인 인증서 번들을 삭제하여 다음 연결 때 새로 만듭니다."):
        generated_ca_path.unlink(missing_ok=True)
        st.success("자동 생성 인증서 번들을 초기화했습니다. 다음 연결 점검에서 다시 생성합니다.")
    ob1, ob2, ob3 = st.columns(3)
    onbid_page_size = ob1.number_input(
        "공매 페이지당 조회건수", min_value=10, max_value=1000, step=10,
        value=max(10, int(onbid_cfg.get("page_size", 100) or 100)),
    )
    onbid_max_pages = ob2.number_input(
        "공매 검색조건별 최대 페이지", min_value=1, max_value=100,
        value=max(1, int(onbid_cfg.get("max_pages", 10) or 10)),
    )
    onbid_cache_enabled = ob3.checkbox(
        "공매 검색 캐시 사용",
        value=bool(onbid_cfg.get("cache_enabled", True)),
    )
    onbid_cache_ttl = st.number_input(
        "공매 캐시 유지시간(분)", min_value=1, max_value=120,
        value=max(1, int(onbid_cfg.get("cache_ttl_minutes", 15) or 15)),
        disabled=not onbid_cache_enabled,
    )
    st.caption(
        "공매는 공공데이터포털의 차세대 온비드 부동산 물건목록 API "
        "`OnbidRlstListSrvc2/getRlstCltrList2`를 사용합니다. 선택지역은 API 요청에 먼저 반영하고, "
        "가격·용도·면적·유찰횟수·입찰마감일은 수집 후 동일한 검색조건으로 다시 검증합니다. "
        "목록 응답이 읍·면·동까지만 제공되는 경우에는 지번 PNU의 본번·부번·산 여부를 해석해 정확한 지번을 자동 복원합니다. "
        "Encoding/Decoding 인증키는 자동 판별합니다. HTTPS 인증서는 기본적으로 macOS 시스템 키체인을 사용하며, "
        "SSL 검증을 끄는 방식은 사용하지 않습니다."
    )
    if st.button("저장된 공매 검색 캐시 비우기", disabled=not onbid_cache_enabled):
        onbid_cache_path = PROJECT_ROOT / str(onbid_cfg.get("cache_dir", "data/onbid_cache"))
        shutil.rmtree(onbid_cache_path, ignore_errors=True)
        st.success("공매 검색 캐시를 비웠습니다.")

    st.divider()
    st.markdown("##### 지도 설정")
    maps_cfg = cfg.setdefault("maps", {})
    naver_map_cfg = maps_cfg.setdefault("naver", {})
    current_naver_client_id = str(naver_map_cfg.get("client_id", "") or "")
    if current_naver_client_id.startswith("${") and current_naver_client_id.endswith("}"):
        current_naver_client_id = ""
    naver_map_client_id = st.text_input(
        "네이버 지도 Maps JavaScript API Client ID (선택)",
        value=current_naver_client_id,
        help=(
            "입력하면 상세 팝업 안에 네이버 지도가 표시됩니다. 비워두면 "
            "네이버 지도 외부 열기 버튼이 기본으로 표시됩니다. Client ID는 비밀키가 아닙니다."
        ),
        placeholder="NAVER Cloud Maps ncpKeyId",
    )
    st.caption(
        "네이버 Cloud Maps에서 Web 서비스 URL에 현재 대시보드 주소(예: http://localhost:8501)를 등록해야 합니다."
    )

    with st.expander("Chrome 경로 설정(자동탐지 실패 시에만 사용)"):
        chrome_binary = st.text_input(
            "Chrome 실행파일 경로",
            value=str(selenium_cfg.get("chrome_binary", "")),
            placeholder="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        driver_path = st.text_input(
            "ChromeDriver 경로",
            value=str(selenium_cfg.get("driver_path", "")),
        )

    if st.button("수집 설정 저장", type="primary"):
        cfg.setdefault("app", {})["top_n_per_profile"] = int(top_n)
        selenium_cfg.update({
            "headless": bool(hide_browser),
            "page_size": int(page_size),
            "max_pages": int(max_pages),
            "max_calls_per_run": int(max_calls),
            "sale_window_days": int(sale_window),
            "search_mode": "fast" if court_search_mode_label == "빠른 검색" else "complete",
            "adaptive_warmup": bool(adaptive_warmup),
            "warmup_settle_seconds": 0.75,
            "legacy_code_fallback_only": bool(legacy_fallback_only),
            "cache_enabled": bool(cache_enabled),
            "cache_ttl_minutes": int(cache_ttl),
            "cache_dir": "data/selenium_cache",
            "fast_mode": bool(court_fast_mode),
            "price_detail_policy": "smart" if court_fast_mode else "always",
            "price_detail_max_per_run": int(price_detail_max),
            "detail_min_delay_seconds": 1.5,
            "detail_jitter_seconds": 0.5,
            "photo_enabled": bool(court_photo_enabled),
            "photo_cache_dir": "data/court_photo_cache",
            "photo_cache_days": int(court_photo_cache_days),
            "photo_max_per_run": int(court_photo_max),
            "photo_wait_seconds": float(selenium_cfg.get("photo_wait_seconds", 0.45) or 0.45),
            "photo_capture_timeout_seconds": float(selenium_cfg.get("photo_capture_timeout_seconds", 2.5) or 2.5),
            "photo_missing_cache_days": int(selenium_cfg.get("photo_missing_cache_days", 7) or 7),
            "photo_map_fallback": bool(court_photo_map_fallback),
            "min_delay_seconds": float(min_delay),
            "jitter_seconds": float(jitter),
            "server_side_region_filter": bool(server_region),
            "chrome_binary": chrome_binary.strip(),
            "driver_path": driver_path.strip(),
        })
        onbid_cfg.update({
            "api_generation": "차세대",
            "base_url": "https://apis.data.go.kr/B010003",
            "service_path": "OnbidRlstListSrvc2",
            "list_operation": "getRlstCltrList2",
            "detail_service_path": "OnbidRlstDtlSrvc2",
            "detail_operation": "getRlstDtlInf2",
            "detail_enabled": bool(onbid_cfg.get("detail_enabled", False)),
            "service_name": "한국자산관리공사_차세대 온비드 부동산 물건목록 조회서비스",
            "service_key": onbid_service_key.strip() or "${KAMCO_API_KEY}",
            "ssl_trust_mode": "system" if use_system_ssl_store else "certifi",
            "ca_bundle_path": onbid_ca_bundle_path.strip(),
            "generated_ca_bundle_path": "data/certs/macos-system-ca.pem",
            "property_division_codes": str(onbid_cfg.get("property_division_codes") or "0007,0010,0005,0002,0003,0006,0008,0011,0013"),
            "private_contract_target": "N",
            "force_land_category": bool(onbid_cfg.get("force_land_category", False)),
            "timeout_seconds": int(onbid_cfg.get("timeout_seconds", 30) or 30),
            "page_size": int(onbid_page_size),
            "max_pages": int(onbid_max_pages),
            "cache_enabled": bool(onbid_cache_enabled),
            "cache_ttl_minutes": int(onbid_cache_ttl),
            "cache_dir": "data/onbid_cache_v2",
        })
        naver_map_cfg["client_id"] = naver_map_client_id.strip()
        persist(cfg)
        st.success("수집·운영 설정을 저장했습니다.")

    st.divider()
    st.subheader("알림 설정")
    notifications = cfg.setdefault("notifications", {})
    telegram = notifications.setdefault("telegram", {})
    email_cfg = notifications.setdefault("email", {})

    n1, n2 = st.columns(2)
    with n1:
        st.markdown("##### Telegram")
        tg_enabled = st.checkbox("Telegram 알림 사용", value=bool(telegram.get("enabled", False)))
        tg_token = st.text_input(
            "봇 토큰", value=str(telegram.get("bot_token", "")), type="password",
            disabled=not tg_enabled,
        )
        tg_chat = st.text_input(
            "채팅 ID", value=str(telegram.get("chat_id", "")), disabled=not tg_enabled,
        )
    with n2:
        st.markdown("##### 이메일")
        email_enabled = st.checkbox("이메일 알림 사용", value=bool(email_cfg.get("enabled", False)))
        smtp_host = st.text_input("SMTP 서버", value=str(email_cfg.get("smtp_host", "smtp.gmail.com")), disabled=not email_enabled)
        smtp_port = st.number_input("SMTP 포트", min_value=1, max_value=65535, value=int(email_cfg.get("smtp_port", 587)), disabled=not email_enabled)
        email_user = st.text_input("SMTP 사용자명", value=str(email_cfg.get("username", "")), disabled=not email_enabled)
        email_password = st.text_input("SMTP 비밀번호", value=str(email_cfg.get("password", "")), type="password", disabled=not email_enabled)
        from_address = st.text_input("보내는 주소", value=str(email_cfg.get("from_address", "")), disabled=not email_enabled)
        to_addresses = st.text_input(
            "받는 주소(쉼표 구분)",
            value=", ".join(email_cfg.get("to_addresses", []) or []),
            disabled=not email_enabled,
        )
    st.caption("알림 인증정보는 이 Mac의 config/config.yaml에 저장됩니다. 공용 컴퓨터에서는 사용하지 마십시오.")
    if st.button("알림 설정 저장"):
        telegram.update({"enabled": tg_enabled, "bot_token": tg_token.strip(), "chat_id": tg_chat.strip()})
        email_cfg.update({
            "enabled": email_enabled,
            "smtp_host": smtp_host.strip(),
            "smtp_port": int(smtp_port),
            "username": email_user.strip(),
            "password": email_password,
            "from_address": from_address.strip(),
            "to_addresses": parse_keywords(to_addresses),
        })
        persist(cfg)
        st.success("알림 설정을 저장했습니다.")

with tab_guide:
    st.markdown(
        """
### 화면에서 사용하는 표준 용어

- **진행상태**: 신건, 유찰, 재매각 등 현재 경매절차의 상태
- **물건용도(지목)**: 전, 답, 과수원, 임야, 대, 잡종지 등
- **감정평가액**: 감정평가서에 따른 평가금액
- **최저매각가격**: 해당 매각기일에서 입찰할 수 있는 최저가격
- **유찰횟수**: 매각되지 않아 다음 기일로 넘어간 횟수
- **토지면적**: 제곱미터(㎡)를 기준으로 저장하며 평을 함께 표시
- **감정평가액 대비 할인율**: `(감정평가액 - 최저매각가격) ÷ 감정평가액 × 100`

### 이용 순서

1. **검색조건 관리**에서 지역과 가격·면적·유찰횟수 등을 선택합니다.
2. **검색조건 저장**을 누릅니다.
3. **검색·대시보드**에서 실행할 검색조건을 선택하고 **지금 검색 실행**을 누릅니다.
4. 결과 표에서 사건번호 링크를 눌러 새 탭의 상세정보와 지도를 확인한 뒤 법원 원문을 다시 확인합니다.
5. 자동 실행은 기존 `scripts/install_mac_launchd.sh`를 사용하며, GUI에서 저장한 검색조건이 그대로 적용됩니다.

### 입찰 전에 반드시 확인할 사항

등기사항증명서, 매각물건명세서, 현황조사서, 감정평가서, 농지취득자격증명 발급 가능성, 도로 접면, 경계·점유, 토지이용계획, 개발행위 제한, 분묘와 법정지상권 가능성, 현장 시세를 직접 확인해야 합니다. 자동 점수는 후보를 압축하는 참고자료이며 입찰·법률·세무 판단을 대신하지 않습니다.

### 수집 원칙

읽기 전용으로 검색하며 입찰서 작성이나 제출은 하지 않습니다. CAPTCHA 또는 접근차단을 우회하지 않으며, 차단 신호가 나타나면 실행을 중단합니다.
"""
    )
