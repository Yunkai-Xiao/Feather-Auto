from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, as_completed, wait
from pathlib import Path
from typing import Any, Callable
from urllib import error, request


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DEFAULT_CURL_FILE = Path("outputs/current_feather_request.curl.txt")
DEFAULT_REDIRECT_GRAPHQL_CURL_FILE = Path("outputs/current_feather_task_or_stagecraft_redirect.curl.txt")
DEFAULT_CONVERSATION_GRAPHQL_CURL_FILE = Path("outputs/current_feather_conversation_widget.curl.txt")
DEFAULT_OUTPUT_ROOT = Path("outputs/content_review")
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODEL = os.environ.get("FEATHER_REVIEW_MODEL", "gpt-5.5")
DEFAULT_CODEX_MODEL = os.environ.get("FEATHER_REVIEW_CODEX_MODEL", DEFAULT_MODEL)
DEFAULT_LLM_BACKEND = os.environ.get("FEATHER_REVIEW_LLM_BACKEND", "auto")
DEFAULT_REVIEW_SPEED = os.environ.get("FEATHER_REVIEW_SPEED", "fast")
CODEX_EXEC_TIMEOUT_SECONDS = int(os.environ.get("FEATHER_REVIEW_CODEX_TIMEOUT_SECONDS", "1800"))
DEFAULT_CODEX_WORKERS = max(1, int(os.environ.get("FEATHER_REVIEW_CODEX_WORKERS", "3")))
DEFAULT_OCR_WORKERS = max(1, int(os.environ.get("FEATHER_REVIEW_OCR_WORKERS", "4")))
DEFAULT_COMMENTS_PER_DECK = max(1, int(os.environ.get("FEATHER_REVIEW_COMMENTS_PER_DECK", "6")))
DEFAULT_OCR_BACKEND = os.environ.get("FEATHER_REVIEW_OCR_BACKEND", "paddle")
DEFAULT_ALLOW_NON_PADDLE_OCR = env_flag("FEATHER_REVIEW_ALLOW_NON_PADDLE_OCR")
DEFAULT_PADDLEOCR_DEVICE = os.environ.get("FEATHER_REVIEW_PADDLEOCR_DEVICE", "cpu")
DEFAULT_PADDLEOCR_LANG = os.environ.get("FEATHER_REVIEW_PADDLEOCR_LANG", "en")
DEFAULT_PADDLEOCR_DET_MODEL = os.environ.get("FEATHER_REVIEW_PADDLEOCR_DET_MODEL", "PP-OCRv5_mobile_det")
DEFAULT_PADDLEOCR_REC_MODEL = os.environ.get("FEATHER_REVIEW_PADDLEOCR_REC_MODEL", "PP-OCRv5_mobile_rec")
SUPPORTED_PADDLEOCR_SUFFIXES = {".bmp", ".dib", ".jpeg", ".jpg", ".png", ".webp", ".pbm", ".pgm", ".ppm", ".pnm", ".sr", ".ras", ".tiff", ".tif", ".pdf"}
OCR_DASH_PUNCTUATION_PROMPT = (
    "Preserve visible dash punctuation when it is clear: use '-' for a short hyphen, "
    "'\u2013' for an en dash, and '\u2014' for an em dash. If a dash-like mark is genuinely ambiguous, "
    "keep '-' and do not guess. "
)
CONTENT_GRADING_REVIEW_RULES = (
    "Reviewer preferences: judge each slide primarily on its own. Do not flag cross-slide repetition; "
    "only flag repetition when the same slide repeats the same idea without adding information. "
    "A short or abstract title can be acceptable when the body text on that same slide clearly explains it; "
    "only flag a vague title or heading when the surrounding slide text does not clarify the meaning, "
    "or when the title itself creates real confusion. Treat OCR dash artifacts leniently: long dashes, "
    "en dashes, hyphens, bullet markers, or mojibake around dash-like punctuation are not content issues by themselves. "
    "Do not comment on a phrase solely because OCR normalized or lost a dash. Avoid OCR punctuation/spacing nitpicks "
    "unless they make the slide text visibly hard to read."
)
_PADDLEOCR_ENGINE: Any | None = None
_PADDLEOCR_INIT_LOCK = threading.Lock()
_PADDLEOCR_PREDICT_LOCK = threading.Lock()
_PADDLEOCR_THREAD_LOCAL = threading.local()
StatusCallback = Callable[[dict[str, Any]], None]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def status(status_dir: Path, **payload: Any) -> None:
    write_json(status_dir / "review_status.json", payload)


def emit_status(status_dir: Path, callback: StatusCallback | None = None, **payload: Any) -> None:
    status(status_dir, **payload)
    if callback:
        callback(dict(payload))


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


def text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def markdown_bullet(value: str) -> str:
    return value.replace("\n", " ").strip()


def deck_payload(value: Any, deck: str) -> dict[str, Any]:
    if isinstance(value, dict):
        decks = value.get("decks")
        if isinstance(decks, dict) and isinstance(decks.get(deck), dict):
            return dict(decks[deck])
        return dict(value)
    return {"raw_model_output": text_value(value)}


def normalize_slide_text_result(value: Any, slide: int) -> dict[str, Any]:
    if isinstance(value, dict):
        slides = value.get("slides")
        if isinstance(slides, list) and slides and isinstance(slides[0], dict):
            row = dict(slides[0])
        else:
            row = dict(value)
    else:
        row = {"raw_text": text_value(value)}
    row["slide"] = int(row.get("slide") or slide)
    row.setdefault("sentences", [])
    row.setdefault("phrases", [])
    row.setdefault("raw_text", "")
    return row


def resolve_ocr_backend(value: Any, allow_non_paddle: bool = True) -> str:
    backend = text_value(value or DEFAULT_OCR_BACKEND).lower()
    if backend not in {"paddle", "codex", "api"}:
        raise SystemExit(f"Unsupported OCR backend: {backend}. Use paddle, codex, or api.")
    if backend != "paddle" and not allow_non_paddle:
        raise SystemExit(
            f"OCR backend {backend!r} is disabled by default. The review pipeline should use PaddleOCR for slide text. "
            "Set FEATHER_REVIEW_ALLOW_NON_PADDLE_OCR=1 or pass --allow-non-paddle-ocr only for an explicit debug fallback."
        )
    return backend


def positive_int(value: Any, default: int = 1) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def image_suffix_from_magic(path: Path) -> str | None:
    with path.open("rb") as handle:
        header = handle.read(16)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return ".tif"
    if header.startswith(b"%PDF"):
        return ".pdf"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return ".webp"
    return None


def paddleocr_compatible_image_path(image_path: Path, output_dir: Path) -> Path:
    suffix = image_path.suffix.lower()
    if suffix in SUPPORTED_PADDLEOCR_SUFFIXES:
        return image_path
    inferred_suffix = image_suffix_from_magic(image_path)
    if not inferred_suffix:
        return image_path
    cache_dir = output_dir / "paddleocr_images"
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / f"{image_path.stem}{inferred_suffix}"
    if not target.exists() or target.stat().st_size != image_path.stat().st_size:
        shutil.copyfile(image_path, target)
    return target


def paddleocr_engine() -> Any:
    engine = getattr(_PADDLEOCR_THREAD_LOCAL, "engine", None)
    if engine is None:
        with _PADDLEOCR_INIT_LOCK:
            engine = getattr(_PADDLEOCR_THREAD_LOCAL, "engine", None)
            if engine is not None:
                return engine
            try:
                from paddleocr import PaddleOCR
            except ImportError as exc:
                raise SystemExit(
                    "PaddleOCR is not installed. Run scripts\\setup-windows.ps1, "
                    "or set FEATHER_REVIEW_OCR_BACKEND=codex to use Codex vision OCR."
                ) from exc
            os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
            kwargs: dict[str, Any] = {
                "device": DEFAULT_PADDLEOCR_DEVICE,
                "use_doc_orientation_classify": False,
                "use_doc_unwarping": False,
                "use_textline_orientation": False,
            }
            if DEFAULT_PADDLEOCR_DET_MODEL:
                kwargs["text_detection_model_name"] = DEFAULT_PADDLEOCR_DET_MODEL
            if DEFAULT_PADDLEOCR_REC_MODEL:
                kwargs["text_recognition_model_name"] = DEFAULT_PADDLEOCR_REC_MODEL
            if not DEFAULT_PADDLEOCR_DET_MODEL and not DEFAULT_PADDLEOCR_REC_MODEL:
                kwargs["lang"] = DEFAULT_PADDLEOCR_LANG
            engine = PaddleOCR(**kwargs)
            _PADDLEOCR_THREAD_LOCAL.engine = engine
    return engine


def is_repairable_ocr_hyphen(text: str, index: int) -> bool:
    if text[index] != "-":
        return False
    before = text[index - 1] if index > 0 else ""
    after = text[index + 1] if index + 1 < len(text) else ""
    if not text[:index].strip():
        return False
    if before.isalnum() and after.isalnum():
        return False
    if before == "-" or after == "-":
        return True
    return before.isspace() or after.isspace()


def ocr_char_weight(char: str) -> float:
    if char.isspace():
        return 0.45
    if char in "-\u2010\u2011\u2012\u2013\u2014\u2212":
        return 0.72
    if char in "ilI.,'`!|:;":
        return 0.42
    if char in "MW@#%&":
        return 1.28
    return 1.0


def longest_true_run(values: Any) -> int:
    longest = 0
    current = 0
    for value in values:
        if bool(value):
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def classify_dash_glyph_from_window(gray_window: Any, entry_height: float, char_width: float) -> dict[str, Any] | None:
    try:
        import numpy as np
    except ImportError:
        return None

    if gray_window is None or gray_window.size < 12:
        return None
    arr = gray_window.astype("float32")
    background = float(np.median(arr))
    contrast = float(np.std(arr))
    threshold = max(18.0, min(55.0, contrast * 0.8))
    foreground = np.abs(arr - background) >= threshold
    height = int(foreground.shape[0])
    if height <= 0:
        return None
    band_y0 = max(0, int(height * 0.25))
    band_y1 = min(height, max(band_y0 + 1, int(height * 0.75)))
    band = foreground[band_y0:band_y1, :]
    if band.size == 0:
        return None
    min_column_hits = max(1, int(band.shape[0] * 0.12))
    column_mask = band.sum(axis=0) >= min_column_hits
    run_px = longest_true_run(column_mask)
    if run_px < 4:
        return None
    height_ratio = run_px / max(1.0, float(entry_height))
    char_ratio = run_px / max(1.0, float(char_width))
    if char_ratio >= 1.15 or height_ratio >= 0.55:
        return {"char": "\u2014", "run_px": run_px, "height_ratio": round(height_ratio, 3), "char_ratio": round(char_ratio, 3)}
    if char_ratio >= 0.78 or height_ratio >= 0.35:
        return {"char": "\u2013", "run_px": run_px, "height_ratio": round(height_ratio, 3), "char_ratio": round(char_ratio, 3)}
    return None


