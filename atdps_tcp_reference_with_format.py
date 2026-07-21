# -*- coding: utf-8 -*-
"""
================================================================================
 ATDPS 직접 TCP 송신 - 인쇄형식 포함 참조 구현 (논문 검증/재현용)
================================================================================

■ 목적
  자동조제기(ATDPS)로 조제 데이터를 TCP 로 직접 전송하는 방식을, 봉지 인쇄형식
  (P 라인)까지 포함해 제3자가 재현·검증할 수 있도록 만든 참조 구현입니다.
  실제 운영 코드의 처방 DB·마스터 의존성을 모두 제거하고, 익명화된 예시 처방과
  단일 인쇄서식(약품명, 82mm)만으로 동일한 프로토콜을 생성/전송합니다.

■ 구성 파일 (2개, 같은 폴더에 두십시오)
    - atdps_tcp_reference_with_format.py   (이 파일)
    - format_drugname_82mm.json            (인쇄서식: '약품명' 82mm)

■ 프로토콜 라인
    |ATDPS1|   시작 토큰
    |H|...|    헤더 (처방/환자 식별, 조제 모드)
    |D|...|    약품별 조제 정보 (여러 줄)
    |P|...|    봉지 인쇄 항목 (인쇄서식에서 좌표/폰트를 읽어 생성)
    |END|      종료 토큰
  각 라인은 CRLF('\r\n') 로 구분합니다.

■ 인쇄형식(P 라인) 개념
  봉지에 무엇을 어디에 인쇄할지는 인쇄서식(JSON)이 정의합니다. 서식의 각 항목은
  좌표(xpos,ypos)/폰트/크기와 '내용(content)'을 가지며, content 가 placeholder
  (#병실#, #환자명#, <약품 일반명>, <약품 수량>, $복용 일자$ 등)인 경우 처방의
  실제 값으로 치환되어 인쇄됩니다. 약품 목록은 서식의 반복 규칙(drug_rows)에 따라
  한 줄씩 좌표를 내려가며 배치합니다.

■ 실행
    python atdps_tcp_reference_with_format.py            # 프로토콜 생성/출력만
    python atdps_tcp_reference_with_format.py --send     # 실제 ATDPS 로 TCP 송신
================================================================================
"""
import os
import sys
import json
import time
import socket
from datetime import datetime


# ==============================================================================
# [설정] 병원/장비 한정 값 — 실제 환경에 맞게 수정하십시오.
#        배포본에는 문서화용 예시 값(RFC 5737 TEST-NET)이 들어 있습니다.
# ==============================================================================
ATDPS_HOST = "192.0.2.10"     # ← 실제 조제기 IP 로 교체 (예: 병원 내부망 고정 IP)
ATDPS_PORT = 1001             # ATDPS 수신 포트
ENCODING = "cp949"            # ATDPS 는 한글을 cp949 로 받음
SOCKET_TIMEOUT = 15
HANDSHAKE_GAP = 0.05
PROTO = "ATDPS1"
PINDEX_BASE = 900000

# 인쇄서식 파일 경로 (이 스크립트와 같은 폴더)
FORMAT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "format_drugname_82mm.json")


# ==============================================================================
# [익명화된 예시 처방]
#   실제로는 처방 DB(가공된 조제 결과)에서 만들어집니다. 여기서는 가상 처방을
#   직접 정의합니다. 헤더 인쇄에 쓰이는 환자정보(병실/진료과/성별/나이 등)도 포함.
# ==============================================================================
EXAMPLE_PRESCRIPTIONS = [
    {
        "pindex": PINDEX_BASE + 1,
        "patient_name": "홍길동",         # 가상
        "patient_no": "00000001",
        "visit_no": "00100",
        "admin_date": "2024-01-01",
        "urgent": False,
        # 헤더(P 라인) 인쇄용 환자정보
        "ward": "3W-101",                # 병실
        "dept": "내과",                  # 진료과
        "sex": "M",                      # 성별
        "age": "68",                     # 나이
        # 이 봉지의 복용 안내문 (<복용 설명>)
        "dose_note": "아침 식후 30분",
        "drugs": [
            {"name": "예시정A 10mg", "code": "EX0001", "canister": 12,
             "admin_day": 3, "qty": [1, 0, 1, 0, 0]},
            {"name": "예시정B 5mg", "code": "EX0002", "canister": 7,
             "admin_day": 3, "qty": [1, 0, 1, 1, 0]},
            {"name": "예시정C 25mg", "code": "EX0003", "canister": 20,
             "admin_day": 5, "qty": [1.5, 0, 0, 0, 0]},
            {"name": "예시정D 50mg", "code": "EX0004", "canister": 0,
             "admin_day": 3, "qty": [1, 0, 1, 0, 0]},
        ],
    },
]


