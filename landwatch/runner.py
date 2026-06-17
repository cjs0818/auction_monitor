from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import load_config
from .db import Database
from .filtering import matches_profile
from .models import AuctionItem
from .notify import send_notifications
from .providers import build_provider
from .report import save_reports
from .scoring import score_item

logger = logging.getLogger(__name__)


@dataclass
class RunResult:
    items: list[AuctionItem]
    new_items: list[AuctionItem]
    changed_items: list[AuctionItem]
    report_csv: str
    report_html: str
    notification_channels: list[str]
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    excluded_items: list[dict[str, Any]] = field(default_factory=list)


def run_once(
    config_path: str = "config/config.yaml",
    notify: bool = True,
    profile_names: list[str] | set[str] | tuple[str, ...] | None = None,
    force_refresh: bool = False,
    search_target: str | None = None,
) -> RunResult:
    cfg = load_config(config_path)
    db = Database(str(cfg.get("app", {}).get("database_path", "data/landwatch.db")))
    provider = build_provider(cfg, search_target=search_target)
    if force_refresh and hasattr(provider, "cache_enabled"):
        provider.cache_enabled = False
    run_id = db.start_run()
    items: list[AuctionItem] = []
    new_items: list[AuctionItem] = []
    changed_items: list[AuctionItem] = []
    diagnostics: list[dict[str, Any]] = []
    excluded_items: list[dict[str, Any]] = []

    selected_names = {str(x) for x in profile_names or []}

    try:
        for profile in cfg.get("profiles", []):
            if not profile.get("enabled", True):
                continue
            if selected_names and str(profile.get("name", "")) not in selected_names:
                continue
            profile_started_at = time.monotonic()
            candidates = provider.fetch(profile)
            matched: list[AuctionItem] = []
            reason_counts: Counter[str] = Counter()
            for item in candidates:
                ok, reasons = matches_profile(item, profile)
                if not ok:
                    reason_counts.update(reasons)
                    if len(excluded_items) < 500:
                        excluded_items.append({
                            "검색조건": str(profile.get("name", "")),
                            "매각구분": item.sale_type or "-",
                            "사건/공고번호": item.case_number or "-",
                            "물건번호/물건관리번호": item.item_number or "-",
                            "진행기관": item.court or "-",
                            "소재지": item.address or "-",
                            "진행상태": item.status or "-",
                            "물건용도": item.usage or "-",
                            "유찰횟수": int(item.failed_count or 0),
                            "최저매각가격": int(item.min_price or 0),
                            "감정평가액": int(item.appraisal_price or 0),
                            "할인율(%)": round(float(item.discount_percent or 0), 1),
                            "토지면적(㎡)": round(float(item.land_area_m2 or 0), 2),
                            "매각기일": item.auction_date.isoformat() if item.auction_date else "-",
                            "제외사유": ", ".join(reasons),
                        })
                    continue
                matched.append(item)
            provider_region_diag = getattr(provider, "last_fetch_diagnostics", []) or []
            fetch_summary = getattr(provider, "last_fetch_summary", {}) or {}
            region_summary = "; ".join(
                f"{d.get('검색방식', '')} · {d.get('지역', '')} "
                f"[{d.get('조회코드', '')}/{d.get('코드구분', '')}] "
                f"{d.get('조회기간', '')} 구간 {d.get('완료구간', '')} "
                f"전체 {int(d.get('법원 전체건수', 0) or 0):,}건 · "
                f"서버행 {int(d.get('수집건수', 0) or 0):,}건 · "
                f"지역일치 {int(d.get('지역일치건수', d.get('수집건수', 0)) or 0):,}건 · "
                f"지역제외 {int(d.get('지역불일치제외', 0) or 0):,}건 · {d.get('비고', '')}"
                for d in provider_region_diag
            ) or "-"
            diagnostics.append({
                "검색조건": str(profile.get("name", "")),
                "검색대상": search_target or (cfg.get("app", {}) or {}).get("search_target", "경매"),
                "실행지역": ", ".join(profile.get("regions", []) or ["전국"]),
                "총 소요시간(초)": fetch_summary.get("총 소요시간(초)", "-"),
                "목록 수집시간(초)": fetch_summary.get("총 소요시간(초)", "-"),
                "상세·사진 보강시간(초)": "-",
                "실제 법원요청": fetch_summary.get("실제 법원요청", "-"),
                "실제 공매요청": fetch_summary.get("실제 공매요청", "-"),
                "캐시 재사용": fetch_summary.get("캐시 재사용", "-"),
                "요청대기시간(초)": fetch_summary.get("요청대기시간(초)", "-"),
                "서버응답시간(초)": fetch_summary.get("서버응답시간(초)", "-"),
                "브라우저준비시간(초)": fetch_summary.get("브라우저준비시간(초)", "-"),
                "법원 수집건수": len(candidates),
                "조건 통과건수": len(matched),
                "지역코드 조회내역": region_summary,
                "주요 제외사유": ", ".join(
                    f"{reason} {count}건" for reason, count in reason_counts.most_common(5)
                ) or "-",
            })

            # 먼저 목록자료만으로 점수를 계산하고 상위 표시후보를 정한다.
            # 법원 사진은 공식 사건상세 화면을 추가로 열어야 하므로 화면에 표시할
            # 상위 후보에만 적용해 검색시간과 사이트 호출량을 통제한다.
            scored = [score_item(item, profile) for item in matched]
            scored.sort(key=lambda x: x.score, reverse=True)
            top_n = int(cfg.get("app", {}).get("top_n_per_profile", 20))
            selected = scored[:top_n]

            enriched: list[AuctionItem] = []
            for item in selected:
                try:
                    item = provider.fetch_detail(item)
                except Exception as exc:  # detail/photo failure should not abort the run
                    logger.warning("상세조회 실패 %s: %s", item.auction_id, exc)
                enriched.append(score_item(item, profile))

            enriched.sort(key=lambda x: x.score, reverse=True)
            # 공급자가 경매·공매 결합형이어도 내부 법원 공급자의 사진 통계를 찾는다.
            photo_sources = getattr(provider, "providers", [provider])
            for source in photo_sources:
                if source.__class__.__name__ == "CourtAuctionSeleniumProvider":
                    diagnostics[-1]["경매사진 캐시"] = int(getattr(source, "photo_cache_hits", 0) or 0)
                    diagnostics[-1]["경매사진 신규수집"] = int(getattr(source, "photo_new_count", 0) or 0)
                    diagnostics[-1]["경매사진 실패"] = int(getattr(source, "photo_failure_count", 0) or 0)
                    diagnostics[-1]["경매가격 상세교정"] = int(getattr(source, "price_detail_success_count", 0) or 0)
                    diagnostics[-1]["경매가격 상세생략"] = int(getattr(source, "price_detail_skipped_count", 0) or 0)
                    diagnostics[-1]["경매가격 교정실패"] = int(getattr(source, "price_detail_failure_count", 0) or 0)
                    break

            profile_total = round(time.monotonic() - profile_started_at, 1)
            try:
                list_seconds = float(fetch_summary.get("총 소요시간(초)", 0) or 0)
            except (TypeError, ValueError):
                list_seconds = 0.0
            diagnostics[-1]["총 소요시간(초)"] = profile_total
            diagnostics[-1]["상세·사진 보강시간(초)"] = round(max(0.0, profile_total - list_seconds), 1)

            for item in enriched:
                state = db.upsert(item)
                items.append(item)
                if state == "new":
                    new_items.append(item)
                elif state == "changed":
                    changed_items.append(item)

        items.sort(key=lambda x: x.score, reverse=True)
        report_csv, report_html = save_reports(
            items,
            str(cfg.get("app", {}).get("report_dir", "reports")),
            cfg.get("source", {}) or {},
        )
        notify_items = new_items + changed_items
        channels = send_notifications(notify_items, cfg.get("notifications", {})) if notify else []
        db.finish_run(
            run_id, status="success", found=len(items), new=len(new_items), changed=len(changed_items)
        )
        return RunResult(
            items, new_items, changed_items, str(report_csv), str(report_html), channels,
            diagnostics=diagnostics, excluded_items=excluded_items,
        )
    except Exception as exc:
        db.finish_run(run_id, status="error", found=len(items), new=len(new_items), changed=len(changed_items), message=str(exc))
        raise
    finally:
        try:
            provider.close()
        except Exception:
            logger.debug("데이터 공급자 종료 실패", exc_info=True)
