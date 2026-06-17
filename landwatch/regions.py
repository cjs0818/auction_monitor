from __future__ import annotations

from collections import OrderedDict
from typing import Any

# 행정표준 기준 시·도(2자리) 및 시·군·구(5자리) 코드표.
# 법원 자유검색 요청에서는 시·도와 시·군·구를 별도 필드로 보내므로
# rprsAdongSggCd에는 5자리 전체코드의 뒤 3자리만 전달한다.
# 복합시의 일반구보다 사용자가 요청한 시·군 단위를 우선 제공한다.
PROVINCE_CODES: OrderedDict[str, str] = OrderedDict([
    ("서울특별시", "11"),
    ("부산광역시", "26"),
    ("대구광역시", "27"),
    ("인천광역시", "28"),
    ("광주광역시", "29"),
    ("대전광역시", "30"),
    ("울산광역시", "31"),
    ("세종특별자치시", "36"),
    ("경기도", "41"),
    ("강원특별자치도", "51"),
    ("충청북도", "43"),
    ("충청남도", "44"),
    ("전북특별자치도", "52"),
    ("전라남도", "46"),
    ("경상북도", "47"),
    ("경상남도", "48"),
    ("제주특별자치도", "50"),
])

MUNICIPALITY_CODES: dict[str, OrderedDict[str, str]] = {
    "서울특별시": OrderedDict([
        ("종로구", "11110"), ("중구", "11140"), ("용산구", "11170"),
        ("성동구", "11200"), ("광진구", "11215"), ("동대문구", "11230"),
        ("중랑구", "11260"), ("성북구", "11290"), ("강북구", "11305"),
        ("도봉구", "11320"), ("노원구", "11350"), ("은평구", "11380"),
        ("서대문구", "11410"), ("마포구", "11440"), ("양천구", "11470"),
        ("강서구", "11500"), ("구로구", "11530"), ("금천구", "11545"),
        ("영등포구", "11560"), ("동작구", "11590"), ("관악구", "11620"),
        ("서초구", "11650"), ("강남구", "11680"), ("송파구", "11710"),
        ("강동구", "11740"),
    ]),
    "부산광역시": OrderedDict([
        ("중구", "26110"), ("서구", "26140"), ("동구", "26170"),
        ("영도구", "26200"), ("부산진구", "26230"), ("동래구", "26260"),
        ("남구", "26290"), ("북구", "26320"), ("해운대구", "26350"),
        ("사하구", "26380"), ("금정구", "26410"), ("강서구", "26440"),
        ("연제구", "26470"), ("수영구", "26500"), ("사상구", "26530"),
        ("기장군", "26710"),
    ]),
    "대구광역시": OrderedDict([
        ("중구", "27110"), ("동구", "27140"), ("서구", "27170"),
        ("남구", "27200"), ("북구", "27230"), ("수성구", "27260"),
        ("달서구", "27290"), ("달성군", "27710"), ("군위군", "27720"),
    ]),
    "인천광역시": OrderedDict([
        ("중구", "28110"), ("동구", "28140"), ("미추홀구", "28177"),
        ("연수구", "28185"), ("남동구", "28200"), ("부평구", "28237"),
        ("계양구", "28245"), ("서구", "28260"), ("강화군", "28710"),
        ("옹진군", "28720"),
    ]),
    "광주광역시": OrderedDict([
        ("동구", "29110"), ("서구", "29140"), ("남구", "29155"),
        ("북구", "29170"), ("광산구", "29200"),
    ]),
    "대전광역시": OrderedDict([
        ("동구", "30110"), ("중구", "30140"), ("서구", "30170"),
        ("유성구", "30200"), ("대덕구", "30230"),
    ]),
    "울산광역시": OrderedDict([
        ("중구", "31110"), ("남구", "31140"), ("동구", "31170"),
        ("북구", "31200"), ("울주군", "31710"),
    ]),
    "세종특별자치시": OrderedDict([("세종특별자치시", "36110")]),
    "경기도": OrderedDict([
        ("수원시", "41110"), ("성남시", "41130"), ("의정부시", "41150"),
        ("안양시", "41170"), ("부천시", "41190"), ("광명시", "41210"),
        ("평택시", "41220"), ("동두천시", "41250"), ("안산시", "41270"),
        ("고양시", "41280"), ("과천시", "41290"), ("구리시", "41310"),
        ("남양주시", "41360"), ("오산시", "41370"), ("시흥시", "41390"),
        ("군포시", "41410"), ("의왕시", "41430"), ("하남시", "41450"),
        ("용인시", "41460"), ("파주시", "41480"), ("이천시", "41500"),
        ("안성시", "41550"), ("김포시", "41570"), ("화성시", "41590"),
        ("광주시", "41610"), ("양주시", "41630"), ("포천시", "41650"),
        ("여주시", "41670"), ("연천군", "41800"), ("가평군", "41820"),
        ("양평군", "41830"),
    ]),
    "강원특별자치도": OrderedDict([
        ("춘천시", "51110"), ("원주시", "51130"), ("강릉시", "51150"),
        ("동해시", "51170"), ("태백시", "51190"), ("속초시", "51210"),
        ("삼척시", "51230"), ("홍천군", "51720"), ("횡성군", "51730"),
        ("영월군", "51750"), ("평창군", "51760"), ("정선군", "51770"),
        ("철원군", "51780"), ("화천군", "51790"), ("양구군", "51800"),
        ("인제군", "51810"), ("고성군", "51820"), ("양양군", "51830"),
    ]),
    "충청북도": OrderedDict([
        ("청주시", "43110"), ("충주시", "43130"), ("제천시", "43150"),
        ("보은군", "43720"), ("옥천군", "43730"), ("영동군", "43740"),
        ("증평군", "43745"), ("진천군", "43750"), ("괴산군", "43760"),
        ("음성군", "43770"), ("단양군", "43800"),
    ]),
    "충청남도": OrderedDict([
        ("천안시", "44130"), ("공주시", "44150"), ("보령시", "44180"),
        ("아산시", "44200"), ("서산시", "44210"), ("논산시", "44230"),
        ("계룡시", "44250"), ("당진시", "44270"), ("금산군", "44710"),
        ("부여군", "44760"), ("서천군", "44770"), ("청양군", "44790"),
        ("홍성군", "44800"), ("예산군", "44810"), ("태안군", "44825"),
    ]),
    "전북특별자치도": OrderedDict([
        ("전주시", "52110"), ("군산시", "52130"), ("익산시", "52140"),
        ("정읍시", "52180"), ("남원시", "52190"), ("김제시", "52210"),
        ("완주군", "52710"), ("진안군", "52720"), ("무주군", "52730"),
        ("장수군", "52740"), ("임실군", "52750"), ("순창군", "52770"),
        ("고창군", "52790"), ("부안군", "52800"),
    ]),
    "전라남도": OrderedDict([
        ("목포시", "46110"), ("여수시", "46130"), ("순천시", "46150"),
        ("나주시", "46170"), ("광양시", "46230"), ("담양군", "46710"),
        ("곡성군", "46720"), ("구례군", "46730"), ("고흥군", "46770"),
        ("보성군", "46780"), ("화순군", "46790"), ("장흥군", "46800"),
        ("강진군", "46810"), ("해남군", "46820"), ("영암군", "46830"),
        ("무안군", "46840"), ("함평군", "46860"), ("영광군", "46870"),
        ("장성군", "46880"), ("완도군", "46890"), ("진도군", "46900"),
        ("신안군", "46910"),
    ]),
    "경상북도": OrderedDict([
        ("포항시", "47110"), ("경주시", "47130"), ("김천시", "47150"),
        ("안동시", "47170"), ("구미시", "47190"), ("영주시", "47210"),
        ("영천시", "47230"), ("상주시", "47250"), ("문경시", "47280"),
        ("경산시", "47290"), ("의성군", "47730"), ("청송군", "47750"),
        ("영양군", "47760"), ("영덕군", "47770"), ("청도군", "47820"),
        ("고령군", "47830"), ("성주군", "47840"), ("칠곡군", "47850"),
        ("예천군", "47900"), ("봉화군", "47920"), ("울진군", "47930"),
        ("울릉군", "47940"),
    ]),
    "경상남도": OrderedDict([
        ("창원시", "48120"), ("진주시", "48170"), ("통영시", "48220"),
        ("사천시", "48240"), ("김해시", "48250"), ("밀양시", "48270"),
        ("거제시", "48310"), ("양산시", "48330"), ("의령군", "48720"),
        ("함안군", "48730"), ("창녕군", "48740"), ("고성군", "48820"),
        ("남해군", "48840"), ("하동군", "48850"), ("산청군", "48860"),
        ("함양군", "48870"), ("거창군", "48880"), ("합천군", "48890"),
    ]),
    "제주특별자치도": OrderedDict([
        ("제주시", "50110"), ("서귀포시", "50130"),
    ]),
}

