"""
council_core.event_analysis — the analyses of Andy's "Prophesee Integrated
6 Tabs (smoke plume, laser proxy)" script, without its Tk window, for the
Typhon plugin typhon.event_analysis (typhon_plugins/event_analysis/,
guide docs/typhon_event_analysis.md; Andy, 25 November 2026).

WHAT IS HERE (each the script's own code, kept line for line where it
could be)
  1. A dot-grid homography: scan a recording into an accumulation picture
     (accumulation), find candidate dots (find_blobs), fit pixels -> mm
     (solve_homography, auto_match), and the dot labelling the window does
     by clicking (DotLabels), saved as the script's JSON (calibration_record).
  2. Rectify a recording to a perspective-corrected MP4 (rectify).
  3. Velocity arrows of event clusters, MP4 + CSV (make_velocity_video).
  4. Melt-pool paths per layer, PNGs + CSV + MP4 (analyze_meltpool).
  5. Raindrops behind frosted glass, MP4 + CSV + trail PNG
     (analyze_raindrops).
  6. Smoke plumes: optical-flow disturbances, object candidates and the
     optical obscuration / event-activity proxy, MP4 + CSVs + heatmaps
     (analyze_smoke).

WHAT CHANGED FROM THE SCRIPT
  * Reading events (open_events). The script used metavision_core's
    EventsIterator, which needs h5py and writes "<name>.raw.tmp_index"
    beside a .raw (council_core/event_playback.py says why Typhon avoids
    it). Here a .raw is read with the HAL alone (metavision_hal, OpenEB),
    as Typhon's raw view reads it, in the same fixed slices EventsIterator
    gave (delta_t, counted from the time-shifted start, empty slices
    included). A .dat (which the HAL cannot read) still goes through
    EventsIterator. A .npz of events (x, y, p, t, width, height) is read
    too, so simulated events can be analysed with no SDK at all.
  * No cv2.imshow: every analysis hands its annotated picture to `show`
    (the plugin's window shows it), and `log` / `stop` are the plugin's.
  * Nothing is overwritten: an output that exists is refused (rectify) or
    given the next free name (available_pair, as in the script).

No Qt and no tkinter here: numpy and OpenCV (cv2) only. Without them the
module still imports, and missing() says what to install.
"""
from __future__ import annotations

import bisect
import csv
import json
import math
import os
import re
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

try:                                    # numpy and OpenCV: the plugin's needs
    import cv2
    import numpy as np
except ImportError:                     # pragma: no cover - said by the host
    cv2 = None                          # type: ignore[assignment]
    np = None                           # type: ignore[assignment]

EVENT_EXT = {'.raw', '.dat', '.npz'}
VIDEO_EXT = {'.avi', '.mp4', '.mkv', '.mov'}
POLARITIES = {'Positive (ON)': 1, 'Negative (OFF)': 0, 'Both': -1}
SPEEDS = {'0.1x': .1, '0.25x': .25, '0.5x': .5, '1x': 1., '2x': 2., '4x': 4., '8x': 8.}

#: The fields of one event, as Metavision gives them (EventCD).
EVENT_DTYPE = [('x', '<u2'), ('y', '<u2'), ('p', '<i2'), ('t', '<i8')]


def _noshow(frame: Any) -> None:
    return None


# ================================================================
# READING EVENTS
# ================================================================
class RawUnavailable(RuntimeError):
    """The recording cannot be read here (no SDK, or not a recording)."""


def _slices(batches: Iterator[Any], slice_us: int) -> Iterator[Any]:
    """Time-ordered batches of events -> one array per `slice_us` slice,
    slice k holding t in [k*slice_us, (k+1)*slice_us), empty slices
    included: EventsIterator's mode='delta_t'."""
    carry = np.zeros(0, EVENT_DTYPE)
    k = 0
    for batch in batches:
        if not len(batch):
            continue
        events = np.concatenate([carry, batch]) if len(carry) else batch
        ks = events['t'].astype(np.int64) // slice_us
        last = int(ks[-1])
        while k < last:
            lo = int(np.searchsorted(ks, k, 'left'))
            hi = int(np.searchsorted(ks, k, 'right'))
            yield events[lo:hi]
            k += 1
        carry = events[ks >= last]
    if len(carry):
        ks = carry['t'].astype(np.int64) // slice_us
        last = int(ks[-1])
        while k <= last:
            yield carry[ks == k]
            k += 1


def _npz_events(path: str, slice_us: int) -> Tuple[Iterator[Any], int, int]:
    with np.load(path, allow_pickle=False) as data:
        names = set(data.files)
        if not {'x', 'y', 'p', 't'} <= names:
            raise RawUnavailable(f'{os.path.basename(path)} holds no events '
                                 f'(it needs arrays x, y, p and t)')
        events = np.zeros(len(data['t']), EVENT_DTYPE)
        for key in ('x', 'y', 'p', 't'):
            events[key] = data[key]
        width = int(data['width']) if 'width' in names else int(events['x'].max(initial=0)) + 1
        height = int(data['height']) if 'height' in names else int(events['y'].max(initial=0)) + 1
    events = events[np.argsort(events['t'], kind='stable')]
    if len(events) and int(events['t'][0]) < 0:
        raise RawUnavailable('event times must not be negative')
    return _slices(iter([events]), slice_us), height, width


def _hal_events(path: str, slice_us: int, hal: Any = None
                ) -> Tuple[Iterator[Any], int, int]:
    """A .raw through the HAL only (no index file written beside it, no
    h5py), time-shifted to start near zero as EventsIterator shifts it."""
    if hal is None:
        import metavision_hal as hal            # noqa: F811 - the SDK
    config = hal.RawFileConfig()
    config.build_index = False
    config.do_time_shifting = True
    device = hal.DeviceDiscovery.open_raw_file(str(path), config)
    if device is None:
        raise RawUnavailable(f'cannot open {os.path.basename(path)}')
    geometry = device.get_i_geometry()
    width, height = int(geometry.get_width()), int(geometry.get_height())

    def batches() -> Iterator[Any]:
        nonlocal device
        stream = device.get_i_events_stream()
        decoder = device.get_i_events_stream_decoder()
        cd = device.get_i_event_cd_decoder()
        got: List[Any] = []
        cd.add_event_buffer_callback(lambda buf: got.append(np.array(buf, copy=True)))
        stream.start()
        try:
            while True:
                ready = stream.poll_buffer()
                if ready < 0:
                    break
                if ready == 0:
                    time.sleep(0.0005)
                    continue
                decoder.decode(stream.get_latest_raw_data())
                while got:
                    yield got.pop(0)
        finally:
            try:
                stream.stop()
            except Exception:                   # noqa: BLE001
                pass
            device = None
    return _slices(batches(), slice_us), height, width


def _core_events(path: str, slice_us: int) -> Tuple[Iterator[Any], int, int]:
    """The script's own reader: metavision_core's EventsIterator."""
    try:
        from metavision_core.event_io import EventsIterator
    except ImportError as exc:
        raise RawUnavailable(
            'reading a .dat needs the Metavision Python SDK '
            '(metavision_core), which is not importable in this Python'
        ) from exc
    iterator = EventsIterator(input_path=path, mode='delta_t', delta_t=slice_us)
    h, w = iterator.get_size()
    if not h or not w:
        raise RawUnavailable('could not obtain the event sensor size')
    return iter(iterator), int(h), int(w)


def open_events(path: str, slice_us: int) -> Tuple[Iterator[Any], int, int]:
    """(slices, height, width) of an event recording: .raw by the HAL
    (else EventsIterator), .dat by EventsIterator, .npz by numpy."""
    slice_us = int(slice_us)
    if slice_us < 1:
        raise ValueError('The event slice must be at least 1 microsecond.')
    if not os.path.isfile(path):
        raise RawUnavailable(f'{path} does not exist')
    ext = os.path.splitext(path)[1].lower()
    if ext == '.npz':
        return _npz_events(path, slice_us)
    if ext == '.raw':
        try:
            import metavision_hal                   # noqa: F401
        except ImportError:
            try:
                return _core_events(path, slice_us)
            except RawUnavailable as exc:
                raise RawUnavailable(
                    'reading a .raw needs the Metavision SDK (metavision_hal '
                    'from OpenEB, or metavision_core), which is not '
                    'installed for this Python') from exc
        return _hal_events(path, slice_us)
    if ext == '.dat':
        return _core_events(path, slice_us)
    raise ValueError('Unsupported event recording (use .raw, .dat or .npz).')


#: The script's name for the reader, kept so its functions read as before.
def event_iterator(path, slice_us):
    return open_events(path, slice_us)


def save_events_npz(path: str, events: Any, width: int, height: int) -> str:
    """Events (a structured array with x, y, p, t, or a dict of arrays) as
    the .npz open_events reads: simulated recordings, and tests."""
    x = np.asarray(events['x'])
    np.savez_compressed(path, x=x.astype(np.uint16),
                        y=np.asarray(events['y']).astype(np.uint16),
                        p=np.asarray(events['p']).astype(np.int16),
                        t=np.asarray(events['t']).astype(np.int64),
                        width=np.int64(width), height=np.int64(height))
    return path


def contrast_image(counts):
    data = np.log1p(counts.astype(np.float32))
    active = data[data > 0]
    if not active.size:
        return np.zeros(counts.shape, np.uint8)
    upper = float(np.percentile(active, 99.5))
    return np.uint8(np.clip(data / max(upper, 1e-10), 0, 1) * 255)


