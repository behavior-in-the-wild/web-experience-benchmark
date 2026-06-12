from __future__ import annotations

"""
Utility functions for building the visual DOM tree and detecting section nodes
from an HTML template.

Exported:
    _crop_element_from_full_page  – crop an element region from a full-page PIL image
    build_visual_tree             – build a pruned tree of visually relevant elements
    get_section_nodes             – identify section-level nodes in the visual tree
"""

import copy
import io
import json
import logging
import os
import re
import signal
import sys
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
try:
    import easyocr as _easyocr
    _EASYOCR_AVAILABLE = True
except ImportError:
    _easyocr = None  # type: ignore
    _EASYOCR_AVAILABLE = False

import html5lib
from lxml import html as lxml_html

# ---------------------------------------------------------------------------
# Imports from local dependencies (all in the same directory)
# ---------------------------------------------------------------------------
_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from utils import (
    get_element_xpath,
    transform_xpath,
)

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None  # type: ignore[assignment, misc]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  Build the visual tree of DOM elements
# ═══════════════════════════════════════════════════════════════════════════

def _crop_element_from_full_page(full_page_img, bbox: dict):
    """Crop an element region from the full-page PIL Image using its bbox.

    Returns a PIL Image crop, or None if the bbox is invalid or PIL is unavailable.
    """
    if full_page_img is None or bbox is None:
        return None
    x, y = bbox.get("x", 0), bbox.get("y", 0)
    w, h = bbox.get("width", 0), bbox.get("height", 0)
    if w <= 0 or h <= 0:
        return None
    return full_page_img.crop((int(x), int(y), int(x + w), int(y + h)))


def _load_display_none_xpaths(folder: str) -> set:
    """Load the set of display:none xpaths from the file written by the screenshot taker."""
    path = os.path.join(folder, "display_none_xpaths.json")
    if os.path.exists(path):
        try:
            with open(path, "r") as fh:
                return set(json.load(fh))
        except Exception:
            pass
    return set()


