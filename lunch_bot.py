#!/usr/bin/env python3
"""
밥플러스 마곡디어반(17호점) 카카오 채널의 "오늘 중식 메뉴판" 이미지를
슬랙 채널로 전송한다. (헤더 코멘트 + 메뉴판 이미지 1장 업로드)

이미지는 슬랙이 URL을 직접 가져오는 image block 방식이 불안정하므로,
바이트를 다운로드해 files_upload 방식으로 채널에 직접 업로드한다.

- 오늘 중식 게시물이 없으면 아무것도 보내지 않고 정상 종료.
- 메뉴판(글자 카드)은 음식 사진과 달리 OCR로 한글이 많이 잡힌다.
  같은 게시물에 '석식 미운영' 등 다른 안내 카드가 섞일 수 있으므로
  '중식'에 가산점, '석식/미운영/휴무' 안내에는 감점을 준다.

환경변수:
  SLACK_BOT_TOKEN    (필수) xoxb- 봇 토큰 (files:write, chat:write)
  SLACK_CHANNEL_ID   (필수) 전송할 채널 ID (예: C0BDDENKY3B)
  CHANNEL_ID         (선택) 카카오 채널 ID, 기본 _HGxjan
"""
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

from PIL import Image
import pytesseract
import holidays

KAKAO_CH = os.environ.get("CHANNEL_ID", "_HGxjan")
API = f"https://pf.kakao.com/rocket-web/web/profiles/{KAKAO_CH}/posts"
# DRY_RUN: 슬랙으로 실제 전송하지 않고 동작만 확인 (수동 테스트용)
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
TOKEN = os.environ.get("SLACK_BOT_TOKEN", "" if DRY_RUN else None)
SLACK_CH = os.environ.get("SLACK_CHANNEL_ID", "" if DRY_RUN else None)
if TOKEN is None or SLACK_CH is None:
    raise SystemExit("SLACK_BOT_TOKEN / SLACK_CHANNEL_ID 환경변수가 필요합니다.")
KAKAO_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://pf.kakao.com/"}
KST = timezone(timedelta(hours=9))
NEG_WORDS = ("석식", "미운영", "휴무", "운영안", "운영 안")
MIN_BOARD_SCORE = 8  # 이 점수 미만이면 메뉴판으로 신뢰하지 않음(음식 사진 오선택 방지)


def http_get(url, headers=None, binary=False, retries=3):
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or {})
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read() if binary else r.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * attempt)
    raise last


def slack_api(method, data, get=False):
    url = f"https://slack.com/api/{method}"
    if get:
        url += "?" + urllib.parse.urlencode(data)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    else:
        req = urllib.request.Request(
            url,
            data=urllib.parse.urlencode(data).encode(),
            headers={
                "Authorization": f"Bearer {TOKEN}",
                "Content-type": "application/x-www-form-urlencoded",
            },
        )
    res = json.loads(urllib.request.urlopen(req, timeout=20).read())
    if not res.get("ok"):
        raise RuntimeError(f"{method} 실패: {res.get('error')}")
    return res


def post_text(text):
    if DRY_RUN:
        print(f"[DRY_RUN] 텍스트 전송 생략:\n  {text}")
        return
    slack_api("chat.postMessage", {"channel": SLACK_CH, "text": text})


def upload_image(raw, title, comment):
    if DRY_RUN:
        print(f"[DRY_RUN] 이미지 전송 생략: title={title!r}, {len(raw)//1024}KB, comment={comment!r}")
        return
    r = slack_api("files.getUploadURLExternal",
                  {"filename": "jungsik.jpg", "length": len(raw)}, get=True)
    upload_url, file_id = r["upload_url"], r["file_id"]
    req = urllib.request.Request(upload_url, data=raw,
                                 headers={"Content-Type": "application/octet-stream"})
    urllib.request.urlopen(req, timeout=20).read()
    slack_api("files.completeUploadExternal", {
        "files": json.dumps([{"id": file_id, "title": title}]),
        "channel_id": SLACK_CH,
        "initial_comment": comment,
    })


def pick_menu_board(media):
    """OCR 점수로 메뉴판 카드 선택 (중식 가산, 석식/미운영 감점)."""
    best, best_score = None, -10**9
    for idx, m in enumerate(media, 1):
        url = (m.get("medium_url") or m.get("url") or "").replace("http://", "https://")
        if not url:
            continue
        try:
            raw = http_get(url, KAKAO_HEADERS, binary=True)
            text = pytesseract.image_to_string(Image.open(io.BytesIO(raw)), lang="kor")
        except Exception:  # noqa: BLE001
            text, raw = "", None
        flat = text.replace(" ", "")
        hangul = sum(1 for ch in flat if "가" <= ch <= "힣")  # 한글 글자 수
        score = hangul
        has_js = "중식" in flat
        neg = next((w for w in NEG_WORDS if w in flat), None)
        if has_js:
            score += 60
        if neg:
            score -= 200
        print(f"  [p{idx}] score={score} (한글{hangul}, 중식={has_js}, neg={neg}) {url.rsplit('/dn/',1)[-1][:24]}")
        if score > best_score:
            best, best_score = (m, raw), score
    return (best[0], best[1], best_score) if best else (None, None, best_score)


