"""
typhon.event_analysis's six tabs as data: each box of Andy's script (its
label, its key and its default, unchanged), and turning what the boxes
hold into the settings each analysis takes, refused with the script's own
words when a value is wrong (its start_* methods).

Standard library only: the window builds its boxes from these lists, and
the tests check the settings without Qt.
"""
from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

POLARITIES = {'Positive (ON)': 1, 'Negative (OFF)': 0, 'Both': -1}
SPEEDS = {'0.1x': .1, '0.25x': .25, '0.5x': .5, '1x': 1., '2x': 2.,
          '4x': 4., '8x': 8.}
EVENT_EXT = {'.raw', '.dat', '.npz'}
VIDEO_EXT = {'.avi', '.mp4', '.mkv', '.mov'}
RECORDING_FILTER = ('Recordings (*.raw *.dat *.npz *.avi *.mp4 *.mkv *.mov)'
                    ';;All files (*)')


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    default: Any = ''
    #: recording | json | csv | folder | save | text | choice | check
    kind: str = 'text'
    choices: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Group:
    title: str          # '' for no box around it
    fields: Tuple[Field, ...]
    note: str = ''


@dataclass(frozen=True)
class Tab:
    key: str
    title: str
    heading: str
    groups: Tuple[Group, ...]
    start: str          # the start button's words


def _f(key: str, label: str, default: Any = '', kind: str = 'text',
       choices: Sequence[str] = ()) -> Field:
    return Field(key, label, default, kind, tuple(choices))


POL = tuple(POLARITIES)
SPD = tuple(SPEEDS)

CALIBRATION = Tab('cal', '1. Create Homography',
                  'Scan a dot-grid recording, click dots of known row and '
                  'column, fit and save the homography (pixels to mm).', (
    Group('', (_f('cal_input', 'Calibration recording', kind='recording'),)),
    Group('Dot grid and camera geometry', (
        _f('rows', 'Grid rows', '13'), _f('cols', 'Grid columns', '13'),
        _f('spacing', 'Dot center spacing (mm)', '9'),
        _f('distance', 'Camera distance', '18'),
        _f('distance_unit', 'Camera distance units', 'inches', 'choice',
           ('inches', 'cm')))),
    Group('Accumulation / detection', (
        _f('cal_slice', 'Event slice (ms)', '50'),
        _f('window_slices', 'Window slices', '4'),
        _f('blob_min', 'Minimum blob area', '3'),
        _f('blob_max', 'Maximum blob area', '1500'),
        _f('snap', 'Dot snap radius (px)', '12'),
        _f('cal_polarity', 'Event polarity', 'Both', 'choice', POL),
        _f('cal_view', 'Accumulation view', 'Full accumulation', 'choice',
           ('Full accumulation', 'Peak window activity')))),
), 'Scan entire recording')

RECTIFY = Tab('rect', '2. Rectify Recording',
              'Original RAW data is unchanged. The export is a '
              'perspective-corrected MP4.', (
    Group('', (
        _f('rect_input', 'Input recording', kind='recording'),
        _f('rect_json', 'Homography JSON', kind='json'),
        _f('rect_output', 'Output MP4 path', kind='save'),
        _f('rect_scale', 'Output pixels per mm', '4'),
        _f('rect_margin', 'Margin (mm)', '5'),
        _f('rect_slice', 'Event slice (ms)', '10'),
        _f('rect_polarity', 'RAW event polarity', 'Both', 'choice', POL))),
), 'Rectify recording to MP4')

VELOCITY = Tab('vel', '3. Velocity Arrows',
               'JSON loaded: velocity labels, arrow colors and CSV use mm/s. '
               'Without JSON: px/s.', (
    Group('', (
        _f('vel_input', 'Input RAW / DAT', kind='recording'),
        _f('vel_folder', 'Output directory', kind='folder'),
        _f('vel_name', 'Output base filename', 'Positive_Event_Velocity'),
        _f('vel_json', 'Homography JSON (optional)', kind='json'),
        _f('vel_polarity', 'Event polarity', 'Positive (ON)', 'choice', POL),
        _f('vel_slice', 'Event slice (microseconds)', '10000'),
        _f('vel_top', 'Largest clusters (%)', '5'),
        _f('vel_area', 'Minimum cluster area', '15'),
        _f('vel_events', 'Minimum events/cluster', '10'),
        _f('vel_join', 'Cluster joining radius', '2'),
        _f('vel_match', 'Max matching distance (px)', '60'),
        _f('vel_maxspeed', 'Max color speed (units/s)', '2000'),
        _f('vel_playback', 'Playback speed', '0.25x', 'choice', SPD),
        _f('vel_fps', 'Export FPS', '60', 'choice', ('30', '60', '120')))),
), 'Generate velocity video + CSV')