PROVINCE_ALIASES = {
    "서울": "서울특별시", "부산": "부산광역시", "대구": "대구광역시",
    "인천": "인천광역시", "광주": "광주광역시", "대전": "대전광역시",
    "울산": "울산광역시", "세종": "세종특별자치시", "경기": "경기도",
    "강원": "강원특별자치도", "강원도": "강원특별자치도",
    "충북": "충청북도", "충남": "충청남도",
    "전북": "전북특별자치도", "전라북도": "전북특별자치도",
    "전남": "전라남도", "경북": "경상북도", "경남": "경상남도",
    "제주": "제주특별자치도",
}


# 이 표는 GUI에서 제공하는 시·군·구 선택항목 전체를 구성한다. 현재 프로그램의
# 선택단위는 복합시의 일반구가 아니라 상위 시 단위이므로 총 229개 항목이다.
# 표의 값은 검증·표시용 5자리 전체코드이며, 실제 법원 요청값은 뒤 3자리다.
EXPECTED_PROVINCE_COUNT = 17
EXPECTED_MUNICIPALITY_COUNT = 229


def province_names() -> list[str]:
    return list(PROVINCE_CODES)


def municipality_names(provinces: list[str] | None = None) -> list[str]:
    provinces = provinces or province_names()
    out: list[str] = []
    for province in provinces:
        out.extend(region_label(province, name) for name in MUNICIPALITY_CODES.get(province, {}))
    return out