def _traverse_and_build(
    nodes: dict,
    folder: str,
    element,
    ocr_reader,  # easyocr.Reader or None
    full_page_img=None,
    parent_xpath: Optional[str] = None,
    curr_depth: int = 0,
    debug_crops_dir: Optional[str] = None,
    display_none_xpaths: Optional[set] = None,
) -> None:
    """Recursively traverse the DOM and populate *nodes* with bbox metadata.

    Args:
        debug_crops_dir: If set, every element crop produced from the full-page
            screenshot is saved as ``<transformed_xpath>.png`` in this directory
            for visual inspection.
        display_none_xpaths: Set of xpaths known to have display:none, loaded
            from display_none_xpaths.json written by the screenshot taker.
    """
    if not isinstance(element.tag, str):
        return

    xpath = get_element_xpath(element)
    transformed = transform_xpath(xpath)

    def _is_display_none(el) -> bool:
        """Return True if the element has display: none.

        Checks the display_none_xpaths set first (populated from Playwright's
        computed styles), then falls back to reading the per-element .json file.
        """
        if not isinstance(el.tag, str):
            return False
        el_xpath = get_element_xpath(el)
        if display_none_xpaths is not None and el_xpath in display_none_xpaths:
            return True
        t_xp = transform_xpath(el_xpath)
        style_file = os.path.join(folder, f"{t_xp}.json")
        if os.path.exists(style_file):
            try:
                with open(style_file, "r") as _fh:
                    return json.load(_fh).get("display", "block").strip().lower() == "none"
            except Exception:
                pass
        return False

    def _itertext_visible(el) -> List[str]:
        """Yield text from el and its descendants, skipping display:none subtrees."""
        parts: List[str] = []
        if el.text:
            parts.append(el.text)
        for child in el:
            if not isinstance(child.tag, str):
                continue
            if not _is_display_none(child):
                parts.extend(_itertext_visible(child))
            if child.tail:
                parts.append(child.tail)
        return parts

    text_content = "".join(_itertext_visible(element)).strip()
    has_image = element.tag == "img" or element.tag == "svg" or element.tag == "canvas" or element.tag == "picture" or element.tag == "video"
    element_tag = element.tag

    # Load bounding-box data first (needed for OCR crop)
    bbox_file = os.path.join(folder, f"{transformed}.bbox.json")
    bbox: Optional[dict] = None
    area = 0
    if os.path.exists(bbox_file):
        try:
            with open(bbox_file, "r") as fh:
                data = json.load(fh)
                bbox = data.get("bbox", {})
                if bbox:
                    area = bbox.get("width", 0) * bbox.get("height", 0)
        except Exception as exc:
            logger.warning("Error reading bbox %s: %s", bbox_file, exc)

    # Also treat elements with a CSS background-image as image-bearing so they
    # are not pruned by _filter_empty_elements (background images are invisible
    # to the tag-based has_image check above).
    if not has_image:
        style_file = os.path.join(folder, f"{transformed}.json")
        if os.path.exists(style_file):
            try:
                with open(style_file, "r") as fh:
                    computed = json.load(fh)
                bg = computed.get("background-image", "none").strip()
                if bg and bg != "none":
                    has_image = True
            except Exception:
                pass

    if has_image:
        # if element has image, perform OCR on the image and store the text content in the element
        html_snippet_path = os.path.join(folder, f"{transformed}.html")
        if os.path.exists(html_snippet_path):
            with open(html_snippet_path, "r") as fh:
                html_snippet = fh.read()
            src = re.search(r"src=\"(.*?)\"", html_snippet)
            if src:
                src = src.group(1)
                if "placehold.co" in src:
                    visible_text = src.split("placehold.co/")[-1].split("/")[0]
                    if not re.match(r"^\d+\s*x\s*\d+$", visible_text):
                        text_content += visible_text
                else:
                    cropped = _crop_element_from_full_page(full_page_img, bbox)
                    if cropped is not None:
                        if debug_crops_dir is not None:
                            cropped.save(os.path.join(debug_crops_dir, f"{transformed}.png"))
                        if ocr_reader is not None:
                            import numpy as np
                            # Convert cropped PIL RGB image to numpy BGR array for OCR
                            cropped_bgr = np.array(cropped)[:, :, ::-1]
                            ocr_text = ocr_reader.readtext(cropped_bgr, detail=0)
                            ocr_text = [t for t in ocr_text if not re.match(r"^\d+\s*x\s*\d+$", t)]
                            ocr_text = " ".join(ocr_text) if ocr_text else ""
                            text_content += ocr_text

    nodes[xpath] = {
        "xpath": xpath,
        "transformed_xpath": transformed,
        "text_content": text_content,
        "bbox": bbox,
        "area": area,
        "parent": parent_xpath,
        "children": [],
        "contains_image": has_image,
        "depth": curr_depth,
        "tag": element_tag
    }

    if parent_xpath and parent_xpath in nodes:
        nodes[parent_xpath]["children"].append(xpath)

    for child in element:
        _traverse_and_build(nodes, folder, child, ocr_reader=ocr_reader, full_page_img=full_page_img, parent_xpath=xpath, curr_depth=curr_depth + 1, debug_crops_dir=debug_crops_dir, display_none_xpaths=display_none_xpaths)

    

