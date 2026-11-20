"""Arm pose estimate and weapon-like object detection for TRELLIS samples (D-012 / D-013).

Works on a mesh in GLB orientation (y-up, front +Z). For a raw TRELLIS sample (z-up) use to_y_up().
Everything is measured in units of the character height H (feet at 0, top of the mesh at 1).

Arm angle (from straight down; T-pose = 90, game bind pose = about 40):
  1. Orthographic front render (looking along -Z) with depth.
  2. Pixels behind the body's mid plane by more than BEHIND are dropped: wings, tails and capes
     sit behind the body, arms in A/T poses are near the mid plane. Rows below 0.30 H are ignored.
  3. Per side, the hand is the outermost remaining silhouette pixel; the shoulder is assumed at
     (body centre +- SHOULDER_X, SHOULDER_Y) (Grim Dawn male bind pose: x 0.108 H, y 0.79 H).
  4. angle = atan2(hand reach beyond the shoulder, shoulder height - hand height).
  A side is "unreliable" when the hand reach is implausible, the two sides differ a lot, or many
  silhouette pixels were dropped as being behind the body (large wings / capes spread sideways).

Weapon-like objects (any cue => weapon_suspected):
  vertical_at_hand  long vertical silhouette run through the hand column beyond what the arm itself
                    covers (a sword or staff held upright or hanging down).
  outer_column_run  a long, thin contiguous vertical run in a column away from the centre line (>= 0.20 H):
                    staffs and spears held beside the body, which the hand column can miss when the
                    fist sticks out further than the shaft. Thin = at most 0.06 H wide
                    (median over 9 rows of the run), which keeps robe hems, cape edges and legs out.
  beyond_hand       silhouette reaching much further from the shoulder than an arm plus hand can.
  forward           mesh parts sticking out in front of the torso front surface at the same height
                    (axes, blades or spears held forward), outside the head: either a projected area
                    beyond FORWARD, or any part more than FORWARD_EXTENT in front (thin blades pointing
                    at the camera have almost no projected area).
Thresholds were set on 31 labelled samples and checked on 25 fresh ones; see docs/trellis_text_and_samples.md
chapter 11 for the evaluation and its limits.
"""
from __future__ import annotations

import math

import numpy as np
import torch
from scipy.ndimage import median_filter

from postprocess_alt import interpolate, rasterize

Z_UP_TO_Y_UP = np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype=np.float32)

SHOULDER_X = 0.108       # shoulder joint distance from the centre line, fraction of H
SHOULDER_Y = 0.79        # shoulder joint height above the feet
BEHIND = 0.06            # drop front-view pixels deeper than this behind the body mid plane (fraction of H)
MIN_ROW = 0.30           # ignore rows below this height (legs, tails on the ground)
TARGET_ARM_DEG = 40.0    # Grim Dawn bind pose (docs/converter.md 2.2)
PROFILE_SMOOTH_ROWS = 15 # median filter on the outer edge profile (about 0.03 H at 512 px)
MIN_SEGMENT = 0.12       # shortest shoulder-to-hand edge that is fitted; shorter (T-pose) uses the point method
RAISED_HAND_Y = 0.70     # outermost point at least this high = arms raised (T-pose-like): use the point method

VERTICAL_AT_HAND = 0.20  # vertical extent of the outermost zone beyond forearm + hand, fraction of H
BEYOND_HAND = 0.58       # max reach from the shoulder joint above 0.45 H, fraction of H
FORWARD = 0.14           # protrusion in front of the torso front surface, fraction of H
OUTER_COLUMN_X = 0.20    # columns at least this far from the centre line are checked for long vertical runs
OUTER_COLUMN_RUN = 0.35  # a contiguous vertical run longer than this there ...
THIN_WIDTH = 0.06        # ... that is at most this wide (median over the run) = upright or hanging long object
INNER_COLUMN_X = 0.06    # closer to the body (0.06-0.20 H) the same check is stricter:
INNER_COLUMN_RUN = 0.60  # longer run ...
INNER_THIN_WIDTH = 0.03  # ... and thinner (legs, sleeves and robe folds stay out)
FORWARD_MIN_AREA = 0.0015  # minimum projected area (fraction of H^2) of forward protrusions, or ...
FORWARD_EXTENT = 0.20    # ... anything sticking out this far in front of the torso (thin blades seen end-on)


