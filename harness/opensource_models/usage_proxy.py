#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def safe_json_loads(raw):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def token_detail(usage, key):
    if not isinstance(usage, dict):
        return None
    for detail_key in (
        "completion_tokens_details",
        "output_tokens_details",
        "prompt_tokens_details",
        "input_tokens_details",
    ):
        detail = usage.get(detail_key)
        if isinstance(detail, dict) and detail.get(key) is not None:
            return detail.get(key)
    return None


def parse_usage_from_response(raw, is_stream):
    if not raw:
        return {}, None, None, None
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return {}, None, None, None

    usage = {}
    response_model = None
    response_id = None
    finish_reason = None

    if is_stream:
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            if obj.get("model"):
                response_model = obj.get("model")
            if obj.get("id"):
                response_id = obj.get("id")
            if isinstance(obj.get("usage"), dict):
                usage = obj["usage"]
            for choice in obj.get("choices", []) or []:
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason"):
                    finish_reason = choice.get("finish_reason")
    else:
        try:
            obj = json.loads(text)
        except Exception:
            return {}, None, None, None
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
        response_model = obj.get("model")
        response_id = obj.get("id")
        for choice in obj.get("choices", []) or []:
            if not isinstance(choice, dict):
                continue
            if choice.get("finish_reason"):
                finish_reason = choice.get("finish_reason")

    return usage, response_model, response_id, finish_reason


class SSEToolCallFixer:
    """Per-request stateful fixer for null tool call IDs and function names in SSE streams.

    vLLM's openai tool-call parser emits id=null and function.name=null on every
    streaming chunk for some models (e.g. gpt-oss harmony format).  The
    @ai-sdk/openai-compatible SDK validates both fields as strings immediately,
    so we must patch them before forwarding.

    Stateful because we must reuse the same generated call_* ID across all chunks
    that share the same tool-call index — generating a new ID per chunk would make
    the SDK treat each chunk as a fresh independent tool call.
    """

    def __init__(self):
        self._ids: dict[int, str] = {}  # tool-call index -> assigned call ID

    def fix_line(self, line: bytes) -> bytes:
        stripped = line.rstrip(b"\r\n")
        if not stripped.startswith(b"data:"):
            return line
        payload = stripped[5:].strip()
        if not payload or payload == b"[DONE]":
            return line
        try:
            obj = json.loads(payload)
        except Exception:
            return line
        modified = False
        for choice in obj.get("choices", []) or []:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if not isinstance(delta, dict):
                continue
            for tc in delta.get("tool_calls", []) or []:
                if not isinstance(tc, dict):
                    continue
                index = tc.get("index") or 0
                # Fix null/missing tool call ID — reuse same ID for same index
                if isinstance(tc.get("id"), str):
                    if index not in self._ids:
                        self._ids[index] = tc["id"]
                else:
                    if index not in self._ids:
                        self._ids[index] = "call_" + uuid.uuid4().hex[:8]
                    tc["id"] = self._ids[index]
                    modified = True
                # Fix null function.name — remove the key so SDK skips validation
                fn = tc.get("function")
                if isinstance(fn, dict) and "name" in fn and fn["name"] is None:
                    del fn["name"]
                    modified = True
        if not modified:
            return line
        ending = line[len(stripped):]
        return b"data: " + json.dumps(obj, ensure_ascii=False).encode("utf-8") + ending


def extract_usage_route(path):
    match = re.match(r"^/__usage/([^/]+)/([^/]+)(/v1(?:/.*)?|$)", path)
    if not match:
        return None, None, path
    job_label = urllib.parse.unquote(match.group(1))
    phase = urllib.parse.unquote(match.group(2))
    upstream_path = match.group(3) or "/v1"
    return job_label, phase, upstream_path


