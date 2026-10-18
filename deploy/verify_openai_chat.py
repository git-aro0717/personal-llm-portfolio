"""One approved local FastAPI -> Harness -> Responses probe. Never retries.

Run from the PowerShell that contains OPENAI_API_KEY. No AWS or Files calls.
"""
import argparse
import json
import logging
import os
from pathlib import Path
import sys
import time

# Make direct script execution independent of the shell's PYTHONPATH.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MODEL = "gpt-5.6-luna"
ENDPOINT = "https://api.openai.com/v1/responses"
PROMPT = "日本語で短く挨拶してください。"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Send one paid Responses request")
    args = parser.parse_args()
    if not args.execute:
        print(json.dumps({"mode":"preview", "endpoint":ENDPOINT, "model":MODEL,
                          "max_requests":1, "prompt":PROMPT}, ensure_ascii=True))
        return 0
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key.strip() or any(c.isspace() for c in key):
        print('{"status":"stopped","category":"local_api_key_missing_or_invalid","requests":0}')
        return 2
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):
        print('{"status":"stopped","category":"not_a_local_environment","requests":0}')
        return 2
    del key
    # Child-process-only settings; never modify the parent PowerShell or AWS.
    os.environ["HARNESS_BACKEND"] = "openai"
    os.environ["OPENAI_MODEL_ID"] = MODEL
    os.environ["OPENAI_MAX_OUTPUT_TOKENS"] = "512"
    os.environ["OPENAI_TIMEOUT_SECONDS"] = "60"
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    import httpx
    from fastapi.testclient import TestClient
    from app.main import app
    from app.harness import openai_api

    marker = ROOT / ".aws-sam" / "v064-e2e" / "normal-chat-1" / "attempt.json"
    marker.parent.mkdir(parents=True, exist_ok=True)
    real_client = httpx.Client
    metadata = {"requests":0}

    class OneRequestTransport(httpx.BaseTransport):
        def __init__(self):
            self.inner = httpx.HTTPTransport(retries=0)

        def handle_request(self, request):
            if metadata["requests"] or request.method != "POST" or str(request.url) != ENDPOINT:
                raise RuntimeError("Unexpected external operation blocked")
            payload = json.loads(request.content)
            if payload.get("model") != MODEL or payload.get("input") != PROMPT or "tools" in payload or payload.get("store") is not False:
                raise RuntimeError("Unexpected inference payload blocked")
            metadata["requests"] = 1
            request.headers['accept-encoding'] = 'identity'
            upstream = self.inner.handle_request(request)
            try:
                metadata["upstream_http_status"] = upstream.status_code
                data = bytearray()
                chunks = [upstream.content] if upstream.is_stream_consumed else upstream.iter_raw()
                for chunk in chunks:
                    if len(data)+len(chunk)>1024*1024:
                        raise httpx.ReadError("Response size limit exceeded")
                    data.extend(chunk)
                try:
                    result = json.loads(data)
                    if isinstance(result, dict):
                        returned_model = result.get("model")
                        if isinstance(returned_model,str) and len(returned_model)<=128:
                            metadata["returned_model"] = returned_model
                        usage = result.get("usage")
                        if isinstance(usage, dict):
                            metadata["usage"] = {k:v for k,v in usage.items() if k in ("input_tokens","output_tokens","total_tokens") and type(v) is int}
                            details = usage.get("output_tokens_details")
                            if isinstance(details,dict) and type(details.get("reasoning_tokens")) is int:
                                metadata["reasoning_tokens"] = details["reasoning_tokens"]
                        error = result.get("error")
                        known = {"invalid_api_key","insufficient_quota","rate_limit_exceeded","model_not_found","unsupported_parameter"}
                        if isinstance(error,dict) and error.get("code") in known:
                            metadata["upstream_error_code"] = error["code"]
                except (ValueError, TypeError):
                    pass
                return httpx.Response(upstream.status_code, content=bytes(data))
            finally:
                upstream.close()

        def close(self):
            self.inner.close()

    def provider_client(**kwargs):
        return real_client(transport=OneRequestTransport(), **kwargs)

    # Exclusive marker prevents an accidental rerun from creating a second charge.
    try:
        with marker.open("x", encoding="utf-8") as output:
            json.dump({"status":"attempt_started", "requested_model":MODEL}, output)
    except FileExistsError:
        print('{"status":"stopped","category":"attempt_already_recorded","requests":0}')
        return 2
    started = time.monotonic()
    result = {"requested_model":MODEL}
    class Categories(logging.Handler):
        def emit(self, record):
            message = record.getMessage()
            prefix = "OpenAI inference failed: "
            if message.startswith(prefix):
                category = message[len(prefix):]
                if category.isascii() and category.replace('_','').isalpha():
                    result['category'] = category
    safe_log = Categories()
    openai_api.logger.addHandler(safe_log)
    try:
        # In-process HTTP /chat still exercises validation, startup, Harness and adapter.
        with TestClient(app, raise_server_exceptions=False) as client:
            openai_api.httpx.Client = provider_client
            response = client.post("/chat", json={"message":PROMPT})
            result["backend_http_status"] = response.status_code
            if response.status_code == 200:
                result["response"] = response.json().get("response")
            result["status"] = "success" if response.status_code == 200 else "failed"
    except Exception as exc:
        # Never print exception text, request bodies, headers or provider error messages.
        result.update(status="failed", category="local_probe_error", error_type=type(exc).__name__)
    finally:
        openai_api.httpx.Client = real_client
        openai_api.logger.removeHandler(safe_log)
    result.update(metadata)
    result["elapsed_seconds"] = round(time.monotonic()-started, 3)
    marker.write_text(json.dumps(result, ensure_ascii=True, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