def repair_ocr_dashes_in_text(text: str, bbox: list[float] | None, image_array: Any) -> tuple[str, list[dict[str, Any]]]:
    if "-" not in text or not bbox or len(bbox) < 4 or image_array is None:
        return text, []
    x0, y0, x1, y1 = [float(value) for value in bbox[:4]]
    if x1 <= x0 or y1 <= y0:
        return text, []

    height_px, width_px = image_array.shape[:2]
    left = max(0, int(x0))
    top = max(0, int(y0))
    right = min(width_px, max(left + 1, int(x1)))
    bottom = min(height_px, max(top + 1, int(y1)))
    entry_width = right - left
    entry_height = bottom - top
    if entry_width <= 0 or entry_height <= 0:
        return text, []

    weights = [ocr_char_weight(char) for char in text]
    total_weight = sum(weights) or float(len(text) or 1)
    chars = list(text)
    adjustments: list[dict[str, Any]] = []
    prefix_weight = 0.0
    for index, char in enumerate(text):
        char_weight = weights[index]
        if is_repairable_ocr_hyphen(text, index):
            center_ratio = (prefix_weight + (char_weight / 2.0)) / total_weight
            center_x = int(left + (entry_width * center_ratio))
            char_width_px = max(1.0, entry_width * (char_weight / total_weight))
            half_window = int(max(8.0, min(72.0, max(entry_height * 0.9, char_width_px * 2.5))))
            window_left = max(left, center_x - half_window)
            window_right = min(right, center_x + half_window)
            gray_window = image_array[top:bottom, window_left:window_right]
            glyph = classify_dash_glyph_from_window(gray_window, entry_height, char_width_px)
            if glyph:
                chars[index] = str(glyph["char"])
                adjustments.append(
                    {
                        "index": index,
                        "from": "-",
                        "to": str(glyph["char"]),
                        "run_px": glyph["run_px"],
                        "height_ratio": glyph["height_ratio"],
                        "char_ratio": glyph["char_ratio"],
                    }
                )
        prefix_weight += char_weight
    if not adjustments:
        return text, []
    return "".join(chars), adjustments


def repair_paddleocr_dash_entries(entries: list[dict[str, Any]], image_path: Path) -> list[dict[str, Any]]:
    dash_entries = [
        entry
        for entry in entries
        if "-" in text_value(entry.get("text")) and isinstance(entry.get("bbox"), list) and sequence_len(entry.get("bbox")) >= 4
    ]
    if not dash_entries:
        return entries
    try:
        import numpy as np
        from PIL import Image

        with Image.open(image_path) as image:
            image_array = np.asarray(image.convert("L"))
    except Exception:
        return entries
    repaired: list[dict[str, Any]] = []
    for entry in entries:
        text = text_value(entry.get("text"))
        if entry in dash_entries:
            repaired_text, adjustments = repair_ocr_dashes_in_text(text, entry.get("bbox"), image_array)
            if adjustments:
                entry = dict(entry)
                entry["text_original"] = text
                entry["text"] = repaired_text
                entry["dash_adjustments"] = adjustments
        repaired.append(entry)
    return repaired


def float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def sequence_len(value: Any) -> int:
    try:
        return len(value)
    except Exception:
        return 0


def median_float(values: list[float], default: float = 18.0) -> float:
    if not values:
        return default
    sorted_values = sorted(values)
    midpoint = len(sorted_values) // 2
    if len(sorted_values) % 2:
        return sorted_values[midpoint]
    return (sorted_values[midpoint - 1] + sorted_values[midpoint]) / 2


def bbox_from_poly(value: Any) -> list[float] | None:
    if hasattr(value, "tolist"):
        value = value.tolist()
    points: list[tuple[float, float]] = []
    for point in value or []:
        if sequence_len(point) >= 2:
            points.append((float(point[0]), float(point[1])))
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return [min(xs), min(ys), max(xs), max(ys)]


def bbox_from_box(value: Any) -> list[float] | None:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if sequence_len(value) < 4:
        return None
    return [float(value[0]), float(value[1]), float(value[2]), float(value[3])]


