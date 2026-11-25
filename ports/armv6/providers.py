"""ARMv6-compatible quota readers. Only fixed HTTPS origins receive credentials."""
import json
import re
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


PROVIDERS = {
    "codex": {"name": "Codex", "credential": "已授權的 Codex 存取權杖",
              "hint": "相容於 CodexBar 的帳戶額度查詢，權杖到期需更換。只填存取權杖，勿貼登入檔或更新權杖。"},
    "antigravity": {"name": "Gemini / Antigravity", "credential": "已授權的 Google 存取權杖",
                    "hint": "實驗性相容查詢。Google 須授權你的帳戶及用戶端讀取 Gemini 配額。權杖過期後需更換。"},
    "deepseek": {"name": "DeepSeek", "credential": "官方 API 金鑰",
                 "hint": "顯示 DeepSeek Platform 帳戶各幣別的 API 餘額。"},
    "openrouter": {"name": "OpenRouter", "credential": "普通 API 金鑰",
                   "hint": "Key USD 是這把金鑰的可用預算。Key cap None 代表金鑰本身未設上限。"},
}

URLS = {"codex": "https://chatgpt.com/backend-api/wham/usage",
        "deepseek": "https://api.deepseek.com/user/balance",
        "openrouter": "https://openrouter.ai/api/v1/key",
        "antigravity_project": "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
        "antigravity": "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary"}
MAX_BODY = 256 * 1024


class ProviderError(ValueError):
    """Public, safe-to-display error; messages never include upstream data."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request_json(provider, secret, body=None):
    if not isinstance(secret, str) or not re.fullmatch(r"[!-~]{1,16384}", secret):
        raise ProviderError("請設定有效的存取憑證")
    headers = {"Authorization": "Bearer " + secret, "Accept": "application/json", "User-Agent": "QuotaDesk/1"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = Request(URLS[provider], data=None if body is None else json.dumps(body).encode(), headers=headers)
    try:
        with build_opener(NoRedirect).open(req, timeout=15) as response:
            body = response.read(MAX_BODY + 1)
            if len(body) > MAX_BODY:
                raise ProviderError("服務回應過大")
    except HTTPError as err:
        err.close()
        if err.code in (401, 403):
            raise ProviderError("憑證已失效或缺少額度查詢權限") from None
        if err.code == 429:
            raise ProviderError("服務暫時限制查詢頻率") from None
        raise ProviderError("額度服務暫時無法使用") from None
    except (OSError, HTTPException, URLError):
        raise ProviderError("無法連線至額度服務") from None
    try:
        return json.loads(body, parse_float=Decimal)
    except (ValueError, RecursionError):
        raise ProviderError("額度服務傳回無效資料") from None


def number(value):
    if type(value) not in (str, int, Decimal):
        raise ValueError
    n = Decimal(value)
    if not n.is_finite() or abs(n) >= 10 ** 12:
        raise ValueError
    return n


def money(label, amount):
    rounded = amount.quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    if abs(rounded) >= 10 ** 12:
        raise ValueError
    return {"label": label, "value": format(rounded if rounded else Decimal("0.00"), "f")}


def antigravity_project(account):
    # Google may return all-100% placeholder buckets for an ineligible client.
    if not isinstance(account, dict) or account.get("error"):
        raise ProviderError("Google 未授權查詢個人訂閱配額")
    for tier in account.get("ineligibleTiers") or []:
        if tier.get("reasonCode") in ("GOOGLE_TOS_NOT_SUPPORTED_BY_CLIENT", "UNSUPPORTED_CLIENT"):
            raise ProviderError("Google 未授權這個用戶端查詢配額")
    project = account.get("cloudaicompanionProject")
    if isinstance(project, dict):
        project = project.get("id") or project.get("projectId")
    if not isinstance(project, str) or not re.fullmatch(r"[A-Za-z0-9/_:.\-]{1,256}", project):
        raise ProviderError("Google 未回報已授權的配額專案")
    return project


def antigravity_metrics(data):
    if data.get("error") or data.get("code") not in (None, 0, 200, "OK"):
        raise ValueError
    quota = data.get("response") or data.get("summary") or data
    periods = {"5h": "5h", "session": "5h", "5-hour": "5h",
               "five-hour": "5h", "five hour": "5h", "weekly": "7d"}
    lanes = {}
    for group in quota["groups"]:
        for bucket in group["buckets"]:
            key = bucket.get("bucketId") or bucket.get("id") or ""
            key = key.lower().replace("_", "-").removesuffix(" limit")
            label = periods.get(key[7:]) if key.startswith("gemini-") else None
            explicit = bucket.get("window")
            if (not label or bucket.get("disabled", False) is not False or
                    (explicit and periods.get(explicit.lower()) != label)):
                continue
            fraction = bucket.get("remainingFraction")
            if fraction is None:
                remaining = bucket.get("remaining") or {}
                fraction = remaining.get("remainingFraction")
                if fraction is None and remaining.get("case") == "remainingFraction":
                    fraction = remaining.get("value")
            if fraction is None:
                continue
            measured = number(fraction)
            if not 0 <= measured <= 1 or label in lanes:
                raise ValueError
            lanes[label] = f"{(measured * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP)}%"
    if not lanes:
        raise ValueError
    return [{"label": label, "value": lanes.get(label, "--")} for label in ("5h", "7d")]


def metrics(provider, data):
    if provider == "deepseek":
        balances = {}
        for row in data["balance_infos"]:
            currency = row["currency"]
            if currency not in ("USD", "CNY") or currency in balances:
                raise ValueError
            balances[currency] = money(currency, number(row["total_balance"]))
        if balances:
            return list(balances.values())
    elif provider == "openrouter":
        key = data["data"]
        if key["limit"] is None and key.get("limit_remaining") is None:
            return [{"label": "Key cap", "value": "None"}]
        if number(key["limit"]) >= 0:
            return [money("Key USD", number(key["limit_remaining"]))]
    elif provider == "antigravity":
        return antigravity_metrics(data)
    elif provider == "codex":
        # The compatibility endpoint belongs to the Codex account, not the Platform API billing service.
        lanes = {}
        limits = data["rate_limit"]
        for lane in ("primary_window", "secondary_window"):
            window = limits.get(lane)
            if not isinstance(window, dict):
                continue
            label = {18000: "5h", 604800: "7d"}.get(window.get("limit_window_seconds"))
            if label and window.get("used_percent") is not None:
                used = number(window["used_percent"])
                if not 0 <= used <= 100 or label in lanes:
                    raise ValueError
                lanes[label] = f"{(100 - used).quantize(Decimal('1'), rounding=ROUND_HALF_UP)}%"
        if lanes:
            return [{"label": label, "value": lanes.get(label, "--")} for label in ("5h", "7d")]
    raise ValueError


def fetch(provider, secret):
    if provider not in PROVIDERS:
        raise ProviderError("不支援的額度來源")
    try:
        if provider == "antigravity":
            info = request_json("antigravity_project", secret, {"metadata": {}})
            data = request_json(provider, secret, {"project": antigravity_project(info)})
        else:
            data = request_json(provider, secret)
        return metrics(provider, data)
    except ProviderError:
        raise
    except (KeyError, ValueError, TypeError, DecimalException, AttributeError):
        raise ProviderError("服務未回報可辨識的額度") from None