def make_handler(args):
    log_lock = threading.Lock()
    os.makedirs(args.output_dir, exist_ok=True)
    api_calls_path = os.path.join(args.output_dir, "api_calls.jsonl")
    errors_path = os.path.join(args.output_dir, "errors.jsonl")

    class UsageProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *fmt_args):
            if args.quiet:
                return
            super().log_message(fmt, *fmt_args)

        def do_GET(self):
            if self.path == "/healthz":
                body = b'{"ok":true}\n'
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.forward()

        def do_POST(self):
            self.forward()

        def do_OPTIONS(self):
            self.forward()

        def write_jsonl(self, path, record):
            line = json.dumps(record, ensure_ascii=False, sort_keys=True)
            with log_lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")

        def forward(self):
            started_at = now_iso()
            started = time.monotonic()
            parsed = urllib.parse.urlsplit(self.path)
            job_label, phase, upstream_path = extract_usage_route(parsed.path)
            query = f"?{parsed.query}" if parsed.query else ""
            upstream_url = args.upstream_base.rstrip("/") + upstream_path + query

            content_length = int(self.headers.get("content-length") or "0")
            request_body = self.rfile.read(content_length) if content_length else b""
            request_json = safe_json_loads(request_body)
            stream = bool(isinstance(request_json, dict) and request_json.get("stream"))

            outgoing_body = request_body
            if args.include_stream_usage and stream and isinstance(request_json, dict):
                stream_options = request_json.get("stream_options")
                if not isinstance(stream_options, dict):
                    stream_options = {}
                stream_options["include_usage"] = True
                request_json["stream_options"] = stream_options
                outgoing_body = json.dumps(request_json).encode("utf-8")

            headers = {}
            for key, value in self.headers.items():
                lower = key.lower()
                if lower in HOP_BY_HOP_HEADERS or lower in {"host", "content-length", "accept-encoding"}:
                    continue
                headers[key] = value
            headers["content-length"] = str(len(outgoing_body))
            headers["accept-encoding"] = "identity"

            req = urllib.request.Request(
                upstream_url,
                data=outgoing_body if self.command not in {"GET", "HEAD"} else None,
                headers=headers,
                method=self.command,
            )

            status_code = 502
            response_headers = {}
            response_capture = bytearray()
            response_bytes = 0
            error = None

            try:
                with urllib.request.urlopen(req, timeout=args.timeout) as resp:
                    status_code = resp.status
                    response_headers = dict(resp.headers.items())
                    self.send_response(status_code)
                    for key, value in response_headers.items():
                        lower = key.lower()
                        if lower in HOP_BY_HOP_HEADERS or lower in {"content-length", "content-encoding"}:
                            continue
                        self.send_header(key, value)
                    self.send_header("connection", "close")
                    self.end_headers()

                    if stream:
                        fixer = SSEToolCallFixer()
                        sse_buf = b""
                        while True:
                            raw = resp.read(args.chunk_size)
                            if not raw:
                                if sse_buf:
                                    fixed = fixer.fix_line(sse_buf)
                                    response_bytes += len(fixed)
                                    if len(response_capture) < args.max_capture_bytes:
                                        remaining = args.max_capture_bytes - len(response_capture)
                                        response_capture.extend(fixed[:remaining])
                                    self.wfile.write(fixed)
                                break
                            sse_buf += raw
                            while b"\n" in sse_buf:
                                idx = sse_buf.index(b"\n")
                                line = sse_buf[: idx + 1]
                                sse_buf = sse_buf[idx + 1 :]
                                fixed = fixer.fix_line(line)
                                response_bytes += len(fixed)
                                if len(response_capture) < args.max_capture_bytes:
                                    remaining = args.max_capture_bytes - len(response_capture)
                                    response_capture.extend(fixed[:remaining])
                                self.wfile.write(fixed)
                    else:
                        while True:
                            chunk = resp.read(args.chunk_size)
                            if not chunk:
                                break
                            response_bytes += len(chunk)
                            if len(response_capture) < args.max_capture_bytes:
                                remaining = args.max_capture_bytes - len(response_capture)
                                response_capture.extend(chunk[:remaining])
                            self.wfile.write(chunk)
                    self.wfile.flush()
            except urllib.error.HTTPError as exc:
                status_code = exc.code
                response_headers = dict(exc.headers.items())
                body = exc.read()
                response_bytes = len(body)
                response_capture.extend(body[: args.max_capture_bytes])
                self.send_response(status_code)
                for key, value in response_headers.items():
                    lower = key.lower()
                    if lower in HOP_BY_HOP_HEADERS or lower in {"content-length", "content-encoding"}:
                        continue
                    self.send_header(key, value)
                self.send_header("content-length", str(len(body)))
                self.send_header("connection", "close")
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                error = repr(exc)
                body = json.dumps({"error": "usage proxy upstream failure", "detail": error}).encode("utf-8")
                self.send_response(status_code)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.send_header("connection", "close")
                self.end_headers()
                self.wfile.write(body)

            latency_ms = round((time.monotonic() - started) * 1000, 3)
            usage, response_model, response_id, finish_reason = parse_usage_from_response(bytes(response_capture), stream)
            prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
            total_tokens = usage.get("total_tokens")
            reasoning_tokens = token_detail(usage, "reasoning_tokens")
            # Store completion_tokens as output-only (non-reasoning) so that
            # prompt + completion + reasoning = total_tokens in aggregates.
            # vLLM reports completion as all generated tokens (reasoning included),
            # so we subtract to avoid double-counting.
            _raw_completion = usage.get("completion_tokens", usage.get("output_tokens"))
            completion_tokens = (
                _raw_completion - reasoning_tokens
                if _raw_completion is not None and reasoning_tokens
                else _raw_completion
            )

            record = {
                "timestamp": started_at,
                "ended_at": now_iso(),
                "model_label": args.model_label,
                "job_label": job_label or "unknown",
                "phase": phase or "unknown",
                "method": self.command,
                "path": parsed.path,
                "upstream_path": upstream_path,
                "status_code": status_code,
                "ok": error is None and 200 <= status_code < 400,
                "latency_ms": latency_ms,
                "stream": stream,
                "request_model": request_json.get("model") if isinstance(request_json, dict) else None,
                "response_model": response_model,
                "response_id": response_id,
                "finish_reason": finish_reason,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "reasoning_tokens": reasoning_tokens,
                "total_tokens": total_tokens,
                "request_bytes": len(outgoing_body),
                "response_bytes": response_bytes,
                "usage": usage,
                "usage_missing": not bool(usage),
                "error": error,
            }
            self.write_jsonl(api_calls_path, record)
            if error or not record["ok"]:
                self.write_jsonl(errors_path, record)

    return UsageProxyHandler


def main():
    parser = argparse.ArgumentParser(description="OpenAI-compatible usage logging proxy for vLLM.")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--upstream-base", required=True, help="Example: http://127.0.0.1:8000")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--model-label", required=True)
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--chunk-size", type=int, default=65536)
    parser.add_argument("--max-capture-bytes", type=int, default=50 * 1024 * 1024)
    parser.add_argument("--no-include-stream-usage", action="store_false", dest="include_stream_usage")
    parser.set_defaults(include_stream_usage=True)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.listen_host, args.listen_port), make_handler(args))
    print(
        json.dumps(
            {
                "event": "usage_proxy_started",
                "listen": f"{args.listen_host}:{args.listen_port}",
                "upstream_base": args.upstream_base,
                "output_dir": args.output_dir,
                "model_label": args.model_label,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
