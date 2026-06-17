from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from landwatch.runner import run_once


def main() -> None:
    parser = argparse.ArgumentParser(description="경매 검색 인덱스 성능 비교 시나리오")
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--profile", action="append", default=[], help="대상 검색조건 이름(복수 지정 가능)")
    parser.add_argument("--target", default="경매")
    args = parser.parse_args()

    profiles = args.profile or None

    # 1) cold: 강제 새로조회(인덱스/검색캐시 우회)
    t0 = time.monotonic()
    cold = run_once(
        config_path=args.config,
        notify=False,
        profile_names=profiles,
        force_refresh=True,
        search_target=args.target,
    )
    cold_elapsed = round(time.monotonic() - t0, 2)

    # 2) populate: 인덱스 미스일 때 생성되는 실행
    t1 = time.monotonic()
    populate = run_once(
        config_path=args.config,
        notify=False,
        profile_names=profiles,
        force_refresh=False,
        search_target=args.target,
    )
    populate_elapsed = round(time.monotonic() - t1, 2)

    # 3) reuse: 직전 생성된 인덱스를 재사용하는 실행
    t2 = time.monotonic()
    reuse = run_once(
        config_path=args.config,
        notify=False,
        profile_names=profiles,
        force_refresh=False,
        search_target=args.target,
    )
    reuse_elapsed = round(time.monotonic() - t2, 2)

    speedup_vs_populate = round((populate_elapsed / reuse_elapsed), 2) if reuse_elapsed > 0 else None
    speedup_vs_cold = round((cold_elapsed / reuse_elapsed), 2) if reuse_elapsed > 0 else None

    print(json.dumps({
        "cold_run_seconds": cold_elapsed,
        "populate_run_seconds": populate_elapsed,
        "reuse_run_seconds": reuse_elapsed,
        "speedup_vs_populate_x": speedup_vs_populate,
        "speedup_vs_cold_x": speedup_vs_cold,
        "cold_found": len(cold.items),
        "populate_found": len(populate.items),
        "reuse_found": len(reuse.items),
        "cold_diagnostics": cold.diagnostics,
        "populate_diagnostics": populate.diagnostics,
        "reuse_diagnostics": reuse.diagnostics,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
