"""Copy the official wheel's bundled ONNX weights into the project cache.

No network is used here. This is an explicit provisioning step, not runtime download.
"""
import hashlib
import json
from pathlib import Path
import shutil


def main():
    import rapidocr
    from app.config import Settings
    root = Path(Settings.from_env().ocr_model_dir)
    root.mkdir(parents=True, exist_ok=True)
    bundled = Path(rapidocr.__file__).parent / "models"
    manifest = {}
    for name in ("Det", "Cls", "Rec"):
        choices = sorted(bundled.glob("*" + name.lower() + "*.onnx"))
        if not choices:
            raise RuntimeError("Official rapidocr wheel does not contain " + name + " model; provision models before starting OCR.")
        source = choices[0]
        target = root / source.name
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if not target.exists() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            shutil.copyfile(source, target)
        manifest[name] = {"file": source.name, "sha256": digest, "source": "rapidocr official wheel"}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"status": "ready", "models": manifest}, ensure_ascii=True))


if __name__ == "__main__":
    main()
