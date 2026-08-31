"""
providers/client.py — The single generic OpenAI-compatible HTTP client.

This is the ONLY module in v4 that talks HTTP to an LLM. It knows nothing
about any specific provider: everything comes from the user-supplied
base_url + optional api_key. Adapted from v3.6's OpenAICompatProvider
(03_PROVIDER_SYSTEM.md §1), with the Gemini-native and Cerebras-specific
branches removed.

Never prints. Failures either raise ProviderError (generate, generate_chat,
stream_chat) or are captured into a result object (ping_model / list_models
via safe wrappers).

The translation pipeline never streams — a batch is either complete or it is
not, and a half-arrived batch cannot be written to a spreadsheet. `stream_chat`
exists only for the Playground's interactive chat, where watching the text
arrive *is* the point; nothing in engine/scheduler may call it.

Reasoning is passed in as a *specification* rather than a level: what parameter
a model takes to think, and whether it can be told not to, is metadata on the
model (providers/reasoning.py, ADR-003). This module inserts the fragment that
module builds and never invents one of its own.
"""

from __future__ import annotations

import json
import threading
import time
import weakref
from dataclasses import dataclass, field
from typing import Callable

import requests

from .. import config
from ..core.models import ProviderError
from .http_utils import (
    classify_rate_limit,
    is_quota_error,
    parse_retry_delay,
    remaining_tokens,
)
from .reasoning import ReasoningConfig, ResolvedReasoning, merge_payload, resolve


@dataclass
class GenerateResult:
    content: str
    tokens:  int = 0
    headers: dict = field(default_factory=dict)
    latency_ms: float = 0.0
    # The split below is what the Playground compares models on. `tokens`
    # stays the billable figure the scheduler has always accounted with, so
    # widening this record changes nothing upstream.
    prompt_tokens:     int = 0
    completion_tokens: int = 0
    total_tokens:      int = 0
    # What the model thought before answering, when it returns that separately.
    # Carried, never rendered by the translation path: the Playground is the one
    # place it is shown, because there the question *is* whether the model is
    # thinking (see tui/screens/playground.py).
    reasoning:         str = ""
    reasoning_tokens:  int = 0


@dataclass
class StreamStats:
    """What a streamed reply turned out to cost, for the chat playground."""
    content:   str = ""
    reasoning: str = ""
    prompt_tokens:     int = 0
    completion_tokens: int = 0
    total_tokens:      int = 0
    reasoning_tokens:  int = 0        # from completion_tokens_details, if sent
    tokens_estimated:  bool = False   # server sent no usage; figures are guesses
    ttft_ms:    float = 0.0           # time to the first visible character
    think_ms:   float = 0.0           # time to the first reasoning fragment
    latency_ms: float = 0.0
    chunks:     int = 0
    finish_reason: str = ""
    stopped:    bool = False          # cancelled by the caller, not by the server

    @property
    def thought(self) -> bool:
        """Did the model actually reason? The only reliable on/off evidence."""
        return bool(self.reasoning.strip()) or self.reasoning_tokens > 0

    @property
    def tokens_per_second(self) -> float:
        """Generation rate — the wait for the first token is not generation."""
        if not self.completion_tokens:
            return 0.0
        generating = max(0.0, self.latency_ms - self.ttft_ms) / 1000.0
        if generating <= 0.001:
            # A short reply from a local model arrives in a single buffer, so
            # the generation window is too small to measure. Rate it over the
            # whole call rather than reporting a confident zero.
            generating = self.latency_ms / 1000.0
        return self.completion_tokens / generating if generating > 0 else 0.0


@dataclass
class PingResult:
    is_ok:       bool
    latency_ms:  float = 0.0
    error:       str = ""      # raw provider error text, shown verbatim in the UI
    status_code: int = 0
    model_id:    str = ""


