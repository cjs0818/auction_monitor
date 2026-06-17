# 차세대 온비드 API 전환 수정

공공데이터포털의 승인 화면에 표시된 차세대 API에 맞춰 공매 수집기를 전환했습니다.

- Base URL: `https://apis.data.go.kr/B010003`
- 서비스: `OnbidRlstListSrvc2`
- 오퍼레이션: `getRlstCltrList2`
- 응답형식: `resultType=json`

기존 `ThingInfoInquireSvc/getUnifyUsageCltr`는 더 이상 기본 호출에 사용하지 않습니다. 기존
`config.yaml`에 구 API 주소가 남아 있어도 프로그램이 실행 시 자동으로 차세대 API로 전환합니다.

## 연결 점검

```bash
.venv/bin/python -I -m landwatch.cli connection-check --target 공매
```

정상 연결 시 `service`가 다음처럼 표시되어야 합니다.

```text
OnbidRlstListSrvc2/getRlstCltrList2
```