def accumulation(path, slice_us, window_slices, polarity, stop, log):
    ext = os.path.splitext(path)[1].lower()
    total = peak = current = None
    count = 0
    source_shape = None
    if ext in EVENT_EXT:
        iterator, h, w = event_iterator(path, slice_us)
        source_shape = (h, w)
        total = np.zeros((h, w), np.uint64)
        current = np.zeros((h, w), np.uint32)
        peak = np.zeros((h, w), np.uint32)
        for i, events in enumerate(iterator):
            if stop.is_set():
                raise RuntimeError('Calibration scan cancelled.')
            if polarity != -1:
                events = events[events['p'] == polarity]
            if len(events):
                xs = events['x'].astype(np.intp)
                ys = events['y'].astype(np.intp)
                mask = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
                xs, ys = xs[mask], ys[mask]
                np.add.at(total, (ys, xs), 1)
                np.add.at(current, (ys, xs), 1)
            count += 1
            if count % window_slices == 0:
                np.maximum(peak, current, out=peak)
                current.fill(0)
            if count % 200 == 0:
                log(f'Calibration: {count:,} event slices scanned')
        np.maximum(peak, current, out=peak)
    elif ext in VIDEO_EXT:
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            raise RuntimeError('Unable to open conventional video.')
        previous = None
        try:
            while not stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    break
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                if total is None:
                    h, w = gray.shape
                    source_shape = (h, w)
                    total = np.zeros((h, w), np.uint64)
                    peak = np.zeros((h, w), np.uint32)
                if previous is not None:
                    delta = cv2.absdiff(gray, previous)
                    total += delta.astype(np.uint64)
                    np.maximum(peak, delta.astype(np.uint32), out=peak)
                previous = gray
                count += 1
                if count % 200 == 0:
                    log(f'Calibration: {count:,} video frames scanned')
        finally:
            cap.release()
        if stop.is_set():
            raise RuntimeError('Calibration scan cancelled.')
    else:
        raise ValueError('Unsupported calibration recording extension.')
    if total is None or count == 0:
        raise ValueError('The recording contains no usable data.')
    return {'total': contrast_image(total), 'peak': contrast_image(peak),
            'width': source_shape[1], 'height': source_shape[0], 'samples': count}


def find_blobs(gray, minimum, maximum):
    clahe = cv2.createCLAHE(clipLimit=2, tileGridSize=(8, 8))
    candidates = []
    for image in (gray, clahe.apply(gray)):
        _, binary = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        for test in (binary, cv2.bitwise_not(binary)):
            n, _, stats, centers = cv2.connectedComponentsWithStats(test, 8)
            for i in range(1, n):
                x, y, w, h, area = stats[i]
                if not (minimum <= area <= maximum) or w < 2 or h < 2:
                    continue
                if max(w / h, h / w) > 2 or x == 0 or y == 0:
                    continue
                if x + w >= gray.shape[1] or y + h >= gray.shape[0]:
                    continue
                point = centers[i]
                if all(np.linalg.norm(point - existing) > 5 for existing in candidates):
                    candidates.append(point)
    return np.asarray(candidates, np.float64).reshape(-1, 2)


def solve_homography(points, spacing):
    if len(points) < 4:
        raise ValueError('Label at least four distinct, non-collinear dots.')
    img = np.asarray([[p['x'], p['y']] for p in points], np.float64)
    world = np.asarray([[p['col'] * spacing, p['row'] * spacing] for p in points], np.float64)
    if len(np.unique(img, axis=0)) < 4 or len(np.unique(world, axis=0)) < 4:
        raise ValueError('Duplicate dot coordinates or row/column labels.')
    if np.linalg.matrix_rank(img - img.mean(0)) < 2 or np.linalg.matrix_rank(world - world.mean(0)) < 2:
        raise ValueError('Dot centers and their physical coordinates must span two dimensions.')
    H, mask = cv2.findHomography(img, world, cv2.RANSAC, 1.0)
    if H is None or not np.isfinite(H).all() or abs(np.linalg.det(H)) < 1e-14:
        raise ValueError('Homography fit failed or is degenerate.')
    actual = cv2.perspectiveTransform(img.reshape(-1, 1, 2), H).reshape(-1, 2)
    error = np.linalg.norm(actual - world, axis=1)
    return {'H': H, 'inverse': np.linalg.inv(H),
            'rms': float(np.sqrt(np.mean(error ** 2))),
            'max_error': float(np.max(error)), 'inliers': int(mask.sum()) if mask is not None else len(points)}


def auto_match(points, candidates, fit, rows, cols, spacing, radius):
    known = {(p['row'], p['col']) for p in points}
    sites = [(r, c) for r in range(rows) for c in range(cols) if (r, c) not in known]
    if not len(candidates) or not sites:
        return list(points), 0
    world = np.asarray([[[c * spacing, r * spacing]] for r, c in sites], np.float64)
    preds = cv2.perspectiveTransform(world, fit['inverse']).reshape(-1, 2)
    occupied = {i for i, p in enumerate(candidates)
                if any(np.linalg.norm(p - [a['x'], a['y']]) <= radius for a in points)}
    matches = []
    for j, p in enumerate(preds):
        if not np.isfinite(p).all():
            continue
        for i in range(len(candidates)):
            if i in occupied:
                continue
            d = float(np.linalg.norm(p - candidates[i]))
            if d <= radius:
                matches.append((d, j, i))
    matches.sort()
    used_sites = set()
    result = list(points)
    for _, j, i in matches:
        if j in used_sites or i in occupied:
            continue
        row, col = sites[j]
        result.append({'row': row, 'col': col, 'x': float(candidates[i, 0]),
                       'y': float(candidates[i, 1]), 'automatic': True})
        occupied.add(i)
        used_sites.add(j)
    return result, len(used_sites)


def load_calibration(path):
    with open(path, 'r', encoding='utf-8') as handle:
        data = json.load(handle)
    H = np.asarray(data['Homography Matrix'], np.float64)
    if H.shape != (3, 3) or not np.isfinite(H).all() or abs(np.linalg.det(H)) < 1e-14:
        raise ValueError('Invalid homography matrix in JSON.')
    if data.get('homography_direction', 'image_pixels_to_world_mm') != 'image_pixels_to_world_mm':
        raise ValueError('Expected a pixels-to-world-mm homography.')
    return data, H


def check_resolution(data, width, height):
    if (int(data['image_width']), int(data['image_height'])) != (width, height):
        raise ValueError('Recording resolution differs from calibration; do not apply H without adjusting pixel coordinates.')


def world_xy(H, x, y):
    p = cv2.perspectiveTransform(np.array([[[x, y]]], np.float64), H)[0, 0]
    return float(p[0]), float(p[1])


def event_frame(events, height, width, polarity):
    image = np.zeros((height, width, 3), np.uint8)
    if polarity != -1:
        events = events[events['p'] == polarity]
    if len(events):
        xs, ys = events['x'].astype(np.intp), events['y'].astype(np.intp)
        good = (xs >= 0) & (ys >= 0) & (xs < width) & (ys < height)
        image[ys[good], xs[good]] = (255, 255, 255)
    return image


def safe_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', value.strip())
    value = os.path.splitext(value)[0].rstrip(' .')
    if not value:
        raise ValueError('Output filename must not be empty.')
    return value


def available_pair(folder, basename):
    n = 0
    while True:
        prefix = basename if n == 0 else f'{basename}_{n:03d}'
        paths = [os.path.join(folder, prefix + suffix) for suffix in ('.mp4', '.csv')]
        if not any(os.path.exists(p) for p in paths):
            return paths
        n += 1


def rectify(path, json_path, output_path, pixel_scale, margin, slice_us, polarity, stop, log, show=_noshow):
    data, H = load_calibration(json_path)
    rows, cols = int(data['grid_rows']), int(data['grid_columns'])
    spacing = float(data['dot_spacing_mm'])
    wout = int(math.ceil(((cols - 1) * spacing + 2 * margin) * pixel_scale))
    hout = int(math.ceil(((rows - 1) * spacing + 2 * margin) * pixel_scale))
    if min(wout, hout) < 2 or wout * hout > 25000000:
        raise ValueError('Rectified output dimensions are invalid or excessive.')
    A = np.array([[pixel_scale, 0, margin * pixel_scale],
                  [0, pixel_scale, margin * pixel_scale], [0, 0, 1]], np.float64)
    T = A @ H
    ext = os.path.splitext(path)[1].lower()
    cap = None
    if ext in EVENT_EXT:
        iterator, height, width = event_iterator(path, slice_us)
        fps = 1e6 / slice_us
        frames = ((event_frame(ev, height, width, polarity) for ev in iterator))
    elif ext in VIDEO_EXT:
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            raise RuntimeError('Cannot open video.')
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        fps = cap.get(cv2.CAP_PROP_FPS)
        if fps <= 0:
            raise ValueError('Video FPS metadata is invalid.')
        def video_frames():
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                yield frame
        frames = video_frames()
    else:
        raise ValueError('Unsupported input recording.')
    try:
        check_resolution(data, width, height)
        if os.path.exists(output_path):
            raise FileExistsError(f'{output_path} exists; it is never overwritten.')
        writer = cv2.VideoWriter(output_path, cv2.VideoWriter_fourcc(*'mp4v'), fps, (wout, hout))
        if not writer.isOpened():
            raise RuntimeError('Could not create MP4 writer.')
        try:
            for n, frame in enumerate(frames, 1):
                if stop.is_set():
                    log('Rectification stopped.')
                    break
                corrected = cv2.warpPerspective(frame, T, (wout, hout))
                writer.write(corrected)
                show(corrected)
                if n % 100 == 0:
                    log(f'Rectified {n:,} frames')
        finally:
            writer.release()
    finally:
        if cap is not None:
            cap.release()
    log(f'Rectified video saved: {output_path}')


def detect_clusters(events, height, width, polarity, min_area, min_events, join_radius):
    binary = np.zeros((height, width), np.uint8)
    if polarity != -1:
        events = events[events['p'] == polarity]
    if not len(events):
        return binary, []
    xs, ys = events['x'].astype(np.intp), events['y'].astype(np.intp)
    good = (xs >= 0) & (ys >= 0) & (xs < width) & (ys < height)
    xs, ys = xs[good], ys[good]
    if not len(xs):
        return binary, []
    binary[ys, xs] = 255
    if join_radius:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * join_radius + 1,) * 2)
        grouped = cv2.dilate(binary, kernel)
    else:
        grouped = binary
    n, labels, stats, centers = cv2.connectedComponentsWithStats(grouped, 8)
    counts = np.bincount(labels[ys, xs], minlength=n)
    output = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area >= min_area and counts[i] >= min_events:
            output.append(dict(cx=float(centers[i, 0]), cy=float(centers[i, 1]),
                               area=int(area), events=int(counts[i]), bbox=(int(x), int(y), int(w), int(h))))
    return binary, output