MELTPOOL = Tab('melt', '4. Melt-Pool Paths',
               'Trigger CSV: time_s,layer_id. Each row starts a new layer; '
               'time must use RAW timeline. With no trigger CSV, the entire '
               'recording is Layer 1. One scan-path PNG and one heatmap PNG '
               'are saved per layer.', (
    Group('', (
        _f('melt_input', 'Input RAW / DAT', kind='recording'),
        _f('melt_json', 'Homography JSON', kind='json'),
        _f('melt_triggers', 'Layer trigger CSV (optional)', kind='csv'),
        _f('melt_folder', 'Output directory', kind='folder'),
        _f('melt_name', 'Output base filename', 'Meltpool_Path'),
        _f('melt_polarity', 'Event polarity', 'Both', 'choice', POL),
        _f('melt_slice', 'Event slice (microseconds)', '1000'),
        _f('melt_min_events', 'Minimum events / active slice', '20'),
        _f('melt_window', 'Rolling fit (active samples)', '5'),
        _f('melt_gap', 'Maximum time gap (ms)', '5'),
        _f('melt_maxspeed', 'Maximum plausible speed (mm/s)', '2500'),
        _f('melt_scale', 'Output pixels / mm', '5'),
        _f('melt_margin', 'Output margin (mm)', '5'),
        _f('melt_line', 'Scan-path line width (px)', '2'),
        _f('melt_offset', 'Layer trigger time offset (s)', '0'),
        _f('melt_fps', 'Video export frame rate', '60'),
        _f('melt_export_video',
           'Export annotated MP4 as well as per-layer PNGs and CSV', True,
           'check'))),
), 'Analyze melt pool and export layer maps')

RAINDROPS = Tab('rain', '5. Raindrop Tracking',
                'Multiple raindrops through cloudy / frosted glass. Tracks '
                'moving event patches; persistent window texture is '
                'suppressed by adaptive event-rate background. Pixel velocity '
                'does not require calibration. Only use a homography if it '
                'was calibrated on the droplet plane.', (
    Group('', (
        _f('rain_input', 'Input RAW / DAT / video', kind='recording'),
        _f('rain_json', 'Homography JSON (optional)', kind='json'),
        _f('rain_folder', 'Output directory', kind='folder'),
        _f('rain_name', 'Output filename', 'Raindrops'),
        _f('rain_polarity', 'RAW event polarity', 'Both', 'choice', POL),
        _f('rain_playback', 'Video playback speed', '1x', 'choice', SPD),
        _f('rain_slice', 'RAW slice (microseconds)', '10000'),
        _f('rain_min_activity', 'Minimum novel activity', '1'),
        _f('rain_alpha', 'Background adaptation alpha (0-1)', '0.01'),
        _f('rain_multiplier', 'Background rejection multiplier', '1.8'),
        _f('rain_video_diff', 'Video brightness-change threshold', '12'),
        _f('rain_morph', 'Morphological radius (pixels)', '1'),
        _f('rain_min_area', 'Minimum blob area (pixels)', '3'),
        _f('rain_max_area', 'Maximum blob area (pixels)', '3000'),
        _f('rain_aspect', 'Maximum blob aspect ratio', '8'),
        _f('rain_step', 'Maximum matching step (pixels)', '35'),
        _f('rain_gap', 'Maximum track gap (seconds)', '0.08'),
        _f('rain_color', 'Color max velocity (px/s or mm/s)', '800'),
        _f('rain_fps', 'Output video FPS', '60'))),
    Group('Optional motion-direction filtering', (
        _f('rain_direction_filter', 'Enable motion-direction filter', False,
           'check'),
        _f('rain_direction', 'Allowed direction', 'Down', 'choice',
           ('Down', 'Up', 'Left', 'Right')),
        _f('rain_direction_tolerance', 'Tolerance (+/- degrees)', '45'),
        _f('rain_direction_min_step', 'Minimum movement (pixels)', '1')),
        'Direction is measured in camera image coordinates. The filter '
        'applies to candidate matches; new tracks are tentative until they '
        'show permitted motion.'),
    Group('', (_f('rain_trails', 'Draw cumulative trajectory overlay', True,
                  'check'),)),
), 'Track droplets and export MP4 / CSV / trail PNG')