def region_label(province: str, municipality: str = "") -> str:
    province = normalize_province(province)
    municipality = str(municipality or "").strip()
    if not municipality:
        return province
    # 세종은 시·도명과 시·군·구명이 동일하므로 시·도 전체와 시·군·구 선택을
    # 구분할 수 있도록 GUI 표기만 ``세종시``로 축약한다.
    if province == "세종특별자치시" and municipality == province:
        return f"{province} 세종시"
    if municipality == province:
        return province
    return f"{province} {municipality}"


def normalize_province(value: str) -> str:
    text = str(value or "").strip()
    return PROVINCE_ALIASES.get(text, text)


def split_region_label(label: str) -> tuple[str, str]:
    text = " ".join(str(label or "").split())
    if not text:
        return "", ""
    first, *rest = text.split(" ")
    province = normalize_province(first)
    municipality = " ".join(rest)
    if province == "세종특별자치시" and municipality == "세종시":
        municipality = "세종특별자치시"
    return province, municipality


def normalize_court_region_code_values(codes: dict[str, Any] | None) -> dict[str, str]:
    """법원 자유검색 요청용 지역코드로 정규화한다.

    행정표준 시·군·구 코드는 5자리(예: 고흥군 ``46770``)지만 법원 검색화면은
    시·도 코드 ``46``을 별도 필드로 전송하고 ``rprsAdongSggCd``에는 하위
    3자리 ``770``을 전송한다. 설정파일에 3자리 또는 5자리가 들어와도 같은
    요청값으로 정규화하며, 5자리 코드의 시·도 접두부가 다르면 차단한다.
    """
    raw = codes or {}
    sido = str(raw.get("sido", "") or "").strip()
    sigungu = str(raw.get("sigungu", "") or "").strip()
    dong = str(raw.get("dong", "") or "").strip()

    if sido and (len(sido) != 2 or not sido.isdigit()):
        raise ValueError(f"시·도 코드는 2자리 숫자여야 합니다: {sido!r}")
    if sigungu:
        if not sigungu.isdigit():
            raise ValueError(f"시·군·구 코드는 숫자여야 합니다: {sigungu!r}")
        if len(sigungu) == 5:
            if not sido:
                sido = sigungu[:2]
            if sigungu[:2] != sido:
                raise ValueError(
                    f"시·도({sido})와 시·군·구({sigungu}) 코드의 앞 2자리가 일치하지 않습니다."
                )
            sigungu = sigungu[2:]
        elif len(sigungu) != 3:
            raise ValueError(
                f"시·군·구 코드는 법원 요청용 3자리 또는 전체 5자리여야 합니다: {sigungu!r}"
            )
        if not sido:
            raise ValueError("3자리 시·군·구 코드에는 2자리 시·도 코드가 함께 필요합니다.")
    return {"sido": sido, "sigungu": sigungu, "dong": dong}

