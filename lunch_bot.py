#!/usr/bin/env python3
"""
마곡 인근 식당 3곳의 "오늘 점심 메뉴" 이미지를 슬랙 채널 한 메시지로 전송한다.
(헤더 코멘트 1줄 + 식당별 이미지 카드 최대 3장을 한 번에 업로드)

  1) 밥플러스 마곡디어반 (카카오 채널)  — 날짜 매칭 후 중식 메뉴판을 이미지 판독으로 선별
  2) 사랑해밥상 마곡      (카카오 채널)  — 주간 식단표에서 오늘 요일 칸만 잘라냄
  3) 밥짓는부엌           (네이버 플레이스 소식) — 하루 1장, 중식/석식 구분 없음

이미지는 슬랙이 URL을 직접 가져오는 image block 방식이 불안정하므로,
바이트를 다운로드해 files_upload 방식으로 채널에 직접 업로드한다.

식당 3곳이 한 메시지에 섞이므로 각 이미지에 '식당명 + 날짜' 띠와 테두리를 구워
카드로 만든다. 슬랙 렌더링·모바일 알림 미리보기에 의존하지 않고 구분되게 하기 위함.

한 곳이 실패해도 나머지는 보낸다. 실패한 곳은 코멘트에 안내 한 줄로 남는다.

환경변수:
  SLACK_BOT_TOKEN    (필수) xoxb- 봇 토큰 (files:write, chat:write)
  SLACK_CHANNEL_ID   (필수) 전송할 채널 ID (예: C0BDDENKY3B)
  CHANNEL_ID         (선택) 밥플러스 카카오 채널 ID, 기본 _HGxjan
  DRY_RUN            (선택) 1이면 슬랙 전송 없이 ./dryrun/ 에 카드 이미지만 저장
"""
import io
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

from PIL import Image, ImageDraw, ImageFont
import pytesseract
import holidays

KAKAO_CH = os.environ.get("CHANNEL_ID", "_HGxjan")
API = f"https://pf.kakao.com/rocket-web/web/profiles/{KAKAO_CH}/posts"
SARANG_CH = "_xerxkxen"                       # 사랑해밥상 마곡 뉴브클라우드힐스
SARANG_API = f"https://pf.kakao.com/rocket-web/web/profiles/{SARANG_CH}/posts"
BABJIT_PLACE = "1578060862"                   # 밥짓는부엌 네이버 플레이스 ID
BABJIT_FEED = f"https://pcmap.place.naver.com/restaurant/{BABJIT_PLACE}/feed"
BABJIT_LINK = (f"https://map.naver.com/p/entry/place/{BABJIT_PLACE}"
               "?placePath=%2Ffeed")
# DRY_RUN: 슬랙으로 실제 전송하지 않고 동작만 확인 (수동 테스트용)
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
TOKEN = os.environ.get("SLACK_BOT_TOKEN", "" if DRY_RUN else None)
SLACK_CH = os.environ.get("SLACK_CHANNEL_ID", "" if DRY_RUN else None)
if TOKEN is None or SLACK_CH is None:
    raise SystemExit("SLACK_BOT_TOKEN / SLACK_CHANNEL_ID 환경변수가 필요합니다.")
KAKAO_HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://pf.kakao.com/"}
# 네이버는 UA만 있는 요청을 429로 막는다. Accept-Language·Referer까지 있어야 200이 온다.
NAVER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://map.naver.com/",
}
# 카드 띠 색 (식당 구분용)
COLOR_BABPLUS, COLOR_SARANG, COLOR_BABJIT = "#2f6f3e", "#c2410c", "#1e3a8a"
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


SHEET_W, SHEET_GAP, SHEET_PAD = 1480, 22, 22


