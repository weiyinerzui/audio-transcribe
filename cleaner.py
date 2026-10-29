"""文稿清理：去除口误、语气词、重复和语音识别错误。

默认走任意 OpenAI 兼容网关（自己配 base_url + key + model）：
  - 有时间戳的字幕 → 分批编号清理，保留时间轴（仍可导出 SRT）
  - 无时间戳的纯文稿 → 按段落分块整篇润色

未配置网关时自动降级为 --clean rules（本地正则，免费、保留时间轴）。

长文稿的稳定性设计（长音频必读）：
  - **流式请求**：按 SSE 增量读取，相邻数据块间隔超时（默认 120s），
    不再受"整段生成必须 300s 内完成"的限制；
  - **小分块**：默认 1200 字/块（NINEROUTER_CLEAN_CHUNK 可调），
    在段落/句子边界切分，避免推理模型单块耗时过长导致读超时；
  - **分块级容错**：某一块失败只保留该块原文，不会让全篇降级为 rules；
  - **预算递增**：输出为空或被截断（finish_reason=length）时逐级加大
    max_tokens 重试（推理模型的思考过程也占用 completion token）。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Callable

import requests

from subtitle import Segment, Transcript

# 清理网关：任意 OpenAI 兼容端点（OpenAI / DeepSeek / 本地 Ollama 均可）
# 必须显式配置 NINEROUTER_BASE_URL，否则降级为本地规则清理，不会把文稿发到陌生主机。
ROUTER_BASE = os.environ.get("NINEROUTER_BASE_URL", "").strip()
ROUTER_MODEL = os.environ.get("NINEROUTER_MODEL", "").strip()

# 每批清理的字幕段数（控制单次请求体积与延迟）
BATCH_SIZE = int(os.environ.get("NINEROUTER_CLEAN_BATCH", "30"))

# 整篇润色时的分块上限（长文稿的关键参数：越小越稳，越大越快）
DEFAULT_CHUNK_LIMIT = int(os.environ.get("NINEROUTER_CLEAN_CHUNK", "1200"))

# 流式请求：连接超时 30s；相邻数据块间隔超时 120s（推理停顿也很少超过这个值）
CONNECT_TIMEOUT = 30
READ_TIMEOUT = int(os.environ.get("NINEROUTER_CLEAN_READ_TIMEOUT", "120"))
MAX_RETRIES = 5

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

# ---------------------------------------------------------------- 网关配置

# 清理网关只通过环境变量 / 命令行参数配置，不读任何 agent 专属配置文件，
# 以保证这个 skill 对任意 agent、任意 OpenAI 兼容服务的行为一致。

def _router_api_key(api_key: str | None = None) -> str:
    if api_key:
        return api_key.strip()
    for name in ("NINEROUTER_API_KEY", "OPENAI_API_KEY"):
        key = os.environ.get(name, "").strip()
        if key:
            return key
    raise RuntimeError(
        "未找到清理模型 API key：请设置 NINEROUTER_API_KEY（配合 NINEROUTER_BASE_URL / "
        "NINEROUTER_MODEL），或用 --clean rules 走免费本地规则清理"
    )

def _resolve_gateway(api_key: str | None, model: str | None, base_url: str | None) -> tuple[str, str, str]:
    """解析清理网关三要素。base_url/model 未配置时归一为一异常，由上层降级为 rules。"""
    key = _router_api_key(api_key)
    resolved_base = (base_url or ROUTER_BASE or "").strip()
    resolved_model = (model or ROUTER_MODEL or "").strip()
    if not resolved_base or not resolved_model:
        raise RuntimeError(
            "未配置清理网关：请同时设置 NINEROUTER_BASE_URL 和 NINEROUTER_MODEL"
            "（或传 --clean-base-url / --clean-model）"
        )
    return key, resolved_model, resolved_base

# ---------------------------------------------------------------- 单次请求

def _budget(text: str, factor: int = 6, floor: int = 8192) -> int:
    """估算输出 token 预算。

    推理型模型（deepseek-r1 等）的思考过程也消耗 completion token，
    预算过小时会出现"正文为空"或被截断，因此按输入长度放大并设下限。
    """
    return max(floor, len(text) * factor)

# 输出为空 / 被截断时逐级加大预算
ESCALATE_BUDGETS = (8192, 16384, 32768)

def _extract(data: dict) -> tuple[str, bool]:
    """从非流式响应体取出 (正文, 是否被截断)。"""
    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    return content.strip(), choice.get("finish_reason") == "length"

def _request_once(prompt: str, api_key: str, model: str, base_url: str,
                  max_tokens: int, stream: bool) -> tuple[int, str, bool, str]:
    """发一次请求，返回 (状态码, 正文, 是否截断, 错误信息)。

    网络层异常（超时/断连）不在这里处理，向上抛给 _chat 退避重试。
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "stream": bool(stream),
    }

    if not stream:
        r = requests.post(url, headers=headers, json=payload,
                          timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        r.encoding = "utf-8"  # 网关常不带 charset，requests 会默认 ISO-8859-1 导致中文乱码
        if r.status_code != 200:
            return r.status_code, "", False, r.text[:300]
        try:
            content, truncated = _extract(r.json())
        except ValueError:
            return 200, "", False, "返回体不是 JSON"
        return 200, content, truncated, ""

    with requests.post(url, headers=headers, json=payload,
                       timeout=(CONNECT_TIMEOUT, READ_TIMEOUT), stream=True) as r:
        r.encoding = "utf-8"  # SSE 的 content-type 是 text/*，不指定会按 ISO-8859-1 解码致乱码
        if r.status_code != 200:
            return r.status_code, "", False, r.text[:300]
        ctype = (r.headers.get("content-type") or "").lower()
        if "text/event-stream" not in ctype:
            # 网关不支持流式，退化为整体读取
            try:
                content, truncated = _extract(r.json())
            except ValueError:
                return 200, "", False, "返回体不是 JSON"
            return 200, content, truncated, ""

        parts: list[str] = []
        truncated = False
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except ValueError:
                continue  # SSE 分帧：坏帧直接跳过，不影响整体
            choice = (obj.get("choices") or [{}])[0]
            piece = (choice.get("delta") or {}).get("content")
            if piece:
                parts.append(piece)
            if choice.get("finish_reason") == "length":
                truncated = True
        return 200, "".join(parts).strip(), truncated, ""

def _chat(prompt: str, api_key: str, model: str, base_url: str, max_tokens: int,
          stream: bool = True, _attempt: int = 0, _escalated: bool = False) -> str:
    """调用清理模型，带网络退避、限流退避和预算递增。"""
    try:
        status, content, truncated, err = _request_once(
            prompt, api_key, model, base_url, max_tokens, stream)
    except requests.RequestException as exc:
        if _attempt < MAX_RETRIES:
            wait = min(30, 3 * (2 ** _attempt))
            print(f"提示：清理模型网络异常，{wait}s 后重试（{_attempt + 1}/{MAX_RETRIES}）",
                  file=sys.stderr)
            time.sleep(wait)
            return _chat(prompt, api_key, model, base_url, max_tokens, stream,
                         _attempt + 1, _escalated)
        raise RuntimeError(f"清理模型请求失败（网络异常）: {exc}")

    # 429/5xx 多为限流或网关瞬时故障：退避重试，不要直接降级掉质量
    if status == 429 or status >= 500:
        if _attempt < MAX_RETRIES:
            wait = min(30, 3 * (2 ** _attempt))
            print(f"提示：清理模型返回 {status}，{wait}s 后重试（{_attempt + 1}/{MAX_RETRIES}）",
                  file=sys.stderr)
            time.sleep(wait)
            return _chat(prompt, api_key, model, base_url, max_tokens, stream,
                         _attempt + 1, _escalated)
        raise RuntimeError(f"清理模型请求失败（{status}）: {err}")

    if status != 200:
        raise RuntimeError(f"清理模型请求失败（{status}）: {err}")

    if content and not truncated:
        return content

    # 空内容或被截断：推理预算不足，逐级加大后重试
    if not _escalated:
        for budget in ESCALATE_BUDGETS:
            if budget <= max_tokens:
                continue
            try:
                return _chat(prompt, api_key, model, base_url, budget, stream,
                             0, _escalated=True)
            except RuntimeError:
                continue
    reason = "输出被截断" if truncated else "返回空内容"
    raise RuntimeError(f"清理模型{reason}（已加大预算仍失败）")

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

# ---------------------------------------------------------------- 分块工具

def _hard_split(s: str, limit: int) -> list[str]:
    return [s[i : i + limit] for i in range(0, len(s), limit)]

def _join(a: str, b: str) -> str:
    """拼接两段文本：中英混排都不产生多余空格。"""
    if not a:
        return b
    if a[-1].isascii() and a[-1].isalnum() and b[:1].isascii() and b[:1].isalnum():
        return a + " " + b
    return a + b

def _split_chunks(text: str, limit: int) -> list[str]:
    """按 段落 → 行 → 句子 → 硬切 的优先级分块，尽量不切断语义。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    units: list[str] = []
    for para in re.split(r"\n{2,}", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= limit:
            units.append(para)
            continue
        for line in para.split("\n"):
            line = line.strip()
            if not line:
                continue
            if len(line) <= limit:
                units.append(line)
                continue
            for sent in re.split(r"(?<=[。！？!?；;])\s*", line):
                sent = sent.strip()
                if not sent:
                    continue
                units.extend([sent] if len(sent) <= limit else _hard_split(sent, limit))

    chunks: list[str] = []
    cur = ""
    for u in units:
        cand = _join(cur, u) if cur else u
        if len(cand) <= limit:
            cur = cand
        else:
            if cur:
                chunks.append(cur)
            cur = u
    if cur:
        chunks.append(cur)
    return chunks

# ---------------------------------------------------------------- 公开接口

def clean_segments_batch(segments: list[Segment], api_key: str | None = None,
                         model: str | None = None, base_url: str | None = None,
                         verbose: bool = True,
                         on_progress: Callable[[int, int, str], None] | None = None,
                         ) -> list[Segment]:
    """分批编号清理，严格保留每段的起止时间。

    单批失败只保留该批原文，不影响其他批次。
    on_progress(current, total, label) 用于对外上报进度（CLI 忽略，Web 转 SSE）。
    """
    api_key, model, base_url = _resolve_gateway(api_key, model, base_url)

    total_batches = (len(segments) + BATCH_SIZE - 1) // BATCH_SIZE
    cleaned: list[Segment] = []
    failed = 0
    for bi, i in enumerate(range(0, len(segments), BATCH_SIZE), 1):
        batch = segments[i : i + BATCH_SIZE]
        lines = "\n".join(f"{j + 1}\t{s.text}" for j, s in enumerate(batch))
        prompt = SEGMENT_PROMPT.format(lines=lines, count=len(batch), i="i")
        try:
            raw = _chat(prompt, api_key, model, base_url,
                        max_tokens=_budget("".join(s.text for s in batch)))
        except (RuntimeError, requests.RequestException) as exc:
            failed += 1
            cleaned.extend(batch)
            print(f"  警告：第 {bi}/{total_batches} 批清理失败（{exc}），保留原文", file=sys.stderr)
            continue

        items = _parse_json_array(raw, len(batch))
        if items is None:
            # 行号解析失败时退回原始文本，绝不猜测时间轴对应关系
            cleaned.extend(batch)
            continue

        for seg, item in zip(batch, items):
            # item 为空串表示模型判定该行已无实际信息（纯语气词），
            # 交给 Transcript 过滤掉；仅在解析整体失败时才保留原文。
            cleaned.append(Segment(item, seg.start_ms, seg.end_ms))
        if verbose:
            print(f"  清理第 {bi}/{total_batches} 批完成", file=sys.stderr)
        if on_progress:
            on_progress(bi, total_batches, "批")

    if failed == total_batches:
        raise RuntimeError(f"全部 {total_batches} 批清理失败")
    return cleaned

def clean_fulltext(text: str, api_key: str | None = None, model: str | None = None,
                   base_url: str | None = None, chunk_limit: int | None = None,
                   verbose: bool = True,
                   on_progress: Callable[[int, int, str], None] | None = None) -> str:
    """整篇润色（用于无时间戳的纯文稿）。

    按段落分块逐块清理；单块失败只保留该块原文，不影响其他块。
    """
    api_key, model, base_url = _resolve_gateway(api_key, model, base_url)

    limit = chunk_limit or DEFAULT_CHUNK_LIMIT
    chunks = _split_chunks(text, limit)
    if not chunks:
        return ""

    out: list[str] = []
    failed = 0
    for i, chunk in enumerate(chunks, 1):
        try:
            cleaned = _chat(FULLTEXT_PROMPT.format(text=chunk), api_key, model, base_url,
                            max_tokens=_budget(chunk))
            out.append(cleaned)
            if verbose:
                print(f"  清理第 {i}/{len(chunks)} 块完成", file=sys.stderr)
            if on_progress:
                on_progress(i, len(chunks), "块")
        except (RuntimeError, requests.RequestException) as exc:
            failed += 1
            out.append(chunk)
            print(f"  警告：第 {i}/{len(chunks)} 块清理失败（{exc}），保留原文", file=sys.stderr)

    if failed == len(chunks):
        raise RuntimeError(f"全部 {len(chunks)} 块清理失败")
    return "\n\n".join(out).strip()

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
                     fulltext: bool = False, chunk_limit: int | None = None,
                     on_progress: Callable[[int, int, str], None] | None = None,
                     **kwargs) -> Transcript:
    """清理整份转录结果。

    - llm：默认按时间轴逐段清理以保留 SRT 时间码；fulltext=True 时整篇润色，
      跨句语义纠错更强但会丢失时间戳
    - rules：逐段本地规则清理（保留时间戳）
    - none：不清理

    LLM 模式下单块/单批失败只保留对应原文；只有网关整体不可用时
    才降级为本地规则清理。
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
                cleaned = clean_segments_batch(transcript.segments, on_progress=on_progress,
                                               **kwargs)
            else:
                cleaned = [Segment(clean_fulltext(transcript.full_text,
                                                  chunk_limit=chunk_limit,
                                                  on_progress=on_progress, **kwargs))]
            return Transcript(cleaned, source=transcript.source)
        except (RuntimeError, requests.RequestException) as exc:
            # 清理网关不可达时不要让整次转录失败：降级为本地规则清理
            print(f"警告：LLM 清理失败（{exc}），已降级为本地规则清理", file=sys.stderr)
            return clean_transcript(transcript, mode="rules")

    raise ValueError(f"未知清理模式: {mode}（可选 llm / rules / none）")