def _court_sigungu_code(sido: str, full_code: str) -> str:
    """5자리 행정표준코드를 법원 요청용 하위 3자리로 변환한다."""
    return normalize_court_region_code_values({
        "sido": sido, "sigungu": full_code, "dong": ""
    })["sigungu"]


def audit_region_code_tables() -> dict[str, Any]:
    """5자리 기준코드와 실제 3자리 법원 요청값을 함께 전수검사한다."""
    errors: list[str] = []
    seen_full_codes: dict[str, str] = {}
    municipality_count = 0

    if len(PROVINCE_CODES) != EXPECTED_PROVINCE_COUNT:
        errors.append(
            f"시·도 개수 불일치: {len(PROVINCE_CODES)} != {EXPECTED_PROVINCE_COUNT}"
        )

    for province, sido in PROVINCE_CODES.items():
        if len(sido) != 2 or not sido.isdigit():
            errors.append(f"{province}: 시·도 코드가 2자리 숫자가 아님({sido})")
        municipalities = MUNICIPALITY_CODES.get(province)
        if municipalities is None:
            errors.append(f"{province}: 시·군·구 코드표 누락")
            continue
        for municipality, full_code in municipalities.items():
            municipality_count += 1
            label = region_label(province, municipality)
            if len(full_code) != 5 or not full_code.isdigit():
                errors.append(f"{label}: 기준 시·군·구 코드가 5자리 숫자가 아님({full_code})")
                continue
            if not full_code.startswith(sido):
                errors.append(f"{label}: 기준코드 {full_code}가 시·도코드 {sido}로 시작하지 않음")
                continue
            request_codes = resolve_region_codes(label)
            expected_request = {"sido": sido, "sigungu": full_code[2:], "dong": ""}
            if request_codes != expected_request:
                errors.append(
                    f"{label}: 법원 요청코드 변환 불일치({request_codes} != {expected_request})"
                )
            if full_code in seen_full_codes:
                errors.append(
                    f"시·군·구 기준코드 중복 {full_code}: {seen_full_codes[full_code]} / {label}"
                )
            else:
                seen_full_codes[full_code] = label

    extra_provinces = sorted(set(MUNICIPALITY_CODES) - set(PROVINCE_CODES))
    if extra_provinces:
        errors.append(f"시·도 표에 없는 시·군·구 그룹: {', '.join(extra_provinces)}")
    if municipality_count != EXPECTED_MUNICIPALITY_COUNT:
        errors.append(
            f"시·군·구 개수 불일치: {municipality_count} != {EXPECTED_MUNICIPALITY_COUNT}"
        )

    return {
        "province_count": len(PROVINCE_CODES),
        "municipality_count": municipality_count,
        "unique_sigungu_code_count": len(seen_full_codes),
        "request_sigungu_length": 3,
        "errors": errors,
        "ok": not errors,
    }

