from __future__ import annotations

import argparse
import codecs
import json
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib import error, request


BASE_URL = "https://feather.openai.com"
GRAPHQL_URL = f"{BASE_URL}/api/graphql"
DEFAULT_CURL_FILE = Path("outputs/current_feather_request.curl.txt")
CLIENT_GIT_HASH = "befa13b162c"
FETCH_SLIDE_CONVERSATION_WIDGET_QUERY = """
query FetchSlideConversationWidget($taskId: UUID!, $layoutKey: String!) {
  conversationWidget(taskId: $taskId, layoutKey: $layoutKey) {
    conversation {
      turns {
        messages {
          completions {
            id
            formData
            messages {
              content {
                __typename
                ... on ChatContentTypeMultimodalText {
                  parts {
                    __typename
                    ... on ChatStrTextPart {
                      text
                    }
                    ... on ChatImageAssetPointer {
                      assetPointer
                      sizeBytes
                      width
                      height
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
  }
}
""".strip()


def option_value(text: str, flag: str) -> str | None:
    patterns = [
        re.escape(flag) + r"\s+\$?'((?:[^'\\]|\\.)*)'",
        re.escape(flag) + r'\s+"((?:[^"\\]|\\.)*)"',
        re.escape(flag) + r'\s+\^"((?:.|\n)*?)\^"',
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.S)
        if match:
            return match.group(1).replace('^"', '"').replace("^&", "&").replace("^$", "$")
    return None


def header_value(text: str, header_name: str) -> str | None:
    escaped = re.escape(header_name)
    patterns = [
        r"-H\s+'{}:\s*([^']*)'".format(escaped),
        r'-H\s+"{}:\s*([^"]*)"'.format(escaped),
        r'-H\s+\^"{}:\s*((?:.|\n)*?)\^"'.format(escaped),
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(1).replace('^"', '"').replace("^&", "&").replace("^$", "$")
    return None


def cookie_value_from_curl(text: str) -> str | None:
    """Read cookies from the common browser Copy-as-cURL formats."""
    return option_value(text, "-b") or option_value(text, "--cookie") or header_value(text, "cookie")


def build_headers(cookie: str, campaign_id: str, referer: str, task_id: str | None = None, task_kind: str | None = None) -> dict[str, str]:
    headers = {
        "accept": "*/*",
        "content-type": "application/json",
        "origin": BASE_URL,
        "referer": referer,
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/149.0.0.0 Safari/537.36"
        ),
        "x-feather-client-campaign-id": campaign_id,
        "x-feather-client-git-hash": CLIENT_GIT_HASH,
        "cookie": cookie,
    }
    if task_id:
        headers["x-feather-client-task-id"] = task_id
    if task_kind:
        headers["x-feather-client-task-kind"] = task_kind
    return headers


class HttpResponse:
    def __init__(self, status_code: int, headers: dict[str, str], content: bytes):
        self.status_code = status_code
        self.headers = headers
        self.content = content

    def json(self) -> Any:
        return json.loads(self.content.decode("utf-8"))


