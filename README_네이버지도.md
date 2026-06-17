# 네이버 지도 기본 앱 변경

경매물건 상세 팝업의 기본 지도 공급자를 Google 지도에서 네이버 지도로 변경합니다.

## 변경 내용

- 지도 탭 이름을 `네이버 지도`로 변경
- 기본 실행 버튼을 `네이버 지도에서 위치 열기`로 변경
- Google 지도는 보조 링크로 이동
- 카카오맵도 보조 링크로 유지
- 네이버 지도 Maps JavaScript API Client ID를 설정하면 팝업 내부에 대화형 네이버 지도와 마커 표시
- Client ID가 없더라도 네이버 지도 외부 열기 기능은 정상 동작

## 적용

실행 중인 Streamlit을 `Ctrl+C`로 종료한 후:

```bash
chmod +x apply_navermap_update.sh
./apply_navermap_update.sh /Users/jschoi/work/land_auction_monitor
```

다시 실행:

```bash
cd /Users/jschoi/work/land_auction_monitor
.venv/bin/streamlit run app.py
```

## 팝업 내부 네이버 지도 사용(선택)

대시보드에서 다음 순서로 입력합니다.

1. `수집·알림 설정`
2. `지도 설정`
3. `네이버 지도 Maps JavaScript API Client ID`
4. `수집 설정 저장`

네이버 Cloud Maps 애플리케이션의 Web 서비스 URL에는 로컬 실행 주소인 `http://localhost:8501`을 등록합니다. Client ID가 비어 있으면 지도 탭에서 네이버 지도 외부 열기 버튼을 기본으로 제공합니다.