class Tracker:
    def __init__(self, radius):
        self.radius, self.previous, self.next_id = radius, [], 1

    def update(self, clusters, time_s):
        current = [dict(c, id=None, previous=None, vx=float('nan'), vy=float('nan'), matched=False) for c in clusters]
        matches = []
        for i, old in enumerate(self.previous):
            dt = time_s - old['time']
            if dt <= 0:
                continue
            for j, obj in enumerate(current):
                dx, dy = obj['cx'] - old['cx'], obj['cy'] - old['cy']
                d = math.hypot(dx, dy)
                if d <= self.radius:
                    matches.append((d, i, j, dt, dx, dy))
        used_old, used_new = set(), set()
        for _, i, j, dt, dx, dy in sorted(matches):
            if i in used_old or j in used_new:
                continue
            old, obj = self.previous[i], current[j]
            obj.update(id=old['id'], previous=(old['cx'], old['cy']),
                       vx=dx / dt, vy=dy / dt, matched=True, dt=dt)
            used_old.add(i)
            used_new.add(j)
        for obj in current:
            if obj['id'] is None:
                obj['id'] = self.next_id
                self.next_id += 1
        self.previous = [dict(id=o['id'], cx=o['cx'], cy=o['cy'], time=time_s) for o in current]
        return current


def speed_color(speed, max_speed):
    fraction = np.uint8([[round(255 * min(max(speed / max_speed, 0.), 1.))]])
    return tuple(int(x) for x in cv2.applyColorMap(fraction, cv2.COLORMAP_JET)[0, 0])


def draw_arrow(frame, obj, vx, vy, speed, max_speed):
    if not np.isfinite(speed) or speed <= 0:
        return
    scale = min(speed / max_speed, 1.)
    distance = 12 + 68 * scale
    start = (round(obj['cx']), round(obj['cy']))
    end = (round(obj['cx'] + vx / speed * distance), round(obj['cy'] + vy / speed * distance))
    color = speed_color(speed, max_speed)
    cv2.arrowedLine(frame, start, end, color, 2, cv2.LINE_AA, tipLength=.25)
    cv2.circle(frame, start, 3, color, -1)


def draw_legend(frame, max_speed, unit):
    h, w = frame.shape[:2]
    bar = min(220, w - 30)
    if bar < 60 or h < 90:
        return
    x, y = 15, h - 45
    cv2.rectangle(frame, (5, y - 24), (min(w - 1, x + bar + 9), h - 2), (25, 25, 25), -1)
    cv2.putText(frame, f'Speed ({unit})', (x, y - 7), cv2.FONT_HERSHEY_SIMPLEX, .42, (255, 255, 255), 1)
    for i in range(bar):
        cv2.line(frame, (x + i, y), (x + i, y + 11), speed_color(i / max(1, bar - 1) * max_speed, max_speed), 1)
    cv2.putText(frame, '0', (x, y + 26), cv2.FONT_HERSHEY_SIMPLEX, .4, (255, 255, 255), 1)
    cv2.putText(frame, f'{max_speed:g}', (max(0, x + bar - 60), y + 26), cv2.FONT_HERSHEY_SIMPLEX, .4, (255, 255, 255), 1)


def make_velocity_video(path, folder, name, settings, calibration_path, stop, log, show=_noshow):
    iterator, height, width = event_iterator(path, settings['slice_us'])
    data = H = None
    if calibration_path:
        data, H = load_calibration(calibration_path)
        check_resolution(data, width, height)
    os.makedirs(folder, exist_ok=True)
    video_path, csv_path = available_pair(folder, safe_name(name))
    fps = settings['fps']
    writer = cv2.VideoWriter(video_path, cv2.VideoWriter_fourcc(*'mp4v'), fps, (width, height))
    if not writer.isOpened():
        raise RuntimeError('Video writer could not be initialized.')
    tracker = Tracker(settings['match_radius'])
    unit = 'mm/s' if H is not None else 'px/s'
    fields = ['Slice', 'Time_s', 'Object_ID', 'Area_px2', 'Event_Count',
              'Center_X_px', 'Center_Y_px', 'VX_px_s', 'VY_px_s', 'Speed_px_s',
              'World_X_mm', 'World_Y_mm', 'VX_mm_s', 'VY_mm_s', 'Speed_mm_s']
    written = 0
    try:
        with open(csv_path, 'w', newline='', encoding='utf-8') as handle:
            csv_writer = csv.DictWriter(handle, fieldnames=fields)
            csv_writer.writeheader()
            for i, events in enumerate(iterator):
                if stop.is_set():
                    log('Velocity analysis stopped.')
                    break
                time_s = (i + .5) * settings['slice_us'] / 1e6
                binary, clusters = detect_clusters(events, height, width, settings['polarity'],
                                                    settings['min_area'], settings['min_events'], settings['join_radius'])
                objects = tracker.update(clusters, time_s)
                keep = max(1, math.ceil(len(objects) * settings['top_percent'] / 100)) if objects else 0
                selected = sorted(objects, key=lambda o: o['area'], reverse=True)[:keep]
                frame = cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)
                arrows = 0
                for obj in selected:
                    if not obj['matched']:
                        continue
                    vx_px, vy_px = obj['vx'], obj['vy']
                    spx = math.hypot(vx_px, vy_px)
                    vals = dict(Slice=i, Time_s=time_s, Object_ID=obj['id'],
                                Area_px2=obj['area'], Event_Count=obj['events'],
                                Center_X_px=obj['cx'], Center_Y_px=obj['cy'],
                                VX_px_s=vx_px, VY_px_s=vy_px, Speed_px_s=spx,
                                World_X_mm='', World_Y_mm='', VX_mm_s='', VY_mm_s='', Speed_mm_s='')
                    shown_vx, shown_vy, shown_speed = vx_px, vy_px, spx
                    if H is not None:
                        x0, y0 = world_xy(H, *obj['previous'])
                        x1, y1 = world_xy(H, obj['cx'], obj['cy'])
                        vx_mm = (x1 - x0) / obj['dt']
                        vy_mm = (y1 - y0) / obj['dt']
                        smm = math.hypot(vx_mm, vy_mm)
                        vals.update(World_X_mm=x1, World_Y_mm=y1,
                                    VX_mm_s=vx_mm, VY_mm_s=vy_mm, Speed_mm_s=smm)
                        # World-vector direction must be projected back to pixel space
                        # for correct on-image arrow orientation under perspective.
                        world_arrow = np.array([[[x1 + vx_mm / max(smm, 1e-9),
                                                  y1 + vy_mm / max(smm, 1e-9)]]], np.float64)
                        projection = cv2.perspectiveTransform(world_arrow, np.linalg.inv(H))[0, 0]
                        display_dx = projection[0] - obj['cx']
                        display_dy = projection[1] - obj['cy']
                        norm = math.hypot(display_dx, display_dy)
                        if norm > 1e-9:
                            shown_vx, shown_vy = (display_dx / norm * smm, display_dy / norm * smm)
                        shown_speed = smm
                    color = speed_color(shown_speed, settings['max_speed'])
                    draw_arrow(frame, obj, shown_vx, shown_vy, shown_speed, settings['max_speed'])
                    x, y, w, h = obj['bbox']
                    cv2.rectangle(frame, (x, y), (x + w, y + h), color, 1)
                    label = f'{shown_speed:.1f} {unit}'
                    cv2.putText(frame, label, (min(width - 1, max(0, int(obj['cx']) + 5)),
                                               min(height - 1, max(15, int(obj['cy']) - 6))),
                                cv2.FONT_HERSHEY_SIMPLEX, .4, color, 1)
                    csv_writer.writerow(vals)
                    arrows += 1
                cv2.putText(frame, f'Time {time_s:.3f}s | Top {settings["top_percent"]:g}% | Arrows {arrows}',
                            (12, 25), cv2.FONT_HERSHEY_SIMPLEX, .46, (255, 255, 255), 1)
                cv2.putText(frame, f'Playback {settings["playback"]:g}x | {unit}',
                            (12, 48), cv2.FONT_HERSHEY_SIMPLEX, .46, (255, 255, 255), 1)
                draw_legend(frame, settings['max_speed'], unit)
                target = math.floor((i + 1) * settings['slice_us'] / 1e6 * fps / settings['playback'] + 1e-9)
                while written < target and not stop.is_set():
                    writer.write(frame)
                    written += 1
                show(frame)
                if (i + 1) % 100 == 0:
                    log(f'Velocity: {i + 1:,} slices; {written:,} output frames')
    finally:
        writer.release()
    log(f'Video saved: {video_path}')
    log(f'CSV saved: {csv_path}')


# ================================================================
# MELT-POOL TRAJECTORIES AND PER-LAYER OUTPUTS
# ================================================================

def load_layer_triggers(path, offset_s=0.0):
    """CSV columns: time_s,layer_id. Times mark the START of each layer.

    The first layer is assumed to start at 0 seconds unless the CSV
    explicitly includes a layer start at/before 0.
    """
    if not path:
        return [(0.0, '1')]
    records = []
    with open(path, newline='', encoding='utf-8-sig') as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not {'time_s', 'layer_id'}.issubset(reader.fieldnames):
            raise ValueError('Trigger CSV must have columns time_s,layer_id.')
        for row in reader:
            t = float(row['time_s']) + offset_s
            label = str(row['layer_id']).strip()
            if not math.isfinite(t) or not label:
                raise ValueError('Invalid trigger time or layer_id.')
            records.append((t, label))
    if not records:
        raise ValueError('Trigger CSV has no rows.')
    records.sort(key=lambda r: r[0])
    if len({t for t, _ in records}) != len(records):
        raise ValueError('Duplicate layer-start times.')
    if records[0][0] > 0:
        records.insert(0, (0.0, 'pre_trigger'))
    return records


