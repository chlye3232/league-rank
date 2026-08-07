# League Rank Web

이 폴더 전체가 비공개 GitHub 저장소에 올릴 배포 패키지입니다. 방문자에게는 `public/`만 배포되며 TikTok 토큰, 프록시, 원본 수집 결과는 포함되지 않습니다.

## 1. GitHub 저장소

1. GitHub에서 새 저장소를 만들고 **Private**를 선택합니다.
2. 이 폴더 안의 파일과 숨김 폴더 `.github`까지 저장소 루트에 올립니다.
3. 저장소 `Settings > Secrets and variables > Actions`로 이동합니다.

다음 **Repository secrets**를 추가합니다.

- `TIKTOK_TT_TOKEN`
- `PROXY_1` ~ `PROXY_5` 중 사용하는 만큼
- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`

프록시 예시: `https://user:password@host:port`

다음 **Repository variable**을 추가합니다.

- `CLOUDFLARE_PROJECT_NAME`: Cloudflare Pages에서 만든 프로젝트명

토큰이나 프록시는 `.env`, JavaScript, JSON, GitHub 코드에 직접 입력하지 마세요.

## 2. Cloudflare Pages

1. Cloudflare `Workers & Pages`에서 Direct Upload 방식의 Pages 프로젝트를 만듭니다.
2. Cloudflare API Token에는 필요한 계정의 `Cloudflare Pages: Edit` 권한만 부여합니다.
3. 프로젝트 이름을 GitHub 변수 `CLOUDFLARE_PROJECT_NAME`과 똑같이 입력합니다.

사이트 주소는 `https://<프로젝트명>.pages.dev`입니다.

## 3. 갱신과 배포

`.github/workflows/refresh-and-deploy.yml`이 약 1시간마다 다음 작업을 수행합니다.

1. 저장된 스냅샷과 ID 캐시 복원
2. 프록시 순환을 적용해 TikTok 리그 갱신
3. 실패한 리그와 성공한 리그를 비공개 이력에 기록
4. 실패한 리그가 있으면 약 5분 뒤 해당 리그만 한 번 재시도
5. `rank`, `id`만 포함한 공개 JSON 생성
6. `public/`만 Cloudflare Pages에 배포

갱신 이력, 실패 목록과 실행 로그는 GitHub Actions의 실행 요약 및 비공개 아티팩트에 90일간 보관됩니다. 재시도 후에도 실패하면 기존 저장 데이터를 유지하고 다음 1시간 정기 갱신에서 다시 시도합니다. 이 기록들은 `public/`에 포함되지 않습니다.

수동 갱신은 GitHub 저장소 `Actions > Refresh and deploy league rankings > Run workflow`에서 실행합니다.

## 4. 로컬 확인

```powershell
python build_static_site.py
python -m http.server 8788 --directory public
```

브라우저에서 `http://127.0.0.1:8788`을 엽니다.

## 공개되는 파일

- `public/index.html`
- `public/styles.css`
- `public/app.js`
- `public/data/rankings.json`
- `public/_headers`

`public/data/rankings.json`에는 리그, 순위와 TikTok 아이디만 들어갑니다. 갱신 시각, 실패 기록과 실행 로그는 공개되지 않습니다.
