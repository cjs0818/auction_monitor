from __future__ import annotations

import argparse
import json
import logging

from .config import load_config
from .providers import SEARCH_TARGET_OPTIONS, build_provider
from .runner import run_once


def main() -> None:
    parser = argparse.ArgumentParser(description="토지 경매·공매 후보 자동 수집·평가")
    parser.add_argument("command", choices=["run", "connection-check", "selenium-check"])
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--target", choices=SEARCH_TARGET_OPTIONS, default=None)
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--force-refresh", action="store_true", help="검색 캐시를 무시하고 원문에서 새로 조회")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.command in {"connection-check", "selenium-check"}:
        cfg = load_config(args.config)
        target = "경매" if args.command == "selenium-check" else args.target
        provider = build_provider(cfg, search_target=target)
        try:
            if not hasattr(provider, "test_connection"):
                raise RuntimeError("현재 데이터 공급자는 연결 점검을 지원하지 않습니다.")
            profiles = [p for p in cfg.get("profiles", []) if p.get("enabled", True)]
            if not profiles:
                raise RuntimeError("활성화된 검색 프로필이 없습니다.")
            result = provider.test_connection(profiles[0])
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            try:
                provider.close()
            except Exception:
                pass
        return

    result = run_once(
        args.config,
        notify=not args.no_notify,
        force_refresh=args.force_refresh,
        search_target=args.target,
    )
    print(json.dumps({
        "found": len(result.items),
        "auction": sum(x.sale_type == "경매" for x in result.items),
        "public_sale": sum(x.sale_type == "공매" for x in result.items),
        "new": len(result.new_items),
        "changed": len(result.changed_items),
        "report_csv": result.report_csv,
        "report_html": result.report_html,
        "notifications": result.notification_channels,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
