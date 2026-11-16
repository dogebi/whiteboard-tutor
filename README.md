# Whiteboard Tutor · MOA

손으로 그리는 화이트보드 강의를 실시간으로 생성하는 단일 페이지 앱입니다.
질문을 넣으면 여러 LLM이 각자 레슨(JSON)을 만들고, **MOA(Mixture-of-Agents)** 합성기가
가중치가 높은 답변을 기준으로 하나의 최종 레슨을 만듭니다. (twin-company `twinmind` MOA 와 같은 방식)

- 프런트: `index.html` — 정적 단일 파일(SVG 필기 애니메이션 엔진 + 4개 내장 데모 레슨)
- 백엔드: `server/moa_server.py` — 키를 쥔 로컬 MOA 서버 (표준 라이브러리만, pip 설치 없음)

## 왜 서버가 필요한가 (키 보호)

GitHub Pages 는 정적 호스팅이라 API 키를 숨길 수 없습니다. 그래서 **브라우저에는 키가 없습니다**.
페이지는 `POST /lesson` 으로 MOA 서버에 질문만 보내고, 서버가 `.env` 의 키로 모델을 호출합니다.
키는 `.env`(gitignore)에만 있고 저장소·Pages 번들에는 들어가지 않습니다.

```
브라우저(GitHub Pages)  ──POST /lesson──▶  MOA 서버(내 PC)  ──▶  deepseek / openrouter / glm / …
        ◀── lesson JSON ──
```

## 실행

```bash
cp .env.example .env          # 키를 채운다 (절대 커밋 금지)
python3 server/moa_server.py  # 기본 http://127.0.0.1:8390
```

그 다음 `index.html` 을 열고(로컬 파일 또는 Pages) 톱니(⚙)에서 서버 주소를 확인합니다.
`?api=http://내서버:8390` 쿼리로도 지정할 수 있습니다.

## MOA 구성

| 역할 | 공급자 | 모델 | weight |
| --- | --- | --- | --- |
| 메인/합성기 | `MAIN_LLM` (기본 deepseek) | deepseek-chat | 1.00 |
| 서브 | openrouter | baidu/ernie-5.5-flash-preview:free | 0.90 |
| 서브 | inception | mercury-2 | 0.85 |
| 서브 | glm | glm-4-flash | 0.80 |

- 메인과 서브는 **동시에** 호출되고, 실패한 공급자는 조용히 제외되어 `meta.failed` 에 남습니다.
- 유효한 레슨 JSON을 낸 답변만 합성에 들어가고, 합성 결과도 스키마 검증을 통과해야 재생됩니다.
- 동일 질문은 `MOA_CACHE_TTL`(기본 300초) 동안 캐시합니다.

## API

```
GET  /health   → {"ok":true,"main":"deepseek","subs":[...],"providers":{...}}
POST /lesson   → {"question":"...","lang":"ko"} → {"lesson":{"steps":[...]},"meta":{...}}
```

`meta` 예시:

```json
{"main":"deepseek","answered":["deepseek","openrouter"],"failed":["inception","glm"],
 "synthesized":true,"ms":43518,"cached":false,
 "models":{"deepseek":"deepseek-chat","openrouter":"baidu/ernie-5.5-flash-preview:free"}}
```

## 레슨 스키마

```json
{"steps":[{"els":[
  {"k":"t","x":60,"y":95,"t":"제목","s":54,"c":"dark"},
  {"k":"s","d":"M 100 100 L 300 120","c":"blue","w":2.6,"arrow":true}
]}]}
```

캔버스는 1200×640, 색은 `dark|red|blue|green|gray`, 손글씨 폰트는 `f:"g"`.

## 배포

GitHub Pages 는 `main` 브랜치 루트를 그대로 서빙합니다.
`.env` 는 `.gitignore` 에 있어 배포되지 않으며, 배포본은 서버 주소(기본 `http://127.0.0.1:8390`)만 압니다.

## 라이선스

원본 화이트보드 튜터(`legacy/whiteboard-tutor.direct-key.html`)는 브라우저에서 직접 API 키를
입력하는 버전입니다. 참고용으로만 남겨두었고 배포 경로에서는 쓰지 않습니다.