def _filter_zero_area(nodes: dict, element: dict, protected_xpaths: Optional[Set[str]] = None) -> Set[str]:
    """Remove elements with zero area, re-parenting their children."""
    redundant: Set[str] = set()
    protected_xpaths = protected_xpaths or set()

    if element["area"] == 0 and element["xpath"] not in protected_xpaths:
        parent_xp = element["parent"]
        if parent_xp is not None and parent_xp in nodes:
            nodes[parent_xp]["children"].remove(element["xpath"])
        for child_xp in element["children"]:
            nodes[child_xp]["parent"] = parent_xp
            nodes[child_xp]["depth"] = element["depth"]
            if parent_xp is not None and parent_xp in nodes:
                nodes[parent_xp]["children"].append(child_xp)
        redundant.add(element["transformed_xpath"])
        del nodes[element["xpath"]]

    children_snapshot = copy.deepcopy(element["children"])
    for child_xp in children_snapshot:
        if child_xp in nodes:
            redundant.update(_filter_zero_area(nodes, nodes[child_xp], protected_xpaths))
    return redundant


def _filter_single_child(nodes: dict, element: dict, section_node_xpaths: Optional[Dict[str, float]] = None) -> Set[str]:
    """Remove elements that are the sole child of their parent with area <= parent.
    
    If section_node_xpaths are provided (each element containing a generated section node's xpath : its original counterpart's width), the single child filtering for these nodes' children is a bit different: If the parent is one of section_nodes_xpaths, with width > original counterpart's width, and the single child has area <= parent's area, then the parent should be marked redundant, instead of the child.
    """
    redundant: Set[str] = set()

    parent_xp = element["parent"]
    element_xp = element["xpath"]
    if parent_xp is not None and parent_xp in nodes:
        parent = nodes[parent_xp]
        if (
            len(parent["children"]) == 1
            and element["area"] <= parent["area"]
            and (element["text_content"] == parent["text_content"] or (element["contains_image"] and parent["text_content"].strip() == ""))
        ):
            if section_node_xpaths is None or parent_xp not in section_node_xpaths or section_node_xpaths[parent_xp] >= parent["bbox"]["width"] or element["area"] == parent["area"]:
                redundant.add(element["transformed_xpath"])
                if element["contains_image"]:
                    parent["contains_image"] = True
                    parent["text_content"] += element["text_content"]
                del nodes[element["xpath"]]
                parent["children"].remove(element["xpath"])
                for child_xp in element["children"]:
                    nodes[child_xp]["parent"] = parent_xp
                    nodes[child_xp]["depth"] = element["depth"]
                    parent["children"].append(child_xp)
            else:
                grandparent_xp = parent["parent"]
                redundant.add(parent_xp)
                if grandparent_xp is not None and grandparent_xp in nodes:
                    grandparent = nodes[grandparent_xp]
                    if parent["contains_image"]:
                        grandparent["contains_image"] = True
                        grandparent["text_content"] += parent["text_content"]
                    grandparent["children"].remove(parent_xp)
                    nodes[element_xp]["parent"] = grandparent_xp
                    nodes[element_xp]["depth"] = parent["depth"]
                    grandparent["children"].append(element_xp)
                    del section_node_xpaths[parent_xp]
                    del nodes[parent_xp]

    children_snapshot = copy.deepcopy(element["children"])
    for child_xp in children_snapshot:
        if child_xp in nodes:
            redundant.update(_filter_single_child(nodes, nodes[child_xp], section_node_xpaths))
    return redundant


