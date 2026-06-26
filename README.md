# babplus-lunch-bot

밥플러스 마곡디어반(17호점) 카카오톡 채널의 **오늘 중식 메뉴판** 이미지를
평일 오전 9시(KST)에 슬랙 `밥플러스-오뭐먹` 채널로 자동 전송한다.
(헤더 한 줄 + 메뉴판 이미지 1장만)

## 동작
1. 카카오 채널 posts API 조회
2. 제목이 `오늘 날짜(M/D) + 중식`인 게시물 탐색 (없으면 전송 생략)
3. 게시물 이미지 중 OCR로 한글 텍스트가 가장 많은 것을 메뉴판으로 선별 (음식 사진 제외)
4. 슬랙 Incoming Webhook으로 전송 (`medium_url` 사용 — xlarge는 슬랙이 거부)

## 실행
- 자동: GitHub Actions cron `3 0 * * 1-5` (UTC) = 평일 09:03 KST
- 수동: Actions 탭 → "밥플러스 점심 메뉴 슬랙 알림" → Run workflow

## 설정 (Secrets)
| 이름 | 설명 |
|------|------|
| `SLACK_WEBHOOK_URL` | 슬랙 Incoming Webhook URL |

채널/대상을 바꾸려면 `CHANNEL_ID` 환경변수(기본 `_HGxjan`)를 워크플로우에 추가하면 된다.