def http_request(
    url: str,
    method: str,
    headers: dict[str, str],
    body: bytes | None = None,
    timeout: int = 30,
) -> HttpResponse:
    req = request.Request(url, data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return HttpResponse(
                int(response.status),
                dict(response.headers.items()),
                response.read(),
            )
    except error.HTTPError as exc:
        return HttpResponse(int(exc.code), dict(exc.headers.items()), exc.read())


def post_json(url: str, headers: dict[str, str], payload: Any, timeout: int = 30) -> HttpResponse:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    response = http_request(url, "POST", headers, body=body, timeout=timeout)
    if response.status_code >= 400:
        preview = response.content.decode("utf-8", errors="replace")[:500]
        raise SystemExit(f"HTTP {response.status_code} from {url}: {preview}")
    return response


def parsed_headers_from_curl(curl_file: Path) -> dict[str, str]:
    curl_text = curl_file.read_text(encoding="utf-8")
    patterns = [
        r"-H\s+\$?'((?:[^'\\]|\\.)*)'",
        r'-H\s+"((?:[^"\\]|\\.)*)"',
        r"-H\s+\^\"((?:.|\n)*?)\^\"",
    ]
    headers: dict[str, str] = {}
    for pattern in patterns:
        for match in re.finditer(pattern, curl_text, re.S):
            raw = match.group(1).replace('^"', '"').replace("^&", "&").replace("^$", "$")
            if ":" not in raw:
                continue
            name, value = raw.split(":", 1)
            name = name.strip()
            value = value.strip()
            if not name:
                continue
            headers[name.lower()] = value
    return headers


def chrome_like_headers(
    header_curl_file: Path | None,
    cookie: str,
    campaign_id: str,
    referer: str,
    task_id: str | None = None,
    task_kind: str | None = "widget-layout",
    request_type: str = "graphql",
) -> dict[str, str]:
    parsed = parsed_headers_from_curl(header_curl_file) if header_curl_file and header_curl_file.exists() else {}
    fallback = build_headers(cookie, campaign_id, referer, task_id=task_id, task_kind=task_kind)
    headers = {**fallback, **parsed}
    for name in ["host", "content-length"]:
        headers.pop(name, None)

    headers["cookie"] = cookie
    headers["referer"] = referer
    headers["origin"] = BASE_URL
    headers["x-feather-client-campaign-id"] = campaign_id
    if task_id:
        headers["x-feather-client-task-id"] = task_id
    if task_kind:
        headers["x-feather-client-task-kind"] = task_kind

    headers.setdefault("accept-language", "zh-CA,zh;q=0.9,en-CA;q=0.8,en;q=0.7,zh-CN;q=0.6")
    headers.setdefault("sec-ch-ua", '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"')
    headers.setdefault("sec-ch-ua-mobile", "?0")
    headers.setdefault("sec-ch-ua-platform", '"Windows"')
    headers.setdefault("sec-fetch-site", "same-origin")
    headers.setdefault("priority", "u=1, i")

    if request_type == "image":
        headers["accept"] = "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
        headers["sec-fetch-dest"] = "image"
        headers["sec-fetch-mode"] = "no-cors"
        headers.pop("content-type", None)
    else:
        headers["accept"] = headers.get("accept") or "*/*"
        headers["content-type"] = "application/json"
        headers["sec-fetch-dest"] = "empty"
        headers["sec-fetch-mode"] = "cors"
    return headers


def load_cookie(curl_file: Path) -> str:
    curl_text = curl_file.read_text(encoding="utf-8")
    cookie = cookie_value_from_curl(curl_text)
    if not cookie:
        raise SystemExit(f"No Feather cookie found in {curl_file}.")
    return cookie


def load_campaign_id(curl_file: Path, fallback: str | None = None) -> str:
    curl_text = curl_file.read_text(encoding="utf-8")
    campaign_id = header_value(curl_text, "x-feather-client-campaign-id") or fallback
    if not campaign_id:
        raw_body = option_value(curl_text, "--data-raw")
        if raw_body:
            try:
                payload = json.loads(decode_curl_data_raw(curl_text, raw_body))
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and payload.get("campaign_id"):
                campaign_id = str(payload["campaign_id"])
    if not campaign_id:
        raise SystemExit(f"No campaign id found in {curl_file}; pass --campaign-id.")
    return campaign_id


def asset_url(task_id: str, asset_id: str) -> str:
    return f"{BASE_URL}/api/assets/{asset_id}/redirect?entity_type=tasks&entity_id={task_id}"


def asset_id_from_url(url: str) -> str:
    marker = "/api/assets/"
    if marker not in url:
        return ""
    rest = url.split(marker, 1)[1]
    return rest.split("/", 1)[0]


def normalize_asset_rows(task_id: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for index, row in enumerate(rows, start=1):
        src = str(row.get("src") or "")
        asset_id = str(row.get("assetId") or row.get("asset_id") or asset_id_from_url(src))
        if not asset_id:
            raise SystemExit(f"Asset row {index} is missing assetId/src.")
        candidate = str(row.get("candidate") or "unknown")
        slide = int(row.get("slide") or row.get("slide_index") or index)
        normalized.append(
            {
                "candidate": candidate,
                "slide": slide,
                "asset_id": asset_id,
                "src": src or asset_url(task_id, asset_id),
                "width": row.get("width"),
                "height": row.get("height"),
            }
        )
    return normalized


def decode_curl_data_raw(curl_text: str, raw_body: str) -> str:
    if re.search(r"--data-raw\s+\$'", curl_text):
        return codecs.decode(raw_body, "unicode_escape")
    return raw_body


def graphql_payload_from_curl(curl_file: Path) -> Any:
    curl_text = curl_file.read_text(encoding="utf-8")
    raw_body = option_value(curl_text, "--data-raw")
    if not raw_body:
        raise SystemExit(f"No --data-raw GraphQL body found in {curl_file}.")
    return json.loads(decode_curl_data_raw(curl_text, raw_body))


def first_graphql_entry(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return payload[0]
    if isinstance(payload, dict):
        return payload
    raise SystemExit("GraphQL payload must be a JSON object or a one-item JSON list.")


def infer_task_id_from_payload(payload: Any) -> str | None:
    variables = first_graphql_entry(payload).get("variables") or {}
    task_id = variables.get("taskId")
    return str(task_id) if task_id else None


def prepare_conversation_payload(
    payload: Any,
    task_id: str | None,
    layout_key: str | None,
    keep_read_auth_token: bool,
) -> tuple[Any, str]:
    entry = first_graphql_entry(payload)
    variables = entry.setdefault("variables", {})
    resolved_task_id = task_id or variables.get("taskId")
    if not resolved_task_id:
        raise SystemExit("No task id found in GraphQL payload; pass --task-id.")
    variables["taskId"] = str(resolved_task_id)
    if layout_key:
        variables["layoutKey"] = layout_key
    if not keep_read_auth_token:
        variables.pop("readAuthToken", None)
    return payload, str(resolved_task_id)


def prepare_original_graphql_payload(
    payload: Any,
    task_id: str,
    layout_key: str | None = None,
    keep_read_auth_token: bool = False,
) -> tuple[Any, str]:
    entry = first_graphql_entry(payload)
    operation_name = str(entry.get("operationName") or "")
    variables = entry.setdefault("variables", {})
    variables["taskId"] = task_id
    if layout_key and "layoutKey" in variables:
        variables["layoutKey"] = layout_key
    if not keep_read_auth_token:
        variables.pop("readAuthToken", None)
    return payload, operation_name


def validate_graphql_response(data: Any) -> dict[str, Any]:
    root = data[0] if isinstance(data, list) and data else data
    if not isinstance(root, dict):
        raise SystemExit("Unexpected GraphQL response shape.")
    errors = root.get("errors")
    if errors:
        messages = "; ".join(str(item.get("message", item)) for item in errors[:3])
        raise SystemExit(f"GraphQL returned errors: {messages}")
    return root


def post_graphql_payload(
    auth_curl_file: Path,
    payload: Any,
    task_id: str,
    campaign_id: str | None,
    task_kind: str = "widget-layout",
    header_curl_file: Path | None = None,
) -> tuple[dict[str, Any], str]:
    cookie = load_cookie(auth_curl_file)
    resolved_campaign_id = load_campaign_id(auth_curl_file, campaign_id)
    headers = chrome_like_headers(
        header_curl_file or auth_curl_file,
        cookie,
        resolved_campaign_id,
        f"{BASE_URL}/tasks/{task_id}",
        task_id=task_id,
        task_kind=task_kind,
        request_type="graphql",
    )
    response = post_json(GRAPHQL_URL, headers, payload, timeout=30)
    return validate_graphql_response(response.json()), resolved_campaign_id


def graphql_data_value(root: dict[str, Any], key: str) -> Any:
    data = root.get("data") or {}
    return data.get(key) if isinstance(data, dict) else None


def asset_ids_from_redirect(root: dict[str, Any]) -> list[str]:
    redirect = graphql_data_value(root, "taskOrStagecraftRedirect") or {}
    asset_ids = redirect.get("assetIds") if isinstance(redirect, dict) else None
    return [str(asset_id) for asset_id in asset_ids] if isinstance(asset_ids, list) else []


def write_graphql_response(path: Path, root: dict[str, Any]) -> None:
    path.write_text(json.dumps([root], ensure_ascii=False, indent=2), encoding="utf-8")


def fetch_conversation_widget(
    graphql_curl_file: Path,
    task_id: str | None,
    layout_key: str | None,
    keep_read_auth_token: bool,
    output_dir: Path,
) -> tuple[dict[str, Any], str, str, Path]:
    curl_text = graphql_curl_file.read_text(encoding="utf-8")
    payload = graphql_payload_from_curl(graphql_curl_file)
    payload, resolved_task_id = prepare_conversation_payload(
        payload,
        task_id=task_id,
        layout_key=layout_key,
        keep_read_auth_token=keep_read_auth_token,
    )
    cookie = cookie_value_from_curl(curl_text)
    if not cookie:
        raise SystemExit(f"No Feather cookie found in {graphql_curl_file}.")
    campaign_id = load_campaign_id(graphql_curl_file)
    task_kind = header_value(curl_text, "x-feather-client-task-kind") or "widget-layout"
    headers = build_headers(
        cookie,
        campaign_id,
        f"{BASE_URL}/tasks/{resolved_task_id}",
        task_id=resolved_task_id,
        task_kind=task_kind,
    )
    response = post_json(GRAPHQL_URL, headers, payload, timeout=30)
    data = response.json()
    root = validate_graphql_response(data)
    response_path = output_dir / "conversation_widget_response.json"
    response_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return root, resolved_task_id, campaign_id, response_path


def fetch_original_two_step_graphql(
    auth_curl_file: Path,
    redirect_template_curl_file: Path,
    conversation_template_curl_file: Path,
    task_id: str,
    campaign_id: str | None,
    layout_key: str,
    keep_read_auth_token: bool,
    output_dir: Path,
) -> tuple[dict[str, Any], str, str, Path, Path]:
    redirect_payload, redirect_operation = prepare_original_graphql_payload(
        graphql_payload_from_curl(redirect_template_curl_file),
        task_id=task_id,
        layout_key=None,
        keep_read_auth_token=keep_read_auth_token,
    )
    if redirect_operation != "TaskOrStagecraftRedirect":
        raise SystemExit(
            f"--redirect-graphql-curl-file must be TaskOrStagecraftRedirect, got {redirect_operation or 'unknown'}."
        )
    redirect_root, resolved_campaign_id = post_graphql_payload(
        auth_curl_file,
        redirect_payload,
        task_id=task_id,
        campaign_id=campaign_id,
        header_curl_file=redirect_template_curl_file,
    )
    redirect_response_path = output_dir / "task_or_stagecraft_redirect_response.json"
    write_graphql_response(redirect_response_path, redirect_root)

    conversation_payload, conversation_operation = prepare_original_graphql_payload(
        graphql_payload_from_curl(conversation_template_curl_file),
        task_id=task_id,
        layout_key=layout_key,
        keep_read_auth_token=keep_read_auth_token,
    )
    if conversation_operation != "FetchConversationWidget":
        raise SystemExit(
            f"--conversation-graphql-curl-file must be FetchConversationWidget, got {conversation_operation or 'unknown'}."
        )
    conversation_root, resolved_campaign_id = post_graphql_payload(
        auth_curl_file,
        conversation_payload,
        task_id=task_id,
        campaign_id=resolved_campaign_id,
        header_curl_file=conversation_template_curl_file,
    )
    conversation_response_path = output_dir / "conversation_widget_response.json"
    write_graphql_response(conversation_response_path, conversation_root)
    return conversation_root, task_id, resolved_campaign_id, conversation_response_path, redirect_response_path


def fetch_conversation_widget_from_task_api(
    auth_curl_file: Path,
    task_id: str,
    campaign_id: str | None,
    layout_key: str,
    output_dir: Path,
) -> tuple[dict[str, Any], str, str, Path]:
    cookie = load_cookie(auth_curl_file)
    resolved_campaign_id = load_campaign_id(auth_curl_file, campaign_id)
    payload = [
        {
            "operationName": "FetchSlideConversationWidget",
            "variables": {"taskId": task_id, "layoutKey": layout_key},
            "query": FETCH_SLIDE_CONVERSATION_WIDGET_QUERY,
        }
    ]
    headers = chrome_like_headers(
        auth_curl_file,
        cookie,
        resolved_campaign_id,
        f"{BASE_URL}/tasks/{task_id}",
        task_id=task_id,
        task_kind="widget-layout",
        request_type="graphql",
    )
    response = post_json(GRAPHQL_URL, headers, payload, timeout=30)
    data = response.json()
    root = validate_graphql_response(data)
    response_path = output_dir / "conversation_widget_response.json"
    response_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return root, task_id, resolved_campaign_id, response_path


def load_conversation_widget_response(response_json: Path) -> dict[str, Any]:
    data = json.loads(response_json.read_text(encoding="utf-8"))
    return validate_graphql_response(data)


def content_parts_from_chat_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content") or {}
    if not isinstance(content, dict):
        content = ((message.get("message") or {}).get("content") or {})
    parts = content.get("parts") or []
    return parts if isinstance(parts, list) else []


def asset_id_from_pointer(pointer: str) -> str:
    if pointer.startswith("feather://"):
        return pointer.removeprefix("feather://")
    return asset_id_from_url(pointer) or pointer


def slide_number_from_text(text: str) -> int | None:
    match = re.search(r"\bslide\s+(\d+)\b", text, re.I)
    return int(match.group(1)) if match else None


def load_candidate_map(candidate_map_json: Path | None) -> dict[str, str]:
    if not candidate_map_json:
        return {}
    rows = json.loads(candidate_map_json.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise SystemExit("--candidate-map-json must contain a JSON list.")
    mapping: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        completion_id = row.get("completionId") or row.get("completion_id")
        candidate = row.get("candidate")
        if completion_id and candidate:
            mapping[str(completion_id)] = str(candidate)
    return mapping


def extract_rows_from_conversation_widget(
    task_id: str,
    root: dict[str, Any],
    candidate_map: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    widget = ((root.get("data") or {}).get("conversationWidget") or {})
    conversation = widget.get("conversation") or {}
    candidate_map = candidate_map or {}
    rows: list[dict[str, Any]] = []
    deck_index = 0
    for turn in conversation.get("turns") or []:
        for message in turn.get("messages") or []:
            for completion in message.get("completions") or []:
                deck_index += 1
                completion_id = str(completion.get("id") or f"deck_{deck_index:02d}")
                form_data = completion.get("formData") or {}
                rating = form_data.get("aesthetics-rating") if isinstance(form_data, dict) else None
                rationale = form_data.get("rationale") if isinstance(form_data, dict) else None
                candidate = candidate_map.get(completion_id) or f"deck_{deck_index:02d}"
                pending_slide: int | None = None
                fallback_slide = 0
                for chat_message in completion.get("messages") or []:
                    for part in content_parts_from_chat_message(chat_message):
                        typename = part.get("__typename")
                        if typename == "ChatStrTextPart":
                            pending_slide = slide_number_from_text(str(part.get("text") or ""))
                            continue
                        if typename != "ChatImageAssetPointer":
                            continue
                        pointer = str(part.get("assetPointer") or "")
                        asset_id = asset_id_from_pointer(pointer)
                        if not asset_id:
                            continue
                        fallback_slide += 1
                        slide = pending_slide or fallback_slide
                        rows.append(
                            {
                                "candidate": candidate,
                                "deck_index": deck_index,
                                "completion_id": completion_id,
                                "slide": slide,
                                "asset_id": asset_id,
                                "asset_pointer": pointer,
                                "src": asset_url(task_id, asset_id),
                                "width": part.get("width"),
                                "height": part.get("height"),
                                "size_bytes": part.get("sizeBytes"),
                                "rating": rating,
                                "rationale": rationale,
                            }
                        )
                        pending_slide = None
    if not rows:
        raise SystemExit("No ChatImageAssetPointer slide assets found in conversationWidget.")
    return rows


def safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value).strip("_") or "unknown"


def download_assets(
    task_id: str,
    campaign_id: str,
    cookie: str,
    rows: list[dict[str, Any]],
    output_dir: Path,
    header_curl_file: Path | None = None,
) -> list[dict[str, Any]]:
    headers = chrome_like_headers(
        header_curl_file,
        cookie,
        campaign_id,
        f"{BASE_URL}/tasks/{task_id}",
        task_id=task_id,
        task_kind="widget-layout",
        request_type="image",
    )
    results = []
    for row in rows:
        response = http_request(str(row["src"]), "GET", headers, timeout=30)
        content_type = response.headers.get("content-type", "")
        ext = mimetypes.guess_extension(content_type.split(";", 1)[0]) or ".bin"
        candidate_dir = output_dir / safe_name(str(row["candidate"]))
        candidate_dir.mkdir(parents=True, exist_ok=True)
        path = candidate_dir / f"slide_{int(row['slide']):02d}_{row['asset_id']}{ext}"
        path.write_bytes(response.content)
        results.append(
            {
                **row,
                "status": response.status_code,
                "content_type": content_type,
                "bytes": len(response.content),
                "path": str(path),
            }
        )
    return results


def candidate_order(results: list[dict[str, Any]]) -> list[str]:
    order = []
    for row in results:
        candidate = str(row["candidate"])
        if candidate not in order:
            order.append(candidate)
    return order


def make_contact_sheet(results: list[dict[str, Any]], output_dir: Path) -> Path | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    candidates = candidate_order(results)
    max_slide = max(int(row["slide"]) for row in results)
    thumb_w, thumb_h = 320, 180
    label_h = 34
    left_w = 52
    sheet = Image.new(
        "RGB",
        (left_w + thumb_w * max_slide, label_h + (thumb_h + label_h) * len(candidates)),
        "white",
    )
    draw = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("arial.ttf", 18)
        small = ImageFont.truetype("arial.ttf", 14)
    except OSError:
        font = ImageFont.load_default()
        small = ImageFont.load_default()

    for slide in range(1, max_slide + 1):
        x = left_w + (slide - 1) * thumb_w
        draw.text((x + 8, 8), f"Slide {slide}", fill=(0, 0, 0), font=font)

    for row_index, candidate in enumerate(candidates):
        y0 = label_h + row_index * (thumb_h + label_h)
        draw.text((8, y0 + 70), candidate, fill=(0, 0, 0), font=font)
        candidate_rows = sorted(
            [row for row in results if row["candidate"] == candidate],
            key=lambda item: int(item["slide"]),
        )
        for row in candidate_rows:
            image = Image.open(row["path"]).convert("RGB")
            image.thumbnail((thumb_w, thumb_h), Image.LANCZOS)
            x = left_w + (int(row["slide"]) - 1) * thumb_w
            sheet.paste(image, (x, y0))
            draw.rectangle([x, y0, x + thumb_w - 1, y0 + thumb_h - 1], outline=(180, 180, 180))
            draw.text((x + 4, y0 + thumb_h + 6), f"{candidate}{row['slide']}", fill=(0, 0, 0), font=small)

    output_path = output_dir / "contact_sheet.jpg"
    sheet.save(output_path, quality=90)
    return output_path


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_candidate: dict[str, dict[str, Any]] = {}
    for row in results:
        bucket = by_candidate.setdefault(
            str(row["candidate"]),
            {
                "count": 0,
                "bytes": 0,
                "statuses": set(),
                "completion_id": row.get("completion_id"),
                "rating": row.get("rating"),
            },
        )
        bucket["count"] += 1
        bucket["bytes"] += int(row["bytes"])
        bucket["statuses"].add(int(row["status"]))
    return {
        "downloaded": len(results),
        "candidates": {
            key: {**value, "statuses": sorted(value["statuses"])}
            for key, value in by_candidate.items()
        },
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download Feather task slide assets.")
    parser.add_argument("--task-id", help="Required for --assets-json; optional override for GraphQL cURL input.")
    parser.add_argument("--campaign-id", help="Optional; inferred from cURL headers when possible.")
    parser.add_argument("--assets-json", type=Path, help="JSON exported from the task page DOM.")
    parser.add_argument("--api", action="store_true", help="Fetch slides directly from Feather's conversationWidget API using --task-id and --curl-file.")
    parser.add_argument("--api-original", action="store_true", help="Fetch slides with the original two GraphQL requests copied from Chrome.")
    parser.add_argument("--redirect-graphql-curl-file", type=Path, help="TaskOrStagecraftRedirect cURL copied from Chrome Network.")
    parser.add_argument("--conversation-graphql-curl-file", type=Path, help="FetchConversationWidget cURL copied from Chrome Network.")
    parser.add_argument("--graphql-curl-file", type=Path, help="FetchConversationWidget request copied as cURL.")
    parser.add_argument("--conversation-response-json", type=Path, help="Saved FetchConversationWidget JSON response.")
    parser.add_argument("--candidate-map-json", type=Path, help="Optional DOM JSON used only to map completion ids to A/B/C labels.")
    parser.add_argument("--layout-key", default="prompt_conversation")
    parser.add_argument("--keep-read-auth-token", action="store_true")
    parser.add_argument("--curl-file", type=Path, help="cURL file to borrow the cookie from for asset downloads.")
    parser.add_argument("--output-dir", type=Path, help="Defaults to outputs/task_slides/<task-id>.")
    parser.add_argument("--no-contact-sheet", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_count = sum(
        bool(value)
        for value in [
            args.assets_json,
            args.api,
            args.api_original,
            args.graphql_curl_file,
            args.conversation_response_json,
        ]
    )
    if input_count != 1:
        raise SystemExit("Pass exactly one of --assets-json, --api, --api-original, --graphql-curl-file, or --conversation-response-json.")

    inferred_task_id = args.task_id
    if args.graphql_curl_file and not inferred_task_id:
        inferred_task_id = infer_task_id_from_payload(graphql_payload_from_curl(args.graphql_curl_file))
    if not inferred_task_id and args.assets_json:
        raise SystemExit("--task-id is required with --assets-json.")
    if not inferred_task_id and args.api:
        raise SystemExit("--task-id is required with --api.")
    if not inferred_task_id and args.api_original:
        raise SystemExit("--task-id is required with --api-original.")
    if not inferred_task_id and args.conversation_response_json:
        raise SystemExit("--task-id is required with --conversation-response-json.")

    output_dir = args.output_dir or Path("outputs") / "task_slides" / str(inferred_task_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    task_id = str(inferred_task_id)
    campaign_id = args.campaign_id
    response_path = None
    redirect_response_path = None
    auth_curl_file = args.curl_file or args.graphql_curl_file or DEFAULT_CURL_FILE
    if args.assets_json:
        raw_rows = json.loads(args.assets_json.read_text(encoding="utf-8"))
        if not isinstance(raw_rows, list):
            raise SystemExit("--assets-json must contain a JSON list.")
        rows = normalize_asset_rows(task_id, raw_rows)
    else:
        candidate_map = load_candidate_map(args.candidate_map_json)
        if args.api:
            if not auth_curl_file.exists():
                raise SystemExit(f"No auth cURL file found for API request: {auth_curl_file}")
            root, task_id, campaign_id, response_path = fetch_conversation_widget_from_task_api(
                auth_curl_file,
                task_id=task_id,
                campaign_id=args.campaign_id,
                layout_key=args.layout_key,
                output_dir=output_dir,
            )
        elif args.api_original:
            if not auth_curl_file.exists():
                raise SystemExit(f"No auth cURL file found for API request: {auth_curl_file}")
            if not args.redirect_graphql_curl_file or not args.conversation_graphql_curl_file:
                raise SystemExit("--api-original requires --redirect-graphql-curl-file and --conversation-graphql-curl-file.")
            root, task_id, campaign_id, response_path, redirect_response_path = fetch_original_two_step_graphql(
                auth_curl_file,
                redirect_template_curl_file=args.redirect_graphql_curl_file,
                conversation_template_curl_file=args.conversation_graphql_curl_file,
                task_id=task_id,
                campaign_id=args.campaign_id,
                layout_key=args.layout_key,
                keep_read_auth_token=args.keep_read_auth_token,
                output_dir=output_dir,
            )
        elif args.graphql_curl_file:
            root, task_id, campaign_id, response_path = fetch_conversation_widget(
                args.graphql_curl_file,
                task_id=args.task_id,
                layout_key=args.layout_key,
                keep_read_auth_token=args.keep_read_auth_token,
                output_dir=output_dir,
            )
        else:
            root = load_conversation_widget_response(args.conversation_response_json)
        rows = extract_rows_from_conversation_widget(task_id, root, candidate_map=candidate_map)
        if redirect_response_path:
            redirect_root = load_conversation_widget_response(redirect_response_path)
            redirect_asset_ids = set(asset_ids_from_redirect(redirect_root))
            row_asset_ids = {str(row["asset_id"]) for row in rows}
            missing_from_redirect = sorted(row_asset_ids - redirect_asset_ids)
            extra_in_redirect = sorted(redirect_asset_ids - row_asset_ids)
            (output_dir / "asset_id_crosscheck.json").write_text(
                json.dumps(
                    {
                        "conversation_asset_count": len(row_asset_ids),
                        "redirect_asset_count": len(redirect_asset_ids),
                        "same_set": not missing_from_redirect and not extra_in_redirect,
                        "missing_from_redirect": missing_from_redirect,
                        "extra_in_redirect": extra_in_redirect,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

    if not auth_curl_file.exists():
        raise SystemExit(f"No auth cURL file found for asset downloads: {auth_curl_file}")
    campaign_id = campaign_id or load_campaign_id(auth_curl_file, args.campaign_id)
    cookie = load_cookie(auth_curl_file)
    asset_header_curl_file = args.conversation_graphql_curl_file or args.graphql_curl_file or auth_curl_file
    results = download_assets(task_id, campaign_id, cookie, rows, output_dir, header_curl_file=asset_header_curl_file)
    results_path = output_dir / "download_results.json"
    results_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    contact_sheet = None if args.no_contact_sheet else make_contact_sheet(results, output_dir)
    print(
        json.dumps(
            {
                **summarize(results),
                "results": str(results_path),
                "contact_sheet": str(contact_sheet) if contact_sheet else None,
                "conversation_response": str(response_path) if response_path else None,
                "redirect_response": str(redirect_response_path) if redirect_response_path else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    failures = [row for row in results if int(row["status"]) != 200]
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