@dataclass
class ListModelsResult:
    is_ok:       bool
    models:      list[str] = field(default_factory=list)
    error:       str = ""
    status_code: int = 0

    @property
    def is_missing_endpoint(self) -> bool:
        """
        True when /models looks unimplemented rather than misconfigured —
        the case where the UI should offer manual model entry instead of
        rejecting the provider (03_PROVIDER_SYSTEM.md §3).
        """
        return self.status_code in (404, 405, 501) or (self.is_ok and not self.models)


# ── Force-abort registry ─────────────────────────────────────────────────────
#
# A run's owner (a screen or the engine) holds one threading.Event; every
# client built with that event registers itself here so the owner can sever
# every in-flight request without knowing which executors/workers/clients
# exist. Weak, so a finished run's clients vanish from the registry without
# anyone having to unregister them.

_ABORT_REGISTRY: "weakref.WeakKeyDictionary[object, weakref.WeakSet]" = \
    weakref.WeakKeyDictionary()
_ABORT_REGISTRY_LOCK = threading.Lock()


def _register_abortable(abort_event, client: "ProviderClient") -> None:
    with _ABORT_REGISTRY_LOCK:
        clients = _ABORT_REGISTRY.get(abort_event)
        if clients is None:
            clients = weakref.WeakSet()
            _ABORT_REGISTRY[abort_event] = clients
        clients.add(client)


def abort_all_for(abort_event) -> int:
    """
    Sever every in-flight request of every client tied to this event.

    Returns how many clients were told to abort (not how many had a request
    in flight — a client may be idle). Safe to call with no registry entry
    and safe to call twice; the second call is a no-op because the sockets
    are already closed.
    """
    closed = 0
    with _ABORT_REGISTRY_LOCK:
        clients = list(_ABORT_REGISTRY.get(abort_event) or ())
    for client in clients:
        try:
            client.abort_active_request()
            closed += 1
        except Exception:
            pass
    return closed


