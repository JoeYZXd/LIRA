import importlib
import json
import subprocess
import sys
import types
from pathlib import Path

import cv2
import numpy as np
import pytest

from lira.diagnostics.ocr_compare import (
    build_parser,
    create_engine,
    digest,
    execute,
    normalize,
    read_evidence,
    samples_from_manifest,
    score,
    summarize,
)


@pytest.mark.parametrize(
    "reference,hypothesis,counts",
    [
        ("甲乙", "甲乙", (0, 0, 0)),
        ("甲乙", "甲丙", (1, 0, 0)),
        ("甲乙", "甲", (0, 1, 0)),
        ("甲乙", "甲乙丙", (0, 0, 1)),
        ("甲乙", "", (0, 2, 0)),
        ("甲，乙。", "甲乙。", (0, 1, 0)),
        ("é甲\n乙", "e\u0301 甲\u3000乙", (0, 0, 0)),
    ],
)
def test_edit_counts_and_normalization(reference, hypothesis, counts):
    result = score(reference, hypothesis)
    assert (
        tuple(result[key] for key in ("substitutions", "deletions", "insertions"))
        == counts
    )
    assert result["cer"] == sum(counts) / len(normalize(reference))


def test_empty_reference_rejected_and_insertion_can_exceed_one():
    with pytest.raises(ValueError, match="Reference"):
        score("\n\u3000", "文字")
    assert score("甲", "甲乙丙")["cer"] == 2


class Runtime:
    def __init__(self, output=None, fail=False):
        self.inputs = []
        self.output = output
        self.fail = fail
        self.released = False

    def inference(self, inputs):
        self.inputs.append(inputs[0].copy())
        if self.fail:
            raise RuntimeError("injected inference failure")
        return [self.output]

    def release(self):
        self.released = True


@pytest.fixture
def production_engine(monkeypatch):
    # Instantiate the actual production engine without loading hardware models.
    # Both production preprocessors, postprocessors, and OcrEngine.read run.
    api = types.ModuleType("rknnlite.api")
    api.RKNNLite = object
    monkeypatch.setitem(sys.modules, "rknnlite", types.ModuleType("rknnlite"))
    monkeypatch.setitem(sys.modules, "rknnlite.api", api)
    import lira.vision

    previous_module = sys.modules.pop("lira.vision.ocr_rknn", None)
    previous_attribute = getattr(lira.vision, "ocr_rknn", None)
    module = importlib.import_module("lira.vision.ocr_rknn")
    engine = object.__new__(module.RknnOcrEngine)
    probability = np.zeros((1, 480, 480, 1), np.float32)
    probability[0, 60:80, 30:300, 0] = 0.99
    probability[0, 200:220, 30:300, 0] = 0.99
    engine._det = Runtime(probability)
    engine._rec = Runtime(np.array([[[0, 1], [1, 0]]], np.float32))
    engine._characters = ["blank", "甲"]
    yield engine
    # This fixture must not leave fake hardware importability in other tests.
    sys.modules.pop("lira.vision.ocr_rknn", None)
    if previous_module is not None:
        sys.modules["lira.vision.ocr_rknn"] = previous_module
    if previous_attribute is not None:
        lira.vision.ocr_rknn = previous_attribute
    else:
        delattr(lira.vision, "ocr_rknn")


def test_real_engine_evidence_exports_exact_inputs_and_missed_line(
    production_engine, tmp_path
):
    engine = production_engine
    det, rec = engine._det, engine._rec
    original_inference = rec.inference

    def sometimes_empty(inputs):
        result = original_inference(inputs)
        if len(rec.inputs) == 2:
            return [np.array([[[1, 0]]], np.float32)]
        return result

    rec.inference = sometimes_empty
    image = np.full((960, 960, 3), (50, 100, 150), np.uint8)
    evidence = read_evidence(engine, image, tmp_path / "evidence")
    assert evidence["success"]
    assert evidence["detected_line_count"] == 2
    assert evidence["recognized_line_count"] == 1
    assert len(evidence["recognition_attempts"]) == 2
    assert evidence["recognition_attempts"][1]["text"] == ""
    assert score("甲甲", evidence["text"])["deletions"] == 1
    assert engine._det is det and engine._rec is rec
    for label, runtime, shape in (
        ("det", det, [1, 480, 480, 3]),
        ("rec", rec, [1, 48, 320, 3]),
    ):
        exported = evidence["inference_inputs"][label]
        assert exported[0]["shape"] == shape
        assert exported[0]["dtype"] == "float32"
        assert np.array_equal(
            np.load(tmp_path / "evidence" / exported[0]["file"]), runtime.inputs[0]
        )
    assert evidence["inference_inputs"]["det"][0]["preview"]