SMOKE = Tab('smoke', '6. Smoke Plumes',
            'Smoke plume: optical flow disturbances + emerging object '
            'candidates. Experimental flow detection for LPBF or fire '
            'recordings. Red regions show local flow deviations; numbered '
            'circles are provisional moving-object candidates, not validated '
            'classifications. RAW/DAT uses event activity; conventional video '
            'uses frame brightness changes.', (
    Group('', (
        _f('smoke_input', 'Input RAW / DAT / video', kind='recording'),
        _f('smoke_json', 'Homography JSON (optional)', kind='json'),
        _f('smoke_folder', 'Output directory', kind='folder'),
        _f('smoke_name', 'Output filename', 'Smoke_Plume_Analysis'),
        _f('smoke_polarity', 'Event polarity', 'Both', 'choice', POL),
        _f('smoke_playback', 'Playback speed', '1x', 'choice', SPD),
        _f('smoke_view', 'Camera view', 'Top-down', 'choice',
           ('Top-down', 'Side view'))),
        'View labels describe the observation geometry; they do not change '
        'pixel directions. A single homography only calibrates one plane.'),
    Group('Optional optical obscuration / activity proxy (laser proxy)', (
        _f('smoke_atten_enabled', 'Enable reference ROI monitoring', False,
           'check'),
        _f('smoke_atten_x', 'ROI X (px)', '0'),
        _f('smoke_atten_y', 'ROI Y (px)', '0'),
        _f('smoke_atten_w', 'ROI width (px; 0 = full image)', '0'),
        _f('smoke_atten_h', 'ROI height (px; 0 = full image)', '0'),
        _f('smoke_atten_baseline', 'Reference frames', '30'),
        _f('smoke_atten_threshold', 'Alert proxy fraction (0-1)', '0.20')),
        'Video: brightness reduction vs reference can suggest plume '
        'obscuration. RAW/DAT: only relative event-activity change, NOT laser '
        'transmission. Use a stable illuminated target and fixed exposure '
        'for video.'),
    Group('Flow and disturbance detection', (
        _f('smoke_slice', 'RAW event slice (us)', '10000'),
        _f('smoke_blur', 'Activity blur sigma', '1.5'),
        _f('smoke_plume_threshold', 'Plume activity threshold', '0.35'),
        _f('smoke_plume_radius', 'Plume close radius (px)', '3'),
        _f('smoke_flow_gain', 'Flow display gain', '100'),
        _f('smoke_fb_window', 'Farneback window (odd)', '15'),
        _f('smoke_flow_radius', 'Local flow radius (px)', '9'),
        _f('smoke_disturbance', 'Disturbance threshold (px/s)', '100'),
        _f('smoke_min_flow', 'Min flow arrow speed (px/s)', '40'),
        _f('smoke_arrow_step', 'Flow arrow grid step (px)', '20'),
        _f('smoke_arrow_mult', 'Flow arrow length multiplier', '4'),
        _f('smoke_flow_max', 'Flow color max (px/s)', '900'))),
    Group('Object candidate tracking', (
        _f('smoke_bg_alpha', 'Adaptive background alpha', '0.01'),
        _f('smoke_novel_mult', 'Novel activity multiplier', '1.5'),
        _f('smoke_object_threshold', 'Object novelty threshold', '0.65'),
        _f('smoke_object_radius', 'Object morphology radius', '1'),
        _f('smoke_min_area', 'Minimum candidate area (px2)', '4'),
        _f('smoke_max_area', 'Maximum candidate area (px2)', '500'),
        _f('smoke_object_aspect', 'Max candidate aspect ratio', '5'),
        _f('smoke_track_step', 'Maximum object match step (px)', '30'),
        _f('smoke_track_gap', 'Maximum object time gap (s)', '0.08'),
        _f('smoke_min_track', 'Min consecutive detections', '3'),
        _f('smoke_display_gain', 'Display brightness gain', '65'),
        _f('smoke_fps', 'Export FPS', '60'),
        _f('smoke_outside_only',
           'Restrict object candidates to outside plume envelope (stricter)',
           False, 'check'),
        _f('smoke_trails', 'Overlay confirmed object trajectories', True,
           'check'))),
), 'Analyze smoke and export MP4, CSVs, heatmaps')