def _filter_subtext(nodes: dict) -> Set[str]:
    """Remove elements that are subtext within a p tag that has other text content."""
    ENHANCED_TEXT_TAGS = ["strong", "a", "sup", "sub", "span", "b", "i", "em", "u", "s", "del", "ins", "small", "big", "mark"]
    redundant: Set[str] = set()

    nodes_copy = OrderedDict({k: v for k, v in sorted(nodes.items(), key=lambda item: item[1]["depth"], reverse=True)})

    # ASSERT: The tree needs to be traversed bottom-up

    nodes_xpaths = list(nodes_copy.keys())
    for element_xp in nodes_xpaths:
        if element_xp in nodes_copy:
            element = nodes_copy[element_xp]
            # if this element is one of the enhanced text tags with parent as <p> tag or an enhanced text tag, then mark it and its enhanced text siblings as redundant if the parent has other non-enhanced text content
            if element["tag"] in ENHANCED_TEXT_TAGS:
                parent_xp = element["parent"]
                if parent_xp is not None and parent_xp in nodes_copy:
                    parent = nodes_copy[parent_xp]
                    # if parent["tag"] == "p" or parent["tag"] in ENHANCED_TEXT_TAGS: # maybe we don't need to consider this condition
                    # get the text content of the parent and siblings that are ENHANCED_TEXT_TAGS
                    enhanced_text_content = ""
                    for child_xp in parent["children"]:
                        if child_xp in nodes_copy and nodes_copy[child_xp]["tag"] in ENHANCED_TEXT_TAGS:
                            enhanced_text_content += nodes_copy[child_xp]["text_content"]
                    enhanced_text_content = re.sub(r"\s+", " ", enhanced_text_content).strip()
                    parent_text_content = re.sub(r"\s+", " ", parent["text_content"]).strip()
                    if enhanced_text_content != parent_text_content:
                        # parent has other non-enhanced text content, so mark the element and its enhanced text siblings as redundant
                        children_snapshot = copy.deepcopy(parent["children"])
                        for child_xp in children_snapshot:
                            if child_xp in nodes_copy and nodes_copy[child_xp]["tag"] in ENHANCED_TEXT_TAGS:
                                redundant.add(nodes_copy[child_xp]["transformed_xpath"])
                                if nodes_copy[child_xp]["contains_image"]:
                                    parent["contains_image"] = True
                                    parent["text_content"] += nodes_copy[child_xp]["text_content"]
                        for child_xp in children_snapshot:
                            if child_xp in nodes_copy and nodes_copy[child_xp]["tag"] in ENHANCED_TEXT_TAGS:
                                child_xp_node = copy.deepcopy(nodes_copy[child_xp])
                                del nodes_copy[child_xp]
                                parent["children"].remove(child_xp)
                                for grandchild_xp in child_xp_node["children"]:
                                    nodes_copy[grandchild_xp]["parent"] = parent_xp
                                    nodes_copy[grandchild_xp]["depth"] = child_xp_node["depth"]
                                    parent["children"].append(grandchild_xp)

    
    nodes.clear()
    nodes.update(nodes_copy)
    return redundant

def _filter_empty_elements(nodes: dict) -> Set[str]:
    """Remove elements that have no text content AND no image, or are tracking pixels (generally of size 1x1)"""
    redundant: Set[str] = set()
    nodes_copy = OrderedDict({k: v for k, v in sorted(nodes.items(), key=lambda item: item[1]["depth"], reverse=True)})
    nodes_xpaths = list(nodes_copy.keys())
    for element_xp in nodes_xpaths:
        if element_xp in nodes_copy:
            element = nodes_copy[element_xp]
            if len(element["children"]) == 0 and element["text_content"].strip() == "" and not element["contains_image"]:
                parent_xp = element["parent"]
                if parent_xp is not None and parent_xp in nodes_copy:
                    nodes_copy[parent_xp]["children"].remove(element["xpath"])
                redundant.add(element["transformed_xpath"])
                del nodes_copy[element_xp]
            # also remove image elements that are tracking pixels (generally of size 1x1)
            if element["contains_image"] and element["bbox"]["width"] == 1 and element["bbox"]["height"] == 1:
                parent_xp = element["parent"]
                if parent_xp is not None and parent_xp in nodes_copy:
                    nodes_copy[parent_xp]["children"].remove(element["xpath"])
                redundant.add(element["transformed_xpath"])
                del nodes_copy[element_xp]

    nodes.clear()
    nodes.update(nodes_copy)
    return redundant

def _is_image_leaf(node: dict) -> bool:
    """True if *node* is an image element with no children and a valid bbox."""
    return (
        bool(node.get("contains_image"))
        and not node.get("children")
        and bool(node.get("bbox"))
    )


