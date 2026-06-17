from __future__ import annotations

import html as html_lib
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .models import AuctionItem
from .ui_config import sqm_to_pyeong

INTEGER_COLUMNS = [
    "최저매각가격/최저입찰가", "최저매각가격", "감정평가액", "유찰횟수",
    "㎡당 최저가격", "㎡당 최저매각가격", "추정시세"
]
DECIMAL_COLUMNS = [
    "투자검토점수", "감정평가액 대비 할인율(%)", "추정시세 대비 할인율(%)", "토지면적(평)"
]
AREA_COLUMNS = ["토지면적(㎡)"]
CASE_NUMBER_RE = re.compile(r"^\d{4}타경\d+$")


def _source_meta(source_cfg: dict[str, Any] | None) -> tuple[str, bool, str]:
    source_cfg = source_cfg or {}
    is_sample = bool(source_cfg.get("is_sample", False))
    label = str(source_cfg.get("label") or ("기능 테스트용 샘플 데이터" if is_sample else "경매·공매 통합 데이터"))
    if is_sample:
        note = "가상 데이터이므로 원문 사이트에서 검색되지 않습니다."
    else:
        note = "가격·입찰일정·상태는 참여 전 대한민국 법원경매정보 또는 온비드 원문에서 반드시 재확인하십시오."
    return label, is_sample, note


def _number_status(item: AuctionItem, is_sample: bool) -> str:
    if is_sample:
        return "샘플(검색불가)"
    if not item.case_number or item.case_number == "-":
        return "번호 없음"
    if item.sale_type == "공매":
        return "공고번호·원문 확인 필요"
    if CASE_NUMBER_RE.fullmatch(item.case_number.strip()):
        return "형식 정상·원문 확인 필요"
    return "사건번호 형식 확인 필요"


def to_dataframe(items: list[AuctionItem], source_cfg: dict[str, Any] | None = None) -> pd.DataFrame:
    """경매·공매 결과를 공통 표준 열로 변환한다."""
    default_label, is_sample, _ = _source_meta(source_cfg)
    rows: list[dict[str, Any]] = []
    for x in items:
        number = x.case_number or "-"
        rows.append({
            "매각구분": x.sale_type or "경매",
            "데이터 출처": default_label if is_sample else (x.source_name or default_label),
            "번호 확인": _number_status(x, is_sample),
            "사건번호 확인": _number_status(x, is_sample),
            "검색조건": x.matched_profile,
            "검토등급": x.grade,
            "투자검토점수": x.score,
            "진행기관": x.court,
            "사건/공고번호": number,
            "물건번호/물건관리번호": x.item_number or "-",
            "진행상태": x.status,
            "물건용도": x.usage,
            "소재지": x.address,
            "최저매각가격/최저입찰가": x.min_price,
            "감정평가액": x.appraisal_price,
            "감정평가액 대비 할인율(%)": round(x.discount_percent, 1),
            "유찰횟수": x.failed_count,
            "토지면적(㎡)": x.land_area_m2,
            "토지면적(평)": sqm_to_pyeong(x.land_area_m2),
            "㎡당 최저가격": round(x.unit_price),
            "매각기일/입찰마감일": x.auction_date.isoformat() if x.auction_date else "",
            "추정시세": x.market_estimate,
            "추정시세 대비 할인율(%)": round(x.market_gap_percent, 1),
            "검토근거": ", ".join(x.score_reasons),
            "주의사항": ", ".join(x.risk_reasons),
            "원문 URL": x.detail_url,
            # 기존 보고서·외부 연동과의 하위 호환 열
            "법원": x.court,
            "사건번호": number,
            "물건번호": x.item_number or "-",
            "최저매각가격": x.min_price,
            "㎡당 최저매각가격": round(x.unit_price),
            "매각기일": x.auction_date.isoformat() if x.auction_date else "",
            "법원경매정보 URL": x.detail_url if (x.sale_type or "경매") == "경매" else "",
        })
    return pd.DataFrame(rows)


def _format_integer(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    try:
        return f"{int(round(float(value))):,}"
    except (TypeError, ValueError):
        return str(value)


def _format_decimal(value: Any, digits: int = 1) -> str:
    if value is None or pd.isna(value):
        return ""
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _format_area(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number.is_integer():
        return f"{number:,.0f}"
    return f"{number:,.2f}".rstrip("0").rstrip(".")


def format_dataframe_for_report(df: pd.DataFrame) -> pd.DataFrame:
    display = df.copy()
    for column in INTEGER_COLUMNS:
        if column in display.columns:
            display[column] = display[column].map(_format_integer)
    for column in DECIMAL_COLUMNS:
        if column in display.columns:
            display[column] = display[column].map(_format_decimal)
    for column in AREA_COLUMNS:
        if column in display.columns:
            display[column] = display[column].map(_format_area)
    return display


def save_reports(
    items: list[AuctionItem],
    report_dir: str,
    source_cfg: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    out = Path(report_dir)
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    label, is_sample, note = _source_meta(source_cfg)
    report_df = format_dataframe_for_report(to_dataframe(items, source_cfg))
    csv_path = out / f"landwatch_{stamp}.csv"
    html_path = out / f"landwatch_{stamp}.html"

    report_df.to_csv(csv_path, index=False, encoding="utf-8-sig")

    banner_class = "sample" if is_sample else "verify"
    banner_title = "샘플 보고서 — 실제 물건 아님" if is_sample else "참여 전 원문 확인 필요"
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = f"""
    <!doctype html><html lang='ko'><head><meta charset='utf-8'><title>LandWatch 토지 경매·공매 보고서</title>
    <style>
      body{{font-family:-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo','Noto Sans KR',sans-serif;margin:24px;color:#202124}}
      h1{{margin-bottom:6px}} .generated{{color:#666;margin-bottom:18px}}
      .banner{{padding:16px 18px;margin:0 0 18px;border-radius:10px;font-weight:700}}
      .banner.sample{{background:#fff0f0;border:2px solid #d93025;color:#a50e0e}}
      .banner.verify{{background:#fff8e1;border:1px solid #e0a800;color:#6b4f00}}
      .source{{font-weight:400;margin-top:6px}}
      .table-wrap{{overflow:auto;max-height:75vh}}
      table{{border-collapse:collapse;width:max-content;min-width:100%;font-size:13px}}
      th,td{{border:1px solid #ddd;padding:7px;vertical-align:top;white-space:nowrap}}
      th{{background:#f2f2f2;position:sticky;top:0}}
      tr:nth-child(even){{background:#fafafa}}
      td{{font-variant-numeric:tabular-nums}}
    </style>
    </head><body><h1>토지 경매·공매 투자후보 보고서</h1>
    <div class='generated'>생성일시: {generated_at}</div>
    <div class='banner {banner_class}'><div>{html_lib.escape(banner_title)}</div>
    <div class='source'>기본 데이터 출처: {html_lib.escape(label)}<br>{html_lib.escape(note)}</div></div>
    <div class='table-wrap'>""" + report_df.to_html(index=False, escape=True) + "</div></body></html>"
    html_path.write_text(html, encoding="utf-8")
    return csv_path, html_path
