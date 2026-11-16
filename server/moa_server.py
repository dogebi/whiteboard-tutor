"""Whiteboard Tutor MOA server.

The browser never holds an API key: the page asks this server for a lesson and
the server runs a Mixture-of-Agents pass (twin-company twinmind MOA 방식) with
the keys from .env.

    GET  /health            -> {"backend": "llm", "providers": {...}}
    POST /lesson            -> {"lesson": {"steps": [...]}, "meta": {...}}
         body: {"question": "...", "lang": "ko", "steps": 8}

stdlib only — no pip install needed.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------- providers
# weight = 신뢰도. 합성 시 높은 weight 가 답변을 주도한다.
PROVIDERS = {
    "deepseek": {
        "base": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
        "key": "DEEPSEEK_API_KEY",
        "weight": 1.00,
        "json_mode": True,
    },
    "groq": {
        "base": "https://api.groq.com/openai/v1/chat/completions",
        "model": "openai/gpt-oss-20b",
        "key": "GROQ_API_KEY",
        "weight": 0.95,
        "json_mode": True,
    },
    "openrouter": {
        "base": "https://openrouter.ai/api/v1/chat/completions",
        # 2026-11-16 실측: minimax/minimax-m3:free 는 유료 전환(404/402) → 무료 풀에서
        # 실제로 응답한 baidu/ernie-5.5-flash-preview:free 사용 (1.2s, JSON 정상).
        "model": "baidu/ernie-5.5-flash-preview:free",
        "key": "OPENROUTER_API_KEY",
        "weight": 0.90,
        "json_mode": False,
    },
    "inception": {
        "base": "https://api.inceptionlabs.ai/v1/chat/completions",
        "model": "mercury-2",
        "key": "INCEPTION_API_KEY",
        "weight": 0.85,
        "json_mode": False,
        "optional": True,  # 무료 쿼터 소진(402) 시 자동 제외
    },
    "glm": {
        "base": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "model": "glm-4-flash",
        "key": "GLM_API_KEY",
        "weight": 0.80,
        "json_mode": False,
        # 중국 도메인이라 보통 프록시 우회가 낫지만, 이 PC의 WSL은 DNS가 깨져 있어
        # 직접 연결이 불가 → 기본은 프록시 경유. GLM_NO_PROXY=1 이면 직접 연결.
        "no_proxy": os.environ.get("GLM_NO_PROXY", "0") == "1",
    },
}

CACHE_TTL = float(os.environ.get("MOA_CACHE_TTL", "300"))
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()

SCHEMA_RULES = """You are a whiteboard tutor that explains topics with hand-drawn animations.
Return ONLY a JSON object (no markdown fences) shaped like:
{"steps":[{"els":[
  {"k":"t","x":0,"y":0,"t":"text","s":30,"c":"dark","f":"g"},
  {"k":"s","d":"M 100 100 L 300 120","c":"blue","w":2.6,"arrow":true,"fill":"pink","dash":false}
]}]}