def extract_paddleocr_entries(value: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    if isinstance(value, dict):
        texts = value.get("rec_texts")
        scores = value.get("rec_scores")
        if isinstance(texts, list):
            score_values = scores if isinstance(scores, list) else []
            polys = value.get("rec_polys")
            if polys is None:
                polys = value.get("dt_polys")
            if polys is None:
                polys = []
            boxes = value.get("rec_boxes")
            if boxes is None:
                boxes = []
            for index, raw_text in enumerate(texts):
                text = text_value(raw_text)
                if not text:
                    continue
                bbox = None
                if index < sequence_len(polys):
                    bbox = bbox_from_poly(polys[index])
                if bbox is None and index < sequence_len(boxes):
                    bbox = bbox_from_box(boxes[index])
                score = score_values[index] if index < len(score_values) else None
                entries.append({"text": text, "score": float_or_none(score), "bbox": bbox})
            return entries
        for nested in value.values():
            entries.extend(extract_paddleocr_entries(nested))
        return entries
    if isinstance(value, (list, tuple)):
        if (
            len(value) >= 2
            and isinstance(value[1], (list, tuple))
            and value[1]
            and isinstance(value[1][0], str)
        ):
            score = value[1][1] if len(value[1]) > 1 else None
            entries.append({"text": text_value(value[1][0]), "score": float_or_none(score)})
            return entries
        for nested in value:
            entries.extend(extract_paddleocr_entries(nested))
    return entries


def render_ocr_layout(entries: list[dict[str, Any]], image_path: Path, chars: int = 112) -> str:
    positioned = [dict(entry) for entry in entries if isinstance(entry.get("bbox"), list) and sequence_len(entry.get("bbox")) >= 4]
    if not positioned:
        return ""
    try:
        from PIL import Image

        with Image.open(image_path) as image:
            width_px = max(1, int(image.size[0]))
    except Exception:
        width_px = max(1, int(max(float(entry["bbox"][2]) for entry in positioned)))

    for entry in positioned:
        x0, y0, x1, y1 = [float(value) for value in entry["bbox"][:4]]
        entry["cy"] = (y0 + y1) / 2
        entry["h"] = max(1.0, y1 - y0)
    median_height = median_float([float(entry["h"]) for entry in positioned])
    row_threshold = max(10.0, median_height * 0.65)
    rows: list[dict[str, Any]] = []
    for entry in sorted(positioned, key=lambda item: (float(item["cy"]), float(item["bbox"][0]))):
        for row in rows:
            if abs(float(entry["cy"]) - float(row["cy"])) <= row_threshold:
                row["entries"].append(entry)
                row["cy"] = sum(float(item["cy"]) for item in row["entries"]) / len(row["entries"])
                row["y0"] = min(float(item["bbox"][1]) for item in row["entries"])
                row["y1"] = max(float(item["bbox"][3]) for item in row["entries"])
                break
        else:
            rows.append(
                {
                    "cy": entry["cy"],
                    "y0": float(entry["bbox"][1]),
                    "y1": float(entry["bbox"][3]),
                    "entries": [entry],
                }
            )
    rows.sort(key=lambda row: float(row["cy"]))
    rendered: list[str] = []
    previous_y1: float | None = None
    for row in rows:
        if previous_y1 is not None and float(row["y0"]) - previous_y1 > max(34.0, median_height * 1.7):
            rendered.append("")
        line = ""
        for entry in sorted(row["entries"], key=lambda item: float(item["bbox"][0])):
            text = " ".join(text_value(entry.get("text")).split())
            if not text:
                continue
            column = max(0, min(chars - 1, int(float(entry["bbox"][0]) / width_px * chars)))
            if len(line) < column:
                line += " " * (column - len(line))
            elif line and not line.endswith(" "):
                line += " | "
            line += text
        rendered.append(line.rstrip())
        previous_y1 = float(row["y1"])
    return "\n".join(rendered).strip()


def unique_ocr_lines(entries: list[dict[str, Any]]) -> tuple[list[str], list[float]]:
    lines: list[str] = []
    scores: list[float] = []
    seen: set[str] = set()
    for entry in entries:
        line = " ".join(text_value(entry.get("text")).split())
        if not line:
            continue
        if not any(char.isalnum() for char in line):
            continue
        key = line.casefold()
        if key in seen:
            continue
        seen.add(key)
        lines.append(line)
        score = float_or_none(entry.get("score"))
        if score is not None:
            scores.append(score)
    return lines, scores


def paddleocr_slide_text_result(deck: str, slide: int, image_path: Path, output_dir: Path) -> dict[str, Any]:
    ocr_image_path = paddleocr_compatible_image_path(image_path, output_dir)
    engine = paddleocr_engine()
    result = engine.predict(str(ocr_image_path))
    entries = extract_paddleocr_entries(result)
    entries = repair_paddleocr_dash_entries(entries, ocr_image_path)
    lines, scores = unique_ocr_lines(entries)
    phrases = [line for line in lines if len(line.split()) <= 12]
    layout_text = render_ocr_layout(entries, ocr_image_path)
    avg_score = round(sum(scores) / len(scores), 4) if scores else None
    row = normalize_slide_text_result(
        {
            "slide": slide,
            "sentences": lines,
            "phrases": phrases,
            "raw_text": layout_text or "\n".join(lines),
            "plain_text": "\n".join(lines),
            "layout_text": layout_text,
            "ocr_backend": "paddle",
            "ocr_image_path": str(ocr_image_path),
            "ocr_confidence_avg": avg_score,
        },
        slide,
    )
    write_json(
        output_dir / "ocr_raw" / f"{raw_name(deck)}_slide_{slide:02d}_paddleocr.json",
        {
            "deck": deck,
            "slide": slide,
            "source_image": str(image_path),
            "ocr_image": str(ocr_image_path),
            "text_entries": entries,
            "text_count": len(lines),
            "confidence_avg": avg_score,
        },
    )
    return row


def write_deck_artifact(output_dir: Path, deck: str, suffix: str, value: Any) -> Path:
    path = output_dir / "deck_reviews" / f"{raw_name(deck)}_{suffix}"
    write_json(path, value)
    return path


def ensure_review_markdown_placeholder(output_dir: Path, task_id: str) -> None:
    path = output_dir / "content_grading_comments.md"
    if path.exists():
        return
    path.write_text(
        "# Content Grading Comments\n\n"
        f"Review is running for task `{task_id}`.\n\n"
        "This file refreshes as slide text, deck comments, and ranking finish.\n",
        encoding="utf-8",
    )


def issue_severity(comment: dict[str, Any]) -> float:
    issue_type = text_value(comment.get("issue_type")).lower()
    confidence = text_value(comment.get("confidence")).lower()
    type_weight = {
        "instruction_following": 2.4,
        "unclear_content": 2.0,
        "repetition": 1.5,
        "ai_slop": 1.3,
        "generic_claim": 1.0,
        "vague_phrase": 0.9,
    }.get(issue_type, 1.0)
    confidence_weight = {"high": 1.0, "medium": 0.75, "low": 0.5}.get(confidence, 0.75)
    return type_weight * confidence_weight


def build_deck_quality_ranking(value: dict[str, Any]) -> list[dict[str, Any]]:
    decks = value.get("decks") if isinstance(value.get("decks"), dict) else {}
    ranking: list[dict[str, Any]] = []
    for deck, deck_data in sorted(decks.items()):
        comments = deck_data.get("comments") if isinstance(deck_data, dict) else []
        comments = comments if isinstance(comments, list) else []
        issue_comments = [comment for comment in comments if isinstance(comment, dict)]
        penalty = sum(issue_severity(comment) for comment in issue_comments)
        high_confidence = sum(1 for comment in issue_comments if text_value(comment.get("confidence")).lower() == "high")
        severe = sum(
            1
            for comment in issue_comments
            if text_value(comment.get("issue_type")).lower() in {"instruction_following", "unclear_content"}
        )
        approximate_score = max(1.0, min(7.0, 7.0 - min(4.5, penalty / 2.25)))
        top_issue = issue_comments[0] if issue_comments else {}
        top_slide = text_value(top_issue.get("slide"))
        top_quote = text_value(top_issue.get("quote"))
        reason = "No clear content grading issues were generated."
        if top_issue:
            reason = (
                f"{len(issue_comments)} generated issue(s), including "
                f"{text_value(top_issue.get('issue_type')) or 'an issue'}"
            )
            if top_slide:
                reason += f" on slide {top_slide}"
            if top_quote:
                reason += f" around \"{top_quote}\""
            reason += "."
        ranking.append(
            {
                "deck": deck,
                "approximate_score": round(approximate_score, 1),
                "issue_count": len(issue_comments),
                "high_confidence_issue_count": high_confidence,
                "severe_issue_count": severe,
                "reason": reason,
            }
        )
    ranking.sort(
        key=lambda row: (
            -float(row["approximate_score"]),
            int(row["severe_issue_count"]),
            int(row["high_confidence_issue_count"]),
            int(row["issue_count"]),
            str(row["deck"]),
        )
    )
    for index, row in enumerate(ranking, start=1):
        row["rank"] = index
    return ranking


def attach_quality_ranking(value: dict[str, Any]) -> dict[str, Any]:
    summary = value.setdefault("summary", {})
    if isinstance(summary, dict):
        ranking = summary.get("deck_quality_ranking")
        if not isinstance(ranking, list) or not ranking:
            ranking = build_deck_quality_ranking(value)
        summary["deck_quality_ranking"] = ranking
        if ranking:
            summary["strongest_candidate"] = ranking[0]["deck"]
            summary["weakest_candidate"] = ranking[-1]["deck"]
    return value


def normalize_deck_quality_ranking(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, dict):
        return []
    ranking = value.get("deck_quality_ranking") or value.get("ranking") or value.get("decks")
    if not isinstance(ranking, list):
        return []
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(ranking, start=1):
        if not isinstance(row, dict):
            continue
        deck = text_value(row.get("deck") or row.get("candidate"))
        if not deck:
            continue
        score = row.get("approximate_score", row.get("score"))
        try:
            approximate_score = round(float(score), 1)
        except (TypeError, ValueError):
            approximate_score = None
        normalized.append(
            {
                "rank": int(row.get("rank") or index),
                "deck": deck,
                "approximate_score": approximate_score,
                "reason": text_value(row.get("reason") or row.get("rationale")),
                "issue_count": row.get("issue_count"),
                "severe_issue_count": row.get("severe_issue_count"),
                "high_confidence_issue_count": row.get("high_confidence_issue_count"),
            }
        )
    normalized.sort(key=lambda row: int(row.get("rank") or 999))
    for index, row in enumerate(normalized, start=1):
        row["rank"] = index
    return normalized


def compact_deck_texts_for_codex(deck_texts: Any) -> dict[str, Any]:
    if not isinstance(deck_texts, dict):
        return {"slides": []}
    slides = deck_texts.get("slides")
    if not isinstance(slides, list):
        return {"slides": []}
    compact_slides: list[dict[str, Any]] = []
    for row in sorted((item for item in slides if isinstance(item, dict)), key=lambda item: int(item.get("slide") or 0)):
        text = text_value(row.get("layout_text") or row.get("raw_text") or row.get("plain_text"))
        if not text:
            sentences = row.get("sentences")
            if isinstance(sentences, list):
                text = "\n".join(text_value(item) for item in sentences if text_value(item))
        compact_slides.append({"slide": int(row.get("slide") or 0), "text": text})
    return {"slides": compact_slides}


def compact_slide_texts_for_codex(slide_texts: dict[str, Any]) -> dict[str, Any]:
    decks = slide_texts.get("decks") if isinstance(slide_texts, dict) else {}
    if not isinstance(decks, dict):
        return {"decks": {}}
    return {"decks": {deck: compact_deck_texts_for_codex(deck_texts) for deck, deck_texts in decks.items()}}


def attach_codex_quality_ranking(
    value: dict[str, Any],
    slide_texts: dict[str, Any],
    task_prompt: str,
    output_dir: Path,
    model: str,
) -> dict[str, Any]:
    prompt = (
        "You are ranking multiple candidate slide decks for Content Grading language quality. "
        "Use only the extracted slide text, generated comments, and task prompt below. Do not inspect files, run commands, or use outside knowledge.\n"
        "Rank decks from strongest language quality to weakest language quality. "
        "Consider severity more than raw issue count: broken/truncated/incomplete text and hard-to-understand wording are worse than mild marketing phrasing. "
        "Reward decks that are clear, natural, specific, and easy to follow. Penalize AI-slop filler, vague product language, within-slide repetition, awkward wording, and broken text. "
        + CONTENT_GRADING_REVIEW_RULES
        + " "
        "Return JSON only with shape "
        "{\"deck_quality_ranking\":[{\"rank\":1,\"deck\":\"deck_01\",\"approximate_score\":6.0,"
        "\"reason\":\"brief reason grounded in slide numbers or comment evidence\"}]}.\n\n"
        "Task prompt:\n"
        + (task_prompt or "(not found)")
        + "\n\nExtracted slide text by deck:\n"
        + json.dumps(compact_slide_texts_for_codex(slide_texts), ensure_ascii=False)
        + "\n\nGenerated comments by deck:\n"
        + json.dumps(value, ensure_ascii=False)
    )
    parsed = run_codex_json(prompt, output_dir, "deck_quality_ranking", model)
    ranking = normalize_deck_quality_ranking(parsed)
    if not ranking:
        return attach_quality_ranking(value)
    summary = value.setdefault("summary", {})
    if isinstance(summary, dict):
        summary["deck_quality_ranking"] = ranking
        summary["strongest_candidate"] = ranking[0]["deck"]
        summary["weakest_candidate"] = ranking[-1]["deck"]
        notes = summary.setdefault("notes", [])
        if isinstance(notes, list):
            notes.append("Deck quality ranking was generated in a final cross-deck pass.")
    return value


def codex_executable() -> str | None:
    candidates = ["codex.cmd", "codex.exe", "codex"] if os.name == "nt" else ["codex"]
    for candidate in candidates:
        path = shutil.which(candidate)
        if path:
            return path
    return None


def resolve_llm_backend(requested: str, api_key: str | None) -> str:
    backend = (requested or "auto").strip().lower()
    if backend == "none":
        return "none"
    if backend == "api":
        if not api_key:
            raise SystemExit("--llm-backend api requires OPENAI_API_KEY.")
        return "api"
    if backend == "codex":
        if not codex_executable():
            raise SystemExit("--llm-backend codex requires the codex CLI in PATH.")
        return "codex"
    if backend == "auto":
        if api_key:
            return "api"
        if codex_executable():
            return "codex"
        return "none"
    raise SystemExit("--llm-backend must be one of: auto, api, codex, none.")


def raw_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value).strip("_") or "codex"


def run_codex_json(
    prompt: str,
    output_dir: Path,
    output_name: str,
    model: str,
    images: list[Path] | None = None,
) -> Any:
    executable = codex_executable()
    if not executable:
        raise SystemExit("codex CLI was not found in PATH.")

    raw_dir = output_dir / "llm_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    name = raw_name(output_name)
    last_message_path = raw_dir / f"{name}_codex_last_message.txt"
    stdout_path = raw_dir / f"{name}_codex_stdout.txt"
    stderr_path = raw_dir / f"{name}_codex_stderr.txt"

    cmd = [
        executable,
        "--ask-for-approval",
        "never",
        "exec",
        "--ephemeral",
        "--sandbox",
        "read-only",
        "--color",
        "never",
        "--cd",
        str(Path.cwd()),
        "--output-last-message",
        str(last_message_path),
        "-m",
        model,
    ]
    for image in images or []:
        cmd.extend(["--image", str(image.resolve())])
    cmd.append("-")

    proc = subprocess.run(
        cmd,
        input=prompt,
        cwd=Path.cwd(),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
        timeout=CODEX_EXEC_TIMEOUT_SECONDS,
    )
    stdout_path.write_text(proc.stdout or "", encoding="utf-8")
    stderr_path.write_text(proc.stderr or "", encoding="utf-8")
    if proc.returncode != 0:
        preview = (proc.stderr or proc.stdout).strip()[:1200]
        raise SystemExit(f"codex exec failed with exit {proc.returncode}: {preview}")

    text = last_message_path.read_text(encoding="utf-8", errors="replace") if last_message_path.exists() else proc.stdout
    try:
        return parse_json_text(text)
    except json.JSONDecodeError:
        return {"raw_model_output": text.strip()}


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
                    "Extract the visible text from these slide screenshots for content grading. "
                    "Return JSON only with shape "
                    "{\"slides\":[{\"slide\":1,\"sentences\":[...],\"phrases\":[...],\"raw_text\":\"...\"}]}. "
                    "Keep short labels, slogans, captions, and repeated phrases. "
                    + OCR_DASH_PUNCTUATION_PROMPT
                    + "Use the slide labels provided before each image. Do not evaluate quality."
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


