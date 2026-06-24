"""
Section node detection (v2 bottom-up).
Extracted from phase3_merge_paruls_visualization.py.
"""

import copy
import logging
from collections import Counter
from typing import Tuple, List, Set

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


def _count_leaves(node_xpath: str, nodes: dict) -> int:
    """Count the number of leaf nodes (no children) in the subtree rooted at *node_xpath*."""
    if node_xpath not in nodes:
        return 0
    children = nodes[node_xpath].get("children", [])
    if not children:
        return 1
    return sum(_count_leaves(c, nodes) for c in children if c in nodes)


def _subdivide_large_sections(
    section_dict: dict,
    all_nodes: dict,
    max_leaves: int,
    content_width: float,
    width_ratio: float = 0.95,
) -> dict:
    """Replace section nodes that have more than *max_leaves* leaves with
    their children from *all_nodes*, recursively, until every section node
    has at most *max_leaves* leaves (or is itself a leaf and cannot be
    split further).

    A node is only subdivided when **every** child has a width that spans
    the content area (``>= width_ratio * content_width``).  If any child
    is narrower, the parent is kept as-is because its children on their
    own do not qualify as full-width section nodes.
    """
    min_section_width = width_ratio * content_width
    result: dict = {}
    queue = list(section_dict.values())

    while queue:
        node = queue.pop(0)
        xp = node["xpath"]
        leaf_count = _count_leaves(xp, all_nodes)

        if leaf_count <= max_leaves:
            result[xp] = node
            continue

        children_xpaths = all_nodes.get(xp, {}).get("children", [])
        if not children_xpaths:
            result[xp] = node
            continue

        children_nodes = [all_nodes[c] for c in children_xpaths if c in all_nodes]
        all_children_wide = all(
            c.get("bbox") and c["bbox"].get("width", 0) >= min_section_width
            for c in children_nodes
        )

        if not all_children_wide:
            result[xp] = node
            continue

        for child in children_nodes:
            queue.append(child)

    return result