def mark_sent(today):
    """오늘 메뉴 전송 완료 마커 기록 (재시도 실행의 중복 전송 방지)."""
    if DRY_RUN:
        return  # 테스트 실행이 실제 예약 실행의 캐시 마커를 오염시키지 않도록
    marker = os.environ.get("SENT_MARKER")
    if marker:
        with open(marker, "w") as f:
            f.write(today.strftime("%Y-%m-%d"))


def already_sent():
    """이전 실행(예: 11:30)에서 오늘 메뉴를 이미 보냈는지."""
    marker = os.environ.get("SENT_MARKER")
    return bool(marker and os.path.exists(marker))


def main():
    today = datetime.now(KST)
    if DRY_RUN:
        print("[DRY_RUN] 슬랙 전송 없이 동작만 확인합니다.")
    # 같은 날 재시도(12:00) 실행인데 11:30에 이미 메뉴를 보냈으면 중복 방지
    if not DRY_RUN and already_sent():
        print("[*] 오늘 메뉴 이미 전송됨 → 재시도 생략")
        return
    # 공휴일이면 전송 생략 (대체공휴일·임시공휴일 포함)
    kr_holidays = holidays.SouthKorea(years=[today.year])
    if today.date() in kr_holidays:
        print(f"[*] 오늘은 공휴일({kr_holidays.get(today.date())}) → 전송 생략")
        return
    md = f"{today.month}/{today.day}"
    label = f"{md}({'월화수목금토일'[today.weekday()]})"
    print(f"[*] 오늘(KST): {today:%Y-%m-%d} | 매칭: '{md}' + '중식'")

    # 제목은 항상 'M/D(요일) ...' 형식 → 여는 괄호까지 붙여 부분문자열 오매칭 방지
    #   (예: 6/2 가 6/26 게시물에 잘못 매칭되는 문제)
    date_key = f"{md}("
    items = json.loads(http_get(API, KAKAO_HEADERS)).get("items", [])
    hit = next((it for it in items
                if date_key in (it.get("title") or "") and "중식" in (it.get("title") or "")), None)
    if not hit:
        print("[*] 오늘 중식 게시물 없음 → 안내 메시지 전송")
        post_text(f"🍱 오늘({label}) 중식 메뉴가 아직 등록되지 않았어요. "
                  f"<https://pf.kakao.com/{KAKAO_CH}/posts|채널에서 직접 확인하기>")
        return
    media = hit.get("media") or []
    if not media:
        print("[*] 이미지 없음 → 안내 메시지 전송")
        post_text(f"🍱 오늘({label}) 중식 게시물은 올라왔지만 이미지가 없어요. "
                  f"<{hit.get('permalink', '')}|게시물 보기>")
        return

    print(f"[*] 게시물 '{hit.get('title','').strip()}' 이미지 {len(media)}장 분석:")
    board, raw, score = pick_menu_board(media)
    sel = (board.get("medium_url") or board.get("url") or "").replace("http://", "https://") if board else None
    print(f"[*] 선택된 메뉴판: score={score} {sel}")

    # 신뢰도 미달(메뉴판을 못 찾음) → 음식 사진 오선택 대신 안내 메시지
    if raw is None or score < MIN_BOARD_SCORE:
        print(f"[*] 메뉴판 신뢰도 부족(score={score} < {MIN_BOARD_SCORE}) → 안내 메시지 전송")
        post_text(f"🍱 오늘({label}) 중식 게시물은 있으나 메뉴판 이미지를 찾지 못했어요. "
                  f"<{hit.get('permalink', '')}|게시물에서 직접 확인하기>")
        return

    # 선별은 medium OCR, 전송은 고화질(xlarge)로 — 글씨가 또렷하게
    big_url = (board.get("xlarge_url") or board.get("large_url")
               or board.get("medium_url") or board.get("url")).replace("http://", "https://")
    try:
        raw = http_get(big_url, KAKAO_HEADERS, binary=True)
        print(f"[*] 전송 화질 업그레이드: {board.get('width')}x{board.get('height')} ({len(raw)//1024}KB)")
    except Exception as e:  # noqa: BLE001
        print(f"[!] xlarge 다운로드 실패({e}) → medium 으로 전송")

    upload_image(raw, "중식 메뉴판", f"🍱 오늘의 중식 — {label}")
    mark_sent(today)
    print("[*] 전송 완료")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"[!] 오류: {e}", file=sys.stderr)
        try:
            post_text(f"⚠️ 오늘 중식 메뉴 자동 전송 실패: {e}")
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