def to_y_up(vertices: np.ndarray) -> np.ndarray:
    return vertices @ Z_UP_TO_Y_UP


def _front_render(v: np.ndarray, f: np.ndarray, res: int):
    lo, hi = v.min(0), v.max(0)
    H = float(hi[1] - lo[1])
    centre = (lo + hi) / 2
    span = float(max(hi[0] - lo[0], hi[1] - lo[1])) * 1.04
    zr = float(hi[2] - lo[2]) / 2 + 1e-6
    vt = torch.tensor(v, dtype=torch.float32, device="cuda")
    clip = torch.stack([
        (vt[:, 0] - float(centre[0])) / (span / 2),
        (vt[:, 1] - float(centre[1])) / (span / 2),
        -(vt[:, 2] - float(centre[2])) / zr * 0.9,  # nearer to the +Z camera = smaller depth
        torch.ones_like(vt[:, 0]),
    ], -1)
    ft = torch.tensor(f, dtype=torch.int64, device="cuda")
    fid, bary = rasterize(clip, ft, res, res)
    z = interpolate(vt[:, 2:3], ft, fid, bary)[..., 0]
    mask = (fid >= 0).cpu().numpy()
    depth = z.cpu().numpy()
    px = span / res
    xs = (np.arange(res) + 0.5) * px - span / 2 + float(centre[0])   # column -> x
    ys = (np.arange(res) + 0.5) * px - span / 2 + float(centre[1])   # row -> y (row 0 = bottom)
    return mask, depth, xs, ys, px, H, float(lo[1])


def _longest_thin_run(mask: np.ndarray, cols: np.ndarray, min_run_px: float, max_width_px: float) -> int:
    """Longest contiguous vertical run (gaps <= 3 rows) among `cols` that is longer than min_run_px and
    whose horizontal width, measured at 9 rows along the run, has a median of at most max_width_px."""
    longest = 0
    for col in cols:
        rows_c = np.nonzero(mask[:, col])[0]
        if len(rows_c) < 2:
            continue
        breaks = np.nonzero(np.diff(rows_c) > 3)[0]
        starts = np.r_[0, breaks + 1]
        ends = np.r_[breaks, len(rows_c) - 1]
        b, e = max(zip(starts, ends), key=lambda be: rows_c[be[1]] - rows_c[be[0]])
        run = int(rows_c[e] - rows_c[b] + 1)
        if run <= min_run_px or run <= longest:
            continue
        widths = []
        for row in np.linspace(rows_c[b], rows_c[e], 9).astype(int):
            line = mask[row]
            if not line[col]:
                continue
            left = col
            while left - 1 >= 0 and line[left - 1]:
                left -= 1
            right = col
            while right + 1 < len(line) and line[right + 1]:
                right += 1
            widths.append(right - left + 1)
        if widths and float(np.median(widths)) <= max_width_px:
            longest = run
    return longest