def resolve_region_codes(label: str) -> dict[str, str] | None:
    province, municipality = split_region_label(label)
    sido = PROVINCE_CODES.get(province)
    if not sido:
        return None
    if not municipality:
        return {"sido": sido, "sigungu": "", "dong": ""}
    full_code = MUNICIPALITY_CODES.get(province, {}).get(municipality)
    if not full_code:
        return None
    return {
        "sido": sido,
        "sigungu": _court_sigungu_code(sido, full_code),
        "dong": "",
    }



# 특별자치도 전환 이후 법원경매정보의 지역목록에는 과거 도 코드와
# 현행 특별자치도 코드가 함께 노출될 수 있다. 일부 경매물건은 과거 코드로
# 검색될 수 있으므로, 현행 코드를 우선 사용하고 0건이면 과거 코드를 재시도한다.
LEGACY_PROVINCE_CODE_MAP: dict[str, str] = {
    "51": "42",  # 강원특별자치도 -> 강원도
    "52": "45",  # 전북특별자치도 -> 전라북도
}


def resolve_region_code_variants(label: str) -> list[dict[str, str]]:
    """법원 검색에 사용할 현행/과거 행정구역 코드 후보를 반환한다.

    일반 시·도/시·군·구는 한 개 후보만 반환한다. 강원특별자치도와
    전북특별자치도는 현행 코드가 0건일 때 사용할 과거 코드 후보를 추가한다.
    """
    primary = resolve_region_codes(label)
    if not primary:
        return []

    variants = [dict(primary)]
    legacy_sido = LEGACY_PROVINCE_CODE_MAP.get(primary.get("sido", ""))
    if not legacy_sido:
        return variants

    legacy = dict(primary)
    current_sido = str(primary.get("sido", ""))
    legacy["sido"] = legacy_sido
    sigungu = str(primary.get("sigungu", ""))
    # 법원 요청의 시·군·구 값은 하위 3자리이므로 특별자치도 전환 전후에도
    # 그대로 사용한다(부안군 800, 원주시 130).
    variants.append(legacy)
    return variants

def build_region_code_map(regions: list[str] | None = None) -> dict[str, dict[str, str]]:
    labels = regions or [
        region_label(province, municipality)
        for province, municipalities in MUNICIPALITY_CODES.items()
        for municipality in municipalities
    ]
    result: dict[str, dict[str, str]] = {}
    for label in labels:
        codes = resolve_region_codes(label)
        if codes:
            result[label] = codes
    return result


# 모듈 로딩 시 전체 코드표를 한 번 검증해 잘못된 지역코드가 실제 법원 요청으로
# 전달되기 전에 즉시 발견한다.
REGION_CODE_AUDIT = audit_region_code_tables()
if not REGION_CODE_AUDIT["ok"]:
    raise RuntimeError("내장 지역코드 표 검증 실패: " + "; ".join(REGION_CODE_AUDIT["errors"]))


def region_defaults_from_profile(profile: dict[str, Any]) -> tuple[list[str], list[str]]:
    provinces: list[str] = []
    municipalities: list[str] = []
    for raw in profile.get("regions", []) or []:
        province, municipality = split_region_label(str(raw))
        if province and province not in provinces:
            provinces.append(province)
        if municipality and municipality != province:
            label = region_label(province, municipality)
            if label not in municipalities:
                municipalities.append(label)
    return provinces, municipalities