def _merge_adjacent_image_nodes(nodes: dict) -> int:
    """Collapse a parent whose children are *all* image leaves into a
    single image-leaf node (the parent itself).

    For each parent node, if every child is an image leaf (no non-image
    siblings), all children are removed and the parent is promoted to an
    image leaf: its ``contains_image`` flag is set, its ``children`` list
    is cleared, and its ``text_content`` is the concatenation of all
    children's text.  The parent's own bbox is kept as-is (it already
    encloses all children).

    If the parent has any non-image child, no merge is performed.

    Returns:
        Number of child nodes removed.
    """
    removed = 0
    # Process deepest nodes first so that a merged node is recorded in
    # `already_merged` before its ancestors are evaluated.
    parent_xpaths = sorted(
        [xp for xp, n in nodes.items() if n.get("children")],
        key=lambda xp: nodes[xp].get("depth", 0),
        reverse=True,
    )

    # Track xpaths that were produced by a previous merge so that their
    # ancestors are not eligible for merging again.
    already_merged: set = set()

    for parent_xp in parent_xpaths:
        if parent_xp not in nodes:
            continue
        parent = nodes[parent_xp]
        children_xpaths = parent.get("children", [])
        if len(children_xpaths) < 2:
            continue

        all_image_leaves = all(
            xp in nodes and _is_image_leaf(nodes[xp])
            for xp in children_xpaths
        )
        if not all_image_leaves:
            continue

        # Do not merge if any child was itself the result of a prior merge.
        if any(xp in already_merged for xp in children_xpaths):
            continue

        # Only merge if all children form a single tight horizontal row or
        # vertical column.  Two children are horizontally adjacent when they
        # sit side-by-side: the horizontal distance between their closer
        # edges is within 2px, and their top edges are within 10px of each
        #  other.  Vertically adjacent is the mirror: the vertical distance
        # between their closer edges is within 2px, and their left edges are
        # within 10px of each other. We check every consecutive pair after
        # sorting by x (horizontal check) or y (vertical check).
        child_bboxes = [nodes[xp]["bbox"] for xp in children_xpaths if nodes[xp].get("bbox")]

        if len(child_bboxes) >= 2:
            sorted_by_x = sorted(child_bboxes, key=lambda b: b["x"])
            horizontally_adjacent = all(
                b["x"] - (a["x"] + a["width"]) < 2 and abs(a["y"] - b["y"]) < 10
                for a, b in zip(sorted_by_x, sorted_by_x[1:])
            )

            sorted_by_y = sorted(child_bboxes, key=lambda b: b["y"])
            vertically_adjacent = all(
                b["y"] - (a["y"] + a["height"]) < 2 and abs(a["x"] - b["x"]) < 10
                for a, b in zip(sorted_by_y, sorted_by_y[1:])
            )

            if not (horizontally_adjacent or vertically_adjacent):
                continue

        merged_text = " ".join(
            nodes[xp]["text_content"]
            for xp in children_xpaths
            if xp in nodes and nodes[xp]["text_content"]
        ).strip()

        for child_xp in list(children_xpaths):
            if child_xp in nodes:
                del nodes[child_xp]
                removed += 1

        parent["children"] = []
        parent["contains_image"] = True
        parent["text_content"] = merged_text
        already_merged.add(parent_xp)

    return removed