def make_sheet(rows):
    """카드 여러 장을 한 장으로 합친다. 슬랙에 이미지를 여러 개 붙이면 공통 비율로
    가운데만 남기고 잘라내기 때문에(띠·가장자리가 날아감) 아예 한 장으로 만든다.

    rows = [[카드 bytes, ...], ...] — 한 줄에 무엇을 놓을지는 SHEET_ROWS 가 정한다.
    카드 비율로 배치를 정하던 때는 사랑해밥상 크롭이 실패해 표 통짜(가로형)가 들어온 날
    세 장이 세로로 쭉 늘어섰다(2026-09-29). 배치는 식당 기준으로 고정한다."""
    rows = [[Image.open(io.BytesIO(raw)).convert("RGB") for raw in row] for row in rows]
    strips = []
    for row in rows:
        if len(row) == 1:
            im = row[0]
            # 세로형이 혼자 한 줄을 다 쓰면 이미지가 쓸데없이 길어진다(짝이 없는 날).
            w = SHEET_W if im.width / im.height >= 0.95 else round(SHEET_W * 0.62)
            im = im.resize((w, round(im.height * w / im.width)), Image.LANCZOS)
            strip = Image.new("RGB", (SHEET_W, im.height), "white")
            strip.paste(im, ((SHEET_W - w) // 2, 0))
            strips.append(strip)
            continue
        h = max(i.height for i in row)                       # 높이 맞춘 뒤
        row = [i.resize((round(i.width * h / i.height), h), Image.LANCZOS) for i in row]
        s = (SHEET_W - SHEET_GAP) / sum(i.width for i in row)  # 줄 폭에 맞춰 축소
        row = [i.resize((round(i.width * s), round(i.height * s)), Image.LANCZOS) for i in row]
        strip = Image.new("RGB", (SHEET_W, max(i.height for i in row)), "white")
        x = 0
        for i in row:
            strip.paste(i, (x, 0))
            x += i.width + SHEET_GAP
        strips.append(strip)

    h = SHEET_PAD * 2 + sum(s.height for s in strips) + SHEET_GAP * (len(strips) - 1)
    sheet = Image.new("RGB", (SHEET_W + SHEET_PAD * 2, h), "white")
    y = SHEET_PAD
    for s in strips:
        sheet.paste(s, (SHEET_PAD, y))
        y += s.height + SHEET_GAP
    buf = io.BytesIO()
    sheet.save(buf, "PNG")
    return buf.getvalue()


def upload_images(cards, comment):
    """[(raw, filename, title), ...] 를 한 메시지에 묶어 업로드한다.
    files.completeUploadExternal 이 배열을 받으므로 이미지 N장이 메시지 1개로 붙는다."""
    if DRY_RUN:
        os.makedirs("dryrun", exist_ok=True)
        print(f"[DRY_RUN] 메시지 1건 전송 생략: comment={comment!r}")
        for raw, fn, title in cards:
            open(os.path.join("dryrun", fn), "wb").write(raw)
            print(f"          + {title} ({len(raw)//1024}KB) → dryrun/{fn}")
        return
    ids = []
    for raw, fn, title in cards:
        r = slack_api("files.getUploadURLExternal",
                      {"filename": fn, "length": len(raw)}, get=True)
        req = urllib.request.Request(r["upload_url"], data=raw,
                                     headers={"Content-Type": "application/octet-stream"})
        urllib.request.urlopen(req, timeout=20).read()
        ids.append({"id": r["file_id"], "title": title})
    slack_api("files.completeUploadExternal", {
        "files": json.dumps(ids),
        "channel_id": SLACK_CH,
        "initial_comment": comment,
    })


# 카드 규격: 식당·원본 해상도와 무관하게 띠 높이·글자 크기를 동일하게 맞춘다.
CARD_W, BAND_H, BAND_FONT, EDGE = 720, 88, 50, 8
FONT_PATHS = (
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",    # ubuntu: fonts-nanum
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",    # ubuntu: fonts-noto-cjk
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",             # macOS 로컬 테스트
)


def make_card(raw, text, color):
    """이미지에 '식당명 · 날짜' 띠와 테두리를 굽는다.
    슬랙이 여러 장을 어떻게 렌더하든(모바일 알림 미리보기 포함) 출처가 따라붙게 하려는 것.
    폰트를 못 찾는 등 실패하면 원본 바이트를 그대로 돌려준다 — 메뉴는 나가야 하므로."""
    try:
        font = next((ImageFont.truetype(p, BAND_FONT) for p in FONT_PATHS
                     if os.path.exists(p)), None)
        if font is None:
            raise RuntimeError("한글 폰트 없음 (apt: fonts-nanum)")
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img = img.resize((CARD_W, round(img.height * CARD_W / img.width)), Image.LANCZOS)
        w, h = CARD_W + EDGE * 2, img.height + BAND_H + EDGE
        out = Image.new("RGB", (w, h), color)
        out.paste(img, (EDGE, BAND_H))
        d = ImageDraw.Draw(out)
        l, t, r, b = d.textbbox((0, 0), text, font=font)
        d.text(((w - (r - l)) / 2 - l, (BAND_H - (b - t)) / 2 - t), text, font=font, fill="white")
        buf = io.BytesIO()
        out.save(buf, "PNG")
        return buf.getvalue()
    except Exception as e:  # noqa: BLE001
        print(f"[!] 카드 렌더 실패({e}) → 원본 이미지로 전송")
        return raw


def theme_color(img):
    """메뉴판 테마색 비율(%) 추정. 밥플러스 중식판=초록 테두리/뱃지, 석식판=분홍.
    '중식'/'석식' 글자가 색 뱃지 안 흰 글씨라 OCR로 안 잡히므로 색으로 보강한다."""
    im = img.convert("RGB")
    w, h = im.size
    px = im.load()
    green = pink = total = 0
    for y in range(0, h, 4):
        for x in range(0, w, 4):
            r, g, b = px[x, y]
            total += 1
            if g > 90 and g > r + 25 and g > b + 25:        # 초록
                green += 1
            if r > 150 and b > 90 and r > g + 30 and b > g - 10:  # 분홍/마젠타
                pink += 1
    if not total:
        return 0.0, 0.0
    return green * 100 / total, pink * 100 / total


def _longest_run(counts, ok, max_gap=2):
    """counts(dict, 정수 키)에서 ok(v)가 참인 키의 최장 연속 구간 (작은 gap 허용)."""
    best = (0, -1)
    cur = last = None
    gap = 0
    for k in sorted(counts):
        if ok(counts[k]):
            if cur is None:
                cur = k
            last = k
            gap = 0
        elif cur is not None:
            gap += 1
            if gap > max_gap:
                if last - cur > best[1] - best[0]:
                    best = (cur, last)
                cur = None
    if cur is not None and last - cur > best[1] - best[0]:
        best = (cur, last)
    return best


def read_badge(img):
    """우상단 색 뱃지(빨강=중식/분홍=석식)의 글자를 OCR로 직접 판독.
    뱃지는 '꽉 찬 색면'이라, 같은 색의 얇은 테두리선과 구분하기 위해
    분홍/빨강이 빽빽한 행이 '연속으로 이어진' 최장 구간만 뱃지로 본다.
    실패 시 None (상위 색/한글 신호로 폴백)."""
    try:
        im = img.convert("RGB")
        w, h = im.size
        px = im.load()
        x0, x1 = int(w * 0.55), int(w * 0.97)
        y0, y1 = int(h * 0.04), int(h * 0.26)

        def is_red(p):  # 빨강·분홍 공통(붉은기 강함)
            r, g, b = p
            return r > 150 and r > g + 35

        rowcnt = {y: sum(1 for x in range(x0, x1) if is_red(px[x, y]))
                  for y in range(y0, y1)}
        peak = max(rowcnt.values()) if rowcnt else 0
        if peak < max(8, (x1 - x0) // 12):   # 뱃지로 보기엔 색면이 너무 작음
            return None
        ya, yb = _longest_run(rowcnt, lambda c: c >= peak * 0.5)
        if yb < ya:
            return None
        colcnt = {x: sum(1 for y in range(ya, yb + 1) if is_red(px[x, y]))
                  for x in range(x0, x1)}
        cpeak = max(colcnt.values())
        xa, xb = _longest_run(colcnt, lambda c: c >= cpeak * 0.5)
        pad = 4
        box = (max(xa - pad, 0), max(ya - pad, 0),
               min(xb + pad, w), min(yb + pad, h))
        badge = im.crop(box)
        bw, bh = badge.size
        big = badge.resize((bw * 5, bh * 5), Image.LANCZOS)  # 확대로 작은 글자 보강
        bpx = big.load()
        for y in range(big.size[1]):                          # 흰 글씨 → 검정, 색면 → 흰
            for x in range(big.size[0]):
                r, g, b = bpx[x, y]
                bpx[x, y] = (0, 0, 0) if (r > 160 and g > 160 and b > 160) else (255, 255, 255)
        for psm in (8, 13, 7):   # 단어/단일줄 모드 우선
            t = pytesseract.image_to_string(big, config=f"--psm {psm} -l kor")
            t = t.replace(" ", "").replace("\n", "")
            if "중식" in t:
                return "중식"
            if "석식" in t:
                return "석식"
        return None
    except Exception:  # noqa: BLE001
        return None


def pick_menu_board(media):
    """메뉴판 카드 선택. OCR(중식 글자) + 테마색(초록=중식/분홍=석식)으로 점수화."""
    best, best_score = None, -10**9
    for idx, m in enumerate(media, 1):
        url = (m.get("medium_url") or m.get("url") or "").replace("http://", "https://")
        if not url:
            continue
        img = None
        try:
            raw = http_get(url, KAKAO_HEADERS, binary=True)
            img = Image.open(io.BytesIO(raw))
            text = pytesseract.image_to_string(img, lang="kor")
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
        # 테마색 보강: OCR이 뱃지 글자를 못 읽어도 초록/분홍으로 중식/석식 구분
        green_pct, pink_pct = theme_color(img) if img is not None else (0.0, 0.0)
        theme = None
        if green_pct >= 1 and green_pct > pink_pct:
            score += 80
            theme = "중식(초록)"
        elif pink_pct >= 1 and pink_pct > green_pct:
            score -= 200
            theme = "석식(분홍)"
        # 뱃지 글자 직접 판독(최우선): 중식 확정 가산 / 석식 확정 제외
        badge = read_badge(img) if img is not None else None
        if badge == "중식":
            score += 200
        elif badge == "석식":
            score -= 1000
        print(f"  [p{idx}] score={score} (한글{hangul}, 중식={has_js}, neg={neg}, "
              f"뱃지={badge}, 색={theme} g{green_pct:.0f}/p{pink_pct:.0f}) {url.rsplit('/dn/',1)[-1][:24]}")
        if score > best_score:
            best, best_score = (m, raw), score
    return (best[0], best[1], best_score) if best else (None, None, best_score)


# ─────────────────────────── 사랑해밥상 (주간 식단표 → 오늘 칸 크롭) ───────────────────────────

def _grid_lines(rng, scan, get, ratio, thr=190, gap=3):
    """rng 방향으로 훑어 scan 구간이 ratio 이상 어두운 좌표를 격자선으로 모은다."""
    n = len(scan)
    out = []
    for i in rng:
        if sum(1 for j in scan if get(i, j) < thr) >= n * ratio:
            if out and i - out[-1][-1] <= gap:
                out[-1].append(i)
            else:
                out.append([i])
    # 두께가 8px를 넘으면 선이 아니라 칠해진 띠(샐러드바 남색 행)다. 중심 하나로 줄이면
    # 띠 아래 경계가 사라진다 — 2026-09-28자 표는 목·금 칸이 비어 남색 행 전체(446~469px)가
    # 한 덩어리로 잡혔고, 가로선이 5개로 줄어 표 판정에 실패했다. 위·아래 경계를 둘 다 낸다.
    return [y for g in out
            for y in ((g[0], g[-1]) if g[-1] - g[0] > 8 else ((g[0] + g[-1]) // 2,))]


def table_grid(im):
    """주간 식단표의 격자선 검출 → (세로선 6개, 가로 대구획선). 표가 아니면 (None, None).

    세로선이 정확히 6개(=요일 5칸)일 때만 표로 인정하므로, 이 검출 자체가
    '주간 식단표인가' 판별기 역할을 겸한다(공지·이벤트·일일메뉴 이미지는 여기서 걸러진다).
    가로선은 라벨열까지 통째로 가로지르는 것만 센다 — 샐러드바 내부 잔선을 세면
    행 개수가 이미지마다 6~9개로 들쭉날쭉해져 자를 위치를 못 잡는다."""
    g = im.convert("L")
    w, h = g.size
    px = g.load()
    # 왼쪽 바깥 테두리는 세지 않는다(라벨열 왼쪽 경계는 crop 때 x=0을 쓰므로 불필요).
    # 고정 3px였는데 2026-08-31자 표(1135px 폭)는 테두리가 정확히 x=3이라 살아남아
    # 세로선이 7개가 됐고 표 판정에 실패했다. 폭 대비 2%면 라벨열선(최소 9%)과 안 겹친다.
    v = [x for x in _grid_lines(range(w), list(range(int(h * .15), int(h * .55), 2)),
                                lambda x, y: px[x, y], .85) if x >= w * .02]
    if len(v) != 6:
        return None, None
    hl = [y for y in _grid_lines(range(h), list(range(3, v[5] - 3, 4)),
                                 lambda y, x: px[x, y], .93) if 5 < y < h - 5]
    return (v, hl) if len(hl) >= 6 else (None, None)


def crop_weekday(im, v, hl, idx):
    """왼쪽 라벨열 + idx번 요일열을 이어붙인다(날짜행 ~ 샐러드바 끝).
    석식행이 요일별로 나뉘어 있으면(실제 운영) 포함하고,
    '석식은 운영하지 않습니다' 같은 통짜 안내 배너면 잘라낸다.

    샐러드바 끝은 선 순번이 아니라 간격으로 찾는다. 샐러드바 줄 수(2~4)와 잡히는 선 개수가
    주마다 달라서, hl[6]을 끝으로 보던 때는 08-31·09-07자 표에서 석식 배너가 딸려 나왔다.
    샐러드바 줄(~30px)은 점심행(~250px)의 1/3보다 한참 작고, 석식행(~180px)은 그보다 크다."""
    top, k = hl[0], 2
    while k + 1 < len(hl) and hl[k + 1] - hl[k] < (hl[2] - hl[1]) / 3:
        k += 1
    bot = hl[k]
    if k + 1 < len(hl):          # 석식행 아래 선이 잡혔을 때만 (로고가 가리면 못 잡는다)
        px = im.convert("L").load()
        ys = list(range(hl[k] + 4, hl[k + 1] - 4, 2))
        if ys and max(sum(1 for y in ys if px[x, y] < 190) / len(ys)
                      for x in range(v[2] - 6, v[2] + 7)) > 0.9:
            bot = hl[k + 1]
    lab = im.crop((0, top, v[0], bot))
    day = im.crop((v[idx], top, v[idx + 1], bot))
    out = Image.new("RGB", (lab.width + day.width, day.height), "white")
    out.paste(lab, (0, 0))
    out.paste(day, (lab.width, 0))
    return out


def date_cell_veto(im, v, hl, idx, today):
    """보낼 칸의 날짜를 OCR해 '명백히 다른 주'면 True(거부).

    열 선택 자체는 요일 인덱스로 하므로 OCR은 판단이 아니라 거부권만 갖는다.
    글자를 못 읽거나 달력에 없는 값(48월/71일 등)이면 그냥 통과시킨다 —
    과거 25주 125칸 측정에서 '유효한 날짜인데 7일 이상 어긋난' 오독은 0건이었고,
    ±1일 수준 오독은 5건 있었으므로 임계는 7일로 둔다."""
    try:
        c = im.crop((v[idx] + 6, hl[0] + 4, v[idx + 1] - 6, hl[1] - 3)).convert("L")
        c = c.point(lambda p: 0 if p < 150 else 255)
        c = c.resize((c.width * 6, c.height * 6), Image.LANCZOS)
        t = pytesseract.image_to_string(c, lang="kor", config="--psm 8").replace(" ", "")
        m = re.search(r"(\d{1,2})\D{1,3}(\d{1,2})\D{0,3}$", t)
        if not m:
            return False
        got = datetime(today.year, int(m.group(1)), int(m.group(2))).date()
        gap = abs((got - today).days)
        print(f"  [사랑해밥상] 날짜 OCR={got} 오차={gap}일")
        return gap >= 7
    except Exception:  # noqa: BLE001
        return False


def fetch_sarang(today, label):
    """(카드 이미지 bytes, None) 또는 (None, 안내문). 주간 식단표에서 오늘 칸만."""
    link = f"<https://pf.kakao.com/{SARANG_CH}/posts|사랑해밥상 채널>"
    if today.weekday() > 4:      # 표는 월~금 5칸뿐이라 주말엔 자를 열이 없다
        return None, f"🥗 사랑해밥상 — 주말({label})은 식단표에 없어요. {link}"
    mon = today - timedelta(days=today.weekday())
    items = json.loads(http_get(SARANG_API, KAKAO_HEADERS)).get("items", [])
    # 이번 주 표인지는 게시 시각으로 판정한다. 표 안 날짜 글자를 읽는 것보다 확실하다
    # (과거 25주 전부 '게시 시각이 속한 주의 월요일 = 표의 시작일'이었다).
    post = None
    for it in items:
        if not it.get("media") or "메뉴" not in (it.get("title") or ""):
            continue
        pub = datetime.fromtimestamp(it["published_at"] / 1000, KST).date()
        if pub - timedelta(days=pub.weekday()) == mon:
            post = it
            break
    if not post:
        return None, f"🥗 사랑해밥상 — 이번 주 식단표가 아직 안 올라왔어요. {link}"

    m = post["media"][0]
    url = (m.get("xlarge_url") or m.get("large_url") or m.get("url")).replace("http://", "https://")
    im = Image.open(io.BytesIO(http_get(url, KAKAO_HEADERS, binary=True))).convert("RGB")
    permalink = (post.get("permalink") or "").replace("http://", "https://")
    v, hl = table_grid(im)
    if not v:
        print("[*] 사랑해밥상 격자 검출 실패 → 원본 통짜 전송")
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue(), None
    if date_cell_veto(im, v, hl, today.weekday(), today):
        print("[*] 사랑해밥상 날짜 거부권 발동 → 원본 통짜 전송")
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue(), None
    print(f"[*] 사랑해밥상 '{(post.get('title') or '').strip()}' "
          f"→ {today.weekday()}번 열 크롭 <{permalink}>")
    buf = io.BytesIO()
    crop_weekday(im, v, hl, today.weekday()).save(buf, "PNG")
    return buf.getvalue(), None


# ─────────────────────────── 밥짓는부엌 (네이버 플레이스 소식) ───────────────────────────

def fetch_babjit(today, label):
    """(이미지 bytes, None) 또는 (None, 안내문). 하루 1장, 중식/석식 구분 없음."""
    link = f"<{BABJIT_LINK}|밥짓는부엌 네이버 소식>"
    html = http_get(BABJIT_FEED, NAVER_HEADERS)
    # 피드 목록이 SSR HTML 안 __APOLLO_STATE__ 에 통째로 들어있어 별도 API 호출이 필요 없다.
    i = html.find("__APOLLO_STATE__")
    if i < 0:
        return None, f"🍚 밥짓는부엌 — 네이버 소식을 읽지 못했어요. {link}"
    state, _ = json.JSONDecoder().raw_decode(html[html.index("{", i):])
    # 제목은 '오늘의 메뉴'처럼 날짜가 빠지는 날이 있어 createdString(YYYYMMDD)으로 맞춘다.
    stamp = today.strftime("%Y%m%d")
    same_day = [v for k, v in state.items()
                if k.startswith("Feed:") and v.get("createdString") == stamp
                and not v.get("isDeleted") and (v.get("thumbnail") or {}).get("url")]
    # 하루에 메뉴판과 홍보글이 같이 올라오는 날이 있다(예: 7/15 '오늘의 메뉴' = 초복 삼계탕
    # 홍보 스톡사진이 먼저, 진짜 메뉴판이 나중에). 제목에 날짜가 박힌 쪽이 늘 실제 메뉴판이라
    # 그쪽을 먼저 집고, 동순위면 feedId가 큰 쪽(나중 게시)을 집는다.
    # feedId까지 보는 건 dict 순서에 기대지 않고 결정론적으로 고르기 위함.
    dated = re.compile(rf"{today.month}\s*월\s*{today.day}\s*일")
    same_day.sort(key=lambda v: (0 if dated.search(v.get("title") or "") else 1,
                                 -(v.get("feedId") or 0)))
    feed = same_day[0] if same_day else None
    if not feed:
        # 2026-08-24: 09:58 게시글이 10:32 실행에서 안 보였다(map.naver.com 에는 보였음).
        # '진짜 미게시'와 'SSR 응답이 스테일/깨짐'을 로그로 구분해 둔다. 다음에 또 나면
        # 최신 createdString 이 어제 날짜로 찍혀 있을 것이고, 그때 다른 엔드포인트를 붙인다.
        seen = sorted((v.get("createdString") or "") for k, v in state.items()
                      if k.startswith("Feed:"))
        print(f"[!] 밥짓는부엌 오늘({stamp}) 글 없음 — Feed {len(seen)}건, "
              f"최신 {seen[-1] if seen else '없음'}")
        if not seen:
            return None, f"🍚 밥짓는부엌 — 네이버 소식을 읽지 못했어요. {link}"
        return None, f"🍚 밥짓는부엌 — 오늘({label}) 메뉴가 아직 안 올라왔어요. {link}"
    print(f"[*] 밥짓는부엌 '{(feed.get('title') or '').strip()}' ({stamp}, 같은 날 {len(same_day)}건)")
    return http_get(feed["thumbnail"]["url"] + "?type=w1500", NAVER_HEADERS, binary=True), None


def mark_sent(today):
    """오늘 메뉴 전송 완료 마커 기록 (재시도 실행의 중복 전송 방지)."""
    if DRY_RUN:
        return  # 테스트 실행이 실제 예약 실행의 캐시 마커를 오염시키지 않도록
    marker = os.environ.get("SENT_MARKER")
    if marker:
        with open(marker, "w") as f:
            f.write(today.strftime("%Y-%m-%d"))


def already_sent():
    """이전 실행(예: 11:00)에서 오늘 메뉴를 이미 보냈는지."""
    marker = os.environ.get("SENT_MARKER")
    return bool(marker and os.path.exists(marker))


def fetch_babplus(today, label):
    """(이미지 bytes, None) 또는 (None, 안내문). 오늘 중식 메뉴판 1장."""
    md = f"{today.month}/{today.day}"
    # 제목의 날짜(M/D)만으로 매칭한다. 관리자가 게시 직후 아침에 제목의 '중식' 표기나
    # 요일 괄호를 다듬는 일이 있어(예: '7/8 중식메뉴' → '7/8(수) 중식메뉴') 그 틈에
    # 실행된 봇이 '중식'/괄호 요구 때문에 게시물을 통째로 놓친다. 날짜는 대체로 처음부터
    # 박혀 있으므로 날짜만 잡고, 중식/석식 구분은 이미지 판독(뱃지/색/OCR)에 맡긴다.
    #   - '/' 앞뒤 공백·전각(／)·앞자리 0 허용:  '7 / 8', '07/08', '7/8'
    #   - 숫자 경계로 오매칭 방지:  '6/2'가 '6/26'에, '7/8'이 '7/80'에 붙지 않게
    date_re = re.compile(rf"(?<!\d)0*{today.month}\s*[/／]\s*0*{today.day}(?!\d)")
    items = json.loads(http_get(API, KAKAO_HEADERS)).get("items", [])
    hits = [it for it in items if date_re.search(it.get("title") or "")]
    chan = f"<https://pf.kakao.com/{KAKAO_CH}/posts|밥플러스 채널>"
    if not hits:
        return None, f"🍱 밥플러스 — 오늘({label}) 중식 메뉴가 아직 등록되지 않았어요. {chan}"

    # 같은 날 중식·석식이 별도 게시물로 올라올 수 있으므로, 날짜가 맞는 모든 게시물의
    # 이미지를 한데 모아 점수화한다. pick_menu_board가 석식(분홍/뱃지/안내문)엔 큰 감점을
    # 주므로 중식 메뉴판이 자연스럽게 최고점으로 선택된다.
    permalink = next((it.get("permalink") for it in hits if it.get("permalink")), "")
    permalink = permalink.replace("http://", "https://")
    media = [m for it in hits for m in (it.get("media") or [])]
    titles = ", ".join(f"'{(it.get('title') or '').strip()}'" for it in hits)
    if not media:
        return None, f"🍱 밥플러스 — 오늘({label}) 게시물은 올라왔지만 이미지가 없어요. <{permalink}|게시물 보기>"

    print(f"[*] 밥플러스 날짜 매칭 게시물 {len(hits)}개({titles}) · 이미지 {len(media)}장 분석:")
    board, raw, score = pick_menu_board(media)
    sel = (board.get("medium_url") or board.get("url") or "").replace("http://", "https://") if board else None
    print(f"[*] 선택된 메뉴판: score={score} {sel}")

    # 신뢰도 미달(중식 메뉴판을 못 찾음: 석식만 있거나 음식 사진뿐) → 안내 메시지
    if raw is None or score < MIN_BOARD_SCORE:
        print(f"[*] 중식 메뉴판 신뢰도 부족(score={score} < {MIN_BOARD_SCORE})")
        return None, (f"🍱 밥플러스 — 오늘({label}) 게시물은 있으나 중식 메뉴판을 찾지 못했어요. "
                      f"<{permalink}|게시물에서 직접 확인하기>")

    # 선별은 medium OCR, 전송은 고화질(xlarge)로 — 글씨가 또렷하게
    big_url = (board.get("xlarge_url") or board.get("large_url")
               or board.get("medium_url") or board.get("url")).replace("http://", "https://")
    try:
        raw = http_get(big_url, KAKAO_HEADERS, binary=True)
        print(f"[*] 전송 화질 업그레이드: {board.get('width')}x{board.get('height')} ({len(raw)//1024}KB)")
    except Exception as e:  # noqa: BLE001
        print(f"[!] xlarge 다운로드 실패({e}) → medium 으로 전송")
    return raw, None


# (표시 이름, 파일명, 띠 색, 수집 함수)
SOURCES = (
    ("밥플러스", "babplus.png", COLOR_BABPLUS, fetch_babplus),
    ("사랑해밥상", "sarang.png", COLOR_SARANG, fetch_sarang),
    ("밥짓는부엌", "babjit.png", COLOR_BABJIT, fetch_babjit),
)
# 합친 이미지의 줄 배치. 세로형 둘은 나란히, 밥짓는부엌(가로형 2단 메뉴판)은 한 줄 통째로
# — 절반 폭에 넣으면 글씨가 읽을 수 없게 작아진다(내용 배율 0.23배 → 한 줄을 다 주면 0.68배).
SHEET_ROWS = (("밥플러스", "사랑해밥상"), ("밥짓는부엌",))


def main():
    today = datetime.now(KST)
    if DRY_RUN:
        print("[DRY_RUN] 슬랙 전송 없이 동작만 확인합니다.")
    # 같은 날 재시도 실행인데 앞선 실행에서 이미 보냈으면 중복 방지
    if not DRY_RUN and already_sent():
        print("[*] 오늘 메뉴 이미 전송됨 → 재시도 생략")
        return
    # 주말이면 전송 생략. 예약 실행은 val.town cron이 평일만 트리거하지만,
    # Actions 탭의 수동 Run workflow 는 그 스케줄을 타지 않으므로 여기서도 막는다.
    if today.weekday() > 4:
        print(f"[*] 오늘은 주말({'월화수목금토일'[today.weekday()]}요일) → 전송 생략")
        return
    # 공휴일이면 전송 생략 (대체공휴일·임시공휴일 포함)
    kr_holidays = holidays.SouthKorea(years=[today.year])
    if today.date() in kr_holidays:
        print(f"[*] 오늘은 공휴일({kr_holidays.get(today.date())}) → 전송 생략")
        return
    label = f"{today.month}/{today.day}({'월화수목금토일'[today.weekday()]})"
    print(f"[*] 오늘(KST): {today:%Y-%m-%d} ({label})")

    # 한 곳이 죽어도 나머지는 보낸다. 실패는 코멘트에 안내 한 줄로만 남긴다.
    cards, notes = {}, []
    # ONLY 가 있으면 그 식당만 보낸다. 한 곳만 실패한 날 그 한 곳을 따로 복구 전송하는 용도
    # (전체 재실행은 나머지 두 곳을 중복 전송한다).
    only = os.environ.get("ONLY", "").strip()
    for name, _filename, color, fetch in SOURCES:
        if only and name != only:
            continue
        try:
            raw, note = fetch(today.date(), label)
        except Exception as e:  # noqa: BLE001
            print(f"[!] {name} 수집 실패: {e}")
            raw, note = None, f"⚠️ {name} — 메뉴를 가져오지 못했어요 ({e})"
        if raw:
            cards[name] = make_card(raw, f"{name} · {label}", color)
        else:
            notes.append(note)

    comment = "\n".join([f"🍽️ 오늘의 점심 — {label}"] + notes)
    if not cards:
        print("[*] 보낼 이미지 없음 → 안내 메시지만 전송")
        post_text(comment)
        mark_sent(today)
        return

    sheet = make_sheet([r for r in ([cards[n] for n in row if n in cards] for row in SHEET_ROWS) if r])
    upload_images([(sheet, "lunch.png", f"오늘의 점심 {label}")], comment)
    mark_sent(today)
    print(f"[*] 전송 완료 (식당 {len(cards)}곳 합친 이미지 1장, 안내 {len(notes)}줄)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"[!] 오류: {e}", file=sys.stderr)
        try:
            post_text(f"⚠️ 오늘 점심 메뉴 자동 전송 실패: {e}")
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)