Rules:
- Canvas is 1200 wide x 640 tall; keep everything inside (x:40-1160, y:50-620).
- "c" is one of: dark, red, blue, green, gray. "f": "g" = handwriting, "p" = plain hand.
- Step 1 is a big title (s 48-56) at y ~95 plus an underline stroke.
- Then 4-9 more steps teaching step by step, in the SAME LANGUAGE as the user's question.
- Keep each text under ~60 chars; use multiple text lines instead of long sentences.
- Use "s" strokes for simple diagrams (arrows, circles via path arcs, axes, waves).
- Text colors: key terms can be red/blue/green; conclusions green.
- Educational, warm, concise. Final step: a short green takeaway."""


# ---------------------------------------------------------------- .env
def load_dotenv(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            v = re.split(r"\s+#", v, maxsplit=1)[0]  # 인라인 주석 제거
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def apply_env_overrides() -> None:
    for name, cfg in PROVIDERS.items():
        env = name.upper().replace("-", "_")
        base = os.environ.get(env + "_BASE_URL")
        model = os.environ.get(env + "_MODEL")
        if base:
            cfg["base"] = base
        if model:
            cfg["model"] = model


def available_providers() -> list[str]:
    return [p for p, c in PROVIDERS.items() if c.get("key") and os.environ.get(c["key"])]


def main_provider() -> str:
    p = os.environ.get("MAIN_LLM", "glm").strip().lower()
    if p in PROVIDERS and os.environ.get(PROVIDERS[p]["key"] or ""):
        return p
    avail = available_providers()
    return avail[0] if avail else "glm"


def sub_providers(main: str) -> tuple[str, ...]:
    subs = tuple(
        p for p in os.environ.get("MOA_SUBS", "deepseek,openrouter,inception,glm").split(",")
        if p.strip() and p.strip() in PROVIDERS
    )
    subs = tuple(dict.fromkeys(p for p in subs if p != main))  # 메인 제외 (같은 키 동시 호출 방지)
    return tuple(sorted(subs, key=lambda p: PROVIDERS[p]["weight"], reverse=True))


# ---------------------------------------------------------------- http client
def _opener(cfg: dict):
    handlers = []
    # no_proxy 는 호출 시점에 env 로 판정한다 (.env 는 import 후에 로드되므로)
    no_proxy = cfg.get("no_proxy") or (cfg.get("key") == "GLM_API_KEY" and os.environ.get("GLM_NO_PROXY", "0") == "1")
    if no_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    else:
        proxies = {}
        for scheme in ("http", "https"):
            proxy = os.environ.get(scheme.upper() + "_PROXY") or os.environ.get(scheme.lower() + "_proxy")
            if proxy:
                proxies[scheme] = proxy
        if proxies:
            handlers.append(urllib.request.ProxyHandler(proxies))

    class _KeepMethod(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            new = super().redirect_request(req, fp, code, msg, headers, newurl)
            if new is not None and req.has_header("Content-Type"):
                new.method = getattr(req, "method", None) or "POST"
                new.data = req.data
            return new

    handlers.append(_KeepMethod())
    return urllib.request.build_opener(*handlers)


def chat(provider: str, system: str, user: str, temperature: float = 0.6, timeout: int = 90) -> str | None:
    cfg = PROVIDERS.get(provider)
    if not cfg:
        return None
    key = os.environ.get(cfg["key"] or "")
    if not key:
        return None
    body = {
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
    }
    if cfg.get("json_mode"):
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        cfg["base"],
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + key},
        method="POST",
    )
    try:
        with _opener(cfg).open(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        return content or None
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:160]
        except Exception:
            pass
        print(f"  [moa] {provider} HTTP {e.code} {detail}", file=sys.stderr)
        return None
    except Exception as e:  # noqa: BLE001 — 한 공급자 실패가 파이프라인을 막지 않는다
        print(f"  [moa] {provider} 실패: {type(e).__name__} {str(e)[:120]}", file=sys.stderr)
        return None


# ---------------------------------------------------------------- lesson handling
def parse_lesson(text: str | None) -> dict | None:
    if not text:
        return None
    text = re.sub(r"```(?:json)?|```", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return obj if validate(obj) else None


def validate(obj) -> bool:
    if not isinstance(obj, dict) or not isinstance(obj.get("steps"), list) or not obj["steps"]:
        return False
    for step in obj["steps"]:
        if not isinstance(step, dict) or not isinstance(step.get("els"), list):
            return False
        for el in step["els"]:
            if not isinstance(el, dict) or el.get("k") not in ("t", "s"):
                return False
            if el["k"] == "t" and not isinstance(el.get("t"), str):
                return False
            if el["k"] == "s" and el.get("shape") not in ("ellipse", "circle", "line") and not el.get("d"):
                return False
    return True


def _truncate(s: str, n: int = 6000) -> str:
    return s if len(s) <= n else s[: n - 600] + "\n…\n" + s[-600:]


def tidy(lesson: dict) -> dict:
    """LLM이 겹쳐 놓은 텍스트를 최소한으로 흩어 놓는다 (같은 줄에 두 글자가 겹치는 문제).

    좌표를 새로 디자인하지 않고, 같은 step 안에서 y가 8px 이내로 겹치는 텍스트만
    아래로 내려 캔버스(640) 안에 유지한다. 폰트 크기가 다르면 큰 쪽을 기준으로 간격을 잡는다.
    """
    for step in lesson.get("steps", []):
        placed: list[tuple[float, float, float]] = []  # (x, y, size)
        for el in step.get("els", []):
            if el.get("k") != "t":
                continue
            x = float(el.get("x") or 0)
            y = float(el.get("y") or 0)
            size = float(el.get("s") or 26)
            for px, py, ps in placed:
                same_line = abs(y - py) < 8
                overlap_x = abs(x - px) < max(120, (size + ps) * 3)
                if same_line and overlap_x:
                    y = py + max(size, ps) * 1.05
                    break
            if y > 610:
                y = 610
            el["y"] = round(y, 1)
            el["x"] = round(x, 1)
            placed.append((x, y, size))
    return lesson


def single_lesson(provider: str, user: str, t0: float) -> dict:
    """단일 공급자 모드 — deepseek 한 번(실패 시 1회 재시도)으로 레슨 생성.

    로컬 접속(127.0.0.1)의 기본 엔진. MOA 합성 단계가 없어 2배 이상 빠르다.
    """
    cfg = PROVIDERS[provider]
    lesson = None
    attempts = 0
    for temperature in (0.6, 0.45):
        attempts += 1
        lesson = parse_lesson(chat(provider, SCHEMA_RULES, user, temperature=temperature))
        if lesson:
            break
    if lesson is None:
        raise RuntimeError(f"{provider}({cfg['model']}) 응답을 레슨 JSON으로 변환하지 못했습니다")
    lesson = tidy(lesson)
    return {
        "lesson": lesson,
        "meta": {
            "backend": "single",
            "engine": provider,
            "main": provider,
            "subs": [],
            "answered": [provider],
            "failed": [],
            "models": {provider: cfg["model"]},
            "synthesized": False,
            "attempts": attempts,
            "ms": round((time.time() - t0) * 1000),
            "cached": False,
        },
    }


def make_lesson(question: str, lang: str | None, engine: str | None = None) -> dict:
    """engine="moa" → 메인+서브 병렬 생성 → 메인 합성 / engine="deepseek" → 단일 호출."""
    t0 = time.time()
    engine = (engine or os.environ.get("DEFAULT_ENGINE") or "deepseek").strip().lower()
    mp = main_provider()
    subs = sub_providers(mp)
    cache_key = f"{engine}|{mp}|{subs}|{lang}|{question.strip().lower()}"
    with _cache_lock:
        hit = _cache.get(cache_key)
        if hit and hit[0] > time.time():
            lesson = json.loads(json.dumps(hit[1]["lesson"]))
            meta = dict(hit[1]["meta"], cached=True, ms=round((time.time() - t0) * 1000))
            return {"lesson": lesson, "meta": meta}

    lang_hint = {"ko": "Korean", "en": "English", "ja": "Japanese", "zh": "Chinese"}.get(
        (lang or "").lower()[:2], None
    )
    user = question if not lang_hint else f"{question}\n\n(Write every label in {lang_hint}.)"

    if engine != "moa":
        provider = engine if engine in PROVIDERS and os.environ.get(PROVIDERS[engine]["key"] or "") else mp
        result = single_lesson(provider, user, t0)
        with _cache_lock:
            _cache[cache_key] = (time.time() + CACHE_TTL, {"lesson": result["lesson"], "meta": result["meta"]})
        return result

    results: dict[str, str | None] = {}

    def run(p: str) -> None:
        results[p] = chat(p, SCHEMA_RULES, user, temperature=0.6)

    threads = [threading.Thread(target=run, args=(p,)) for p in [mp, *subs]]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)

    valid = {p: parse_lesson(r) for p, r in results.items() if parse_lesson(r)}
    pool = [p for p in [mp, *subs] if p in valid]
    meta = {
        "backend": "moa",
        "main": mp,
        "subs": list(subs),
        "answered": pool,
        "failed": [p for p in [mp, *subs] if p not in valid],
        "models": {p: PROVIDERS[p]["model"] for p in pool},
        "weights": {p: PROVIDERS[p]["weight"] for p in pool},
    }

    if not pool:
        raise RuntimeError("모든 공급자가 실패했습니다 (키/프록시/모델명 확인) — " + ", ".join(failed_names(results)))

    if len(pool) == 1:
        lesson = valid[pool[0]]
        meta["synthesized"] = False
    else:
        blocks = "\n\n".join(f"[{p}·w{PROVIDERS[p]['weight']:.2f}]\n{_truncate(results[p] or '')}" for p in pool)
        synth_system = (
            "你是聚合器。下面是多个AI为同一问题生成的板书教学JSON(每条标注了可靠度w, w越高越可靠)。"
            "综合它们, 输出最准确、最完整的最终版本, 严格保持要求的JSON结构。"
            "以w高的回答为主, w低的作为补充; 结构冲突时优先采纳w最高且多数一致的内容。"
            "字段名不能改、不能删除。\n\n【要求的JSON结构】\n" + SCHEMA_RULES
        )
        lesson = parse_lesson(chat(mp, synth_system, blocks, temperature=0.3))
        meta["synthesized"] = lesson is not None
        if lesson is None:  # 합성 실패 → 최고 weight 답변 사용
            lesson = valid[pool[0]]

    lesson = tidy(lesson)
    meta["ms"] = round((time.time() - t0) * 1000)
    meta["cached"] = False
    with _cache_lock:
        _cache[cache_key] = (time.time() + CACHE_TTL, {"lesson": lesson, "meta": meta})
    return {"lesson": lesson, "meta": meta}


def failed_names(results: dict) -> list[str]:
    return [p for p, v in results.items() if not v] or ["(요청 없음)"]


# ---------------------------------------------------------------- http server
class Handler(BaseHTTPRequestHandler):
    server_version = "WhiteboardMOA/1.0"

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?")[0] in ("/", "/index.html"):
            page = os.path.join(ROOT, "index.html")
            if os.path.exists(page):
                body = open(page, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self._cors()
                self.end_headers()
                self.wfile.write(body)
                return
        if self.path.split("?")[0] == "/health":
            mp = main_provider()
            self._json(200, {
                "ok": bool(available_providers()),
                "backend": "moa",
                "default_engine": os.environ.get("DEFAULT_ENGINE", "deepseek"),
                "main": mp,
                "subs": list(sub_providers(mp)),
                "providers": {
                    p: {"model": c["model"], "weight": c["weight"],
                        "key": bool(os.environ.get(c["key"] or ""))}
                    for p, c in PROVIDERS.items()
                },
                "cache_ttl": CACHE_TTL,
            })
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0] != "/lesson":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            self._json(400, {"error": "invalid json"})
            return
        question = str(body.get("question") or "").strip()
        if not question:
            self._json(400, {"error": "question required"})
            return
        if len(question) > 500:
            self._json(400, {"error": "question too long (max 500 chars)"})
            return
        try:
            result = make_lesson(question, body.get("lang"), body.get("engine"))
        except Exception as e:  # noqa: BLE001
            self._json(502, {"error": str(e)[:300]})
            return
        self._json(200, result)

    def log_message(self, fmt: str, *args) -> None:  # 조용한 로그 (키 유출 방지)
        sys.stderr.write("  [http] %s\n" % (fmt % args))


def main() -> None:
    load_dotenv(os.path.join(ROOT, ".env"))
    apply_env_overrides()
    port = int(os.environ.get("MOA_PORT", "8390"))
    host = os.environ.get("MOA_HOST", "127.0.0.1")
    avail = available_providers()
    print(f"[moa] 화이트보드 튜터 서버 · http://{host}:{port}")
    print(f"[moa] 사용 가능 공급자: {avail or '없음 (.env 확인)'}")
    print(f"[moa] 메인={main_provider()} 서브={list(sub_providers(main_provider()))}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