def test_failure_retains_input_and_restores_wrapped_runtime(
    production_engine, tmp_path
):
    engine = production_engine
    runtime = engine._det
    runtime.fail = True
    evidence = read_evidence(
        engine, np.zeros((100, 100, 3), np.uint8), tmp_path / "failure"
    )
    assert not evidence["success"]
    assert "injected" in evidence["error"]
    assert evidence["inference_inputs"]["det"]
    assert engine._det is runtime
    assert "score" not in evidence


def test_partial_model_initialization_releases_first_runtime(
    production_engine, tmp_path, monkeypatch
):
    runtime = Runtime()
    calls = []

    def load(path, core):
        calls.append((path, core))
        if len(calls) == 2:
            raise RuntimeError("second model runtime failed")
        return runtime

    monkeypatch.setattr(type(production_engine), "_load", staticmethod(load))
    det, rec, dictionary = [
        tmp_path / name for name in ("det.rknn", "rec.rknn", "dict.txt")
    ]
    det.write_bytes(b"det")
    rec.write_bytes(b"rec")
    dictionary.write_text("甲\n", encoding="utf-8")
    args = build_parser().parse_args(
        [
            "read",
            "--reference",
            "unused",
            "--det",
            str(det),
            "--rec",
            str(rec),
            "--dictionary",
            str(dictionary),
        ]
    )
    with pytest.raises(RuntimeError, match="second model"):
        create_engine(args)
    assert runtime.released


def test_manifest_hash_validation_and_invalid_restoration(tmp_path):
    image = tmp_path / "baseline.jpg"
    cv2.imwrite(str(image), np.zeros((20, 20, 3), np.uint8))
    manifest = tmp_path / "manifest.json"
    value = {
        "schema": "lira.camera-quality/v1",
        "kind": "baseline",
        "valid": True,
        "capture_ok": True,
        "restore": {"ok": True},
        "scene": {"distance_cm": 20},
        "frames": [{"file": image.name, "sha256": digest(image)}],
    }
    manifest.write_text(json.dumps(value))
    samples = samples_from_manifest(manifest)
    assert samples[0]["group"] == "production-baseline"
    assert samples[0]["scene"]["distance_cm"] == 20
    image.write_bytes(b"changed")
    with pytest.raises(ValueError, match="hash"):
        samples_from_manifest(manifest)
    value["restore"]["ok"] = False
    manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="restoration"):
        samples_from_manifest(manifest)


def test_stages_full_images_only_and_named_selection(tmp_path):
    image = tmp_path / "single.png"
    cv2.imwrite(str(image), np.zeros((20, 20, 3), np.uint8))
    manifest = tmp_path / "manifest.json"
    source = tmp_path / "capture.json"
    source.write_text(json.dumps({"valid": True, "restore": {"ok": True}}))
    manifest.write_text(
        json.dumps(
            {
                "schema": "lira.camera-quality-stages/v1",
                "kind": "stages",
                "valid": True,
                "source_manifest": str(source),
                "source_manifest_sha256": digest(source),
                "outputs": [
                    {
                        "stage": "single-000",
                        "file": image.name,
                        "sha256": digest(image),
                        "rois": {"center": {"file": "not-loaded.png"}},
                    }
                ],
            }
        )
    )
    assert len(samples_from_manifest(manifest, ["single-000"])) == 1
    with pytest.raises(ValueError, match="absent"):
        samples_from_manifest(manifest, ["missing"])
    source.write_text("changed")
    with pytest.raises(ValueError, match="Source capture manifest hash"):
        samples_from_manifest(manifest)


def test_failed_samples_excluded_from_statistics():
    groups = summarize(
        [
            {"group": "a", "success": True, "score": score("甲乙", "甲")},
            {"group": "a", "success": False, "error": "failure"},
            {"group": "b", "success": False},
        ]
    )
    assert groups["a"]["denominator"] == 1
    assert groups["a"]["attempted"] == 2
    assert groups["a"]["mean_cer"] == 0.5
    assert groups["b"]["mean_cer"] is None


def test_score_cli_and_help_load_no_hardware_modules(tmp_path):
    reference, hypothesis = tmp_path / "ref.txt", tmp_path / "hyp.txt"
    reference.write_text("甲乙", encoding="utf-8")
    hypothesis.write_text("甲", encoding="utf-8")
    code = "from lira.diagnostics.ocr_compare import main; import sys; result=main(); assert 'rknnlite.api' not in sys.modules; assert 'lira.vision.ocr_rknn' not in sys.modules; raise SystemExit(result)"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            code,
            "score",
            "--reference",
            str(reference),
            "--text",
            str(hypothesis),
            "--output",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads((Path(result.stdout.strip()) / "report.json").read_text())
    assert report["groups"]["text"]["mean_cer"] == 0.5
    help_result = subprocess.run(
        [sys.executable, "-m", "lira.diagnostics.ocr_compare", "--help"],
        capture_output=True,
        check=False,
    )
    assert help_result.returncode == 0