TABS: Tuple[Tab, ...] = (CALIBRATION, RECTIFY, VELOCITY, MELTPOOL,
                         RAINDROPS, SMOKE)
#: The tabs whose JSON box a saved calibration fills (the script filled
#: three; the other two take the same JSON).
JSON_KEYS = ('rect_json', 'vel_json', 'melt_json', 'rain_json', 'smoke_json')


def fields() -> List[Field]:
    return [f for tab in TABS for g in tab.groups for f in g.fields]


def defaults() -> Dict[str, Any]:
    return {f.key: f.default for f in fields()}


def merged(saved: Mapping[str, Any]) -> Dict[str, Any]:
    """The defaults with every saved value of a known key over them."""
    out = defaults()
    for f in fields():
        if f.key in saved:
            value = saved[f.key]
            if f.kind == 'check':
                out[f.key] = bool(value)
            elif f.kind == 'choice' and str(value) not in f.choices:
                continue
            else:
                out[f.key] = str(value)
    return out


# ======================================================================
# Values -> settings (the script's start_* checks)
# ======================================================================
def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value).strip())
    value = os.path.splitext(value)[0].rstrip(' .')
    if not value:
        raise ValueError('Output filename must not be empty.')
    return value


def _finite(values: Mapping[str, Any]) -> bool:
    return all(math.isfinite(v) for v in values.values()
               if isinstance(v, (int, float)) and not isinstance(v, bool))


def _num(v: Mapping[str, Any], key: str, kind: Any = float) -> Any:
    text = str(v.get(key, '')).strip()
    try:
        return int(float(text)) if kind is int and '.' in text \
            and float(text).is_integer() else kind(text)
    except ValueError:
        label = next((f.label for f in fields() if f.key == key), key)
        raise ValueError(f'{label}: {text!r} is not a number.') from None


def out_folder(v: Mapping[str, Any], key: str, source: str) -> str:
    """The output directory, or (left empty) '<recording>_analysis'
    beside the recording."""
    folder = str(v.get(key, '')).strip()
    if folder:
        return folder
    stem = os.path.splitext(os.path.basename(source))[0] or 'recording'
    return os.path.join(os.path.dirname(os.path.abspath(source)),
                        stem + '_analysis')


def _ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def calibration(v: Mapping[str, Any]) -> Dict[str, Any]:
    path = str(v.get('cal_input', '')).strip()
    if not os.path.isfile(path):
        raise ValueError('Select an existing calibration recording.')
    rows, cols = _num(v, 'rows', int), _num(v, 'cols', int)
    spacing, distance = _num(v, 'spacing'), _num(v, 'distance')
    slice_us = int(_num(v, 'cal_slice') * 1000)
    window_slices = _num(v, 'window_slices', int)
    if min(rows, cols, slice_us, window_slices) < 1 or rows < 2 or cols < 2:
        raise ValueError('Grid and time settings must be positive.')
    if not (math.isfinite(spacing) and spacing > 0 and math.isfinite(distance)
            and distance > 0):
        raise ValueError('Spacing and camera distance must be positive.')
    return dict(path=path, rows=rows, cols=cols, spacing=spacing,
                distance=distance, slice_us=slice_us,
                window_slices=window_slices,
                polarity=POLARITIES[v.get('cal_polarity', 'Both')])


