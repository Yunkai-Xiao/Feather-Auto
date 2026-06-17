from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib import error, request


DEFAULT_CURL_FILE = Path("outputs/current_feather_request.curl.txt")
DEFAULT_REDIRECT_GRAPHQL_CURL_FILE = Path("outputs/current_feather_task_or_stagecraft_redirect.curl.txt")
DEFAULT_CONVERSATION_GRAPHQL_CURL_FILE = Path("outputs/current_feather_conversation_widget.curl.txt")
DEFAULT_OUTPUT_ROOT = Path("outputs/content_review")
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = os.environ.get("FEATHER_REVIEW_MODEL", "gpt-5.5")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def status(status_dir: Path, **payload: Any) -> None:
    write_json(status_dir / "review_status.json", payload)


def require_file(path: Path, label: str) -> None:
    if not path.exists():
        raise SystemExit(f"Missing {label}: {path}")


def run_downloader(
    task_id: str,
    curl_file: Path,
    redirect_graphql_curl_file: Path,
    conversation_graphql_curl_file: Path,
    output_dir: Path,
) -> dict[str, Any]:
    require_file(curl_file, "task search/auth cURL")
    require_file(redirect_graphql_curl_file, "TaskOrStagecraftRedirect cURL")
    require_file(conversation_graphql_curl_file, "FetchConversationWidget cURL")
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "feather_auto.download_task_slides",
        "--api-original",
        "--task-id",
        task_id,
        "--curl-file",
        str(curl_file),
        "--redirect-graphql-curl-file",
        str(redirect_graphql_curl_file),
        "--conversation-graphql-curl-file",
        str(conversation_graphql_curl_file),
        "--output-dir",
        str(output_dir),
    ]
    proc = subprocess.run(cmd, cwd=Path.cwd(), text=True, capture_output=True, check=False)
    (output_dir / "download_stdout.txt").write_text(proc.stdout, encoding="utf-8")
    (output_dir / "download_stderr.txt").write_text(proc.stderr, encoding="utf-8")
    if proc.returncode != 0:
        raise SystemExit(f"slide download failed with exit {proc.returncode}: {proc.stderr or proc.stdout}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"stdout": proc.stdout}


def build_slide_manifest(download_results_path: Path, output_dir: Path) -> dict[str, Any]:
    rows = read_json(download_results_path)
    decks: dict[str, dict[str, Any]] = {}
    for row in rows:
        deck = str(row["candidate"])
        slide = int(row["slide"])
        deck_bucket = decks.setdefault(
            deck,
            {
                "completion_id": row.get("completion_id"),
                "deck_index": row.get("deck_index"),
                "slides": {},
            },
        )
        deck_bucket["slides"][str(slide)] = {
            "slide": slide,
            "asset_id": row.get("asset_id"),
            "path": row.get("path"),
            "content_type": row.get("content_type"),
            "bytes": row.get("bytes"),
            "status": row.get("status"),
        }
    manifest = {
        "deck_count": len(decks),
        "slide_count": len(rows),
        "decks": {
            deck: {
                **value,
                "slides": dict(sorted(value["slides"].items(), key=lambda item: int(item[0]))),
            }
            for deck, value in sorted(decks.items())
        },
    }
    write_json(output_dir / "slide_manifest.json", manifest)
    return manifest


def data_url(path: Path) -> str:
    suffix = path.suffix.lower()
    mime = "image/jpeg"
    if suffix == ".png":
        mime = "image/png"
    elif suffix == ".webp":
        mime = "image/webp"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def post_openai(api_key: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        OPENAI_RESPONSES_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with request.urlopen(req, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        preview = exc.read().decode("utf-8", errors="replace")[:1000]
        raise SystemExit(f"OpenAI API HTTP {exc.code}: {preview}") from exc


def response_text(response: dict[str, Any]) -> str:
    if isinstance(response.get("output_text"), str):
        return str(response["output_text"])
    chunks: list[str] = []
    for item in response.get("output") or []:
        if item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks).strip()


def parse_json_text(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        stripped = stripped.removeprefix("json").strip()
    return json.loads(stripped)


def task_prompt_from_conversation(response_path: Path | None) -> str:
    if not response_path or not response_path.exists():
        return ""
    root = read_json(response_path)
    root = root[0] if isinstance(root, list) and root else root
    conversation = (((root.get("data") or {}).get("conversationWidget") or {}).get("conversation") or {})
    for turn in conversation.get("turns") or []:
        for message in turn.get("messages") or []:
            raw_message = message.get("message") or {}
            content = raw_message.get("content") or {}
            parts = content.get("parts") or []
            texts = [str(part.get("text") or "") for part in parts if part.get("__typename") == "ChatStrTextPart"]
            if texts:
                return "\n".join(texts).strip()
    return ""


def extract_slide_texts(manifest: dict[str, Any], output_dir: Path, api_key: str, model: str) -> dict[str, Any]:
    result = {"decks": {}}
    for deck, deck_data in manifest["decks"].items():
        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": (
                    "Extract the visible text from these slide screenshots. "
                    "Return JSON only with shape {\"slides\":[{\"slide\":1,\"sentences\":[...],\"raw_text\":\"...\"}]}. "
                    "Use the slide labels provided before each image. Do not evaluate quality."
                ),
            }
        ]
        for slide_key, slide_data in deck_data["slides"].items():
            image_path = Path(str(slide_data["path"]))
            content.append({"type": "input_text", "text": f"{deck} slide {slide_key}"})
            content.append({"type": "input_image", "image_url": data_url(image_path)})
        response = post_openai(
            api_key,
            {
                "model": model,
                "input": [{"role": "user", "content": content}],
            },
        )
        raw_text = response_text(response)
        try:
            parsed = parse_json_text(raw_text)
        except json.JSONDecodeError:
            parsed = {"raw_model_output": raw_text}
        result["decks"][deck] = parsed
        write_json(output_dir / "llm_raw" / f"{deck}_text_response.json", response)
    write_json(output_dir / "slide_text_by_deck.json", result)
    return result


