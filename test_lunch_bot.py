#!/usr/bin/env python3
"""사랑해밥상 주간 식단표 격자 검출·크롭 자체 점검. 네트워크 없이 돈다.

  python3 test_lunch_bot.py

실제 표를 축약해 흉내낸 이미지를 만들어 넣는다. 격자선 검출과 요일 열 선택이
망가지면(테두리를 선으로 오인, 열 순서 밀림, 석식 판정 반전) 여기서 걸린다.
"""
import os
from PIL import Image, ImageDraw

os.environ["DRY_RUN"] = "1"
import lunch_bot as L  # noqa: E402

LABEL_W, COL_W = 100, 200
# 실제 표와 같은 8개 대구획선: 날짜행 위/아래, 점심 아래, 샐러드바 3줄, 샐러드바 아래, 석식 아래
YS = [40, 76, 326, 356, 390, 418, 446, 626]
IMG_H = YS[-1] + 12
MARK_Y = YS[1] + 20          # 열 식별 표식을 그릴 높이 (점심칸 안)


def fake_table(dinner_split, band=False):
    """5열 주간 식단표 흉내. dinner_split=True면 석식행도 요일별로 나뉜다.
    band=True면 샐러드바 마지막 줄을 남색 행처럼 통째로 칠한다(2026-09-28자 표)."""
    w = LABEL_W + COL_W * 5
    im = Image.new("RGB", (w + 4, IMG_H), "white")
    d = ImageDraw.Draw(im)
    d.text((w // 2, 12), "주간식단표", fill="black")      # 표 위 헤더 (크롭에서 빠져야 함)
    xs = [LABEL_W + COL_W * i for i in range(6)]
    v_bottom = YS[7] if dinner_split else YS[6]
    for x in xs:
        d.line([(x, YS[0]), (x, v_bottom)], fill="black", width=2)
    for y in YS:
        d.line([(0, y), (w, y)], fill="black", width=2)
    if band:
        d.rectangle([0, YS[5], w, YS[6]], fill=(0, 32, 96))
    for i in range(5):                                    # i번 열엔 (i+1)*10px 빨간 표식
        d.rectangle([xs[i] + 20, MARK_Y, xs[i] + 20 + (i + 1) * 10 - 1, MARK_Y + 20], fill="red")
    return im


def main():
    flat = fake_table(dinner_split=False)     # 석식 = 통짜 안내 배너
    v, hl = L.table_grid(flat)
    assert v is not None, "격자 검출 실패"
    assert len(v) == 6, f"세로선 6개여야 함, 실제 {len(v)}"
    assert v[0] == LABEL_W, f"라벨열 경계 {v[0]} != {LABEL_W}"
    assert len(hl) == 8, f"가로 대구획선 8개여야 함, 실제 {len(hl)} ({hl})"

    for idx in range(5):
        crop = L.crop_weekday(flat, v, hl, idx)
        assert crop.height == YS[6] - YS[0], f"석식 안내행이 안 잘림 (높이 {crop.height})"
        px = crop.convert("RGB").load()
        marks = sum(1 for x in range(crop.width)
                    if px[x, MARK_Y - YS[0] + 10] == (255, 0, 0))
        assert marks == (idx + 1) * 10, f"{idx}번 열이 아닌 다른 열이 잘림 (표식 {marks}px)"

    split = fake_table(dinner_split=True)     # 석식을 요일별로 실제 운영하는 주
    v2, hl2 = L.table_grid(split)
    assert L.crop_weekday(split, v2, hl2, 0).height == YS[7] - YS[0], "석식행이 빠짐"

    # 칠해진 띠가 선 하나로 뭉치면 선 순번이 밀려 석식 배너가 딸려 나오거나 표 판정이 깨진다
    band = fake_table(dinner_split=False, band=True)
    v3, hl3 = L.table_grid(band)
    assert v3 is not None, "남색 띠가 있는 표 격자 검출 실패"
    h3 = L.crop_weekday(band, v3, hl3, 1).height          # 띠 경계는 선 굵기만큼 ±1px
    assert abs(h3 - (YS[6] - YS[0])) <= 2, f"띠 아래 경계를 못 찾음 (높이 {h3})"

    # 표가 아닌 이미지는 걸러져야 한다 (공지/일일메뉴 이미지가 여기로 새면 안 됨)
    assert L.table_grid(Image.new("RGB", (600, 400), "white"))[0] is None, "표 아닌 이미지가 통과"

    print("OK — 격자 6열 검출 / 요일별 열 선택 / 석식 포함·제외 / 칠해진 띠 / 비표 배제")


if __name__ == "__main__":
    main()