def grid(v: Mapping[str, Any]) -> Tuple[int, int, float]:
    """(rows, cols, spacing) as the calibration tab's boxes hold them."""
    rows, cols, spacing = (_num(v, 'rows', int), _num(v, 'cols', int),
                           _num(v, 'spacing'))
    if rows < 2 or cols < 2 or not (math.isfinite(spacing) and spacing > 0):
        raise ValueError('Grid rows and columns must be at least 2 and the '
                         'spacing positive.')
    return rows, cols, spacing


def rectify(v: Mapping[str, Any]) -> Dict[str, Any]:
    inp = str(v.get('rect_input', '')).strip()
    js = str(v.get('rect_json', '')).strip()
    out = str(v.get('rect_output', '')).strip()
    if not os.path.isfile(inp) or not os.path.isfile(js):
        raise ValueError('Select input recording, JSON and output MP4.')
    if not out:
        out = os.path.splitext(inp)[0] + '_rectified.mp4'
    if not out.lower().endswith('.mp4'):
        out += '.mp4'
    if os.path.abspath(inp) == os.path.abspath(out):
        raise ValueError('Output cannot overwrite input.')
    scale, margin = _num(v, 'rect_scale'), _num(v, 'rect_margin')
    slice_us = round(_num(v, 'rect_slice') * 1000)
    if not all(map(math.isfinite, (scale, margin))) or scale <= 0 or \
            margin < 0 or slice_us <= 0:
        raise ValueError('Invalid rectification settings.')
    if _ext(inp) not in EVENT_EXT | VIDEO_EXT:
        raise ValueError('Unsupported input recording.')
    return dict(path=inp, json=js, output=out, scale=scale, margin=margin,
                slice_us=slice_us,
                polarity=POLARITIES[v.get('rect_polarity', 'Both')])


def velocity(v: Mapping[str, Any]) -> Dict[str, Any]:
    path = str(v.get('vel_input', '')).strip()
    js = str(v.get('vel_json', '')).strip()
    if not os.path.isfile(path) or _ext(path) not in EVENT_EXT:
        raise ValueError('Velocity mode requires a Prophesee RAW or DAT '
                         'recording (or a .npz of events).')
    if js and not os.path.isfile(js):
        raise ValueError('Homography JSON does not exist.')
    s = dict(slice_us=_num(v, 'vel_slice', int),
             top_percent=_num(v, 'vel_top'),
             min_area=_num(v, 'vel_area', int),
             min_events=_num(v, 'vel_events', int),
             join_radius=_num(v, 'vel_join', int),
             match_radius=_num(v, 'vel_match'),
             max_speed=_num(v, 'vel_maxspeed'),
             polarity=POLARITIES[v.get('vel_polarity', 'Positive (ON)')],
             playback=SPEEDS[v.get('vel_playback', '0.25x')],
             fps=_num(v, 'vel_fps', int))
    if s['slice_us'] <= 0 or not 0 < s['top_percent'] <= 100 or \
            min(s['min_area'], s['min_events']) < 1:
        raise ValueError('Invalid slice, cluster percentage, or area/event '
                         'thresholds.')
    if s['join_radius'] < 0 or not all(
            math.isfinite(x) and x > 0 for x in (s['match_radius'],
                                                 s['max_speed'])):
        raise ValueError('Invalid joining radius, matching distance or '
                         'color-speed maximum.')
    return dict(path=path, json=js, settings=s,
                folder=out_folder(v, 'vel_folder', path),
                name=safe_name(v.get('vel_name', '')))


