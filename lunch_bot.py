#!/usr/bin/env python3
"""
밥플러스 마곡디어반(17호점) 카카오 채널의 "오늘 중식 메뉴판" 이미지를
슬랙 채널(밥플러스-오뭐먹)로 전송한다. (헤더 + 메뉴판 이미지 1장만)

- 오늘 중식 게시물이 없으면 아무것도 보내지 않고 정상 종료.
- 메뉴판(글자 카드)은 음식 사진과 달리 OCR로 텍스트가 많이 잡히므로,
  게시물 이미지 중 한글 텍스트가 가장 많이 인식되는 것을 메뉴판으로 본다.

환경변수:
  SLACK_WEBHOOK_URL  (필수) 슬랙 Incoming Webhook URL
  CHANNEL_ID         (선택) 카카오 채널 ID, 기본 _HGxjan
"""
import io
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone, timedelta

from PIL import Image
import pytesseract

CHANNEL = os.environ.get("CHANNEL_ID", "_HGxjan")
API = f"https://pf.kakao.com/rocket-web/web/profiles/{CHANNEL}/posts"
HOOK = os.environ["SLACK_WEBHOOK_URL"]
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://pf.kakao.com/"}
KST = timezone(timedelta(hours=9))


def http_get(url, binary=False, retries=3):
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read() if binary else r.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * attempt)
    raise last


def post_slack(payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        HOOK, data=data, headers={"Content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8")


def notify_error(msg):
    try:
        post_slack({"text": f"⚠️ 오늘 중식 메뉴 자동 전송 실패: {msg}"})
    except Exception:  # noqa: BLE001
        pass


def pick_menu_board(media):
    """이미지 중 OCR 한글 텍스트가 가장 많은 것을 메뉴판으로 선택."""
    best, best_score = None, -1
    for m in media:
        url = (m.get("medium_url") or m.get("url") or "").replace("http://", "https://")
        if not url:
            continue
        try:
            raw = http_get(url, binary=True)
            text = pytesseract.image_to_string(Image.open(io.BytesIO(raw)), lang="kor")
        except Exception:  # noqa: BLE001
            text = ""
        # 한글 글자 수로 점수화 (음식 사진은 거의 0)
        score = sum(1 for ch in text if "가" <= ch <= "힣")
        # "중식" 배지가 보이면 가산점
        if "중식" in text.replace(" ", ""):
            score += 50
        if score > best_score:
            best, best_score = m, score
    return best, best_score


def main():
    today = datetime.now(KST)
    md = f"{today.month}/{today.day}"  # 6/26
    weekday = "월화수목금토일"[today.weekday()]
    label = f"{md}({weekday})"
    print(f"[*] 오늘(KST): {today:%Y-%m-%d} | 매칭: '{md}' + '중식'")

    data = json.loads(http_get(API))
    items = data.get("items", [])

    hit = next(
        (it for it in items if md in (it.get("title") or "") and "중식" in (it.get("title") or "")),
        None,
    )
    if not hit:
        print("[*] 오늘 중식 게시물 없음 → 전송 생략")
        return

    media = hit.get("media") or []
    if not media:
        print("[*] 이미지 없음 → 전송 생략")
        return

    board, score = pick_menu_board(media)
    print(f"[*] 메뉴판 선택 score={score}")
    if not board or score < 5:
        # OCR로 메뉴판을 못 찾으면 첫 이미지로 폴백
        board = media[0]

    img = (board.get("medium_url") or board.get("url")).replace("http://", "https://")
    payload = {
        "text": f"🍱 오늘의 중식 — {label}",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"🍱 오늘의 중식 — {label}", "emoji": True}},
            {"type": "image", "image_url": img, "alt_text": "중식 메뉴판"},
        ],
    }
    res = post_slack(payload)
    print(f"[*] 슬랙 응답: {res}")
    if res.strip() != "ok":
        raise RuntimeError(f"슬랙 전송 실패: {res}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"[!] 오류: {e}", file=sys.stderr)
        notify_error(str(e))
        sys.exit(1)
