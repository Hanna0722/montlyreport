#!/usr/bin/env python3
"""구글시트 '내보내기 텍스트'(## Sheet name: ... 로 탭이 구분된 CSV) → build_month_tab.py 용 data.json

사용: python3 tools/export_to_data.py --sales <매출분석 내보내기> [--shipping <국제운송료 내보내기>] --out data.json
커넥터로 읽을 수 없을 때 사용자가 파일을 첨부해 주는 대체 경로, 그리고 테스트용.
"""
import argparse, csv, io, json, re


def sheets(path):
    out, name, buf = {}, None, []
    for line in open(path, encoding="utf-8").read().splitlines():
        m = re.match(r"## Sheet name: (.*)$", line)
        if m:
            if name is not None:
                out[name] = list(csv.reader(io.StringIO("\n".join(buf))))
            name, buf = m.group(1).strip(), []
        else:
            buf.append(line)
    if name is not None:
        out[name] = list(csv.reader(io.StringIO("\n".join(buf))))
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sales", required=True)
    ap.add_argument("--shipping")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    s = sheets(a.sales)
    data = {"정리": s["정리"], "SUM": s["SUM"]}
    if a.shipping:
        sh = sheets(a.shipping)
        data["shipping"] = sh["2026_shipping"]
        data["freight"] = sh["2026_운임정산(US/UK)"]
    json.dump(data, open(a.out, "w", encoding="utf-8"), ensure_ascii=False)
    print({k: len(v) for k, v in data.items()})
