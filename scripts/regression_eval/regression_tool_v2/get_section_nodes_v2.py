"""
Section node detection (v2 bottom-up).
Extracted from phase3_merge_paruls_visualization.py.
"""

import copy
import logging
from collections import Counter
from typing import Tuple

_logger = logging.getLogger("analyze_template")


def _rects_overlap(t1, l1, w1, h1, t2, l2, w2, h2):
    """True if two rectangles (top, left, width, height) overlap."""
    if w1 <= 0 or h1 <= 0 or w2 <= 0 or h2 <= 0:
        return False
    if l1 + w1 <= l2 or l2 + w2 <= l1:
        return False
    if t1 + h1 <= t2 or t2 + h2 <= t1:
        return False
    return True


def _filter_non_overlapping_fragments(boxes):
    """boxes: list of (top, left, width, height, fragment). Return list of fragments with no overlapping bboxes (keep first in doc order)."""
    kept = []
    for top, left, w, h, f in boxes:
        overlaps = False
        for k_top, k_left, k_w, k_h, _ in kept:
            if _rects_overlap(top, left, w, h, k_top, k_left, k_w, k_h):
                overlaps = True
                break
        if not overlaps:
            kept.append((top, left, w, h, f))
    return [f for _, _, _, _, f in kept]


def _get_section_nodes_v2(nodes: dict, page_width: float, page_height: float, body_xpath: str = "/html/body", pass1_tolerance: float = 0.98, pass2_tolerance: float = 0.8, thresh_height=None) -> Tuple[dict, float]:
    """
    Bottom-up section detection (like extract_fragments + extract_fragments_second_pass).
    Uses bbox from visual tree. Two passes with tolerance 0.98 then 0.8.
    Applies _filter_non_overlapping_fragments. Returns (section_nodes_dict, content_width).
    """
    _logger.info("Detecting section nodes (v2 bottom-up) ...")
    nodes_copy = {k: v for k, v in copy.deepcopy(nodes).items() if v.get("bbox")}
    if not nodes_copy:
        return {}, page_width
    significant_widths = [v["bbox"]["width"] for v in nodes_copy.values() if v["bbox"]["width"] >= 0.3 * page_width]
    content_width = Counter([round(w / 10) * 10 for w in significant_widths]).most_common(1)[0][0] if significant_widths else page_width
    if content_width >= 0.95 * page_width:
        content_width = page_width
    ref_width = content_width

    def get_leaves():
        return [n for n in nodes_copy.values() if not n.get("children")]

    def traverse_upward(leaf, tolerance):
        current = leaf
        while current and current["xpath"] != body_xpath:
            bbox = current.get("bbox")
            if not bbox:
                parent_xp = current.get("parent")
                current = nodes_copy.get(parent_xp) if parent_xp else None
                continue
            # Skip full-page-width containers — they are structural wrappers, not sections
            if bbox["width"] >= 0.95 * page_width:
                parent_xp = current.get("parent")
                current = nodes_copy.get(parent_xp) if parent_xp else None
                continue
            if bbox["width"] >= tolerance * ref_width:
                return current
            parent_xp = current.get("parent")
            current = nodes_copy.get(parent_xp) if parent_xp else None
        return None

    def mark_descendants_processed(node, processed):
        processed.add(node["xpath"])
        for ch_xp in node.get("children", []):
            if ch_xp in nodes_copy:
                mark_descendants_processed(nodes_copy[ch_xp], processed)

    processed_xpaths = set()
    candidates = []

    for leaf in get_leaves():
        if leaf["xpath"] in processed_xpaths:
            continue
        node = traverse_upward(leaf, pass1_tolerance)
        if node and node["xpath"] not in processed_xpaths:
            bbox = node["bbox"]
            candidates.append((bbox["y"], bbox["x"], bbox["width"], bbox["height"], node))
            mark_descendants_processed(node, processed_xpaths)

    for leaf in get_leaves():
        if leaf["xpath"] in processed_xpaths:
            continue
        node = traverse_upward(leaf, pass2_tolerance)
        if node and node["xpath"] not in processed_xpaths:
            bbox = node["bbox"]
            candidates.append((bbox["y"], bbox["x"], bbox["width"], bbox["height"], node))
            mark_descendants_processed(node, processed_xpaths)

    if not candidates:
        return {}, content_width
    candidates.sort(key=lambda c: (c[0], c[1]))
    filtered = _filter_non_overlapping_fragments(candidates)
    section_dict = {n["xpath"]: n for n in filtered}
    _logger.info("Found %d section nodes (v2)", len(section_dict))
    return section_dict, content_width
