#!/usr/bin/env python3
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from landwatch.regions import (
    MUNICIPALITY_CODES,
    PROVINCE_CODES,
    REGION_CODE_AUDIT,
    region_label,
    resolve_region_codes,
)


def main() -> int:
    output = ROOT / "data" / "reference" / "court_region_code_audit.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=["시도", "시군구", "GUI표기", "시도코드", "기준시군구코드(5자리)", "법원요청코드(3자리)", "검증결과"],
        )
        writer.writeheader()
        for province, municipalities in MUNICIPALITY_CODES.items():
            for municipality in municipalities:
                label = region_label(province, municipality)
                codes = resolve_region_codes(label) or {}
                ok = (
                    codes.get("sido") == PROVINCE_CODES[province]
                    and codes.get("sigungu") == municipalities[municipality][2:]
                    and len(str(codes.get("sigungu", ""))) == 3
                )
                writer.writerow({
                    "시도": province,
                    "시군구": municipality,
                    "GUI표기": label,
                    "시도코드": codes.get("sido", ""),
                    "기준시군구코드(5자리)": municipalities[municipality],
                    "법원요청코드(3자리)": codes.get("sigungu", ""),
                    "검증결과": "정상" if ok else "오류",
                })
    print(json.dumps(REGION_CODE_AUDIT, ensure_ascii=False, indent=2))
    print(f"CSV: {output}")
    return 0 if REGION_CODE_AUDIT["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
