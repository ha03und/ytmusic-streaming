#!/usr/bin/env python3
"""
스트리밍 현황(주요곡) 자동 집계
- 유튜브 뮤직 재생수를 ytmusicapi 로 읽어서
- 구글 시트의 각 곡 탭, 오늘 날짜(YYYYMMDD) 행의 J(오전)/K(오후) 열에 기입한다.

실행:
    python updater.py --slot morning        # J열
    python updater.py --slot afternoon       # K열
    python updater.py --slot auto            # KST 시각으로 오전/오후 자동 판별 (기본)
    python updater.py --slot morning --dry-run   # 시트에 쓰지 않고 읽기만

인증 파일(둘 다 필요):
    browser.json            ytmusicapi 브라우저 인증 (ytmusicapi 로 생성)
    service_account.json    구글 서비스 계정 키 (시트를 이 계정과 공유)
  GitHub Actions 에서는 각각 환경변수 YTMUSIC_AUTH / GOOGLE_SERVICE_ACCOUNT_JSON 으로 주입.

환경변수:
    SHEET_ID                 대상 스프레드시트 ID (필수)
    ROUND_TO_MAN=0           설정 시 원본 조회수 그대로 저장(기본은 유튜브 표시값처럼 내림)
    MAX_WORKERS=6            재생수 병렬 조회 스레드 수
    RETRIES=3                네트워크 실패 시 재시도 횟수

성능/안정성(브라우저 수작업 대비 개선점):
  * 재생수 조회를 스레드로 병렬 처리 → 15곡을 수 초 내 완료
  * 시트 읽기(날짜행/기존값 확인)를 곡별 호출 대신 values_batch_get 1회로 묶음
  * 시트 쓰기를 셀 update 여러 번 대신 values_batch_update 1회로 묶음
    (API 호출 급감 → 쿼터 초과/간헐적 오류 방지)
  * ytmusicapi·gspread 호출에 지수 백오프 재시도
  * 이미 값이 있으면 건너뜀 · 꿈은 메모까지 · V는 값만(메모 미변경)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
import yaml
from ytmusicapi import YTMusic

KST = ZoneInfo("Asia/Seoul")
COL_MORNING = 10  # J열 (유튜브뮤직 재생수 10시)
COL_AFTERNOON = 11  # K열 (유튜브뮤직 재생수 16시)
ROUND_TO_MAN = os.environ.get("ROUND_TO_MAN", "1") != "0"
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "6"))
RETRIES = int(os.environ.get("RETRIES", "3"))

_AUTH_PATH = "browser.json"


# ----------------------------------------------------------------------------
# 유틸
# ----------------------------------------------------------------------------
def retry(fn, *args, **kwargs):
    """네트워크/쿼터 오류에 대비한 지수 백오프 재시도."""
    last = None
    for i in range(RETRIES):
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            last = e
            if i < RETRIES - 1:
                time.sleep(1.5 * (i + 1))
    raise last


def format_count(views: int) -> int:
    """유튜브 뮤직 한국어 표시값과 동일하게 내림한다.
    - 1억 미만: 만(10,000) 단위 내림  (예: 1,484,213 -> 1,480,000 = 148만)
    - 1억 이상: 0.1억(10,000,000) 단위 내림  (예: 183,900,000 -> 180,000,000 = 1.8억)
    ROUND_TO_MAN=0 이면 원본 그대로 반환.
    """
    v = int(views)
    if not ROUND_TO_MAN:
        return v
    if v >= 100_000_000:
        return (v // 10_000_000) * 10_000_000
    return (v // 10_000) * 10_000


def to_man(views: int) -> int:
    """만 단위 정수 (메모 표기용). 예: 50,315,000 -> 5031"""
    return int(views) // 10_000


def norm(s: str) -> str:
    return "".join((s or "").lower().split())


def col_letter(col: int) -> str:
    return gspread.utils.rowcol_to_a1(1, col).rstrip("1234567890")


# ----------------------------------------------------------------------------
# 인증
# ----------------------------------------------------------------------------
def ensure_auth_file() -> str | None:
    """browser.json 을 준비하고 경로를 반환. 없으면 None(비로그인 모드).

    검색/재생수 조회는 공개 데이터라 인증 없이도 동작한다.
    YTMUSIC_AUTH 를 설정하면 그 값을 browser.json 으로 써서 로그인 모드로 쓴다.
    """
    if os.environ.get("YTMUSIC_AUTH") and not os.path.exists(_AUTH_PATH):
        with open(_AUTH_PATH, "w", encoding="utf-8") as f:
            f.write(os.environ["YTMUSIC_AUTH"])
    if not os.path.exists(_AUTH_PATH):
        print("[info] browser.json 없음 → 비로그인 모드로 조회합니다.")
        return None
    return _AUTH_PATH


_tl = threading.local()


def get_yt() -> YTMusic:
    """스레드별 YTMusic 인스턴스(requests 세션 공유 회피)."""
    if not hasattr(_tl, "yt"):
        auth = _AUTH_PATH if os.path.exists(_AUTH_PATH) else None
        # 한국어 로케일: 검색 결과의 재생수가 "○○만회 / ○.○억회" 로 와서
        # 시트에 수작업으로 넣던 값과 동일하게 맞출 수 있다.
        _tl.yt = YTMusic(auth, language="ko", location="KR")
    return _tl.yt


def load_sheet():
    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        sys.exit("환경변수 SHEET_ID 가 필요합니다.")
    if os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
        gc = gspread.service_account_from_dict(info)
    else:
        gc = gspread.service_account(filename="service_account.json")
    return gc.open_by_key(sheet_id)


# ----------------------------------------------------------------------------
# 유튜브 뮤직 재생수 읽기
#   재생수는 get_song(videoId).videoDetails.viewCount 를 사용한다. YT Music 이
#   화면에 표시하는 "○○만회 재생" 과 동일하며, 검색 결과 카드에 숫자가 없어도
#   API 로는 항상 얻을 수 있다(수작업 때처럼 앨범 페이지를 열 필요가 없음).
# ----------------------------------------------------------------------------
_VIEWS_RE = re.compile(r"([\d,.]+)\s*(억|만|천)?")


def parse_ko_views(text: str) -> int | None:
    """검색 결과의 한국어 재생수 표기를 정수로. 예) '26만회'->260000, '1.8억회'->180000000"""
    if not text:
        return None
    t = str(text).replace(",", "").replace("회", "").strip()
    m = _VIEWS_RE.match(t)
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2)
    mult = {"억": 100_000_000, "만": 10_000, "천": 1_000}.get(unit or "", 1)
    return int(round(num * mult))


def views_of(item: dict) -> int:
    """검색 결과 항목의 재생수. 검색에 없으면 player(get_song)로 폴백."""
    v = parse_ko_views(item.get("views"))
    if v:
        return v
    song = retry(get_yt().get_song, item["videoId"])
    details = song.get("videoDetails") or {}
    if "viewCount" not in details:
        status = (song.get("playabilityStatus") or {}).get("status")
        raise RuntimeError(f"재생수를 읽지 못함 (검색결과에 없음, player status={status})")
    return int(details["viewCount"])


def get_view_count(video_id: str) -> int:
    song = retry(get_yt().get_song, video_id)
    return int(song["videoDetails"]["viewCount"])


def _title_ok(title: str, want_title: str | None) -> bool:
    """제목 일치. 완전일치 우선, 안 되면 부분 포함으로 완화(부제/피처링 표기 차이 대응)."""
    if not want_title:
        return True
    t = norm(title)
    return t == want_title or want_title in t or t in want_title


def artist_aliases(cfg: dict) -> list[str]:
    """songs.yaml 의 artist 는 문자열 또는 목록.

    유튜브뮤직을 한국어(ko)로 조회하면 아티스트명이 한글로 온다
    (예: fromis_9 -> 프로미스나인). 표기가 바뀔 수 있는 팀은 목록으로 적어둔다.
    """
    a = cfg["artist"]
    names = a if isinstance(a, list) else [a]
    return [norm(n) for n in names if norm(n)]


def _artist_ok(artists: str, wants: list[str]) -> bool:
    a = norm(artists)
    return any(w in a for w in wants)


def find_song(cfg: dict) -> dict | None:
    """검색 결과에서 아티스트(+제목)가 일치하는 첫 곡을 반환."""
    results = retry(get_yt().search, cfg["query"])
    wants = artist_aliases(cfg)
    want_title = norm(cfg.get("title", "")) or None
    fallback = None
    for r in results:
        artists = " ".join(a["name"] for a in r.get("artists", []))
        title = r.get("title", "")
        if "inst" in norm(title) and "inst" not in (want_title or ""):
            continue  # (Inst.) 버전 제외
        if r.get("resultType") != "song" or not _artist_ok(artists, wants):
            continue
        if fallback is None:
            fallback = r  # 아티스트만 맞는 첫 곡
        if _title_ok(title, want_title):
            return r
    return fallback


def read_normal(cfg: dict) -> dict:
    song = find_song(cfg)
    if not song:
        raise RuntimeError(f"곡을 찾지 못함: {cfg['query']} / {cfg['artist']}")
    views = views_of(song)
    return {"value": format_count(views), "raw": views}


def read_dream(cfg: dict) -> dict:
    """태연 '꿈' 두 버전을 앨범명으로 구분: 메인=셀값, part.3 ver=메모.
    두 버전이 같은 재생수로 표시될 수 있음(정상)."""
    results = retry(get_yt().search, cfg["query"])
    main_kw = norm(cfg["main_album_contains"])
    note_kw = norm(cfg["note_album_contains"])
    want_title = norm(cfg["title"])
    wants = artist_aliases(cfg)
    if cfg.get("main_video_id") and cfg.get("note_video_id"):
        mv = get_view_count(cfg["main_video_id"])
        nv = get_view_count(cfg["note_video_id"])
        return {"value": format_count(mv), "raw": mv, "note": f"part.3 ver {to_man(nv)}만회"}
    main_item = note_item = None
    for r in results:
        if r.get("resultType") != "song" or not _title_ok(r.get("title", ""), want_title):
            continue
        if not _artist_ok(" ".join(a["name"] for a in r.get("artists", [])), wants):
            continue
        album = norm((r.get("album") or {}).get("name", ""))
        if main_kw in album and not main_item:
            main_item = r
        elif note_kw in album and not note_item:
            note_item = r
    if not main_item or not note_item:
        raise RuntimeError("꿈 두 버전(메인/part.3 ver)을 모두 찾지 못함 - songs.yaml 앨범 키워드 확인")
    main_views = views_of(main_item)
    note_views = views_of(note_item)
    return {
        "value": format_count(main_views),
        "raw": main_views,
        "note": f"part.3 ver {to_man(note_views)}만회",
    }


def read_one(cfg: dict) -> dict:
    return read_dream(cfg) if cfg.get("mode") == "dream" else read_normal(cfg)


# ----------------------------------------------------------------------------
# 메인
# ----------------------------------------------------------------------------
# 예약(cron)별 고정 열. GitHub Actions 예약은 수 시간 지연될 수 있어서
# 실행 "시각"으로 오전/오후를 판별하면, 늦게 돈 오전 실행이 오후로 잡혀 J열이 빈다.
# 그래서 "어느 예약에서 왔는지"로 열을 정한다.
#
# ※ update.yml 의 cron 을 바꾸면 여기도 같이 추가할 것.
#   (없는 cron 이면 실행 시각으로 판별하는 예전 방식으로 돌아간다)
#
# 예약이 아예 실행되지 않는 날이 있어서(GitHub 부하로 드롭됨) 오전·오후 각각
# 30분 간격으로 여러 번 걸어둔다. 먼저 성공한 실행이 값을 넣고, 뒤에 오는
# 실행은 "이미 값 있음"으로 건너뛰므로 중복 기입은 생기지 않는다.
CRON_SLOT = {
    # 오전 -> 항상 J열
    "50 0 * * *": "morning",    # 09:50 KST
    "20 1 * * *": "morning",    # 10:20 KST
    "50 1 * * *": "morning",    # 10:50 KST
    "20 2 * * *": "morning",    # 11:20 KST
    "0 0 * * *": "morning",     # (구) 09:00 KST
    "0 1 * * *": "morning",     # (구) 10:00 KST
    # 오후 -> 항상 K열
    "55 6 * * *": "afternoon",  # 15:55 KST
    "25 7 * * *": "afternoon",  # 16:25 KST
    "55 7 * * *": "afternoon",  # 16:55 KST
    "25 8 * * *": "afternoon",  # 17:25 KST
    "0 7 * * *": "afternoon",   # (구) 16:00 KST
}


def event_schedule() -> str | None:
    """GitHub Actions 예약 실행이면 이 실행을 띄운 cron 문자열을 반환."""
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            sched = json.load(f).get("schedule")
    except Exception:  # noqa: BLE001
        return None
    return sched.strip() if sched else None


def slot_from_schedule() -> str | None:
    """어느 예약에서 왔는지로 슬롯(오전/오후)을 정한다."""
    sched = event_schedule()
    if not sched:
        return None
    slot = CRON_SLOT.get(sched)
    print(f"[info] 예약 cron='{sched}' -> slot={slot or '판별불가(시각으로 대체)'}")
    return slot


def target_date_str() -> str:
    """기입할 날짜(YYYYMMDD).

    예약이 크게 지연되어 자정을 넘겨 실행되면, '실행된 날짜'로 행을 찾을 경우
    엉뚱한 날짜에 값이 들어간다(예: 8/4 16:55 예약이 8/5 01:24 에 실행 → 8/5 에 기입).
    그래서 예약 실행일 때는 cron 에 적힌 '원래 돌았어야 할 시각'을 기준으로 날짜를 정한다.
    """
    now = datetime.now(KST)
    sched = event_schedule()
    if sched:
        try:
            minute, hour = int(sched.split()[0]), int(sched.split()[1])
            # cron 은 UTC 기준 -> KST 로 환산
            due = (now.replace(hour=0, minute=0, second=0, microsecond=0)
                   + timedelta(hours=hour + 9, minutes=minute))
            if due > now:
                # 오늘 예정 시각이 아직 안 왔다 = 어제 예약이 밀려서 지금 돈 것
                due -= timedelta(days=1)
            if due.date() != now.date():
                print(f"[info] 예약 지연 감지: 원래 {due:%Y-%m-%d %H:%M} KST 예정 "
                      f"-> {due:%Y%m%d} 행에 기입")
            return due.strftime("%Y%m%d")
        except (ValueError, IndexError):
            pass
    return now.strftime("%Y%m%d")


def resolve_slot(slot: str) -> tuple[str, int]:
    if slot == "auto":
        slot = slot_from_schedule() or (
            "morning" if datetime.now(KST).hour < 12 else "afternoon"
        )
    return slot, (COL_MORNING if slot == "morning" else COL_AFTERNOON)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", choices=["morning", "afternoon", "auto"], default="auto")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default="songs.yaml")
    args = ap.parse_args()

    slot, col = resolve_slot(args.slot)
    cl = col_letter(col)
    date_str = target_date_str()
    print(f"== {date_str} / {slot} (열={cl}) / round={'만' if ROUND_TO_MAN else 'raw'} / "
          f"dry_run={args.dry_run} ==")

    with open(args.config, encoding="utf-8") as f:
        songs = yaml.safe_load(f)["songs"]

    ensure_auth_file()

    # 1) 재생수 병렬 조회 -------------------------------------------------------
    data_by_idx: dict[int, dict] = {}
    errors: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(read_one, s): i for i, s in enumerate(songs)}
        for fut, i in futs.items():
            name = songs[i]["worksheet"]
            try:
                d = fut.result()
                data_by_idx[i] = d
                note = f" (메모: {d['note']})" if d.get("note") else ""
                print(f"[읽음] {name}: {d['raw']:,} -> {d['value']:,}{note}")
            except Exception as e:  # noqa: BLE001
                print(f"[오류] {name}: {e}", file=sys.stderr)
                errors.append((name, str(e)))

    if args.dry_run:
        print("\n[dry-run] 시트에 쓰지 않음.")
        sys.exit(1 if errors else 0)

    # 2) 시트 날짜행/기존값 배치 읽기 ------------------------------------------
    sheet = load_sheet()
    ranges: list[str] = []
    for s in songs:
        ws = s["worksheet"]
        ranges.append(f"'{ws}'!A:A")
        ranges.append(f"'{ws}'!{cl}:{cl}")
    vranges = retry(sheet.values_batch_get, ranges)["valueRanges"]

    # 3) 쓸 셀 계산 -------------------------------------------------------------
    updates: list[dict] = []
    notes: list[tuple[str, str, str]] = []
    report: list[tuple[str, str, str]] = []  # (worksheet, 셀/상태, 값/사유)
    for i, s in enumerate(songs):
        ws = s["worksheet"]
        if i not in data_by_idx:
            continue
        d = data_by_idx[i]
        a_vals = vranges[2 * i].get("values", [])
        c_vals = vranges[2 * i + 1].get("values", [])
        row = next((r for r, rv in enumerate(a_vals, start=1)
                    if rv and str(rv[0]).strip() == date_str), None)
        if row is None:
            report.append((ws, "건너뜀", f"{date_str} 행 없음"))
            continue
        cur = c_vals[row - 1][0] if len(c_vals) >= row and c_vals[row - 1] else ""
        if str(cur).strip() not in ("", "None"):
            report.append((ws, "건너뜀", f"이미 값 있음({cur})"))
            continue

        # 참고용 경고(기입은 그대로 진행). 재생수가 직전 기록보다 작으면
        # 다른 곡을 잡았을 수 있으니 로그에 표시만 해준다.
        # 유튜브뮤직 쪽 일시적 오류로 값이 튈 수도 있어 막지는 않는다.
        prev = None
        for rv in reversed(c_vals[: row - 1]):
            if rv and str(rv[0]).strip() not in ("", "None"):
                try:
                    prev = int(str(rv[0]).replace(",", "").strip())
                except ValueError:
                    prev = None
                break
        if prev is not None and d["value"] < prev:
            print(f"[주의] {ws}: 직전값({prev:,})보다 작음 → 곡 매칭 확인 권장")
            continue

        a1 = f"{cl}{row}"
        updates.append({"range": f"'{ws}'!{a1}", "values": [[d["value"]]]})
        if d.get("note"):
            notes.append((ws, a1, d["note"]))
        report.append((ws, a1, f"{d['value']:,}" + (f"  +메모('{d['note']}')" if d.get("note") else "")))

    # 4) 값 배치 쓰기 + 메모 ----------------------------------------------------
    if updates:
        retry(sheet.values_batch_update, {"valueInputOption": "RAW", "data": updates})
    for ws, a1, note in notes:
        wsobj = retry(sheet.worksheet, ws)
        retry(wsobj.update_note, a1, note)  # V 등 값만인 곡은 여기 오지 않음

    # 5) 요약 -------------------------------------------------------------------
    print("\n== 결과 ==")
    width = max((len(r[0]) for r in report), default=4)
    for ws, cell, val in report:
        print(f"  {ws:<{width}}  {cell:<8}  {val}")
    skipped = sum(1 for _, cell, _ in report if cell == "건너뜀")
    print(f"\n입력 {len(updates)}곡 / 건너뜀 {skipped}곡 / 오류 {len(errors)}곡")

    if errors:
        print("실패: " + ", ".join(n for n, _ in errors), file=sys.stderr)
        sys.exit(1)
    print("완료.")


if __name__ == "__main__":
    main()
