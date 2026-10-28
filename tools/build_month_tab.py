#!/usr/bin/env python3
"""월간 매출 리포트(index.html)에 새 월 탭을 만들거나 기존 월 탭을 시트 값으로 다시 채운다.

원칙
  * 가장 최근 월 탭을 템플릿으로 복제하고 숫자/텍스트만 바꾼다 (스타일은 그대로 유지).
  * 모든 숫자는 data.json(구글시트 원본 값)에서만 계산한다. 추정/보정 없음.
  * 시트 요약 블록과 월별 시리즈를 교차검증하고, 불일치/이상치는 warnings 로 출력한다.
  * 인사이트 문구는 insights.json 으로만 주입한다 (직접 생성하지 않음).

사용
  python3 tools/build_month_tab.py --html index.html --data data.json \
      --month 10 --insights insights.json --out index.html [--template-month 9] [--report report.json]
  --dry-run : 파일을 쓰지 않고 검증/요약만 출력
"""
import argparse, copy, json, re, subprocess, sys, tempfile, os
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from bs4 import BeautifulSoup, NavigableString, Comment

CH = ["일본경매", "일본구매", "미국경매", "영국경매", "미국구매"]          # 차트/범례 고정 순서
CH_TABLE = ["일본경매", "일본구매", "미국경매", "영국경매", "미국구매"]
SHEET_CH = {"일본경매": "일본 경매", "일본구매": "일본 구매", "미국경매": "미국 경매",
            "미국구매": "미국 구매", "영국경매": "영국 경매"}