def meltpool(v: Mapping[str, Any]) -> Dict[str, Any]:
    source = str(v.get('melt_input', '')).strip()
    js = str(v.get('melt_json', '')).strip()
    triggers = str(v.get('melt_triggers', '')).strip()
    if not os.path.isfile(source) or _ext(source) not in EVENT_EXT:
        raise ValueError('Melt-pool mode needs a RAW or DAT recording.')
    if not os.path.isfile(js):
        raise ValueError('Select a valid calibration JSON file.')
    if triggers and not os.path.isfile(triggers):
        raise ValueError('The layer trigger CSV does not exist.')
    s = dict(slice_us=_num(v, 'melt_slice', int),
             polarity=POLARITIES[v.get('melt_polarity', 'Both')],
             min_events=_num(v, 'melt_min_events', int),
             fit_window=_num(v, 'melt_window', int),
             max_gap_s=_num(v, 'melt_gap') / 1000,
             max_speed=_num(v, 'melt_maxspeed'),
             scale=_num(v, 'melt_scale'),
             margin=_num(v, 'melt_margin'),
             line_width=_num(v, 'melt_line', int),
             offset_s=_num(v, 'melt_offset'),
             fps=_num(v, 'melt_fps', int),
             export_video=bool(v.get('melt_export_video', True)))
    if (s['slice_us'] < 1 or s['min_events'] < 1 or s['fit_window'] < 2
            or s['line_width'] < 1 or s['fps'] < 1 or not _finite(s)
            or s['max_gap_s'] <= 0 or s['max_speed'] <= 0 or s['scale'] <= 0
            or s['margin'] < 0):
        raise ValueError('Invalid melt-pool analysis settings.')
    return dict(path=source, json=js, triggers=triggers, settings=s,
                folder=out_folder(v, 'melt_folder', source),
                name=safe_name(v.get('melt_name', '')))


def raindrops(v: Mapping[str, Any]) -> Dict[str, Any]:
    source = str(v.get('rain_input', '')).strip()
    js = str(v.get('rain_json', '')).strip()
    if not os.path.isfile(source):
        raise ValueError('Select a valid input recording.')
    if _ext(source) not in EVENT_EXT | VIDEO_EXT:
        raise ValueError('Unsupported input recording format.')
    if js and not os.path.isfile(js):
        raise ValueError('Selected homography JSON does not exist.')
    s = dict(slice_us=_num(v, 'rain_slice', int),
             polarity=POLARITIES[v.get('rain_polarity', 'Both')],
             playback=SPEEDS[v.get('rain_playback', '1x')],
             min_activity=_num(v, 'rain_min_activity'),
             bg_alpha=_num(v, 'rain_alpha'),
             bg_multiplier=_num(v, 'rain_multiplier'),
             video_diff=_num(v, 'rain_video_diff', int),
             morph=_num(v, 'rain_morph', int),
             min_area=_num(v, 'rain_min_area', int),
             max_area=_num(v, 'rain_max_area', int),
             max_aspect=_num(v, 'rain_aspect'),
             max_step=_num(v, 'rain_step'),
             max_gap=_num(v, 'rain_gap'),
             color_max=_num(v, 'rain_color'),
             out_fps=_num(v, 'rain_fps', int),
             overlay_trails=bool(v.get('rain_trails', True)),
             direction_filter=bool(v.get('rain_direction_filter', False)),
             direction=str(v.get('rain_direction', 'Down')),
             direction_tolerance=_num(v, 'rain_direction_tolerance'),
             direction_min_step=_num(v, 'rain_direction_min_step'))
    if (s['slice_us'] < 1 or s['out_fps'] < 1 or s['video_diff'] < 0
            or s['morph'] < 0 or s['morph'] > 12 or s['min_area'] < 1
            or s['max_area'] < s['min_area'] or s['max_step'] <= 0
            or s['max_gap'] <= 0 or s['color_max'] <= 0
            or s['max_aspect'] < 1 or s['min_activity'] <= 0
            or s['bg_multiplier'] < 0
            or s['direction'] not in ('Down', 'Up', 'Left', 'Right')
            or not 0 <= s['direction_tolerance'] <= 90
            or s['direction_min_step'] < 0
            or not 0 < s['bg_alpha'] <= 1 or not _finite(s)):
        raise ValueError('Invalid raindrop settings.')
    return dict(path=source, json=js, settings=s,
                folder=out_folder(v, 'rain_folder', source),
                name=safe_name(v.get('rain_name', '')))