def _merge_ranges(ranges: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """Merge overlapping/adjacent 1-D ranges."""
    if not ranges:
        return []
    sorted_r = sorted(ranges, key=lambda r: r[0])
    merged = [sorted_r[0]]
    for start, end in sorted_r[1:]:
        if start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _find_horizontal_groups(nodes_list: List[dict], y_tolerance: int = 20) -> List[List[dict]]:
    """Group nodes that share a similar vertical band."""
    if not nodes_list:
        return []
    sorted_nodes = sorted(nodes_list, key=lambda n: n["bbox"]["y"])
    groups: List[List[dict]] = []
    current_group = [sorted_nodes[0]]
    y_min = sorted_nodes[0]["bbox"]["y"]
    y_max = y_min + sorted_nodes[0]["bbox"]["height"]

    for node in sorted_nodes[1:]:
        ny = node["bbox"]["y"]
        nh = node["bbox"]["height"]
        if ny <= y_max + y_tolerance and ny + nh >= y_min - y_tolerance:
            current_group.append(node)
            y_min = min(y_min, ny)
            y_max = max(y_max, ny + nh)
        else:
            groups.append(current_group)
            current_group = [node]
            y_min = ny
            y_max = ny + nh

    if current_group:
        groups.append(current_group)
    return groups


def _group_spans_width(group: List[dict], content_width: float, threshold: float = 0.95) -> bool:
    """Check whether a group of elements collectively spans the content width."""
    x_ranges = [(n["bbox"]["x"], n["bbox"]["x"] + n["bbox"]["width"]) for n in group]
    merged = _merge_ranges(x_ranges)
    total = sum(end - start for start, end in merged)
    return total >= content_width * threshold


def _remove_contained_nodes(nodes_list: List[dict], margin: int = 2) -> List[dict]:
    """Remove nodes fully enclosed within a larger node."""
    if not nodes_list:
        return []
    sorted_by_area = sorted(nodes_list, key=lambda n: n["area"], reverse=True)
    kept: List[dict] = []
    for node in sorted_by_area:
        contained = False
        nb = node["bbox"]
        for kn in kept:
            kb = kn["bbox"]
            if (
                nb["x"] >= kb["x"] - margin
                and nb["y"] >= kb["y"] - margin
                and nb["x"] + nb["width"] <= kb["x"] + kb["width"] + margin
                and nb["y"] + nb["height"] <= kb["y"] + kb["height"] + margin
            ):
                contained = True
                break
        if not contained:
            kept.append(node)
    kept.sort(key=lambda n: n["bbox"]["y"])
    return kept


def _remove_overlapping_nodes(nodes_list: List[dict]) -> List[dict]:
    """Remove partially overlapping nodes, keeping the larger one.

    After gap filling, two nodes may partially overlap (neither fully
    contains the other).  Sort by area descending; for each node, drop
    any later node whose bbox overlaps it.
    """
    if not nodes_list:
        return []
    sorted_by_area = sorted(nodes_list, key=lambda n: n["area"], reverse=True)
    kept: List[dict] = []
    for node in sorted_by_area:
        nb = node["bbox"]
        overlaps_kept = False
        for kn in kept:
            kb = kn["bbox"]
            # Check bbox overlap (x1/y1/x2/y2 style)
            n_x2 = nb["x"] + nb["width"]
            n_y2 = nb["y"] + nb["height"]
            k_x2 = kb["x"] + kb["width"]
            k_y2 = kb["y"] + kb["height"]
            if nb["x"] < k_x2 and n_x2 > kb["x"] and nb["y"] < k_y2 and n_y2 > kb["y"]:
                overlaps_kept = True
                break
        if not overlaps_kept:
            kept.append(node)
    kept.sort(key=lambda n: n["bbox"]["y"])
    return kept


def _get_section_nodes_v2(nodes: dict, page_width: float, page_height: float, body_xpath: str = "/html/body", pass1_tolerance: float = 0.98, pass2_tolerance: float = 0.8, thresh_height=None, max_leaves: int = None) -> Tuple[dict, float]:
    """
    Bottom-up section detection (like extract_fragments + extract_fragments_second_pass).
    Uses bbox from visual tree. Two passes with tolerance 0.98 then 0.8.
    Applies _filter_non_overlapping_fragments. Returns (section_nodes_dict, content_width).
    """
    _logger.info("Detecting section nodes (v2 bottom-up) ...")
    nodes_copy = {k: v for k, v in copy.deepcopy(nodes).items() if v.get("bbox")}
    if thresh_height is not None and thresh_height > 0:
        nodes_copy = {
            k: v for k, v in nodes_copy.items()
            if v["bbox"]["height"] <= 0.9 * page_height
            and v["bbox"]["height"] <= thresh_height
        }
    else:
        nodes_copy = {k: v for k, v in nodes_copy.items()
                      if v["bbox"]["height"] <= 0.9 * page_height}
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

    # Group remaining narrow elements horizontally
    remaining = [
        nodes_copy[xp] for xp in nodes_copy
        if xp not in processed_xpaths
        and nodes_copy[xp]["bbox"]["width"] < 0.95 * content_width
    ]
    for group in _find_horizontal_groups(remaining):
        if len(group) > 1 and _group_spans_width(group, content_width):
            for n in group:
                bbox = n["bbox"]
                candidates.append((bbox["y"], bbox["x"], bbox["width"], bbox["height"], n))
                mark_descendants_processed(n, processed_xpaths)

    if not candidates:
        return {}, content_width
    candidates.sort(key=lambda c: (c[0], c[1]))
    filtered = _filter_non_overlapping_fragments(candidates)
    final = _remove_contained_nodes(filtered)

    # Fill vertical gaps that aren't covered by any section node
    merged_y = _merge_ranges(
        [(n["bbox"]["y"], n["bbox"]["y"] + n["bbox"]["height"]) for n in final]
    )
    gaps: List[Tuple[float, float]] = []
    if not merged_y:
        gaps = [(0, page_height)]
    else:
        if merged_y[0][0] > 0:
            gaps.append((0, merged_y[0][0]))
        for i in range(len(merged_y) - 1):
            gaps.append((merged_y[i][1], merged_y[i + 1][0]))
        if merged_y[-1][1] < page_height:
            gaps.append((merged_y[-1][1], page_height))

    if gaps:
        # Collect existing section xpaths to avoid re-adding them
        final_xpaths = {n["xpath"] for n in final}
        gap_nodes: List[dict] = []
        # Search the ORIGINAL nodes (pre-height-filter) so we can find
        # content in gaps whose parent was height-filtered away.
        gap_source = {k: v for k, v in copy.deepcopy(nodes).items()
                      if v.get("bbox") and v["bbox"].get("width", 0) > 0
                      and v["bbox"].get("height", 0) > 0
                      and k not in final_xpaths}
        for node in gap_source.values():
            bbox = node["bbox"]
            node_y1 = bbox["y"]
            node_y2 = bbox["y"] + bbox["height"]
            for g_start, g_end in gaps:
                # Accept node if it has any meaningful overlap with the
                # gap (> 1px).  The overlap removal pass that runs after
                # gap filling will clean up nodes that spill too far
                # into existing sections.
                overlap_start = max(node_y1, g_start)
                overlap_end = min(node_y2, g_end)
                overlap = max(0, overlap_end - overlap_start)
                gap_size = g_end - g_start
                if overlap > 1 and bbox["height"] <= gap_size * 3:
                    gap_nodes.append(node)
                    break  # don't add same node for multiple gaps
        gap_nodes.sort(key=lambda n: (n["bbox"]["y"], -n["bbox"]["width"]))
        checked: Set[int] = set()
        for idx, node in enumerate(gap_nodes):
            if idx in checked:
                continue
            final.append(node)
            for idx2, node2 in enumerate(gap_nodes):
                if (
                    node2["bbox"]["y"] >= node["bbox"]["y"]
                    and node2["bbox"]["y"] + node2["bbox"]["height"]
                    <= node["bbox"]["y"] + node["bbox"]["height"]
                ):
                    checked.add(idx2)

    # Deduplicate again after gap filling
    final = _remove_contained_nodes(final)
    # Remove partially overlapping nodes: when two nodes overlap, keep the larger one
    final = _remove_overlapping_nodes(final)
    section_dict = {n["xpath"]: n for n in final}
    _logger.info("Found %d section nodes (v2)", len(section_dict))

    if max_leaves is not None and max_leaves > 0:
        before = len(section_dict)
        section_dict = _subdivide_large_sections(section_dict, nodes, max_leaves, content_width)
        if len(section_dict) != before:
            _logger.info(
                "Subdivided oversized sections: %d -> %d (max_leaves=%d)",
                before, len(section_dict), max_leaves,
            )
            # Subdivision may introduce overlaps with gap-filled nodes; clean up
            final2 = _remove_contained_nodes(list(section_dict.values()))
            final2 = _remove_overlapping_nodes(final2)
            section_dict = {n["xpath"]: n for n in final2}

    return section_dict, content_width
