#!/usr/bin/env python3
"""
스트리밍 현황(주요곡) — 유튜브 뮤직 재생수 자동 기입

각 곡 탭의 오늘 날짜(YYYYMMDD) 행에서
  오전 → J열 (유튜브뮤직 재생수 10시)
  오후 → K열 (유튜브뮤직 재생수 16시)
에 유튜브 뮤직 재생수를 기입한다.

실행:
  python updater.py --slot morning     # J열
  python updater.py --slot afternoon   # K열
  python updater.py --slot auto        # 예약 정보/시각으로 자동 판별 (기본)
  python updater.py --dry-run          # 시트에 쓰지 않고 읽기만
  python updater.py --only 10CM        # 한 곡만 (문제 진단용)

환경변수:
  SHEET_ID                      대상 스프레드시트 ID (필수)
  GOOGLE_SERVICE_ACCOUNT_JSON   서비스 계정 키 JSON 전체
  ROUND_TO_MAN=0                내림 없이 원본값 저장
  ATTEMPTS=3                    재생수를 못 읽었을 때 재시도 횟수


── 설계 원칙 (이전 판에서 사고가 났던 지점들) ──────────────────────

1. 읽은 값만 기입한다.
   예전엔 일부 곡에 보정계수를 곱해서 화면값에 맞췄는데(_SCALE), 두 숫자가
   자라는 속도가 달라 시간이 갈수록 어긋났다. 실측해보니 보정을 끈 값이
   유튜브뮤직 화면값과 정확히 일치했다. 그래서 어떤 곱셈도 하지 않는다.

2. 재생수가 붙은 검색 카드만 인정한다.
   카드에 재생수가 없으면 예전엔 get_song(player)으로 폴백했는데, 깃허브
   같은 데이터센터 IP에서는 그게 LOGIN_REQUIRED 로 막힌다. 로그인을 붙여도
   안 풀린다(2026-08-13 확인). 그래서 폴백을 아예 두지 않는다.

3. 못 읽으면 비워둔다.
   틀린 값이 들어가는 것보다 빈칸이 낫다. 못 읽은 곡은 오류가 아니라
   '수동 입력 대상'으로 보고한다.

4. 이상하면 쓰지 않는다.
   직전값 대비 너무 낮거나(다른 곡·오류) 너무 높으면(다른 곡) 기입하지 않는다.
   예전엔 낮은 쪽만 막아서, 도유카가 38만 -> 2440만(같은 가수의 다른 곡)으로
   튀었을 때 그대로 시트에 들어갔다.

5. 같은 조건으로 재시도하지 않는다.
   유튜브는 검색 결과에 재생수를 붙여줄 때도 있고 안 붙여줄 때도 있는데,
   예전 재시도는 같은 세션·같은 검색어로만 다시 물어봐서 늘 같은 답을 받았다.
   이 판은 시도할 때마다 세션을 새로 만들고 검색어도 바꿔가며 시도한다.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
import yaml
from ytmusicapi import YTMusic

KST = ZoneInfo("Asia/Seoul")

COL_MORNING = 10    # J
COL_AFTERNOON = 11  # K

ROUND_TO_MAN = os.environ.get("ROUND_TO_MAN", "1") != "0"
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "5"))
ATTEMPTS = int(os.environ.get("ATTEMPTS", "3"))

# 직전값 대비 허용 범위 (이 밖이면 기입하지 않음)
MIN_RATIO = 0.97
MAX_RATIO = 1.5


class NoPlays(Exception):
    """검색 결과에 재생수가 붙어오지 않아 읽지 못한 경우."""


# ============================================================================
# 유튜브 뮤직에서 재생수 읽기
# ============================================================================

_VIEWS = re.compile(r"([\d,.]+)\s*(억|만|천)?\s*회")


def parse_plays(text: str | None) -> int | None:
    """'3096만회' -> 30960000, '1.8억회' -> 180000000. 못 읽으면 None."""
    if not text:
        return None
    m = _VIEWS.search(str(text).replace(",", ""))
    if not m:
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    unit = {"억": 100_000_000, "만": 10_000, "천": 1_000}.get(m.group(2) or "", 1)
    return int(round(num * unit))


def norm(s: str | None) -> str:
    return "".join((s or "").lower().split())


def artists_of(card: dict) -> str:
    return " ".join(a.get("name", "") for a in card.get("artists", []) or [])


def new_session() -> YTMusic:
    """새 세션. 시도마다 새로 만드는 이유는 파일 상단 설계원칙 5번 참고."""
    return YTMusic(language="ko", location="KR")


def query_variants(cfg: dict) -> list[str]:
    """검색어 후보를 앞에서부터 시도한다.

    같은 곡이라도 검색어를 바꾸면 결과 구성이 달라져서, 어떤 검색어에서는
    재생수가 붙은 카드가 나오기도 한다. 그래서 한 가지만 쓰지 않는다.
    """
    title = cfg.get("title") or ""
    artist = cfg["artist"][0] if isinstance(cfg["artist"], list) else cfg["artist"]
    out: list[str] = []
    for q in (cfg["query"], f"{title} {artist}", f"{artist} {title}", title):
        q = " ".join(str(q).split())
        if q and q not in out:
            out.append(q)
    return out


def pick_card(cards: list[dict], cfg: dict) -> tuple[int, str] | None:
    """재생수가 붙은 카드 중에서 이 곡에 해당하는 것을 고른다.

    우선순위
      1) video_id 가 일치하는 카드
      2) 제목·아티스트가 모두 맞는 카드
    재생수가 없는 카드는 아예 후보로 보지 않는다(설계원칙 2번).
    """
    want_title = norm(cfg.get("title"))
    wants = cfg["artist"] if isinstance(cfg["artist"], list) else [cfg["artist"]]
    wants = [norm(a) for a in wants if norm(a)]
    vid = cfg.get("video_id")

    by_title: tuple[int, str] | None = None
    for c in cards:
        if c.get("resultType") not in (None, "song"):
            continue
        plays = parse_plays(c.get("views"))
        if not plays:
            continue

        title = c.get("title", "")
        if "inst" in norm(title) and "inst" not in want_title:
            continue  # (Inst.) 제외

        if vid and c.get("videoId") == vid:
            return plays, "videoId"

        if by_title is None and want_title:
            a = norm(artists_of(c))
            t = norm(title)
            if any(w in a for w in wants) and (t == want_title or want_title in t or t in want_title):
                by_title = (plays, "제목·아티스트")
    return by_title


def read_plays(cfg: dict) -> dict:
    """한 곡의 재생수를 읽는다. 세션과 검색어를 바꿔가며 시도한다.

    videoId 가 일치하는 카드를 끝까지 우선해서 찾는다. 제목·아티스트로만 맞은
    카드는 곧바로 쓰지 않고 최후 수단으로 남겨둔다.
    (2026-08-14: 이별후회가 첫 검색어에서 안 잡히자 다음 검색어의 '제목만 맞는'
     카드를 덥석 집어 4만회짜리 엉뚱한 값을 읽었다. 그래서 순서를 이렇게 바꿨다.)
    """
    name = cfg["worksheet"]
    loose: tuple[int, str] | None = None   # 제목·아티스트로만 맞은 후보

    for attempt in range(1, ATTEMPTS + 1):
        try:
            yt = new_session()
        except Exception as e:  # noqa: BLE001
            print(f"  [경고] {name}: 세션 생성 실패({e})")
            time.sleep(2 * attempt)
            continue

        for q in query_variants(cfg):
            try:
                cards = yt.search(q)
            except Exception as e:  # noqa: BLE001
                print(f"  [경고] {name}: 검색 실패({q}) {e}")
                continue

            got = pick_card(cards, cfg)
            if not got:
                continue
            plays, how = got

            if how == "videoId":          # 확실한 일치 — 바로 채택
                if attempt > 1 or q != cfg["query"]:
                    print(f"  [복구] {name}: {attempt}번째 시도 / 검색어 '{q}' 에서 읽음")
                return {"value": format_count(plays), "raw": plays,
                        "how": f"videoId/{q}", "sure": True}

            if loose is None:             # 제목만 맞음 — 일단 보류
                loose = (plays, q)

        if attempt < ATTEMPTS:
            time.sleep(2 * attempt)

    if loose is not None:
        plays, q = loose
        print(f"  [주의] {name}: videoId 가 맞는 카드를 못 찾아 제목으로 맞춤 "
              f"(검색어 '{q}') → 값 확인 필요")
        return {"value": format_count(plays), "raw": plays,
                "how": f"제목만/{q}", "sure": False}

    raise NoPlays("검색 결과에 재생수가 붙어오지 않음")


def read_dream(cfg: dict) -> dict:
    """태연 '꿈' — 두 버전을 videoId 로 구분한다.
    셀   = note_video_id (SPECIAL)
    메모 = 'part.3 ver ○○만회' (main_video_id = Part.3)
    """
    cell = read_plays({**cfg, "video_id": cfg["note_video_id"]})
    try:
        p3 = read_plays({**cfg, "video_id": cfg["main_video_id"]})
        cell["note"] = f"part.3 ver {p3['raw'] // 10_000}만회"
    except NoPlays:
        print(f"  [경고] 꿈: part.3 버전을 못 읽어 메모 생략")
    return cell


def read_one(cfg: dict) -> dict:
    return read_dream(cfg) if cfg.get("mode") == "dream" else read_plays(cfg)


def format_count(views: int) -> int:
    """유튜브 뮤직 표시값과 동일하게 내림.
    1억 이상은 0.1억 단위, 그 미만은 만 단위.
    (검색 카드에서 읽은 값은 이미 표시값이라 대개 그대로다.)
    """
    v = int(views)
    if not ROUND_TO_MAN:
        return v
    if v >= 100_000_000:
        return (v // 10_000_000) * 10_000_000
    return (v // 10_000) * 10_000


# ============================================================================
# 실행 슬롯 / 날짜
# ============================================================================
# GitHub 예약은 수 시간 늦게 돌 때가 있다. 실행 '시각'으로 오전/오후를 판별하면
# 늦게 돈 오전 실행이 오후로 잡혀 J열이 빈다. 그래서 '어느 예약에서 왔는지'로
# 열을 정하고, 날짜도 '원래 돌았어야 할 시각' 기준으로 잡는다.
CRON_SLOT = {
    "50 0 * * *": "morning",    # 09:50 KST
    "20 1 * * *": "morning",    # 10:20
    "50 1 * * *": "morning",    # 10:50
    "20 2 * * *": "morning",    # 11:20
    "0 0 * * *":  "morning",    # (구) 09:00
    "0 1 * * *":  "morning",    # (구) 10:00
    "55 6 * * *": "afternoon",  # 15:55
    "25 7 * * *": "afternoon",  # 16:25
    "55 7 * * *": "afternoon",  # 16:55
    "25 8 * * *": "afternoon",  # 17:25
    "0 7 * * *":  "afternoon",  # (구) 16:00
}


def event_schedule() -> str | None:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            sched = json.load(f).get("schedule")
    except Exception:  # noqa: BLE001
        return None
    return sched.strip() if sched else None


def resolve_slot(slot: str) -> tuple[str, int]:
    if slot == "auto":
        sched = event_schedule()
        slot = (CRON_SLOT.get(sched) if sched else None) or (
            "morning" if datetime.now(KST).hour < 12 else "afternoon"
        )
        if sched:
            print(f"[info] 예약 cron='{sched}' -> {slot}")
    return slot, (COL_MORNING if slot == "morning" else COL_AFTERNOON)


def target_date() -> str:
    now = datetime.now(KST)
    sched = event_schedule()
    if sched:
        try:
            minute, hour = int(sched.split()[0]), int(sched.split()[1])
            due = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
                hours=hour + 9, minutes=minute)
            if due > now:
                due -= timedelta(days=1)   # 어제 예약이 밀려서 지금 돈 것
            if due.date() != now.date():
                print(f"[info] 예약 지연 감지 -> {due:%Y%m%d} 행에 기입")
            return due.strftime("%Y%m%d")
        except (ValueError, IndexError):
            pass
    return now.strftime("%Y%m%d")


# ============================================================================
# 시트
# ============================================================================

def open_sheet():
    sheet_id = os.environ.get("SHEET_ID")
    if not sheet_id:
        sys.exit("환경변수 SHEET_ID 가 필요합니다.")
    if os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"):
        gc = gspread.service_account_from_dict(
            json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]))
    else:
        gc = gspread.service_account(filename="service_account.json")
    return gc.open_by_key(sheet_id)


def col_letter(col: int) -> str:
    return gspread.utils.rowcol_to_a1(1, col).rstrip("0123456789")


def find_row(col_a: list, date_str: str) -> int | None:
    for i, row in enumerate(col_a, start=1):
        if row and str(row[0]).strip() == date_str:
            return i
    return None


def last_value_above(col_vals: list, row: int) -> int | None:
    for rv in reversed(col_vals[: row - 1]):
        if rv and str(rv[0]).strip() not in ("", "None"):
            try:
                return int(str(rv[0]).replace(",", "").strip())
            except ValueError:
                return None
    return None


# ============================================================================
# 메인
# ============================================================================

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", choices=["morning", "afternoon", "auto"], default="auto")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="이 탭 이름 한 곡만 처리 (진단용)")
    ap.add_argument("--config", default="songs.yaml")
    args = ap.parse_args()

    slot, col = resolve_slot(args.slot)
    cl = col_letter(col)
    date_str = target_date()

    with open(args.config, encoding="utf-8") as f:
        songs = yaml.safe_load(f)["songs"]
    if args.only:
        songs = [s for s in songs if s["worksheet"] == args.only]
        if not songs:
            sys.exit(f"그런 탭 없음: {args.only}")

    print(f"== {date_str} / {slot} ({cl}열) / {len(songs)}곡 / dry_run={args.dry_run} ==")

    # 1) 재생수 조회 ----------------------------------------------------------
    got: dict[int, dict] = {}
    unread: list[str] = []      # 재생수를 못 읽은 곡

    def work(idx_cfg):
        i, cfg = idx_cfg
        try:
            return i, read_one(cfg), None
        except NoPlays as e:
            return i, None, str(e)
        except Exception as e:  # noqa: BLE001
            return i, None, f"오류: {e}"

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for i, data, err in ex.map(work, list(enumerate(songs))):
            name = songs[i]["worksheet"]
            if data:
                got[i] = data
                note = f"  +메모({data['note']})" if data.get("note") else ""
                print(f"[읽음] {name}: {data['value']:,}{note}")
            else:
                print(f"[못읽음] {name}: {err}")
                unread.append(name)

    if args.dry_run:
        print("\n[dry-run] 시트에 쓰지 않음.")
        if unread:
            print("수동 입력 필요: " + ", ".join(unread))
        return

    # 2) 시트 읽기 ------------------------------------------------------------
    sheet = open_sheet()
    ranges: list[str] = []
    for s in songs:
        ranges.append(f"'{s['worksheet']}'!A:A")
        ranges.append(f"'{s['worksheet']}'!{cl}:{cl}")
    vranges = sheet.values_batch_get(ranges)["valueRanges"]

    # 3) 쓸 셀 판정 -----------------------------------------------------------
    updates: list[dict] = []
    notes: list[tuple[str, str, str]] = []
    blocked: list[str] = []     # 값이 이상해서 막은 곡
    report: list[tuple[str, str]] = []

    for i, s in enumerate(songs):
        ws = s["worksheet"]
        if i not in got:
            continue
        val = got[i]["value"]

        col_a = vranges[2 * i].get("values", [])
        col_v = vranges[2 * i + 1].get("values", [])

        row = find_row(col_a, date_str)
        if row is None:
            report.append((ws, f"{date_str} 행 없음"))
            continue

        cur = col_v[row - 1][0] if len(col_v) >= row and col_v[row - 1] else ""
        if str(cur).strip() not in ("", "None"):
            report.append((ws, f"이미 값 있음({cur})"))
            continue

        prev = last_value_above(col_v, row)

        # videoId 확인이 안 된 값은 비교할 직전값이 없으면 쓰지 않는다.
        # (범위 검사로도 걸러낼 수 없어서 틀린 값이 그대로 들어갈 수 있다)
        if not got[i].get("sure", True) and not prev:
            print(f"[막음] {ws}: videoId 미확인 + 비교할 직전값 없음 → 수동 확인 필요")
            blocked.append(ws)
            continue

        if prev:
            if val < prev * MIN_RATIO:
                print(f"[막음] {ws}: {val:,} 이 직전값({prev:,})보다 크게 낮음")
                blocked.append(ws)
                continue
            if val > prev * MAX_RATIO:
                print(f"[막음] {ws}: {val:,} 이 직전값({prev:,})의 {MAX_RATIO}배 초과 "
                      f"— 다른 곡일 수 있음")
                blocked.append(ws)
                continue

        a1 = f"{cl}{row}"
        updates.append({"range": f"'{ws}'!{a1}", "values": [[val]]})
        if got[i].get("note"):
            notes.append((ws, a1, got[i]["note"]))
        report.append((ws, f"{a1} = {val:,}"))

    # 4) 쓰기 ----------------------------------------------------------------
    if updates:
        sheet.values_batch_update({"valueInputOption": "RAW", "data": updates})
    for ws, a1, note in notes:   # 꿈만 해당. V 등은 메모를 건드리지 않는다.
        sheet.worksheet(ws).update_note(a1, note)

    # 5) 요약 ----------------------------------------------------------------
    print("\n== 결과 ==")
    width = max((len(r[0]) for r in report), default=4)
    for ws, msg in report:
        print(f"  {ws:<{width}}  {msg}")

    todo = sorted(set(unread + blocked))
    print(f"\n기입 {len(updates)}곡 / 수동필요 {len(todo)}곡")
    if todo:
        print(">>> 손으로 넣을 곡: " + ", ".join(todo))
        print(f"    유튜브 뮤직 검색 → '노래' 필터 → 재생수 확인 → {cl}열에 입력")
    print("완료.")


if __name__ == "__main__":
    main()