def analyze(v: np.ndarray, f: np.ndarray, res: int = 512, target_deg: float = TARGET_ARM_DEG) -> dict:
    """Pose and weapon cues of one mesh (y-up, front +Z)."""
    mask, depth, xs, ys, px, H, y0 = _front_render(v, f, res)
    hy = (ys - y0) / H                     # row height as fraction of H
    # body centre from the legs: rows 0.12-0.28 H, midpoint of the silhouette runs at least 0.04 H wide
    # (legs or a robe; thin shafts are skipped). Vertex medians are pulled sideways by shields.
    band = (v[:, 1] - y0) / H
    tv = v[(band > 0.45) & (band < 0.75)]
    mids = []
    for row in np.nonzero((hy >= 0.12) & (hy <= 0.28))[0]:
        cols = np.nonzero(mask[row])[0]
        if len(cols) == 0:
            continue
        breaks = np.nonzero(np.diff(cols) > 1)[0]
        runs = [(cols[b], cols[e]) for b, e in zip(np.r_[0, breaks + 1], np.r_[breaks, len(cols) - 1])
                if (cols[e] - cols[b] + 1) * px >= 0.04 * H]
        if runs:
            runs = sorted(runs, key=lambda be: be[1] - be[0], reverse=True)[:2]
            mids.append((xs[min(b for b, _ in runs)] + xs[max(e for _, e in runs)]) / 2)
    cx = float(np.median(mids)) if mids else float(np.median(tv[:, 0]))
    core = tv[np.abs(tv[:, 0] - cx) < 0.08 * H]
    z_mid = float((core[:, 2].max() + core[:, 2].min()) / 2) if len(core) else float(np.median(tv[:, 2]))
    hx = (xs - cx) / H                     # column offset from the centre as fraction of H

    rows_ok = hy >= MIN_ROW
    near_all = mask & (depth >= z_mid - BEHIND * H)
    near = near_all & rows_ok[:, None]
    far = mask & (depth < z_mid - BEHIND * H) & rows_ok[:, None]

    out = {"height": round(H, 4), "centre_x": round(cx, 4), "body_mid_z": round(z_mid, 4), "sides": {}}
    angles = []
    shoulder_row = int(np.searchsorted(hy, SHOULDER_Y - 0.03))
    for side, sign in (("left", 1.0), ("right", -1.0)):   # character's left = +X
        side_cols = (sign * hx) > 0
        sel = near & side_cols[None, :]
        r, c = np.nonzero(sel)
        info = {}
        if len(c) == 0:
            info.update(angle_deg=None, reliable=False, reason="no silhouette on this side")
            out["sides"][side] = info
            continue
        reach = sign * hx[c]
        # outer edge profile: outermost near-plane pixel per row, median-smoothed against spikes
        prof = np.full(len(hy), -np.inf)
        np.maximum.at(prof, r, reach)
        valid = np.isfinite(prof)
        sm = median_filter(np.where(valid, prof, 0.0), size=PROFILE_SMOOTH_ROWS, mode="nearest")
        lo_row = int(np.searchsorted(hy, MIN_ROW + 0.05))
        rows = np.arange(lo_row, shoulder_row + 1)
        rows = rows[valid[rows]]
        hand_row = int(rows[np.argmax(sm[rows])]) if len(rows) else int(r[np.argmax(reach)])
        hand_x, hand_y = float(sm[hand_row]), float(hy[hand_row])
        seg = np.arange(hand_row, shoulder_row + 1)
        seg = seg[valid[seg]]
        seg_len = float(hy[shoulder_row] - hy[hand_row])
        k = int(np.argmax(reach))                      # outermost point anywhere above MIN_ROW
        point_x, point_y = float(reach[k]), float(hy[r[k]])
        raised = point_y >= RAISED_HAND_Y                # T-pose-like: the hand is above the fitted edge range
        if not raised and seg_len >= MIN_SEGMENT and len(seg) >= 8:
            sy, sx = hy[seg], sm[seg]
            i, j = np.triu_indices(len(seg), 1)
            keep = np.abs(sy[j] - sy[i]) > 0.02
            slope = float(np.median((sx[j] - sx[i])[keep] / (sy[j] - sy[i])[keep])) if keep.any() else 0.0
            angle = math.degrees(math.atan(max(-slope, 0.0)))  # edge moves outward as it goes down
            method = "edge profile (Theil-Sen)"
        else:
            hand_x, hand_y = point_x, point_y
            angle = math.degrees(math.atan2(max(hand_x - SHOULDER_X, 0.0), SHOULDER_Y - hand_y))
            method = "shoulder-to-outermost point"
        outer = side_cols[None, :] & ((sign * hx)[None, :] > SHOULDER_X)
        dropped = float((far & outer).sum()) / max(float(((near | far) & outer).sum()), 1.0)
        reasons = []
        if not (0.08 <= hand_x <= 0.75):
            reasons.append(f"hand reach {hand_x:.2f} H out of range")
        notes = []
        if dropped > 0.30:
            notes.append(f"{dropped:.0%} of the side silhouette is behind the body (wings/cape) and was ignored")
        # weapon cue 1: vertical extent of the outermost zone (down to the ground, contiguous rows)
        # beyond what forearm + hand cover
        top = float(reach.max())
        zr, zc = np.nonzero(near_all & side_cols[None, :])
        zone_rows = np.unique(zr[(sign * hx[zc]) >= top - 0.08])
        extent = 0.0
        if len(zone_rows):
            breaks = np.nonzero(np.diff(zone_rows) > 3)[0]        # allow gaps of up to 3 rows
            starts = np.r_[0, breaks + 1]
            ends = np.r_[breaks, len(zone_rows) - 1]
            extent = float(max(zone_rows[e] - zone_rows[b] + 1 for b, e in zip(starts, ends))) * px / H
        expected = 0.08 / math.tan(math.radians(max(angle, 8.0))) + 0.12
        vertical_excess = max(extent - expected, 0.0)
        # weapon cue 1b: long vertical run in any column far from the centre line (staffs, spears, swords
        # held upright or hanging down). Arms cross a column only briefly; legs, robes and capes stay inside.
        far_cols = np.nonzero(side_cols & ((sign * hx) >= OUTER_COLUMN_X))[0]
        longest = _longest_thin_run(near_all, far_cols, OUTER_COLUMN_RUN * H / px, THIN_WIDTH * H / px)
        outer_column_run = longest * px / H
        # weapon cue 1c: the same close to the body (spears held upright beside the torso), stricter
        inner_cols = np.nonzero(side_cols & ((sign * hx) >= INNER_COLUMN_X) & ((sign * hx) < OUTER_COLUMN_X))[0]
        inner_column_run = _longest_thin_run(near_all, inner_cols, INNER_COLUMN_RUN * H / px, INNER_THIN_WIDTH * H / px) * px / H
        # weapon cue 2: reach from the shoulder joint of parts outside the torso, above 0.45 H
        o = (reach > SHOULDER_X + 0.05) & (hy[r] >= 0.45)
        max_reach = float(np.hypot(reach[o] - SHOULDER_X, hy[r][o] - SHOULDER_Y).max()) if o.any() else 0.0
        info.update(
            angle_deg=round(angle, 1), method=method, hand=[round(hand_x, 3), round(hand_y, 3)],
            arm_edge_length=round(seg_len, 3), dropped_behind=round(dropped, 3),
            reliable=not reasons, reason="; ".join(reasons), note="; ".join(notes),
            outer_zone_vertical_extent=round(extent, 3), vertical_excess=round(vertical_excess, 3),
            max_reach_from_shoulder=round(max_reach, 3), outer_column_run=round(outer_column_run, 3),
            inner_column_run=round(inner_column_run, 3),
        )
        out["sides"][side] = info
        if not reasons:
            angles.append(angle)

    L, R = out["sides"].get("left", {}), out["sides"].get("right", {})
    if L.get("angle_deg") is not None and R.get("angle_deg") is not None and abs(L["angle_deg"] - R["angle_deg"]) > 25:
        for s in (L, R):
            s["reliable"] = False
            s["reason"] = (s["reason"] + "; " if s["reason"] else "") + "left/right differ by more than 25 deg"
        angles = []
    out["arm_angle_deg"] = round(float(np.mean(angles)), 1) if angles else None
    out["pose_reliable"] = bool(angles) and len(angles) == 2
    out["pose_class"] = (None if out["arm_angle_deg"] is None else
                         "T" if out["arm_angle_deg"] >= 65 else "A" if out["arm_angle_deg"] >= 25 else "arms down")

    # weapon cue 3: forward protrusion in front of the torso front surface at the same height
    rel_y = (v[:, 1] - y0) / H
    bins = np.clip((rel_y * 50).astype(int), 0, 49)
    slab = np.abs(v[:, 0] - cx) < 0.08 * H
    front = np.full(50, -np.inf)
    np.maximum.at(front, bins[slab], v[slab, 2])
    filled = np.nonzero(np.isfinite(front))[0]
    if len(filled):  # empty height bins take the nearest filled bin
        front = front[filled[np.abs(np.arange(50)[:, None] - filled[None, :]).argmin(1)]]
    head_y = 0.82
    prot = (v[:, 2] - front[bins] > FORWARD * H) & (rel_y > MIN_ROW) & (rel_y < head_y)
    prot_area = 0.0
    if prot.any():
        pts = v[prot][:, :2]
        cells = np.unique(np.floor(pts / (0.01 * H)).astype(int), axis=0)
        prot_area = len(cells) * 1e-4
    in_rows = (rel_y > MIN_ROW) & (rel_y < head_y)
    forward_extent = float(((v[in_rows, 2] - front[bins[in_rows]]) / H).max()) if in_rows.any() else 0.0
    cues = {
        "vertical_at_hand": max(L.get("vertical_excess", 0.0), R.get("vertical_excess", 0.0)),
        "beyond_hand": max(L.get("max_reach_from_shoulder", 0.0), R.get("max_reach_from_shoulder", 0.0)),
        "outer_column_run": max(L.get("outer_column_run", 0.0), R.get("outer_column_run", 0.0)),
        "inner_column_run": max(L.get("inner_column_run", 0.0), R.get("inner_column_run", 0.0)),
        "forward_area": round(prot_area, 4),
        "forward_extent": round(max(forward_extent, 0.0), 4),
    }
    hits = []
    if cues["vertical_at_hand"] > VERTICAL_AT_HAND:
        hits.append("vertical_at_hand")
    if cues["beyond_hand"] > BEYOND_HAND:
        hits.append("beyond_hand")
    if cues["outer_column_run"] > OUTER_COLUMN_RUN:
        hits.append("outer_column_run")
    if cues["inner_column_run"] > INNER_COLUMN_RUN:
        hits.append("inner_column_run")
    if cues["forward_area"] > FORWARD_MIN_AREA or cues["forward_extent"] > FORWARD_EXTENT:
        hits.append("forward")
    out["weapon_cues"] = {k: round(float(x), 4) for k, x in cues.items()}
    out["weapon_suspected"] = bool(hits)
    out["weapon_reasons"] = hits
    out["target_arm_deg"] = target_deg
    out["arm_angle_error_deg"] = None if out["arm_angle_deg"] is None else round(abs(out["arm_angle_deg"] - target_deg), 1)
    return out