def parse_body(text: str) -> dict:
    """
    Tolerant JSON body parser.

    Some real "OpenAI-compatible" servers answer a *non-streaming* request
    with `Content-Type: text/event-stream` and append `data: [DONE]` after
    the JSON object, or wrap the object in SSE `data:` framing. A plain
    json.loads() then dies on "Extra data".

    This is not streaming support: nothing is rendered incrementally. It only
    recovers the one complete response object from a body that carries extra
    framing around it. Raises ValueError if there is no usable JSON at all.
    """
    body = (text or "").strip()
    if not body:
        raise ValueError("empty response body")

    decoder = json.JSONDecoder()

    # Plain object, possibly with trailing junk such as `data: [DONE]`.
    if body[0] in "{[":
        try:
            data, _end = decoder.raw_decode(body)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass

    # SSE framing: one or more `data: {...}` lines.
    chunks: list[dict] = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            data, _end = decoder.raw_decode(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            chunks.append(data)

    if not chunks:
        raise ValueError("no JSON object found in response body")

    # A complete (non-delta) object wins — that is the normal case here.
    for chunk in chunks:
        choices = chunk.get("choices") or []
        if choices and isinstance(choices[0], dict) and "message" in choices[0]:
            return chunk

    # Otherwise the server really did stream deltas at us despite being asked
    # not to: stitch them into one final message so the caller still gets a
    # complete response to parse.
    merged = dict(chunks[-1])
    content = "".join(
        (c.get("choices") or [{}])[0].get("delta", {}).get("content") or ""
        for c in chunks
    )
    merged["choices"] = [{"index": 0, "message": {"role": "assistant", "content": content}}]
    for chunk in reversed(chunks):
        if chunk.get("usage"):
            merged["usage"] = chunk["usage"]
            break
    return merged


def normalize_base_url(base_url: str) -> str:
    """Strip trailing slashes and a trailing endpoint path if the user pasted one."""
    url = (base_url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/completions", "/models"):
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    return url


def _reasoning_payload(
    reasoning: ResolvedReasoning | ReasoningConfig | dict | None,
) -> dict:
    """
    The reasoning fragment for a request body.

    Callers hand over whatever they have: an already-resolved decision (the
    normal path, from retry.py), a bare config (resolve it with the model's own
    default), or nothing. Never raises — an unusable spec means no parameter,
    which is the safe answer (FR-407).
    """
    if reasoning is None:
        return {}
    try:
        if isinstance(reasoning, ResolvedReasoning):
            return reasoning.payload()
        return resolve(reasoning).payload()
    except Exception:
        return {}


class ProviderClient:
    """
    One instance per (base_url, api_key) pair. Cheap to construct.

    api_key may be None/"" — no Authorization header is sent, which is what
    local servers (Ollama, LM Studio, vLLM) expect.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        timeout: float = config.DEFAULT_HTTP_TIMEOUT,
        abort_event=None,
    ) -> None:
        self.base_url = normalize_base_url(base_url)
        self.api_key = (api_key or "").strip() or None
        self.timeout = timeout
        # Set by the run's owner (screen/engine) so an HTTP request in flight
        # can be severed from another thread: `requests` cannot be interrupted
        # mid-read, but closing the underlying socket makes the blocked read
        # raise immediately. None (the default) keeps the client fully
        # synchronous, which is what every existing caller expects.
        self.abort_event = abort_event
        # Streaming replies carry no token counts unless asked; some servers
        # reject the field that asks. Set false after one such rejection so
        # the rest of the session stops paying for the round trip.
        self._ask_for_usage = True
        # The response currently being read, if any, and the lock that guards
        # it — the abort path closes it from another thread.
        self._active_response = None
        self._response_lock = threading.Lock()
        if abort_event is not None:
            _register_abortable(abort_event, self)

    # ── URLs / headers ────────────────────────────────────────────────────────

    @property
    def models_url(self) -> str:
        return f"{self.base_url}/models"

    @property
    def generate_url(self) -> str:
        return f"{self.base_url}/chat/completions"

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    # ── Abort ──────────────────────────────────────────────────────────────────

    def abort_active_request(self) -> None:
        """
        Sever whatever HTTP request this client is currently reading.

        Called from the UI thread on a force-cancel. `requests`/urllib3 offer
        no cooperative cancellation for a blocked read, but closing the
        underlying socket makes the blocked recv() raise immediately in the
        worker thread, which surfaces there as a ProviderError. Idempotent and
        safe when nothing is in flight.
        """
        with self._response_lock:
            response = self._active_response
        if response is None:
            return
        try:
            response.close()
        except Exception:
            pass        # already closed, or the socket died on its own

    def _abortable_post(self, json_body: dict, stream: bool = False):
        """
        requests.post with the response registered for aborting.

        The response is registered the moment it exists and unregistered in a
        finally, so the abort window covers the whole read — body and stream
        alike. A closed socket turns into requests.exceptions.ConnectionError /
        ChunkedEncodingError on the reading thread, which the callers already
        map to a ProviderError.
        """
        resp = requests.post(
            self.generate_url,
            headers=self._headers(),
            json=json_body,
            timeout=(10, self.timeout),
            stream=stream,
        )
        with self._response_lock:
            self._active_response = resp
        return resp

    def _release_response(self) -> None:
        with self._response_lock:
            self._active_response = None

    def _aborted(self) -> bool:
        return self.abort_event is not None and self.abort_event.is_set()

    # ── list_models ───────────────────────────────────────────────────────────

    def list_models(self) -> ListModelsResult:
        """
        GET {base_url}/models. Never raises — the caller needs to distinguish
        "endpoint missing" from "credentials wrong" to decide whether to offer
        the manual-model fallback.
        """
        try:
            resp = requests.get(self.models_url, headers=self._headers(), timeout=15)
        except requests.exceptions.RequestException as exc:
            return ListModelsResult(is_ok=False, error=f"{type(exc).__name__}: {exc}")

        if resp.status_code != 200:
            return ListModelsResult(
                is_ok=False,
                error=f"HTTP {resp.status_code}: {resp.text[:500]}".strip(),
                status_code=resp.status_code,
            )

        try:
            body = parse_body(resp.text)
        except ValueError:
            return ListModelsResult(
                is_ok=False,
                error=f"Non-JSON response from {self.models_url}: {resp.text[:300]}",
                status_code=resp.status_code,
            )

        # Standard OpenAI shape: {"data": [{"id": ...}, ...]}.
        # Tolerate a bare list, which some local servers return.
        entries = body.get("data", []) if isinstance(body, dict) else body
        models: list[str] = []
        if isinstance(entries, list):
            for item in entries:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    models.append(item["id"])
                elif isinstance(item, str):
                    models.append(item)

        return ListModelsResult(is_ok=True, models=sorted(set(models)), status_code=200)

    # ── generate ──────────────────────────────────────────────────────────────

    def _chat_body(
        self,
        model: str,
        messages: list[dict],
        temperature: float,
        reasoning: ResolvedReasoning | ReasoningConfig | dict | None,
        max_completion_tokens: int,
        response_format: dict | None,
        stream: bool,
    ) -> dict:
        """
        The request body every call shape shares. `stream` is always stated
        rather than left to the server: some default to SSE framing unless
        told otherwise, and the translation pipeline cannot read that.

        `reasoning` is a *specification*, not a level: providers/reasoning.py
        turns the model's own metadata into the fragment this body needs —
        a nested `chat_template_kwargs.enable_thinking` for one model, a flat
        `reasoning_effort` for another, and nothing at all for a model whose
        reasoning cannot be controlled. Guessing a parameter here would get
        the whole request rejected (ADR-003).
        """
        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "stream": stream,
        }
        merge_payload(body, _reasoning_payload(reasoning))
        if max_completion_tokens > 0:
            body["max_completion_tokens"] = max_completion_tokens
        if response_format:
            body["response_format"] = response_format
        return body

    @staticmethod
    def _usage_from(data: dict) -> tuple[int, int, int, int]:
        """(billable, prompt, completion, total) — cached prompt tokens are
        not billable, so they come off the figure the pools account with."""
        usage = data.get("usage") or {}
        total = int(usage.get("total_tokens") or 0)
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        cached = 0
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            cached = int(details.get("cached_tokens") or 0)
        billable = max(0, total - cached)
        return billable or total, prompt, completion, total

    @staticmethod
    def _reasoning_tokens_from(data: dict) -> int:
        """
        What the model spent thinking, when the server itemises it.

        OpenAI-style servers report it under
        `usage.completion_tokens_details.reasoning_tokens`; some spell the block
        or the key differently. It is the one figure that proves reasoning ran
        even when the content itself is withheld, so it is worth reading
        defensively rather than not at all.
        """
        usage = data.get("usage") or {}
        for block_key in ("completion_tokens_details", "output_tokens_details"):
            details = usage.get(block_key)
            if isinstance(details, dict):
                for key in ("reasoning_tokens", "thinking_tokens", "reasoning"):
                    value = details.get(key)
                    if isinstance(value, (int, float)):
                        return int(value)
        for key in ("reasoning_tokens", "thinking_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    def generate(
        self,
        model: str,
        prompt: str,
        temperature: float = config.DEFAULT_TEMPERATURE,
        reasoning: ResolvedReasoning | ReasoningConfig | dict | None = None,
        max_completion_tokens: int = 0,
        response_format: dict | None = None,
    ) -> GenerateResult:
        """
        Single OpenAI-compatible chat completion from one user turn — what the
        whole translation pipeline calls. Raises ProviderError on any failure,
        always carrying the provider's raw response text.
        """
        return self.generate_chat(
            model,
            [{"role": "user", "content": prompt}],
            temperature=temperature,
            reasoning=reasoning,
            max_completion_tokens=max_completion_tokens,
            response_format=response_format,
        )

    def generate_chat(
        self,
        model: str,
        messages: list[dict],
        temperature: float = config.DEFAULT_TEMPERATURE,
        reasoning: ResolvedReasoning | ReasoningConfig | dict | None = None,
        max_completion_tokens: int = 0,
        response_format: dict | None = None,
    ) -> GenerateResult:
        """
        The same call with a full message list, so a caller can send a system
        turn or a conversation. Raises ProviderError.

        `response_format` is an explicit per-model opt-in (structured output);
        it is never sent unless the caller asks for it, because plenty of
        OpenAI-compatible servers reject the field.

        The HTTP request is sent with `stream=True` at the transport level
        only — the BODY still tells the server `"stream": false`, so the
        provider computes the whole answer and sends it in one burst, exactly
        as it did before: same latency profile, same usage blocks, no
        per-token SSE. The transport-level stream exists for control, not
        delivery: `requests.post` with `stream=False` blocks inside the POST
        until the whole body has been read, so the response object — the
        thing the abort registry and the deadline timer close — only exists
        after the read is over. With `stream=True` the response exists as
        soon as the headers arrive, is registered for abort, and the body is
        then drained in one pass; `parse_body` already understands SSE-framed
        chunks, so a server that answers a non-streaming request with SSE
        anyway is still read correctly.

        **Total deadline.** `timeout=(connect, read)` is an *inactivity*
        timeout per socket read — a server that trickles one byte every few
        seconds resets it forever, and a "120s timeout" run measured 227s and
        beyond. So the user's `request_timeout` is now enforced as a
        wall-clock cap on the WHOLE request: a deadline timer is armed when
        the request is sent and closes the response — the same severing move
        the force-cancel makes — the moment the cap is hit, whether or not
        bytes are still arriving. The resulting read error is reported as an
        ordinary timeout (`is_timeout=True`), so the retry loop rotates the
        slot and re-issues exactly as it always did for a silent stall. The
        timer is cancelled the moment the body has been read.
        """
        body = self._chat_body(
            model, messages, temperature, reasoning,
            max_completion_tokens, response_format, stream=False,
        )

        started = time.time()
        deadline_hit = threading.Event()
        try:
            resp = self._abortable_post(body, stream=True)
            timer = threading.Timer(
                max(1.0, self.timeout), self._sever_past_deadline,
                args=(resp, deadline_hit),
            )
            timer.daemon = True
            timer.start()
            try:
                latency_ms = (time.time() - started) * 1000.0
                headers, text = dict(resp.headers), resp.text
            finally:
                timer.cancel()
                self._release_response()
                resp.close()
        except requests.exceptions.Timeout:
            # A read timeout is how the user's `request_timeout` setting is
            # enforced: the retry loop counts this as a failed attempt (server
            # class, like a 504) and the next attempt may land elsewhere.
            # `is_timeout` tells the pool to rotate away from this slot even
            # under round-robin — a slot that hangs must not hold its wave.
            raise ProviderError(
                f"Connection timed out after {self.timeout:.0f}s",
                status_code=504,
                retry_delay=config.RETRY_DELAY_ERR,
                kind=ProviderError.KIND_SERVER,
                is_timeout=True,
            )
        except requests.exceptions.ConnectionError as exc:
            if self._aborted():
                # The force-cancel path closed the socket under this read.
                # Report it as a cancellation, not a connection failure —
                # the retry loop checks the stop flag and gives up quietly
                # instead of rotating and re-issuing.
                raise ProviderError(
                    "Request aborted (force cancel)",
                    status_code=0,
                    retry_delay=0,
                    kind=ProviderError.KIND_UNKNOWN,
                )
            if deadline_hit.is_set():
                # The deadline timer closed the socket under this read: the
                # request outlived the user's timeout while bytes kept
                # trickling in. Same handling as a silent read timeout —
                # this is the enforcement, not a provider outage.
                raise ProviderError(
                    f"Request exceeded the {self.timeout:.0f}s total deadline "
                    f"(still receiving data)",
                    status_code=504,
                    retry_delay=config.RETRY_DELAY_ERR,
                    kind=ProviderError.KIND_SERVER,
                    is_timeout=True,
                )
            raise ProviderError(
                f"HTTP request failed: {type(exc).__name__}: {exc}",
                status_code=0,
                retry_delay=config.RETRY_DELAY_ERR,
            )
        except requests.exceptions.RequestException as exc:
            if deadline_hit.is_set() and not self._aborted():
                # The deadline severs mid-read as a chunked-encoding/connection
                # error rather than a Timeout, depending on where the read was.
                raise ProviderError(
                    f"Request exceeded the {self.timeout:.0f}s total deadline "
                    f"(still receiving data)",
                    status_code=504,
                    retry_delay=config.RETRY_DELAY_ERR,
                    kind=ProviderError.KIND_SERVER,
                    is_timeout=True,
                )
            raise ProviderError(
                f"HTTP request failed: {type(exc).__name__}: {exc}",
                status_code=0,
                retry_delay=config.RETRY_DELAY_ERR,
            )

        self._raise_for_status(resp.status_code, text, headers)

        try:
            data = parse_body(text)
        except ValueError as exc:
            raise ProviderError(
                f"Unreadable response body ({exc}): {text[:200]}",
                status_code=resp.status_code,
                response_text=text,
                headers=headers,
            )

        try:
            message = data["choices"][0]["message"]
            content = message.get("content")
            reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
            if content is None:
                # Some servers return reasoning-only messages with null content.
                content = reasoning
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"Malformed response (missing choices[0].message.content): {exc}",
                status_code=resp.status_code,
                response_text=text,
                headers=headers,
            )

        billable, prompt_tokens, completion_tokens, total_tokens = self._usage_from(data)

        return GenerateResult(
            content=content,
            tokens=billable,
            headers=headers,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            reasoning=reasoning if reasoning != content else "",
            reasoning_tokens=self._reasoning_tokens_from(data),
        )

    def _sever_past_deadline(self, resp, deadline_hit: threading.Event) -> None:
        """
        Close the response because the request outlived the user's timeout.

        Runs on the deadline timer's thread. Closing from another thread makes
        the blocked `resp.text` read raise immediately — the same mechanism
        `abort_active_request` uses for a force-cancel — and `deadline_hit`
        tells the except clauses which of the three causes fired (deadline,
        force-cancel, ordinary network failure).
        """
        deadline_hit.set()
        try:
            resp.close()
        except Exception:
            pass        # already closed, or the socket died on its own

    # ── stream_chat ───────────────────────────────────────────────────────────

    def stream_chat(
        self,
        model: str,
        messages: list[dict],
        on_delta: Callable[[str], None],
        temperature: float = config.DEFAULT_TEMPERATURE,
        reasoning: ResolvedReasoning | ReasoningConfig | dict | None = None,
        max_completion_tokens: int = 0,
        stop_event=None,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> StreamStats:
        """
        Stream one reply, handing each fragment to `on_delta` as it lands, and
        return what it cost. Raises ProviderError, same as generate().

        Playground only. The translation pipeline must never call this: a
        batch is either complete or it is not, and half a batch cannot be
        written to a spreadsheet.

        `on_reasoning` receives the thinking fragments, separately from the
        answer, for the one screen that shows them. It is optional and off by
        default — everywhere except the chat playground, reasoning content is
        collected into `StreamStats.reasoning` and never surfaced.

        `stop_event` is checked between fragments, so Stop takes effect at the
        next one rather than at the end. The request itself is not
        interruptible mid-read — that is a `requests` limitation.
        """
        attempts = 2 if self._ask_for_usage else 1
        for attempt in range(attempts):
            body = self._chat_body(
                model, messages, temperature, reasoning,
                max_completion_tokens, None, stream=True,
            )
            if self._ask_for_usage:
                # Without this most servers stream no usage block at all and
                # every token figure below would be a guess.
                body["stream_options"] = {"include_usage": True}
            try:
                return self._consume_stream(body, on_delta, stop_event, on_reasoning)
            except ProviderError as exc:
                rejected = (
                    self._ask_for_usage
                    and attempt == 0
                    and 400 <= exc.status_code < 500
                    and "stream_options" in (exc.response_text or "")
                )
                if not rejected:
                    raise
                self._ask_for_usage = False      # and don't ask again this session
        raise ProviderError("stream_chat exhausted its attempts", status_code=0)

    def _consume_stream(
        self, body: dict, on_delta, stop_event, on_reasoning=None
    ) -> StreamStats:
        started = time.time()
        try:
            resp = self._abortable_post(body, stream=True)
        except requests.exceptions.Timeout:
            raise ProviderError(
                f"Connection timed out after {self.timeout:.0f}s",
                status_code=504,
                retry_delay=config.RETRY_DELAY_ERR,
                kind=ProviderError.KIND_SERVER,
                is_timeout=True,
            )
        except requests.exceptions.RequestException as exc:
            raise ProviderError(
                f"HTTP request failed: {type(exc).__name__}: {exc}",
                status_code=0,
                retry_delay=config.RETRY_DELAY_ERR,
            )

        stats = StreamStats()
        try:
            if resp.status_code != 200:
                # Reading .text drains the stream, which is what we want here.
                self._raise_for_status(resp.status_code, resp.text, dict(resp.headers))

            pieces: list[str] = []
            thinking: list[str] = []
            usage: dict = {}
            aborted_mid_stream = False
            for raw in resp.iter_lines(decode_unicode=True):
                if stop_event is not None and stop_event.is_set():
                    stats.stopped = True
                    break
                if self._aborted():
                    aborted_mid_stream = True
                    break
                if not raw:
                    continue
                line = raw.strip()
                if line.startswith("data:"):
                    line = line[5:].strip()
                if not line:
                    continue
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue          # keep-alive comment or a partial frame
                if not isinstance(chunk, dict):
                    continue

                stats.chunks += 1
                if chunk.get("usage"):
                    usage = chunk["usage"]
                choices = chunk.get("choices") or []
                if not choices or not isinstance(choices[0], dict):
                    continue
                choice = choices[0]
                if choice.get("finish_reason"):
                    stats.finish_reason = str(choice["finish_reason"])
                delta = choice.get("delta") or {}
                think = delta.get("reasoning_content") or delta.get("reasoning") or ""
                if think:
                    if not stats.think_ms:
                        stats.think_ms = (time.time() - started) * 1000.0
                    thinking.append(think)
                    if on_reasoning is not None:
                        on_reasoning(think)
                piece = delta.get("content") or ""
                if not piece:
                    continue
                if not stats.ttft_ms:
                    stats.ttft_ms = (time.time() - started) * 1000.0
                pieces.append(piece)
                on_delta(piece)
        finally:
            self._release_response()
            resp.close()

        if aborted_mid_stream:
            raise ProviderError(
                "Request aborted (force cancel)",
                status_code=0,
                retry_delay=0,
                kind=ProviderError.KIND_UNKNOWN,
            )

        stats.latency_ms = (time.time() - started) * 1000.0
        stats.content = "".join(pieces)
        stats.reasoning = "".join(thinking)

        if usage:
            _billable, stats.prompt_tokens, stats.completion_tokens, stats.total_tokens = (
                self._usage_from({"usage": usage})
            )
            stats.reasoning_tokens = self._reasoning_tokens_from({"usage": usage})
        if not stats.completion_tokens and stats.content:
            # No usage block: roughly four characters to a token. Flagged so
            # the UI can mark the number as the guess it is.
            stats.tokens_estimated = True
            stats.completion_tokens = max(1, round(len(stats.content) / 4))
            stats.total_tokens = stats.completion_tokens
        return stats

    # ── ping_model ────────────────────────────────────────────────────────────

    def ping_model(self, model_id: str, timeout: float = config.PING_TIMEOUT) -> PingResult:
        """
        Minimal live request against one model. Used both for connection
        validation and for the Manage Provider batch test — one mechanism,
        not two (03_PROVIDER_SYSTEM.md §3).

        Never raises for expected failures; returns is_ok=False plus the raw
        provider error text.
        """
        started = time.time()
        try:
            resp = requests.post(
                self.generate_url,
                headers=self._headers(),
                json={
                    "model": model_id,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                    "stream": False,
                },
                timeout=(10, timeout),
            )
        except requests.exceptions.Timeout:
            return PingResult(
                is_ok=False,
                latency_ms=(time.time() - started) * 1000.0,
                error=f"Connection timed out after {timeout:.0f}s",
                status_code=504,
                model_id=model_id,
            )
        except requests.exceptions.RequestException as exc:
            return PingResult(
                is_ok=False,
                latency_ms=(time.time() - started) * 1000.0,
                error=f"{type(exc).__name__}: {exc}",
                status_code=0,
                model_id=model_id,
            )

        latency_ms = (time.time() - started) * 1000.0
        if resp.status_code == 200:
            return PingResult(
                is_ok=True, latency_ms=latency_ms, status_code=200, model_id=model_id
            )

        return PingResult(
            is_ok=False,
            latency_ms=latency_ms,
            error=f"HTTP {resp.status_code}: {resp.text[:500]}".strip(),
            status_code=resp.status_code,
            model_id=model_id,
        )

    # ── shared error mapping ──────────────────────────────────────────────────

    @staticmethod
    def _raise_for_status(status_code: int, text: str, headers: dict) -> None:
        if status_code == 200:
            return

        if status_code == 429:
            delay = parse_retry_delay(text, headers, fallback=config.RETRY_DELAY_FALLBACK_429)
            is_daily, _is_temporary = classify_rate_limit(text, headers)
            if not is_daily and remaining_tokens(headers) == 0:
                delay = max(delay, config.RETRY_DELAY_FALLBACK_429)
            # A 429 is a *temporary* rate limit unless the provider positively
            # says the daily/quota wall was hit. An anonymous 429 must never be
            # reported as quota exhaustion — that is the false positive that
            # retired a healthy connection for the rest of the job.
            raise ProviderError(
                f"Rate limit (429) — retry in {delay}s",
                status_code=429,
                is_rate_limit=True,
                is_tpd=is_daily,
                retry_delay=delay,
                response_text=text,
                headers=headers,
                kind=(ProviderError.KIND_QUOTA if is_daily
                      else ProviderError.KIND_RATE_LIMIT),
            )

        if status_code in (500, 502, 503, 504):
            raise ProviderError(
                f"Service unavailable ({status_code})",
                status_code=status_code,
                retry_delay=config.RETRY_DELAY_503,
                response_text=text,
                headers=headers,
                kind=ProviderError.KIND_SERVER,
            )

        if status_code in (401, 403):
            # Some providers report a spent quota as 403 with an
            # `insufficient_quota` body — that is a real quota wall. A bare
            # 401/403 with no such signal is authentication, not a rate limit.
            quota = is_quota_error(text)
            raise ProviderError(
                f"HTTP {status_code}: {text[:300]}".strip(),
                status_code=status_code,
                retry_delay=config.RETRY_DELAY_ERR,
                response_text=text,
                headers=headers,
                kind=(ProviderError.KIND_QUOTA if quota
                      else ProviderError.KIND_AUTH),
            )

        if status_code in (400, 404, 405, 415, 422):
            # A bad request or an unknown model — retrying or switching
            # connection will not help, and it is not a quota problem.
            raise ProviderError(
                f"HTTP {status_code}: {text[:300]}".strip(),
                status_code=status_code,
                retry_delay=config.RETRY_DELAY_ERR,
                response_text=text,
                headers=headers,
                kind=ProviderError.KIND_INVALID,
            )

        # Anything else: keep the raw text for diagnostics and stay neutral —
        # no assumption of quota exhaustion.
        raise ProviderError(
            f"HTTP {status_code}: {text[:300]}".strip(),
            status_code=status_code,
            retry_delay=config.RETRY_DELAY_ERR,
            response_text=text,
            headers=headers,
            kind=(ProviderError.KIND_QUOTA if is_quota_error(text)
                  else ProviderError.KIND_UNKNOWN),
        )
