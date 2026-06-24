"""Visual diff between two treatment screenshots.

The shared core (`compute_diff_grid`, `dilate_small_components`) is fully symmetric with respect to both inputs: the
algorithm gives the same change regions regardless of which screenshot is
passed first (modulo coordinate space — the output is always in img_a's
space).  Three candidate alignments are tried — same-size direct diff,
width-only resize of img_b to img_a's width, and symmetric LANCZOS where
both images are resampled equally — and the candidate with the fewest
flagged tiles wins.  The vertical alignment search uses ±4-row offsets in
both directions for both images, so sub-pixel shifts in either direction
are handled without bias.  When one image is taller, the top-anchored and
bottom-anchored masks are ORed inside the overlap zone so real insertions
are never silently swallowed by the AND.
After dilation, `_filter_thin_components` removes 1-tile-wide stripes that
survive as rendering noise.
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


def dilate_small_components(
    grid: np.ndarray,
    small_max_tiles: int = 30,
    pad: int = 2,
) -> np.ndarray:
    """Pad small connected components by ``pad`` tiles on each side.

    Small components (< ``small_max_tiles``) are expanded so the VLM and the
    reviewer have surrounding context; larger components are left tile-accurate.
    """
    if not grid.any():
        return grid
    rows, cols = grid.shape
    visited = np.zeros_like(grid, dtype=bool)
    out = np.zeros_like(grid)
    neighbours = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    for r0 in range(rows):
        for c0 in range(cols):
            if not grid[r0, c0] or visited[r0, c0]:
                continue
            stack: list[tuple[int, int]] = [(r0, c0)]
            cells: list[tuple[int, int]] = []
            while stack:
                y, x = stack.pop()
                if y < 0 or y >= rows or x < 0 or x >= cols:
                    continue
                if not grid[y, x] or visited[y, x]:
                    continue
                visited[y, x] = True
                cells.append((y, x))
                for dy, dx in neighbours:
                    stack.append((y + dy, x + dx))
            if len(cells) < small_max_tiles:
                ys = [c[0] for c in cells]
                xs = [c[1] for c in cells]
                out[
                    max(0, min(ys) - pad) : min(rows - 1, max(ys) + pad) + 1,
                    max(0, min(xs) - pad) : min(cols - 1, max(xs) + pad) + 1,
                ] = True
            else:
                for y, x in cells:
                    out[y, x] = True
    return out


def _filter_thin_components(grid: np.ndarray, min_tile_dim: int) -> np.ndarray:
    """Drop connected components that span fewer than ``min_tile_dim`` tiles in
    either axis. Removes rendering-noise stripes that survive dilation but
    carry no meaningful height or width.
    """
    if not grid.any():
        return grid
    rows, cols = grid.shape
    visited = np.zeros_like(grid, dtype=bool)
    out = np.zeros_like(grid)
    neighbours = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
    for r0 in range(rows):
        for c0 in range(cols):
            if not grid[r0, c0] or visited[r0, c0]:
                continue
            stack: list[tuple[int, int]] = [(r0, c0)]
            cells: list[tuple[int, int]] = []
            while stack:
                y, x = stack.pop()
                if y < 0 or y >= rows or x < 0 or x >= cols:
                    continue
                if not grid[y, x] or visited[y, x]:
                    continue
                visited[y, x] = True
                cells.append((y, x))
                for dy, dx in neighbours:
                    stack.append((y + dy, x + dx))
            ys = [c[0] for c in cells]
            xs = [c[1] for c in cells]
            if (max(ys) - min(ys) + 1) >= min_tile_dim and (max(xs) - min(xs) + 1) >= min_tile_dim:
                for y, x in cells:
                    out[y, x] = True
    return out


def _diff_grid_for_images(
    img_a: Image.Image,
    img_b: Image.Image,
    tile: int,
    threshold: int,
) -> np.ndarray:
    """Compute the raw boolean tile grid for an already-loaded image pair.

    The output is always in img_a's coordinate space.  Three candidate
    alignments are tried; the one with the fewest flagged tiles is returned.
    Factored out of ``compute_diff_grid`` so the blur-retry path can reuse the
    same logic on blurred copies without re-opening files from disk.
    """
    w_a, h_a = img_a.width, img_a.height
    w_b, h_b = img_b.width, img_b.height
    arr_a = np.asarray(img_a, dtype=np.int16)
    arr_b = np.asarray(img_b, dtype=np.int16)

    search_half = 4
    sample_rows = 200

    def _anchor_diff(
        arr_ref: np.ndarray,
        arr_cmp: np.ndarray,
        h_ref: int,
        h_cmp: int,
        ref_base: int,
        cmp_base: int,
    ) -> tuple[np.ndarray, int, int]:
        """Best-aligned pixel-max diff starting at the given anchor rows.

        Searches both positive deltas (shift cmp down relative to ref) and
        negative deltas (shift ref down relative to cmp), so sub-pixel
        vertical offsets in either direction are handled symmetrically.
        Returns (diff_array, row_count, ref_row_offset) where ref_row_offset
        is the number of ref rows skipped at the top due to a negative delta.
        """
        best_count = 10 ** 18
        best_delta = 0
        for delta in range(-search_half, search_half + 1):
            rp = ref_base + max(0, -delta)
            cp = cmp_base + max(0, delta)
            if rp >= h_ref or cp >= h_cmp:
                continue
            act_d = min(h_ref - rp, h_cmp - cp)
            if act_d <= 0:
                continue
            idx = (
                np.arange(act_d)
                if act_d <= sample_rows
                else np.linspace(0, act_d - 1, sample_rows).astype(np.int64)
            )
            count = int(
                (np.abs(arr_ref[rp + idx] - arr_cmp[cp + idx]).max(axis=2) > threshold).sum()
            )
            if count < best_count:
                best_count, best_delta = count, delta
        rp = ref_base + max(0, -best_delta)
        cp = cmp_base + max(0, best_delta)
        act = min(h_ref - rp, h_cmp - cp)
        return (
            np.abs(arr_ref[rp : rp + act] - arr_cmp[cp : cp + act]).max(axis=2),
            act,
            rp - ref_base,  # ref_row_offset
        )

    def _candidate(
        arr_ref: np.ndarray,
        arr_cmp: np.ndarray,
        h_ref: int,
        h_cmp: int,
        w: int,
    ) -> np.ndarray:
        """Diff tile grid with arr_ref as the fixed coordinate space.

        Two anchor points (top and bottom) are combined with AND; inside any
        height-mismatch overlap zone the anchors are ORed so genuine content
        insertions in either image are never suppressed.
        """
        diff_top, act_top, off_top = _anchor_diff(arr_ref, arr_cmp, h_ref, h_cmp, 0, 0)
        mask_top = np.ones((h_ref, w), dtype=bool)
        mask_top[off_top : off_top + act_top] = diff_top > threshold

        r_off = max(0, h_ref - h_cmp)
        c_off = max(0, h_cmp - h_ref)
        diff_bot, act_bot, off_bot = _anchor_diff(arr_ref, arr_cmp, h_ref, h_cmp, r_off, c_off)
        mask_bot = np.ones((h_ref, w), dtype=bool)
        mask_bot[r_off + off_bot : r_off + off_bot + act_bot] = diff_bot > threshold

        mask = mask_top & mask_bot

        top_end = off_top + act_top
        bot_start = r_off + off_bot
        bot_end = bot_start + act_bot
        if r_off > 0:
            ol_start = max(bot_start, off_top)  # must be >= both bot_start and off_top
            ol_end = min(top_end, bot_end)
            if ol_end > ol_start:
                rs = np.arange(ol_start, ol_end)
                mask[ol_start:ol_end] = (
                    (diff_top[rs - off_top] > threshold)
                    | (diff_bot[rs - bot_start] > threshold)
                )
        elif c_off > 0:
            ol_end = min(top_end, bot_end)
            ol_start = max(off_top, off_bot)
            if ol_end > ol_start:
                rs = np.arange(ol_start, ol_end)
                mask[ol_start:ol_end] = (
                    (diff_top[rs - off_top] > threshold)
                    | (diff_bot[rs - off_bot] > threshold)
                )

        ph, pw = (-h_ref) % tile, (-w) % tile
        padded = np.pad(mask, ((0, ph), (0, pw)))
        H, W = padded.shape
        count_grid = padded.reshape(H // tile, tile, W // tile, tile).sum(axis=(1, 3))
        # Require at least 5 pixels changed per tile — catches sparse text/element
        # changes (e.g. anti-aliased CTA copy) while staying well above the 0-count
        # baseline we observe on pixel-identical or aliasing-only pairs.
        return count_grid >= 5

    candidates: list[np.ndarray] = []

    # Candidate 1: same-width direct diff (skipped when widths differ — no resize needed).
    if w_a == w_b:
        candidates.append(_candidate(arr_a, arr_b, h_a, h_b, w_a))

    # Candidate 2: width-normalised — img_b resized to img_a's width preserving
    # aspect ratio; output remains in img_a's horizontal coordinate space.
    if w_a != w_b:
        new_h_b = round(h_b * w_a / w_b)
        arr_b_w = np.asarray(img_b.resize((w_a, new_h_b), Image.LANCZOS), dtype=np.int16)
        candidates.append(_candidate(arr_a, arr_b_w, h_a, new_h_b, w_a))

    # Candidate 3: symmetric LANCZOS — both images resampled with LANCZOS to
    # img_a's pixel dimensions so any resampling artifacts affect both equally;
    # output is in img_a's coordinate space.
    arr_a_r = np.asarray(img_a.resize((w_a, h_a), Image.LANCZOS), dtype=np.int16)
    arr_b_r = np.asarray(img_b.resize((w_a, h_a), Image.LANCZOS), dtype=np.int16)
    candidates.append(_candidate(arr_a_r, arr_b_r, h_a, h_a, w_a))

    return min(candidates, key=lambda g: int(g.sum()))


def compute_diff_grid(
    path_a: Path,
    path_b: Path,
    tile: int = 32,
    threshold: int = 30,
    min_tile_dim: int = 2,
    blur_radius: int = 0,
) -> tuple[np.ndarray, int, int]:
    """Return ``(tile_grid, width_a, height_a)`` for the two screenshots.

    ``tile_grid`` is a boolean array (one cell per ``tile``-sized square of
    img_a) marking tiles where the two screenshots differ.  Three candidate
    alignments are tried; the one yielding the fewest flagged tiles wins.
    Post-dilation, components thinner than ``min_tile_dim`` tiles on either
    axis are filtered as rendering noise.

    The result is symmetric: the same changed regions are found regardless of
    argument order, expressed in the coordinate space of the first argument.

    ``blur_radius`` applies a Gaussian blur to both images before diffing.
    Callers that observe noisy results (e.g. bbox coverage >60% after dilation)
    can retry with ``blur_radius=1`` to suppress sub-pixel aliasing artifacts.
    """
    img_a = Image.open(path_a).convert("RGB")
    img_b = Image.open(path_b).convert("RGB")
    if blur_radius > 0:
        blur = ImageFilter.GaussianBlur(radius=blur_radius)
        img_a = img_a.filter(blur)
        img_b = img_b.filter(blur)
    w_a, h_a = img_a.width, img_a.height

    grid = _diff_grid_for_images(img_a, img_b, tile, threshold)
    grid = dilate_small_components(grid)
    if min_tile_dim > 1:
        grid = _filter_thin_components(grid, min_tile_dim)
    return grid, w_a, h_a



def bboxes_from_grid(
    grid: np.ndarray,
    w: int,
    h: int,
    tile: int = 32,
    min_tiles: int = 3,
    pad: int = 10,
) -> list[dict]:
    """Extract pixel bounding boxes from a (dilated) boolean tile grid.

    Connected components with fewer than ``min_tiles`` tiles are dropped.
    ``pad`` pixels of padding are added around each box, clamped to image bounds.
    Returns dicts with ``x, y, w, h`` (pixels) and ``*_pct`` variants.
    Sorted top-to-bottom, left-to-right.
    """
    th, tw = grid.shape
    visited = np.zeros_like(grid, dtype=bool)
    boxes: list[dict] = []
    neighbours = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]

    for y0 in range(th):
        for x0 in range(tw):
            if not grid[y0, x0] or visited[y0, x0]:
                continue
            stack: list[tuple[int, int]] = [(y0, x0)]
            cells: list[tuple[int, int]] = []
            while stack:
                y, x = stack.pop()
                if y < 0 or y >= th or x < 0 or x >= tw:
                    continue
                if not grid[y, x] or visited[y, x]:
                    continue
                visited[y, x] = True
                cells.append((y, x))
                for dy, dx in neighbours:
                    stack.append((y + dy, x + dx))
            if len(cells) < min_tiles:
                continue
            ys = [c[0] for c in cells]
            xs = [c[1] for c in cells]
            x1 = max(0, min(xs) * tile - pad)
            y1 = max(0, min(ys) * tile - pad)
            x2 = min(w, (max(xs) + 1) * tile + pad)
            y2 = min(h, (max(ys) + 1) * tile + pad)
            boxes.append({
                "x": x1, "y": y1, "w": x2 - x1, "h": y2 - y1,
                "x_pct": round(100 * x1 / w, 3),
                "y_pct": round(100 * y1 / h, 3),
                "w_pct": round(100 * (x2 - x1) / w, 3),
                "h_pct": round(100 * (y2 - y1) / h, 3),
            })

    boxes.sort(key=lambda b: (b["y"], b["x"]))
    return boxes


def component_bbox_fraction(bboxes: list[dict], w: int, h: int) -> float:
    """Sum of per-component bbox areas as a fraction of image area.

    A 1-pixel layout shift creates anti-aliasing diffs at every text edge,
    yielding many small scattered components whose total bbox area is a small
    fraction of the image.  Genuine content changes produce fewer, larger
    components whose total bbox area is a large fraction.  This metric therefore
    separates aliasing noise (low fraction) from real differences (high fraction)
    without being fooled by empty gaps between distant clusters.
    """
    return sum(b["w"] * b["h"] for b in bboxes) / (w * h)


def pick_box_color(
    img_arr: np.ndarray,
    hue_bins: int = 36,
    max_pixels: int = 10_000,
) -> tuple[int, int, int]:
    """Return a vivid color whose hue is most absent from the image.

    Builds a hue histogram over pixels that are sufficiently saturated and
    bright (grey / white / black are ignored — they can't be confused with a
    vivid annotation border).  The hue bin with the fewest such pixels is
    chosen, and the corresponding fully-saturated, fully-bright RGB color is
    returned.  The result is entirely image-driven: no fixed palette is used.

    ``hue_bins=36`` gives 10° resolution (36 possible output hues).
    """
    import colorsys

    flat = img_arr.reshape(-1, 3)
    if len(flat) > max_pixels:
        flat = flat[:: len(flat) // max_pixels]

    rgb = flat.astype(np.float32) / 255.0
    r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]

    maxc = np.maximum(np.maximum(r, g), b)
    diff = maxc - np.minimum(np.minimum(r, g), b)
    safe_maxc = np.where(maxc > 0, maxc, 1.0)
    sat = np.where(maxc > 0, diff / safe_maxc, 0.0)

    # Only colorful, non-dark pixels matter — grey/white/black can't visually
    # clash with a vivid box border.
    colorful = (sat > 0.25) & (maxc > 0.20)
    if not colorful.any():
        # Fully achromatic image: any vivid color works; pick red.
        return (220, 0, 0)

    safe_diff = np.where(diff > 0, diff, 1.0)
    h = np.zeros(len(r), dtype=np.float32)
    mr = (maxc == r) & colorful
    mg = (maxc == g) & colorful
    mb = (maxc == b) & colorful
    h[mr] = ((g[mr] - b[mr]) / safe_diff[mr]) % 6
    h[mg] = (b[mg] - r[mg]) / safe_diff[mg] + 2
    h[mb] = (r[mb] - g[mb]) / safe_diff[mb] + 4
    h = (h / 6.0) % 1.0

    hist, _ = np.histogram(h[colorful], bins=hue_bins, range=(0.0, 1.0))
    occupied = hist > 0

    if occupied.all():
        # Every hue is present — fall back to the least-populated bin.
        emptiest = int(np.argmin(hist))
        gap_center = emptiest + 0.5
    else:
        # Find the largest circular gap (run of unoccupied bins) and pick its
        # centre — that hue is furthest from everything present in the image.
        occ2 = np.concatenate([occupied, occupied])
        best_len, best_start = 0, 0
        i = 0
        while i < len(occ2):
            if not occ2[i]:
                j = i
                while j < len(occ2) and not occ2[j]:
                    j += 1
                if j - i > best_len:
                    best_len = j - i
                    best_start = i % hue_bins
                i = j
            else:
                i += 1
        gap_center = (best_start + best_len / 2.0) % hue_bins

    hue_center = (gap_center + 0.5) / hue_bins
    ro, go, bo = colorsys.hsv_to_rgb(hue_center % 1.0, 1.0, 1.0)
    return (round(ro * 255), round(go * 255), round(bo * 255))


def annotate_image_with_bboxes(
    img: Image.Image,
    bboxes: list[dict],
    color: tuple[int, int, int],
    border_width: int = 4,
) -> bytes:
    """Draw colored border rectangles around changed regions and return PNG bytes.

    Only the border is drawn (no fill) so the underlying content stays visible.
    """
    annotated = img.copy().convert("RGB")
    draw = ImageDraw.Draw(annotated)
    for box in bboxes:
        x1, y1 = box["x"], box["y"]
        x2, y2 = x1 + box["w"], y1 + box["h"]
        for i in range(border_width):
            draw.rectangle([x1 + i, y1 + i, x2 - i, y2 - i], outline=color)
    buf = io.BytesIO()
    annotated.save(buf, format="PNG")
    return buf.getvalue()



def stitch_region_crops(
    img: Image.Image,
    bboxes: list[dict],
    gap: int = 20,
    min_width: int = 600,
) -> bytes:
    """Crop each bbox from img, upscale, and stack vertically into a single PNG.

    All crops are scaled to the same width (the widest upscaled crop) so the
    canvas is uniform.  A ``gap``-pixel white strip separates consecutive crops.
    """
    crops: list[Image.Image] = []
    for b in bboxes:
        x, y, w, h = b["x"], b["y"], b["w"], b["h"]
        crop = img.crop((x, y, x + w, y + h))
        if 0 < crop.width < min_width:
            scale = min_width / crop.width
            crop = crop.resize(
                (min_width, max(1, round(crop.height * scale))), Image.LANCZOS
            )
        crops.append(crop)

    target_w = max(c.width for c in crops)
    total_h = sum(c.height for c in crops) + gap * (len(crops) - 1)
    canvas = Image.new("RGB", (target_w, total_h), color=(255, 255, 255))
    y_off = 0
    for crop in crops:
        canvas.paste(crop, (0, y_off))
        y_off += crop.height + gap

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