SUM_COL = {"일본경매": 3, "미국경매": 4, "영국경매": 5, "일본구매": 6, "미국구매": 7, "합계": 8}
MONTH_KEY = {5: "May", 6: "Jun", 7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec"}
YEAR = 2026
WARN = []      # 알림(진행 가능)
BLOCK = []     # 확인 전에는 파일을 쓰지 않는 문제 (빈 값/오류 값/의심 값/교차검증 불일치)
ERR_TOKENS = ("#REF!", "#DIV/0!", "#N/A", "#VALUE!", "#NAME?", "#NUM!", "#NULL!", "Err:", "#ERROR!")


def warn(msg):
    WARN.append(msg)


def block(msg):
    BLOCK.append(msg)


# ───────────────────────── 숫자 유틸 ─────────────────────────
def rh(x, nd=0):
    q = Decimal(1).scaleb(-nd)
    return Decimal(str(x)).quantize(q, rounding=ROUND_HALF_UP)


def num(s):
    """'₩ 1,234 ', '¥17,113', '5.78%', '▲82' → float. 빈 값은 None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip().replace("\n", "")
    if t in ("", "-"):
        return None
    neg = t.startswith("-") or t.startswith("▼") or t.startswith("-₩") or "-₩" in t
    t = re.sub(r"[^\d.]", "", t)
    if t in ("", "."):
        return None
    v = float(t)
    return -v if neg else v


def pct(a, b):
    """(a/b-1)*100, b==0 → None"""
    if b in (0, None) or a is None:
        return None
    return (a / b - 1) * 100


def fpct(p, signed=True):
    if p is None:
        return "-"
    v = rh(p, 2)
    s = f"{abs(v):,.2f}%"
    if not signed:
        return s
    return ("+" if v > 0 else "-" if v < 0 else "") + s


def man(v):
    return int(rh(v / 10000))


def fman(v, signed=False):
    m = man(v)
    s = f"{abs(m):,}만원"
    if signed:
        return ("+" if m > 0 else "-" if m < 0 else "") + s
    return ("-" if m < 0 else "") + s


def feok(v):
    m = man(v)
    eok, rem = divmod(m, 10000)
    if eok > 0:
        return f"{eok}억{rem:,}만원" if rem else f"{eok}억원"
    return f"{m:,}만원"


def cls(p):
    if p is None:
        return "flat"
    return "up" if rh(p, 2) > 0 else "down" if rh(p, 2) < 0 else "flat"


# ───────────────────────── 시트 파싱 ─────────────────────────
def vals(x):
    return x["values"] if isinstance(x, dict) else x


def cell(rows, r, c):
    try:
        return rows[r][c]
    except IndexError:
        return ""


def parse_jeongri(rows, m):
    """'정리' 탭 → 월별 시리즈 + 요약 블록(26년도 m월) + 가입 데이터"""
    out = {"series": {}, "block": {}, "join": {}}
    # 시리즈
    heads = [i for i, r in enumerate(rows) if len(r) > 2 and r[1] in SHEET_CH.values() and r[2] == "1월"]
    inv = {v: k for k, v in SHEET_CH.items()}
    for n, h in enumerate(heads):
        end = heads[n + 1] if n + 1 < len(heads) else len(rows)
        # 가입수 표 이전에서 끊기
        for j in range(h + 1, end):
            if len(rows[j]) > 1 and rows[j][1] in ("2026",):
                end = j
                break
        block = rows[h + 1:end]
        ch = inv[rows[h][1]]
        i25 = next(i for i, r in enumerate(block) if len(r) > 1 and str(r[1]).startswith("25'"))
        r26, r25 = block[:i25], block[i25:]

        def row(region, pred):
            for r in region:
                if len(r) > 1 and pred(str(r[1])):
                    return r
            raise KeyError(f"{ch}: row not found")

        def series(r):
            return [num(cell([r], 0, 1 + k)) for k in range(1, 13)]

        s = {}
        s["cnt26"] = series(row(r26, lambda l: l.startswith("26'")))
        s["price26"] = series(row(r26, lambda l: l.endswith("총액(원)") and (l.startswith("낙찰가") or l.startswith("구매가"))))
        s["fee26"] = series(row(r26, lambda l: l == "수수료 총액(원)"))
        s["avgp26_raw"] = [str(cell([row(r26, lambda l: l.startswith("평균 낙찰가") or l.startswith("평균 구매가"))], 0, 1 + k)).strip() for k in range(1, 13)]
        s["avgp26"] = [num(x) for x in s["avgp26_raw"]]
        s["cnt25"] = series(row(r25, lambda l: l.startswith("25'")))
        s["price25"] = series(row(r25, lambda l: l in ("낙찰가 총액(원)", "구매가 총액(원)")))
        s["fee25"] = series(row(r25, lambda l: l == "수수료 총액(원)"))
        out["series"][ch] = s
    # 요약 블록
    hdr_r = hdr_c = None
    for r in range(0, 4):
        for c, v in enumerate(rows[r] if r < len(rows) else []):
            if str(v).strip() == f"{YEAR % 100}년도 {m}월":
                hdr_r, hdr_c = r, c
    if hdr_r is None:
        warn(f"'정리' 탭 상단에 '{YEAR % 100}년도 {m}월' 요약 블록이 없어 시트 자체 요약값과의 교차검증을 건너뜀")
    else:
        names = ["일본경매", "일본구매", "미국경매", "미국구매", "영국경매", "합계"]
        for r in range(hdr_r + 1, min(len(rows), hdr_r + 20)):
            label = str(cell(rows, r, hdr_c)).strip()
            if not label:
                continue
            out["block"][label] = {n: str(cell(rows, r, hdr_c + 1 + k)).strip() for k, n in enumerate(names)}
    # 가입
    def find(label_start, nth=0):
        hit = [r for r in rows if len(r) > 1 and str(r[1]).startswith(label_start)]
        return hit[nth] if len(hit) > nth else None
    for key, lab in (("sign25", "가입수(2025)"), ("sign26", "가입수(2026)"),
                     ("trade25", "가입월거래(2025)"), ("trade26", "가입월거래(2026)")):
        r = find(lab)
        out["join"][key] = [num(cell([r], 0, 1 + k)) for k in range(1, 13)] if r else None
    return out


def parse_daily(rows, m):
    days = {}
    for r in rows:
        if r and re.match(r"\d{4}-\d{2}-\d{2}$", str(r[0]).strip()):
            d = date.fromisoformat(str(r[0]).strip())
            days[d] = {k: (num(cell([r], 0, i)) or 0) for k, i in SUM_COL.items()}
    return days


def month_range(m):
    first = date(YEAR, m, 1)
    last = (date(YEAR + (m == 12), m % 12 + 1, 1)) - timedelta(days=1)
    return first, last


def weekly(days, m):
    first, last = month_range(m)
    # 마지막으로 실적(합계>0)이 있는 날
    have = [d for d, v in days.items() if first <= d <= last and v["합계"] > 0]
    through = max(have) if have else None
    partial = through is None or through < last
    end = through or last
    start = first - timedelta(days=first.weekday())
    weeks, d = [], start
    while d <= end:
        we = min(d + timedelta(days=6), end)
        rng = [d + timedelta(days=i) for i in range((we - d).days + 1)]
        miss = [x for x in rng if x not in days]
        if miss:
            warn(f"주간 집계에 SUM 일별 데이터가 없는 날짜: {miss[0]}~{miss[-1]} (SUM 범위를 더 넓게 읽어야 함)")
        agg = {k: sum(days.get(x, {}).get(k, 0) for x in rng) for k in SUM_COL}
        weeks.append({"label": f"{len(weeks) + 1}주차({d.month}/{d.day}~{we.month}/{we.day})", "agg": agg, "start": d, "end": we})
        d += timedelta(days=7)
    return weeks, through, partial


def parse_shipping(rows, m):
    """2026_shipping: 'YYMM' 라벨이 H열(idx7)에 있는 월 블록"""
    tag = f"{YEAR % 100}{m:02d}"
    out = {}
    for i, r in enumerate(rows):
        if len(r) > 7 and str(r[7]).strip() == tag:
            j = i + 1
            # 헤더 행(국제운송료 수익(원화))까지 이동
            while j < len(rows) and str(cell(rows, j, 7)).strip() != "국제운송료 수익(원화)":
                j += 1
            j += 1
            while j < len(rows):
                name = str(cell(rows, j, 7)).strip()
                if not name:
                    break
                out[name] = {"cnt": num(cell(rows, j, 8)) or 0, "won": num(cell(rows, j, 10)) or 0}
                if name == "총합":
                    break
                j += 1
            return out
    return None


def parse_freight(rows, m):
    """2026_운임정산(US/UK): 월 표 (A4:D16)"""
    for r in rows:
        if r and str(r[0]).strip() == f"{m}월" and len(r) >= 4:
            return {"US": num(r[1]) or 0, "UK": num(r[2]) or 0, "합계": num(r[3]) or 0}
    return None


# ───────────────────────── 지표 계산 ─────────────────────────
def compute(data, m):
    J = parse_jeongri(vals(data["정리"]), m)
    S = J["series"]
    k = m - 1
    M = {"ch": {}, "tot": {}, "series": S, "join": J["join"]}
    for ch in CH:
        s = S[ch]
        nz = lambda x: x if x is not None else 0     # 빈 값은 계산만 0으로 두고, quality_checks 가 반드시 BLOCK 으로 보고한다
        c = dict(cnt=nz(s["cnt26"][k]), cnt_pm=nz(s["cnt26"][k - 1]), cnt_py=nz(s["cnt25"][k]),
                 price=nz(s["price26"][k]), price_pm=nz(s["price26"][k - 1]), price_py=nz(s["price25"][k]),
                 fee=nz(s["fee26"][k]), fee_pm=nz(s["fee26"][k - 1]), fee_py=nz(s["fee25"][k]),
                 avgp=s["avgp26"][k], avgp_pm=s["avgp26"][k - 1])
        M["ch"][ch] = c
    tot = {}
    for key in ("cnt", "price", "fee"):
        for suf in ("", "_pm", "_py"):
            tot[key + suf] = sum(M["ch"][ch][key + suf] for ch in CH)
    div = lambda a, b: (a / b) if b else None
    tot["avgp"] = div(tot["price"], tot["cnt"])
    tot["avgp_pm"] = div(tot["price_pm"], tot["cnt_pm"])
    tot["avgp_py"] = div(tot["price_py"], tot["cnt_py"])
    tot["rate"] = (div(tot["fee"], tot["price"]) or 0) * 100
    tot["rate_pm"] = (div(tot["fee_pm"], tot["price_pm"]) or 0) * 100
    M["tot"] = tot
    # 작년 평균 물품가(현지통화): 시트 요약 블록
    B = J["block"]
    for ch in CH:
        c = M["ch"][ch]
        raw = B.get("작년 평균물품가", {}).get(ch)
        c["avgp_py"] = num(raw) if raw else None
        if c["avgp_py"] is None:
            warn(f"{ch}: 작년 평균물품가를 요약 블록에서 못 읽음 → 전년 평균물품가 증감 '-' 표시")
    # 교차검증: 시트 요약 블록 vs 시리즈 계산
    chk = [("주문건수", "cnt", 1), ("물품가 총액", "price", 1), ("수수료 총액", "fee", 1)]
    for lab, key, _ in chk:
        for ch in CH + ["합계"]:
            raw = B.get(lab, {}).get(ch)
            if raw is None:
                continue
            mine = tot[key] if ch == "합계" else M["ch"][ch][key]
            if abs(num(raw) - mine) > 1:
                block(f"교차검증 불일치: {ch} {lab} 시트요약={raw.strip()} / 시리즈계산={mine:,.0f}")
    for lab, key in (("전월대비 수수료 수입", "fee"), ("전년대비 수수료 수입", "fee")):
        suf = "_pm" if "전월" in lab else "_py"
        for ch in CH + ["합계"]:
            raw = B.get(lab, {}).get(ch)
            if raw is None:
                continue
            a = tot if ch == "합계" else M["ch"][ch]
            mine = pct(a[key], a[key + suf])
            if mine is not None and abs(num(raw) - mine) > 0.011:
                block(f"교차검증 불일치: {ch} {lab} 시트요약={raw} / 계산={mine:.2f}%")
    # 월별 총 수수료(만원) 추이
    M["fee_by_month"] = [sum((S[ch]["fee26"][i] or 0) for ch in CH) for i in range(m)]
    M["trend_man"] = [man(v) for v in M["fee_by_month"]]
    # 채널별 MoM(JP/US/UK) 최근 4개월
    M["mom_months"] = list(range(max(1, m - 3), m + 1))
    M["mom"] = {}
    for ch in ("일본경매", "미국경매", "영국경매"):
        f = S[ch]["fee26"]
        M["mom"][ch] = [pct(f[i - 1], f[i - 2]) if i >= 2 else None for i in M["mom_months"]]
        M["mom"][ch] = [pct(f[i - 1], f[i - 2]) if i >= 2 else None for i in M["mom_months"]]
    return M


def quality_checks(data, M, days, m, W, partial):
    """빈 값 / 오류 값 / 의심 값 점검. 발견되면 BLOCK 에 넣어 사용자 확인 전에는 쓰지 않게 한다."""
    # (a) 사용 범위 전체에서 시트 오류값
    for name in ("정리", "SUM", "shipping", "freight"):
        if name not in data:
            continue
        for r, row in enumerate(vals(data[name])):
            for c, v in enumerate(row):
                if isinstance(v, str) and any(t in v for t in ERR_TOKENS):
                    block(f"시트 오류값: {name} 탭 {r + 1}행 {c + 1}열 = '{v}'")
    # (b) 대상 월/전월/전년 시리즈의 빈 값·0·음수
    k = m - 1
    lab = {"cnt": "주문건수", "price": "물품가", "fee": "수수료"}
    for ch in CH:
        s = M["series"][ch]
        for key, nm in lab.items():
            for tag, idx, arr in (("대상월", k, s[key + "26"]), ("전월", k - 1, s[key + "26"]), ("전년동월", k, s[key + "25"])):
                v = arr[idx]
                if v is None:
                    block(f"빈 값: {ch} {nm} {tag}")
                elif v < 0:
                    block(f"음수 값: {ch} {nm} {tag} = {v:,.0f}")
                elif v == 0 and not (partial and tag == "대상월"):
                    block(f"0 값(의심): {ch} {nm} {tag} = 0 (실제 0인지 미입력인지 확인 필요)")
        if M["ch"][ch]["cnt"] and M["ch"][ch]["fee"] == 0:
            block(f"의심: {ch} 주문 {int(M['ch'][ch]['cnt'])}건인데 수수료 0")
        if M["ch"][ch]["cnt"] == 0 and M["ch"][ch]["fee"]:
            block(f"의심: {ch} 주문 0건인데 수수료 {M['ch'][ch]['fee']:,.0f}")
        if M["ch"][ch].get("avgp") is None:
            block(f"빈 값: {ch} 평균 물품가(현지통화)")
    for ch in CH:
        for i in range(m - 1):
            if M["series"][ch]["fee26"][i] is None or M["series"][ch]["cnt26"][i] is None:
                block(f"빈 값: {ch} {i + 1}월 수수료/주문건수 (월별 추이 차트에 0으로 표시될 수 있음)")
    # (c) 가입 데이터
    J = M["join"]
    for key, nm in (("sign26", "신규가입자"), ("trade26", "가입월거래"), ("sign25", "작년 신규가입자"), ("trade25", "작년 가입월거래")):
        arr = J.get(key)
        if arr is None:
            block(f"빈 값: {nm} 행을 시트에서 찾지 못함")
        elif arr[k] in (None, 0) and not partial:
            block(f"빈 값/0(의심): {nm} {m}월")
        elif arr[k] is None:
            block(f"빈 값: {nm} {m}월")
    # (d) 일별 데이터: 월 내 합계 0인 날(시스템 오류·누락 의심), 중간 누락
    first, last = month_range(m)
    through = max((d for d, v in days.items() if first <= d <= last and v["합계"] > 0), default=None)
    d = first
    while through and d <= through:
        if d not in days:
            block(f"일별 데이터 누락: {d} (SUM 탭)")
        elif days[d]["합계"] == 0:
            block(f"일별 주문건수 0(의심): {d} (실제 무거래/시스템 오류/미집계 확인 필요)")
        d += timedelta(days=1)
    # (e) 운송료
    ship = parse_shipping(vals(data["shipping"]), m) if data.get("shipping") else None
    if ship:
        for nm, b in ship.items():
            if b["cnt"] and not b["won"] and nm != "총합":
                block(f"의심: 배송사 {nm} 출고 {int(b['cnt'])}건인데 고객결제금액 0")
        body = sum(b["won"] for n, b in ship.items() if n != "총합")
        if "총합" in ship and ship["총합"]["won"] and abs(body - ship["총합"]["won"]) > 1:
            block(f"교차검증 불일치: 배송사별 합 {body:,.0f} / 시트 총합 {ship['총합']['won']:,.0f}")
    fr = parse_freight(vals(data["freight"]), m) if data.get("freight") else None
    if data.get("freight") and fr is None:
        block(f"빈 값: 운임정산(US/UK) {m}월 행을 찾지 못함")
    elif fr and not partial and fr["합계"] == 0:
        block("0 값(의심): 운임정산(US/UK) 합계 0")


# ───────────────────────── HTML 도우미 ─────────────────────────
def txt(el, s):
    """요소의 마지막 문자열 노드를 교체(앞쪽 자식 태그는 유지)"""
    last = None
    for c in el.contents:
        if isinstance(c, NavigableString) and c.strip():
            last = c
    if last is None:
        el.append(NavigableString(s))
    else:
        last.replace_with(NavigableString(s))


def set_text(el, s):
    el.clear()
    el.append(NavigableString(s))


def badge(span, text, kind):
    classes = [c for c in span.get("class", []) if not c.startswith("badge-")]
    span["class"] = classes + [f"badge-{kind}"]
    set_text(span, text)


def color_class(td, kind):
    c = [x for x in td.get("class", []) if x not in ("up", "down")]
    if kind in ("up", "down"):
        c.append(kind)
    if c:
        td["class"] = c
    elif "class" in td.attrs:
        del td["class"]


def style_color(td, kind):
    """인라인 style 의 color 를 up/down 변수로 교체"""
    st = td.get("style", "")
    st = re.sub(r"color:\s*var\(--(up|down)\);?", "", st)
    if kind in ("up", "down"):
        st = st.rstrip(";") + f";color:var(--{kind})"
    td["style"] = st.strip(";") + ";"


def month_remap(root, t, m):
    """템플릿 월(t)→대상 월(m), 전월(t-1)→(m-1) 치환"""
    def sub(mo):
        n = int(mo.group(1))
        if n == t:
            return f"{m}월"
        if n == t - 1:
            return f"{m - 1}월"
        return mo.group(0)
    for node in root.find_all(string=True):
        if isinstance(node, Comment):      # HTML 주석은 화면에 보이면 안 되므로 그대로 둔다
            continue
        if isinstance(node, NavigableString) and "월" in node and node.parent.name not in ("script", "style"):
            new = re.sub(r"(?<![\d~])(\d{1,2})월", sub, str(node))
            new = re.sub(r"(~)(\d{1,2})월", lambda mo: f"~{m}월" if int(mo.group(2)) == t else mo.group(0), new)
            if new != str(node):
                node.replace_with(NavigableString(new))


def find_card(root, label):
    for c in root.select(".kpi-card"):
        l = c.select_one(".kpi-label")
        if l and l.get_text(strip=True) == label:
            return c
    raise KeyError(label)


def section_label(root, startswith):
    for p in root.select("p.section-label"):
        if p.get_text(strip=True).startswith(startswith):
            return p
    return None


# ───────────────────────── 섹션 채우기 ─────────────────────────
def fill_kpis(root, M, m):
    T = M["tot"]
    pm = m - 1
    c = find_card(root, "수수료 총액")
    c.select_one(".kpi-value").string = f"{man(T['fee']):,}만"
    b = c.select(".kpi-badge")
    p1, p2 = pct(T["fee"], T["fee_pm"]), pct(T["fee"], T["fee_py"])
    badge(b[0], f"{pm}월대비 {fpct(p1)}", "up" if cls(p1) == "up" else "down")
    badge(b[1], f"전년 {m}월 대비 {fpct(p2)}", "up" if cls(p2) == "up" else "down")
    c = find_card(root, "물품가 총액")
    c.select_one(".kpi-value").string = f"{T['price'] / 1e8:.2f}억"
    b = c.select(".kpi-badge")
    p1, p2 = pct(T["price"], T["price_pm"]), pct(T["price"], T["price_py"])
    badge(b[0], f"{pm}월대비 {fpct(p1)}", "up" if cls(p1) == "up" else "down")
    badge(b[1], f"전년 {m}월 대비 {fpct(p2)}", "up" if cls(p2) == "up" else "down")
    c = find_card(root, "주문 건수")
    c.select_one(".kpi-value").string = f"{int(T['cnt']):,}건"
    b = c.select(".kpi-badge")
    p1, p2 = pct(T["cnt"], T["cnt_pm"]), pct(T["cnt"], T["cnt_py"])
    badge(b[0], f"{pm}월대비 {fpct(p1)}", "up" if cls(p1) == "up" else "down")
    badge(b[1], f"전년 {m}월 대비 {fpct(p2)}", "up" if cls(p2) == "up" else "down")
    c = find_card(root, "평균 수수료율")
    c.select_one(".kpi-value").string = f"{rh(T['rate'], 2)}%"
    badge(c.select_one(".kpi-badge"), f"전월 {rh(T['rate_pm'], 2)}%", "flat")


def fill_donuts(root, M, m):
    T = M["tot"]
    vals_ = [("수수료 비중", "fee", f"{man(T['fee']):,}만"), ("주문건수 비중", "cnt", f"{int(T['cnt']):,}건")]
    for title_part, key, center in vals_:
        card = next(c for c in root.select(".card") if title_part in (c.select_one(".card-title") or "").get_text())
        txt(card.select_one(".card-title"), f"{m}월 {title_part}")
        card.select_one(".donut-center-val").string = center
        legs = card.select(".legend-row .leg")
        for leg, ch in zip(legs, CH):
            share = M["ch"][ch][key] / T[key] * 100
            txt(leg, f"{ch} {rh(share, 1)}%")


def fill_trend(root, M, m):
    card = next(c for c in root.select(".card") if "월별 수수료 총액 흐름" in c.get_text())
    txt(card.select_one(".card-title"), f"월별 수수료 총액 흐름 (1~{m}월)")
    table = card.select_one("table")
    thr = table.select_one("thead tr")
    th_tpl = copy.copy(thr.select("th")[1])
    for th in thr.select("th")[1:]:
        th.decompose()
    for i in range(1, m + 1):
        th = copy.copy(th_tpl)
        set_text(th, f"{i}월")
        thr.append(th)
    tr = table.select_one("tbody tr")
    tds = tr.select("td")
    td_tpl, label = copy.copy(tds[1]), tds[0]
    for td in tds[1:]:
        td.decompose()
    tm = M["trend_man"]
    base = "border:1px solid var(--border);padding:4px;"
    for i in range(m):
        td = copy.copy(td_tpl)
        if i == 0:
            set_text(td, "-")
            td["style"] = base
        else:
            d = tm[i] - tm[i - 1]
            set_text(td, f"{'+' if d > 0 else '-' if d < 0 else ''}{abs(d):,}만원")
            k_ = "up" if d > 0 else "down" if d < 0 else None
            td["style"] = base + (f"color:var(--{k_});" if k_ else "") + "font-weight:600;"
        tr.append(td)


def fill_mom(root, M, m):
    card = next(c for c in root.select(".card") if "전월대비 증감률" in c.get_text())
    table = card.select_one("table")
    thr = table.select_one("thead tr")
    ths = thr.select("th")
    th_tpl = copy.copy(ths[1])
    for th in ths[1:]:
        th.decompose()
    for mo in M["mom_months"]:
        th = copy.copy(th_tpl)
        set_text(th, f"{mo}월")
        thr.append(th)
    names = {"JP": "일본경매", "US": "미국경매", "UK": "영국경매"}
    for tr in table.select("tbody tr"):
        tds = tr.select("td")
        ch = names[tds[0].get_text(strip=True)]
        td_tpl = copy.copy(tds[1])
        for td in tds[1:]:
            td.decompose()
        for p in M["mom"][ch]:
            td = copy.copy(td_tpl)
            set_text(td, fpct(p))
            k_ = cls(p) if cls(p) in ("up", "down") else None
            td["style"] = "border:1px solid var(--border);padding:4px;" + (f"color:var(--{k_});" if k_ else "") + "font-weight:600;"
            tr.append(td)


def fill_weekly(root, W, m):
    p = section_label(root, "주간 주문건수 추이")
    set_text(p, f"주간 주문건수 추이 ({m}월 1~{len(W)}주차)")


def fill_tables(root, M, m):
    pm = m - 1
    # 수수료 표
    for t in root.select("table.fee-table"):
        heads = [th.get_text(strip=True) for th in t.select("thead th")]
        kind = "fee" if heads[1].endswith("수수료") else "price"
        ths = t.select("thead th")
        set_text(ths[1], f"{m}월 수수료" if kind == "fee" else f"{m}월 물품가 총액")
        set_text(ths[3], f"전년 {m}월 대비")
        rows = t.select("tbody tr")
        for tr in rows:
            tds = tr.select("td")
            name = tds[0].get_text(strip=True)
            a = M["tot"] if name == "합계" else M["ch"][name]
            key = "fee" if kind == "fee" else "price"
            v, vpm, vpy = a[key], a[key + "_pm"], a[key + "_py"]
            if kind == "fee":
                txt(tds[1], fman(v))
            else:
                txt(tds[1], feok(v))
            for idx, base in ((2, vpm), (3, vpy)):
                p = pct(v, base)
                color_class(tds[idx], cls(p) if cls(p) in ("up", "down") else None)
                sub = tds[idx].select_one(".fee-sub")
                tds[idx].contents[0].replace_with(NavigableString(
                    (fman(v - base, signed=True) if kind == "fee" else fman(v - base, signed=True))))
                sub.string = fpct(p)
            # 4열
            c4 = tds[4]
            sub = c4.select_one(".fee-sub")
            pp_m, pp_y = None, None
            if kind == "fee":
                txt_main = f"{int(a['cnt']):,}건"
                pp_m, pp_y = pct(a["cnt"], a["cnt_pm"]), pct(a["cnt"], a["cnt_py"])
            else:
                if a.get("avgp") is None:
                    main = "-"
                    pp_m = pp_y = None
                elif name == "합계":
                    main = f"￦{rh(a['avgp']):,}"
                    pp_m, pp_y = pct(a["avgp"], a["avgp_pm"]), pct(a["avgp"], a["avgp_py"])
                else:
                    sym = {"일본경매": "￥", "일본구매": "￥", "미국경매": "$", "미국구매": "$", "영국경매": "￡"}[name]
                    nd = 0 if sym == "￥" else 2
                    main = f"{sym}{rh(a['avgp'], nd):,}"
                    pp_m = pct(a["avgp"], a["avgp_pm"])
                    pp_y = pct(a["avgp"], a["avgp_py"]) if a.get("avgp_py") else None
                txt_main = main
            c4.contents[0].replace_with(NavigableString(txt_main))
            spans = sub.select("span")
            for sp, p in zip(spans, (pp_m, pp_y)):
                set_text(sp, fpct(p))
                sp["class"] = [cls(p)] if cls(p) in ("up", "down") else []
            # 거래비중
            share = (a["cnt"] / M["tot"]["cnt"] * 100) if name != "합계" else 100
            set_text(tds[5], "100%" if name == "합계" else f"{rh(share, 1)}%")


def fill_join(root, M, m):
    J = M["join"]
    s26, s25, t26, t25 = J["sign26"], J["sign25"], J["trade26"], J["trade25"]
    k = m - 1
    c = find_card(root, "신규 가입자")
    c.select_one(".kpi-value").string = f"{int(s26[k]):,}명"
    b = c.select(".kpi-badge")
    p1, p2 = pct(s26[k], s26[k - 1]), pct(s26[k], s25[k])
    badge(b[0], f"{m - 1}월 신규가입자 {int(s26[k - 1]):,}명 ({fpct(p1)})", "up" if cls(p1) == "up" else "down")
    badge(b[1], f"작년 {m}월 신규가입자 {int(s25[k]):,}명 ({fpct(p2)})", "up" if cls(p2) == "up" else "down")
    c = find_card(root, "가입월 거래")
    c.select_one(".kpi-value").string = f"{int(t26[k]):,}건"
    b = c.select(".kpi-badge")
    p1, p2 = pct(t26[k], t26[k - 1]), pct(t26[k], t25[k])
    badge(b[0], f"{m - 1}월 가입월 거래 {int(t26[k - 1]):,}건 ({fpct(p1)})", "up" if cls(p1) == "up" else "down")
    badge(b[1], f"작년 {m}월 가입월 거래 {int(t25[k]):,}건 ({fpct(p2)})", "up" if cls(p2) == "up" else "down")
    c = find_card(root, "가입월 결제건수 비율")
    c.select_one(".kpi-value").string = f"{rh(t26[k] / s26[k] * 100, 2)}%"
    card = next(x for x in root.select(".card") if "신규가입자 · 가입월 거래 추이" in x.get_text())
    txt(card.select_one(".card-title"), f"신규가입자 · 가입월 거래 추이 (1~{m}월)")
    grid = next(g for g in card.find_all("div", recursive=False)
                if "grid-template-columns:repeat(" in g.get("style", ""))
    items = grid.find_all("div", recursive=False)
    tpl = copy.copy(items[0])
    for it in items:
        it.decompose()
    grid["style"] = re.sub(r"repeat\(\d+,", f"repeat({m},", grid["style"])
    for i in range(m):
        it = copy.copy(tpl)
        divs = it.find_all("div")
        set_text(divs[0], f"{i + 1}월")
        # 1월은 전년 12월(가입월거래 2025의 12월) 대비
        d = (t26[i] - t26[i - 1]) if i > 0 else ((t26[0] - t25[11]) if t25[11] is not None else None)
        if d is None:
            set_text(divs[1], "-")
            divs[1]["style"] = re.sub(r"color:var\(--(up|down)\);?", "", divs[1]["style"]) + "color:var(--text-muted);"
        else:
            set_text(divs[1], f"{'+' if d > 0 else '-' if d < 0 else ''}{abs(int(d)):,}")
            divs[1]["style"] = re.sub(r"color:var\(--[a-z-]+\);?", "", divs[1]["style"]).rstrip(";") + f";color:var(--{'up' if d > 0 else 'down' if d < 0 else 'text-muted'});"
        if i == m - 1:
            it["style"] = it.get("style", "").replace("border-right:1px solid var(--border);", "")
        elif "border-right" not in it.get("style", ""):
            it["style"] = it.get("style", "") + "border-right:1px solid var(--border);"
        grid.append(it)


def build_shipping_section(soup, data, m, anchor_tpl=None):
    """국제배송 현황 섹션 (기존 .kpi-card / .card / .data-table 스타일 재사용)"""
    ship = parse_shipping(vals(data["shipping"]), m) if data.get("shipping") else None
    prev = parse_shipping(vals(data["shipping"]), m - 1) if data.get("shipping") else None
    fr = parse_freight(vals(data["freight"]), m) if data.get("freight") else None
    frp = parse_freight(vals(data["freight"]), m - 1) if data.get("freight") else None
    if not ship or "총합" not in ship:
        warn(f"국제운송료 시트에서 {YEAR % 100}{m:02d} 블록을 찾지 못해 국제배송 섹션을 생략")
        return None, None
    prev = prev or {}
    tot, ptot = ship["총합"], prev.get("총합")
    per = lambda b: (b["won"] / b["cnt"]) if b and b["cnt"] else None

    def badge_html(p, label):
        k = cls(p)
        kind = "up" if k == "up" else "down" if k == "down" else "flat"
        return f'<span class="kpi-badge badge-{kind}">{label} {fpct(p)}</span>'

    cards = []
    p = pct(tot["cnt"], ptot["cnt"]) if ptot else None
    cards.append(("총 출고건수", f"{int(tot['cnt']):,}건", badge_html(p, f"{m - 1}월대비") if ptot else ""))
    p = pct(tot["won"], ptot["won"]) if ptot else None
    cards.append(("고객 결제 국제운임", feok(tot["won"]), badge_html(p, f"{m - 1}월대비") if ptot else ""))
    a, b = per(tot), per(ptot)
    p = pct(a, b) if a and b else None
    cards.append(("건당 고객 결제운임", f"₩{int(rh(a)):,}" if a else "-", badge_html(p, f"{m - 1}월대비") if p is not None else ""))
    if fr:
        p = pct(fr["합계"], frp["합계"]) if frp and frp["합계"] else None
        cards.append(("미국·영국 운임 정산 수익", fman(fr["합계"]), badge_html(p, f"{m - 1}월대비") if p is not None else ""))
    kpis = "".join(
        f'<div class="kpi-card" style="background-color:#ffffff;"><div class="kpi-label">{l}</div>'
        f'<div class="kpi-value">{v}</div><div style="display:flex;flex-direction:column;gap:4px;">{bdg}</div></div>'
        for l, v, bdg in cards)
    rows = []
    order = [k for k in ship if k != "총합"]
    for name in order + ["총합"]:
        cur, old = ship[name], prev.get(name)
        if name != "총합" and not cur["cnt"] and not (old and old["cnt"]):
            continue
        pc = pct(cur["cnt"], old["cnt"]) if old and old["cnt"] else None
        pw = pct(cur["won"], old["won"]) if old and old["won"] else None
        share = cur["won"] / tot["won"] * 100 if tot["won"] else 0
        unit = per(cur)
        sub = lambda p: f'<span class="fee-sub"><span class="{cls(p)}">{fpct(p)}</span></span>' if p is not None else ""
        rows.append(
            f"<tr><td>{'' if name == '총합' else '<span class=\"ch-dot\"></span>'}{'합계' if name == '총합' else name}</td>"
            f"<td>{int(cur['cnt']):,}건{sub(pc)}</td>"
            f"<td>{feok(cur['won'])}{sub(pw)}</td>"
            f"<td>{('₩' + format(int(rh(unit)), ',')) if unit else '-'}</td>"
            f"<td>{'100%' if name == '총합' else format(rh(share, 1), '') + '%'}</td></tr>")
    html = (
        f'<p class="section-label">국제배송 현황 ({m}월 · 배송사별 출고건수 / 고객 결제 국제운임)</p>'
        f'<div style="display:grid;grid-template-columns:repeat({len(cards)},1fr);gap:12px;margin-bottom:16px;min-width:0;">{kpis}</div>'
        f'<div class="card" style="margin-bottom:24px;padding:14px 18px"><div class="table-wrap"><table class="data-table fee-table">'
        f"<thead><tr><th>배송사</th><th>{m}월 출고건수</th><th>{m}월 고객 결제금액</th><th>건당 결제금액</th><th>결제금액 비중</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div></div>")
    info = {"ship": ship, "prev": prev, "freight": fr}
    return BeautifulSoup(html, "html.parser"), info


def fill_insights(root, ins):
    stack = root.select_one(".insight-stack")
    cols = stack.select(".insight-column")
    for col, key in zip(cols, ("up", "down")):
        grid = col.select_one(".insight-grid")
        cards = grid.select(".insight-card")
        tpl = copy.copy(cards[0])
        for c in cards:
            c.decompose()
        for item in ins.get(key, []):
            c = copy.copy(tpl)
            set_text(c.select_one(".insight-title"), item["title"])
            body = c.select_one(".insight-body")
            body.clear()
            body.append(BeautifulSoup(item["body"], "html.parser"))
            grid.append(c)
    merged = root.select_one(".insight-grid-merged .insight-content")
    titles, bodies = merged.select(".insight-title"), merged.select(".insight-body")
    t_tpl, b_tpl = copy.copy(titles[0]), copy.copy(bodies[0])
    for e in titles + bodies:
        e.decompose()
    for item in ins.get("merged", []):
        t, b = copy.copy(t_tpl), copy.copy(b_tpl)
        set_text(t, item["title"])
        b.clear()
        b.append(BeautifulSoup(item["body"], "html.parser"))
        merged.append(t)
        merged.append(b)


def minimal_patch(orig, new):
    """원본 탭 문자열에서 '값이 달라진 토큰'만 새 값으로 바꾼다 (서식·공백·<br> 표기는 원본 유지).
    두 문자열의 의미 있는 토큰(태그/텍스트) 수와 태그 구조가 같을 때만 동작하고, 아니면 None."""
    import html as _h
    pat = re.compile(r"<!--.*?-->|<[^>]+>|[^<]+", re.S)

    def toks(t):
        out = []
        for mo in pat.finditer(t):
            x = mo.group(0)
            if x.startswith("<!--"):
                continue
            if x.startswith("<"):
                name = re.sub(r"\s+", " ", x).replace(" />", ">").replace("/>", ">")
                out.append(("tag", mo.start(), mo.end(), name))
            elif x.strip():
                out.append(("txt", mo.start(), mo.end(), _h.unescape(" ".join(x.split()))))
        return out
    a, b = toks(orig), toks(new)
    if len(a) != len(b) or any(x[0] != y[0] for x, y in zip(a, b)):
        return None
    edits = []
    for x, y in zip(a, b):
        if x[3] == y[3]:
            continue
        if x[0] == "tag" and re.sub(r"<(\w+).*", r"\1", x[3]) != re.sub(r"<(\w+).*", r"\1", y[3]):
            return None
        if x[0] == "txt":
            old = orig[x[1]:x[2]]
            lead = old[:len(old) - len(old.lstrip())]
            trail = old[len(old.rstrip()):]
            newtxt = new[y[1]:y[2]].strip()
            edits.append((x[1], x[2], lead + newtxt + trail))
        else:
            edits.append((x[1], x[2], new[y[1]:y[2]]))
    res = orig
    for st, en, rep in sorted(edits, reverse=True):
        res = res[:st] + rep + res[en:]
    return res, len(edits)


# ───────────────────────── JS ─────────────────────────
def js_arr(a):
    return "[" + ", ".join(str(x) for x in a) + "]"


def build_js(js_tpl, t, m, M, W):
    kt, km = MONTH_KEY[t], MONTH_KEY[m]
    js = js_tpl.replace(kt, km).replace(kt.lower() + "ChartsInitialized", km.lower() + "ChartsInitialized")
    S = M["series"]
    # 주간
    labels = "[" + ", ".join(f"'{w['label']}'" for w in W) + "]"
    js = re.sub(r"labels: \['1주차[^\]]*\]", f"labels: {labels}", js, count=1)
    series = {"전체": "합계", "일본경매": "일본경매", "일본구매": "일본구매", "미국경매": "미국경매"}
    for lab, key in series.items():
        arr = js_arr([int(w["agg"][key]) for w in W])
        js, n = re.subn(rf"(label: '{lab}', data: )\[[^\]]*\]", rf"\g<1>{arr}", js, count=1)
        if n != 1:
            warn(f"JS 치환 실패: 주간 {lab}")
    if t != m:
        js = re.sub(r"// WEEKLY LINE \([^)]*\)", f"// WEEKLY LINE ({m}월 주간)", js, count=1)
    # 월 라벨 / 추이
    months = "[" + ",".join(f"'{i}월'" for i in range(1, m + 1)) + "]"
    js = re.sub(rf"(var months{km} = )\[[^\]]*\]", rf"\g<1>{months}", js)
    js = re.sub(rf"(var totalFee{km} = )\[[^\]]*\];[^\n]*", rf"\g<1>{js_arr(M['trend_man'])}; // 만원 단위 (전 채널 합계, 시트 수수료 총액 기준)", js)
    # 도넛
    fee_d = js_arr([int(M["ch"][c]["fee"]) for c in CH])
    cnt_d = js_arr([int(M["ch"][c]["cnt"]) for c in CH])
    js = re.sub(r"(// DONUT 수수료\s+new Chart\([^\n]*\n[^\n]*\n[^\n]*\n[^\n]*?data: )\[[^\]]*\]", rf"\g<1>{fee_d}", js, count=1)
    js = re.sub(r"(// DONUT 주문\s+new Chart\([^\n]*\n[^\n]*\n[^\n]*\n[^\n]*?data: )\[[^\]]*\]", rf"\g<1>{cnt_d}", js, count=1)
    # MoM
    ml = "[" + ", ".join(f"'{i}월'" for i in M["mom_months"]) + "]"
    js = re.sub(r"(// MoM LINE \()[^)]*\)", rf"\g<1>{M['mom_months'][0]}~{m}월)", js, count=1)
    js = re.sub(r"(// MoM LINE.*?labels: )\[[^\]]*\]", lambda mo: mo.group(1) + ml, js, count=1, flags=re.S)
    for lab, ch in (("JP 일본경매", "일본경매"), ("US 미국경매", "미국경매"), ("UK 영국경매", "영국경매")):
        arr = js_arr([float(rh(p, 2)) if p is not None else "null" for p in M["mom"][ch]])
        js, n = re.subn(rf"(label: '{lab}', data: )\[[^\]]*\]", rf"\g<1>{arr}", js, count=1)
        if n != 1:
            warn(f"JS 치환 실패: MoM {lab}")
    # 가입
    J = M["join"]
    jl = "[" + ", ".join(f"'{i}월'" for i in range(1, m + 1)) + "]"
    js = re.sub(r"(// 가입월거래 라인차트 \()[^)]*\)", rf"\g<1>1~{m}월)", js, count=1)
    js = re.sub(r"(// 가입월거래 라인차트.*?labels: )\[[^\]]*\]", lambda mo: mo.group(1) + jl, js, count=1, flags=re.S)
    for lab, key in (("가입월거래 2025", "trade25"), ("가입월거래 2026", "trade26"), ("신규가입자 2026", "sign26")):
        arr = js_arr([int(x) for x in J[key][:m]])
        js, n = re.subn(rf"(label: '{lab}', data: )\[[^\]]*\]", rf"\g<1>{arr}", js, count=1)
        if n != 1:
            warn(f"JS 치환 실패: 가입 {lab}")
    return js


# ───────────────────────── 메인 ─────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", default="index.html")
    ap.add_argument("--data", required=True)
    ap.add_argument("--month", type=int, required=True)
    ap.add_argument("--template-month", type=int)
    ap.add_argument("--insights")
    ap.add_argument("--out")
    ap.add_argument("--report")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--ack", action="store_true", help="BLOCK 항목을 사용자에게 확인받은 뒤에만 지정 (확인 내역은 report 에 기록)")
    ap.add_argument("--extra-html", action="append", default=[], help="요청월 전용 섹션 HTML 파일(여러 개 가능). 인사이트 앞에 삽입")
    a = ap.parse_args()
    m = a.month
    html = open(a.html, encoding="utf-8").read()
    data = json.load(open(a.data, encoding="utf-8"))

    # 템플릿 월 = 지정값 또는 존재하는 가장 마지막 월 탭 (대상 월 제외)
    keys = {v.lower(): k for k, v in MONTH_KEY.items()}
    present = sorted(keys[x] for x in re.findall(r'<div class="tab-content" id="tab-(\w+)">', html) if x in keys)
    if a.template_month:
        t = a.template_month
    elif m in present:          # 이미 있는 월 → 그 탭을 시트 값으로 다시 채움(갱신 모드)
        t = m
    else:
        earlier = [x for x in present if x < m]
        t = max(earlier) if earlier else None
    if t is None or t not in present:
        sys.exit(f"템플릿으로 쓸 월 탭이 없습니다 (현재 탭: {present})")
    kt, km = MONTH_KEY[t], MONTH_KEY[m]
    refresh = (t == m)      # 같은 월 탭을 시트 값으로 다시 채우는 모드 (전용 섹션·인사이트 유지, 배송 섹션은 이미 있을 때만 갱신)

    # 계산
    M = compute(data, m)
    days = parse_daily(vals(data["SUM"]), m)
    W, through, partial = weekly(days, m)
    first, last = month_range(m)
    if partial:
        warn(f"{m}월 데이터가 {through}까지만 있음(월말 {last}) → 잠정치. 정식 월간 보고는 월말 확정 후 실행 권장")
        warn(f"마지막 일자 {through}는 당일 집계 중인 부분 일자일 수 있음 (시트 '정리' 갱신일과 SUM 마지막 행 확인)")

    quality_checks(data, M, days, m, W, partial)

    # 템플릿 탭 추출
    s_mark = f'<div class="tab-content" id="tab-{kt.lower()}">'
    e_mark = f"<!-- /tab-{kt.lower()} -->"
    si, ei = html.index(s_mark), html.index(e_mark)
    frag = html[si:ei]
    indent_end = frag.rstrip()
    soup = BeautifulSoup(frag, "html.parser")
    root = soup.find("div", class_="tab-content")
    root["id"] = f"tab-{km.lower()}"

    # 1) 월 토큰 치환 → 2) 숫자 채우기
    month_remap(root, t, m)
    for e in root.select("canvas"):
        e["id"] = e["id"].replace(kt, km)
    p = section_label(root, "핵심 지표")
    set_text(p, f"핵심 지표 - {m}월 실적" + (" (잠정)" if partial else ""))
    fill_kpis(root, M, m)
    fill_donuts(root, M, m)
    fill_trend(root, M, m)
    fill_mom(root, M, m)
    fill_weekly(root, W, m)
    fill_tables(root, M, m)
    fill_join(root, M, m)

    # 템플릿 월의 '요청월 전용' 섹션 제거 (표준 섹션이 아닌 section-label 과 그 내용)
    STD = ("핵심 지표", "주간 주문건수 추이", "채널별 전월", "물품가(낙찰가", "신규 가입자", "국제배송 현황", "주요 인사이트")
    for lab_el in list(root.select("p.section-label")):
        label = lab_el.get_text(strip=True)
        if label.startswith(STD) or refresh:
            continue
        warn(f"템플릿 월의 전용 섹션 '{label}' 은(는) 새 탭에 복사하지 않음 (필요하면 --extra-html 로 요청월 실제 값을 넣어 추가)")
        el = lab_el.find_next_sibling()
        lab_el.decompose()
        while el is not None and not (el.name == "hr" or (el.name == "p" and "section-label" in el.get("class", []))):
            nxt = el.find_next_sibling()
            el.decompose()
            el = nxt
    # 국제배송 섹션 (재생성)
    old = section_label(root, "국제배송 현황")
    if old:
        nxt = old.find_next_sibling()
        grid, card = nxt, nxt.find_next_sibling() if nxt else None
        for e in (grid, card, old):
            if e:
                e.decompose()
    ship_soup, ship_info = (None, None) if (refresh and not old) else build_shipping_section(soup, data, m)
    if refresh and not old:
        warn("갱신 모드: 기존 탭에 국제배송 현황 섹션이 없어 추가하지 않음 (필요하면 별도로 요청)")
    if ship_soup is not None:
        hr = root.select_one("hr.divider")
        for el in list(ship_soup.contents):
            hr.insert_before(copy.copy(el))
    for path in a.extra_html:
        frag_ = BeautifulSoup(open(path, encoding="utf-8").read(), "html.parser")
        hr = root.select_one("hr.divider")
        for el in list(frag_.contents):
            hr.insert_before(copy.copy(el))
        warn(f"요청월 전용 섹션 삽입: {path} (값이 시트/사용자 제공 실제 값인지 확인했는지 점검)")
    # 인사이트
    if a.insights:
        fill_insights(root, json.load(open(a.insights, encoding="utf-8")))
    elif refresh:
        warn("갱신 모드: 기존 인사이트 문구를 유지함 → 문구 속 숫자가 시트와 맞는지 별도 대조 필요")
    else:
        warn("insights.json 이 없어 템플릿 월의 인사이트 문구가 그대로 남아 있음 → 반드시 새로 작성해야 함")

    new_tab = str(root)
    # 3) HTML 조립
    out = html
    if f'id="tab-{km.lower()}"' in html:       # 같은 월 재생성 → 교체
        s2 = html.index(f'<div class="tab-content" id="tab-{km.lower()}">')
        e2 = html.index(f"<!-- /tab-{km.lower()} -->")
        mp = minimal_patch(html[s2:e2], new_tab) if refresh else None
        if mp:
            out = html[:s2] + mp[0] + html[e2:]
            warn(f"최소 패치 적용: 값/속성이 달라진 {mp[1]}개 토큰만 교체 (서식 유지)")
        else:
            if refresh:
                warn("최소 패치 불가(탭 구조가 달라짐) → 탭 전체를 다시 직렬화함: git diff 가 커질 수 있으니 확인")
            out = html[:s2] + new_tab + "\n  " + html[e2:]
        js_s = out.index(f"let {km.lower()}ChartsInitialized = false;")
        js_e = out.index("// ── TAB SWITCHING ──")
        out = out[:js_s] + build_js(out[js_s:js_e], m, m, M, W) + out[js_e:]
    else:                                     # 신규 월 → 템플릿 탭 뒤에 삽입
        e_end = ei + len(e_mark)
        out = html[:e_end] + "\n\n  " + new_tab + f"\n  <!-- /tab-{km.lower()} -->" + html[e_end:]
        # 탭 버튼
        btn_t = re.search(rf'\s*<button class="month-tab-btn[^"]*" id="tabBtn{kt}" onclick="switchMonthTab\(\'{kt.lower()}\'\)">{t}월</button>', out).group(0)
        btn_new = f'\n    <button class="month-tab-btn" id="tabBtn{km}" onclick="switchMonthTab(\'{km.lower()}\')">{m}월</button>'
        out = out.replace(btn_t, re.sub(r'month-tab-btn active', 'month-tab-btn', btn_t) + btn_new, 1)
        # JS 함수 (템플릿 월 함수 복제)
        js_s = out.index(f"let {kt.lower()}ChartsInitialized = false;")
        js_e = out.index("// ── TAB SWITCHING ──")
        js_tpl = out[js_s:js_e]
        js_new = build_js(js_tpl, t, m, M, W)
        out = out[:js_e] + js_new + out[js_e:]
        # switchMonthTab
        last_m = f"document.getElementById('tabBtn{kt}').classList.toggle('active', month === '{kt.lower()}');"
        add_t = f"\n  document.getElementById('tab-{km.lower()}').classList.toggle('active', month === '{km.lower()}');"
        add_b = f"\n  document.getElementById('tabBtn{km}').classList.toggle('active', month === '{km.lower()}');"
        out = out.replace(f"document.getElementById('tab-{kt.lower()}').classList.toggle('active', month === '{kt.lower()}');",
                          f"document.getElementById('tab-{kt.lower()}').classList.toggle('active', month === '{kt.lower()}');" + add_t, 1)
        out = out.replace(last_m, last_m + add_b, 1)
        end_txt = f"{YEAR}.{m:02d}.{last.day} 기준"
        if partial:
            end_txt = f"{YEAR}.{m:02d}.{through.day:02d} 기준(잠정)"
        meta = f"meta.innerHTML = '<strong>{YEAR}년 {m}월 월간 매출 보고서</strong>{YEAR}.{m:02d}.01 ~ {end_txt.replace(' 기준', '')} 기준';"
        foot_d = through if partial else last
        foot = f"foot.textContent = '비드바이코리아 기획운영팀 내부 보고용 · {YEAR}년 {m}월 {foot_d.day}일 기준" + ("(잠정)" if partial else "") + "';"
        old_else = re.search(r"\} else \{\n(\s*meta\.innerHTML = '<strong>%d년 %d월.*?initSepCharts\(\);|\s*meta\.innerHTML = '<strong>%d년 %d월.*?init%sCharts\(\);)\n\s*\}" % (YEAR, t, YEAR, t, kt), out, re.S)
        if not old_else:
            sys.exit("switchMonthTab 의 마지막 else 분기를 찾지 못했습니다 (구조 변경됨)")
        blk = old_else.group(0)
        new_blk = (f"}} else if (month === '{kt.lower()}') {{" + blk[len("} else {"):-1].rstrip() + "\n  }"
                   f" else {{\n    {meta}\n    {foot}\n    init{km}Charts();\n  }}")
        out = out.replace(blk, new_blk, 1)
        out = re.sub(r"switchMonthTab\('%s'\);\n\}\);" % kt.lower(), f"switchMonthTab('{km.lower()}');\n}});", out)

    # 4) 문법 검사 (마지막 <script> 블록들)
    scripts = re.findall(r"<script>(.*?)</script>", out, re.S)
    for i, sc in enumerate(scripts):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(sc)
        r = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
        os.unlink(f.name)
        if r.returncode:
            warn(f"JS 문법 오류(script #{i}): {r.stderr.strip()[:300]}")

    # 5) 이상치 점검 (사람이 확인할 것)
    for ch in CH:
        c = M["ch"][ch]
        for key, lab in (("cnt", "주문건수"), ("fee", "수수료")):
            p_ = pct(c[key], c[key + "_pm"])
            if p_ is not None and abs(p_) >= 30 and not partial:
                warn(f"이상치 후보: {ch} {lab} 전월대비 {p_:+.1f}% (원인 확인 필요)")
    # 템플릿 월의 기존 추이 값과 시트의 과거월 값 비교 (시트 소급 수정 감지)
    old_list = re.search(r"var totalFee%s = (\[[^\]]*\])" % kt, html)
    if old_list:
        oldv = json.loads(old_list.group(1))
        for i, v in enumerate(oldv):
            if i < len(M["trend_man"]) and v != M["trend_man"][i]:
                warn(f"과거월 불일치: {i + 1}월 수수료 총액 기존 리포트 {v}만 → 시트 현재값 {M['trend_man'][i]}만 (시트 소급 수정 또는 수기 보정)")

    report = {
        "month": m, "template_month": t, "partial": partial, "data_through": str(through),
        "totals": {k: M["tot"][k] for k in ("cnt", "price", "fee", "rate", "rate_pm")},
        "weeks": [{"label": w["label"], **{k: int(v) for k, v in w["agg"].items()}} for w in W],
        "trend_man": M["trend_man"], "shipping": ship_info, "warnings": WARN,
        "blocking_issues": BLOCK, "acknowledged": bool(a.ack and BLOCK),
    }
    if a.report:
        json.dump(report, open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))
    if BLOCK:
        print("\n[확인 필요] 아래 항목은 사용자 확인 전에는 리포트에 쓰지 않습니다:")
        for b_ in BLOCK:
            print("  -", b_)
    if BLOCK and not a.ack and not a.dry_run:
        print("\n[중단] 파일을 쓰지 않았습니다. 사용자에게 확인받은 뒤 --ack 로 다시 실행하세요.")
        sys.exit(2)
    if not a.dry_run:
        open(a.out or a.html, "w", encoding="utf-8").write(out)
        print(f"\n[written] {a.out or a.html}")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:      # 이미 발견된 데이터 문제가 있으면 그것부터 보고
        if BLOCK:
            print("\n[확인 필요] 처리 중 오류가 발생했고, 그 전에 아래 데이터 문제가 발견되었습니다:")
            for b_ in BLOCK:
                print("  -", b_)
            print(f"\n(내부 오류: {type(e).__name__}: {e})")
            sys.exit(2)
        raise