def run_slide_text_codex_job(deck: str, slide: int, image_path: Path, output_dir: Path, model: str) -> tuple[str, int, dict[str, Any]]:
    prompt = (
        "You are extracting visible text from one slide screenshot for content grading.\n"
        "Use only the attached image. Do not inspect files, run commands, or use outside knowledge.\n"
        f"The attached image is {deck} slide {slide}.\n\n"
        "Return JSON only with shape "
        "{\"slide\":1,\"sentences\":[...],\"phrases\":[...],\"raw_text\":\"...\"}.\n"
        "Keep short labels, slogans, captions, repeated phrases, and incomplete phrases if they are visible. "
        + OCR_DASH_PUNCTUATION_PROMPT
        + "Do not evaluate quality in this step."
    )
    parsed = run_codex_json(prompt, output_dir, f"{deck}_slide_{slide:02d}_text", model, images=[image_path])
    return deck, slide, normalize_slide_text_result(parsed, slide)


def run_slide_text_paddleocr_job(deck: str, slide: int, image_path: Path, output_dir: Path) -> tuple[str, int, dict[str, Any]]:
    return deck, slide, paddleocr_slide_text_result(deck, slide, image_path, output_dir)


def run_slide_text_job(
    deck: str,
    slide: int,
    image_path: Path,
    output_dir: Path,
    model: str,
    ocr_backend: str,
) -> tuple[str, int, dict[str, Any]]:
    if ocr_backend == "paddle":
        return run_slide_text_paddleocr_job(deck, slide, image_path, output_dir)
    if ocr_backend == "codex":
        return run_slide_text_codex_job(deck, slide, image_path, output_dir, model)
    raise SystemExit(f"OCR backend {ocr_backend!r} is not supported in the Codex streaming pipeline.")


def extract_slide_texts_paddleocr(
    manifest: dict[str, Any],
    output_dir: Path,
    workers: int = 1,
    status_callback: StatusCallback | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    result = {"decks": {}}
    decks = manifest["decks"]
    deck_slide_totals = {deck: len(deck_data["slides"]) for deck, deck_data in decks.items()}
    jobs: list[tuple[str, int, Path]] = []
    for deck, deck_data in decks.items():
        result["decks"][deck] = {"slides": []}
        for slide_key, slide_data in deck_data["slides"].items():
            jobs.append((deck, int(slide_key), Path(str(slide_data["path"]))))

    total = len(jobs)
    completed = 0
    deck_completed = {deck: 0 for deck in decks}
    max_workers = min(positive_int(workers, 1), total or 1)

    def record(deck: str, slide: int, row: dict[str, Any]) -> None:
        nonlocal completed
        result["decks"][deck]["slides"].append(row)
        result["decks"][deck]["slides"].sort(key=lambda item: int(item.get("slide") or 0))
        completed += 1
        deck_completed[deck] += 1
        write_json(output_dir / "slide_text_by_deck.partial.json", result)
        if deck_completed[deck] == deck_slide_totals[deck]:
            write_json(output_dir / "deck_reviews" / f"{raw_name(deck)}_slide_text.json", result["decks"][deck])
        emit_status(
            output_dir,
            status_callback,
            state="extracting_slide_text_with_paddleocr",
            task_id=task_id,
            stage="slide_text",
            completed=completed,
            total=total,
            current_deck=deck,
            current_slide=slide,
            ocr_backend="paddle",
        )

    if max_workers == 1:
        for deck, slide, image_path in jobs:
            record(deck, slide, paddleocr_slide_text_result(deck, slide, image_path, output_dir))
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(run_slide_text_paddleocr_job, deck, slide, image_path, output_dir): (deck, slide)
                for deck, slide, image_path in jobs
            }
            for future in as_completed(future_map):
                deck, slide = future_map[future]
                _, _, row = future.result()
                record(deck, slide, row)

    write_json(output_dir / "slide_text_by_deck.json", result)
    return result


def extract_slide_texts_codex(
    manifest: dict[str, Any],
    output_dir: Path,
    model: str,
    workers: int = 1,
    status_callback: StatusCallback | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    result = {"decks": {}}
    decks = manifest["decks"]
    deck_slide_totals = {deck: len(deck_data["slides"]) for deck, deck_data in decks.items()}
    jobs: list[tuple[str, int, Path]] = []
    for deck, deck_data in decks.items():
        result["decks"][deck] = {"slides": []}
        for slide_key, slide_data in deck_data["slides"].items():
            jobs.append((deck, int(slide_key), Path(str(slide_data["path"]))))

    def run_job(deck: str, slide: int, image_path: Path) -> tuple[str, int, dict[str, Any]]:
        return run_slide_text_codex_job(deck, slide, image_path, output_dir, model)

    total = len(jobs)
    completed = 0
    deck_completed = {deck: 0 for deck in decks}
    max_workers = min(positive_int(workers, 1), total or 1)

    def record(deck: str, slide: int, row: dict[str, Any]) -> None:
        nonlocal completed
        result["decks"][deck]["slides"].append(row)
        result["decks"][deck]["slides"].sort(key=lambda item: int(item.get("slide") or 0))
        completed += 1
        deck_completed[deck] += 1
        write_json(output_dir / "slide_text_by_deck.partial.json", result)
        if deck_completed[deck] == deck_slide_totals[deck]:
            write_json(output_dir / "deck_reviews" / f"{raw_name(deck)}_slide_text.json", result["decks"][deck])
        emit_status(
            output_dir,
            status_callback,
            state="extracting_slide_text_with_codex",
            task_id=task_id,
            stage="slide_text",
            completed=completed,
            total=total,
            current_deck=deck,
            current_slide=slide,
            codex_workers=max_workers,
            ocr_backend="codex",
        )

    if max_workers == 1:
        for deck, slide, image_path in jobs:
            record(deck, slide, run_job(deck, slide, image_path)[2])
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(run_job, deck, slide, image_path): (deck, slide)
                for deck, slide, image_path in jobs
            }
            for future in as_completed(future_map):
                deck, slide = future_map[future]
                _, _, row = future.result()
                record(deck, slide, row)

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
        + CONTENT_GRADING_REVIEW_RULES
        + " "
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


def run_deck_issue_candidates_codex_job(
    deck: str,
    deck_texts: Any,
    task_prompt: str,
    output_dir: Path,
    model: str,
) -> tuple[str, dict[str, Any]]:
    prompt = (
        "You are assisting with Content Grading preparation for one deck only. "
        "Use only the extracted slide text and task prompt below. Do not inspect files, run commands, or use outside knowledge.\n"
        f"Deck under review: {deck}.\n"
        "Find potential content issues and strengths grounded in this deck's slide text. "
        "Focus on AI content slop, instruction following, relevance, specificity, filler, placeholders, "
        "odd wording, self-referential text, and understandability. Do not judge visual aesthetics unless it affects understanding. "
        + CONTENT_GRADING_REVIEW_RULES
        + " "
        "Return JSON only with shape "
        "{\"issue_candidates\":[{\"slide\":1,\"type\":\"ai_slop|instruction_following|understandability|strength\","
        "\"evidence\":\"...\",\"why_it_matters\":\"...\"}]}.\n\n"
        "Task prompt:\n"
        + (task_prompt or "(not found)")
        + "\n\nExtracted slide text for this deck:\n"
        + json.dumps(deck_texts, ensure_ascii=False)
    )
    parsed = run_codex_json(prompt, output_dir, f"{deck}_issue_candidates", model)
    return deck, deck_payload(parsed, deck)


def generate_issue_candidates_codex(
    slide_texts: dict[str, Any],
    task_prompt: str,
    output_dir: Path,
    model: str,
    workers: int = 1,
    status_callback: StatusCallback | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    result = {"decks": {}}
    deck_items = list((slide_texts.get("decks") or {}).items())

    def run_job(deck: str, deck_texts: Any) -> tuple[str, dict[str, Any]]:
        return run_deck_issue_candidates_codex_job(deck, deck_texts, task_prompt, output_dir, model)

    total = len(deck_items)
    completed = 0
    max_workers = min(positive_int(workers, 1), total or 1)

    def record(deck: str, payload: dict[str, Any]) -> None:
        nonlocal completed
        result["decks"][deck] = payload
        completed += 1
        write_json(output_dir / "content_issue_candidates.partial.json", result)
        write_json(output_dir / "deck_reviews" / f"{raw_name(deck)}_issue_candidates.json", payload)
        emit_status(
            output_dir,
            status_callback,
            state="finding_issue_candidates_with_codex",
            task_id=task_id,
            stage="issue_candidates",
            completed=completed,
            total=total,
            current_deck=deck,
            codex_workers=max_workers,
        )

    if max_workers == 1:
        for deck, deck_texts in deck_items:
            record(*run_job(deck, deck_texts))
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(run_job, deck, deck_texts): deck for deck, deck_texts in deck_items}
            for future in as_completed(future_map):
                record(*future.result())

    write_json(output_dir / "content_issue_candidates.json", result)
    return result