# ==============================================================================
# [인쇄서식 로드]
# ==============================================================================
def load_format():
    with open(FORMAT_FILE, encoding="utf-8") as f:
        return json.load(f)


# ==============================================================================
# [수량 포맷 / 분리]
# ==============================================================================
def _fmt_qty(q):
    try:
        fv = float(q)
    except (ValueError, TypeError):
        return ""
    if fv == 0:
        return "0"
    if fv == int(fv):
        return str(int(fv))
    return ("%g" % fv)


def _split_qty(q):
    """1.5 -> (1.0, 0.5). 캐니스터 자동은 정수 알약만 → 소수부는 수동(STS)로 분리."""
    try:
        fv = float(q or 0)
    except (ValueError, TypeError):
        fv = 0.0
    ip = float(int(fv))
    return ip, round(fv - ip, 3)


# ==============================================================================
# [H / D 라인 생성]
# ==============================================================================
def build_hd_lines(rx):
    pidx = rx["pindex"]
    lines = []

    # H 라인 (헤더): PIndex, 환자명/번호/투약번호, 조제모드 플래그 등.
    standard_flag = "0"
    urgent_flag = "1" if rx.get("urgent") else "0"
    sep_number = 1
    h = ["", "H", str(pidx),
         rx.get("patient_name", ""), rx.get("patient_no", ""), rx.get("visit_no", ""),
         "----", standard_flag, str(sep_number), urgent_flag, "10",
         rx.get("admin_date", ""), "", "", "", "0", "", "0", "0", "0",
         "", "", "0", "01", "", "", "0", "0", ""]
    lines.append("|".join(h))

    # D 라인 (약품별 조제 정보). 소수 수량은 정수부(자동)+소수부(STS)로 분리.
    def emit_d(drug, cani, qty5, sts_flag):
        d = ["", "D", str(pidx),
             str(drug["admin_day"]), str(cani), drug["name"],
             _fmt_qty(qty5[0]), _fmt_qty(qty5[1]), _fmt_qty(qty5[2]),
             _fmt_qty(qty5[3]), _fmt_qty(qty5[4]),
             ("1" if sts_flag else "0"), "0", "", "", "", "", "",
             drug["code"], "", "", "", "0", "", "", "", "", "", "",
             "0", "", "0", "1", "0", "", "0", ""]
        lines.append("|".join(d))

    for drug in rx["drugs"]:
        qty = drug["qty"]
        cani = drug.get("canister", 0)
        try:
            has_cani = int(cani or 0) > 0
        except (ValueError, TypeError):
            has_cani = False
        if not has_cani:
            emit_d(drug, 0, qty, sts_flag=True)
            continue
        int_parts, frac_parts, has_frac = [], [], False
        for q in qty:
            ip, fp = _split_qty(q)
            int_parts.append(ip); frac_parts.append(fp)
            if fp > 0:
                has_frac = True
        if not has_frac:
            emit_d(drug, cani, qty, sts_flag=False)
        else:
            if any(ip > 0 for ip in int_parts):
                emit_d(drug, cani, int_parts, sts_flag=False)
            emit_d(drug, 0, frac_parts, sts_flag=True)

    return lines


# ==============================================================================
# [P 라인 생성] — 인쇄서식의 placeholder 를 처방 실제값으로 치환
# ==============================================================================
def build_p_lines(rx, fmt):
    """인쇄서식(fmt)에 따라 봉지 인쇄 항목(P 라인)을 생성.
    - fixed_items: 헤더/일자/안내문 등 고정 위치 항목. placeholder 를 실제값으로 치환.
    - drug_rows:   약품 목록을 반복 규칙에 따라 좌표를 내려가며 배치.
    """
    pidx = rx["pindex"]
    lines = []

    # 처방 → placeholder 치환 값 매핑
    subst = {
        "#병실#": rx.get("ward", ""),
        "#진료과#": rx.get("dept", ""),
        "#성별#": rx.get("sex", ""),
        "#나이#": rx.get("age", ""),
        "#환자 번호#": rx.get("patient_no", ""),
        "#환자명#": rx.get("patient_name", ""),
        "$복용 일자$": rx.get("admin_date", ""),
        "<복용 설명>": rx.get("dose_note", ""),
    }

    def emit_p(content, xpos, ypos, fontname, font_size, bold):
        # P 라인 필드: [1]='P' [2]=PIndex [4]=인쇄내용 [5]=x [6]=y
        #             [7]=폰트명 [8]=크기 [9]=굵기 ...
        f = ["", "P", str(pidx), "B",
             str(content), str(xpos), str(ypos),
             fontname, str(font_size), ("1" if bold else "0"),
             "0", "", "", "", "0", "", "0", ""]
        lines.append("|".join(f))

    # 1) 고정 항목: placeholder 면 실제값으로 치환해 인쇄
    for it in fmt.get("fixed_items", []):
        content = it["content"]
        if content in subst:
            value = subst[content]
            if value == "":
                continue   # 값이 없으면 인쇄 생략
            content = value
        emit_p(content, it["xpos"], it["ypos"], it["fontname"],
               it["font_size"], it["bold"])

    # 2) 약품 목록: 반복 규칙으로 한 줄씩 배치 (일반명 + 수량)
    dr = fmt["drug_rows"]
    y = dr["start_ypos"]
    for i, drug in enumerate(rx["drugs"]):
        if i >= dr.get("max_rows", 22):
            break
        total_qty = sum(float(q) for q in drug["qty"])   # 표시용 총 수량 예시
        emit_p(drug["name"], dr["name_xpos"], y, dr["fontname"],
               dr["name_font_size"], dr["bold"])
        emit_p(_fmt_qty(total_qty), dr["qty_xpos"], y, dr["fontname"],
               dr["qty_font_size"], dr["bold"])
        y += dr["row_step"]

    return lines