def test_initialization_failure_writes_failed_samples_and_scope_warning(tmp_path):
    ref, image = tmp_path / "ref.txt", tmp_path / "image.png"
    ref.write_text("甲", encoding="utf-8")
    cv2.imwrite(str(image), np.zeros((20, 20, 3), np.uint8))
    args = build_parser().parse_args(
        [
            "read",
            "--reference",
            str(ref),
            "--image",
            str(image),
            "--reference-scope",
            "body-only",
            "--output",
            str(tmp_path),
        ]
    )

    def fail(_):
        raise FileNotFoundError("model missing")

    assert execute(args, fail) == 1
    report = json.loads(next(tmp_path.glob("ocr-*/report.json")).read_text())
    assert not report["samples"][0]["success"]
    assert "score" not in report["samples"][0]
    assert report["groups"]["unattributed"]["denominator"] == 0
    assert "pinyin" in report["reference_warning"]


def test_read_cli_preserves_extra_text_and_engine_release(production_engine, tmp_path):
    ref, image = tmp_path / "ref.txt", tmp_path / "image.png"
    ref.write_text("甲", encoding="utf-8")
    cv2.imwrite(str(image), np.zeros((960, 960, 3), np.uint8))
    args = build_parser().parse_args(
        [
            "read",
            "--reference",
            str(ref),
            "--image",
            str(image),
            "--expected-lines",
            "2",
            "--output",
            str(tmp_path),
        ]
    )
    assert (
        execute(
            args,
            lambda _: (
                production_engine,
                {"backend": "fake runtime, real production preprocessing"},
            ),
        )
        == 0
    )
    report = json.loads(next(tmp_path.glob("ocr-*/report.json")).read_text())
    assert report["samples"][0]["score"]["insertions"] == 1
    assert report["samples"][0]["expected_physical_lines"] == 2
    assert production_engine._det.released and production_engine._rec.released


@pytest.mark.parametrize(
    "failure", ["invalid-rois", "region-inference", "region-export"]
)
def test_auxiliary_failure_preserves_full_page_score(
    production_engine, tmp_path, monkeypatch, failure
):
    ref, image, rois = [
        tmp_path / name for name in ("ref.txt", "image.png", "rois.json")
    ]
    ref.write_text("甲", encoding="utf-8")
    cv2.imwrite(str(image), np.zeros((960, 960, 3), np.uint8))
    names = ("center", "top_left", "top_right", "bottom_left", "bottom_right")
    rois.write_text(json.dumps({name: [0, 0, 960, 960] for name in names}))
    if failure == "invalid-rois":
        rois.write_text("{}")
    elif failure == "region-inference":
        runtime = production_engine._det
        original_inference = runtime.inference

        def fail_first_region(inputs):
            runtime.fail = len(runtime.inputs) == 1
            return original_inference(inputs)

        monkeypatch.setattr(runtime, "inference", fail_first_region)
    else:
        original_mkdir = Path.mkdir

        def fail_first_region_export(path, *args, **kwargs):
            if path.name.endswith("-roi-center"):
                raise OSError("injected region export failure")
            return original_mkdir(path, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", fail_first_region_export)
    args = build_parser().parse_args(
        [
            "read",
            "--reference",
            str(ref),
            "--image",
            str(image),
            "--rois",
            str(rois),
            "--output",
            str(tmp_path),
        ]
    )
    assert (
        execute(args, lambda _: (production_engine, {"backend": "production test"}))
        == 1
    )
    report = json.loads(next(tmp_path.glob("ocr-*/report.json")).read_text())
    sample = report["samples"][0]
    assert sample["success"] and sample["score"]["cer"] == 1
    assert "error" not in sample
    assert report["groups"]["unattributed"]["denominator"] == 1
    assert report["groups"]["unattributed"]["mean_cer"] == 1
    assert report["primary_success"] and not report["success"]
    assert not report["auxiliary_success"] and not sample["auxiliary_success"]
    if failure == "invalid-rois":
        assert "Exactly five" in sample["auxiliary_error"]
    else:
        regions = sample["auxiliary_roi_ocr"]
        assert not regions["center"]["success"]
        assert "injected" in regions["center"]["error"]
        assert "score" not in regions["center"]
        assert all(regions[name]["success"] for name in names[1:])