def weighted_velocity(history):
    """Least-squares velocity from recent (t, X_mm, Y_mm) samples."""
    if len(history) < 2:
        return None
    a = np.asarray(history, np.float64)
    t = a[:, 0] - a[:, 0].mean()
    denominator = float(np.dot(t, t))
    if denominator < 1e-16:
        return None
    vx = float(np.dot(t, a[:, 1] - a[:, 1].mean()) / denominator)
    vy = float(np.dot(t, a[:, 2] - a[:, 2].mean()) / denominator)
    return vx, vy


def make_melt_geometry(data, H, scale, margin):
    rows, cols = int(data['grid_rows']), int(data['grid_columns'])
    spacing = float(data['dot_spacing_mm'])
    if not math.isfinite(spacing) or spacing <= 0:
        raise ValueError('Invalid calibration grid spacing.')
    width = int(math.ceil(((cols - 1) * spacing + 2 * margin) * scale))
    height = int(math.ceil(((rows - 1) * spacing + 2 * margin) * scale))
    if min(width, height) < 2 or width * height > 25000000:
        raise ValueError('Melt map dimensions too large. Reduce pixels/mm.')
    A = np.array([[scale, 0, margin * scale],
                  [0, scale, margin * scale], [0, 0, 1]], np.float64)
    return A @ H, width, height


def draw_layer_background(width, height, scale, margin, grid_data):
    image = np.full((height, width, 3), 15, np.uint8)
    spacing = float(grid_data['dot_spacing_mm'])
    rows, cols = int(grid_data['grid_rows']), int(grid_data['grid_columns'])
    for col in range(cols):
        x = int(round((col * spacing + margin) * scale))
        if 0 <= x < width:
            cv2.line(image, (x, 0), (x, height - 1), (42, 42, 42), 1)
    for row in range(rows):
        y = int(round((row * spacing + margin) * scale))
        if 0 <= y < height:
            cv2.line(image, (0, y), (width - 1, y), (42, 42, 42), 1)
    return image


def save_layer_images(layer_id, layer_index, path_canvas, heat, out_dir, base, log):
    tag = safe_name(str(layer_id))
    stem = f'{base}_layer_{layer_index:04d}_{tag}'
    path_png = os.path.join(out_dir, stem + '_path.png')
    heat_png = os.path.join(out_dir, stem + '_activity.png')
    heat_f = np.log1p(heat.astype(np.float32))
    maximum = float(np.max(heat_f)) if heat_f.size else 0.0
    values = np.uint8(255 * heat_f / maximum) if maximum > 0 else np.zeros(heat.shape, np.uint8)
    colored = cv2.applyColorMap(values, cv2.COLORMAP_TURBO)
    colored[values == 0] = 0
    if not cv2.imwrite(path_png, path_canvas):
        raise RuntimeError('Failed to write ' + path_png)
    if not cv2.imwrite(heat_png, colored):
        raise RuntimeError('Failed to write ' + heat_png)
    log(f'Layer {layer_id}: {path_png}; {heat_png}')


def analyze_meltpool(source, json_path, trigger_csv, folder, name, settings, stop, log, show=_noshow):
    """Analyze sensor RAW/DAT once; export per-layer path and event maps.

    Events are grouped by fixed microsecond slices. Timestamp CSV layer
    triggers are aligned to these slices. Reported velocities use original
    recording time rather than exported-video playback.
    """
    data, H = load_calibration(json_path)
    iterator, sensor_h, sensor_w = event_iterator(source, settings['slice_us'])
    check_resolution(data, sensor_w, sensor_h)
    T, out_w, out_h = make_melt_geometry(data, H, settings['scale'], settings['margin'])
    triggers = load_layer_triggers(trigger_csv, settings['offset_s'])
    trigger_times = [v[0] for v in triggers]
    log(f'Melt tracking: {sensor_w}x{sensor_h} -> {out_w}x{out_h}, '
        f'{len(triggers)} scheduled layer starts.')
    root = os.path.join(folder, safe_name(name))
    if os.path.exists(root):
        n = 1
        while os.path.exists(root + f'_{n:03d}'):
            n += 1
        root += f'_{n:03d}'
    os.makedirs(root)
    base = os.path.basename(root)
    csv_path = os.path.join(root, base + '_meltpool.csv')
    video_path = os.path.join(root, base + '_meltpool.mp4')
    writer = None
    if settings['export_video']:
        writer = cv2.VideoWriter(video_path, cv2.VideoWriter_fourcc(*'mp4v'),
                                 settings['fps'], (out_w, out_h))
        if not writer.isOpened():
            raise RuntimeError('Cannot create melt-pool MP4.')

    fields = ['Slice', 'Time_s', 'Layer_ID', 'Segment_ID', 'Status', 'Event_Count',
              'Active_Pixels', 'Center_X_px', 'Center_Y_px', 'World_X_mm', 'World_Y_mm',
              'VX_mm_s', 'VY_mm_s', 'Speed_mm_s', 'Direction_deg']
    layer_index = None
    layer_id = None
    canvas = heat = None
    history = deque(maxlen=settings['fit_window'])
    previous_t = previous_world = None
    segment = 0
    video_frames = 0
    slices = 0
    last_frame = None
    try:
        with open(csv_path, 'w', newline='', encoding='utf-8') as fh:
            csvout = csv.DictWriter(fh, fieldnames=fields)
            csvout.writeheader()
            for i, ev in enumerate(iterator):
                if stop.is_set():
                    log('Melt-pool analysis stopped; completed layers and partial current layer will be exported.')
                    break
                t = (i + .5) * settings['slice_us'] / 1e6
                new_index = max(0, bisect.bisect_right(trigger_times, t) - 1)
                new_id = triggers[new_index][1]
                if new_index != layer_index:
                    if layer_index is not None:
                        save_layer_images(layer_id, layer_index + 1, canvas, heat, root, base, log)
                    layer_index = new_index
                    layer_id = new_id
                    canvas = draw_layer_background(out_w, out_h, settings['scale'], settings['margin'], data)
                    heat = np.zeros((out_h, out_w), np.float32)
                    history.clear()
                    previous_t = previous_world = None
                    segment += 1
                    log(f'Starting layer {layer_id} at {t:.5f} s')

                if settings['polarity'] != -1:
                    ev = ev[ev['p'] == settings['polarity']]
                xs = ev['x'].astype(np.intp)
                ys = ev['y'].astype(np.intp)
                good = (xs >= 0) & (xs < sensor_w) & (ys >= 0) & (ys < sensor_h)
                xs, ys = xs[good], ys[good]
                n = len(xs)
                active = np.unique(ys * sensor_w + xs).size if n else 0
                row = dict(Slice=i, Time_s=f'{t:.9f}', Layer_ID=layer_id,
                           Segment_ID=segment, Status='INACTIVE', Event_Count=n,
                           Active_Pixels=active, Center_X_px='', Center_Y_px='',
                           World_X_mm='', World_Y_mm='', VX_mm_s='', VY_mm_s='',
                           Speed_mm_s='', Direction_deg='')
                position = None
                speed = None
                if n >= settings['min_events']:
                    cx, cy = float(xs.mean()), float(ys.mean())
                    wx, wy = world_xy(H, cx, cy)
                    if math.isfinite(wx) and math.isfinite(wy):
                        position = (wx, wy)
                        row.update(Status='ACTIVE', Center_X_px=cx, Center_Y_px=cy,
                                   World_X_mm=wx, World_Y_mm=wy)
                        if previous_t is not None:
                            elapsed = t - previous_t
                            step_speed = math.hypot(wx - previous_world[0], wy - previous_world[1]) / max(elapsed, 1e-12)
                            if elapsed > settings['max_gap_s'] or step_speed > settings['max_speed']:
                                history.clear()
                                segment += 1
                                row['Segment_ID'] = segment
                                row['Status'] = 'RESTART_GAP' if elapsed > settings['max_gap_s'] else 'RESTART_JUMP'
                        history.append((t, wx, wy))
                        velocity = weighted_velocity(history)
                        if velocity is not None:
                            vx, vy = velocity
                            speed = math.hypot(vx, vy)
                            if speed <= settings['max_speed']:
                                row.update(VX_mm_s=vx, VY_mm_s=vy, Speed_mm_s=speed,
                                           Direction_deg=math.degrees(math.atan2(vy, vx)))
                            else:
                                speed = None
                                row['Status'] = 'UNRELIABLE_SPEED'
                        current_xy = (int(round((wx + settings['margin']) * settings['scale'])),
                                      int(round((wy + settings['margin']) * settings['scale'])))
                        if (0 <= current_xy[0] < out_w and 0 <= current_xy[1] < out_h):
                            if len(history) >= 2 and row['Status'] == 'ACTIVE' and speed is not None:
                                old = history[-2]
                                old_xy = (int(round((old[1] + settings['margin']) * settings['scale'])),
                                          int(round((old[2] + settings['margin']) * settings['scale'])))
                                color = speed_color(speed, settings['max_speed'])
                                cv2.line(canvas, old_xy, current_xy, color, settings['line_width'], cv2.LINE_AA)
                                delta = (current_xy[0]-old_xy[0], current_xy[1]-old_xy[1])
                                if math.hypot(*delta) >= 5:
                                    cv2.arrowedLine(canvas, old_xy, current_xy, color,
                                                    max(1, settings['line_width']), cv2.LINE_AA, tipLength=.25)
                            cv2.circle(canvas, current_xy, max(1, settings['line_width']), (255,255,255), -1)
                        previous_t, previous_world = t, position
                        # Count events spatially in world-plane cells, not just centroids.
                        raw_counts = np.bincount(ys * sensor_w + xs, minlength=sensor_w * sensor_h)
                        count_frame = raw_counts.reshape(sensor_h, sensor_w).astype(np.float32)
                        projected_counts = cv2.warpPerspective(count_frame, T, (out_w, out_h),
                                                                flags=cv2.INTER_LINEAR)
                        heat += projected_counts
                else:
                    # Break tracking when the melt pool is not active.
                    if history:
                        segment += 1
                    history.clear()
                    previous_t = previous_world = None
                csvout.writerow(row)
                slices += 1
                if writer is not None:
                    annotated = canvas.copy()
                    title = f'Layer {layer_id} | {t:.4f} s | {row["Status"]}'
                    cv2.putText(annotated, title, (10, 25), cv2.FONT_HERSHEY_SIMPLEX,
                                .52, (235, 235, 235), 1, cv2.LINE_AA)
                    if speed is not None:
                        cv2.putText(annotated, f'{speed:.1f} mm/s', (10, 49),
                                    cv2.FONT_HERSHEY_SIMPLEX, .52, (235,235,235), 1, cv2.LINE_AA)
                    target = math.floor((i + 1) * settings['slice_us'] / 1e6 * settings['fps'] + 1e-9)
                    while video_frames < target and not stop.is_set():
                        writer.write(annotated)
                        video_frames += 1
                    last_frame = annotated
                    show(annotated)
                else:
                    show(canvas)
                if (i + 1) % 500 == 0:
                    log(f'Melt-pool: {i+1:,} windows, layer {layer_id}, {video_frames:,} video frames')
            if layer_index is not None:
                save_layer_images(layer_id, layer_index + 1, canvas, heat, root, base, log)
    finally:
        if writer is not None:
            writer.release()
    log(f'Melt pool outputs: {root}')
    log(f'CSV: {csv_path}')
    if writer is not None:
        log(f'MP4: {video_path}')