# ==============================================================================
# [프로토콜 텍스트 조립]
# ==============================================================================
def build_protocol_text(rx, fmt):
    lines = ["|%s|" % PROTO]
    lines += build_hd_lines(rx)
    lines += build_p_lines(rx, fmt)
    lines.append("|END|")
    return "\r\n".join(lines) + "\r\n"


# ==============================================================================
# [TCP 송신] — persistent socket + 최초 연결 시 핸드셰이크
# ==============================================================================
_sock = None


def _handshake(s):
    try:
        s.recv(4096)   # CONNECT_INFO
    except socket.timeout:
        pass
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for cmd in [
        "|%s|C|SERVER_OPTION|0|\r\n" % PROTO,
        "|%s|C|CUTTING_OPTION|0|0|0|0|0|\r\n" % PROTO,
        "|%s|C|MAIN_FRAME_REQUEST|\r\n" % PROTO,
        "|%s|C|HEATER_TEMP_CONTROL|%s||||\r\n" % (PROTO, now),
    ]:
        s.sendall(cmd.encode(ENCODING))
        time.sleep(HANDSHAKE_GAP)


def send_protocol(protocol_text, verbose=True):
    global _sock
    payload = protocol_text.encode(ENCODING)
    if _sock is not None:
        try:
            _sock.sendall(payload)
            if verbose:
                print("  [재사용] %d bytes 송신" % len(payload))
            return len(payload)
        except OSError:
            try:
                _sock.close()
            except OSError:
                pass
            _sock = None
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(SOCKET_TIMEOUT)
    if verbose:
        print("  [연결] TCP %s:%d (최초 연결 - 핸드셰이크 포함)" % (ATDPS_HOST, ATDPS_PORT))
    s.connect((ATDPS_HOST, ATDPS_PORT))
    _handshake(s)
    s.sendall(payload)
    _sock = s
    if verbose:
        print("  [송신] %d bytes (연결 유지)" % len(payload))
    return len(payload)


def close_socket():
    global _sock
    if _sock is not None:
        try:
            _sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            _sock.close()
        except OSError:
            pass
        _sock = None


# ==============================================================================
# [실행 진입점]
# ==============================================================================
def main():
    do_send = "--send" in sys.argv
    fmt = load_format()

    print("=" * 70)
    print(" ATDPS TCP 송신 참조 구현 (인쇄형식 포함) - 예시 처방 %d건"
          % len(EXAMPLE_PRESCRIPTIONS))
    print(" 인쇄서식: %s (%s)" % (fmt["format_name"], fmt["format_size"]))
    print(" 대상: %s:%d  |  실제 송신: %s"
          % (ATDPS_HOST, ATDPS_PORT, "예(--send)" if do_send else "아니오(생성만)"))
    print("=" * 70)

    for rx in EXAMPLE_PRESCRIPTIONS:
        text = build_protocol_text(rx, fmt)
        print("\n[처방 PIndex=%d, 환자=%s]" % (rx["pindex"], rx["patient_name"]))
        for line in text.split("\r\n"):
            if line:
                print("  " + line)
        if do_send:
            try:
                n = send_protocol(text)
                print("  -> 송신 완료 (%d bytes)" % n)
            except OSError as e:
                print("  -> 송신 실패: %s: %s" % (type(e).__name__, e))

    if do_send:
        close_socket()

    print("\n" + "=" * 70)
    print(" 주의: TCP 전송에 예외가 없어도 이는 '데이터가 장비에 도달'했다는 의미이며,")
    print("       조제 성공을 보장하지 않습니다. 실제 조제 결과는 ATDPS 장비 화면에서")
    print("       확인해야 합니다.")
    print("=" * 70)


if __name__ == "__main__":
    main()
