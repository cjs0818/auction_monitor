# 온비드 HTTPS 인증서 검증 오류 수정

## 확인된 원인

차세대 온비드 API 엔드포인트와 인증키 승인 상태는 정상이며, 다음 오류는 API 인증 오류가 아닙니다.

```text
SSLCertVerificationError: self-signed certificate in certificate chain
```

Python Requests가 사용하는 정적 CA 번들이 macOS 키체인 또는 기관/VPN/보안프로그램이 설치한 루트 인증서를 신뢰하지 못할 때 발생합니다.

## 수정 내용

- macOS 시스템 키체인을 사용하는 `truststore` 적용
- `truststore`를 사용할 수 없는 구형/격리 Python에서는 macOS 키체인의 공개 인증서만 추출해 CA 번들 자동 생성
- 사용자 지정 PEM CA 번들 경로 지원
- SSL 인증서 검증은 항상 유지하며 `verify=False`는 사용하지 않음
- 오류 메시지와 로그에서 `serviceKey` 제거
- 연결 점검 결과에 사용 중인 TLS 신뢰 방식 표시

## 설치

기존 프로그램에 패치를 적용한 뒤 다음을 실행합니다.

```bash
cd /Users/jschoi/work/land_auction_monitor
./scripts/setup_mac.sh
```

또는 현재 환경에 의존성만 설치합니다.

```bash
.venv/bin/python -I -m pip install "truststore>=0.10,<1"
```

## 대시보드 설정

`수집·알림 설정 → 온비드 공매 설정`에서 다음을 확인합니다.

- `macOS 시스템 인증서 사용`: 켬
- `사용자 CA 인증서 번들 경로`: 일반적으로 비워 둠

기관에서 별도 루트 인증서 PEM 파일을 제공했다면 그 파일의 절대경로를 입력합니다.

## 연결 점검

```bash
.venv/bin/python -I -m landwatch.cli connection-check --target 공매
```

정상 연결 시 TLS 방식이 다음 중 하나로 표시됩니다.

- `system-truststore`
- `macos-keychain-bundle`
- `custom-ca-bundle`

## 보안 안내

공개된 로그나 PDF에 인증키 원문이 포함되었다면 공공데이터포털에서 인증키를 재발급한 뒤 새 키를 저장하십시오.
