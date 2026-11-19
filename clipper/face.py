"""Suggest crop rectangles: the streamer's face cam and the gameplay area.
Rects are normalised [x, y, w, h] in 0..1 of the source frame. The user adjusts them in the dashboard."""
import subprocess
import warnings

import cv2
import numpy as np


def _frames(path, duration, n=14):
    out = []
    for i in range(n):
        t = duration * (i + 0.5) / n
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                              "-vf", "scale=960:-2", "-f", "image2pipe", "-vcodec", "png", "-"],
                             capture_output=True).stdout
        if raw:
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if img is not None:
                out.append(img)
    return out


def _fit(cx, cy, w, h, aspect):
    """Rect with the given aspect (w/h, in source pixels) around a centre, kept inside the frame."""
    if w / h > aspect:
        h = w / aspect
    else:
        w = h * aspect
    k = max(w, h, 1.0)          # shrink, keeping the aspect, until it fits in the frame
    w, h = w / k, h / k
    x = min(max(cx - w / 2, 0), 1 - w)
    y = min(max(cy - h / 2, 0), 1 - h)
    return [round(x, 4), round(y, 4), round(w, 4), round(h, 4)]


def suggest(path, info, split=0.4):
    """Returns {"face": rect|None, "game": rect, "found": bool}.
    split = share of the 1080x1920 frame given to the face band."""
    W, H = info["width"], info["height"]
    face_aspect = (1080 / (1920 * split)) / (W / H)        # normalised-units aspect for the face band
    game_aspect = (1080 / (1920 * (1 - split))) / (W / H)
    game = _fit(0.5, 0.5, game_aspect, 1.0, game_aspect)   # tallest centred crop of the gameplay

    def no_face():
        return {"face": _fit(0.85, 0.2, 0.3, 0.3 / face_aspect, face_aspect),
                "game": game, "found": False}

    try:
        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        # OpenCV can return an empty classifier when its cascade XML is missing/unreadable.
        if cascade.empty():
            warnings.warn("OpenCV face cascade failed to load; using gameplay-only crop.", RuntimeWarning)
            return no_face()

        boxes = []
        for img in _frames(path, info["duration"]):
            g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            ih, iw = g.shape
            detections = cascade.detectMultiScale(g, 1.1, 6, minSize=(max(24, iw // 40),) * 2)
            for (x, y, w, h) in detections:
                boxes.append(((x + w / 2) / iw, (y + h / 2) / ih, w / iw, h / ih))
    except cv2.error as e:
        warnings.warn(f"OpenCV face detection failed ({e}); using gameplay-only crop.", RuntimeWarning)
        return no_face()

    if not boxes:
        return no_face()

    # the streamer's face: seen in many frames at about the same place AND big
    # (count x size, so a poster or a small face in the gameplay loses to the face cam)
    b = np.array(boxes)
    best, best_v, tol = None, 0.0, None
    for cx, cy, w, h in b:
        t = max(0.03, w * 0.6)
        near = (np.abs(b[:, 0] - cx) < t) & (np.abs(b[:, 1] - cy) < t)
        v = int(np.sum(near)) * float(np.median(b[near, 2]))
        if v > best_v:
            best, best_v, tol = (cx, cy, w, h), v, t
    near = b[(np.abs(b[:, 0] - best[0]) < tol) & (np.abs(b[:, 1] - best[1]) < tol)]
    cx, cy, fw, fh = np.median(near, axis=0)
    # head-and-shoulders framing: ~3.2 face widths wide, face a little above centre
    face = _fit(cx, cy + fh * 0.35, fw * 3.2, fh * 2.6, face_aspect)
    return {"face": face, "game": game, "found": True}