def generate_issue_candidates(
    slide_texts: dict[str, Any],
    task_prompt: str,
    output_dir: Path,
    api_key: str,
    model: str,
) -> dict[str, Any]:
    prompt = (
        "You are assisting with Content Grading preparation. Do not write final submit-ready rationales. "
        "Find potential content issues and strengths only, grounded in slide text. "
        "Focus on AI content slop, instruction following, relevance, specificity, filler, placeholders, "
        "odd wording, self-referential text, and understandability. Do not judge visual aesthetics unless it affects understanding. "
        "Do not fact-check with outside knowledge. Return JSON only with shape "
        "{\"decks\":{\"deck_01\":{\"issue_candidates\":[{\"slide\":1,\"type\":\"ai_slop|instruction_following|understandability|strength\","
        "\"evidence\":\"...\",\"why_it_matters\":\"...\"}]}}}."
    )
    response = post_openai(
        api_key,
        {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        {"type": "input_text", "text": "User request / task prompt:\n" + (task_prompt or "(not found)")},
                        {"type": "input_text", "text": json.dumps(slide_texts, ensure_ascii=False)},
                    ],
                }
            ],
        },
    )
    raw_text = response_text(response)
    try:
        parsed = parse_json_text(raw_text)
    except json.JSONDecodeError:
        parsed = {"raw_model_output": raw_text}
    write_json(output_dir / "content_issue_candidates.json", parsed)
    write_json(output_dir / "llm_raw" / "issue_candidates_response.json", response)
    return parsed


def run_review_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = args.output_dir or DEFAULT_OUTPUT_ROOT / args.task_id
    output_dir.mkdir(parents=True, exist_ok=True)
    status(output_dir, state="starting", task_id=args.task_id)

    download_summary = None
    if not args.skip_download:
        status(output_dir, state="downloading", task_id=args.task_id)
        download_summary = run_downloader(
            args.task_id,
            args.curl_file,
            args.redirect_graphql_curl_file,
            args.conversation_graphql_curl_file,
            output_dir,
        )

    results_path = output_dir / "download_results.json"
    require_file(results_path, "download_results.json")
    manifest = build_slide_manifest(results_path, output_dir)

    conversation_path = output_dir / "conversation_widget_response.json"
    task_prompt = task_prompt_from_conversation(conversation_path)
    (output_dir / "task_prompt.txt").write_text(task_prompt, encoding="utf-8")

    api_key = os.environ.get("OPENAI_API_KEY")
    llm_state = "skipped_no_openai_api_key"
    slide_texts = None
    issue_candidates = None
    if api_key and not args.no_llm:
        status(output_dir, state="extracting_slide_text", task_id=args.task_id)
        slide_texts = extract_slide_texts(manifest, output_dir, api_key, args.model)
        status(output_dir, state="finding_issue_candidates", task_id=args.task_id)
        issue_candidates = generate_issue_candidates(slide_texts, task_prompt, output_dir, api_key, args.model)
        llm_state = "completed"

    final = {
        "state": "completed",
        "task_id": args.task_id,
        "output_dir": str(output_dir),
        "download_summary": download_summary,
        "manifest": str(output_dir / "slide_manifest.json"),
        "task_prompt": str(output_dir / "task_prompt.txt"),
        "slide_text_by_deck": str(output_dir / "slide_text_by_deck.json") if slide_texts else None,
        "content_issue_candidates": str(output_dir / "content_issue_candidates.json") if issue_candidates else None,
        "llm": llm_state,
    }
    status(output_dir, **final)
    return final


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download claimed task slides and prepare Content Grading evidence.")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--curl-file", type=Path, default=DEFAULT_CURL_FILE)
    parser.add_argument("--redirect-graphql-curl-file", type=Path, default=DEFAULT_REDIRECT_GRAPHQL_CURL_FILE)
    parser.add_argument("--conversation-graphql-curl-file", type=Path, default=DEFAULT_CONVERSATION_GRAPHQL_CURL_FILE)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(json.dumps(run_review_pipeline(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