def build_visual_tree(analysis_folder: str, html_content: str, section_node_xpaths: Optional[Dict[str, float]] = None, debug_crops: bool = False) -> Tuple[dict, str]:
    """Build a pruned tree of visually relevant elements.
    If section_node_xpaths are provided, the single child filtering for section nodes' children is a bit different.

    Args:
        debug_crops: When True, save each image-element crop to
            ``<analysis_folder>/_debug_crops/`` for visual inspection.

    Returns:
        ``(nodes_dict, body_xpath)`` where *nodes_dict* maps xpath -> node info.
    """
    logger.info("Building visual tree ...")

    # Parse HTML with html5lib for browser-consistent structure
    try:
        parser = html5lib.HTMLParser(
            tree=html5lib.treebuilders.getTreeBuilder("lxml"),
            namespaceHTMLElements=False,
        )
        doc = parser.parse(io.BytesIO(html_content.encode("utf-8")))
        if hasattr(doc, "getroot"):
            doc = doc.getroot()
        tree = doc.find(".//body")
    except Exception:
        tree = lxml_html.fromstring(html_content).find(".//body")

    if tree is None:
        raise RuntimeError("Failed to locate <body> element in HTML")

    nodes: dict = {}
    ocr_reader = _easyocr.Reader(['en']) if _EASYOCR_AVAILABLE else None
    if not _EASYOCR_AVAILABLE:
        logger.warning("easyocr not installed — OCR text extraction from images disabled")

    full_page_img = None
    fp_path = os.path.join(analysis_folder, "full_page.png")
    if PILImage is not None and os.path.exists(fp_path):
        full_page_img = PILImage.open(fp_path).convert("RGB")

    debug_crops_dir = None
    if debug_crops:
        debug_crops_dir = os.path.join(analysis_folder, "_debug_crops")
        os.makedirs(debug_crops_dir, exist_ok=True)
        logger.info("Debug crops will be saved to %s", debug_crops_dir)

    display_none_xpaths = _load_display_none_xpaths(analysis_folder)
    def _alarm_handler(signum, frame):
        raise RuntimeError("build_visual_tree timed out after 300s")
    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(300)
    try:
        _traverse_and_build(nodes, analysis_folder, tree, ocr_reader=ocr_reader, full_page_img=full_page_img, debug_crops_dir=debug_crops_dir, display_none_xpaths=display_none_xpaths)
    except RuntimeError:
        logger.error("build_visual_tree timed out after 300s — aborting DOM traversal")
        raise
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
    body_xpath = get_element_xpath(tree)

    logger.info("Built tree with %d nodes", len(nodes))

    # Prune zero-area and single-child-redundant nodes
    if body_xpath not in nodes:
        raise RuntimeError(f"visual tree failed: missing protected body node {body_xpath}")

    n_zero = len(_filter_zero_area(nodes, nodes[body_xpath], protected_xpaths={body_xpath}))
    logger.info("Filtered %d zero-area elements", n_zero)

    # Prune elements that have no text content AND no image
    n_empty = len(_filter_empty_elements(nodes))
    logger.info("Filtered %d empty elements", n_empty)

    # tags such as strong, a, sup, sub, span, b, i, em, u, s, del, ins, small, big, mark, when occurring within a p tag that has other text content should be marked visually redundant
    n_subtext = len(_filter_subtext(nodes))
    logger.info("Filtered %d subtext elements", n_subtext)

    if body_xpath not in nodes:
        raise RuntimeError(f"visual tree failed after pruning: missing protected body node {body_xpath}")

    n_single = len(_filter_single_child(nodes, nodes[body_xpath], section_node_xpaths))
    logger.info("Filtered %d single-child redundant elements", n_single)

    # Merge sibling image leaves that are horizontally adjacent into one node
    n_img_merged = _merge_adjacent_image_nodes(nodes)
    logger.info("Merged %d horizontally-adjacent image nodes", n_img_merged)

    # for each non-image node that has children, get the text content, and replace all the children nodes' text content with `<removed>` tag,
    # if there is some text remaining, then add new leaf text nodes corresponding to each of the remaining text content,
    # separated by the `<removed>` tag
    # n_text_nodes = add_pure_text_nodes(nodes)
    # logger.info("Added %d pure text nodes", n_text_nodes)

    return nodes, body_xpath


