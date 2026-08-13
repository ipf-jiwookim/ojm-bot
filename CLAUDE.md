# ojm-bot

마곡 인근 식당 3곳의 점심 메뉴를 슬랙에 자동 전송하는 단일 파일 파이썬 봇.
**무엇을 어떻게 판별하는지는 [README.md](README.md)에 있다. 여기엔 작업하다 걸리는 것만 적는다.**

## 슬랙 전송

운영 채널(`오늘-점심-모묵지`)로 바로 나간다. **사용자가 명시적으로 요청하기 전에는 보내지 않는다.**
로컬 실행은 반드시 `DRY_RUN=1` — 전송도 안 하고 전송 마커도 안 건드린다.

```bash
DRY_RUN=1 python3 lunch_bot.py   # ./dryrun/lunch.png 로 합친 이미지만 생성
python3 test_lunch_bot.py        # 격자 검출·요일 열 선택 자체 점검 (네트워크 불필요)
```

## 같은 날 재전송하려면 캐시 마커를 지워야 한다

두 번째 실행은 GitHub Actions 캐시 마커(`lunch-sent-YYYY-MM-DD`)에 걸려 조용히 스킵된다.
"코드를 고쳤는데 실행해도 아무 일이 없다"면 대개 이것이다.

```bash
gh cache list --limit 3
gh cache delete <id>
```

## 스케줄은 이 레포에 없다

GitHub cron은 누락이 잦아 제거했다. 외부 스케줄러(**val.town**)가 평일 10:30 KST에
`workflow_dispatch`로 트리거한다. 워크플로 파일을 고쳐도 실행 시각은 안 바뀐다.
공휴일 차단은 val.town과 봇 양쪽에 있고, 주말 차단은 봇에만 있다(수동 실행 대비).

val.town 스크립트는 아직 옛 레포 경로(`repos/ipf-jiwookim/babplus-lunch-bot/...`)를 호출한다.
GitHub 리네임 리다이렉트로 정상 동작하는 것을 확인했으므로(2026-08-13 실전 전송)
**그대로 둔다.** 굳이 건드리지 말 것. 누가 옛 이름으로 레포를 새로 만들면 그때 깨진다.

## 사랑해밥상 요일 판별을 뒤집지 말 것

어느 요일 칸인지는 **이미지를 읽지 않고** `published_at`(그 주 월요일) + `date.today().weekday()`로
정한다. OCR은 거부권만 갖는다 — 유효한 날짜인데 7일 이상 어긋날 때만 크롭을 포기한다.

과거 25주 실측: 게시 시각 기준 **25/25**, 날짜 OCR 기준 **21/25**. OCR을 판별에 쓰면 퇴보다.

격자 검출이나 크롭을 건드렸으면 과거 게시물 전체로 회귀 검증한다. 카카오 posts API의
페이지네이션 파라미터는 `since=<직전 페이지 마지막 item의 sort 값>`이다
(SPA 번들에서 확인. `limit`/`page`/`offset`/`cursor`는 전부 무시된다):

```bash
curl -s -H 'User-Agent: Mozilla/5.0' -H 'Referer: https://pf.kakao.com/' \
  'https://pf.kakao.com/rocket-web/web/profiles/_xerxkxen/posts?since=<sort>'
```

## 외부 요청 헤더

- **카카오**: `User-Agent` + `Referer: https://pf.kakao.com/`
- **네이버**: 위 둘만으로는 **429**가 온다. `Accept-Language: ko-KR,ko;q=0.9`까지 있어야 200.
  검색·반경 API는 캡차(`ncaptcha`)로 막혀 있다. 쓸 수 있는 건 플레이스 상세/소식 SSR뿐.

## 한글 폰트가 없으면 조용히 폴백한다

카드의 식당명 띠에 한글을 그린다. 러너에 `fonts-nanum`이 필요하고
(`.github/workflows/lunch.yml`의 apt 설치 줄), 못 찾으면 **카드를 건너뛰고 원본을 보낸다**.
띠가 사라졌으면 폰트를 먼저 의심할 것.

## 코드 관례

- 단일 파일(`lunch_bot.py`) 유지. 식당 추가는 `fetch_*` 함수 하나 + `SOURCES` 항목 하나.
- 수집 함수는 예외를 던지지 말고 `(None, "안내문")`을 돌려준다. 한 곳이 죽어도 나머지는 보낸다.
- 주석·로그·커밋 메시지는 한국어. 주석은 "왜 이렇게 했는지"를 적는다(실측치가 있으면 숫자로).