def render_content_grading_markdown(value: Any) -> str:
    if not isinstance(value, dict):
        return "# Content Grading Comments\n\n" + text_value(value) + "\n"

    if isinstance(value.get("raw_model_output"), str):
        return "# Content Grading Comments\n\n" + value["raw_model_output"].strip() + "\n"

    lines = ["# Content Grading Comments", ""]
    summary = value.get("summary")
    if isinstance(summary, dict):
        strongest = text_value(summary.get("strongest_candidate"))
        weakest = text_value(summary.get("weakest_candidate"))
        if strongest or weakest:
            lines.append("## Summary")
            if strongest:
                lines.append(f"- Strongest candidate: {markdown_bullet(strongest)}")
            if weakest:
                lines.append(f"- Weakest candidate: {markdown_bullet(weakest)}")
            lines.append("")
        ranking = summary.get("deck_quality_ranking")
        if isinstance(ranking, list) and ranking:
            lines.append("## Deck Quality Ranking")
            for row in ranking:
                if not isinstance(row, dict):
                    continue
                rank = text_value(row.get("rank")) or "?"
                deck = text_value(row.get("deck")) or "unknown"
                score = text_value(row.get("approximate_score"))
                reason = markdown_bullet(text_value(row.get("reason")))
                issue_count = text_value(row.get("issue_count"))
                severe_count = text_value(row.get("severe_issue_count"))
                label = f"{rank}. {deck}"
                if score:
                    label += f" - approx {score}/7"
                details = []
                if issue_count:
                    details.append(f"{issue_count} issue(s)")
                if severe_count:
                    details.append(f"{severe_count} severe/readability issue(s)")
                if details:
                    label += f" ({', '.join(details)})"
                lines.append(label)
                if reason:
                    lines.append(f"   - {reason}")
            lines.append("")
        notes = summary.get("notes")
        if isinstance(notes, list) and notes:
            lines.append("## Cross-Deck Notes")
            for note in notes:
                text = markdown_bullet(text_value(note))
                if text:
                    lines.append(f"- {text}")
            lines.append("")

    decks = value.get("decks")
    if not isinstance(decks, dict):
        return "\n".join(lines).rstrip() + "\n"

    for deck, deck_data in sorted(decks.items()):
        lines.append(f"## {deck}")
        if isinstance(deck_data, dict):
            overall = text_value(deck_data.get("overall_comment") or deck_data.get("overall"))
            if overall:
                lines.append(f"- Overall: {markdown_bullet(overall)}")
                lines.append("")
        comments = deck_data.get("comments") if isinstance(deck_data, dict) else None
        if not isinstance(comments, list) or not comments:
            lines.append("- No clear content grading comments.")
            lines.append("")
            continue
        for index, comment in enumerate(comments, start=1):
            if not isinstance(comment, dict):
                continue
            slide = text_value(comment.get("slide")) or "?"
            issue_type = text_value(comment.get("issue_type")) or "issue"
            quote = text_value(comment.get("quote"))
            critique = text_value(comment.get("critique"))
            suggestion = text_value(comment.get("suggestion"))
            comment_draft = text_value(comment.get("comment_draft"))
            confidence = text_value(comment.get("confidence"))

            lines.append(f"### {index}. Slide {slide} - {issue_type}")
            if quote:
                lines.append(f"- Quote/phrase: {markdown_bullet(quote)}")
            if critique:
                lines.append(f"- Critique: {markdown_bullet(critique)}")
            if suggestion:
                lines.append(f"- Suggestion: {markdown_bullet(suggestion)}")
            if comment_draft:
                lines.append(f"- Comment draft: {markdown_bullet(comment_draft)}")
            if confidence:
                lines.append(f"- Confidence: {markdown_bullet(confidence)}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def generate_content_grading_comments(
    slide_texts: dict[str, Any],
    issue_candidates: dict[str, Any],
    task_prompt: str,
    output_dir: Path,
    api_key: str,
    model: str,
) -> dict[str, Any]:
    prompt = (
        "You are helping a human reviewer do Content Grading for slide decks. "
        "Use only the extracted slide text and the provided task prompt; do not fact-check with outside knowledge. "
        "Find sentences or short phrases that are empty, generic, AI-slop-like, filler, vague, repetitive within a single slide, "
        "self-referential, or weakly connected to the requested task. "
        + CONTENT_GRADING_REVIEW_RULES
        + " "
        "For each meaningful issue, produce a concise critique and a practical improvement suggestion. "
        "Only include comments worth a human reviewer's attention. Avoid nitpicks based on OCR noise. "
        "If a deck has no clear content issue, return an empty comments array for that deck. "
        "Preserve quote punctuation from the extracted text when possible; do not normalize clear en or em dashes to '-'. "
        "Return JSON only with shape "
        "{\"summary\":{\"strongest_candidate\":\"deck_01\",\"weakest_candidate\":\"deck_02\",\"notes\":[...]},"
        "\"decks\":{\"deck_01\":{\"comments\":[{\"slide\":1,\"issue_type\":\"ai_slop|vague_phrase|generic_claim|"
        "repetition|instruction_following|unclear_content\",\"quote\":\"exact sentence or phrase\","
        "\"critique\":\"why this is weak\",\"suggestion\":\"how to improve it\","
        "\"comment_draft\":\"reviewer-facing comment in 1-2 sentences\",\"confidence\":\"low|medium|high\"}]}}}."
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
                        {"type": "input_text", "text": "Task prompt:\n" + (task_prompt or "(not found)")},
                        {"type": "input_text", "text": "Extracted slide text:\n" + json.dumps(slide_texts, ensure_ascii=False)},
                        {
                            "type": "input_text",
                            "text": "Earlier issue candidates:\n" + json.dumps(issue_candidates, ensure_ascii=False),
                        },
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
    if isinstance(parsed, dict):
        parsed = attach_quality_ranking(parsed)
    write_json(output_dir / "content_grading_comments.json", parsed)
    (output_dir / "content_grading_comments.md").write_text(render_content_grading_markdown(parsed), encoding="utf-8")
    write_json(output_dir / "llm_raw" / "content_grading_comments_response.json", response)
    return parsed


def initial_content_grading_result() -> dict[str, Any]:
    return {
        "summary": {
            "notes": [
                "Slide-level drafts stream as soon as each slide OCR finishes; deck-level comments replace them after each deck is merged.",
                "Deck quality ranking is inferred from generated comment severity and should be treated as a reviewer aid.",
            ]
        },
        "decks": {},
    }


def run_slide_content_grading_comment_codex_job(
    deck: str,
    slide_row: dict[str, Any],
    task_prompt: str,
    output_dir: Path,
    model: str,
) -> tuple[str, int, dict[str, Any]]:
    slide = int(slide_row.get("slide") or 0)
    prompt = (
        "You are helping a human reviewer do Content Grading for one slide only. "
        "Use only this slide's extracted text and the task prompt below. Do not inspect files, run commands, or use outside knowledge.\n"
        f"Deck under review: {deck}. Slide under review: {slide}.\n"
        "Find sentences or short phrases that are empty, generic, AI-slop-like, filler, vague, repetitive within this slide, "
        "self-referential, or hard to understand on this individual slide. "
        + CONTENT_GRADING_REVIEW_RULES
        + " "
        "Only include comments worth a human reviewer's attention. Avoid nitpicks based on OCR noise. "
        "If this slide has no clear content issue, return an empty comments array. "
        "Preserve quote punctuation from the extracted text when possible; do not normalize clear en or em dashes to '-'. "
        "Return JSON only with shape "
        "{\"comments\":[{\"slide\":1,\"issue_type\":\"ai_slop|vague_phrase|generic_claim|"
        "repetition|instruction_following|unclear_content\",\"quote\":\"exact sentence or phrase\","
        "\"critique\":\"why this is weak\",\"suggestion\":\"how to improve it\","
        "\"comment_draft\":\"reviewer-facing comment in 1-2 sentences\",\"confidence\":\"low|medium|high\"}]}.\n\n"
        "Task prompt:\n"
        + (task_prompt or "(not found)")
        + "\n\nExtracted slide text:\n"
        + json.dumps(slide_row, ensure_ascii=False)
    )
    parsed = run_codex_json(prompt, output_dir, f"{deck}_slide_{slide:02d}_content_grading_comments", model)
    payload = deck_payload(parsed, deck)
    comments = payload.get("comments") if isinstance(payload, dict) else None
    if isinstance(comments, list):
        for comment in comments:
            if isinstance(comment, dict):
                comment["slide"] = int(comment.get("slide") or slide)
    return deck, slide, payload


def run_deck_content_grading_comments_codex_job(
    deck: str,
    deck_texts: Any,
    deck_issues: Any,
    task_prompt: str,
    output_dir: Path,
    model: str,
) -> tuple[str, dict[str, Any]]:
    prompt = (
        "You are helping a human reviewer do Content Grading for one slide deck. "
        "Use only this deck's extracted slide text, this deck's earlier issue candidates, and the task prompt below. "
        "Do not inspect files, run commands, or use outside knowledge.\n"
        f"Deck under review: {deck}.\n"
        "Find sentences or short phrases that are empty, generic, AI-slop-like, filler, vague, repetitive within a single slide, "
        "self-referential, or weakly connected to the requested task. "
        + CONTENT_GRADING_REVIEW_RULES
        + " "
        "For each meaningful issue, produce a concise critique and a practical improvement suggestion. "
        "Only include comments worth a human reviewer's attention. Avoid nitpicks based on OCR noise. "
        "If this deck has no clear content issue, return an empty comments array. "
        "Preserve quote punctuation from the extracted text when possible; do not normalize clear en or em dashes to '-'. "
        "Return JSON only with shape "
        "{\"comments\":[{\"slide\":1,\"issue_type\":\"ai_slop|vague_phrase|generic_claim|"
        "repetition|instruction_following|unclear_content\",\"quote\":\"exact sentence or phrase\","
        "\"critique\":\"why this is weak\",\"suggestion\":\"how to improve it\","
        "\"comment_draft\":\"reviewer-facing comment in 1-2 sentences\",\"confidence\":\"low|medium|high\"}]}.\n\n"
        "Task prompt:\n"
        + (task_prompt or "(not found)")
        + "\n\nExtracted slide text for this deck:\n"
        + json.dumps(compact_deck_texts_for_codex(deck_texts), ensure_ascii=False)
        + "\n\nEarlier issue candidates for this deck:\n"
        + json.dumps(deck_issues, ensure_ascii=False)
    )
    parsed = run_codex_json(prompt, output_dir, f"{deck}_content_grading_comments", model)
    return deck, deck_payload(parsed, deck)


def run_deck_content_grading_comments_codex_fast_job(
    deck: str,
    deck_texts: Any,
    task_prompt: str,
    output_dir: Path,
    model: str,
    comments_per_deck: int = DEFAULT_COMMENTS_PER_DECK,
) -> tuple[str, dict[str, Any]]:
    comment_count = max(1, int(comments_per_deck))
    prompt = (
        "You are helping a human reviewer do Content Grading for one slide deck. "
        "Use only this deck's extracted OCR text and the task prompt below. Do not inspect files, run commands, or use outside knowledge.\n"
        f"Deck under review: {deck}.\n\n"
        f"Write one overall assessment and {comment_count} clear, high-signal content comments. "
        "Focus on sentences or short phrases that are empty, generic, AI-slop-like, filler, vague, repetitive within a single slide, "
        "self-referential, weakly connected to the requested task, or genuinely unclear. "
        + CONTENT_GRADING_REVIEW_RULES
        + " "
        "Prefer comments that a reviewer can paste or adapt. Avoid tiny OCR-only noise unless the deck text itself would visibly read broken to a user. "
        "Every comment must include the slide number, the exact quote/phrase, a critique, an improvement suggestion, and a reviewer-facing comment draft. "
        "Preserve quote punctuation from the extracted text when possible; do not normalize clear en or em dashes to '-'. Return JSON only with shape "
        "{\"overall_comment\":\"one concise overall assessment\","
        "\"comments\":[{\"slide\":1,\"issue_type\":\"ai_slop|vague_phrase|generic_claim|"
        "repetition|instruction_following|unclear_content\",\"quote\":\"exact sentence or phrase\","
        "\"critique\":\"why this is weak\",\"suggestion\":\"how to improve it\","
        "\"comment_draft\":\"reviewer-facing comment in 1-2 sentences\",\"confidence\":\"low|medium|high\"}]}. "
        f"Return exactly {comment_count} comments unless there are fewer than {comment_count} meaningful content issues.\n\n"
        "Task prompt:\n"
        + (task_prompt or "(not found)")
        + "\n\nStructured extracted slide text for this deck:\n"
        + json.dumps(compact_deck_texts_for_codex(deck_texts), ensure_ascii=False)
    )
    parsed = run_codex_json(prompt, output_dir, f"{deck}_content_grading_comments_fast", model)
    return deck, deck_payload(parsed, deck)


def write_content_grading_current(output_dir: Path, result: dict[str, Any], partial: bool) -> None:
    current = attach_quality_ranking(json.loads(json.dumps(result, ensure_ascii=False)))
    json_name = "content_grading_comments.partial.json" if partial else "content_grading_comments.json"
    write_json(output_dir / json_name, current)
    (output_dir / "content_grading_comments.md").write_text(render_content_grading_markdown(current), encoding="utf-8")


def write_deck_comment_outputs(output_dir: Path, deck: str, payload: dict[str, Any]) -> None:
    deck_result = {
        "summary": {"notes": [f"Generated for {deck} as soon as this deck completed."]},
        "decks": {deck: payload},
    }
    write_json(output_dir / "deck_reviews" / f"{raw_name(deck)}_content_grading_comments.json", deck_result)
    (output_dir / "deck_reviews" / f"{raw_name(deck)}_content_grading_comments.md").write_text(
        render_content_grading_markdown(deck_result),
        encoding="utf-8",
    )


def write_slide_comment_outputs(output_dir: Path, deck: str, slide: int, payload: dict[str, Any]) -> None:
    slide_result = {
        "summary": {"notes": [f"Draft generated for {deck} slide {slide} as soon as slide OCR completed."]},
        "decks": {deck: payload},
    }
    write_json(output_dir / "deck_reviews" / f"{raw_name(deck)}_slide_{slide:02d}_content_grading_comments.json", slide_result)
    (output_dir / "deck_reviews" / f"{raw_name(deck)}_slide_{slide:02d}_content_grading_comments.md").write_text(
        render_content_grading_markdown(slide_result),
        encoding="utf-8",
    )


def merge_slide_comment_draft(result: dict[str, Any], deck: str, payload: dict[str, Any]) -> None:
    deck_bucket = result.setdefault("decks", {}).setdefault(deck, {"comments": []})
    comments = payload.get("comments") if isinstance(payload, dict) else []
    if isinstance(comments, list):
        existing = deck_bucket.setdefault("comments", [])
        if isinstance(existing, list):
            existing.extend(comment for comment in comments if isinstance(comment, dict))


def generate_content_grading_comments_codex(
    slide_texts: dict[str, Any],
    issue_candidates: dict[str, Any],
    task_prompt: str,
    output_dir: Path,
    model: str,
    workers: int = 1,
    status_callback: StatusCallback | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    result = initial_content_grading_result()
    decks = slide_texts.get("decks") or {}
    issue_decks = issue_candidates.get("decks") if isinstance(issue_candidates.get("decks"), dict) else {}
    deck_items = list(decks.items())

    def run_job(deck: str, deck_texts: Any) -> tuple[str, dict[str, Any]]:
        deck_issues = issue_decks.get(deck, {}) if isinstance(issue_decks, dict) else {}
        return run_deck_content_grading_comments_codex_job(deck, deck_texts, deck_issues, task_prompt, output_dir, model)

    total = len(deck_items)
    completed = 0
    max_workers = min(positive_int(workers, 1), total or 1)

    def write_current(partial: bool) -> None:
        write_content_grading_current(output_dir, result, partial)

    def record(deck: str, payload: dict[str, Any]) -> None:
        nonlocal completed
        result["decks"][deck] = payload
        completed += 1
        write_deck_comment_outputs(output_dir, deck, payload)
        write_current(partial=completed < total)
        emit_status(
            output_dir,
            status_callback,
            state="writing_content_grading_comments_with_codex",
            task_id=task_id,
            stage="content_grading_comments",
            completed=completed,
            total=total,
            current_deck=deck,
            latest_deck_comment_md=str(output_dir / "deck_reviews" / f"{raw_name(deck)}_content_grading_comments.md"),
            content_grading_comments_md=str(output_dir / "content_grading_comments.md"),
            codex_workers=max_workers,
        )

    if max_workers == 1:
        for deck, deck_texts in deck_items:
            record(*run_job(deck, deck_texts))
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {executor.submit(run_job, deck, deck_texts): deck for deck, deck_texts in deck_items}
            for future in as_completed(future_map):
                record(*future.result())

    emit_status(
        output_dir,
        status_callback,
        state="ranking_deck_quality_with_codex",
        task_id=task_id,
        stage="deck_quality_ranking",
        completed=0,
        total=1,
        codex_workers=1,
    )
    try:
        result = attach_codex_quality_ranking(result, slide_texts, task_prompt, output_dir, model)
    except (Exception, SystemExit) as exc:
        fallback_note = f"Deck quality ranking fell back to local severity heuristics after ranking pass failed: {exc}"
        result = attach_quality_ranking(result)
        notes = result.setdefault("summary", {}).setdefault("notes", [])
        if isinstance(notes, list):
            notes.append(fallback_note)
    emit_status(
        output_dir,
        status_callback,
        state="ranking_deck_quality_with_codex",
        task_id=task_id,
        stage="deck_quality_ranking",
        completed=1,
        total=1,
        content_grading_comments_md=str(output_dir / "content_grading_comments.md"),
        codex_workers=1,
    )
    write_json(output_dir / "content_grading_comments.json", result)
    (output_dir / "content_grading_comments.md").write_text(render_content_grading_markdown(result), encoding="utf-8")
    return result


def generate_content_grading_comments_codex_fast_streaming(
    manifest: dict[str, Any],
    task_prompt: str,
    output_dir: Path,
    model: str,
    workers: int = 1,
    ocr_workers: int = DEFAULT_OCR_WORKERS,
    comments_per_deck: int = DEFAULT_COMMENTS_PER_DECK,
    ocr_backend: str = DEFAULT_OCR_BACKEND,
    status_callback: StatusCallback | None = None,
    task_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    ocr_backend = resolve_ocr_backend(ocr_backend)
    if ocr_backend == "api":
        raise SystemExit("OCR backend 'api' is not supported with the fast Codex pipeline. Use paddle or codex.")
    decks = manifest["decks"]
    total_slides = sum(len(deck_data["slides"]) for deck_data in decks.values())
    total_decks = len(decks)
    max_codex_workers = min(positive_int(workers, 1), max(1, total_decks))
    max_ocr_workers = positive_int(ocr_workers, DEFAULT_OCR_WORKERS)
    comment_count = max(1, int(comments_per_deck))
    slide_texts = {"decks": {deck: {"slides": []} for deck in decks}}
    issue_candidates = {"decks": {}}
    grading_comments = initial_content_grading_result()
    started_at = time.perf_counter()
    deck_comment_started_at: dict[str, float] = {}
    completed_slides = 0
    completed_comments = 0
    future_meta: dict[Any, dict[str, Any]] = {}

    def drain_done(comment_executor: ThreadPoolExecutor, block: bool = False) -> None:
        nonlocal completed_comments
        pending = set(future_meta)
        if not pending:
            return
        if block:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
        else:
            done, _ = wait(pending, timeout=0, return_when=FIRST_COMPLETED)
        for future in done:
            meta = future_meta.pop(future)
            deck, payload = future.result()
            grading_comments["decks"][deck] = payload
            completed_comments += 1
            finished_at = time.perf_counter()
            comment_elapsed = finished_at - deck_comment_started_at.get(deck, started_at)
            write_deck_comment_outputs(output_dir, deck, payload)
            write_content_grading_current(output_dir, grading_comments, partial=completed_comments < total_decks)
            emit_status(
                output_dir,
                status_callback,
                state="fast_content_grading_with_codex",
                task_id=task_id,
                stage="content_grading_comments",
                completed=completed_comments,
                total=total_decks,
                current_deck=deck,
                latest_deck_comment_md=str(output_dir / "deck_reviews" / f"{raw_name(deck)}_content_grading_comments.md"),
                content_grading_comments_md=str(output_dir / "content_grading_comments.md"),
                codex_workers=max_codex_workers,
                ocr_backend=ocr_backend,
                review_speed="fast",
                ocr_workers=max_ocr_workers,
                comments_per_deck=comment_count,
                elapsed_seconds=round(finished_at - started_at, 1),
                deck_comment_elapsed_seconds=round(comment_elapsed, 1),
            )

    with ThreadPoolExecutor(max_workers=max_codex_workers) as comment_executor, ThreadPoolExecutor(
        max_workers=max_ocr_workers
    ) as ocr_executor:
        for deck, deck_data in decks.items():
            deck_bucket = slide_texts["decks"][deck]
            deck_jobs = [
                (deck, int(slide_key), Path(str(slide_data["path"])))
                for slide_key, slide_data in deck_data["slides"].items()
            ]
            deck_ocr_workers = min(max_ocr_workers, max(1, len(deck_jobs)))
            future_map = {
                ocr_executor.submit(run_slide_text_job, deck_name, slide, image_path, output_dir, model, ocr_backend): slide
                for deck_name, slide, image_path in deck_jobs
            }
            for future in as_completed(future_map):
                slide = future_map[future]
                _, _, row = future.result()
                deck_bucket["slides"].append(row)
                deck_bucket["slides"].sort(key=lambda item: int(item.get("slide") or 0))
                completed_slides += 1
                write_json(output_dir / "slide_text_by_deck.partial.json", slide_texts)
                emit_status(
                    output_dir,
                    status_callback,
                    state="fast_content_grading_with_codex",
                    task_id=task_id,
                    stage="slide_text",
                    completed=completed_slides,
                    total=total_slides,
                    current_deck=deck,
                    current_slide=slide,
                    codex_workers=max_codex_workers,
                    ocr_workers=deck_ocr_workers,
                    ocr_backend=ocr_backend,
                    review_speed="fast",
                    comments_per_deck=comment_count,
                    elapsed_seconds=round(time.perf_counter() - started_at, 1),
                )
                drain_done(comment_executor, block=False)
            write_json(output_dir / "deck_reviews" / f"{raw_name(deck)}_slide_text.json", deck_bucket)
            future = comment_executor.submit(
                run_deck_content_grading_comments_codex_fast_job,
                deck,
                deck_bucket,
                task_prompt,
                output_dir,
                model,
                comment_count,
            )
            future_meta[future] = {"deck": deck}
            deck_comment_started_at[deck] = time.perf_counter()
            emit_status(
                output_dir,
                status_callback,
                state="fast_content_grading_with_codex",
                task_id=task_id,
                stage="deck_comment_submitted",
                completed=completed_comments,
                total=total_decks,
                current_deck=deck,
                content_grading_comments_md=str(output_dir / "content_grading_comments.md"),
                codex_workers=max_codex_workers,
                ocr_workers=max_ocr_workers,
                ocr_backend=ocr_backend,
                review_speed="fast",
                comments_per_deck=comment_count,
                elapsed_seconds=round(time.perf_counter() - started_at, 1),
            )
            drain_done(comment_executor, block=False)

        while future_meta:
            drain_done(comment_executor, block=True)

    write_json(output_dir / "slide_text_by_deck.json", slide_texts)
    write_json(output_dir / "content_issue_candidates.json", issue_candidates)
    emit_status(
        output_dir,
        status_callback,
        state="ranking_deck_quality_with_codex",
        task_id=task_id,
        stage="deck_quality_ranking",
        completed=0,
        total=1,
        codex_workers=1,
        ocr_workers=max_ocr_workers,
        ocr_backend=ocr_backend,
        review_speed="fast",
        comments_per_deck=comment_count,
        elapsed_seconds=round(time.perf_counter() - started_at, 1),
    )
    try:
        grading_comments = attach_codex_quality_ranking(grading_comments, slide_texts, task_prompt, output_dir, model)
    except (Exception, SystemExit) as exc:
        fallback_note = f"Deck quality ranking fell back to local severity heuristics after ranking pass failed: {exc}"
        grading_comments = attach_quality_ranking(grading_comments)
        notes = grading_comments.setdefault("summary", {}).setdefault("notes", [])
        if isinstance(notes, list):
            notes.append(fallback_note)
    emit_status(
        output_dir,
        status_callback,
        state="ranking_deck_quality_with_codex",
        task_id=task_id,
        stage="deck_quality_ranking",
        completed=1,
        total=1,
        content_grading_comments_md=str(output_dir / "content_grading_comments.md"),
        codex_workers=1,
        ocr_workers=max_ocr_workers,
        ocr_backend=ocr_backend,
        review_speed="fast",
        comments_per_deck=comment_count,
        elapsed_seconds=round(time.perf_counter() - started_at, 1),
    )
    write_json(output_dir / "content_grading_comments.json", grading_comments)
    (output_dir / "content_grading_comments.md").write_text(render_content_grading_markdown(grading_comments), encoding="utf-8")
    return slide_texts, issue_candidates, grading_comments


def generate_content_grading_comments_codex_streaming(
    manifest: dict[str, Any],
    task_prompt: str,
    output_dir: Path,
    model: str,
    workers: int = 1,
    ocr_backend: str = DEFAULT_OCR_BACKEND,
    status_callback: StatusCallback | None = None,
    task_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    ocr_backend = resolve_ocr_backend(ocr_backend)
    if ocr_backend == "api":
        raise SystemExit("OCR backend 'api' is not supported with the Codex streaming pipeline. Use paddle or codex.")
    decks = manifest["decks"]
    slide_texts = {"decks": {deck: {"slides": []} for deck in decks}}
    issue_candidates = {"decks": {}}
    grading_comments = initial_content_grading_result()
    deck_slide_totals = {deck: len(deck_data["slides"]) for deck, deck_data in decks.items()}
    deck_completed = {deck: 0 for deck in decks}
    slide_jobs: list[tuple[str, int, Path]] = []
    for deck, deck_data in decks.items():
        for slide_key, slide_data in deck_data["slides"].items():
            slide_jobs.append((deck, int(slide_key), Path(str(slide_data["path"]))))

    total_slides = len(slide_jobs)
    total_decks = len(decks)
    completed_slides = 0
    completed_issues = 0
    completed_slide_comments = 0
    completed_comments = 0
    next_slide_index = 0
    max_workers = min(positive_int(workers, 1), max(1, total_slides + total_decks * 2))
    pending = set()
    future_meta: dict[Any, dict[str, Any]] = {}
    finalized_decks: set[str] = set()

    def submit_slide_jobs(executor: ThreadPoolExecutor) -> None:
        nonlocal next_slide_index
        while next_slide_index < total_slides and len(pending) < max_workers:
            deck, slide, image_path = slide_jobs[next_slide_index]
            next_slide_index += 1
            future = executor.submit(run_slide_text_job, deck, slide, image_path, output_dir, model, ocr_backend)
            pending.add(future)
            future_meta[future] = {"kind": "slide_text"}

    def submit_issue_job(executor: ThreadPoolExecutor, deck: str) -> None:
        future = executor.submit(
            run_deck_issue_candidates_codex_job,
            deck,
            slide_texts["decks"][deck],
            task_prompt,
            output_dir,
            model,
        )
        pending.add(future)
        future_meta[future] = {"kind": "issue_candidates", "deck": deck}

    def submit_slide_comment_job(executor: ThreadPoolExecutor, deck: str, row: dict[str, Any]) -> None:
        future = executor.submit(
            run_slide_content_grading_comment_codex_job,
            deck,
            row,
            task_prompt,
            output_dir,
            model,
        )
        pending.add(future)
        future_meta[future] = {"kind": "slide_content_grading_comments", "deck": deck, "slide": int(row.get("slide") or 0)}

    def submit_comment_job(executor: ThreadPoolExecutor, deck: str, deck_issues: dict[str, Any]) -> None:
        future = executor.submit(
            run_deck_content_grading_comments_codex_job,
            deck,
            slide_texts["decks"][deck],
            deck_issues,
            task_prompt,
            output_dir,
            model,
        )
        pending.add(future)
        future_meta[future] = {"kind": "content_grading_comments", "deck": deck}

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        submit_slide_jobs(executor)
        while pending:
            done, pending_next = wait(pending, return_when=FIRST_COMPLETED)
            pending.clear()
            pending.update(pending_next)
            for future in done:
                meta = future_meta.pop(future)
                kind = meta["kind"]
                if kind == "slide_text":
                    deck, slide, row = future.result()
                    slide_texts["decks"][deck]["slides"].append(row)
                    slide_texts["decks"][deck]["slides"].sort(key=lambda item: int(item.get("slide") or 0))
                    completed_slides += 1
                    deck_completed[deck] += 1
                    write_json(output_dir / "slide_text_by_deck.partial.json", slide_texts)
                    if deck_completed[deck] == deck_slide_totals[deck]:
                        write_json(output_dir / "deck_reviews" / f"{raw_name(deck)}_slide_text.json", slide_texts["decks"][deck])
                        submit_issue_job(executor, deck)
                    submit_slide_comment_job(executor, deck, row)
                    emit_status(
                        output_dir,
                        status_callback,
                        state="streaming_content_grading_with_codex",
                        task_id=task_id,
                        stage="slide_text",
                        completed=completed_slides,
                        total=total_slides,
                        current_deck=deck,
                        current_slide=slide,
                        codex_workers=max_workers,
                        ocr_backend=ocr_backend,
                    )
                elif kind == "slide_content_grading_comments":
                    deck, slide, payload = future.result()
                    completed_slide_comments += 1
                    write_slide_comment_outputs(output_dir, deck, slide, payload)
                    if deck not in finalized_decks:
                        merge_slide_comment_draft(grading_comments, deck, payload)
                        write_content_grading_current(output_dir, grading_comments, partial=True)
                    emit_status(
                        output_dir,
                        status_callback,
                        state="streaming_content_grading_with_codex",
                        task_id=task_id,
                        stage="slide_comments",
                        completed=completed_slide_comments,
                        total=total_slides,
                        current_deck=deck,
                        current_slide=slide,
                        latest_slide_comment_md=str(
                            output_dir / "deck_reviews" / f"{raw_name(deck)}_slide_{slide:02d}_content_grading_comments.md"
                        ),
                        content_grading_comments_md=str(output_dir / "content_grading_comments.md"),
                        codex_workers=max_workers,
                        ocr_backend=ocr_backend,
                    )
                elif kind == "issue_candidates":
                    deck, payload = future.result()
                    issue_candidates["decks"][deck] = payload
                    completed_issues += 1
                    write_json(output_dir / "content_issue_candidates.partial.json", issue_candidates)
                    write_json(output_dir / "deck_reviews" / f"{raw_name(deck)}_issue_candidates.json", payload)
                    submit_comment_job(executor, deck, payload)
                    emit_status(
                        output_dir,
                        status_callback,
                        state="streaming_content_grading_with_codex",
                        task_id=task_id,
                        stage="issue_candidates",
                        completed=completed_issues,
                        total=total_decks,
                        current_deck=deck,
                        codex_workers=max_workers,
                        ocr_backend=ocr_backend,
                    )
                elif kind == "content_grading_comments":
                    deck, payload = future.result()
                    grading_comments["decks"][deck] = payload
                    finalized_decks.add(deck)
                    completed_comments += 1
                    write_deck_comment_outputs(output_dir, deck, payload)
                    write_content_grading_current(output_dir, grading_comments, partial=completed_comments < total_decks)
                    emit_status(
                        output_dir,
                        status_callback,
                        state="streaming_content_grading_with_codex",
                        task_id=task_id,
                        stage="content_grading_comments",
                        completed=completed_comments,
                        total=total_decks,
                        current_deck=deck,
                        latest_deck_comment_md=str(output_dir / "deck_reviews" / f"{raw_name(deck)}_content_grading_comments.md"),
                        content_grading_comments_md=str(output_dir / "content_grading_comments.md"),
                        codex_workers=max_workers,
                        ocr_backend=ocr_backend,
                    )
            submit_slide_jobs(executor)

    write_json(output_dir / "slide_text_by_deck.json", slide_texts)
    write_json(output_dir / "content_issue_candidates.json", issue_candidates)
    emit_status(
        output_dir,
        status_callback,
        state="ranking_deck_quality_with_codex",
        task_id=task_id,
        stage="deck_quality_ranking",
        completed=0,
        total=1,
        codex_workers=1,
        ocr_backend=ocr_backend,
    )
    try:
        grading_comments = attach_codex_quality_ranking(grading_comments, slide_texts, task_prompt, output_dir, model)
    except (Exception, SystemExit) as exc:
        fallback_note = f"Deck quality ranking fell back to local severity heuristics after ranking pass failed: {exc}"
        grading_comments = attach_quality_ranking(grading_comments)
        notes = grading_comments.setdefault("summary", {}).setdefault("notes", [])
        if isinstance(notes, list):
            notes.append(fallback_note)
    emit_status(
        output_dir,
        status_callback,
        state="ranking_deck_quality_with_codex",
        task_id=task_id,
        stage="deck_quality_ranking",
        completed=1,
        total=1,
        content_grading_comments_md=str(output_dir / "content_grading_comments.md"),
        codex_workers=1,
        ocr_backend=ocr_backend,
    )
    write_json(output_dir / "content_grading_comments.json", grading_comments)
    (output_dir / "content_grading_comments.md").write_text(render_content_grading_markdown(grading_comments), encoding="utf-8")
    return slide_texts, issue_candidates, grading_comments


def run_review_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    output_dir = args.output_dir or DEFAULT_OUTPUT_ROOT / args.task_id
    output_dir.mkdir(parents=True, exist_ok=True)
    ensure_review_markdown_placeholder(output_dir, args.task_id)
    status_callback = getattr(args, "status_callback", None)
    emit_status(output_dir, status_callback, state="starting", task_id=args.task_id)

    download_summary = None
    if not args.skip_download:
        emit_status(output_dir, status_callback, state="downloading", task_id=args.task_id)
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
    requested_backend = getattr(args, "llm_backend", DEFAULT_LLM_BACKEND)
    llm_backend = "none" if getattr(args, "no_llm", False) else resolve_llm_backend(requested_backend, api_key)
    allow_non_paddle_ocr = bool(getattr(args, "allow_non_paddle_ocr", DEFAULT_ALLOW_NON_PADDLE_OCR)) or DEFAULT_ALLOW_NON_PADDLE_OCR
    ocr_backend = resolve_ocr_backend(
        getattr(args, "ocr_backend", DEFAULT_OCR_BACKEND),
        allow_non_paddle=allow_non_paddle_ocr,
    )
    review_speed = text_value(getattr(args, "review_speed", DEFAULT_REVIEW_SPEED)).lower() or "fast"
    if review_speed not in {"fast", "thorough"}:
        raise SystemExit(f"Unsupported review speed: {review_speed}. Use fast or thorough.")
    llm_state = "skipped_no_llm_backend" if llm_backend == "none" else "starting"
    slide_texts = None
    issue_candidates = None
    grading_comments = None
    chosen_model = None
    if llm_backend == "api":
        chosen_model = args.model
        emit_status(
            output_dir,
            status_callback,
            state="extracting_slide_text",
            task_id=args.task_id,
            ocr_backend=ocr_backend,
        )
        if ocr_backend == "paddle":
            slide_texts = extract_slide_texts_paddleocr(
                manifest,
                output_dir,
                workers=positive_int(getattr(args, "codex_workers", DEFAULT_CODEX_WORKERS), DEFAULT_CODEX_WORKERS),
                status_callback=status_callback,
                task_id=args.task_id,
            )
        elif ocr_backend == "codex":
            slide_texts = extract_slide_texts_codex(
                manifest,
                output_dir,
                getattr(args, "codex_model", DEFAULT_CODEX_MODEL),
                workers=positive_int(getattr(args, "codex_workers", DEFAULT_CODEX_WORKERS), DEFAULT_CODEX_WORKERS),
                status_callback=status_callback,
                task_id=args.task_id,
            )
        else:
            slide_texts = extract_slide_texts(manifest, output_dir, api_key, args.model)
        emit_status(output_dir, status_callback, state="finding_issue_candidates", task_id=args.task_id)
        issue_candidates = generate_issue_candidates(slide_texts, task_prompt, output_dir, api_key, args.model)
        emit_status(output_dir, status_callback, state="writing_content_grading_comments", task_id=args.task_id)
        grading_comments = generate_content_grading_comments(
            slide_texts,
            issue_candidates,
            task_prompt,
            output_dir,
            api_key,
            args.model,
        )
        llm_state = "completed"
    elif llm_backend == "codex":
        chosen_model = getattr(args, "codex_model", DEFAULT_CODEX_MODEL)
        codex_workers = positive_int(getattr(args, "codex_workers", DEFAULT_CODEX_WORKERS), DEFAULT_CODEX_WORKERS)
        ocr_workers = positive_int(getattr(args, "ocr_workers", DEFAULT_OCR_WORKERS), DEFAULT_OCR_WORKERS)
        comments_per_deck = positive_int(getattr(args, "comments_per_deck", DEFAULT_COMMENTS_PER_DECK), DEFAULT_COMMENTS_PER_DECK)
        emit_status(
            output_dir,
            status_callback,
            state="streaming_content_grading_with_codex",
            task_id=args.task_id,
            stage="starting",
            codex_workers=codex_workers,
            ocr_workers=ocr_workers,
            comments_per_deck=comments_per_deck,
            ocr_backend=ocr_backend,
            review_speed=review_speed,
        )
        if review_speed == "fast":
            slide_texts, issue_candidates, grading_comments = generate_content_grading_comments_codex_fast_streaming(
                manifest,
                task_prompt,
                output_dir,
                chosen_model,
                workers=codex_workers,
                ocr_workers=ocr_workers,
                comments_per_deck=comments_per_deck,
                ocr_backend=ocr_backend,
                status_callback=status_callback,
                task_id=args.task_id,
            )
        else:
            slide_texts, issue_candidates, grading_comments = generate_content_grading_comments_codex_streaming(
                manifest,
                task_prompt,
                output_dir,
                chosen_model,
                workers=codex_workers,
                ocr_backend=ocr_backend,
                status_callback=status_callback,
                task_id=args.task_id,
            )
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
        "content_grading_comments": str(output_dir / "content_grading_comments.json") if grading_comments else None,
        "content_grading_comments_md": str(output_dir / "content_grading_comments.md") if grading_comments else None,
        "llm_backend": llm_backend,
        "llm_model": chosen_model,
        "ocr_backend": ocr_backend,
        "review_speed": review_speed,
        "codex_workers": positive_int(getattr(args, "codex_workers", DEFAULT_CODEX_WORKERS), DEFAULT_CODEX_WORKERS)
        if llm_backend == "codex"
        else None,
        "ocr_workers": positive_int(getattr(args, "ocr_workers", DEFAULT_OCR_WORKERS), DEFAULT_OCR_WORKERS)
        if llm_backend == "codex"
        else None,
        "comments_per_deck": positive_int(getattr(args, "comments_per_deck", DEFAULT_COMMENTS_PER_DECK), DEFAULT_COMMENTS_PER_DECK)
        if llm_backend == "codex"
        else None,
        "llm": llm_state,
    }
    emit_status(output_dir, status_callback, **final)
    return final


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download claimed task slides and prepare Content Grading evidence.")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--curl-file", type=Path, default=DEFAULT_CURL_FILE)
    parser.add_argument("--redirect-graphql-curl-file", type=Path, default=DEFAULT_REDIRECT_GRAPHQL_CURL_FILE)
    parser.add_argument("--conversation-graphql-curl-file", type=Path, default=DEFAULT_CONVERSATION_GRAPHQL_CURL_FILE)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--llm-backend",
        choices=["auto", "api", "codex", "none"],
        default=DEFAULT_LLM_BACKEND,
        help="LLM backend. auto uses OPENAI_API_KEY first, then Codex CLI subscription auth.",
    )
    parser.add_argument("--codex-model", default=DEFAULT_CODEX_MODEL, help="Model passed to codex exec when using --llm-backend codex.")
    parser.add_argument(
        "--codex-workers",
        type=int,
        default=DEFAULT_CODEX_WORKERS,
        help="Maximum parallel codex exec workers for independent slide/deck steps.",
    )
    parser.add_argument(
        "--ocr-workers",
        type=int,
        default=DEFAULT_OCR_WORKERS,
        help="Maximum parallel OCR workers used by the fast PaddleOCR deck pipeline.",
    )
    parser.add_argument(
        "--comments-per-deck",
        type=int,
        default=DEFAULT_COMMENTS_PER_DECK,
        help="Number of high-signal content comments to request per deck in fast mode.",
    )
    parser.add_argument(
        "--ocr-backend",
        choices=["paddle", "codex", "api"],
        default=DEFAULT_OCR_BACKEND,
        help="Slide text extraction backend. Default paddle uses local PaddleOCR; non-paddle backends require --allow-non-paddle-ocr.",
    )
    parser.add_argument(
        "--allow-non-paddle-ocr",
        action="store_true",
        default=DEFAULT_ALLOW_NON_PADDLE_OCR,
        help="Explicitly allow Codex/API vision OCR fallback. Off by default so OCR stays on PaddleOCR.",
    )
    parser.add_argument(
        "--review-speed",
        choices=["fast", "thorough"],
        default=DEFAULT_REVIEW_SPEED,
        help="fast uses one Codex call per deck plus final ranking; thorough also drafts per-slide comments.",
    )
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    print(json.dumps(run_review_pipeline(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