# ═══════════════════════════════════════════════════════════════════════════
#  STEP 1c (cont.) – Section node detection
# ═══════════════════════════════════════════════════════════════════════════

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

    Args:
        section_dict: Current ``{xpath: node}`` mapping of section nodes.
        all_nodes: The full visual tree produced by ``build_visual_tree``.
        max_leaves: Maximum allowed leaf count per section node.
        content_width: Dominant content width of the page (pixels).
        width_ratio: Fraction of *content_width* a child must span to be
            considered a valid section-width node (default ``0.95``).

    Returns:
        A new section dict where oversized nodes have been replaced by
        their smaller children.
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


def get_section_nodes(
    nodes: dict,
    page_width: float,
    page_height: float,
    thresh_height: int | None = 2900,
    max_leaves: int | None = None,
) -> Tuple[dict, float]:
    """Identify section-level nodes in the visual tree.

    Args:
        nodes: Full visual tree from ``build_visual_tree``.
        page_width: Width of the rendered page in pixels.
        page_height: Height of the rendered page in pixels.
        thresh_height: Maximum allowed pixel height for a candidate node.
            Nodes taller than this (or >0.9*page_height) are excluded.
        max_leaves: When set, any section node whose subtree (in *nodes*)
            contains more than *max_leaves* leaf elements is recursively
            subdivided into its children until every section fits within
            the threshold.

    Returns:
        ``(section_nodes_dict, content_width)``
    """
    logger.info("Detecting section nodes ...")
    nodes_copy = copy.deepcopy(nodes)

    # step-1: remove elements whose height is > 0.9*page_height
    if thresh_height is not None and thresh_height > 0:
        nodes_copy = {k: v for k, v in nodes_copy.items() if v['bbox']['height'] <= 0.9*page_height and v['bbox']['height'] <= thresh_height}
    else:
        nodes_copy = {k: v for k, v in nodes_copy.items() if v['bbox']['height'] <= 0.9*page_height}

    # Determine the dominant content width
    significant_widths = [
        v["bbox"]["width"]
        for v in nodes_copy.values()
        if v["bbox"] and v["bbox"]["width"] >= 0.3 * page_width
    ]
    if not significant_widths:
        content_width = page_width
    else:
        rounded = [round(w / 10) * 10 for w in significant_widths]
        content_width = Counter(rounded).most_common(1)[0][0]
        if content_width >= 0.95 * page_width:
            content_width = page_width

    # Full-width section candidates
    full_width = [
        n for n in nodes_copy.values()
        if n["bbox"] and n["bbox"]["width"] >= 0.95 * content_width
    ]

    # Group remaining elements horizontally
    remaining = [
        n for n in nodes_copy.values()
        if n["bbox"] and n["bbox"]["width"] < 0.95 * content_width
    ]
    group_sections: List[dict] = []
    for group in _find_horizontal_groups(remaining):
        if len(group) > 1 and _group_spans_width(group, content_width):
            group_sections.extend(copy.deepcopy(group))

    # Remove nodes that are fully contained in larger nodes
    final = _remove_contained_nodes(group_sections + full_width)

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
        gap_nodes: List[dict] = []
        for node in nodes_copy.values():
            if not node["bbox"]:
                continue
            for g_start, g_end in gaps:
                if (
                    node["bbox"]["y"] >= g_start
                    and node["bbox"]["y"] + node["bbox"]["height"] <= g_end
                ):
                    gap_nodes.append(node)
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

    section_dict = {n["xpath"]: n for n in final}

    # Subdivide sections that exceed the leaf-count threshold
    if max_leaves is not None and max_leaves > 0:
        before = len(section_dict)
        section_dict = _subdivide_large_sections(section_dict, nodes, max_leaves, content_width)
        if len(section_dict) != before:
            logger.info(
                "Subdivided oversized sections: %d -> %d (max_leaves=%d)",
                before, len(section_dict), max_leaves,
            )

    logger.info("Found %d section nodes", len(section_dict))
    return section_dict, content_width
