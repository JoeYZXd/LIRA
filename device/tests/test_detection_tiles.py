import cv2
import numpy as np

from lira.vision.detection_tiles import detect_document, merge_boxes


def box(x, y, w, h):
    return np.float32([[x, y], [x+w, y], [x+w, y+h], [x, y+h]])


def test_small_image_uses_one_pass():
    calls = []
    def detector(image):
        calls.append(image.shape)
        return [box(10, 20, 100, 30)]
    result = detect_document(np.zeros((960, 960, 3), np.uint8), detector)
    assert len(calls) == len(result) == 1


def test_tiles_cover_page_and_restore_global_coordinates():
    image = np.zeros((2400, 1800, 3), np.int32)
    # Pixel channels encode coordinates to observe the actual tile origins.
    image[:, :, 0] = np.arange(1800)[None, :]
    image[:, :, 1] = np.arange(2400)[:, None]
    coverage = np.zeros(image.shape[:2], bool)
    origins = []
    def detector(crop):
        if crop.shape == image.shape:
            return []
        x, y = int(crop[0, 0, 0]), int(crop[0, 0, 1])
        assert max(crop.shape[:2]) <= 1024
        origins.append((x, y))
        coverage[y:y+crop.shape[0], x:x+crop.shape[1]] = True
        return [box(100, 100, 100, 20)]
    result = detect_document(image, detector)
    assert coverage.all()
    for x, y in origins:
        assert any(np.allclose(p, box(x+100, y+100, 100, 20)) for p in result)


def test_duplicate_and_seam_fragments_merge_without_merging_neighbor_lines():
    result = merge_boxes([
        box(0, 0, 180, 20), box(100, 0, 180, 20),
        box(5, 1, 170, 18), box(0, 30, 280, 20),
        box(350, 0, 100, 20),
    ])
    assert len(result) == 3
    assert any(np.allclose(p, box(0, 0, 280, 20)) for p in result)


def test_empty_image_does_not_call_detector():
    assert detect_document(np.empty((0, 0, 3)), lambda _: 1 / 0) == []


def test_tilted_neighbor_lines_remain_separate():
    angle = np.radians(8)
    rotation = np.float32([[np.cos(angle), -np.sin(angle)],
                          [np.sin(angle), np.cos(angle)]])
    first = box(0, 0, 900, 20) @ rotation.T
    next_line = box(0, 35, 900, 20) @ rotation.T
    assert len(merge_boxes([first, next_line])) == 2


def test_local_views_recover_detail_lost_by_full_page_resize():
    image = np.full((2400, 1800, 3), 255, np.uint8)
    image[1000:1016, 900:1100] = 0
    def detector(crop):
        small = cv2.resize(crop, (480, 480))
        rows = np.where((small[:, :, 0] < 128).any(axis=1))[0]
        if len(rows) < 5:
            return []
        # Return its original crop coordinates, like DB postprocessing does.
        points = cv2.findNonZero((crop[:, :, 0] < 128).astype(np.uint8))
        x, y, w, h = cv2.boundingRect(points)
        return [box(x, y, w, h)]
    assert detector(image) == []
    result = detect_document(image, detector)
    assert len(result) == 1
    assert np.allclose(result[0], box(900, 1000, 200, 16))
