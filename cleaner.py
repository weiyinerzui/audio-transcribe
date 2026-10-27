"""文稿清理：去除口误、语气词、重复和语音识别错误。

默认走任意 OpenAI 兼容网关（自己配 base_url + key + model）：
  - 有时间戳的字幕 → 分批编号清理，保留时间轴（仍可导出 SRT）
  - 无时间戳的纯文稿 → 整篇润色

未配置网关时自动降级为 --clean rules（本地正则，免费、保留时间轴）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

import requests

from subtitle import Segment, Transcript

# 清理网关：任意 OpenAI 兼容端点（OpenAI / DeepSeek / 本地 Ollama 均可）
# 必须显式配置 NINEROUTER_BASE_URL，否则降级为本地规则清理，不会把文稿发到陌生主机。
ROUTER_BASE = os.environ.get("NINEROUTER_BASE_URL", "").strip()
ROUTER_MODEL = os.environ.get("NINEROUTER_MODEL", "").strip()

# 每批清理的段数（控制单次请求体积与延迟）
BATCH_SIZE = 30

CLEAN_RULES = (
    "清理要求：\n"
    "1. 删除口误、口头禅和语气词（如：嗯、啊、呃、哦、那个、就是、然后、就是说、对吧）；\n"
    "2. 删除无意义的重复和卡顿（如「今天今天」→「今天」、「一下下」→「一下」）；\n"
    "3. 修正明显的语音识别错别字；\n"
    "4. 严格保留原意、原语言和原有信息，不得增删事实、不得翻译、不得总结。"
)

SEGMENT_PROMPT = (
    "你是专业字幕校对员。下面是带编号的字幕行，请逐行清理。\n\n"
    + CLEAN_RULES
    + "\n5. 不得合并行也不得拆分行；输出数组长度必须等于 {count}，"
    "第 i 个元素只对应序号 i 那一行；\n"
    "6. 若某行删除语气词后已无任何实际信息，对应元素输出空字符串。\n\n"
    "输入（每行格式为 序号<TAB>文本）：\n{lines}\n\n"
    "只输出一个 JSON 字符串数组（长度恰好为 {count}），不要解释、不要 markdown 代码块。"
)

FULLTEXT_PROMPT = (
    "你是专业文稿校对员。请清理下面的语音转写文稿。\n\n"
    + CLEAN_RULES
    + "\n5. 保持自然分段，不要输出任何解释或标注。\n\n"
    "原始文稿：\n{text}\n\n"
    "只输出清理后的完整文稿正文，不要标注、不要 markdown 代码块。"
)


# ---------------------------------------------------------------- 基础设施

_PROVIDER_NAME = os.environ.get("NINEROUTER_PROVIDER", "9router")

def _provider_config() -> dict:
    """从 agent 配置文件读网关信息（可选，仅 pi 用户存在）。

    非 pi 环境下文件不存在，直接返回空，不影响其他 agent 使用；
    优先走 NINEROUTER_BASE_URL / NINEROUTER_MODEL / NINEROUTER_API_KEY 环境变量。
    """
    try:
        cfg = json.load(open(os.path.expanduser("~/.pi/agent/models.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for section in (cfg, cfg.get("providers") or {}):
        if isinstance(section, dict):
            entry = section.get(_PROVIDER_NAME)
            if isinstance(entry, dict) and (entry.get("apiKey") or entry.get("baseUrl")):
                return entry
    return {}


def _router_api_key(api_key: str | None = None) -> str:
    if api_key:
        return api_key.strip()
    for name in ("NINEROUTER_API_KEY", "ROUTER_API_KEY"):
        key = os.environ.get(name, "").strip()
        if key:
            return key
    key = str(_provider_config().get("apiKey") or "").strip()
    if key:
        return key
    raise RuntimeError(
        "未找到清理模型 API key：请设置 NINEROUTER_API_KEY（配合 NINEROUTER_BASE_URL / "
        "NINEROUTER_MODEL），或用 --clean rules 走免费本地规则清理"
    )


def _resolve_gateway(api_key: str | None, model: str | None, base_url: str | None) -> tuple[str, str, str]:
    """解析清理网关三要素。base_url/model 未配置时归一为一异常，由上层降级为 rules。"""
    key = _router_api_key(api_key)
    cfg = _provider_config()
    resolved_base = (base_url or ROUTER_BASE or cfg.get("baseUrl") or "").strip()
    resolved_model = (model or ROUTER_MODEL or "").strip()
    if not resolved_base or not resolved_model:
        raise RuntimeError(
            "未配置清理网关：请同时设置 NINEROUTER_BASE_URL 和 NINEROUTER_MODEL"
            "（或传 --clean-base-url / --clean-model）"
        )
    return key, resolved_model, resolved_base

def _budget(text: str, factor: int = 4, floor: int = 2048) -> int:
    """估算输出 token 预算。

    推理型模型（deepseek-r1 等）的思考过程也消耗 completion token，
    预算过小时会出现“正文为空”，因此按输入长度放大并设下限。
    """
    return max(floor, len(text) * factor)


# 空内容重试时逐级加大预算（推理模型思考长度不稳定，需要留足余量）
RETRY_BUDGETS = (8192, 16384)


def _chat(prompt: str, api_key: str, model: str, base_url: str, max_tokens: int,
          _attempt: int = 0) -> str:
    r = requests.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "stream": False,
        },
        timeout=300,
    )
    if r.status_code != 200:
        # 429 限流：退避重试，避免直接降级掉质量
        if r.status_code == 429 and _attempt < 4:
            wait = min(30, 5 * (2 ** _attempt))
            print(f"提示：清理模型限流，{wait}s 后重试（{_attempt + 1}/4）", file=sys.stderr)
            time.sleep(wait)
            return _chat(prompt, api_key, model, base_url, max_tokens, _attempt + 1)
        raise RuntimeError(f"清理模型请求失败（{r.status_code}）: {r.text[:300]}")
    try:
        content = (r.json()["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError, ValueError):
        raise RuntimeError("清理模型返回格式异常")
    if not content:
        # 推理模型偶发把预算全耗在思考上（finish_reason=max_tokens），加大预算重试
        for retry_budget in RETRY_BUDGETS:
            if retry_budget <= max_tokens:
                continue
            try:
                return _chat(prompt, api_key, model, base_url, retry_budget)
            except RuntimeError:
                continue
        raise RuntimeError("清理模型返回空内容（推理预算不足，已重试仍失败）")
    return content


def _parse_json_array(raw: str, expect: int) -> list[str] | None:
    """从模型输出里取出 JSON 字符串数组，容忍 ```json 包裹。"""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return None
    try:
        arr = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(arr, list) or len(arr) != expect:
        return None
    return [("" if item is None else str(item)).strip() for item in arr]


# ---------------------------------------------------------------- 公开接口

def clean_segments_batch(segments: list[Segment], api_key: str | None = None,
                         model: str | None = None, base_url: str | None = None) -> list[Segment]:
    """分批编号清理，严格保留每段的起止时间。"""
    api_key, model, base_url = _resolve_gateway(api_key, model, base_url)

    cleaned: list[Segment] = []
    for i in range(0, len(segments), BATCH_SIZE):
        batch = segments[i : i + BATCH_SIZE]
        lines = "\n".join(f"{j + 1}\t{s.text}" for j, s in enumerate(batch))
        prompt = SEGMENT_PROMPT.format(lines=lines, count=len(batch), i="i")
        raw = _chat(prompt, api_key, model, base_url,
                    max_tokens=_budget("".join(s.text for s in batch), factor=6, floor=4096))
        items = _parse_json_array(raw, len(batch))

        if items is None:
            # 行号解析失败时退回原始文本，绝不猜测时间轴对应关系
            cleaned.extend(batch)
            continue

        for seg, item in zip(batch, items):
            # item 为空串表示模型判定该行已无实际信息（纯语气词），
            # 交给 Transcript 过滤掉；仅在解析整体失败时才保留原文。
            cleaned.append(Segment(item, seg.start_ms, seg.end_ms))
    return cleaned


def clean_fulltext(text: str, api_key: str | None = None, model: str | None = None,
                   base_url: str | None = None) -> str:
    """整篇润色（用于无时间戳的纯文稿）。"""
    api_key, model, base_url = _resolve_gateway(api_key, model, base_url)

    # 超长文本分块，避免超出上下文
    limit = 6000
    if len(text) <= limit:
        chunks = [text]
    else:
        chunks = [text[i : i + limit] for i in range(0, len(text), limit)]

    out = []
    for chunk in chunks:
        out.append(
            _chat(FULLTEXT_PROMPT.format(text=chunk), api_key, model, base_url,
                  max_tokens=_budget(chunk, factor=6, floor=8192))
        )
    return "\n".join(out).strip()


# 本地规则清理：常见语气词与口头禅
_FILLER = re.compile(
    r"嗯+|啊+|呃+|哦+|噢+|哎+|唉+|嘛+|哈+|啧+|那个|这个|就是说|就是|然后|对吧|是吧|"
    r"怎么说呢|你知道吗|其实吧|反正"
)
_REPEAT = re.compile(r"([\u4e00-\u9fff]{1,4})\1")


def clean_with_rules(text: str) -> str:
    """本地规则清理（不联网、免费、保留原结构）。"""
    text = _FILLER.sub("", text)
    for _ in range(3):  # 递归折叠：看看看 → 看
        new = _REPEAT.sub(r"\1", text)
        if new == text:
            break
        text = new
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"[，,]{2,}", "，", text)
    text = re.sub(r"[。.]{2,}", "。", text)
    # 标点收尾：逗号后紧跟句末标点则删逗号；行首残留标点去掉
    text = re.sub(r"[，,、]\s*([。！？])", r"\1", text)
    text = re.sub(r"^[，,、。.\s]+", "", text)
    text = re.sub(r"\n[，,、]+", "\n", text)
    text = re.sub(r"[，,、][ \t]*$", "", text, flags=re.M)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def clean_transcript(transcript: Transcript, mode: str = "llm",
                     fulltext: bool = False, **kwargs) -> Transcript:
    """清理整份转录结果。

    - llm：默认按时间轴逐段清理以保留 SRT 时间码；fulltext=True 时整篇润色，
      跨句语义纠错更强但会丢失时间戳
    - rules：逐段本地规则清理（保留时间戳）
    - none：不清理
    """
    if mode == "none":
        return transcript

    if mode == "rules":
        return Transcript(
            [Segment(clean_with_rules(s.text), s.start_ms, s.end_ms) for s in transcript.segments],
            source=transcript.source,
        )

    if mode == "llm":
        if not transcript.segments:
            return transcript
        try:
            if transcript.has_timing and not fulltext:
                cleaned = clean_segments_batch(transcript.segments, **kwargs)
            else:
                cleaned = [Segment(clean_fulltext(transcript.full_text, **kwargs))]
            return Transcript(cleaned, source=transcript.source)
        except (RuntimeError, requests.RequestException) as exc:
            # 清理网关不可达时不要让整次转录失败：降级为本地规则清理
            print(f"警告：LLM 清理失败（{exc}），已降级为本地规则清理", file=sys.stderr)
            return clean_transcript(transcript, mode="rules")

    raise ValueError(f"未知清理模式: {mode}（可选 llm / rules / none）")