# ================================================================
# RAINDROPS BEHIND A CLOUDY / FROSTED WINDOW
# ================================================================

# Direction filter uses IMAGE coordinates: X increases right, Y increases down.
# Thus "Down" corresponds to +Y, even when a homography is loaded.
def rain_direction_allowed(dx, dy, direction, tolerance_degrees, min_step):
    distance = math.hypot(dx, dy)
    if distance < min_step:
        return False
    axes = {'Down': (0.0, 1.0), 'Up': (0.0, -1.0),
            'Right': (1.0, 0.0), 'Left': (-1.0, 0.0)}
    ax, ay = axes[direction]
    alignment = (dx * ax + dy * ay) / distance
    return alignment >= math.cos(math.radians(tolerance_degrees)) - 1e-12



def analyze_raindrops(source, folder, name, homography_path, s, stop, log, show=_noshow):
    """Track separate transient event blobs; adaptive pixel-activity background.

    Tracking is in image space. Optional H maps matched endpoints to mm.
    The H is only valid when calibrated at the plane containing droplets.
    """
    ext = os.path.splitext(source)[1].lower()
    if ext in EVENT_EXT:
        reader, height, width = event_iterator(source, s['slice_us'])
        fps_source = 1e6 / s['slice_us']
        def frames():
            for i, ev in enumerate(reader):
                if s['polarity'] != -1:
                    ev = ev[ev['p'] == s['polarity']]
                counts = np.zeros((height, width), np.uint16)
                if len(ev):
                    x, y = ev['x'].astype(np.intp), ev['y'].astype(np.intp)
                    valid = (x >= 0) & (x < width) & (y >= 0) & (y < height)
                    # saturating by counting in int32 only per frame
                    flat = y[valid] * width + x[valid]
                    binc = np.bincount(flat, minlength=height * width).reshape(height, width)
                    counts = np.minimum(binc, 65535).astype(np.uint16)
                yield i / fps_source, counts
    elif ext in VIDEO_EXT:
        cap = cv2.VideoCapture(source)
        if not cap.isOpened():
            raise RuntimeError('Unable to read input video.')
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps_source = float(cap.get(cv2.CAP_PROP_FPS))
        if fps_source <= 0 or not math.isfinite(fps_source):
            cap.release()
            raise RuntimeError('Video has invalid FPS.')
        def frames():
            prior = None
            try:
                i = 0
                while True:
                    ok, f = cap.read()
                    if not ok:
                        break
                    gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
                    if prior is None:
                        activity = np.zeros_like(gray, np.uint16)
                    else:
                        # Approximation for video; true polarity is unavailable.
                        d = cv2.absdiff(gray, prior)
                        activity = np.where(d >= s['video_diff'], d, 0).astype(np.uint16)
                    prior = gray
                    yield i / fps_source, activity
                    i += 1
            finally:
                cap.release()
    else:
        raise ValueError('Supported inputs: RAW/DAT/AVI/MP4/MKV/MOV.')

    H = None
    if homography_path:
        info, H = load_calibration(homography_path)
        check_resolution(info, width, height)

    # Persistent static activity background, adapted over time.
    background = np.zeros((height, width), np.float32)
    history = {}              # ID -> last position/time/missed/history
    next_id = 1
    os.makedirs(folder, exist_ok=True)
    video_path, csv_path = available_pair(folder, name)
    writer = cv2.VideoWriter(video_path, cv2.VideoWriter_fourcc(*'mp4v'),
                             float(s['out_fps']), (width, height))
    if not writer.isOpened():
        raise RuntimeError('Could not create raindrop video.')
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                       (2 * s['morph'] + 1, 2 * s['morph'] + 1))
    total = 0
    written = 0
    trails = np.zeros((height, width, 3), np.uint8)
    log(f'Raindrop tracking: {width}x{height}, video {video_path}')
    fields = ['time_s', 'track_id', 'x_px', 'y_px', 'vx_px_s', 'vy_px_s',
              'speed_px_s', 'X_mm', 'Y_mm', 'vx_mm_s', 'vy_mm_s', 'speed_mm_s',
              'area_px2', 'event_activity', 'matched']
    try:
        with open(csv_path, 'w', newline='', encoding='utf-8') as fh:
            output = csv.DictWriter(fh, fieldnames=fields)
            output.writeheader()
            for index, (t, activity) in enumerate(frames()):
                if stop.is_set():
                    break
                current = activity.astype(np.float32)
                # Compare against the existing background BEFORE updating it.
                novelty = np.maximum(current - s['bg_multiplier'] * background, 0)
                active = np.uint8(novelty >= s['min_activity']) * 255
                if s['morph'] > 0:
                    active = cv2.morphologyEx(active, cv2.MORPH_CLOSE, kernel)
                    active = cv2.morphologyEx(active, cv2.MORPH_OPEN, kernel)
                n, labels, stats, centers = cv2.connectedComponentsWithStats(active, 8)
                detections = []
                for k in range(1, n):
                    x, y, w, h, area = stats[k]
                    if not (s['min_area'] <= area <= s['max_area']):
                        continue
                    # Filter highly diffuse horizontal noise / glints.
                    if max(w / max(1, h), h / max(1, w)) > s['max_aspect']:
                        continue
                    cx, cy = map(float, centers[k])
                    if not (np.isfinite(cx) and np.isfinite(cy)):
                        continue
                    detections.append({'x':cx, 'y':cy, 'area':int(area),
                                       'count':float(current[labels == k].sum())})
                # Nearest-neighbor, one-to-one matching with a maximum elapsed gap.
                pairs = []
                for tid, old in history.items():
                    dt = t - old['time']
                    if dt <= 0 or dt > s['max_gap']:
                        continue
                    for di, d in enumerate(detections):
                        distance = math.hypot(d['x'] - old['x'], d['y'] - old['y'])
                        if distance <= s['max_step']:
                            if s['direction_filter'] and not rain_direction_allowed(
                                    d['x'] - old['x'], d['y'] - old['y'],
                                    s['direction'], s['direction_tolerance'],
                                    s['direction_min_step']):
                                continue
                            pairs.append((distance, tid, di))
                assigned_ids, assigned_d = set(), set()
                for _, tid, di in sorted(pairs):
                    if tid in assigned_ids or di in assigned_d:
                        continue
                    assigned_ids.add(tid)
                    assigned_d.add(di)
                    detections[di]['track_id'] = tid
                for di, d in enumerate(detections):
                    if di not in assigned_d:
                        d['track_id'] = next_id
                        next_id += 1
                frame = cv2.cvtColor(np.uint8(np.clip(np.log1p(current) * 70, 0, 255)),
                                      cv2.COLOR_GRAY2BGR)
                for d in detections:
                    tid = d['track_id']
                    old = history.get(tid)
                    matched = old is not None and 0 < t - old['time'] <= s['max_gap']
                    vx = vy = speed = float('nan')
                    Vx = Vy = V = float('nan')
                    X = Y = float('nan')
                    if H is not None:
                        X, Y = world_xy(H, d['x'], d['y'])
                    # During directional filtering, hold new IDs as tentative.
                    # Do not draw or export them until a permitted displacement
                    # verifies their direction on a subsequent frame.
                    if s['direction_filter'] and not matched:
                        history[tid] = {'x': d['x'], 'y': d['y'],
                                        'X': X, 'Y': Y, 'time': t}
                        continue
                    if matched:
                        dt = t - old['time']
                        vx = (d['x'] - old['x']) / dt
                        vy = (d['y'] - old['y']) / dt
                        speed = math.hypot(vx, vy)
                        if H is not None and np.isfinite(old['X']):
                            Vx, Vy = (X - old['X']) / dt, (Y - old['Y']) / dt
                            V = math.hypot(Vx, Vy)
                        color_speed = V if H is not None else speed
                        color = speed_color(color_speed, s['color_max'])
                        a = (int(old['x']), int(old['y']))
                        b = (int(d['x']), int(d['y']))
                        cv2.arrowedLine(frame, a, b, color, 2, cv2.LINE_AA, tipLength=.3)
                        cv2.line(trails, a, b, color, 1, cv2.LINE_AA)
                    else:
                        color = (0, 220, 220)
                    cv2.circle(frame, (int(d['x']), int(d['y'])), 4, color, 1)
                    cv2.putText(frame, str(tid), (int(d['x']) + 6, int(d['y']) - 4),
                                cv2.FONT_HERSHEY_SIMPLEX, .38, color, 1, cv2.LINE_AA)
                    output.writerow(dict(time_s=f'{t:.6f}', track_id=tid, x_px=d['x'],
                                         y_px=d['y'], vx_px_s=vx, vy_px_s=vy,
                                         speed_px_s=speed, X_mm=X, Y_mm=Y,
                                         vx_mm_s=Vx, vy_mm_s=Vy, speed_mm_s=V,
                                         area_px2=d['area'], event_activity=d['count'], matched=int(matched)))
                    history[tid] = {'x':d['x'], 'y':d['y'], 'X':X, 'Y':Y, 'time':t}
                # Retain lost tracks for a short gap, then purge them.
                history = {tid:o for tid,o in history.items() if t - o['time'] <= s['max_gap']}
                background = (1 - s['bg_alpha']) * background + s['bg_alpha'] * current
                if s['overlay_trails']:
                    frame = cv2.addWeighted(frame, 1, trails, .65, 0)
                display_count = (sum(1 for d in detections if d['track_id'] in assigned_ids)
                                 if s['direction_filter'] else len(detections))
                cv2.putText(frame, f't={t:.3f}s | drops={display_count}', (10, 22),
                            cv2.FONT_HERSHEY_SIMPLEX, .55, (255,255,255), 1)
                desired = int(math.floor((t + 1/fps_source) * s['out_fps'] / s['playback'] + 1e-8))
                while written < desired and not stop.is_set():
                    writer.write(frame)
                    written += 1
                show(frame)
                total += 1
                if total % 100 == 0:
                    log(f'Rain: {total:,} slices/frames, {next_id - 1:,} tracks, {written:,} exported frames')
    finally:
        writer.release()
    trail_path = os.path.splitext(video_path)[0] + '_trails.png'
    cv2.imwrite(trail_path, trails)
    log(f'Raindrop results: {csv_path}; {video_path}; {trail_path}')


