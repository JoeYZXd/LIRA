"""Explicit, local OCR evidence export; importing this module never loads RKNN."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import statistics
import time
import unicodedata
import uuid
from pathlib import Path

import cv2
import numpy as np

from lira.vision.engine import OcrEngine


def normalize(text: str) -> str:
    """NFC and remove Unicode whitespace on both sides; preserve punctuation."""
    return "".join(unicodedata.normalize("NFC", text).split())


def score(reference: str, recognized: str) -> dict:
    ref, hyp = normalize(reference), normalize(recognized)
    if not ref:
        raise ValueError("Reference must contain non-whitespace characters")
    # Cells carry (distance, substitutions, deletions, insertions). Only two
    # rows are retained; ties prefer diagonal, deletion, then insertion.
    previous = [(j, 0, 0, j) for j in range(len(hyp) + 1)]
    for i, expected in enumerate(ref, 1):
        current = [(i, 0, i, 0)]
        for j, actual in enumerate(hyp, 1):
            d, s, deleted, inserted = previous[j - 1]
            diagonal = (
                d + (expected != actual),
                s + (expected != actual),
                deleted,
                inserted,
            )
            d, s, deleted, inserted = previous[j]
            deletion = (d + 1, s, deleted + 1, inserted)
            d, s, deleted, inserted = current[j - 1]
            insertion = (d + 1, s, deleted, inserted + 1)
            best = diagonal if diagonal[0] <= deletion[0] else deletion
            current.append(best if best[0] <= insertion[0] else insertion)
        previous = current
    distance, substitutions, deletions, insertions = previous[-1]
    return {
        "reference_raw": reference,
        "recognized_raw": recognized,
        "reference_normalized": ref,
        "recognized_normalized": hyp,
        "normalization": "Unicode NFC; remove all Unicode whitespace; retain punctuation",
        "tie_break": "diagonal, deletion, insertion",
        "reference_characters": len(ref),
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
        "edit_distance": distance,
        "cer": distance / len(ref),
    }


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class InferenceRecorder:
    """Save the exact tensor passed by the unmodified production preprocessor."""

    def __init__(self, runtime, directory: Path, label: str):
        self.runtime, self.directory, self.label = runtime, directory, label
        self.records = []

    def inference(self, *args, **kwargs):
        inputs = kwargs.get("inputs", args[0] if args else None)
        if inputs is None:
            raise ValueError("No model inputs supplied")
        for index, tensor in enumerate(inputs):
            value = np.asarray(tensor)
            name = f"{self.label}-{len(self.records):04d}-{index}.npy"
            path = self.directory / name
            np.save(path, value, allow_pickle=False)
            record = {
                "file": name,
                "sha256": digest(path),
                "shape": list(value.shape),
                "dtype": str(value.dtype),
            }
            self.records.append(record)
            # Detector preview preserves raw 0..255 BGR input for inspection;
            # the .npy is authoritative (no display conversion is scored).
            if (
                self.label == "det"
                and value.ndim == 4
                and value.shape[0] == 1
                and value.shape[-1] == 3
            ):
                preview = path.with_suffix(".png")
                if not cv2.imwrite(
                    str(preview), np.clip(value[0], 0, 255).astype(np.uint8)
                ):
                    raise RuntimeError("Detector preview write failed")
                record["preview"] = preview.name
        return self.runtime.inference(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.runtime, name)


class EvidenceEngine(OcrEngine):
    """Use OcrEngine.read itself, retaining even detections with empty text."""

    def __init__(self, engine, directory: Path):
        self.engine, self.directory = engine, directory
        self.polygons, self.recognitions = [], []

    def detect(self, image):
        polygons = self.engine.detect(image)
        self.polygons = [np.asarray(p).tolist() for p in polygons]
        return polygons

    def recognize(self, crop):
        record = {"index": len(self.recognitions), "shape": list(crop.shape)}
        self.recognitions.append(record)
        if crop.size:
            path = self.directory / f"crop-{record['index']:04d}.png"
            if not cv2.imwrite(str(path), crop):
                raise RuntimeError("Recognition crop write failed")
            record.update(file=path.name, sha256=digest(path))
        text = self.engine.recognize(crop)
        record["text"] = text
        return text


def read_evidence(engine, image, directory: Path) -> dict:
    """Restore runtime attributes even on failures; failures retain partial evidence."""
    directory.mkdir(parents=True, exist_ok=False)
    wrapper = EvidenceEngine(engine, directory)
    original = {}
    recorders = {}
    evidence = {"success": False}
    started = time.monotonic()
    try:
        for attribute, label in (("_det", "det"), ("_rec", "rec")):
            original[attribute] = getattr(engine, attribute)
            recorder = InferenceRecorder(original[attribute], directory, label)
            setattr(engine, attribute, recorder)
            recorders[label] = recorder
        # Inherited OcrEngine.read performs the real sorting, perspective crops,
        # recognition stripping and empty-text exclusion, without a second path.
        lines = wrapper.read(image)
        evidence.update(
            success=True,
            lines=[
                {"polygon": np.asarray(line.polygon).tolist(), "text": line.text}
                for line in lines
            ],
            text="\n".join(line.text for line in lines),
            recognized_line_count=len(lines),
        )
    except Exception as exc:  # noqa: BLE001 - preserve arbitrary backend failures as evidence
        evidence["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for attribute, runtime in original.items():
            setattr(engine, attribute, runtime)
        evidence.update(
            elapsed_seconds=time.monotonic() - started,
            detected_line_count=len(wrapper.polygons),
            detected_polygons=wrapper.polygons,
            recognition_attempts=wrapper.recognitions,
            inference_inputs={
                label: recorder.records for label, recorder in recorders.items()
            },
        )
        write_json(directory / "evidence.json", evidence)
    return evidence


def contained_file(directory: Path, name: str) -> Path:
    path = (directory / name).resolve()
    if path.parent != directory.resolve():
        raise ValueError("Manifest file must reside directly in its directory")
    return path


def samples_from_manifest(path: Path, stages: list[str] | None = None) -> list[dict]:
    path = path.resolve()
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("valid") is not True:
        raise ValueError("Manifest is not valid")
    schema = manifest.get("schema")
    source_verification = "not applicable"
    if schema == "lira.camera-quality-stages/v1" and manifest.get("kind") == "stages":
        source_path = Path(manifest["source_manifest"])
        source_hash = manifest["source_manifest_sha256"]
        if source_path.is_file():
            if digest(source_path) != source_hash:
                raise ValueError("Source capture manifest hash mismatch")
            source = json.loads(source_path.read_text(encoding="utf-8"))
            if (
                source.get("valid") is not True
                or source.get("restore", {}).get("ok") is not True
            ):
                raise ValueError("Source capture is not valid/restored")
            source_verification = "source manifest hash and validity verified"
        else:
            source_verification = "source capture manifest unavailable locally; link/hash retained, capture provenance unverified"
        entries = manifest["outputs"]
        if stages:
            entries = [entry for entry in entries if entry["stage"] in stages]
            if {entry["stage"] for entry in entries} != set(stages):
                raise ValueError("Requested stages are absent")
    elif schema == "lira.camera-quality/v1" and manifest.get("kind") == "baseline":
        if (
            manifest.get("capture_ok") is not True
            or manifest.get("restore", {}).get("ok") is not True
        ):
            raise ValueError("Baseline capture/restoration unsuccessful")
        entries = [
            {**entry, "stage": "production-baseline"} for entry in manifest["frames"]
        ]
    else:
        raise ValueError("Expected stages or production baseline manifest")
    if not entries:
        raise ValueError("Manifest contains no image samples")
    samples = []
    for entry in entries:
        image = contained_file(path.parent, entry["file"])
        actual = digest(image)
        if actual != entry["sha256"]:
            raise ValueError("Image hash differs from manifest")
        samples.append(
            {
                "image": str(image),
                "sha256": actual,
                "group": entry["stage"],
                "manifest": str(path),
                "manifest_sha256": digest(path),
                "scene": manifest.get("scene"),
                "source_manifest": manifest.get("source_manifest"),
                "source_manifest_sha256": manifest.get("source_manifest_sha256"),
                "source_verification": source_verification,
                "independent_capture_unit": str(path)
                if schema == "lira.camera-quality/v1"
                else manifest["source_manifest"],
                "provenance": "manifest image hash verified; scene is operator supplied",
            }
        )
    return samples


def engine_identity(engine, paths: dict[str, Path]) -> dict:
    import inspect

    from lira.vision import engine as engine_module
    from lira.vision import layout

    files = {
        **paths,
        "engine_source": Path(inspect.getfile(type(engine))),
        "read_source": Path(engine_module.__file__),
        "layout_source": Path(layout.__file__),
    }
    result = {
        "backend": f"{type(engine).__module__}.{type(engine).__name__}",
        "files": {
            name: {"path": str(path.resolve()), "sha256": digest(path)}
            for name, path in files.items()
        },
    }
    try:
        result["rknn_toolkit_lite2_version"] = importlib.metadata.version(
            "rknn-toolkit-lite2"
        )
    except importlib.metadata.PackageNotFoundError:
        result["rknn_toolkit_lite2_version"] = "unavailable"
    return result


def create_engine(args):
    # Deliberately lazy: host score/help require no pyclipper, RKNN or board.
    from lira.vision.ocr_rknn import DET_RKNN, REC_DICT, REC_RKNN, RknnOcrEngine

    paths = {
        "det": Path(args.det) if args.det else DET_RKNN,
        "rec": Path(args.rec) if args.rec else REC_RKNN,
        "dictionary": Path(args.dictionary) if args.dictionary else REC_DICT,
    }
    engine = object.__new__(RknnOcrEngine)
    try:
        engine.__init__(paths["det"], paths["rec"], paths["dictionary"])
        return engine, engine_identity(engine, paths)
    except Exception as exc:
        # A later model/runtime or identity error must not leak the first NPU
        # runtime that production construction already attached.
        errors = release_engine(engine)
        if errors:
            raise RuntimeError(
                f"{type(exc).__name__}: {exc}; partial initialization cleanup errors: {errors}"
            ) from exc
        raise


def release_engine(engine) -> list[str]:
    errors = []
    for attribute in ("_det", "_rec"):
        runtime = getattr(engine, attribute, None)
        if runtime is not None and hasattr(runtime, "release"):
            try:
                runtime.release()
            except Exception as exc:  # noqa: BLE001 - preserve arbitrary backend failures as evidence
                errors.append(f"{attribute}: {type(exc).__name__}: {exc}")
    return errors


def summarize(samples: list[dict]) -> dict:
    groups = {}
    for sample in samples:
        group = groups.setdefault(
            sample["group"],
            {"attempted": 0, "successful": 0, "failed": 0, "cer_values": []},
        )
        group["attempted"] += 1
        if sample.get("success") and "score" in sample:
            group["successful"] += 1
            group["cer_values"].append(sample["score"]["cer"])
        else:
            group["failed"] += 1
    for group in groups.values():
        values = group["cer_values"]
        group["mean_cer"] = statistics.mean(values) if values else None
        group["median_cer"] = statistics.median(values) if values else None
        group["denominator"] = len(values)
    return groups


def execute(args, engine_factory=create_engine) -> int:
    output = Path(args.output) / (
        "ocr-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:10]
    )
    output.mkdir(parents=True, exist_ok=False)
    report = {
        "schema": "lira.ocr-comparison/v1",
        "command": args.command,
        "success": False,
        "reference_scope": args.reference_scope,
        "expected_physical_lines": args.expected_lines,
        "samples": [],
        "acceptance": "Diagnostic evidence only; no automatic recommendation",
        "statistics_warning": "Group denominator is successful image scores, not independent captures. Shared independent_capture_unit values represent the same batch; no stability claim is inferred.",
    }
    engine = None
    try:
        reference = Path(args.reference).read_text(encoding="utf-8")
        score(reference, "")  # Reject empty reference before inference.
        report.update(
            reference_raw=reference,
            reference_normalized=normalize(reference),
            reference_sha256=digest(Path(args.reference)),
        )
        if args.reference_scope == "body-only":
            report["reference_warning"] = (
                "Full-image CER includes extra pinyin, headers and page numbers as insertions. Body-only reference does not describe all printed text. No output is filtered."
            )
        if args.command == "score":
            for pathstring in args.text:
                path = Path(pathstring)
                sample = {
                    "group": "text",
                    "text_file": str(path.resolve()),
                    "success": False,
                }
                report["samples"].append(sample)
                try:
                    sample.update(
                        success=True,
                        score=score(reference, path.read_text(encoding="utf-8")),
                        sha256=digest(path),
                    )
                except Exception as exc:  # noqa: BLE001 - preserve arbitrary backend failures as evidence
                    sample.update(success=False, error=f"{type(exc).__name__}: {exc}")
        else:
            for pathstring in args.manifest:
                try:
                    report["samples"].extend(
                        samples_from_manifest(Path(pathstring), args.stage)
                    )
                except Exception as exc:  # noqa: BLE001 - preserve arbitrary backend failures as evidence
                    report["samples"].append(
                        {
                            "group": "invalid-manifest",
                            "manifest": pathstring,
                            "success": False,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
            for pathstring in args.image:
                report["samples"].append(
                    {
                        "image": str(Path(pathstring).resolve()),
                        "group": "unattributed",
                        "provenance_warning": "Direct image has no controlled scene/capture provenance",
                    }
                )
            if not report["samples"]:
                raise ValueError("At least one image or manifest is required")
            try:
                engine, report["engine"] = engine_factory(args)
            except Exception as exc:  # noqa: BLE001 - preserve arbitrary backend failures as evidence
                report["initialization_error"] = f"{type(exc).__name__}: {exc}"
            for index, sample in enumerate(report["samples"]):
                if "error" in sample:
                    continue
                sample["success"] = False
                try:
                    if engine is None:
                        raise RuntimeError("OCR engine initialization failed")
                    path = Path(sample["image"])
                    sample["sha256"] = digest(path)
                    image = cv2.imread(str(path))
                    if image is None:
                        raise ValueError("Cannot decode image")
                    sample["image_shape"] = list(image.shape)
                    evidence = read_evidence(
                        engine, image, output / f"sample-{index:04d}"
                    )
                    sample.update(evidence)
                    sample["evidence_directory"] = f"sample-{index:04d}"
                    if sample["success"]:
                        sample["score"] = score(reference, evidence["text"])
                        # Detected counts cannot prove physical lines were all found.
                        sample["expected_physical_lines"] = args.expected_lines
                    if args.rois:
                        from lira.diagnostics.camera_quality import validate_rois

                        rois = validate_rois(
                            json.loads(Path(args.rois).read_text(encoding="utf-8")),
                            image.shape[1],
                            image.shape[0],
                        )
                        auxiliary = {}
                        for name, (x, y, width, height) in rois.items():
                            auxiliary[name] = read_evidence(
                                engine,
                                image[y : y + height, x : x + width],
                                output / f"sample-{index:04d}-roi-{name}",
                            )
                        sample["auxiliary_roi_ocr"] = auxiliary
                        sample["auxiliary_warning"] = (
                            "Original-image region OCR is auxiliary only; never included in full-page CER or acceptance"
                        )
                except Exception as exc:  # noqa: BLE001 - preserve arbitrary backend failures as evidence
                    sample.update(success=False, error=f"{type(exc).__name__}: {exc}")
        report["groups"] = summarize(report["samples"])
        report["success"] = bool(report["samples"]) and all(
            s.get("success") for s in report["samples"]
        )
    except Exception as exc:  # noqa: BLE001 - preserve arbitrary backend failures as evidence
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if engine is not None:
            release_errors = release_engine(engine)
            if release_errors:
                report["release_errors"] = release_errors
                report["success"] = False
        write_json(output / "report.json", report)
    print(output)
    return 0 if report["success"] else 1


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("score", "read"):
        sub = commands.add_parser(command)
        sub.add_argument(
            "--reference",
            required=True,
            help="UTF-8 correct text; whitespace removed, punctuation retained",
        )
        sub.add_argument(
            "--reference-scope",
            choices=("all-printed-text", "body-only"),
            default="all-printed-text",
        )
        sub.add_argument(
            "--expected-lines",
            type=int,
            help="Manually counted physical text lines, never paragraph count",
        )
        sub.add_argument("--output", default="camera-experiments")
        if command == "score":
            sub.add_argument(
                "--text",
                action="append",
                required=True,
                help="UTF-8 recognized text; repeat for multiple files",
            )
        else:
            sub.add_argument("--manifest", action="append", default=[])
            sub.add_argument(
                "--stage",
                action="append",
                help="Select named full-image processing stage, repeatable",
            )
            sub.add_argument("--image", action="append", default=[])
            sub.add_argument("--det")
            sub.add_argument("--rec")
            sub.add_argument("--dictionary")
            sub.add_argument(
                "--rois",
                help="Optional five original-image ROI rectangles JSON; auxiliary OCR only",
            )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if args.expected_lines is not None and args.expected_lines <= 0:
        parser.error("--expected-lines must be positive")
    return execute(args)


if __name__ == "__main__":
    raise SystemExit(main())