def analyze_raw(raw: dict, res: int = 512, target_deg: float = TARGET_ARM_DEG, turn_180: bool = False) -> dict:
    """analyze() on a raw TRELLIS sample (generate_glb.py dump format, z-up).

    turn_180: analyse the sample turned 180 deg about the vertical axis (front -Z -> +Z). The checks assume the
    front faces +Z; TRELLIS sometimes faces -Z (backlog B-004), where wings, tails and capes on the back end up in
    the arm silhouette and the forward cue. generate_glb.py records both to show orientation-dependent results.
    """
    v = to_y_up(raw["mesh"]["vertices"].numpy().astype(np.float32))
    f = raw["mesh"]["faces"].numpy().astype(np.int64)
    if len(v) == 0 or len(f) == 0:
        raise ValueError("empty mesh: nothing to analyse (generate_glb.py skips empty samples)")
    if turn_180:
        v = v * np.array([-1.0, 1.0, -1.0], dtype=np.float32)   # rotation about +Y; keeps the face winding
    return analyze(v, f, res, target_deg)


def front_preview(raw: dict, res: int = 320) -> np.ndarray:
    """Orthographic front view (from +Z) of a raw sample coloured with the mesh decoder's vertex colours."""
    v = to_y_up(raw["mesh"]["vertices"].numpy().astype(np.float32))
    if len(v) == 0:
        raise ValueError("empty mesh: nothing to render (generate_glb.py draws an empty tile instead)")
    f = torch.tensor(raw["mesh"]["faces"].numpy().astype(np.int64), device="cuda")
    attrs = raw["mesh"].get("vertex_attrs")
    colours = attrs[:, :3].float().cuda() if attrs is not None else torch.full((len(v), 3), 0.7, device="cuda")
    lo, hi = v.min(0), v.max(0)
    centre = (lo + hi) / 2
    half = float(max(hi[0] - lo[0], hi[1] - lo[1])) * 0.54
    vt = torch.tensor(v, device="cuda")
    zr = float(hi[2] - lo[2]) / 2 + 1e-6
    clip = torch.stack([(vt[:, 0] - float(centre[0])) / half, (vt[:, 1] - float(centre[1])) / half,
                        -(vt[:, 2] - float(centre[2])) / zr * 0.9, torch.ones_like(vt[:, 0])], -1)
    fid, bary = rasterize(clip, f, res, res)
    col = interpolate(colours, f, fid, bary)
    col = torch.where((fid >= 0)[..., None], col, torch.full_like(col, 0.9))
    return (col.flip(0).clamp(0, 1).cpu().numpy() * 255).astype(np.uint8)