def smoke(v: Mapping[str, Any]) -> Dict[str, Any]:
    source = str(v.get('smoke_input', '')).strip()
    js = str(v.get('smoke_json', '')).strip()
    if not os.path.isfile(source):
        raise ValueError('Select an existing recording.')
    if _ext(source) not in EVENT_EXT | VIDEO_EXT:
        raise ValueError('Unsupported recording format.')
    if js and not os.path.isfile(js):
        raise ValueError('Selected calibration does not exist.')
    s = dict(slice_us=_num(v, 'smoke_slice', int),
             polarity=POLARITIES[v.get('smoke_polarity', 'Both')],
             playback=SPEEDS[v.get('smoke_playback', '1x')],
             blur_sigma=_num(v, 'smoke_blur'),
             plume_threshold=_num(v, 'smoke_plume_threshold'),
             plume_radius=_num(v, 'smoke_plume_radius', int),
             flow_gain=_num(v, 'smoke_flow_gain'),
             farneback_win=_num(v, 'smoke_fb_window', int),
             flow_radius=_num(v, 'smoke_flow_radius', int),
             disturbance_threshold=_num(v, 'smoke_disturbance'),
             min_flow_px_s=_num(v, 'smoke_min_flow'),
             arrow_step=_num(v, 'smoke_arrow_step', int),
             arrow_multiplier=_num(v, 'smoke_arrow_mult'),
             flow_color_max=_num(v, 'smoke_flow_max'),
             bg_alpha=_num(v, 'smoke_bg_alpha'),
             novel_multiplier=_num(v, 'smoke_novel_mult'),
             object_novelty=_num(v, 'smoke_object_threshold'),
             object_radius=_num(v, 'smoke_object_radius', int),
             object_min_area=_num(v, 'smoke_min_area', int),
             object_max_area=_num(v, 'smoke_max_area', int),
             object_aspect=_num(v, 'smoke_object_aspect'),
             track_step=_num(v, 'smoke_track_step'),
             track_gap=_num(v, 'smoke_track_gap'),
             min_track_samples=_num(v, 'smoke_min_track', int),
             display_gain=_num(v, 'smoke_display_gain'),
             out_fps=_num(v, 'smoke_fps', int),
             outside_only=bool(v.get('smoke_outside_only', False)),
             draw_trails=bool(v.get('smoke_trails', True)),
             view_geometry=str(v.get('smoke_view', 'Top-down')),
             atten_enabled=bool(v.get('smoke_atten_enabled', False)),
             atten_roi_x=_num(v, 'smoke_atten_x', int),
             atten_roi_y=_num(v, 'smoke_atten_y', int),
             atten_roi_w=_num(v, 'smoke_atten_w', int),
             atten_roi_h=_num(v, 'smoke_atten_h', int),
             atten_baseline_frames=_num(v, 'smoke_atten_baseline', int),
             atten_threshold=_num(v, 'smoke_atten_threshold'))
    if (s['slice_us'] < 1 or s['out_fps'] < 1 or s['playback'] <= 0
            or s['blur_sigma'] <= 0 or s['plume_threshold'] < 0
            or not 0 <= s['plume_radius'] <= 20 or s['flow_gain'] <= 0
            or s['farneback_win'] < 5 or s['farneback_win'] % 2 != 1
            or s['flow_radius'] < 1 or s['flow_radius'] > 50
            or s['disturbance_threshold'] <= 0 or s['min_flow_px_s'] < 0
            or s['arrow_step'] < 5 or s['arrow_multiplier'] <= 0
            or s['flow_color_max'] <= 0 or not 0 < s['bg_alpha'] <= 1
            or s['novel_multiplier'] < 0 or s['object_novelty'] <= 0
            or not 0 <= s['object_radius'] <= 20 or s['object_min_area'] < 1
            or s['object_max_area'] < s['object_min_area']
            or s['object_aspect'] < 1 or s['track_step'] <= 0
            or s['track_gap'] <= 0 or s['min_track_samples'] < 2
            or s['display_gain'] <= 0 or s['atten_roi_x'] < 0
            or s['atten_roi_y'] < 0 or s['atten_roi_w'] < 0
            or s['atten_roi_h'] < 0 or s['atten_baseline_frames'] < 2
            or not 0 <= s['atten_threshold'] <= 1 or not _finite(s)):
        raise ValueError('Invalid smoke detection parameters.')
    return dict(path=source, json=js, settings=s,
                folder=out_folder(v, 'smoke_folder', source),
                name=safe_name(v.get('smoke_name', '')))


#: tab key -> its values-to-settings function
CHECKS = {'cal': calibration, 'rect': rectify, 'vel': velocity,
          'melt': meltpool, 'rain': raindrops, 'smoke': smoke}


def tab(key: str) -> Optional[Tab]:
    return next((t for t in TABS if t.key == key), None)