# ============================================================
# SIXTH TAB: SMOKE-PLUME FLOW / DISTURBANCES / OBJECT CANDIDATES
# ============================================================

def analyze_smoke(source, folder, name, homography_path, s, stop, log, show=_noshow):
    """Exploratory optical-event plume analysis.

    Disturbance is a deviation from locally averaged 2D optical flow,
    NOT a calibrated turbulence or physical flow-field measurement.
    Objects are compact candidates based on activity/temporal novelty;
    user review is required to distinguish objects from plume structures.
    """
    extension = os.path.splitext(source)[1].lower()
    capture = None
    if extension in EVENT_EXT:
        reader, h, w = event_iterator(source, s['slice_us'])
        source_fps = 1e6 / s['slice_us']
        def stream():
            for i, events in enumerate(reader):
                if stop.is_set():
                    break
                if s['polarity'] != -1:
                    events = events[events['p'] == s['polarity']]
                if len(events):
                    x = events['x'].astype(np.intp)
                    y = events['y'].astype(np.intp)
                    good = (x >= 0) & (x < w) & (y >= 0) & (y < h)
                    counts = np.bincount(y[good] * w + x[good], minlength=h*w)
                    activity = counts.reshape(h, w).astype(np.float32)
                else:
                    activity = np.zeros((h,w), np.float32)
                yield i / source_fps, activity, None
    elif extension in VIDEO_EXT:
        capture = cv2.VideoCapture(source)
        if not capture.isOpened():
            raise RuntimeError('Could not open smoke-plume video.')
        w = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        source_fps = float(capture.get(cv2.CAP_PROP_FPS))
        if source_fps <= 0 or not math.isfinite(source_fps):
            capture.release()
            raise RuntimeError('Invalid input FPS.')
        def stream():
            previous = None
            index = 0
            try:
                while not stop.is_set():
                    ok, frame = capture.read()
                    if not ok:
                        break
                    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    if previous is None:
                        activity = np.zeros_like(gray, np.float32)
                    else:
                        signed = gray.astype(np.int16) - previous.astype(np.int16)
                        if s['polarity'] == 1:
                            activity = np.maximum(signed, 0).astype(np.float32)
                        elif s['polarity'] == 0:
                            activity = np.maximum(-signed, 0).astype(np.float32)
                        else:
                            activity = np.abs(signed).astype(np.float32)
                    previous = gray
                    yield index / source_fps, activity, gray
                    index += 1
            finally:
                capture.release()
    else:
        raise ValueError('Smoke input must be RAW/DAT/AVI/MP4/MKV/MOV.')
    if w <= 0 or h <= 0:
        raise ValueError('Invalid sensor/video dimensions.')
    H = None
    if homography_path:
        info, H = load_calibration(homography_path)
        check_resolution(info, w, h)

    os.makedirs(folder, exist_ok=True)
    video_path, measurements_csv = available_pair(folder, name)
    stem = os.path.splitext(video_path)[0]
    object_csv = stem + '_objects.csv'
    disturbance_png = stem + '_disturbances.png'
    activity_png = stem + '_activity.png'
    trajectories_png = stem + '_object_trajectories.png'
    writer = cv2.VideoWriter(video_path, cv2.VideoWriter_fourcc(*'mp4v'),
                             float(s['out_fps']), (w, h))
    if not writer.isOpened():
        raise RuntimeError('Could not initialize smoke output MP4.')

    bg = np.zeros((h,w), np.float32)
    cumulative = np.zeros((h,w), np.float32)
    disturbance_total = np.zeros((h,w), np.float32)
    trails = np.zeros((h,w,3), np.uint8)
    # Optional fixed detector ROI. Zero width/height means whole image.
    rx, ry = s['atten_roi_x'], s['atten_roi_y']
    rw, rh = s['atten_roi_w'], s['atten_roi_h']
    if rw <= 0 or rh <= 0:
        rx, ry, rw, rh = 0, 0, w, h
    if rx < 0 or ry < 0 or rx+rw > w or ry+rh > h:
        raise ValueError('Optical-monitoring ROI exceeds source image bounds.')
    roi_slice = (slice(ry,ry+rh), slice(rx,rx+rw))
    baseline_values = []
    reference_level = None
    attenuation_series = []
    last_flow_image = None
    history = {}
    next_id = 1
    frame_count = 0
    written = 0
    plume_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                              (2*s['plume_radius']+1, 2*s['plume_radius']+1))
    object_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                               (2*s['object_radius']+1, 2*s['object_radius']+1))
    flow_window = 2 * s['flow_radius'] + 1
    fieldnames = ['time_s','plume_area_px2','plume_event_activity',
                  'flow_mean_px_s','flow_mean_x_px_s','flow_mean_y_px_s',
                  'disturbance_mean_px_s','disturbance_peak_px_s',
                  'disturbed_pixels','object_candidates','tracked_objects',
                  'view_geometry','optical_proxy_type','roi_signal','reference_signal',
                  'relative_signal_ratio','obscuration_proxy_fraction',
                  'optical_warning']
    object_fields = ['time_s','object_id','x_px','y_px','vx_px_s','vy_px_s',
                     'speed_px_s','X_mm','Y_mm','vx_mm_s','vy_mm_s',
                     'speed_mm_s','area_px2','event_activity','confirmed']
    log(f'Smoke processing {w}x{h}; detection is exploratory, not object classification.')
    try:
        with open(measurements_csv,'w',newline='',encoding='utf-8') as f1, \
             open(object_csv,'w',newline='',encoding='utf-8') as f2:
            measurements = csv.DictWriter(f1,fieldnames=fieldnames)
            objects_writer = csv.DictWriter(f2,fieldnames=object_fields)
            measurements.writeheader()
            objects_writer.writeheader()
            for t,activity,gray in stream():
                if stop.is_set():
                    break
                current = np.log1p(activity)
                # For conventional video: transmission/occlusion PROXY based
                # on brightness of a stable, controlled backlit reference ROI.
                # For RAW/DAT: event-activity change indicator ONLY. Event
                # rates are not proportional to absolute optical intensity.
                if gray is not None:
                    proxy_type = 'video_brightness_proxy'
                    roi_signal = float(np.mean(gray[roi_slice]))
                else:
                    proxy_type = 'event_activity_change_only'
                    roi_signal = float(np.mean(activity[roi_slice]))
                if len(baseline_values) < s['atten_baseline_frames']:
                    baseline_values.append(roi_signal)
                    if len(baseline_values) == s['atten_baseline_frames']:
                        reference_level = float(np.median(baseline_values))
                signal_ratio = obscuration = float('nan')
                warning = 0
                if reference_level is not None and reference_level > 1e-6:
                    signal_ratio = roi_signal/reference_level
                    # This quantity is NOT physical laser attenuation.
                    # Only meaningful as obscuration if the ROI images a
                    # steady reference intensity with fixed camera exposure.
                    obscuration = max(0., min(1., 1. - signal_ratio))
                    if s['atten_enabled'] and obscuration >= s['atten_threshold']:
                        warning = 1
                attenuation_series.append((float(t),roi_signal,reference_level,
                                            signal_ratio,obscuration,warning))
                cumulative += current
                # Use an adaptive activity background before updating it.
                novelty = np.maximum(current - s['novel_multiplier'] * bg, 0)
                smooth = cv2.GaussianBlur(current,(0,0),s['blur_sigma'])
                plume_mask = np.uint8(smooth >= s['plume_threshold']) * 255
                if s['plume_radius']:
                    plume_mask = cv2.morphologyEx(plume_mask,cv2.MORPH_CLOSE,plume_kernel)
                # Optical flow on smoothed, intensity-normalized activity images.
                flow_image = np.uint8(np.clip(smooth * s['flow_gain'],0,255))
                flow = None
                dx = dy = np.zeros((h,w),np.float32)
                deviation = np.zeros((h,w),np.float32)
                flow_speed = np.zeros((h,w),np.float32)
                if last_flow_image is not None:
                    flow = cv2.calcOpticalFlowFarneback(
                        last_flow_image,flow_image,None,
                        pyr_scale=.5,levels=3,winsize=s['farneback_win'],
                        iterations=3,poly_n=5,poly_sigma=1.2,flags=0)
                    dx = flow[...,0] * source_fps
                    dy = flow[...,1] * source_fps
                    flow_speed = cv2.magnitude(dx,dy)
                    local_x = cv2.GaussianBlur(dx,(flow_window,flow_window),0)
                    local_y = cv2.GaussianBlur(dy,(flow_window,flow_window),0)
                    deviation = cv2.magnitude(dx-local_x,dy-local_y)
                    # Exclude inactive regions to avoid classifying numerical noise.
                    deviation[plume_mask == 0] = 0
                last_flow_image = flow_image
                motion_mask = plume_mask > 0
                flow_means = (float(dx[motion_mask].mean()),float(dy[motion_mask].mean())) if motion_mask.any() else (0.,0.)
                flow_avg = float(flow_speed[motion_mask].mean()) if motion_mask.any() else 0.
                disturbed = (deviation >= s['disturbance_threshold']) & motion_mask
                disturbance_total += deviation * (disturbed.astype(np.float32))
                disturbance_mean = float(deviation[motion_mask].mean()) if motion_mask.any() else 0.
                disturbance_max = float(deviation[motion_mask].max()) if motion_mask.any() else 0.

                # Candidate objects: compact regions of activity NOVELTY,
                # bounded by size/aspect and optionally outside dense plume core.
                candidate_mask = np.uint8(novelty >= s['object_novelty']) * 255
                if s['object_radius']:
                    candidate_mask = cv2.morphologyEx(candidate_mask,cv2.MORPH_OPEN,object_kernel)
                    candidate_mask = cv2.morphologyEx(candidate_mask,cv2.MORPH_CLOSE,object_kernel)
                if s['outside_only']:
                    # Local plume envelope; candidates inside it are rejected.
                    envelope = cv2.dilate(plume_mask, plume_kernel, iterations=1)
                    candidate_mask[envelope > 0] = 0
                n, labels, stats, centers = cv2.connectedComponentsWithStats(candidate_mask,8)
                detections = []
                for k in range(1,n):
                    x,y,bw,bh,area = stats[k]
                    aspect = max(bw/max(1,bh),bh/max(1,bw))
                    if area < s['object_min_area'] or area > s['object_max_area'] or aspect > s['object_aspect']:
                        continue
                    cx,cy = map(float,centers[k])
                    if not (math.isfinite(cx) and math.isfinite(cy)):
                        continue
                    detections.append(dict(x=cx,y=cy,area=int(area),count=float(activity[labels==k].sum())))
                options = []
                for track_id,old in history.items():
                    dt = t - old['t']
                    if dt <= 0 or dt > s['track_gap']:
                        continue
                    # Constant-velocity prediction improves matching across sparse events.
                    px = old['x'] + old['vx']*dt if np.isfinite(old['vx']) else old['x']
                    py = old['y'] + old['vy']*dt if np.isfinite(old['vy']) else old['y']
                    for j,d in enumerate(detections):
                        sep = math.hypot(d['x']-px,d['y']-py)
                        if sep <= s['track_step']:
                            options.append((sep,track_id,j))
                used_t,used_d = set(),set()
                for _,track_id,j in sorted(options):
                    if track_id in used_t or j in used_d:
                        continue
                    used_t.add(track_id)
                    used_d.add(j)
                    detections[j]['id'] = track_id
                for j,d in enumerate(detections):
                    if j not in used_d:
                        d['id'] = next_id
                        next_id += 1
                updated = {}
                image_gray = np.uint8(np.clip(current * s['display_gain'],0,255))
                annotated = cv2.cvtColor(image_gray,cv2.COLOR_GRAY2BGR)
                disturbance_mask = np.uint8(disturbed)*255
                annotated[disturbance_mask > 0] = (20,55,225)
                # Flow arrows on a coarse sampling grid, only in plume.
                if flow is not None:
                    for yy in range(s['arrow_step']//2,h,s['arrow_step']):
                        for xx in range(s['arrow_step']//2,w,s['arrow_step']):
                            if not motion_mask[yy,xx]:
                                continue
                            fx = float(dx[yy,xx])
                            fy = float(dy[yy,xx])
                            if math.hypot(fx,fy) < s['min_flow_px_s']:
                                continue
                            scalar = min(1.,math.hypot(fx,fy)/s['flow_color_max'])
                            ex = int(round(xx + fx/source_fps * s['arrow_multiplier']))
                            ey = int(round(yy + fy/source_fps * s['arrow_multiplier']))
                            color = speed_color(math.hypot(fx,fy),s['flow_color_max'])
                            cv2.arrowedLine(annotated,(xx,yy),(ex,ey),color,1,cv2.LINE_AA,tipLength=.3)
                confirmed_count = 0
                for d in detections:
                    oid = d['id']
                    old = history.get(oid)
                    vx = vy = speed = float('nan')
                    if old is not None and 0 < t-old['t'] <= s['track_gap']:
                        dt = t-old['t']
                        vx = (d['x']-old['x'])/dt
                        vy = (d['y']-old['y'])/dt
                        speed = math.hypot(vx,vy)
                        count = old['count']+1
                    else:
                        count = 1
                    X = Y = Vx = Vy = V = float('nan')
                    if H is not None:
                        X,Y = world_xy(H,d['x'],d['y'])
                        if old is not None and np.isfinite(old['X']) and np.isfinite(speed):
                            dt = t-old['t']
                            Vx,Vy = (X-old['X'])/dt,(Y-old['Y'])/dt
                            V = math.hypot(Vx,Vy)
                    confirmed = count >= s['min_track_samples']
                    updated[oid] = dict(x=d['x'],y=d['y'],t=t,vx=vx,vy=vy,X=X,Y=Y,count=count)
                    if not confirmed:
                        continue
                    confirmed_count += 1
                    color = speed_color(V if H is not None and np.isfinite(V) else (speed if np.isfinite(speed) else 0),s['flow_color_max'])
                    here = (int(round(d['x'])),int(round(d['y'])))
                    cv2.circle(annotated,here,6,color,2)
                    cv2.putText(annotated,f'O{oid}',(here[0]+8,here[1]-5),cv2.FONT_HERSHEY_SIMPLEX,.42,color,1)
                    if old is not None and np.isfinite(speed):
                        before = (int(round(old['x'])),int(round(old['y'])))
                        cv2.arrowedLine(annotated,before,here,color,2,cv2.LINE_AA,tipLength=.2)
                        cv2.line(trails,before,here,color,2,cv2.LINE_AA)
                    objects_writer.writerow(dict(time_s=f'{t:.6f}',object_id=oid,
                        x_px=d['x'],y_px=d['y'],vx_px_s=vx,vy_px_s=vy,speed_px_s=speed,
                        X_mm=X,Y_mm=Y,vx_mm_s=Vx,vy_mm_s=Vy,speed_mm_s=V,
                        area_px2=d['area'],event_activity=d['count'],confirmed=1))
                # Preserve history through brief absences; clear stale tracks.
                history = {key:val for key,val in history.items() if t-val['t'] <= s['track_gap']}
                history.update(updated)
                measurements.writerow(dict(time_s=f'{t:.6f}',plume_area_px2=int(motion_mask.sum()),
                    plume_event_activity=float(activity[motion_mask].sum()),
                    flow_mean_px_s=flow_avg,flow_mean_x_px_s=flow_means[0],
                    flow_mean_y_px_s=flow_means[1],disturbance_mean_px_s=disturbance_mean,
                    disturbance_peak_px_s=disturbance_max,disturbed_pixels=int(disturbed.sum()),
                    object_candidates=len(detections),tracked_objects=confirmed_count,view_geometry=s['view_geometry'],
                    optical_proxy_type=proxy_type,roi_signal=roi_signal,
                    reference_signal=reference_level,relative_signal_ratio=signal_ratio,
                    obscuration_proxy_fraction=obscuration,optical_warning=warning))
                bg = (1-s['bg_alpha'])*bg + s['bg_alpha']*current
                if s['draw_trails']:
                    annotated = cv2.addWeighted(annotated,1,trails,.8,0)
                cv2.putText(annotated,f't={t:.3f}s  plume={int(motion_mask.sum())}px  disturbed={int(disturbed.sum())}px  objects={confirmed_count}',
                            (10,22),cv2.FONT_HERSHEY_SIMPLEX,.48,(255,255,255),1,cv2.LINE_AA)
                if s['atten_enabled']:
                    cv2.rectangle(annotated,(rx,ry),(rx+rw-1,ry+rh-1),(0,255,255),1)
                    status = ('ROI reference learning' if reference_level is None else
                              f'Optical proxy: {100*obscuration:.1f}% apparent loss'
                              if gray is not None else
                              f'Event activity ratio: {signal_ratio:.2f}')
                    cv2.putText(annotated,status,(10,min(h-10,45)),
                                cv2.FONT_HERSHEY_SIMPLEX,.43,
                                (0,0,255) if warning else (0,255,255),1,cv2.LINE_AA)
                desired = int(math.floor((t+1/source_fps)*s['out_fps']/s['playback']+1e-8))
                while written < desired and not stop.is_set():
                    writer.write(annotated)
                    written += 1
                show(annotated)
                frame_count += 1
                if frame_count % 100 == 0:
                    log(f'Smoke: {frame_count:,} slices/frames; {next_id-1:,} object track IDs; {written:,} output frames')
    finally:
        writer.release()
        if capture is not None:
            capture.release()
    def save_heat(data,path):
        positive = data[data>0]
        if positive.size:
            cap = np.percentile(positive,99.5)
            gray = np.uint8(np.clip(data/max(float(cap),1e-9),0,1)*255)
        else:
            gray = np.zeros(data.shape,np.uint8)
        if not cv2.imwrite(path,cv2.applyColorMap(gray,cv2.COLORMAP_TURBO)):
            raise RuntimeError('Failed to save: '+path)
    save_heat(cumulative,activity_png)
    save_heat(disturbance_total,disturbance_png)
    if not cv2.imwrite(trajectories_png,trails):
        raise RuntimeError('Failed to save trajectories.')
    # The primary measurement CSV already contains the ROI time series.
    log('Optical reference: brightness change proxy for conventional video; '
        'event-activity change indicator for RAW/DAT, NOT beam attenuation.')
    log('Smoke outputs: '+video_path+' | '+measurements_csv+' | '+object_csv)
    log('Heatmaps: '+disturbance_png+' | '+activity_png+' | '+trajectories_png)



# ================================================================
# THE CALIBRATION TAB'S STATE (the script's Application methods
# start_cal ... save_cal, without the window)
# ================================================================
class DotLabels:
    """The dots of a calibration: the scan, the candidate dots, the dots
    labelled with their grid row and column, and the fit. The window calls
    these with the image coordinates of a click; each raises ValueError
    with the script's words when a click or a fit is refused."""

    def __init__(self) -> None:
        self.recording: Optional[Dict[str, Any]] = None
        self.points: List[Dict[str, Any]] = []
        self.candidates = np.empty((0, 2), np.float64)
        self.fit: Optional[Dict[str, Any]] = None
        self.next_row = 0
        self.next_col = 0

    # the scan
    def scanned(self, result: Dict[str, Any]) -> None:
        self.recording = result
        self.points = []
        self.fit = None
        self.candidates = np.empty((0, 2), np.float64)
        self.next_row = self.next_col = 0

    def image(self, view: str = 'Full accumulation') -> Optional[Any]:
        if self.recording is None:
            return None
        key = 'peak' if view == 'Peak window activity' else 'total'
        return self.recording[key]

    def detect(self, view: str, minimum: float, maximum: float) -> int:
        image = self.image(view)
        if image is None:
            return 0
        self.candidates = find_blobs(image, float(minimum), float(maximum))
        return len(self.candidates)

    # clicking
    def add(self, x: float, y: float, rows: int, cols: int, *,
            snap: bool = False, snap_radius: float = 12.0,
            advance: bool = True) -> bool:
        """Label the dot at image (x, y) as (next_row, next_col). False
        when the click is outside the picture (nothing happens)."""
        if self.recording is None:
            return False
        r, c = int(self.next_row), int(self.next_col)
        if not (0 <= r < rows and 0 <= c < cols):
            raise ValueError('Row or column is outside the grid.')
        if any(p['row'] == r and p['col'] == c for p in self.points):
            raise ValueError('That grid position is already assigned.')
        if not (0 <= x < self.recording['width'] and 0 <= y < self.recording['height']):
            return False
        if snap and len(self.candidates):
            d = np.linalg.norm(self.candidates - [x, y], axis=1)
            index = int(np.argmin(d))
            if d[index] <= float(snap_radius):
                x, y = self.candidates[index]
        if any(math.hypot(x - p['x'], y - p['y']) < 3 for p in self.points):
            raise ValueError('A dot has already been assigned near that position.')
        self.points.append(dict(row=r, col=c, x=float(x), y=float(y), automatic=False))
        self.fit = None
        if advance:
            used = {(p['row'], p['col']) for p in self.points}
            for k in range(r * cols + c + 1, rows * cols):
                nxt = (k // cols, k % cols)
                if nxt not in used:
                    self.next_row, self.next_col = nxt
                    break
        return True

    def remove_near(self, x: float, y: float, radius: float) -> bool:
        if not self.points:
            return False
        distances = [math.hypot(x - p['x'], y - p['y']) for p in self.points]
        i = int(np.argmin(distances))
        if distances[i] < radius:
            self._remove(i)
            return True
        return False

    def _remove(self, index: int) -> None:
        p = self.points.pop(index)
        self.next_row, self.next_col = p['row'], p['col']
        self.fit = None

    def undo(self) -> None:
        if self.points:
            self._remove(len(self.points) - 1)

    # fitting
    def fit_points(self, spacing: float) -> Dict[str, Any]:
        self.fit = None
        self.fit = solve_homography(self.points, float(spacing))
        return self.fit

    def match(self, rows: int, cols: int, spacing: float, radius: float) -> int:
        if self.fit is None:
            self.fit_points(spacing)
        more, count = auto_match(self.points, self.candidates, self.fit,
                                 int(rows), int(cols), float(spacing), float(radius))
        if count:
            new_fit = solve_homography(more, float(spacing))
            self.points, self.fit = more, new_fit
        return count

    def picture(self, view: str, rows: int, cols: int, spacing: float) -> Optional[Any]:
        """The script's redraw: the scan with candidate dots (cyan), the
        fitted grid's predicted dots (magenta crosses) and the labelled
        dots (green; orange when auto-matched), BGR, at image size."""
        image = self.image(view)
        if image is None:
            return None
        frame = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        for x, y in self.candidates:
            cv2.circle(frame, (round(x), round(y)), 3, (255, 255, 0), 1)
        if self.fit is not None:
            world = np.asarray([[[c * spacing, r * spacing]] for r in range(rows) for c in range(cols)], np.float64)
            expected = cv2.perspectiveTransform(world, self.fit['inverse']).reshape(-1, 2)
            for x, y in expected:
                if np.isfinite([x, y]).all() and 0 <= x < frame.shape[1] and 0 <= y < frame.shape[0]:
                    cv2.drawMarker(frame, (round(x), round(y)), (255, 0, 255), cv2.MARKER_CROSS, 5, 1)
        for p in self.points:
            pos = (round(p['x']), round(p['y']))
            color = (0, 165, 255) if p.get('automatic') else (0, 255, 0)
            cv2.circle(frame, pos, 5, color, 2)
            cv2.putText(frame, f'{p["row"]},{p["col"]}', (pos[0] + 6, pos[1] - 5),
                        cv2.FONT_HERSHEY_SIMPLEX, .35, color, 1)
        return frame

    def record(self, *, source: str, rows: int, cols: int, spacing: float,
               distance: float, distance_unit: str) -> Dict[str, Any]:
        """The script's calibration JSON (save_cal), checked as it checked
        it. Fits first when there is no fit."""
        if self.recording is None:
            raise ValueError('Scan a calibration recording first.')
        if self.fit is None:
            self.fit_points(spacing)
        dmm = float(distance) * (25.4 if distance_unit == 'inches' else 10)
        if not all(math.isfinite(v) and v > 0 for v in (dmm, float(spacing))):
            raise ValueError('Invalid distance or grid spacing.')
        manual = sum(not p.get('automatic', False) for p in self.points)
        if manual < 4:
            raise ValueError('At least four manually indexed dots are required.')
        return dict(calibration_type='partial_grid_planar_homography',
                    created_utc=datetime.now(timezone.utc).isoformat(),
                    source_recording=os.path.basename(source),
                    image_width=self.recording['width'], image_height=self.recording['height'],
                    grid_rows=rows, grid_columns=cols, dot_spacing_mm=spacing,
                    grid_width_mm=(cols - 1) * spacing, grid_height_mm=(rows - 1) * spacing,
                    camera_distance_original=distance, camera_distance_original_units=distance_unit,
                    camera_distance_mm=dmm, camera_distance_cm=dmm / 10,
                    coordinate_units='mm', homography_direction='image_pixels_to_world_mm',
                    **{'Homography Matrix': self.fit['H'].tolist(),
                       'Inverse Homography Matrix': self.fit['inverse'].tolist()},
                    rms_error_mm=self.fit['rms'], max_error_mm=self.fit['max_error'],
                    ransac_inliers=self.fit['inliers'], manual_points=manual,
                    matched_points=len(self.points), observations=self.points,
                    notes='Valid for the unchanged camera geometry and calibration plane. Lens distortion not corrected.')

    def save(self, path: str, record: Dict[str, Any], view: str = 'Full accumulation') -> Tuple[str, str]:
        """The JSON at `path` and its preview PNG beside it."""
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(record, f, indent=2)
        image = cv2.cvtColor(self.image(view), cv2.COLOR_GRAY2BGR)
        for p in self.points:
            cv2.circle(image, (round(p['x']), round(p['y'])), 5, (0, 255, 0), 2)
        preview = os.path.splitext(path)[0] + '_preview.png'
        cv2.imwrite(preview, image)
        return path, preview


def free_path(folder: str, basename: str, suffix: str) -> str:
    """<folder>/<basename><suffix>, or _001, _002 ... when it exists."""
    n = 0
    while True:
        prefix = basename if n == 0 else f'{basename}_{n:03d}'
        path = os.path.join(folder, prefix + suffix)
        if not os.path.exists(path):
            return path
        n += 1


def rgb_bytes(frame: Any, max_side: int = 0) -> Tuple[int, int, bytes]:
    """(width, height, RGB888 bytes) of a BGR or grey picture, shrunk so
    its longer side is at most `max_side` (0: as it is): what the window
    turns into a QImage without importing numpy."""
    if frame.ndim == 2:
        frame = cv2.cvtColor(frame, cv2.COLOR_GRAY2BGR)
    h, w = frame.shape[:2]
    if max_side and max(h, w) > max_side:
        scale = max_side / max(h, w)
        frame = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))),
                           interpolation=cv2.INTER_AREA)
        h, w = frame.shape[:2]
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    return w, h, np.ascontiguousarray(rgb).tobytes()


def missing() -> str:
    """'' when numpy and OpenCV are importable, else what to install."""
    if np is None or cv2 is None:
        return ('the event analysis needs numpy and OpenCV: '
                'pip install opencv-python')
    return ''
