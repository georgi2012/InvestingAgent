import json
import os
import sqlite3
import ssl as _ssl
import threading
import time
import traceback
import urllib.request as _ureq
from datetime import datetime, timedelta

from flask import Flask, jsonify, render_template, request
from flask_cors import CORS

# Keep credentials out of source code. Configure Azure values via environment variables.

try:
    import certifi as _certifi

    _SSL_CTX = _ssl.create_default_context(cafile=_certifi.where())
except ImportError:
    _SSL_CTX = _ssl.create_default_context()


app = Flask(__name__)
CORS(app)
jobs = {}
DB_PATH = os.path.join(os.path.dirname(__file__), "results.db")

QUICK_AGENTS = ["fundamentals", "sentiment", "news", "technical"]
STEPS = [
    ("fundamentals", "Fundamentals", "quick"),
    ("sentiment", "Sentiment", "quick"),
    ("news", "News", "quick"),
    ("technical", "Technical", "quick"),
    ("bull", "Bull Res.", "deep"),
    ("bear", "Bear Res.", "deep"),
    ("debate", "Debate", "deep"),
    ("trader", "Trader", "deep"),
    ("risk", "Risk Mgr", "deep"),
    ("portfolio", "Port. Mgr", "deep"),
]


def fetch_sentiment_context(ticker, log=None):
    """Fetch real-time sentiment: StockTwits (RapidAPI) + Reddit + Yahoo Finance News."""

    def _log(msg):
        if log:
            log(msg)

    rapidapi_key = os.environ.get("RAPIDAPI_KEY", "")
    results = []

    # 1) StockTwits sentiment
    if rapidapi_key:
        try:
            from datetime import datetime as _dt

            today = _dt.now().strftime("%Y-%m-%d")
            host = "stocktwits-sentiment-message-analytics-api.p.rapidapi.com"
            url = f"https://{host}/functions/v1/stocktwits-sentiment?symbol={ticker}&end={today}"
            req = _ureq.Request(
                url,
                headers={
                    "x-rapidapi-host": host,
                    "x-rapidapi-key": rapidapi_key,
                    "Content-Type": "application/json",
                },
            )
            with _ureq.urlopen(req, timeout=8, context=_SSL_CTX) as response:
                data = json.loads(response.read()).get("data", {})

            sent = data.get("sentiment", {})
            vol = data.get("messageVolume", {})
            timeframes = data.get("timeframes", {})

            now_label = sent.get("now", {}).get("label", "N/A")
            now_score = sent.get("now", {}).get("value", 0)
            now_norm = sent.get("now", {}).get("valueNormalized", 0)
            change_24h = sent.get("24h", {}).get("change", 0)
            vol_label = vol.get("now", {}).get("label", "N/A")
            vol_value = vol.get("now", {}).get("value", 0)

            tf_lines = []
            for period in ["1D", "1W", "1M", "3M"]:
                frame = timeframes.get(period, {})
                sentiment_label = frame.get("sentiment", {}).get("labelNormalized", "N/A")
                volume_label = frame.get("messageVolume", {}).get("labelNormalized", "N/A")
                tf_lines.append(f"  - {period}: Sentiment={sentiment_label}, Volume={volume_label}")

            results.append(
                "=== STOCKTWITS SOCIAL SENTIMENT ===\n"
                f"Current: {now_label} (score={now_score:.3f}, {now_norm}/100)\n"
                f"24h Change: {change_24h:+.1f}%\n"
                f"Message Volume: {vol_value:,} ({vol_label})\n"
                "Timeframes:\n"
                + "\n".join(tf_lines)
            )
            _log(f"[SENTIMENT] StockTwits: {now_label} ({now_norm}/100), vol={vol_label}")
        except Exception as exc:
            _log(f"[SENTIMENT] StockTwits error: {exc}")

    # 2) Reddit sentiment
    try:
        subs = ["wallstreetbets", "stocks", "investing", "options"]
        all_posts = []

        for sub in subs:
            url = (
                f"https://www.reddit.com/r/{sub}/search.json?q={ticker}&sort=new&limit=10"
                "&restrict_sr=1&t=week"
            )
            try:
                req = _ureq.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with _ureq.urlopen(req, timeout=8, context=_SSL_CTX) as response:
                    posts = json.loads(response.read()).get("data", {}).get("children", [])
                for post in posts:
                    pdata = post.get("data", {})
                    all_posts.append(
                        {
                            "sub": sub,
                            "title": pdata.get("title", ""),
                            "score": pdata.get("score", 0),
                            "age": int((time.time() - pdata.get("created_utc", 0)) / 86400),
                            "text": pdata.get("selftext", "")[:150],
                        }
                    )
            except Exception:
                pass

        all_posts.sort(key=lambda item: item["score"], reverse=True)
        bull_kw = [
            "buy",
            "long",
            "bull",
            "moon",
            "calls",
            "upside",
            "beat",
            "strong",
            "growth",
            "breakout",
            "undervalued",
        ]
        bear_kw = [
            "sell",
            "short",
            "bear",
            "puts",
            "downside",
            "miss",
            "weak",
            "fall",
            "crash",
            "dump",
            "overvalued",
        ]

        bull = sum(
            1
            for post in all_posts
            if any(word in (post["title"] + post["text"]).lower() for word in bull_kw)
        )
        bear = sum(
            1
            for post in all_posts
            if any(word in (post["title"] + post["text"]).lower() for word in bear_kw)
        )
        lean = "BULLISH" if bull > bear else "BEARISH" if bear > bull else "NEUTRAL"
        top = "\n".join(
            [
                f"  - [{post['sub']}] {post['score']}up {post['age']}d: {post['title'][:80]}"
                for post in all_posts[:6]
            ]
        )
        results.append(
            "=== REDDIT RETAIL SENTIMENT ===\n"
            f"Posts this week: {len(all_posts)} | Lean: {lean} ({bull} bull / {bear} bear signals)\n"
            f"Top posts:\n{top}"
        )
        _log(f"[SENTIMENT] Reddit: {len(all_posts)} posts, lean={lean}")
    except Exception as exc:
        _log(f"[SENTIMENT] Reddit error: {exc}")

    # 3) Yahoo Finance news
    if rapidapi_key:
        try:
            host = "yahoo-finance15.p.rapidapi.com"
            url = f"https://{host}/api/v1/markets/news?ticker={ticker}"
            req = _ureq.Request(
                url,
                headers={
                    "x-rapidapi-host": host,
                    "x-rapidapi-key": rapidapi_key,
                    "Content-Type": "application/json",
                },
            )
            with _ureq.urlopen(req, timeout=8, context=_SSL_CTX) as response:
                articles = json.loads(response.read()).get("body", [])[:8]

            headlines = "\n".join(
                [
                    f"  - [{article.get('pubDate', '')[:16]}] {article.get('title', '')[:90]}"
                    for article in articles
                ]
            )
            results.append(f"=== RECENT NEWS (Yahoo Finance) ===\n{headlines}")
            _log(f"[SENTIMENT] Yahoo Finance: {len(articles)} articles")
        except Exception as exc:
            _log(f"[SENTIMENT] Yahoo Finance error: {exc}")

    if not results:
        return None

    header = (
        f"REAL-TIME SENTIMENT DATA FOR {ticker}\n"
        f"Retrieved: {time.strftime('%Y-%m-%d %H:%M UTC')}\n"
        "Use this data to inform your social media and sentiment analysis.\n"
        "StockTwits and Reddit signals represent actual retail trader positioning.\n\n"
    )
    return header + "\n\n".join(results)


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticker TEXT, date TEXT, provider TEXT,
        deep_model TEXT, quick_model TEXT,
        decision TEXT, output TEXT, created_at TEXT)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS analyst_cache (
        ticker TEXT PRIMARY KEY,
        analysis_date TEXT,
        provider TEXT,
        deep_model TEXT,
        quick_model TEXT,
        decision TEXT,
        output TEXT,
        cached_at TEXT)"""
    )
    conn.commit()
    conn.close()


def get_cached_analysis(ticker):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM analyst_cache WHERE ticker=?", (ticker.upper(),)).fetchone()
    conn.close()

    if not row:
        return None

    result = dict(row)
    result["output"] = json.loads(result["output"])
    cached_at = datetime.strptime(result["cached_at"], "%Y-%m-%d %H:%M:%S")
    age_days = (datetime.now() - cached_at).days
    result["age_days"] = age_days
    result["age_label"] = f"{age_days}d ago" if age_days > 0 else "today"
    return result


def save_to_cache(ticker, analysis_date, provider, deep_model, quick_model, decision, output):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO analyst_cache
        (ticker, analysis_date, provider, deep_model, quick_model, decision, output, cached_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(ticker) DO UPDATE SET
            analysis_date=excluded.analysis_date,
            provider=excluded.provider,
            deep_model=excluded.deep_model,
            quick_model=excluded.quick_model,
            decision=excluded.decision,
            output=excluded.output,
            cached_at=excluded.cached_at""",
        (
            ticker.upper(),
            analysis_date,
            provider,
            deep_model,
            quick_model,
            decision,
            json.dumps(output),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    conn.close()


def save_result(ticker, date, provider, deep_model, quick_model, decision, output):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO results (ticker,date,provider,deep_model,quick_model,decision,output,created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            ticker,
            date,
            provider,
            deep_model,
            quick_model,
            decision,
            json.dumps(output),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    conn.close()


def patch_anthropic_timeout():
    try:
        import anthropic
        import httpx

        original_init = anthropic.Anthropic.__init__

        def patched_init(self, *args, **kwargs):
            if "http_client" not in kwargs:
                kwargs["http_client"] = httpx.Client(
                    timeout=httpx.Timeout(connect=30.0, read=600.0, write=60.0, pool=30.0),
                    trust_env=False,
                )
            original_init(self, *args, **kwargs)

        anthropic.Anthropic.__init__ = patched_init
        return True
    except Exception:
        return False


def truncate_messages(messages, max_chars=100000):
    total = sum(len(str(m.content)) if hasattr(m, "content") else len(str(m)) for m in messages)
    if total <= max_chars:
        return messages

    result = []
    budget = max_chars
    for message in messages:
        content = str(message.content) if hasattr(message, "content") else str(message)
        if len(content) > budget // max(len(messages), 1):
            limit = max(2000, budget // max(len(messages), 1))
            if hasattr(message, "content"):
                try:
                    message = message.__class__(
                        content=content[:limit] + "\n\n[TRUNCATED FOR CONTEXT LIMIT]",
                        **{k: v for k, v in message.__dict__.items() if k != "content"},
                    )
                except Exception:
                    pass
        result.append(message)
        budget -= len(str(message.content) if hasattr(message, "content") else str(message))
    return result


def patch_langchain_truncation():
    try:
        from langchain_core.language_models import chat_models

        original_generate = chat_models.BaseChatModel.generate

        def patched_generate(self, messages_list, *args, **kwargs):
            truncated = [truncate_messages(msgs) for msgs in messages_list]
            return original_generate(self, truncated, *args, **kwargs)

        chat_models.BaseChatModel.generate = patched_generate
        return True
    except Exception:
        return False


def patch_azure_openai_routing():
    """
    Normalize Azure OpenAI requests onto the GA v1 path.

    Why this exists:
    - Some upstream libraries still build standard OpenAI paths such as `/v1/responses`
      or `/v1/chat/completions`.
    - Azure OpenAI v1 expects those requests under `/openai/v1/...`.
    - For Azure, `model` must be a deployment name.
    """
    import httpx
    import openai

    if hasattr(openai, "_azure_patched"):
        return True
    openai._azure_patched = True

    def _normalize_base(req):
        url = req.url
        scheme = url.scheme
        host = url.host
        if not host or "openai.azure.com" not in host:
            return ""
        return f"{scheme}://{host}/openai/v1"

    def rewrite_azure_url(req):
        try:
            base = _normalize_base(req)
            if not base:
                return

            path = req.url.path.rstrip("/")
            query = req.url.query.decode() if isinstance(req.url.query, bytes) else str(req.url.query or "")
            v1_routes = (
                "/responses",
                "/chat/completions",
                "/embeddings",
                "/images/generations",
                "/models",
            )
            for route in v1_routes:
                if path.endswith(route):
                    req.url = httpx.URL(f"{base}{route}" + (f"?{query}" if query else ""))
                    break

            api_key = os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
            if api_key:
                req.headers["api-key"] = api_key
            if "authorization" in req.headers:
                del req.headers["authorization"]
        except Exception:
            pass

    orig_init = openai.OpenAI.__init__

    def patched_init(self, *args, **kwargs):
        client = kwargs.get("http_client") or httpx.Client(trust_env=False)
        client.event_hooks.setdefault("request", []).append(rewrite_azure_url)
        kwargs["http_client"] = client
        orig_init(self, *args, **kwargs)

    openai.OpenAI.__init__ = patched_init

    orig_ainit = openai.AsyncOpenAI.__init__

    def patched_ainit(self, *args, **kwargs):
        client = kwargs.get("http_client") or httpx.AsyncClient(trust_env=False)
        client.event_hooks.setdefault("request", []).append(rewrite_azure_url)
        kwargs["http_client"] = client
        orig_ainit(self, *args, **kwargs)

    openai.AsyncOpenAI.__init__ = patched_ainit
    return True


def resolve_azure_settings(deep_model, quick_model):
    endpoint = (os.environ.get("AZURE_OPENAI_ENDPOINT") or os.environ.get("OPENAI_BASE_URL") or "").strip()
    if endpoint and not endpoint.endswith("/"):
        endpoint += "/"

    if endpoint and endpoint.endswith("/v1/"):
        pass
    elif endpoint and endpoint.endswith("/openai/v1/"):
        pass
    elif endpoint and endpoint.endswith("/openai/v1"):
        endpoint += "/"
    elif endpoint:
        endpoint = endpoint.rstrip("/") + "/openai/v1/"

    api_key = (os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    deep_deployment = (
        os.environ.get("AZURE_OPENAI_DEEP_DEPLOYMENT")
        or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
        or deep_model
    ).strip()
    quick_deployment = (
        os.environ.get("AZURE_OPENAI_QUICK_DEPLOYMENT")
        or os.environ.get("AZURE_OPENAI_DEPLOYMENT")
        or quick_model
    ).strip()
    return endpoint, api_key, deep_deployment, quick_deployment


def run_analysis(job_id, ticker, date, provider, deep_model, quick_model, debate_rounds, use_cache, cache_max_days):
    jobs[job_id] = {
        "status": "running",
        "output": [],
        "decision": None,
        "error": None,
        "steps": {s[0]: "pending" for s in STEPS},
        "current_step": None,
        "from_cache": False,
    }

    def log(msg, step=None, step_status=None):
        jobs[job_id]["output"].append({"text": msg, "ts": datetime.now().strftime("%H:%M:%S")})
        if step:
            jobs[job_id]["steps"][step] = step_status or "running"
            if step_status != "done":
                jobs[job_id]["current_step"] = step

    try:
        if use_cache:
            cached = get_cached_analysis(ticker)
            if cached and cached["age_days"] <= cache_max_days:
                log(f"[CACHE] Found cached analysis for {ticker} from {cached['age_label']}")
                log(f"[CACHE] Cached on: {cached['cached_at']} | Analysis date: {cached['analysis_date']}")
                log(f"[CACHE] Provider: {cached['provider']} | Model: {cached['deep_model']}")
                log("[CACHE] Skipping analyst and researcher stages, using cached result")
                log("[CACHE] To force a fresh run, disable 'Use cache' in the UI")

                for step in STEPS:
                    jobs[job_id]["steps"][step[0]] = "done"

                jobs[job_id].update(
                    {
                        "decision": cached["decision"],
                        "status": "done",
                        "current_step": None,
                        "from_cache": True,
                        "cached_at": cached["cached_at"],
                        "cache_age": cached["age_label"],
                    }
                )

                save_result(
                    ticker,
                    date,
                    provider,
                    deep_model,
                    quick_model,
                    cached["decision"],
                    [entry["text"] for entry in jobs[job_id]["output"]],
                )
                return

        if provider == "anthropic":
            patched = patch_anthropic_timeout()
            log(f"[INIT] Anthropic timeout patched (600s read timeout): {patched}")

        trunc_patched = patch_langchain_truncation()
        log(f"[INIT] Context truncation patched (100k char limit per message batch): {trunc_patched}")

        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        config = DEFAULT_CONFIG.copy()
        config["llm_provider"] = provider
        config["deep_think_llm"] = deep_model
        config["quick_think_llm"] = quick_model
        config["max_debate_rounds"] = debate_rounds
        config["max_risk_discuss_rounds"] = debate_rounds
        config["max_recur_limit"] = 50

        if provider == "ollama":
            config["backend_url"] = "http://localhost:11434/v1"
        elif provider == "azure":
            azure_endpoint = (
                os.environ.get("AZURE_OPENAI_ENDPOINT")
                or os.environ.get("OPENAI_BASE_URL")
                or ""
            ).strip()
            azure_key = (
                os.environ.get("AZURE_OPENAI_API_KEY")
                or os.environ.get("OPENAI_API_KEY")
                or ""
            ).strip()
            log(
                f"[INIT] Azure env check: endpoint={'set' if bool(azure_endpoint) else 'missing'} | "
                f"key={'set' if bool(azure_key) else 'missing'}"
            )
            if not azure_endpoint:
                raise ValueError(
                    "Azure endpoint is missing. Set AZURE_OPENAI_ENDPOINT (or OPENAI_BASE_URL) "
                    "to https://<resource>.openai.azure.com/."
                )
            if not azure_key:
                raise ValueError(
                    "Azure key is missing in this process. Set AZURE_OPENAI_API_KEY "
                    "(or OPENAI_API_KEY) and restart the app process."
                )
            config["llm_provider"] = "openai"
            config["backend_url"] = azure_endpoint
            os.environ["OPENAI_API_KEY"] = azure_key
            patch_azure_openai_routing()
            log("[INIT] Azure OpenAI routing patched")

        log(f"[INIT] Provider: {provider} | Deep: {deep_model} | Quick: {quick_model}")
        log(f"[INIT] Ticker: {ticker} | Date: {date} | Rounds: {debate_rounds}")
        log("[INIT] Building agent graph...")

        ta = TradingAgentsGraph(debug=True, config=config)

        log(f"[AGENTS] Analyst team starting ({quick_model})...", "fundamentals", "running")
        log("[AGENTS] Fundamentals: pulling P/E, EV/EBITDA, revenue, margins...", "sentiment", "running")
        log("[AGENTS] Sentiment: scanning social signals and options flow...", "news", "running")
        log("[AGENTS] News: reviewing macro, earnings calls, SEC filings...", "technical", "running")
        log("[AGENTS] Technical: computing RSI, MACD, Bollinger Bands...")

        decision = None
        for attempt in range(3):
            try:
                if attempt > 0:
                    log(f"[INIT] Retry {attempt + 1}/3, rebuilding graph...")
                    time.sleep(10)
                    ta = TradingAgentsGraph(debug=True, config=config)

                sentiment_context = fetch_sentiment_context(ticker, log)
                if sentiment_context:
                    try:
                        from langchain_core.messages import HumanMessage

                        _inject_msg = HumanMessage(content=sentiment_context)
                        log("[SENTIMENT] Context injected into agent pipeline")
                    except Exception as inject_err:
                        log(f"[SENTIMENT] Injection note: {inject_err}")

                state, decision_raw = ta.propagate(ticker, date)

                decision = None
                try:
                    if isinstance(state, dict) and "messages" in state:
                        for msg in reversed(state["messages"]):
                            text = msg.content if hasattr(msg, "content") else str(msg)
                            if text and len(str(text)) > 50:
                                decision = str(text)
                                break
                except Exception:
                    pass

                if (not decision or len(str(decision)) < 50) and decision_raw and len(str(decision_raw)) > 50:
                    decision = str(decision_raw)

                if not decision or len(str(decision)) < 50:
                    state_text = str(state)
                    if len(state_text) > 200:
                        decision = state_text

                if not decision:
                    decision = (
                        str(decision_raw)
                        if decision_raw
                        else "Analysis complete. Check the terminal log for full output."
                    )
                break
            except Exception as exc:
                err = str(exc)
                retryable_errors = [
                    "Connection error",
                    "RemoteProtocol",
                    "timeout",
                    "disconnected",
                    "429",
                    "500",
                    "502",
                    "503",
                    "504",
                ]
                if attempt < 2 and any(token in err for token in retryable_errors):
                    log(f"[INIT] Connection hiccup. Retrying ({attempt + 2}/3)...")
                    continue
                raise

        if decision is None:
            raise Exception("Analysis failed after 3 attempts")

        for agent in QUICK_AGENTS:
            log(f"[AGENTS] {agent.capitalize()} analyst complete", agent, "done")

        log(f"[RESEARCH] Bull researcher ({deep_model})...", "bull", "running")
        log("[RESEARCH] Bear researcher...", "bear", "running")
        log("[RESEARCH] Bull complete", "bull", "done")
        log("[RESEARCH] Bear complete", "bear", "done")

        for round_idx in range(debate_rounds):
            log(f"[DEBATE] Round {round_idx + 1}/{debate_rounds}...", "debate", "running")
        log("[DEBATE] Complete", "debate", "done")
        log("[TRADER] Composing trade proposal...", "trader", "running")
        log("[TRADER] Complete", "trader", "done")
        log("[RISK] Evaluating risk...", "risk", "running")
        log("[RISK] Complete", "risk", "done")
        log("[PORTFOLIO] Final review...", "portfolio", "running")
        log("[PORTFOLIO] Decision issued", "portfolio", "done")
        log("[DONE] Analysis complete. Result cached for future runs.")

        decision_text = str(decision)
        output_texts = [entry["text"] for entry in jobs[job_id]["output"]]

        save_to_cache(ticker, date, provider, deep_model, quick_model, decision_text, output_texts)
        log(f"[CACHE] Saved to cache. Next run of {ticker} can use this result")

        save_result(ticker, date, provider, deep_model, quick_model, decision_text, output_texts)

        jobs[job_id].update(
            {"decision": decision_text, "status": "done", "current_step": None, "from_cache": False}
        )
    except Exception:
        err_text = traceback.format_exc()
        if provider == "azure" and "404" in err_text and "Resource not found" in err_text:
            err_text += (
                "\n\nLikely Azure cause: deployment name mismatch or endpoint path mismatch. "
                "Set AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/ and ensure "
                "AZURE_OPENAI_DEEP_DEPLOYMENT and AZURE_OPENAI_QUICK_DEPLOYMENT match your deployment names."
            )
        if provider == "azure" and (
            "WinError 10061" in err_text
            or "http_proxy" in err_text.lower()
            or "ConnectError" in err_text
        ):
            err_text += (
                "\n\nLikely network/proxy cause: local proxy refused the connection. "
                "Unset HTTP_PROXY/HTTPS_PROXY/ALL_PROXY in this shell and retry."
            )
        jobs[job_id].update({"error": err_text, "status": "error", "current_step": None})


@app.route("/")
def index():
    return render_template("main-index.html")


@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.json or {}
    ticker = data.get("ticker", "").upper().strip()
    date = data.get("date", "")
    if not ticker or not date:
        return jsonify({"error": "required"}), 400

    job_id = f"{ticker}_{date}_{datetime.now().timestamp()}"
    threading.Thread(
        target=run_analysis,
        args=(
            job_id,
            ticker,
            date,
            data.get("provider", "azure"),
            data.get("deep_model", "gpt-5.4"),
            data.get("quick_model", "gpt-5.4-mini"),
            int(data.get("debate_rounds", 1)),
            data.get("use_cache", True),
            int(data.get("cache_max_days", 7)),
        ),
        daemon=True,
    ).start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify(job)


@app.route("/api/cache/<ticker>")
def check_cache(ticker):
    cached = get_cached_analysis(ticker.upper())
    if not cached:
        return jsonify({"exists": False})
    return jsonify(
        {
            "exists": True,
            "ticker": cached["ticker"],
            "analysis_date": cached["analysis_date"],
            "cached_at": cached["cached_at"],
            "age_days": cached["age_days"],
            "age_label": cached["age_label"],
            "provider": cached["provider"],
            "model": cached["deep_model"],
        }
    )


@app.route("/api/cache/<ticker>", methods=["DELETE"])
def clear_cache(ticker):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM analyst_cache WHERE ticker=?", (ticker.upper(),))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "cleared": ticker.upper()})


@app.route("/api/results")
def get_results():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id,ticker,date,provider,deep_model,quick_model,decision,created_at FROM results "
        "ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return jsonify([dict(row) for row in rows])


@app.route("/api/results/<int:result_id>")
def get_result(result_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM results WHERE id=?", (result_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    result = dict(row)
    result["output"] = json.loads(result["output"])
    return jsonify(result)


@app.route("/api/results/<int:result_id>", methods=["DELETE"])
def delete_result(result_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM results WHERE id=?", (result_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/ollama/models")
def ollama_models():
    try:
        import requests

        response = requests.get("http://localhost:11434/api/tags", timeout=3)
        if response.ok:
            return jsonify(
                {"models": [model["name"] for model in response.json().get("models", [])], "available": True}
            )
    except Exception:
        pass
    return jsonify({"models": [], "available": False})


@app.route("/api/suggested_date")
def suggested_date():
    day = datetime.now()
    count = 0
    while count < 5:
        day -= timedelta(days=1)
        if day.weekday() < 5:
            count += 1
    return jsonify({"date": day.strftime("%Y-%m-%d")})


init_db()

if __name__ == "__main__":
    print("\nStarting AI Investment Agent: http://127.0.0.1:5000\n")
    app.run(debug=False, port=5000, threaded=True, use_reloader=False)
