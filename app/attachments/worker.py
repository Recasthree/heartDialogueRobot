"""Bounded subprocess parser. No model API, network OCR or arbitrary commands."""
import contextlib
import hashlib
import io
import json
import sys
from functools import lru_cache
from pathlib import Path

MAX_PIXELS = 25_000_000
MAX_TEXT = 16000


def file_kind(data):
    if data.startswith(b"%PDF-"):
        return "pdf"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if b"\x00" not in data:
        try:
            text = data.decode("utf-8-sig")
            if any(ord(c) < 32 and c not in "\r\n\t" for c in text):
                raise ValueError("unsupported_file")
            return "txt"
        except UnicodeDecodeError:
            pass
    raise ValueError("unsupported_file")


@lru_cache(maxsize=1)
def ocr_engine(model_dir):
    model_dir = Path(model_dir)
    manifest = model_dir / "manifest.json"
    if not manifest.exists():
        raise RuntimeError("ocr_models_not_installed")
    specs = json.loads(manifest.read_text(encoding="utf-8"))
    params = {"EngineConfig.onnxruntime.intra_op_num_threads": 2,
              "EngineConfig.onnxruntime.inter_op_num_threads": 1}
    for name in ("Det", "Cls", "Rec"):
        path = model_dir / specs[name]["file"]
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != specs[name]["sha256"]:
            raise RuntimeError("ocr_model_integrity_failed")
        params[name + ".model_path"] = str(path)
    from rapidocr import RapidOCR
    return RapidOCR(params=params)


def ocr_image(image, model_dir):
    import numpy as np
    if image.width * image.height > MAX_PIXELS:
        raise ValueError("image_pixel_limit")
    engine = ocr_engine(str(model_dir))
    result = engine(np.asarray(image.convert("RGB")))
    texts, boxes, scores = result.txts, result.boxes, result.scores
    if texts is None:
        return []
    return [{"text": str(text), "bbox": box.tolist(), "confidence": float(score)}
            for text, box, score in zip(texts, boxes, scores)]


def parse(path, model_dir, selected_pages=None, force_ocr=False):
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    data = Path(path).read_bytes()
    if not data or len(data) > 20 * 1024 * 1024:
        raise ValueError("file_size_limit")
    kind = file_kind(data)
    output = []
    if kind == "txt":
        text = data.decode("utf-8-sig")
        if len(text) > MAX_TEXT:
            raise ValueError("text_limit_choose_excerpt")
        return {"kind": kind, "page_count": 1, "pages": [page(1, text, "text", [])]}
    if kind in {"png", "jpeg"}:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            if image.width * image.height > MAX_PIXELS:
                raise ValueError("image_pixel_limit")
            blocks = ocr_image(image, model_dir)
        output.append(page(1, "\n".join(b["text"] for b in blocks), "ocr", blocks))
        return {"kind": kind, "page_count": 1, "pages": output}
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise ValueError("encrypted_pdf")
    count = len(reader.pages)
    numbers = selected_pages or list(range(1, count + 1))
    if not numbers or len(numbers) > 20 or len(set(numbers)) != len(numbers) or any(n < 1 or n > count for n in numbers):
        raise ValueError("page_limit_choose_up_to_20_pages")
    pdf = None
    try:
        for number in numbers:
            text = reader.pages[number - 1].extract_text() or ""
            if not force_ocr and len("".join(text.split())) >= 20:
                output.append(page(number, text, "pdf_text", []))
                continue
            if pdf is None:
                import pypdfium2
                pdf = pypdfium2.PdfDocument(data)
            pdf_page = pdf[number - 1]
            try:
                width, height = pdf_page.get_size()
                if width * height * 4 > MAX_PIXELS:
                    raise ValueError("image_pixel_limit")
                bitmap = pdf_page.render(scale=2)
                try:
                    blocks = ocr_image(bitmap.to_pil(), model_dir)
                finally:
                    bitmap.close()
            finally:
                pdf_page.close()
            output.append(page(number, "\n".join(b["text"] for b in blocks), "ocr", blocks))
    finally:
        if pdf:
            pdf.close()
    return {"kind": kind, "page_count": count, "pages": output}


def page(number, text, method, blocks):
    if len(text) > MAX_TEXT:
        raise ValueError("page_text_limit_choose_excerpt")
    if not text.strip():
        raise ValueError("no_readable_text")
    return {"page": number, "text": text, "method": method, "blocks": blocks}


if __name__ == "__main__":
    try:
        with contextlib.redirect_stdout(sys.stderr):
            result = parse(sys.argv[1], sys.argv[2], json.loads(sys.argv[3]), sys.argv[4] == "1")
        print(json.dumps({"ok": True, "data": result}, ensure_ascii=True))
    except Exception as exc:
        # Only allow known error codes, not paths / source documents in logs.
        code = str(exc) if isinstance(exc, (ValueError, RuntimeError)) and str(exc).replace("_", "").isalnum() else type(exc).__name__
        print(json.dumps({"ok": False, "error": code}))
        sys.exit(1)
