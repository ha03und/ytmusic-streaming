# ytmusic-streaming

유튜브 뮤직 재생수를 매일 자동으로 구글 시트(스트리밍 현황·주요곡)에 기입.

- **읽기**: [`ytmusicapi`](https://github.com/sigma67/ytmusicapi) 로 각 곡 검색 → `get_song` 의 `viewCount`(재생수). 검색 카드에 숫자가 안 보여도 API로는 항상 얻음(수작업 때처럼 앨범 페이지를 열 필요 없음)
- **쓰기**: `gspread`(서비스 계정)로 곡 탭의 오늘 날짜(YYYYMMDD) 행, **J열=오전 / K열=오후** 에 기입
- **스케줄**: GitHub Actions cron (매일 10시·16시 KST) — 로컬 수동 실행도 가능
- 이미 값이 있으면 건너뜀 · `꿈`은 메모(`part.3 ver ○○만회`)까지 · `V`는 값만(메모 미변경)

### 안정성·속도 개선 (v2)
- 재생수 조회를 **병렬 처리**(`MAX_WORKERS`) → 15곡 수 초 내 완료
- 시트 읽기(날짜행/기존값)를 `values_batch_get` **1회**, 쓰기를 `values_batch_update` **1회**로 묶음 → API 호출 급감, 쿼터 초과/간헐 오류 방지
- 모든 유튜브·시트 호출에 **지수 백오프 재시도**(`RETRIES`)
- 실행 끝에 **곡별 셀·값·건너뜀 사유** 요약 출력

> ⚠️ 유튜브 뮤직은 공식 재생수 API가 없어 비공식 `ytmusicapi`를 사용합니다. 인증(`browser.json`)은
> 만료되므로 실패가 반복되면 재발급하세요. 저장 값은 유튜브 표시값처럼 만/억 단위로 내림합니다
> (`ROUND_TO_MAN=0` 이면 원본 조회수 그대로 저장).

## 설정 대상 곡
`songs.yaml` 에서 곡 추가/삭제/검색어 수정. `worksheet` 는 시트 탭 이름과 정확히 일치해야 합니다.

## 필요한 것 (요약)
1. **서비스 계정**: Google Cloud에서 서비스 계정 생성 → JSON 키 발급 → 그 계정 이메일에게 대상 시트를 **편집자**로 공유. (Google Sheets API 활성화)
2. **ytmusicapi 인증**: 로컬에서 `pip install ytmusicapi` 후
   ```bash
   ytmusicapi browser   # 안내에 따라 브라우저 요청 헤더 붙여넣기 → browser.json 생성
   ```
3. **시트 ID**: 시트 URL `/d/<여기>/edit` 부분.

## 로컬 실행
```bash
pip install -r requirements.txt
export SHEET_ID="1QWz...WEs"
# browser.json, service_account.json 을 이 폴더에 둔다
python updater.py --slot auto           # 시각으로 오전/오후 자동 판별
python updater.py --slot morning        # 강제 오전(J)
python updater.py --slot afternoon --dry-run   # 시트에 쓰지 않고 읽기만
```

## GitHub Actions 자동화
리포지토리 **Settings → Secrets and variables → Actions** 에 3개 등록:

| Secret | 값 |
|---|---|
| `SHEET_ID` | 스프레드시트 ID |
| `YTMUSIC_AUTH` | `browser.json` 파일 내용 전체(JSON) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | 서비스 계정 키 JSON 전체 |

`.github/workflows/update.yml` 이 매일 10시/16시(KST) 실행. Actions 탭에서 **Run workflow** 로 수동 실행도 가능(slot 선택).

## 주의
- `browser.json`·`service_account.json` 은 **절대 커밋 금지** (`.gitignore` 처리됨).
- 검색 결과가 바뀌어 곡 매칭이 틀리면 `songs.yaml` 의 `title`/`artist` 를 조정하세요.
- `꿈` 두 버전 판별은 앨범명 키워드(`Part.3` / `SPECIAL`)로 합니다. 앨범명이 바뀌면 수정 필요.
