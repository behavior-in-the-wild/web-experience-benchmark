"""
This script creates a visual comparison report between two HTML pages.
It uses the output of dom_utils.py to build the report.
The report is an HTML/JS file that visualizes the following information in three columns:
- Column 1: The original source HTML
- Column 2: The generated HTML
- Column 3: A list of differences between the original and generated HTML:
  - Each difference contains a natural language description: The <original element> has <property> <value> in the original HTML, but <value> in the generated HTML.
  - It also shows the IoU between the bounding boxes of the corresponding elements in the original and generated HTML.
  - When hovering over a difference, the bounding boxes of the corresponding elements in the original and generated HTML are highlighted in columns 1 and 2.
At the top of the report an overall Leaf IoU score is shown, measuring the average IoU between the corresponding leaf elements in the original and generated HTML.
- A section level IoU score measuring the average IoU between the corresponding section bounding boxes is also shown at the top of the report.
- The report is saved in the same directory as the generated HTML file.
The report is named as <original_image_name>_comparison_report_<matching_type>.html.

Steps to create the report:
1. Input is the original source HTML path and the generated HTML path.
2. The script calculates the leaf level matches and section level matches based upon the logic in the /home/colligo/mIoU_comparison/src/matching_module.py file's compute_matches3 function. Part of that flow is already done through the dom_utils.py script, so those outputs can be taken directly.
3. There are 3 ways of matching: heuristic, vlm and embedding, as shown in the compute_matches3 function. The script calculates the matches for each of these ways and generates a separate report for all 3 ways. The IoU scores and bounding boxes logic is there in the /home/colligo/mIoU_comparison/src/evaluate_section_wise_matches.py file.
4. The differences should be arranged in increasing order of IoUs (only leaf differences across the template are shown in column 3). To get the natural language description of the differences, take the text content of the leaf node and add "is bigger/smaller in the generated version" (based upon the bounding box areas).
5. The report should be saved in the same directory as the generated HTML file. The report is named as <original_image_name>_comparison_report_<matching_type>.html.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import html as html_module
import io
import json
import logging
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

# ---------------------------------------------------------------------------
# Path setup — all dependencies live in the same directory
# ---------------------------------------------------------------------------
_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from screenshot_taker import capture_element_screenshots
from browser_config import launch_chromium, new_context, set_content_and_settle, goto_and_settle
from utils import (
    transform_xpath,
    reverse_transform_xpath,
    load_prompt,
    load_json_content,
    compute_style_diff,
)
from client import create_async_ai_client, AsyncAIClient

from dom_utils import (
    _crop_element_from_full_page,
    build_visual_tree,
    get_section_nodes,
)

from get_section_nodes_v2 import _get_section_nodes_v2

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  Utility helpers
# ═══════════════════════════════════════════════════════════════════════════

def _bbox_center(bbox: dict) -> np.ndarray:
    """Return the (x, y) centre of a bounding box as a numpy array."""
    return np.array([bbox["x"] + bbox["width"] / 2, bbox["y"] + bbox["height"] / 2])


def _bbox_area(bbox: Optional[dict]) -> float:
    """Return the area of a bounding box, or 0 if None."""
    if not bbox:
        return 0.0
    return bbox["width"] * bbox["height"]


def _bbox_iou(a: dict, b: dict) -> float:
    """Compute standard 2-D IoU between two bounding boxes."""
    if not a or not b:
        return 0.0
    ix1 = max(a["x"], b["x"])
    iy1 = max(a["y"], b["y"])
    ix2 = min(a["x"] + a["width"], b["x"] + b["width"])
    iy2 = min(a["y"] + a["height"], b["y"] + b["height"])
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    union = _bbox_area(a) + _bbox_area(b) - inter
    return inter / union if union > 0 else 0.0


def _get_union_bbox(bboxes: List[dict]) -> Optional[dict]:
    """Compute the axis-aligned union bounding box."""
    valid = [b for b in bboxes if b]
    if not valid:
        return None
    return {
        "x": min(b["x"] for b in valid),
        "y": min(b["y"] for b in valid),
        "width": max(b["x"] + b["width"] for b in valid) - min(b["x"] for b in valid),
        "height": max(b["y"] + b["height"] for b in valid) - min(b["y"] for b in valid),
    }

def sanitize_section_matches(
    vlm_section_matches: Dict[str, Tuple[List[str], List[str]]],
) -> Dict[int, Tuple[List[str], List[str]]]:
    """Normalize and deduplicate section match pairs.

    The VLM may return section names in inconsistent formats (e.g.
    ``"Section.A"``, ``"Section A"``, ``"SectionA"``).  This function:

    1. Normalizes every section name to the canonical ``"Section-<id>"``
       format.
    2. Merges match pairs that reference the same section on either the
       original or generated side, so each section appears at most once.

    Returns a new dict keyed by consecutive integers.
    """

    def _normalize_section_name(name: str) -> str:
        # Handle separators: Section.A, Section A, Section-A
        normalized = re.sub(
            r"[Ss]ection[\.\s\-]([A-Za-z0-9]+)", r"Section-\1", name,
        )
        # Handle no separator: SectionA, Section1, sectionB
        normalized = re.sub(
            r"[Ss]ection([A-Z0-9][A-Za-z0-9]*)", r"Section-\1", normalized,
        )
        return normalized

    sanitized: Dict[int, Tuple[List[str], List[str]]] = {}
    seen_orig: Set[str] = set()
    seen_gen: Set[str] = set()
    next_idx = 0

    for _raw_idx, raw_pair in vlm_section_matches.items():
        norm_orig = [_normalize_section_name(s) for s in raw_pair[0]]
        norm_gen = [_normalize_section_name(s) for s in raw_pair[1]]
        pair = (norm_orig, norm_gen)

        # Check if any original section was already seen -> merge
        merged = False
        for orig_label in pair[0]:
            if orig_label in seen_orig:
                existing_idx = next(
                    k for k, v in sanitized.items()
                    if orig_label in v[0]
                )
                merged_orig = set(sanitized[existing_idx][0])
                merged_gen = set(sanitized[existing_idx][1])
                merged_orig.update(pair[0])
                merged_gen.update(pair[1])
                sanitized[existing_idx] = (
                    list(merged_orig), list(merged_gen),
                )
                seen_orig.update(pair[0])
                seen_gen.update(pair[1])
                merged = True
                break

        # Check if any generated section was already seen -> merge
        if not merged:
            for gen_label in pair[1]:
                if gen_label in seen_gen:
                    existing_idx = next(
                        k for k, v in sanitized.items()
                        if gen_label in v[1]
                    )
                    merged_orig = set(sanitized[existing_idx][0])
                    merged_gen = set(sanitized[existing_idx][1])
                    merged_orig.update(pair[0])
                    merged_gen.update(pair[1])
                    sanitized[existing_idx] = (
                        list(merged_orig), list(merged_gen),
                    )
                    seen_orig.update(pair[0])
                    seen_gen.update(pair[1])
                    merged = True
                    break

        # New pair -- no overlap with anything seen so far
        if not merged:
            sanitized[next_idx] = pair
            next_idx += 1
            seen_orig.update(pair[0])
            seen_gen.update(pair[1])

    return sanitized


def normalize_leaf_name(name: str) -> str:
    """
    Normalize leaf names to standard format 'Leaf-X'.
    
    Converts various formats like 'Leaf.A', 'Leaf A', 'LeafA', 'leaf-A', 'LEAF-1' etc. to 'Leaf-X'.
    
    Examples:
        'Leaf.A' -> 'Leaf-A'
        'Leaf A' -> 'Leaf-A'
        'LeafA' -> 'Leaf-A'
        'leaf-A' -> 'Leaf-A'
        'Leaf.1' -> 'Leaf-1'
        'Leaf1' -> 'Leaf-1'
        'LEAF-B' -> 'Leaf-B'
    """
    # Replace common separators (dot, space, hyphen, or no separator) with hyphen
    # First handle cases with separators: Leaf.A, Leaf A, Leaf-A
    normalized = re.sub(r'[Ll][Ee][Aa][Ff][\.\s\-]([A-Za-z0-9]+)', r'Leaf-\1', name)
    # Then handle cases without separator: LeafA, Leaf1, leafB
    # Only match if followed by uppercase letter or digit (to avoid matching 'Leafs' etc.)
    normalized = re.sub(r'[Ll][Ee][Aa][Ff]([A-Z0-9][A-Za-z0-9]*)', r'Leaf-\1', normalized)
    return normalized

def merge_overlapping_pairs(pairs: List[Tuple[Set[str], Set[str]]]) -> List[Tuple[Set[str], Set[str]]]:
    """
    Merge pairs that share any leaf elements (either original or generated).
    
    Single-pass algorithm: maintains tracking sets for seen original and generated
    elements. When a new pair overlaps with existing elements, it's merged with
    the corresponding existing pair.
    
    Example:
        Input pairs:
            [({"leaf-1"}, {"leaf-A"}),           # Pair 0: new, add to result
             ({"leaf-1", "leaf-2"}, {"leaf-B"}), # Pair 1: "leaf-1" seen -> merge with Pair 0
             ({"leaf-3"}, {"leaf-B"}),           # Pair 2: "leaf-B" seen -> merge with merged Pair 0+1
             ({"leaf-4"}, {"leaf-D"})]           # Pair 3: independent, add as new
        
        Step-by-step:
            After Pair 0: result = {0: ({"leaf-1"}, {"leaf-A"})}
                          seen_orig = {"leaf-1"}, seen_gen = {"leaf-A"}
            
            After Pair 1: "leaf-1" in seen_orig -> find existing pair 0, merge
                          result = {0: ({"leaf-1", "leaf-2"}, {"leaf-A", "leaf-B"})}
                          seen_orig = {"leaf-1", "leaf-2"}, seen_gen = {"leaf-A", "leaf-B"}
            
            After Pair 2: "leaf-B" in seen_gen -> find existing pair 0, merge
                          result = {0: ({"leaf-1", "leaf-2", "leaf-3"}, {"leaf-A", "leaf-B"})}
                          seen_orig = {"leaf-1", "leaf-2", "leaf-3"}
            
            After Pair 3: no overlap -> add as new pair 1
                          result = {0: (...), 1: ({"leaf-4"}, {"leaf-D"})}
        
        Output:
            [({"leaf-1", "leaf-2", "leaf-3"}, {"leaf-A", "leaf-B"}),
             ({"leaf-4"}, {"leaf-D"})]
    
    Args:
        pairs: List of (orig_set, gen_set) tuples
    
    Returns:
        List of merged (orig_set, gen_set) tuples
    """
    if not pairs:
        return []
    
    # Result dict: {index: [orig_set, gen_set]}
    sanitized_pairs = {}
    
    # Track which elements have been seen (for quick lookup)
    seen_orig_elements = set()
    seen_gen_elements = set()
    
    sanitized_match_idx = 0
    
    for orig_set, gen_set in pairs:
        is_merged = False
        
        # Check if any original element is already in an existing pair
        for orig_elem in orig_set:
            if orig_elem in seen_orig_elements:
                # Find the existing pair that contains this element
                existing_idx = None
                for idx, (existing_orig, existing_gen) in sanitized_pairs.items():
                    if orig_elem in existing_orig:
                        existing_idx = idx
                        break
                
                if existing_idx is not None:
                    # Merge current pair into existing pair
                    # Example: existing = ({"leaf-1"}, {"leaf-A"})
                    #          current  = ({"leaf-1", "leaf-2"}, {"leaf-B"})
                    #          merged   = ({"leaf-1", "leaf-2"}, {"leaf-A", "leaf-B"})
                    sanitized_pairs[existing_idx][0].update(orig_set)
                    sanitized_pairs[existing_idx][1].update(gen_set)
                    
                    # Update tracking sets
                    seen_orig_elements.update(orig_set)
                    seen_gen_elements.update(gen_set)
                    is_merged = True
                    break
        
        # If not merged via original elements, check generated elements
        if not is_merged:
            for gen_elem in gen_set:
                if gen_elem in seen_gen_elements:
                    # Find the existing pair that contains this element
                    existing_idx = None
                    for idx, (existing_orig, existing_gen) in sanitized_pairs.items():
                        if gen_elem in existing_gen:
                            existing_idx = idx
                            break
                    
                    if existing_idx is not None:
                        # Merge current pair into existing pair
                        sanitized_pairs[existing_idx][0].update(orig_set)
                        sanitized_pairs[existing_idx][1].update(gen_set)
                        
                        # Update tracking sets
                        seen_orig_elements.update(orig_set)
                        seen_gen_elements.update(gen_set)
                        is_merged = True
                        break
        
        # If no overlap found, add as new pair
        if not is_merged:
            sanitized_pairs[sanitized_match_idx] = [set(orig_set), set(gen_set)]
            sanitized_match_idx += 1
            seen_orig_elements.update(orig_set)
            seen_gen_elements.update(gen_set)
    
    # Convert to list of tuples
    return [(set(orig), set(gen)) for orig, gen in sanitized_pairs.values()]

def convert_vlm_leaf_matches_to_xpath_pairs(leaf_matches_vlm: dict, 
                                            leaf_label_mapping: dict) -> list:
    """
    Convert VLM leaf matches to xpath-based pairs with sanitization.
    
    Since VLM returns labels (like 'Leaf-1', 'leaf-A'), we need to convert them to xpaths
    using the leaf_label_mapping provided in section_wise_matches_vlm.
    
    Sanitization: Merges matches that share any leaf elements (either original or generated)
    into a single match. Also normalizes leaf names before xpath conversion.
    
    Example input:
        leaf_matches_vlm = {
          "1": [["Leaf-1"], ["leaf-A"]],
          "2": [["Leaf-1", "Leaf-2"], ["leaf-B"]],
          "3": [["Leaf-4"], ["leaf-D"]],
          "leaf-E": null
        }
        leaf_label_mapping = {
            "original_leaves": {"Leaf-1": "html__body__div_1", "Leaf-2": "html__body__div_2", "Leaf-4": "html__body__div_4"},
            "generated_leaves": {"Leaf-A": "html__body__span_A", "Leaf-B": "html__body__span_B", "Leaf-D": "html__body__span_D"}
        }
    
    Intermediate steps:
        1. Normalize labels: "leaf-A" -> "Leaf-A"
        2. Convert to xpaths: "Leaf-A" -> "html__body__span_A"
        3. Merge overlapping pairs
        4. Convert transformed xpaths to original xpaths
    
    Final output (xpath pairs):
        [({"/html/body/div[1]", "/html/body/div[2]"}, {"/html/body/span[A]", "/html/body/span[B]"}),
         ({"/html/body/div[4]"}, {"/html/body/span[D]"})]
    
    Args:
        leaf_matches_vlm: Dict of {index: [[orig_labels], [gen_labels]] or null}
        leaf_label_mapping: Dict with "original_leaves" and "generated_leaves" label-to-xpath mappings
    
    Returns:
        List of sanitized/merged (orig_xpath_set, gen_xpath_set) tuples
    """
    # Get label-to-xpath mappings
    orig_label_to_xpath = leaf_label_mapping.get("original_leaves", {})
    gen_label_to_xpath = leaf_label_mapping.get("generated_leaves", {})
    
    # Step 1: Extract raw pairs from VLM output, converting labels to xpaths
    raw_pairs = []
    for key, value in leaf_matches_vlm.items():
        if value is None or not isinstance(value, list) or len(value) != 2:
            continue
        
        orig_labels, gen_labels = value
        if isinstance(orig_labels, list) and isinstance(gen_labels, list):
            # Normalize and convert labels to xpaths
            orig_xpaths = set()
            for label in orig_labels:
                if isinstance(label, str):
                    normalized = normalize_leaf_name(label)
                    # Try to find xpath for this label (handles case variations)
                    xpath = orig_label_to_xpath.get(normalized)
                    if xpath:
                        orig_xpaths.add(xpath)
            
            gen_xpaths = set()
            for label in gen_labels:
                if isinstance(label, str):
                    normalized = normalize_leaf_name(label)
                    # Try to find xpath for this label
                    xpath = gen_label_to_xpath.get(normalized)
                    if xpath:
                        gen_xpaths.add(xpath)
            
            if orig_xpaths and gen_xpaths:
                raw_pairs.append((orig_xpaths, gen_xpaths))
    
    # Step 2: Merge overlapping pairs (by xpath)
    merged_pairs = merge_overlapping_pairs(raw_pairs)

    # Step 3: Convert transformed xpaths to original xpaths
    converted_pairs = []
    for orig, gen in merged_pairs:
        converted_orig = [reverse_transform_xpath(xp) for xp in orig]
        converted_gen = [reverse_transform_xpath(xp) for xp in gen]
        converted_pairs.append((converted_orig, converted_gen))
    
    # Convert frozensets back to regular sets for consistency
    return [(set(orig), set(gen)) for orig, gen in converted_pairs]

# ═══════════════════════════════════════════════════════════════════════════
#  LLM-based diff generation (adapted from critique.py)
# ═══════════════════════════════════════════════════════════════════════════

_report_ai_client: Optional[AsyncAIClient] = None

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None  # type: ignore[assignment, misc]


def _crop_from_full_page(full_page_path: Path, bbox: dict) -> Optional[bytes]:
    """Crop an element region from full_page.png using bbox via _crop_element_from_full_page.

    Returns PNG bytes of the cropped region, or None on failure.
    """
    if PILImage is None or not full_page_path.exists() or not bbox:
        return None
    try:
        img = PILImage.open(full_page_path).convert("RGB")
        cropped = _crop_element_from_full_page(img, bbox)
        if cropped is None:
            return None
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        logger.warning("Failed to crop from %s: %s", full_page_path, e)
        return None


async def _generate_diff_for_node_report(
    node_id: str,
    original_html_str: str,
    translated_html_str: str,
    style_diff: dict,
    children_aggregated_diffs: Optional[List] = None,
    original_img_data_url: Optional[str] = None,
    translated_img_data_url: Optional[str] = None,
) -> Tuple[Optional[List], Optional[str]]:
    """Generate a visual diff for a single DOM node via LLM.

    *original_img_data_url* and *translated_img_data_url* are base64 data URLs
    (e.g. from cropping full_page.png by bbox).

    Returns ``(content_payload_mini, llm_response_text)`` on success,
    or ``(None, error_json_string)`` on failure.
    """
    global _report_ai_client

    if children_aggregated_diffs:
        prompt_template = load_prompt("node_diff_with_children").replace("{node_id}", node_id)
    else:
        prompt_template = load_prompt("node_diff_leaf").replace("{node_id}", node_id)

    if not (original_img_data_url and original_img_data_url.startswith("data:")):
        logger.error("Original image data URL missing for node %s", node_id)
        return None, json.dumps({"error": f"Original screenshot missing for node {node_id}"})
    if not (translated_img_data_url and translated_img_data_url.startswith("data:")):
        logger.error("Translated image data URL missing for node %s", node_id)
        return None, json.dumps({"error": f"Translated screenshot missing for node {node_id}"})

    img_url_1 = original_img_data_url
    img_url_2 = translated_img_data_url

    content_payload = [
        {"type": "text", "text": prompt_template},
        {"type": "image_url", "image_url": {"url": img_url_1}},
        {"type": "image_url", "image_url": {"url": img_url_2}},
        {"type": "text", "text": "Original HTML Snippet:\n" + original_html_str},
        {"type": "text", "text": "Translated HTML Snippet:\n" + translated_html_str},
        {"type": "text", "text": "Computed Style Differences:\n" + json.dumps(style_diff, indent=2)},
    ]

    if children_aggregated_diffs:
        children_fixed = copy.deepcopy(children_aggregated_diffs)
        for idx in range(len(children_fixed)):
            if children_fixed[idx] != "" and isinstance(children_fixed[idx], dict) and "content_payload" in children_fixed[idx]:
                children_fixed[idx].pop("content_payload")
        content_payload.append(
            {"type": "text", "text": "Children Differences:\n" + json.dumps(children_fixed, indent=2)}
        )

    messages = [{"role": "user", "content": content_payload}]

    try:
        response = await _report_ai_client.get_model_response(messages, temperature=0)
        content_payload_mini = [
            {"type": "text", "text": prompt_template},
            {"type": "image_url", "image_url": {"url": original_img_data_url}},
            {"type": "image_url", "image_url": {"url": translated_img_data_url}},
            {"type": "text", "text": "Original HTML Snippet:\n" + original_html_str},
            {"type": "text", "text": "Translated HTML Snippet:\n" + translated_html_str},
            {"type": "text", "text": "Computed Style Differences:\n" + json.dumps(style_diff, indent=2)},
        ]
        if children_aggregated_diffs:
            content_payload_mini.append(
                {"type": "text", "text": "Children Differences:\n" + json.dumps(children_fixed, indent=2)}
            )
        return content_payload_mini, response
    except Exception as e:
        logger.error("Error calling AI client for node %s: %s", node_id, e)
        return None, json.dumps({"error": f"AI client call failed for node {node_id}: {e}"})


async def _verify_root_node_diffs_report_async(
    root_group_id: str,
    diff_items_to_verify: List[dict],
    orig_img_data_url: str,
    gen_img_data_url: str,
) -> Any:
    """Verify the detected differences for the root group using LLM.

    Adapted from critique.py verify_root_node_diffs_async.
    Accepts data URLs for images (from cropped full_page) instead of file paths.
    """
    global _report_ai_client

    verification_prompt = load_prompt("root_verification")
    if not (orig_img_data_url and orig_img_data_url.startswith("data:")):
        logger.error("Original image missing for root verification: %s", root_group_id)
        return {"error": f"Original screenshot missing for root {root_group_id} verification"}
    if not (gen_img_data_url and gen_img_data_url.startswith("data:")):
        logger.error("Translated image missing for root verification: %s", root_group_id)
        return {"error": f"Translated screenshot missing for root {root_group_id} verification"}

    diff_items_clean = []
    for d in diff_items_to_verify:
        dc = copy.deepcopy(d)
        dc.pop("content_payload", None)
        diff_items_clean.append(dc)

    content_payload = [
        {"type": "text", "text": verification_prompt},
        {"type": "image_url", "image_url": {"url": orig_img_data_url}},
        {"type": "image_url", "image_url": {"url": gen_img_data_url}},
        {"type": "text", "text": "Potential Differences (to be verified):\n" + json.dumps(diff_items_clean, indent=2)},
    ]
    messages = [{"role": "user", "content": content_payload}]

    MAX_RETRIES = 3
    llm_response = ""
    for attempt in range(MAX_RETRIES):
        try:
            llm_response = await _report_ai_client.get_model_response(messages, temperature=0)
            match = re.search(r"```json\n(.*?)\n```", llm_response, re.DOTALL)
            if match:
                res_match = json.loads(match.group(1))
                if not isinstance(res_match, list):
                    raise json.JSONDecodeError(
                        "LLM response for verification was not a JSON list.",
                        match.group(1), 0,
                    )
                for diff_item in res_match:
                    if isinstance(diff_item, dict):
                        if "dissimilarity_score" in diff_item and isinstance(diff_item["dissimilarity_score"], (int, float)):
                            diff_item["dissimilarity_score"] = round(diff_item["dissimilarity_score"] / 10, 2)
                        if "noticeability_score" in diff_item and isinstance(diff_item["noticeability_score"], (int, float)):
                            diff_item["noticeability_score"] = round(diff_item["noticeability_score"] / 10, 2)
                return res_match
        except json.JSONDecodeError as e:
            logger.warning(
                "Error parsing LLM JSON for root verification (%s), attempt %d: %s. Raw: %s",
                root_group_id, attempt + 1, e, llm_response[:300] if llm_response else "",
            )
            if attempt == MAX_RETRIES - 1:
                return {"error": "Failed to parse LLM JSON for verification after retries", "raw_response": llm_response}
            await asyncio.sleep(1 + attempt)
        except Exception as e:
            logger.error("Unexpected error during root verification LLM call (%s), attempt %d: %s", root_group_id, attempt + 1, e)
            if attempt == MAX_RETRIES - 1:
                return {"error": f"Unexpected error during verification LLM call: {e}"}
            await asyncio.sleep(1 + attempt)

    return {"error": f"Root verification failed after {MAX_RETRIES} retries for {root_group_id}"}


def _get_match_groups(
    leaf_diffs: List[dict],
    section_matches: List[Tuple[List[str], List[str]]],
) -> List[Tuple[List[str], List[str]]]:
    """Extract match groups from leaf_diffs and section_matches.

    Each group is (orig_xpaths, gen_xpaths). Counts may differ (many-to-many).
    """
    groups: List[Tuple[List[str], List[str]]] = []
    seen_orig_xpaths: Set[str] = set()
    seen_gen_xpaths: Set[str] = set()

    for diff in leaf_diffs:
        orig_raw = diff.get("orig_xpath", "")
        gen_raw = diff.get("gen_xpath", "")
        if not orig_raw or not gen_raw:
            continue
        orig_xps = [x.strip() for x in orig_raw.split("|") if x.strip()]
        gen_xps = [x.strip() for x in gen_raw.split("|") if x.strip()]
        seen_orig_xpaths.update(orig_xps)
        seen_gen_xpaths.update(gen_xps)
        if orig_xps and gen_xps:
            groups.append((orig_xps, gen_xps))

    for orig_list, gen_list in section_matches:
        if orig_list and gen_list:
            seen_orig_xpaths.update(orig_list)
            seen_gen_xpaths.update(gen_list)
            groups.append((list(orig_list), list(gen_list)))
    
    # Add the '/html/body' match group by default if it isn't matched already
    if '/html/body' not in seen_orig_xpaths and '/html/body' not in seen_gen_xpaths:
        groups.append((['/html/body'], ['/html/body']))

    return groups


async def _process_match_group(
    orig_xpaths: List[str],
    gen_xpaths: List[str],
    orig_nodes: dict,
    orig_analysis_dir: Path,
    gen_analysis_dir: Path,
    children_aggregated_diffs: Optional[List[Any]] = None,
) -> Tuple[str, Any]:
    """Process a match group: combine HTML, bbox, and styles for all component nodes,
    then call _generate_diff_for_node_report with the combined inputs.

    *children_aggregated_diffs*: diffs from child groups (nodes whose orig nodes
    are descendants of nodes in orig_xpaths), already processed at deeper levels.

    Returns (group_id, parsed_diff_or_empty).
    """
    orig_full_page = orig_analysis_dir / "full_page.png"
    gen_full_page = gen_analysis_dir / "full_page.png"
    if not orig_full_page.exists() or not gen_full_page.exists():
        logger.warning("Missing full_page.png in analysis dirs")
        return ("|".join(transform_xpath(x) for x in orig_xpaths), "")

    orig_html_parts: List[str] = []
    orig_bboxes: List[dict] = []
    orig_styles_merged: dict = {}

    for xp in orig_xpaths:
        t_xp = transform_xpath(xp)
        html_file = orig_analysis_dir / f"{t_xp}.html"
        bbox_file = orig_analysis_dir / f"{t_xp}.bbox.json"
        json_file = orig_analysis_dir / f"{t_xp}.json"
        if html_file.exists():
            orig_html_parts.append(html_file.read_text(encoding="utf-8"))
        bbox_data = load_json_content(bbox_file)
        bbox = bbox_data.get("bbox", {}) if isinstance(bbox_data, dict) else {}
        if bbox:
            orig_bboxes.append(bbox)
        styles = load_json_content(json_file) if json_file.exists() else {}
        if isinstance(styles, dict):
            orig_styles_merged.update(styles)

    gen_html_parts: List[str] = []
    gen_bboxes: List[dict] = []
    gen_styles_merged: dict = {}

    for xp in gen_xpaths:
        t_xp = transform_xpath(xp)
        html_file = gen_analysis_dir / f"{t_xp}.html"
        bbox_file = gen_analysis_dir / f"{t_xp}.bbox.json"
        json_file = gen_analysis_dir / f"{t_xp}.json"
        if html_file.exists():
            gen_html_parts.append(html_file.read_text(encoding="utf-8"))
        bbox_data = load_json_content(bbox_file)
        bbox = bbox_data.get("bbox", {}) if isinstance(bbox_data, dict) else {}
        if bbox:
            gen_bboxes.append(bbox)
        styles = load_json_content(json_file) if json_file.exists() else {}
        if isinstance(styles, dict):
            gen_styles_merged.update(styles)

    if not orig_html_parts or not gen_html_parts:
        logger.warning("No HTML content for match group orig=%s gen=%s", orig_xpaths, gen_xpaths)
        return ("|".join(transform_xpath(x) for x in orig_xpaths), "")

    orig_html_combined = "\n\n".join(orig_html_parts)
    gen_html_combined = "\n\n".join(gen_html_parts)

    orig_union_bbox = _get_union_bbox(orig_bboxes) if orig_bboxes else {}
    gen_union_bbox = _get_union_bbox(gen_bboxes) if gen_bboxes else {}

    orig_crop_bytes = _crop_from_full_page(orig_full_page, orig_union_bbox)
    gen_crop_bytes = _crop_from_full_page(gen_full_page, gen_union_bbox)
    if not orig_crop_bytes or not gen_crop_bytes:
        logger.warning("Failed to crop screenshots for match group")
        return ("|".join(transform_xpath(x) for x in orig_xpaths), "")

    orig_img_data_url = f"data:image/png;base64,{base64.b64encode(orig_crop_bytes).decode('utf-8')}"
    gen_img_data_url = f"data:image/png;base64,{base64.b64encode(gen_crop_bytes).decode('utf-8')}"

    style_diff = compute_style_diff(orig_styles_merged, gen_styles_merged)
    group_id = "|".join(transform_xpath(x) for x in orig_xpaths)
    children_diffs = children_aggregated_diffs if children_aggregated_diffs else None

    MAX_RETRIES = 3
    for attempt in range(MAX_RETRIES):
        payload_mini, llm_response = await _generate_diff_for_node_report(
            group_id, orig_html_combined, gen_html_combined,
            style_diff,
            children_diffs,
            orig_img_data_url, gen_img_data_url,
        )

        if payload_mini is None:
            if attempt == MAX_RETRIES - 1:
                return (group_id, "")
            await asyncio.sleep(1 + attempt)
            continue

        try:
            match = re.search(r"```json\n(.*?)\n```", llm_response, re.DOTALL)
            if match:
                parsed = json.loads(match.group(1))
                parsed["content_payload"] = payload_mini
                logger.info("Successfully processed diff for match group %s", group_id)
                return (group_id, parsed)
            else:
                logger.warning("No JSON block in LLM response for group %s (attempt %d)", group_id, attempt + 1)
                if attempt == MAX_RETRIES - 1:
                    return (group_id, "")
                await asyncio.sleep(1 + attempt)
        except json.JSONDecodeError as e:
            logger.error("JSON parse error for group %s (attempt %d): %s", group_id, attempt + 1, e)
            if attempt == MAX_RETRIES - 1:
                return (group_id, "")
            await asyncio.sleep(2 + attempt * 2)

    return (group_id, "")


async def process_dom_tree_for_report(
    orig_nodes: dict,
    gen_nodes: dict,
    leaf_diffs: List[dict],
    section_matches: List[Tuple[List[str], List[str]]],
    orig_analysis_dir: str,
    gen_analysis_dir: str,
    ai_provider: str = "gpt41",
) -> str:
    """Process match groups from leaf_diffs and section_matches, generating LLM-based diffs.

    For each match group (possibly many-to-many orig/gen nodes), HTML, bboxes, and
    styles are combined appropriately and _generate_diff_for_node_report is called.

    Returns a pretty-printed JSON string of the verified diffs suitable for
    display in the report's 4th tab.
    """
    global _report_ai_client
    _report_ai_client = create_async_ai_client(provider=ai_provider)

    orig_dir = Path(orig_analysis_dir)
    gen_dir = Path(gen_analysis_dir)

    match_groups = _get_match_groups(leaf_diffs, section_matches)
    if not match_groups:
        logger.warning("No match groups found for LLM diff generation.")
        return json.dumps({"info": "No match groups for LLM diff generation."}, indent=2)

    logger.info("LLM diff: %d match groups to process", len(match_groups))

    groups_by_depth: Dict[int, List[Tuple[List[str], List[str]]]] = defaultdict(list)
    group_id_to_orig_list: Dict[str, List[str]] = {}
    for orig_list, gen_list in match_groups:
        depth = max(orig_nodes.get(xp, {}).get("depth", 0) for xp in orig_list)
        groups_by_depth[depth].append((orig_list, gen_list))
        gid = "|".join(transform_xpath(x) for x in orig_list)
        group_id_to_orig_list[gid] = orig_list

    # Build set of matched xpaths and group unmatched orig_nodes by depth
    matched_xpaths: Set[str] = set()
    for orig_list, _ in match_groups:
        matched_xpaths.update(orig_list)

    unmatched_by_depth: Dict[int, List[str]] = defaultdict(list)
    for xp in orig_nodes:
        if xp not in matched_xpaths:
            d = orig_nodes[xp].get("depth", 0)
            unmatched_by_depth[d].append(xp)

    # Reverse mapping: xpath -> gid (updated as unmatched nodes are processed)
    xpath_to_gid: Dict[str, str] = {}
    for gid, orig_list in group_id_to_orig_list.items():
        for xp in orig_list:
            xpath_to_gid[xp] = gid

    all_depth_levels = set(groups_by_depth.keys()) | set(unmatched_by_depth.keys())
    depth_levels = sorted(all_depth_levels, reverse=True)

    all_group_diffs: Dict[str, Any] = {}

    start_time = time.time()
    for d_level in depth_levels:
        groups_at_level = groups_by_depth.get(d_level, [])
        if groups_at_level:
            logger.info("LLM diff: processing depth %d (%d groups)", d_level, len(groups_at_level))

        tasks = []
        for orig_list, gen_list in groups_at_level:
            gid = "|".join(transform_xpath(x) for x in orig_list)
            orig_list_set = set(orig_list)
            children_diffs = []
            for other_gid, other_diff in all_group_diffs.items():
                if not other_diff or other_diff == "":
                    continue
                if isinstance(other_diff, dict) and "error" in other_diff:
                    continue
                other_orig_list = group_id_to_orig_list.get(other_gid, [])
                if not other_orig_list:
                    continue
                other_depth = max(
                    orig_nodes.get(xp, {}).get("depth", 0)
                    for xp in other_orig_list
                )
                if other_depth <= d_level:
                    continue
                if all(
                    orig_nodes.get(xp, {}).get("parent") in orig_list_set
                    for xp in other_orig_list
                ):
                    children_diffs.append(other_diff)

            tasks.append(
                _process_match_group(
                    orig_list, gen_list, orig_nodes, orig_dir, gen_dir,
                    children_aggregated_diffs=children_diffs if children_diffs else None,
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)

        for (orig_list, gen_list), result in zip(groups_at_level, results):
            gid = "|".join(transform_xpath(x) for x in orig_list)
            if isinstance(result, Exception):
                logger.error("Task failed for group %s: %s", gid, result)
                all_group_diffs[gid] = ""
            else:
                group_id, diff_val = result
                all_group_diffs[group_id] = diff_val

        # Process unmatched orig_nodes at this depth: aggregate immediate children's diffs
        for xp in unmatched_by_depth.get(d_level, []):
            children_xps = orig_nodes[xp].get("children", [])
            node_diffs = []
            for child_xp in children_xps:
                child_gid = xpath_to_gid.get(child_xp, transform_xpath(child_xp))
                child_diff = all_group_diffs.get(child_gid)
                if child_diff and child_diff != "" and not (isinstance(child_diff, dict) and "error" in child_diff):
                    node_diffs.append(child_diff)
            node_gid = transform_xpath(xp)
            if node_diffs:
                merged_diff: Dict[str, Any] = {}
                diff_count = 0
                for child_diff in node_diffs:
                    if isinstance(child_diff, dict):
                        for key, val in sorted(child_diff.items()):
                            if key.startswith("difference_") and isinstance(val, dict):
                                diff_count += 1
                                merged_diff[f"difference_{diff_count}"] = val
                            elif key == "webpage_section" and "webpage_section" not in merged_diff:
                                merged_diff["webpage_section"] = val
                all_group_diffs[node_gid] = merged_diff if merged_diff else ""
            else:
                all_group_diffs[node_gid] = ""
            group_id_to_orig_list[node_gid] = [xp]
            xpath_to_gid[xp] = node_gid

    elapsed = time.time() - start_time
    logger.info("LLM diff: all groups processed in %.2f seconds", elapsed)

    # Root verification: ideally, there should be a single root group '/html/body'
    # combine all groups at shallowest depth, merge their diffs, verify and build final_verified_root_diff_output
    final_verified_root_diff_output = None
    shallowest_depth = min(groups_by_depth.keys()) if groups_by_depth else None
    if shallowest_depth is not None:
        root_groups = groups_by_depth[shallowest_depth]
        if root_groups:
            # Combine all groups at min depth: union of orig/gen xpaths and merge diffs from all_group_diffs
            combined_orig_list: List[str] = []
            combined_gen_list: List[str] = []
            seen_orig: Set[str] = set()
            seen_gen: Set[str] = set()
            combined_diffs: Dict[str, Any] = {}
            first_webpage_section = None

            for orig_list, gen_list in root_groups:
                for xp in orig_list:
                    if xp not in seen_orig:
                        seen_orig.add(xp)
                        combined_orig_list.append(xp)
                for xp in gen_list:
                    if xp not in seen_gen:
                        seen_gen.add(xp)
                        combined_gen_list.append(xp)
                gid = "|".join(transform_xpath(x) for x in orig_list)
                group_diff = all_group_diffs.get(gid)
                if group_diff and isinstance(group_diff, dict) and "error" not in group_diff:
                    if first_webpage_section is None and "webpage_section" in group_diff:
                        first_webpage_section = group_diff["webpage_section"]
                    for key in sorted(group_diff.keys()):
                        if key.startswith("difference_") and isinstance(group_diff[key], dict):
                            combined_diffs[f"difference_{len(combined_diffs) + 1}"] = group_diff[key]

            root_gid = "|".join(transform_xpath(x) for x in combined_orig_list)  # combined id for logging
            unverified_root_diff_data = combined_diffs.copy()
            if first_webpage_section is not None:
                unverified_root_diff_data["webpage_section"] = first_webpage_section

            diff_items_to_verify = [v for k, v in sorted(combined_diffs.items()) if k.startswith("difference_") and isinstance(v, dict)]

            if diff_items_to_verify:
                logger.info(
                    "Verifying %d differences for combined root (%d groups at depth %d) ...",
                    len(diff_items_to_verify), len(root_groups), shallowest_depth,
                )
                orig_bboxes = []
                for xp in combined_orig_list:
                    t_xp = transform_xpath(xp)
                    bbox_file = orig_dir / f"{t_xp}.bbox.json"
                    bbox_data = load_json_content(bbox_file)
                    bbox = bbox_data.get("bbox", {}) if isinstance(bbox_data, dict) else {}
                    if bbox:
                        orig_bboxes.append(bbox)
                gen_bboxes = []
                for xp in combined_gen_list:
                    t_xp = transform_xpath(xp)
                    bbox_file = gen_dir / f"{t_xp}.bbox.json"
                    bbox_data = load_json_content(bbox_file)
                    bbox = bbox_data.get("bbox", {}) if isinstance(bbox_data, dict) else {}
                    if bbox:
                        gen_bboxes.append(bbox)
                orig_union_bbox = _get_union_bbox(orig_bboxes) if orig_bboxes else {}
                gen_union_bbox = _get_union_bbox(gen_bboxes) if gen_bboxes else {}
                orig_full_page = orig_dir / "full_page.png"
                gen_full_page = gen_dir / "full_page.png"
                orig_crop_bytes = _crop_from_full_page(orig_full_page, orig_union_bbox)
                gen_crop_bytes = _crop_from_full_page(gen_full_page, gen_union_bbox)
                if orig_crop_bytes and gen_crop_bytes:
                    orig_img_data_url = f"data:image/png;base64,{base64.b64encode(orig_crop_bytes).decode('utf-8')}"
                    gen_img_data_url = f"data:image/png;base64,{base64.b64encode(gen_crop_bytes).decode('utf-8')}"
                    verified_root_diffs_list = await _verify_root_node_diffs_report_async(
                        root_gid, diff_items_to_verify, orig_img_data_url, gen_img_data_url,
                    )
                    if isinstance(verified_root_diffs_list, dict) and "error" in verified_root_diffs_list:
                        logger.warning(
                            "Verification of root group diffs resulted in an error: %s",
                            verified_root_diffs_list["error"],
                        )
                        final_verified_root_diff_output = {
                            "verification_error": verified_root_diffs_list["error"],
                            "unverified_diff": unverified_root_diff_data,
                        }
                    else:
                        final_verified_root_diff_output = {
                            "webpage_section": first_webpage_section or {"section_id": root_gid, "section_description": "N/A"},
                        }
                        for i, diff in enumerate(verified_root_diffs_list, 1):
                            final_verified_root_diff_output[f"difference_{i}"] = diff
                        # Compute average dissimilarity across all valid diffs
                        dissim_scores = []
                        for differ in verified_root_diffs_list:
                            if isinstance(differ, dict):
                                try:
                                    dissim_scores.append(float(differ["dissimilarity_score"]))
                                except (KeyError, TypeError, ValueError):
                                    pass
                        if dissim_scores:
                            final_verified_root_diff_output["average_dissimilarity_score"] = sum(dissim_scores) / len(dissim_scores)
                else:
                    logger.error("Cannot verify root diffs: failed to crop root screenshots")
                    final_verified_root_diff_output = {
                        "error": "Failed to crop root screenshots for verification",
                        "unverified_diff": unverified_root_diff_data,
                    }
            elif unverified_root_diff_data:
                logger.info("No 'difference_' items found in combined root diff to verify.")
                final_verified_root_diff_output = unverified_root_diff_data
            else:
                final_verified_root_diff_output = {"error": f"No valid diff data from {len(root_groups)} root groups at depth {shallowest_depth}"}
        else:
            final_verified_root_diff_output = {"error": "Could not identify root group"}
    else:
        final_verified_root_diff_output = {"error": "Could not identify root group"}

    if final_verified_root_diff_output is not None:
        return json.dumps(final_verified_root_diff_output, indent=2)

    # Fallback: collect meaningful diffs (non-empty) into a structured output
    structured: Dict[str, Any] = {}
    diff_idx = 1
    for gid in sorted(all_group_diffs.keys()):
        diff_val = all_group_diffs[gid]
        if diff_val and diff_val != "":
            clean_val = copy.deepcopy(diff_val) if isinstance(diff_val, dict) else diff_val
            if isinstance(clean_val, dict):
                clean_val.pop("content_payload", None)
            structured[f"difference_{diff_idx}"] = {
                "node_id": gid,
                "diff": clean_val,
            }
            diff_idx += 1

    return json.dumps(structured, indent=2)


# ═══════════════════════════════════════════════════════════════════════════
#  Step 1 - Prepare analysis data (element screenshots & bboxes)
# ═══════════════════════════════════════════════════════════════════════════

def prepare_html_analysis(
    html_path: str,
    analysis_dir: str,
    label: str = "original",
) -> str:
    """Capture element screenshots & bboxes for the given HTML file.

    Args:
        html_path: Path to the saved HTML file (used for both XPath collection
                   and Playwright rendering). When the file was saved by
                   ``fetch_url_as_html`` it already contains a ``<base>`` tag
                   so external CSS, fonts, and images resolve correctly.
        analysis_dir: Directory to write analysis artifacts (screenshots, bboxes).
        label: Label for log messages (e.g. "original", "generated").

    Returns the path to the analysis directory.
    """
    html_path = Path(html_path)
    if not html_path.exists():
        raise FileNotFoundError(f"{label.capitalize()} HTML not found: {html_path}")

    analysis_dir = Path(analysis_dir)
    analysis_dir.mkdir(parents=True, exist_ok=True)

    with open(html_path, "r", encoding="utf-8") as fh:
        html_content = fh.read()
    (analysis_dir / f"{label}.html").write_text(html_content, encoding="utf-8")

    logger.info("Capturing DOM geometry for the %s HTML ...", label)
    success, error = capture_element_screenshots(
        html_content=html_content,
        output_folder=Path(analysis_dir),
        target_xpath_str="/html/body",
    )
    if not success:
        raise RuntimeError(f"{label} HTML screenshot capture failed: {error}")

    logger.info("%s analysis saved to %s", label, analysis_dir)
    return str(analysis_dir)


def take_full_page_screenshot_b64(html_path: str) -> Tuple[str, dict]:
    """Take a full-page screenshot and return ``(base64_png, {width, height})``.

    *html_path* may be a local file path or a ``http(s)://`` URL.
    When a URL is given the page is loaded via ``goto`` so all remote
    resources (scripts, styles, images) are fetched and executed before
    the screenshot is taken.
    """
    from playwright.sync_api import sync_playwright  # noqa: E402 (lazy)

    is_url = html_path.startswith("http://") or html_path.startswith("https://")

    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        context = new_context(browser)
        page = context.new_page()
        if is_url:
            goto_and_settle(page, html_path)
        else:
            with open(html_path, "r", encoding="utf-8") as fh:
                html_content = fh.read()
            set_content_and_settle(page, html_content)

        dims = page.evaluate(
            """() => ({
                width: document.documentElement.scrollWidth,
                height: document.documentElement.scrollHeight
            })"""
        )
        screenshot_bytes = page.screenshot(full_page=True)
        context.close()
        browser.close()

    return base64.b64encode(screenshot_bytes).decode("utf-8"), dims


def _inject_base_tag(html_content: str, url: str) -> str:
    """Inject a ``<base href="origin/">`` tag into *html_content* so that
    relative URLs for CSS, fonts, and images resolve correctly when the HTML
    is loaded via ``set_content`` or ``srcdoc`` (neither of which has a
    meaningful base URL by default).

    The base is set to the scheme+host+path-directory of *url*, e.g.
    ``https://docs.python.org/3.11/`` stays as-is, while
    ``https://example.com/page`` becomes ``https://example.com/``.
    """
    from urllib.parse import urljoin
    # Base = everything up to and including the last '/' in the path
    base = urljoin(url, ".")
    tag = f'<base href="{base}">'
    # Insert after <head> if present, otherwise before the first tag
    lower = html_content.lower()
    for marker in ("<head>", "<html>"):
        idx = lower.find(marker)
        if idx != -1:
            insert_at = idx + len(marker)
            return html_content[:insert_at] + tag + html_content[insert_at:]
    # Fallback: prepend
    return tag + html_content


def fetch_url_as_html(url: str, output_path: str) -> str:
    """Fetch a URL with Playwright, wait for full render, and save the
    fully-rendered HTML (``document.documentElement.outerHTML``) to
    *output_path*.

    Using ``networkidle`` ensures all async scripts and dynamic content
    have settled before the HTML is captured, so the saved file reflects
    what a real browser would show — not just the raw server response.

    Returns the saved HTML content string.
    """
    from playwright.sync_api import sync_playwright  # noqa: E402 (lazy)

    logger.info("Fetching URL for HTML capture: %s", url)
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        context = new_context(browser)
        page = context.new_page()
        goto_and_settle(page, url)
        html_content = page.evaluate("document.documentElement.outerHTML")
        context.close()
        browser.close()

    # Inject a <base> tag so that when this HTML is later loaded via
    # set_content() or srcdoc, all relative CSS/font/image URLs resolve
    # correctly against the original origin.
    html_content = _inject_base_tag(html_content, url)

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(html_content)
    logger.info("Saved rendered HTML from %s -> %s", url, output_path)
    return html_content


# ═══════════════════════════════════════════════════════════════════════════
#  Step 2 - Leaf / section extraction helpers
# ═══════════════════════════════════════════════════════════════════════════

def get_leaves_for_section(
    nodes: dict, section_xpath: str,
) -> Tuple[List[str], List[str]]:
    """Return ``(text_leaf_xpaths, image_leaf_xpaths)`` for a section."""
    text_leaves: List[str] = []
    image_leaves: List[str] = []
    visited: Set[str] = set()
    queue = [section_xpath]

    while queue:
        xp = queue.pop(0)
        if xp in visited:
            continue
        visited.add(xp)

        # Handle combined xpaths (e.g. "xpath1|xpath2")
        if "|" in xp:
            for part in xp.split("|"):
                if part in nodes:
                    queue.append(part)
            continue

        if xp not in nodes:
            continue

        node = nodes[xp]
        if len(node["children"]) == 0:
            if node["contains_image"]:
                image_leaves.append(xp)
            else:
                text_leaves.append(xp)
        else:
            queue.extend(node["children"])

    return text_leaves, image_leaves


# ═══════════════════════════════════════════════════════════════════════════
#  Step 3 - Heuristic matching (HTML parsing based matching)
# ═══════════════════════════════════════════════════════════════════════════

def heuristic_section_matching(
    orig_section_nodes: dict,
    gen_section_nodes: dict,
    similarity_threshold: float = 0.8,
) -> dict:
    """Match sections by text similarity + bbox proximity.

    Returns ``{orig_xpath: gen_xpath}``."""
    if len(orig_section_nodes) == 1 and len(gen_section_nodes) == 1:
        return {
            list(orig_section_nodes.keys())[0]: list(gen_section_nodes.keys())[0]
        }

    candidates = []
    best_sim_for_orig: dict = {}  # o_xp -> (best_sim, best_g_xp) for diagnostics
    for o_xp, o_node in orig_section_nodes.items():
        o_text = re.sub(r"\s+", " ", o_node["text_content"]).strip()
        o_center = _bbox_center(o_node["bbox"])
        for g_xp, g_node in gen_section_nodes.items():
            g_text = re.sub(r"\s+", " ", g_node["text_content"]).strip()
            g_center = _bbox_center(g_node["bbox"])
            sim = SequenceMatcher(None, o_text, g_text).ratio()
            prev_best = best_sim_for_orig.get(o_xp, (0.0, None))
            if sim > prev_best[0]:
                best_sim_for_orig[o_xp] = (sim, g_xp)
            if sim < similarity_threshold:
                continue
            dist = np.linalg.norm(o_center - g_center)
            spatial = 1 - min(dist / 1000.0, 1.0)
            if o_text.strip() == "":
                score = sim * 0.2 + spatial * 0.8
            else:
                score = sim * 0.8 + spatial * 0.2
            candidates.append(
                {"orig": o_xp, "gen": g_xp, "score": score}
            )

    candidates.sort(key=lambda c: c["score"], reverse=True)
    matches: dict = {}
    matched_gen: Set[str] = set()
    for c in candidates:
        if c["orig"] not in matches and c["gen"] not in matched_gen:
            matches[c["orig"]] = c["gen"]
            matched_gen.add(c["gen"])

    # Log unmatched orig sections with diagnostic reason
    for o_xp in orig_section_nodes:
        if o_xp not in matches:
            o_text = re.sub(r"\s+", " ", orig_section_nodes[o_xp]["text_content"]).strip()
            best = best_sim_for_orig.get(o_xp, (0.0, None))
            logger.info(
                "Unmatched orig section: %s | text_content=%r (len=%d) | "
                "best_sim=%.3f (vs %s) | threshold=%.2f",
                o_xp, o_text[:80], len(o_text), best[0], best[1], similarity_threshold,
            )

    return matches


def _section_matches_dict_to_list(
    matches: dict,
) -> List[Tuple[List[str], List[str]]]:
    """Convert {orig_xpath: gen_xpath} dict to List[Tuple[List[str], List[str]]]."""
    return [([o], [g]) for o, g in matches.items()]


def heuristic_text_leaves_matching(
    orig_nodes: dict,
    gen_nodes: dict,
    orig_text_leaves: List[str],
    gen_text_leaves: List[str],
    similarity_threshold: float = 0.9,
) -> Tuple[List[Tuple[List[str], List[str]]], Set[str], dict]:
    """Match text leaves by SequenceMatcher + bbox proximity.

    Handles three cases:
    1. Near-exact text matches (SequenceMatcher >= similarity_threshold).
    2. One original maps to adjacent/sibling generated leaves whose combined
       text is near-identical to the original.
    3. Substring/superstring: one leaf's text is fully contained in the other,
       producing many-to-many groups:
       - orig superstring of gen → one orig matched to multiple gens
         (iteratively consume remaining orig text with more gen leaves).
       - orig substring of gen → multiple origs matched to one gen
         (iteratively consume remaining gen text with more orig leaves).

    Returns ``(match_groups, matched_gen_set, gen_nodes)`` where
    ``match_groups`` is a list of ``([orig_xpaths], [gen_xpaths])`` tuples.
    ``compute_leaf_ious`` natively accepts this list format.
    """
    # ------------------------------------------------------------------ #
    # Pass 1 – near-exact text similarity                                 #
    # ------------------------------------------------------------------ #
    candidates = []
    for o_xp in orig_text_leaves:
        o_node = orig_nodes[o_xp]
        o_text = re.sub(r"\s+", " ", o_node["text_content"]).strip()
        o_center = _bbox_center(o_node["bbox"])
        for g_xp in gen_text_leaves:
            g_node = gen_nodes[g_xp]
            g_text = re.sub(r"\s+", " ", g_node["text_content"]).strip()
            g_center = _bbox_center(g_node["bbox"])
            sim = SequenceMatcher(None, o_text, g_text).ratio()
            if sim < similarity_threshold:
                continue
            dist = np.linalg.norm(o_center - g_center)
            spatial = 1 - min(dist / 1000.0, 1.0)
            score = (sim * 0.2 + spatial * 0.8) if o_text == "" else (sim * 0.8 + spatial * 0.2)
            candidates.append({"orig": o_xp, "gen": g_xp, "score": score})

    candidates.sort(key=lambda c: c["score"], reverse=True)
    matched_orig: Set[str] = set()
    matched_gen: Set[str] = set()
    p1_groups: List[Tuple[List[str], List[str]]] = []
    for c in candidates:
        if c["orig"] not in matched_orig and c["gen"] not in matched_gen:
            p1_groups.append(([c["orig"]], [c["gen"]]))
            matched_orig.add(c["orig"])
            matched_gen.add(c["gen"])

    # ------------------------------------------------------------------ #
    # Pass 2 – combine adjacent/sibling generated leaves                  #
    # ------------------------------------------------------------------ #
    remaining_orig = [o for o in orig_text_leaves if o not in matched_orig]
    remaining_gen = [g for g in gen_text_leaves if g not in matched_gen]

    p2_groups: List[Tuple[List[str], List[str]]] = []
    if remaining_orig and remaining_gen:
        remaining_gen_sorted = sorted(
            remaining_gen,
            key=lambda x: (gen_nodes[x]["bbox"]["y"], gen_nodes[x]["bbox"]["x"]),
        )
        to_combine: List[str] = []

        # Find sibling pairs
        for gxp in remaining_gen_sorted:
            if gxp in to_combine:
                continue
            g_node = gen_nodes[gxp]
            for sib in remaining_gen_sorted:
                if sib == gxp or sib in to_combine:
                    continue
                if gen_nodes[sib]["parent"] == g_node["parent"]:
                    to_combine.extend([gxp, sib])
                    break

        # Find vertically / horizontally adjacent pairs
        for gxp in remaining_gen_sorted:
            if gxp in to_combine:
                continue
            g = gen_nodes[gxp]
            for adj in remaining_gen_sorted:
                if adj == gxp or adj in to_combine:
                    continue
                a = gen_nodes[adj]
                if (a["bbox"]["y"] - (g["bbox"]["y"] + g["bbox"]["height"]) < 2
                        and abs(g["bbox"]["x"] - a["bbox"]["x"]) < 10):
                    to_combine.extend([gxp, adj])
                    break
                if (a["bbox"]["x"] - (g["bbox"]["x"] + g["bbox"]["width"]) < 2
                        and abs(g["bbox"]["y"] - a["bbox"]["y"]) < 10):
                    to_combine.extend([gxp, adj])
                    break

        # Build (g1, g2) pairs and score against remaining orig leaves
        cands2 = []
        for i in range(0, len(to_combine) - 1, 2):
            g1, g2 = to_combine[i], to_combine[i + 1]
            combined_text = gen_nodes[g1]["text_content"] + gen_nodes[g2]["text_content"]
            combined_bbox = _get_union_bbox([gen_nodes[g1]["bbox"], gen_nodes[g2]["bbox"]])
            combined_center = _bbox_center(combined_bbox)
            for ro_xp in remaining_orig:
                ro = orig_nodes[ro_xp]
                sim = SequenceMatcher(None, ro["text_content"], combined_text).ratio()
                if sim < similarity_threshold:
                    continue
                dist = np.linalg.norm(_bbox_center(ro["bbox"]) - combined_center)
                spatial = 1 - min(dist / 1000.0, 1.0)
                cands2.append({"orig": ro_xp, "gen_pair": (g1, g2), "score": sim * 0.8 + spatial * 0.2})

        cands2.sort(key=lambda c: c["score"], reverse=True)
        for c in cands2:
            g1, g2 = c["gen_pair"]
            if c["orig"] not in matched_orig and g1 not in matched_gen and g2 not in matched_gen:
                p2_groups.append(([c["orig"]], [g1, g2]))
                matched_orig.add(c["orig"])
                matched_gen.add(g1)
                matched_gen.add(g2)

    # ------------------------------------------------------------------ #
    # Pass 3 – substring / superstring matching (many-to-many)           #
    # ------------------------------------------------------------------ #
    remaining_orig = [o for o in orig_text_leaves if o not in matched_orig]
    remaining_gen = [g for g in gen_text_leaves if g not in matched_gen]

    p3_groups: List[Tuple[List[str], List[str]]] = []
    if remaining_orig and remaining_gen:
        # Pre-normalise texts (lower-case for substring checks)
        o_norm: Dict[str, str] = {
            xp: re.sub(r"\s+", " ", orig_nodes[xp]["text_content"]).strip().lower()
            for xp in remaining_orig
        }
        g_norm: Dict[str, str] = {
            xp: re.sub(r"\s+", " ", gen_nodes[xp]["text_content"]).strip().lower()
            for xp in remaining_gen
        }

        # Collect all (o_xp, g_xp) pairs where one text is a proper substring
        # of the other
        sub_cands = []
        for o_xp in remaining_orig:
            o_text = o_norm[o_xp]
            if not o_text:
                continue
            o_center = _bbox_center(orig_nodes[o_xp]["bbox"])
            for g_xp in remaining_gen:
                g_text = g_norm[g_xp]
                if not g_text:
                    continue
                g_center = _bbox_center(gen_nodes[g_xp]["bbox"])
                if g_text in o_text and g_text != o_text:
                    # orig is superstring → will consume gen leaves from orig
                    coverage = len(g_text) / len(o_text)
                    rel = "orig_super"
                elif o_text in g_text and o_text != g_text:
                    # gen is superstring → will consume orig leaves from gen
                    coverage = len(o_text) / len(g_text)
                    rel = "gen_super"
                else:
                    continue
                dist = np.linalg.norm(o_center - g_center)
                spatial = 1 - min(dist / 1000.0, 1.0)
                sub_cands.append({
                    "orig": o_xp, "gen": g_xp,
                    "score": coverage * 0.7 + spatial * 0.3,
                    "rel": rel,
                })

        sub_cands.sort(key=lambda c: c["score"], reverse=True)

        # Accumulate groups, tracking remaining (unconsumed) text per anchor.
        # orig_super: one orig → multiple gens  (orig_groups[o_xp] = [g_xps])
        # gen_super:  multiple origs → one gen  (gen_groups[g_xp]  = [o_xps])
        orig_groups: Dict[str, List[str]] = {}
        gen_groups: Dict[str, List[str]] = {}
        orig_rem: Dict[str, str] = {}   # remaining text of each orig being consumed
        gen_rem: Dict[str, str] = {}    # remaining text of each gen being consumed
        sub_matched_orig: Set[str] = set()
        sub_matched_gen: Set[str] = set()

        for c in sub_cands:
            o_xp, g_xp, rel = c["orig"], c["gen"], c["rel"]

            if rel == "orig_super":
                if g_xp in sub_matched_gen:
                    continue
                # orig may already be accumulating more gens (in orig_groups); allow that.
                # But if it was consumed by a gen_super group, skip - this will most likely never happen.
                if o_xp in sub_matched_orig and o_xp not in orig_groups:
                    continue
                o_rem = orig_rem.get(o_xp, o_norm[o_xp])
                g_text = g_norm[g_xp]
                if g_text not in o_rem:
                    continue
                orig_groups.setdefault(o_xp, []).append(g_xp)
                sub_matched_gen.add(g_xp)
                sub_matched_orig.add(o_xp)
                orig_rem[o_xp] = o_rem.replace(g_text, "", 1).strip()

            else:  # gen_super
                if o_xp in sub_matched_orig:
                    continue
                # gen may already be accumulating more origs; allow that.
                # But if it was consumed by an orig_super group, skip - this will most likely never happen.
                if g_xp in sub_matched_gen and g_xp not in gen_groups:
                    continue
                g_rem = gen_rem.get(g_xp, g_norm[g_xp])
                o_text = o_norm[o_xp]
                if o_text not in g_rem:
                    continue
                gen_groups.setdefault(g_xp, []).append(o_xp)
                sub_matched_orig.add(o_xp)
                sub_matched_gen.add(g_xp)
                gen_rem[g_xp] = g_rem.replace(o_text, "", 1).strip()

        for o_xp, gen_list in orig_groups.items():
            gen_list.sort(key=lambda xp: (gen_nodes[xp]["bbox"]["y"], gen_nodes[xp]["bbox"]["x"]))
            p3_groups.append(([o_xp], gen_list))
            matched_orig.add(o_xp)
            matched_gen.update(gen_list)
        for g_xp, orig_list in gen_groups.items():
            orig_list.sort(key=lambda xp: (orig_nodes[xp]["bbox"]["y"], orig_nodes[xp]["bbox"]["x"]))
            p3_groups.append((orig_list, [g_xp]))
            matched_orig.update(orig_list)
            matched_gen.add(g_xp)

    all_groups = p1_groups + p2_groups + p3_groups
    return all_groups, matched_gen, gen_nodes


def heuristic_image_leaves_matching(
    orig_nodes: dict,
    gen_nodes: dict,
    orig_image_leaves: List[str],
    gen_image_leaves: List[str],
    matched_text_leaves: dict,
    max_distance: float = 200.0,
    similarity_threshold: float = 0.9,
) -> Tuple[List[Tuple[List[str], List[str]]], Set[str]]:
    """Match image leaves using relative-vector projection from text anchors.

    Returns ``(match_groups, matched_gen_set)`` where match_groups is a list
    of ``([orig_xpaths], [gen_xpaths])`` tuples."""
    matched_gen: Set[str] = set(matched_text_leaves.values())
    matches: dict = {}   # 1:1 dict used internally for anchor projection
    candidates = []

    # --- Pass 1: near-exact text content + position matching ---------------
    for o_xp in orig_image_leaves:
        o_node = orig_nodes[o_xp]
        o_text = re.sub(r"\s+", " ", o_node["text_content"]).strip()
        if o_text == "":
            continue
        for g_xp in gen_image_leaves:
            g_node = gen_nodes[g_xp]
            g_text = re.sub(r"\s+", " ", g_node["text_content"]).strip()
            if g_text == "":
                continue
            sim = SequenceMatcher(None, o_text, g_text).ratio()
            if sim < similarity_threshold:
                continue
            o_center = _bbox_center(o_node["bbox"])
            g_center = _bbox_center(g_node["bbox"])
            dist = np.linalg.norm(o_center - g_center)
            spatial = 1 - min(dist / 1000.0, 1.0)
            score = sim * 0.8 + spatial * 0.2
            candidates.append({"orig": o_xp, "gen": g_xp, "score": score})

    candidates.sort(key=lambda c: c["score"], reverse=True)
    for c in candidates:
        if c["orig"] not in matches and c["gen"] not in matched_gen:
            matches[c["orig"]] = c["gen"]
            matched_gen.add(c["gen"])

    # --- Pass 2: substring / superstring text matching ---------------------
    remaining_orig_img = [o for o in orig_image_leaves if o not in matches]
    remaining_gen_img = [g for g in gen_image_leaves if g not in matched_gen]

    sub_groups: List[Tuple[List[str], List[str]]] = []
    if remaining_orig_img and remaining_gen_img:
        o_norm: Dict[str, str] = {
            xp: re.sub(r"\s+", " ", orig_nodes[xp]["text_content"]).strip().lower()
            for xp in remaining_orig_img
        }
        g_norm: Dict[str, str] = {
            xp: re.sub(r"\s+", " ", gen_nodes[xp]["text_content"]).strip().lower()
            for xp in remaining_gen_img
        }

        sub_cands = []
        for o_xp in remaining_orig_img:
            o_text = o_norm[o_xp]
            if not o_text:
                continue
            o_center = _bbox_center(orig_nodes[o_xp]["bbox"])
            for g_xp in remaining_gen_img:
                g_text = g_norm[g_xp]
                if not g_text:
                    continue
                g_center = _bbox_center(gen_nodes[g_xp]["bbox"])
                if g_text in o_text and g_text != o_text:
                    coverage = len(g_text) / len(o_text)
                    rel = "orig_super"
                elif o_text in g_text and o_text != g_text:
                    coverage = len(o_text) / len(g_text)
                    rel = "gen_super"
                else:
                    continue
                dist = np.linalg.norm(o_center - g_center)
                spatial = 1 - min(dist / 1000.0, 1.0)
                sub_cands.append({
                    "orig": o_xp, "gen": g_xp,
                    "score": coverage * 0.7 + spatial * 0.3,
                    "rel": rel,
                })

        sub_cands.sort(key=lambda c: c["score"], reverse=True)

        orig_groups: Dict[str, List[str]] = {}
        gen_groups: Dict[str, List[str]] = {}
        orig_rem: Dict[str, str] = {}
        gen_rem: Dict[str, str] = {}
        sub_matched_orig: Set[str] = set()
        sub_matched_gen: Set[str] = set()

        for c in sub_cands:
            o_xp, g_xp, rel = c["orig"], c["gen"], c["rel"]
            if rel == "orig_super":
                if g_xp in sub_matched_gen:
                    continue
                if o_xp in sub_matched_orig and o_xp not in orig_groups:
                    continue
                o_rem = orig_rem.get(o_xp, o_norm[o_xp])
                g_text = g_norm[g_xp]
                if g_text not in o_rem:
                    continue
                orig_groups.setdefault(o_xp, []).append(g_xp)
                sub_matched_gen.add(g_xp)
                sub_matched_orig.add(o_xp)
                orig_rem[o_xp] = o_rem.replace(g_text, "", 1).strip()
            else:  # gen_super
                if o_xp in sub_matched_orig:
                    continue
                if g_xp in sub_matched_gen and g_xp not in gen_groups:
                    continue
                g_rem = gen_rem.get(g_xp, g_norm[g_xp])
                o_text = o_norm[o_xp]
                if o_text not in g_rem:
                    continue
                gen_groups.setdefault(g_xp, []).append(o_xp)
                sub_matched_orig.add(o_xp)
                sub_matched_gen.add(g_xp)
                gen_rem[g_xp] = g_rem.replace(o_text, "", 1).strip()

        for o_xp, gen_list in orig_groups.items():
            gen_list.sort(key=lambda xp: (gen_nodes[xp]["bbox"]["y"], gen_nodes[xp]["bbox"]["x"]))
            sub_groups.append(([o_xp], gen_list))
            matched_gen.update(gen_list)
        for g_xp, orig_list in gen_groups.items():
            orig_list.sort(key=lambda xp: (orig_nodes[xp]["bbox"]["y"], orig_nodes[xp]["bbox"]["x"]))
            sub_groups.append((orig_list, [g_xp]))
            matched_gen.add(g_xp)

    sub_matched_orig_img: Set[str] = {o for o_list, _ in sub_groups for o in o_list}
    for o_xp in orig_image_leaves:
        if o_xp in matches or o_xp in sub_matched_orig_img:
            continue
        o_center = _bbox_center(orig_nodes[o_xp]["bbox"])

        # 1. Find nearest matched text anchor in original
        nearest_anchor = None
        gen_anchor = None
        min_dist = float("inf")
        for anchor_xp, gen_anchor_xp in matched_text_leaves.items():
            d = np.linalg.norm(o_center - _bbox_center(orig_nodes[anchor_xp]["bbox"]))
            if d < min_dist:
                min_dist = d
                nearest_anchor = anchor_xp
                gen_anchor = gen_anchor_xp
        
        # also check for anchors from the image leaf matches so far
        for anchor_xp, gen_anchor_xp in matches.items():
            d = np.linalg.norm(o_center - _bbox_center(orig_nodes[anchor_xp]["bbox"]))
            if d < min_dist:
                min_dist = d
                nearest_anchor = anchor_xp
                gen_anchor = gen_anchor_xp

        if nearest_anchor is None:
            continue

        # 2. Relative vector from anchor to image in original
        anchor_center = _bbox_center(orig_nodes[nearest_anchor]["bbox"])
        vec = o_center - anchor_center

        # 3. Project onto generated graph
        gen_anchor_center = _bbox_center(gen_nodes[gen_anchor]["bbox"])
        predicted = gen_anchor_center + vec

        # 4. Find closest unmatched generated image leaf
        best = None
        best_dist = float("inf")
        for g_xp in gen_image_leaves:
            if g_xp in matched_gen:
                continue
            d = np.linalg.norm(predicted - _bbox_center(gen_nodes[g_xp]["bbox"]))
            if d < best_dist:
                best_dist = d
                best = g_xp

        if best and best_dist < max_distance:
            matches[o_xp] = best
            matched_gen.add(best)

    # Fallback: if no text anchors existed, match by pure bbox proximity
    if not matched_text_leaves:
        cands = []
        for o_xp in orig_image_leaves:
            if o_xp in matches:
                continue
            o_c = _bbox_center(orig_nodes[o_xp]["bbox"])
            for g_xp in gen_image_leaves:
                g_c = _bbox_center(gen_nodes[g_xp]["bbox"])
                cands.append({"orig": o_xp, "gen": g_xp, "dist": np.linalg.norm(o_c - g_c)})
        cands.sort(key=lambda c: c["dist"])
        for c in cands:
            if c["orig"] not in matches and c["gen"] not in matched_gen:
                matches[c["orig"]] = c["gen"]
                matched_gen.add(c["gen"])

    all_img_groups = [([o], [g]) for o, g in matches.items()] + sub_groups
    return all_img_groups, matched_gen


# ═══════════════════════════════════════════════════════════════════════════
#  Step 4 - IoU computation with coordinate adjustment
# ═══════════════════════════════════════════════════════════════════════════

def compute_leaf_ious(
    matched_nodes: Any,
    orig_nodes: dict,
    gen_nodes: dict,
    orig_section_bbox: dict,
    gen_section_bbox: dict,
    orig_sec_xpaths: Optional[List[str]] = None,
    gen_sec_xpaths: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Compute IoUs for matched leaf groups with cumulative coordinate adjustment.

    *matched_nodes* may be either:

    * ``Dict[str, str]`` - classic 1:1 matching (one orig xpath -> one gen
      xpath).  Internally normalised to the list-of-tuples form below.
    * ``List[Tuple[List[str], List[str]]]`` - many-to-many matching where
      each entry is ``(orig_leaf_xpaths, gen_leaf_xpaths)``.  When a group
      contains multiple leaves, the **union bbox** on each side is used for
      the IoU computation and coordinate adjustment.

    Returns a list of dicts (one per matched group), ready for the report.
    """
    if not matched_nodes or not orig_section_bbox or not gen_section_bbox:
        return []

    # ------------------------------------------------------------------
    # Normalise matched_nodes to List[Tuple[List[str], List[str]]]
    # Accepts:
    #   Dict[str, str]                          - 1:1 pairs
    #   List[Tuple[List[str], List[str]]]       - many:many (list form)
    #   List[Tuple[Set[str], Set[str]]]         - many:many (set form)
    # ------------------------------------------------------------------
    if isinstance(matched_nodes, dict):
        match_groups: List[Tuple[List[str], List[str]]] = [
            ([o_xp], [g_xp]) for o_xp, g_xp in matched_nodes.items()
        ]
    else:
        # Ensure each side is a list (callers may pass sets)
        match_groups = [
            (list(o) if not isinstance(o, list) else o,
             list(g) if not isinstance(g, list) else g)
            for o, g in matched_nodes
        ]

    osw = orig_section_bbox["width"]
    osh = orig_section_bbox["height"]
    gsw = gen_section_bbox["width"]
    gsh = gen_section_bbox["height"]
    if osw <= 0 or osh <= 0 or gsw <= 0 or gsh <= 0:
        return []

    # ------------------------------------------------------------------
    # Helper: absolute bbox -> relative (fraction of section dimensions)
    # ------------------------------------------------------------------
    def _to_rel(bbox: dict, sec_bbox: dict, sw: float, sh: float) -> dict:
        return {
            "x": (bbox["x"] - sec_bbox["x"]) / sw,
            "y": (bbox["y"] - sec_bbox["y"]) / sh,
            "width": bbox["width"] / sw,
            "height": bbox["height"] / sh,
        }

    # ------------------------------------------------------------------
    # Build per-group relative bboxes (union when >1 leaf per side)
    # ------------------------------------------------------------------
    valid_groups: List[Tuple[List[str], List[str], dict, dict]] = []
    # Also keep individual gen-leaf relative bboxes for coordinate adjustment
    gen_leaf_rel: Dict[str, dict] = {}

    for orig_xps, gen_xps in match_groups:
        orig_abs_bboxes = [
            orig_nodes[xp]["bbox"] for xp in orig_xps
            if xp in orig_nodes and orig_nodes[xp].get("bbox")
        ]
        gen_abs_bboxes = [
            gen_nodes[xp]["bbox"] for xp in gen_xps
            if xp in gen_nodes and gen_nodes[xp].get("bbox")
        ]
        if not orig_abs_bboxes or not gen_abs_bboxes:
            continue

        orig_union = _get_union_bbox(orig_abs_bboxes)
        gen_union = _get_union_bbox(gen_abs_bboxes)

        orig_rel_bbox = _to_rel(orig_union, orig_section_bbox, osw, osh)
        gen_rel_bbox = _to_rel(gen_union, gen_section_bbox, gsw, gsh)

        valid_groups.append((orig_xps, gen_xps, orig_rel_bbox, gen_rel_bbox))

        # Track each individual gen leaf for coordinate adjustment
        for gxp in gen_xps:
            gb = gen_nodes.get(gxp, {}).get("bbox")
            if gb and gxp not in gen_leaf_rel:
                gen_leaf_rel[gxp] = _to_rel(gb, gen_section_bbox, gsw, gsh)

    # Sort groups by generated union-bbox position (y, then x)
    valid_groups.sort(key=lambda g: (g[3]["y"], g[3]["x"]))

    # # Deep-copy gen rel bboxes for cumulative adjustment
    # adjusted_gen_group = [copy.deepcopy(g[3]) for g in valid_groups]
    # adjusted_gen_leaf = copy.deepcopy(gen_leaf_rel)

    results: List[Dict[str, Any]] = []

    for group_idx, (orig_xps, gen_xps, orig_rel_bbox, gen_rel_bbox) in enumerate(valid_groups):
        # Compute IoU between the group bboxes
        iou = _bbox_iou(orig_rel_bbox, gen_rel_bbox)

        # ---- Cumulative coordinate adjustment for subsequent groups ----
        # height_diff = orig_rel_bbox["height"] - gen_rel_bbox["height"]
        # width_diff = orig_rel_bbox["width"] - gen_rel_bbox["width"]
        # y_diff = orig_rel_bbox["y"] - gen_rel_bbox["y"]
        # x_diff = orig_rel_bbox["x"] - gen_rel_bbox["x"]
        # gen_rel_bbox_bottom = gen_rel_bbox["y"] + gen_rel_bbox["height"]
        # gen_rel_bbox_right = gen_rel_bbox["x"] + gen_rel_bbox["width"]

        # for future_idx in range(group_idx + 1, len(valid_groups)):
        #     future_bbox = adjusted_gen_group[future_idx]
        #     has_horizontal_overlap = (
        #         future_bbox["x"] < gen_rel_bbox["x"] + gen_rel_bbox["width"]
        #         and future_bbox["x"] + future_bbox["width"] > gen_rel_bbox["x"]
        #     )
        #     has_vertical_overlap = (
        #         future_bbox["y"] < gen_rel_bbox["y"] + gen_rel_bbox["height"]
        #         and future_bbox["y"] + future_bbox["height"] > gen_rel_bbox["y"]
        #     )
        #     if has_horizontal_overlap and future_bbox["y"] + future_bbox["height"] > gen_rel_bbox_bottom:
        #         future_bbox["y"] += (height_diff + y_diff)
        #     if has_vertical_overlap and future_bbox["x"] + future_bbox["width"] > gen_rel_bbox_right:
        #         future_bbox["x"] += (width_diff + x_diff)

        # ---- Build human-readable description for the group ----
        # Collect text content from all original leaves in the group
        text_parts: List[str] = []
        any_image = False
        for oxp in orig_xps:
            node = orig_nodes.get(oxp, {})
            txt = (node.get("text_content") or "").strip()
            if txt:
                text_parts.append(txt)
            if node.get("contains_image"):
                any_image = True

        display_text = " | ".join(text_parts) if text_parts else (
            "[image element]" if any_image else "[empty element]"
        )
        if len(display_text) > 80:
            display_text = display_text[:77] + "..."

        # Compare union areas for size description
        orig_union_abs = _get_union_bbox([
            orig_nodes[xp]["bbox"] for xp in orig_xps
            if xp in orig_nodes and orig_nodes[xp].get("bbox")
        ])
        gen_union_abs = _get_union_bbox([
            gen_nodes[xp]["bbox"] for xp in gen_xps
            if xp in gen_nodes and gen_nodes[xp].get("bbox")
        ])
        orig_area = _bbox_area(orig_union_abs) if orig_union_abs else 0
        gen_area = _bbox_area(gen_union_abs) if gen_union_abs else 0
        if gen_area > orig_area:
            size_desc = "is bigger in the generated version"
        elif gen_area < orig_area:
            size_desc = "is smaller in the generated version"
        else:
            size_desc = "has roughly the same size in the generated version"

        # For single-leaf groups, store the xpath directly; for multi-leaf
        # groups, join with "|".
        orig_xpath_str = orig_xps[0] if len(orig_xps) == 1 else "|".join(orig_xps)
        gen_xpath_str = gen_xps[0] if len(gen_xps) == 1 else "|".join(gen_xps)

        # Transformed xpaths
        orig_transformed_parts = [
            orig_nodes[xp].get("transformed_xpath", xp) for xp in orig_xps
            if xp in orig_nodes
        ]
        gen_transformed_parts = [
            gen_nodes[xp].get("transformed_xpath", xp) for xp in gen_xps
            if xp in gen_nodes
        ]
        orig_transformed_str = (
            orig_transformed_parts[0] if len(orig_transformed_parts) == 1
            else "|".join(orig_transformed_parts)
        )
        gen_transformed_str = (
            gen_transformed_parts[0] if len(gen_transformed_parts) == 1
            else "|".join(gen_transformed_parts)
        )

        results.append({
            "orig_xpath": orig_xpath_str,
            "gen_xpath": gen_xpath_str,
            "orig_transformed": orig_transformed_str,
            "gen_transformed": gen_transformed_str,
            "iou": round(iou, 4),
            "text_content": display_text,
            "size_description": size_desc,
            "orig_bbox": orig_union_abs,
            "gen_bbox": gen_union_abs,
            "orig_rel_bbox": orig_rel_bbox,
            "gen_rel_bbox": copy.deepcopy(gen_rel_bbox),
            "orig_sec_bbox": orig_section_bbox,
            "gen_sec_bbox": gen_section_bbox,
            "orig_sec_xpaths": orig_sec_xpaths,
            "gen_sec_xpaths": gen_sec_xpaths,
            "is_image": any_image,
        })

    return results


def compute_section_ious(
    matched_sections: List[Tuple[List[str], List[str]]],
    orig_section_nodes: dict,
    gen_section_nodes: dict,
) -> Tuple[float, List[dict]]:
    """Return ``(mean_section_iou, per_section_results)``.

    Each entry in *matched_sections* is a ``(orig_xpaths, gen_xpaths)`` tuple
    where each side may contain one or more section xpaths.  When multiple
    sections are matched together the IoU is computed over the union bbox.

    Each result dict contains:

    * ``orig``, ``gen`` - xpath lists
    * ``iou`` - float
    * ``orig_bbox``, ``gen_bbox`` - union bounding boxes
    * ``description`` - human-readable description of size/position changes
    """
    per_section: List[dict] = []
    for idx, (orig_xps, gen_xps) in enumerate(matched_sections):
        orig_bbox = _get_union_bbox([
            orig_section_nodes[xp]["bbox"] for xp in orig_xps
        ])
        # print([orig_section_nodes[xp]["children"] for xp in orig_xps])
        gen_bbox = _get_union_bbox([
            gen_section_nodes[xp]["bbox"] for xp in gen_xps
        ])
        # print([gen_section_nodes[xp]["children"] for xp in gen_xps])
        # IoU based on width/height only (ignore position differences).
        # Normalize both bboxes to origin (0,0) so only dimensions matter.
        if orig_bbox and gen_bbox:
            orig_at_origin = {"x": 0, "y": 0, "width": orig_bbox["width"], "height": orig_bbox["height"]}
            gen_at_origin = {"x": 0, "y": 0, "width": gen_bbox["width"], "height": gen_bbox["height"]}
            iou = _bbox_iou(orig_at_origin, gen_at_origin)
        else:
            iou = 0.0

        # Build a human-readable description
        desc_parts: List[str] = []

        # Section label
        desc_parts.append(f"Section {idx + 1}")

        # Size comparison
        orig_area = _bbox_area(orig_bbox)
        gen_area = _bbox_area(gen_bbox)
        if orig_area > 0 and gen_area > 0:
            ratio = gen_area / orig_area
            if ratio > 1.05:
                desc_parts.append(f"Generated is {ratio:.1f}x larger")
            elif ratio < 0.95:
                desc_parts.append(f"Generated is {1/ratio:.1f}x smaller")
            else:
                desc_parts.append("Roughly the same size")

        # Position shift
        if orig_bbox and gen_bbox:
            dx = gen_bbox["x"] - orig_bbox["x"]
            dy = gen_bbox["y"] - orig_bbox["y"]
            if abs(dx) > 5 or abs(dy) > 5:
                desc_parts.append(
                    f"Position shifted by ({dx:+.0f} horizontal, {dy:+.0f} vertical) px"
                )

        per_section.append({
            "orig": orig_xps,
            "gen": gen_xps,
            "iou": round(iou, 4),
            "orig_bbox": orig_bbox,
            "gen_bbox": gen_bbox,
            "description": " | ".join(desc_parts),
        })
    avg = (sum(s["iou"] for s in per_section) / len(per_section)
           if per_section else 0.0)
    return round(avg, 4), per_section


# ═══════════════════════════════════════════════════════════════════════════
#  Step 5 - Section matching from ground-truth JSON
# ═══════════════════════════════════════════════════════════════════════════

def load_section_matching_from_json(
    output_dir: str,
    orig_section_nodes: dict,
    gen_nodes: Optional[dict] = None,
) -> Optional[List[Tuple[List[str], List[str]]]]:
    """Load pre-computed section matching from ``section_structure_xpaths.json``.

    The JSON (produced by ``merge_html_sections`` in
    ``dom_utils.py``) maps each section_id to
    the xpaths of the ``acr-structure`` divs it contributed to the final
    generated HTML.

    The section_ids correspond to the original sections sorted by
    y-coordinate - the same ordering that ``get_section_nodes`` produces.

    Returns matched sections as ``List[Tuple[List[str], List[str]]]``, or
    ``None`` if the JSON file does not exist.
    """
    json_path = os.path.join(output_dir, "section_structure_xpaths.json")
    if not os.path.exists(json_path):
        return None

    with open(json_path, "r", encoding="utf-8") as fh:
        xpaths_data = json.load(fh)

    final_html_mapping = xpaths_data.get("final_html", {})
    if not final_html_mapping:
        logger.warning("final_html mapping is empty in section_structure_xpaths.json")
        return []

    # Sort original section nodes by y (then x) to establish section_id ordering.
    sorted_orig = sorted(
        orig_section_nodes.items(),
        key=lambda item: (item[1]["bbox"]["y"], item[1]["bbox"]["x"]),
    )

    matched_sections: List[Tuple[List[str], List[str]]] = []
    if gen_nodes is not None:
        gen_node_keys: set = set(gen_nodes.keys())
        # arrange gen_node_keys in ascending order of string length
        gen_node_keys = sorted(gen_node_keys, key=lambda x: len(x))

    for section_idx, (orig_xpath, _orig_node) in enumerate(sorted_orig):
        sid = str(section_idx)
        gen_xpaths_from_json = final_html_mapping.get(sid, [])
        if not gen_xpaths_from_json:
            logger.warning(
                "No generated xpaths for section_id %s (orig xpath: %s)",
                sid, orig_xpath,
            )
            continue

        if gen_nodes is not None:
            # Resolve each JSON xpath to a gen_section_node key.
            resolved_gen_xpaths: List[str] = []
            for json_xp in gen_xpaths_from_json:
                if json_xp in gen_node_keys:
                    resolved_gen_xpaths.append(json_xp)
                else:
                    # Fallback: find a gen_section_node that is the closest
                    # descendant of the JSON xpath (the acr-structure div may
                    # sit one level above or below the detected section node).
                    found = False
                    for gen_key in gen_node_keys:
                        if gen_key.startswith(json_xp + "/"):
                            if gen_key not in resolved_gen_xpaths:
                                resolved_gen_xpaths.append(gen_key)
                            found = True

                    if not found:
                        logger.warning(
                            "Could not resolve gen xpath %s to any "
                            "gen_section_node (section_id %s)", json_xp, sid,
                        )

            if resolved_gen_xpaths:
                matched_sections.append(([orig_xpath], resolved_gen_xpaths))
        else:
            matched_sections.append(([orig_xpath], gen_xpaths_from_json))

    if gen_nodes is not None:
        logger.info(
            "Section matching from JSON: %d pairs (orig sections: %d, gen nodes: %d)",
            len(matched_sections), len(orig_section_nodes), len(gen_nodes),
        )
    else:
        logger.info(
            "Section matching from JSON: %d pairs (orig sections: %d)",
            len(matched_sections), len(orig_section_nodes),
        )
    logger.info("Section matching from JSON: %s", matched_sections)
    return matched_sections


# ═══════════════════════════════════════════════════════════════════════════
#  Step 6 - Orchestrate matching for a given type
# ═══════════════════════════════════════════════════════════════════════════

def _run_heuristic_matching(
    orig_nodes: dict,
    gen_nodes: dict,
    orig_section_nodes: dict,
    gen_section_nodes: dict,
    output_dir: str,
) -> Tuple[List[dict], List[Tuple[List[str], List[str]]]]:
    """Run pure-heuristic *leaf* matching with heuristic or JSON-based section matching.

    Uses ``section_structure_xpaths.json`` if present; otherwise runs
    heuristic_section_matching.

    Returns ``(leaf_diffs, matched_sections)``.
    """
    matched_sections = load_section_matching_from_json(
        output_dir, orig_section_nodes, gen_nodes,
    )
    if matched_sections is None:
        matches_dict = heuristic_section_matching(
            orig_section_nodes, gen_section_nodes,
        )
        matched_sections = _section_matches_dict_to_list(matches_dict)
        logger.info("Heuristic: section matches (heuristic): %d pairs", len(matched_sections))
    else:
        logger.info("Heuristic: section matches from JSON: %d pairs", len(matched_sections))

    all_leaf_diffs: List[dict] = []
    for orig_section_list, gen_section_list in matched_sections:
        # Gather leaves from ALL sections in the match group
        text1: List[str] = []
        img1: List[str] = []
        for o_s in orig_section_list:
            t, i = get_leaves_for_section(orig_nodes, o_s)
            text1.extend(t)
            img1.extend(i)

        text2: List[str] = []
        img2: List[str] = []
        for g_s in gen_section_list:
            t, i = get_leaves_for_section(gen_nodes, g_s)
            text2.extend(t)
            img2.extend(i)

        text_match_groups, matched_gen, gen_nodes = heuristic_text_leaves_matching(
            orig_nodes, gen_nodes, text1, text2,
        )
        # some image leaves in the original may be generated as text leaves, because they
        # look like text boxes. so, we take the unmatched text leaves from the generated set
        # and try to match them with the image leaves in the original
        unmatched_gen_text_leaves = list(set(text2) - matched_gen)
        # Flatten to a 1:1 anchor dict for heuristic_image_leaves_matching
        # (spatial projection only needs one representative pair per group)
        text_anchor_dict = {
            o_list[0]: g_list[0]
            for o_list, g_list in text_match_groups
            if o_list and g_list
        }
        img_matches, _ = heuristic_image_leaves_matching(
            orig_nodes, gen_nodes, img1, img2 + unmatched_gen_text_leaves, text_anchor_dict,
        )
        all_match_groups = text_match_groups + img_matches

        orig_s_bbox = _get_union_bbox([
            orig_section_nodes[o_s]["bbox"] for o_s in orig_section_list
        ])
        gen_s_bbox = _get_union_bbox([
            gen_nodes[g_s]["bbox"] for g_s in gen_section_list
        ])

        diffs = compute_leaf_ious(
            all_match_groups, orig_nodes, gen_nodes,
            orig_s_bbox, gen_s_bbox,
            orig_sec_xpaths=orig_section_list,
            gen_sec_xpaths=gen_section_list,
        )
        all_leaf_diffs.extend(diffs)

    return all_leaf_diffs, matched_sections


def _run_embedding_matching(
    orig_nodes: dict,
    gen_nodes: dict,
    orig_section_nodes: dict,
    gen_section_nodes: dict,
    orig_analysis_dir: str,
    gen_analysis_dir: str,
    output_dir: str,
) -> Tuple[List[dict], List[Tuple[List[str], List[str]]]]:
    """Run embedding-based *leaf* matching with JSON-based section matching.

    Section matching is loaded from ``section_structure_xpaths.json``
    (ground-truth produced by the conversion script) instead of being
    computed via embedding similarity.

    Returns ``(leaf_diffs, matched_sections)``.
    """
    try:
        from matching_module import ElementMatcher  # noqa: E402
    except ImportError as exc:
        raise RuntimeError(
            "embedding matching dependencies unavailable: ElementMatcher requires "
            "DINOv2 and SentenceTransformer"
        ) from exc

    # text_leaves_matching(use_embeddings=True) expects a specific directory
    # layout.  It reads original screenshots from ``reduced_output_dir``
    # (captured with is_reduced=True so images are replaced by gray
    # placeholders) and generated screenshots from a sibling directory
    # obtained via reduced_output_dir.replace("original_output_reduced",
    # "translated_output").
    #
    # Layout we create under <output_dir>/matching_workspace/:
    #   original_output_reduced/  - reduced screenshots of the original HTML
    #   translated_output/        - symlink to generated_analysis/
    workspace = Path(output_dir) / "matching_workspace"
    workspace.mkdir(exist_ok=True)

    # 1. Capture reduced screenshots for the original HTML
    orig_reduced_dir = workspace / "original_output_reduced"
    if not orig_reduced_dir.is_dir() or len(list(orig_reduced_dir.iterdir())) < 3:
        orig_html_path = Path(orig_analysis_dir) / "original.html"
        with open(orig_html_path, "r", encoding="utf-8") as fh:
            orig_html_content = fh.read()
        
        Path(orig_reduced_dir).mkdir(parents=True, exist_ok=True)

        logger.info("Capturing reduced screenshots for the original HTML ...")
        success, error = capture_element_screenshots(
            html_content=orig_html_content,
            output_folder=orig_reduced_dir,
            target_xpath_str="/html/body",
            is_reduced=True,
        )
        if not success:
            raise RuntimeError(f"reduced screenshot capture failed: {error}")
    else:
        logger.info("Reduced original screenshots already exist - skipping capture.")

    # 2. Symlink generated_analysis/ as translated_output/
    gen_link = workspace / "translated_output"
    if not gen_link.exists():
        gen_link.symlink_to(Path(gen_analysis_dir).resolve())

    logger.info("Loading ML models for embedding matching ...")
    matcher = ElementMatcher(device="cpu")

    # Section matching: use JSON if available, else matcher.section_matching
    matched_sections = load_section_matching_from_json(
        output_dir, orig_section_nodes, gen_nodes,
    )
    if matched_sections is None:
        matches_dict, _, gen_section_nodes = matcher.section_matching(
            orig_section_nodes, gen_section_nodes,
            use_embeddings=True,
            reduced_output_dir=str(orig_reduced_dir),
        )
        matched_sections = _section_matches_dict_to_list(matches_dict)
        logger.info("Embedding: section matches (matcher.section_matching): %d pairs", len(matched_sections))
    else:
        logger.info("Embedding: section matches from JSON: %d pairs", len(matched_sections))

    all_leaf_diffs: List[dict] = []
    for orig_section_list, gen_section_list in matched_sections:
        # Gather leaves from ALL sections in the match group
        text1: List[str] = []
        img1: List[str] = []
        for o_s in orig_section_list:
            t, i = get_leaves_for_section(orig_nodes, o_s)
            text1.extend(t)
            img1.extend(i)

        text2: List[str] = []
        img2: List[str] = []
        for g_s in gen_section_list:
            t, i = get_leaves_for_section(gen_nodes, g_s)
            text2.extend(t)
            img2.extend(i)

        all_leaves1 = text1 + img1
        all_leaves2 = text2 + img2

        leaves_matches, _, gen_nodes = matcher.text_leaves_matching(
            orig_nodes, gen_nodes, all_leaves1, all_leaves2,
            use_embeddings=True,
            reduced_output_dir=str(orig_reduced_dir),
        )

        orig_s_bbox = _get_union_bbox([
            orig_section_nodes[o_s]["bbox"] for o_s in orig_section_list
        ])
        gen_s_bbox = _get_union_bbox([
            gen_nodes[g_s]["bbox"] for g_s in gen_section_list
        ])

        diffs = compute_leaf_ious(
            leaves_matches, orig_nodes, gen_nodes,
            orig_s_bbox, gen_s_bbox,
            orig_sec_xpaths=orig_section_list,
            gen_sec_xpaths=gen_section_list,
        )
        all_leaf_diffs.extend(diffs)

    return all_leaf_diffs, matched_sections


def _run_vlm_matching(
    orig_nodes: dict,
    gen_nodes: dict,
    orig_section_nodes: dict,
    gen_section_nodes: dict,
    orig_analysis_dir: str,
    gen_analysis_dir: str,
    orig_screenshot_path: str,
    gen_screenshot_path: str,
    output_dir: str,
) -> Tuple[List[dict], List[Tuple[List[str], List[str]]]]:
    """Run VLM-based *leaf* matching with JSON-based section matching.

    Section matching is loaded from ``section_structure_xpaths.json``
    (ground-truth produced by the conversion script) instead of being
    computed via VLM.  Leaf matching within each section pair still uses
    the VLM pipeline.

    Returns ``(leaf_diffs, matched_sections)``.
    """
    try:
        import dotenv
        dotenv.load_dotenv(Path(__file__).resolve().parent.parent / ".env")

        from matching_module import (  # noqa: E402
            ElementMatcher,
            create_stacked_leaves_visualization,
            create_stacked_sections_visualization,
        )
        from client import create_ai_client  # noqa: E402
    except Exception as exc:
        raise RuntimeError(f"VLM matching dependencies unavailable: {exc}") from exc

    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT")
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")
    if not endpoint or not api_key:
        raise RuntimeError("Azure OpenAI credentials not set for VLM matching")

    client = create_ai_client(provider="gpt41")

    logger.info("Loading ML models for VLM matching ...")
    matcher = ElementMatcher(device="cpu")

    workspace = Path(orig_analysis_dir).parent / "vlm_workspace"
    workspace.mkdir(exist_ok=True)

    # Use the full-page screenshots already saved in main() Step-2
    orig_body_png = orig_screenshot_path
    gen_body_png = gen_screenshot_path
    if not Path(orig_body_png).exists() or not Path(gen_body_png).exists():
        raise RuntimeError(
            f"full-page screenshots not found for VLM matching: "
            f"{orig_body_png} / {gen_body_png}"
        )

    # Section matching: use JSON if available, else matcher.section_matching_vlm
    matched_sections = load_section_matching_from_json(
        output_dir, orig_section_nodes, gen_nodes,
    )
    if matched_sections is None:
        sections_viz_path = str(workspace / "sections_overview.png")
        try:
            _, section_label_mapping = create_stacked_sections_visualization(
                orig_screenshot_path=str(orig_body_png),
                gen_screenshot_path=str(gen_body_png),
                orig_section_nodes=orig_section_nodes,
                gen_section_nodes=gen_section_nodes,
                output_path=sections_viz_path,
            )
        except Exception as exc:
            raise RuntimeError(f"failed to create VLM sections visualization: {exc}") from exc

        if len(orig_section_nodes) == 1 and len(gen_section_nodes) == 1:
            vlm_section_matches = {"1": [["Section-1"], ["Section-A"]]}
        else:
            vlm_section_matches = matcher.section_matching_vlm(sections_viz_path, client)

        if not vlm_section_matches or "error" in vlm_section_matches:
            raise RuntimeError(f"VLM section matching failed: {vlm_section_matches}")

        sanitized_vlm_matches = sanitize_section_matches(vlm_section_matches)
        orig_label_to_xp = section_label_mapping.get("original_section_label_to_xpath", {})
        gen_label_to_xp = section_label_mapping.get("generated_section_label_to_xpath", {})

        matched_sections = []
        for _idx, (orig_labels, gen_labels) in sanitized_vlm_matches.items():
            orig_transformed_xps = [
                orig_label_to_xp[lbl]
                for lbl in orig_labels if lbl in orig_label_to_xp
            ]
            gen_transformed_xps = [
                gen_label_to_xp[lbl]
                for lbl in gen_labels if lbl in gen_label_to_xp
            ]
            if not orig_transformed_xps or not gen_transformed_xps:
                continue

            orig_section_xpaths = [
                reverse_transform_xpath(txp)
                for txp in orig_transformed_xps
                if reverse_transform_xpath(txp) in orig_section_nodes
            ]
            gen_section_xpaths = [
                reverse_transform_xpath(txp)
                for txp in gen_transformed_xps
                if reverse_transform_xpath(txp) in gen_section_nodes
            ]
            if orig_section_xpaths and gen_section_xpaths:
                matched_sections.append((orig_section_xpaths, gen_section_xpaths))

        logger.info("VLM: section matches (matcher.section_matching_vlm): %d pairs", len(matched_sections))
    else:
        logger.info("VLM: section matches from JSON: %d pairs", len(matched_sections))
    # logger.info("VLM matched sections: %s", matched_sections)

    # --- VLM leaf matching ---
    all_leaf_diffs: List[dict] = []
    leaves_viz_dir = workspace / "leaf_visualizations"
    leaves_viz_dir.mkdir(exist_ok=True)

    for orig_section_list, gen_section_list in matched_sections:
        # Gather leaves from ALL sections in this match pair
        text1: List[str] = []
        img1: List[str] = []
        for o_s in orig_section_list:
            t, i = get_leaves_for_section(orig_nodes, o_s)
            text1.extend(t)
            img1.extend(i)

        text2: List[str] = []
        img2: List[str] = []
        for g_s in gen_section_list:
            t, i = get_leaves_for_section(gen_nodes, g_s)
            text2.extend(t)
            img2.extend(i)

        # Compute union bounding boxes across all sections in the pair
        orig_s_bbox = _get_union_bbox([
            orig_section_nodes[o_s]["bbox"] for o_s in orig_section_list
        ])
        gen_s_bbox = _get_union_bbox([
            gen_nodes[g_s]["bbox"] for g_s in gen_section_list
        ])

        section_name = "|".join(
            transform_xpath(xp) for xp in orig_section_list
        )

        try:
            viz_path = str(leaves_viz_dir / f"{section_name}_leaves.png")
            _, leaf_label_mapping = create_stacked_leaves_visualization(
                orig_screenshot_path=str(orig_body_png),
                gen_screenshot_path=str(gen_body_png),
                orig_section_bbox=orig_s_bbox,
                gen_section_bbox=gen_s_bbox,
                orig_text_leaves=text1,
                orig_image_leaves=img1,
                gen_text_leaves=text2,
                gen_image_leaves=img2,
                subset_orig_nodes=orig_nodes,
                subset_gen_nodes=gen_nodes,
                output_path=viz_path,
            )

            vlm_leaf_matches = matcher.leaf_matching_vlm(viz_path, client)

            # normalize the keys in leaf_label_mapping
            leaf_label_mapping = {
                "original_leaves": {normalize_leaf_name(k): v for k, v in leaf_label_mapping["original_leaves"].items()},
                "generated_leaves": {normalize_leaf_name(k): v for k, v in leaf_label_mapping["generated_leaves"].items()}
            }

            pairs = convert_vlm_leaf_matches_to_xpath_pairs(vlm_leaf_matches, leaf_label_mapping)

            diffs = compute_leaf_ious(
                pairs, orig_nodes, gen_nodes,
                orig_s_bbox, gen_s_bbox,
                orig_sec_xpaths=orig_section_list,
                gen_sec_xpaths=gen_section_list,
            )
            all_leaf_diffs.extend(diffs)
        except Exception as exc:
            logger.warning(
                "VLM leaf visualization/matching failed for sections %s: %s",
                section_name, exc,
            )
            text_m, _, gen_nodes = heuristic_text_leaves_matching(orig_nodes, gen_nodes, text1, text2)
            text_anchor_m = {o_list[0]: g_list[0] for o_list, g_list in text_m if o_list and g_list}
            img_m, _ = heuristic_image_leaves_matching(orig_nodes, gen_nodes, img1, img2, text_anchor_m)
            diffs = compute_leaf_ious(
                text_m + img_m,
                orig_nodes, gen_nodes, orig_s_bbox, gen_s_bbox,
                orig_sec_xpaths=orig_section_list,
                gen_sec_xpaths=gen_section_list,
            )
            all_leaf_diffs.extend(diffs)

    return all_leaf_diffs, matched_sections


def run_matching(
    matching_type: str,
    orig_nodes: dict,
    gen_nodes: dict,
    orig_section_nodes: dict,
    gen_section_nodes: dict,
    orig_analysis_dir: str,
    gen_analysis_dir: str,
    output_dir: str,
    orig_screenshot_path: str = "",
    gen_screenshot_path: str = "",
) -> Tuple[List[dict], List[dict], float, float, Dict[str, Any]]:
    """Run matching for the given type.

    Returns ``(leaf_diffs, section_diffs, avg_leaf_iou, avg_section_iou, extra_stats)``.

    *extra_stats* contains:
        - ``leaf_above_thresh`` / ``leaf_above_pct``: count / % of leaves with IoU >= 0.95
        - ``sec_above_thresh`` / ``sec_above_pct``: count / % of sections with IoU >= 0.95
        - ``iou_threshold``: the threshold used (0.95)
        - ``total_orig_leaves`` / ``total_gen_leaves``: total leaf count per side
        - ``matched_leaves``: number of matched leaf pairs
        - ``missing_leaves``: orig leaves with no match in generated
        - ``extra_leaves``: generated leaves with no match in original

    *section_diffs* is a list of dicts with keys ``orig``, ``gen``, ``iou``,
    ``orig_bbox``, ``gen_bbox``, ``description``.
    """
    IOU_THRESHOLD = 0.95

    # Deep copy so each matching type works independently
    o_n = copy.deepcopy(orig_nodes)
    g_n = copy.deepcopy(gen_nodes)
    o_s = copy.deepcopy(orig_section_nodes)
    g_s = copy.deepcopy(gen_section_nodes)

    if matching_type == "heuristic":
        leaf_diffs, matched_secs = _run_heuristic_matching(
            o_n, g_n, o_s, g_s, output_dir,
        )
    elif matching_type == "embedding":
        leaf_diffs, matched_secs = _run_embedding_matching(
            o_n, g_n, o_s, g_s, orig_analysis_dir, gen_analysis_dir,
            output_dir,
        )
    elif matching_type == "vlm":
        leaf_diffs, matched_secs = _run_vlm_matching(
            o_n, g_n, o_s, g_s, orig_analysis_dir, gen_analysis_dir,
            orig_screenshot_path, gen_screenshot_path, output_dir,
        )
    else:
        raise ValueError(f"Unknown matching type: {matching_type}")

    # Sort diffs by original leaf area descending (largest first)
    leaf_diffs.sort(key=lambda d: (_bbox_area(d.get("orig_bbox")), d.get("orig_bbox")["y"]), reverse=True)

    avg_leaf = (round(sum(d["iou"] for d in leaf_diffs) / len(leaf_diffs), 4)
                if leaf_diffs else 0.0)
    avg_sec, section_diffs = compute_section_ious(matched_secs, o_n, g_n)

    # ── Extra statistics: threshold %, missing/extra leaves ───────────
    total_orig_leaves: Set[str] = set()
    total_gen_leaves: Set[str] = set()
    for orig_section_list, gen_section_list in matched_secs:
        for o_sec in orig_section_list:
            t, i = get_leaves_for_section(o_n, o_sec)
            total_orig_leaves.update(t)
            total_orig_leaves.update(i)
        for g_sec in gen_section_list:
            t, i = get_leaves_for_section(g_n, g_sec)
            total_gen_leaves.update(t)
            total_gen_leaves.update(i)

    matched_orig: Set[str] = set()
    matched_gen: Set[str] = set()
    for d in leaf_diffs:
        for xp in d["orig_xpath"].split("|"):
            matched_orig.add(xp)
        for xp in d["gen_xpath"].split("|"):
            matched_gen.add(xp)

    missing_xpaths = sorted(total_orig_leaves - matched_orig)
    extra_xpaths = sorted(total_gen_leaves - matched_gen)

    def _leaf_info(xp: str, nodes: dict, side: str) -> Dict[str, Any]:
        node = nodes.get(xp, {})
        bbox = node.get("bbox") or {"x": 0, "y": 0, "width": 0, "height": 0}
        txt = (node.get("text_content") or "").strip()
        if not txt:
            txt = "[image element]" if node.get("contains_image") else "[empty element]"
        if len(txt) > 80:
            txt = txt[:77] + "..."
        return {"xpath": xp, "bbox": bbox, "text": txt, "side": side,
                "is_image": bool(node.get("contains_image"))}

    missing_leaves = [_leaf_info(xp, o_n, "orig") for xp in missing_xpaths if xp in o_n]
    extra_leaves = [_leaf_info(xp, g_n, "gen") for xp in extra_xpaths if xp in g_n]

    n_matched = len(leaf_diffs)
    leaf_above = sum(1 for d in leaf_diffs if d["iou"] >= IOU_THRESHOLD)
    sec_above = sum(1 for s in section_diffs if s["iou"] >= IOU_THRESHOLD)

    # Compute unmatched sections: orig/gen section xpaths not present in any matched pair
    matched_orig_secs: Set[str] = set()
    matched_gen_secs: Set[str] = set()
    for orig_sec_list, gen_sec_list in matched_secs:
        matched_orig_secs.update(orig_sec_list)
        matched_gen_secs.update(gen_sec_list)
    unmatched_orig_sections = sorted(set(o_s.keys()) - matched_orig_secs)
    unmatched_gen_sections = sorted(set(g_s.keys()) - matched_gen_secs)

    if unmatched_orig_sections:
        logger.info("Unmatched orig sections (%d):", len(unmatched_orig_sections))
        for xp in unmatched_orig_sections:
            logger.info("  ORIG unmatched: %s", xp)
    if unmatched_gen_sections:
        logger.info("Unmatched gen sections (%d):", len(unmatched_gen_sections))
        for xp in unmatched_gen_sections:
            logger.info("  GEN  unmatched: %s", xp)

    extra_stats: Dict[str, Any] = {
        "iou_threshold": IOU_THRESHOLD,
        "leaf_above_thresh": leaf_above,
        "leaf_above_pct": round(100.0 * leaf_above / n_matched, 1) if n_matched else 0.0,
        "sec_above_thresh": sec_above,
        "sec_above_pct": round(100.0 * sec_above / len(section_diffs), 1) if section_diffs else 0.0,
        "matched_leaves": n_matched,
        "total_orig_leaves": len(total_orig_leaves),
        "total_gen_leaves": len(total_gen_leaves),
        "missing_leaves": missing_leaves,
        "extra_leaves": extra_leaves,
        "unmatched_orig_sections": unmatched_orig_sections,
        "unmatched_gen_sections": unmatched_gen_sections,
    }

    return leaf_diffs, section_diffs, avg_leaf, avg_sec, extra_stats


# ═══════════════════════════════════════════════════════════════════════════
#  Step 6 - HTML report generation
# ═══════════════════════════════════════════════════════════════════════════

def _esc(text: str) -> str:
    """HTML-escape a string."""
    return html_module.escape(text, quote=True)


def _build_extra_stats_html(
    extra_stats: Optional[Dict[str, Any]],
    section_count: int = 0,
) -> Tuple[str, float, int, int, int, int]:
    """Build HTML score cards for threshold %, missing & extra leaves.
    Returns (html, iou_threshold, matched_leaves, section_count,
             unmatched_orig_sections, unmatched_gen_sections) for JS recompute."""
    if not extra_stats:
        return ("", 0.95, 0, section_count, 0, 0)
    es = extra_stats
    thresh = es.get("iou_threshold", 0.95)
    matched = es.get("matched_leaves", 0)
    n_unmatched_orig = len(es.get("unmatched_orig_sections", []))
    n_unmatched_gen = len(es.get("unmatched_gen_sections", []))
    html = (
        f'<div class="sc"><div class="v" id="leafAbovePct">{es["leaf_above_pct"]:.1f}%</div>'
        f'<div class="l" id="leafAboveLabel">Leaves >= {thresh} IoU ({es["leaf_above_thresh"]}/{matched})</div></div>'
        f'<div class="sc"><div class="v" id="secAbovePct">{es["sec_above_pct"]:.1f}%</div>'
        f'<div class="l" id="secAboveLabel">Sections >= {thresh} IoU ({es["sec_above_thresh"]})</div></div>'
    )
    return (html, thresh, matched, section_count, n_unmatched_orig, n_unmatched_gen)


def generate_html_report(
    orig_html: str,
    gen_html: str,
    orig_screenshot_b64: str,
    gen_screenshot_b64: str,
    orig_page_dims: dict,
    gen_page_dims: dict,
    leaf_diffs: List[dict],
    section_diffs: List[dict],
    matching_type: str,
    avg_leaf_iou: float,
    avg_section_iou: float,
    output_path: str,
    extra_stats: Optional[Dict[str, Any]] = None,
    verified_diffs_pretty: Optional[str] = None,
) -> None:
    """Generate a self-contained HTML report with three columns.

    Columns 1 & 2: Editable HTML code + live preview tabs, with download.
    Column 3: Diff list; hovering computes boxes on the fly and focuses previews.
    """
    # --- Build VLM diff lookup: real_xpath -> list of (key, diff_dict) ---
    # Populated now so leaf & section loops can embed matched diffs inline.
    _vlm_xpath_lookup: Dict[str, List[tuple]] = {}
    if verified_diffs_pretty:
        try:
            _vdiffs_data = json.loads(verified_diffs_pretty)
        except Exception:
            _vdiffs_data = None
        if _vdiffs_data and isinstance(_vdiffs_data, dict):
            for _vkey in sorted(_vdiffs_data.keys()):
                if not _vkey.startswith("difference_"):
                    continue
                _vd = _vdiffs_data[_vkey]
                if not isinstance(_vd, dict):
                    continue
                _elem_id = _vd.get("element_id", "")
                if _elem_id:
                    _real_xp = reverse_transform_xpath(_elem_id)
                    _vlm_xpath_lookup.setdefault(_real_xp, []).append((_vkey, _vd))

    def _vlm_diff_block(matched: List[tuple]) -> str:
        """Return HTML for VLM diffs matched to a leaf/section item."""
        parts = []
        for vkey, vd in matched:
            elem_desc = _esc(str(vd.get("element_description", "")))
            css_change = _esc(str(vd.get("computed_style_change", "N/A")))
            difference = _esc(str(vd.get("difference", "")))
            dissim = _esc(str(vd.get("dissimilarity_score", "")))
            notice = _esc(str(vd.get("noticeability_score", "")))
            parts.append(
                f'<div class="vlm-diff-block">'
                f'<div class="vlm-diff-key">{_esc(vkey)}</div>'
                f'<div class="vlm-diff-row"><span class="vlm-label">Element:</span> <span class="vlm-val">{elem_desc}</span></div>'
                f'<div class="vlm-diff-row"><span class="vlm-label">CSS change:</span> <span class="vlm-val">{css_change}</span></div>'
                f'<div class="vlm-diff-row"><span class="vlm-label">Difference:</span> <span class="vlm-val">{difference}</span></div>'
                f'<div class="vlm-diff-scores">'
                f'<span class="vlm-score-badge">Dissimilarity: {dissim}</span>'
                f'<span class="vlm-score-badge">Noticeability: {notice}</span>'
                f'</div>'
                f'</div>'
            )
        return "".join(parts)

    # --- Build Leaf diff items HTML (with xpaths for on-the-fly highlighting) ---
    leaf_rows = []
    for i, diff in enumerate(leaf_diffs):
        iou = diff["iou"]
        if iou < 0.3:
            cls = "low"
        elif iou < 0.7:
            cls = "mid"
        else:
            cls = "high"

        text = _esc(diff["text_content"])
        desc = _esc(diff["size_description"])
        ob = diff["orig_bbox"]
        gb = diff["gen_bbox"]
        orig_xpaths = diff.get("orig_xpath", "").split("|") if diff.get("orig_xpath") else []
        gen_xpaths = diff.get("gen_xpath", "").split("|") if diff.get("gen_xpath") else []
        orig_xpaths_json = _esc(json.dumps(orig_xpaths))
        gen_xpaths_json = _esc(json.dumps(gen_xpaths))
        orig_sec_xpaths = diff.get("orig_sec_xpaths") or []
        gen_sec_xpaths = diff.get("gen_sec_xpaths") or []
        osb = diff.get("orig_sec_bbox")
        gsb = diff.get("gen_sec_bbox")
        sec_attrs = ""
        if orig_sec_xpaths and gen_sec_xpaths:
            sec_attrs = (
                f' data-orig-sec-xpaths="{_esc(json.dumps(orig_sec_xpaths))}" '
                f'data-gen-sec-xpaths="{_esc(json.dumps(gen_sec_xpaths))}"'
            )
        if osb and gsb:
            sec_attrs += (
                f' data-orig-sec-ox="{osb["x"]}" data-orig-sec-oy="{osb["y"]}" '
                f'data-orig-sec-ow="{osb["width"]}" data-orig-sec-oh="{osb["height"]}" '
                f'data-gen-sec-gx="{gsb["x"]}" data-gen-sec-gy="{gsb["y"]}" '
                f'data-gen-sec-gw="{gsb["width"]}" data-gen-sec-gh="{gsb["height"]}"'
            )

        ob_str = f'(x={ob["x"]:.1f}, y={ob["y"]:.1f}, w={ob["width"]:.1f}, h={ob["height"]:.1f})'
        gb_str = f'(x={gb["x"]:.1f}, y={gb["y"]:.1f}, w={gb["width"]:.1f}, h={gb["height"]:.1f})'
        bbox_details = [f'<div class="bbox-coords">Orig: {_esc(ob_str)}<br>Gen: {_esc(gb_str)}</div>']
        orb = diff.get("orig_rel_bbox")
        grb = diff.get("gen_rel_bbox")
        if orb is not None and grb is not None:
            orb_str = f'(x={orb["x"]:.4f}, y={orb["y"]:.4f}, w={orb["width"]:.4f}, h={orb["height"]:.4f})'
            grb_str = f'(x={grb["x"]:.4f}, y={grb["y"]:.4f}, w={grb["width"]:.4f}, h={grb["height"]:.4f})'
            if osb and gsb:
                osb_str = f'(x={osb["x"]:.1f}, y={osb["y"]:.1f}, w={osb["width"]:.1f}, h={osb["height"]:.1f})'
                gsb_str = f'(x={gsb["x"]:.1f}, y={gsb["y"]:.1f}, w={gsb["width"]:.1f}, h={gsb["height"]:.1f})'
                bbox_details.append(
                    f'<div class="bbox-coords">Section bbox:<br>Orig: {_esc(osb_str)}<br>Gen: {_esc(gsb_str)}</div>'
                )
            bbox_details.append(
                f'<div class="bbox-coords">Relative bbox (fraction of each section\'s dimensions, used for IoU):<br>Orig: {_esc(orb_str)}<br>Gen: {_esc(grb_str)}</div>'
            )
        bbox_block = (
            f'<details class="bbox-coords-details"><summary>Initial bbox (Python)</summary>'
            f'{"".join(bbox_details)}'
            f'</details>'
        )
        matched_vlm = []
        for oxp in orig_xpaths:
            matched_vlm.extend(_vlm_xpath_lookup.get(oxp, []))
        vlm_block = _vlm_diff_block(matched_vlm) if matched_vlm else ""

        leaf_rows.append(
            f'<div class="diff-item {cls}" '
            f'data-ox="{ob["x"]}" data-oy="{ob["y"]}" '
            f'data-ow="{ob["width"]}" data-oh="{ob["height"]}" '
            f'data-gx="{gb["x"]}" data-gy="{gb["y"]}" '
            f'data-gw="{gb["width"]}" data-gh="{gb["height"]}" '
            f'data-python-iou="{iou}" '
            f'{sec_attrs} '
            f'data-orig-xpaths="{orig_xpaths_json}" data-gen-xpaths="{gen_xpaths_json}" '
            f'onmouseenter="showBoxes(this)" onmouseleave="hideBoxes()">'
            f'<span class="iou-badge">{iou:.4f}</span> '
            f'<div style="flex:1;min-width:0">'
            f'<span class="diff-text">&ldquo;{text}&rdquo; {desc}</span>'
            f'{bbox_block}'
            f'{vlm_block}'
            f'</div>'
            f"</div>"
        )

    leaf_items_html = "\n".join(leaf_rows) if leaf_rows else (
        '<div class="no-diffs">No matched leaf pairs found.</div>'
    )

    # --- Build Section diff items HTML (with xpaths) ---
    section_rows = []
    for i, sd in enumerate(section_diffs):
        iou = sd["iou"]
        if iou < 0.3:
            cls = "low"
        elif iou < 0.7:
            cls = "mid"
        else:
            cls = "high"

        ob = sd.get("orig_bbox") or {"x": 0, "y": 0, "width": 0, "height": 0}
        gb = sd.get("gen_bbox") or {"x": 0, "y": 0, "width": 0, "height": 0}
        orig_xpaths = sd.get("orig", []) or []
        gen_xpaths = sd.get("gen", []) or []
        orig_xpaths_json = _esc(json.dumps(orig_xpaths))
        gen_xpaths_json = _esc(json.dumps(gen_xpaths))

        desc_parts = sd.get("description", "").split(" | ")
        section_label = _esc(desc_parts[0]) if desc_parts else f"Section {i+1}"
        detail_lines = "".join(
            f'<div class="sec-detail">{_esc(p)}</div>'
            for p in desc_parts[1:]
        )
        ob_str = f'(x={ob["x"]:.1f}, y={ob["y"]:.1f}, w={ob["width"]:.1f}, h={ob["height"]:.1f})'
        gb_str = f'(x={gb["x"]:.1f}, y={gb["y"]:.1f}, w={gb["width"]:.1f}, h={gb["height"]:.1f})'
        sec_bbox_block = (
            f'<details class="bbox-coords-details"><summary>Initial bbox (Python)</summary>'
            f'<div class="bbox-coords">Orig: {_esc(ob_str)}<br>Gen: {_esc(gb_str)}</div>'
            f'</details>'
        )

        sec_matched_vlm = []
        for oxp in orig_xpaths:
            sec_matched_vlm.extend(_vlm_xpath_lookup.get(oxp, []))
        sec_vlm_block = _vlm_diff_block(sec_matched_vlm) if sec_matched_vlm else ""

        section_rows.append(
            f'<div class="diff-item sec-item {cls}" '
            f'data-ox="{ob["x"]}" data-oy="{ob["y"]}" '
            f'data-ow="{ob["width"]}" data-oh="{ob["height"]}" '
            f'data-gx="{gb["x"]}" data-gy="{gb["y"]}" '
            f'data-gw="{gb["width"]}" data-gh="{gb["height"]}" '
            f'data-orig-xpaths="{orig_xpaths_json}" data-gen-xpaths="{gen_xpaths_json}" '
            f'onmouseenter="showBoxes(this)" onmouseleave="hideBoxes()">'
            f'<span class="iou-badge">{iou:.4f}</span> '
            f'<div class="sec-desc">'
            f'<div class="sec-label">{section_label}</div>'
            f'{detail_lines}'
            f'{sec_bbox_block}'
            f'{sec_vlm_block}'
            f'</div>'
            f"</div>"
        )

    section_items_html = "\n".join(section_rows) if section_rows else (
        '<div class="no-diffs">No matched section pairs found.</div>'
    )

    # --- Build Unmatched leaves HTML ---
    missing_leaves = (extra_stats or {}).get("missing_leaves", [])
    extra_leaves = (extra_stats or {}).get("extra_leaves", [])
    unmatched_rows = []
    if missing_leaves:
        unmatched_rows.append(
            f'<div class="unmatched-header">Missing in Generated ({len(missing_leaves)})</div>'
        )
        for ml in missing_leaves:
            bb = ml["bbox"]
            bb_str = f'(x={bb["x"]:.1f}, y={bb["y"]:.1f}, w={bb["width"]:.1f}, h={bb["height"]:.1f})'
            bbox_block = (
                f'<details class="bbox-coords-details"><summary>Initial bbox (Python)</summary>'
                f'<div class="bbox-coords">Orig: {_esc(bb_str)}</div>'
                f'</details>'
            )
            xpaths_json = _esc(json.dumps([ml["xpath"]]))
            unmatched_rows.append(
                f'<div class="diff-item" '
                f'data-ox="{bb["x"]}" data-oy="{bb["y"]}" '
                f'data-ow="{bb["width"]}" data-oh="{bb["height"]}" '
                f'data-gx="0" data-gy="0" data-gw="0" data-gh="0" '
                f'data-orig-xpaths="{xpaths_json}" data-gen-xpaths="[]" '
                f'data-unmatched-side="orig" '
                f'onmouseenter="showBoxes(this)" onmouseleave="hideBoxes()">'
                f'<span class="side-badge missing">Missing</span> '
                f'<div style="flex:1;min-width:0">'
                f'<span class="diff-text">&ldquo;{_esc(ml["text"])}&rdquo;</span>'
                f'{bbox_block}'
                f'</div>'
                f'</div>'
            )
    if extra_leaves:
        unmatched_rows.append(
            f'<div class="unmatched-header">Extra in Generated ({len(extra_leaves)})</div>'
        )
        for el in extra_leaves:
            bb = el["bbox"]
            bb_str = f'(x={bb["x"]:.1f}, y={bb["y"]:.1f}, w={bb["width"]:.1f}, h={bb["height"]:.1f})'
            bbox_block = (
                f'<details class="bbox-coords-details"><summary>Initial bbox (Python)</summary>'
                f'<div class="bbox-coords">Gen: {_esc(bb_str)}</div>'
                f'</details>'
            )
            xpaths_json = _esc(json.dumps([el["xpath"]]))
            unmatched_rows.append(
                f'<div class="diff-item" '
                f'data-ox="0" data-oy="0" data-ow="0" data-oh="0" '
                f'data-gx="{bb["x"]}" data-gy="{bb["y"]}" '
                f'data-gw="{bb["width"]}" data-gh="{bb["height"]}" '
                f'data-orig-xpaths="[]" data-gen-xpaths="{xpaths_json}" '
                f'data-unmatched-side="gen" '
                f'onmouseenter="showBoxes(this)" onmouseleave="hideBoxes()">'
                f'<span class="side-badge extra">Extra</span> '
                f'<div style="flex:1;min-width:0">'
                f'<span class="diff-text">&ldquo;{_esc(el["text"])}&rdquo;</span>'
                f'{bbox_block}'
                f'</div>'
                f'</div>'
            )
    n_unmatched = len(missing_leaves) + len(extra_leaves)
    unmatched_items_html = "\n".join(unmatched_rows) if unmatched_rows else (
        '<div class="no-diffs">No unmatched leaves found.</div>'
    )

    # VLM diffs are now embedded inline in the Sections/Leaves tab items above.

    # --- Compute VLM CSS similarity score for header ---
    vlm_css_similarity_html = ""
    if verified_diffs_pretty:
        try:
            _vd = json.loads(verified_diffs_pretty)
            _avg_dissim = _vd.get("average_dissimilarity_score") if isinstance(_vd, dict) else None
            if _avg_dissim is not None:
                _vlm_sim = 1.0 - float(_avg_dissim)
                vlm_css_similarity_html = (
                    f'<div class="sc"><div class="v">{_vlm_sim:.4f}</div>'
                    f'<div class="l">VLM CSS Similarity</div></div>'
                )
        except Exception:
            pass

    orig_html_js = json.dumps(orig_html).replace("</", "<\\/")
    gen_html_js = json.dumps(gen_html).replace("</", "<\\/")
    extra_stats_html, iou_thresh, matched_leaves, sec_count, unmatched_orig_secs, unmatched_gen_secs = _build_extra_stats_html(
        extra_stats, len(section_diffs)
    )

    report_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Conversion Report &ndash; {_esc(matching_type.title())} Matching</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/codemirror.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/mode/xml/xml.min.js"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{height:100%;overflow:hidden}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;background:#eef1f5;color:#1e293b;font-size:14px}}

.hdr{{background:#1e293b;color:#f8fafc;padding:14px 24px;display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:16px;border-bottom:1px solid #e2e8f0}}
.hdr h1{{font-size:1rem;font-weight:600;letter-spacing:-0.01em}}
.scores{{display:flex;gap:12px;flex-wrap:wrap}}
.sc{{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:6px;padding:8px 14px;text-align:center}}
.sc .v{{font-size:1.25rem;font-weight:600}}
.sc .l{{font-size:.7rem;opacity:.8;margin-top:2px}}
.recompute-btn{{margin-left:auto;padding:6px 14px;font-size:.75rem;font-weight:500;border:1px solid rgba(255,255,255,.3);border-radius:6px;background:rgba(255,255,255,.1);color:#f8fafc;cursor:pointer;transition:background .15s}}
.recompute-btn:hover{{background:rgba(255,255,255,.18)}}

.wrap{{display:flex;gap:12px;padding:12px;height:calc(100vh - 64px)}}
.col{{flex:1;background:#fff;border-radius:8px;border:1px solid #e2e8f0;box-shadow:0 1px 3px rgba(0,0,0,.04);display:flex;flex-direction:column;overflow:hidden;min-width:0}}
.col-hdr{{padding:12px 16px;font-weight:600;font-size:.85rem;border-bottom:1px solid #e2e8f0;background:#f8fafc;flex-shrink:0;color:#334155}}

.panel-tabs{{display:flex;border-bottom:1px solid #e2e8f0;background:#f8fafc;flex-shrink:0}}
.panel-tab{{padding:10px 16px;font-size:.8rem;font-weight:500;cursor:pointer;border:none;background:transparent;color:#64748b;border-bottom:2px solid transparent;margin-bottom:-1px;transition:color .15s,border-color .15s}}
.panel-tab:hover{{color:#334155}}
.panel-tab.active{{color:#0f172a;border-bottom-color:#3b82f6}}
.panel-content{{display:none;flex:1;flex-direction:column;overflow:hidden;min-height:0}}
.panel-content.active{{display:flex}}

.toolbar{{display:flex;gap:8px;padding:8px 12px;background:#f8fafc;border-bottom:1px solid #e2e8f0;flex-shrink:0}}
.toolbar-btn{{padding:6px 12px;border:1px solid #e2e8f0;border-radius:6px;background:#fff;cursor:pointer;font-size:.75rem;color:#475569;transition:all .15s}}
.toolbar-btn:hover{{background:#f1f5f9;border-color:#cbd5e1;color:#334155}}

.code-editor-wrap{{flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden}}
.code-editor-wrap .CodeMirror{{flex:1;min-height:0;font-size:12px;line-height:1.5;border:none;border-radius:0}}
.code-editor-wrap .CodeMirror-gutters{{background:#f1f5f9;border-right:1px solid #e2e8f0}}
.code-editor-wrap .CodeMirror-linenumber{{color:#94a3b8;padding:0 8px 0 4px}}
.code-editor-wrap .CodeMirror{{background:#f8fafc}}
.code-editor-wrap .cm-s-default .cm-tag{{color:#0369a1}}
.code-editor-wrap .cm-s-default .cm-attribute{{color:#7c3aed}}
.code-editor-wrap .cm-s-default .cm-string{{color:#059669}}
.code-editor-wrap .cm-s-default .cm-comment{{color:#64748b;font-style:italic}}
.code-editor-wrap .cm-s-default .cm-meta{{color:#0d9488}}
.preview-wrap{{flex:1;display:flex;flex-direction:column;min-height:0;overflow-x:hidden;overflow-y:auto;background:#fff;position:relative}}
.iframe-zoom-wrap{{width:1280px;transform-origin:top left}}
.iframe-zoom-wrap iframe{{width:1280px;height:3000px;border:none;display:block}}

.tab-bar{{display:flex;border-bottom:1px solid #e2e8f0;background:#f8fafc;flex-shrink:0}}
.tab-btn{{padding:10px 16px;font-size:.8rem;font-weight:500;cursor:pointer;border:none;background:transparent;color:#64748b;border-bottom:2px solid transparent;margin-bottom:-1px;transition:color .15s,border-color .15s}}
.tab-btn:hover{{color:#334155}}
.tab-btn.active{{color:#0f172a;border-bottom-color:#3b82f6}}
.tab-content{{display:none;overflow-y:auto;flex:1;min-height:0}}
.tab-content.active{{display:block}}

.diff-list{{padding:10px 12px}}
.diff-item{{padding:10px 12px;border-radius:6px;cursor:pointer;display:flex;align-items:flex-start;gap:10px;margin-bottom:6px;transition:all .15s;border:1px solid transparent}}
.diff-item:hover{{background:#f8fafc;border-color:#e2e8f0}}
.iou-badge{{flex-shrink:0;font-size:.72rem;font-weight:600;padding:3px 8px;border-radius:4px}}
.diff-item.low .iou-badge{{background:#fef2f2;color:#b91c1c}}
.diff-item.mid .iou-badge{{background:#fffbeb;color:#b45309}}
.diff-item.high .iou-badge{{background:#f0fdf4;color:#15803d}}
.diff-item.na .iou-badge{{background:#f1f5f9;color:#64748b}}
.diff-text{{font-size:.8rem;color:#475569;line-height:1.5}}
.bbox-coords-details{{font-size:.7rem;margin-top:6px}}
.bbox-coords-details summary{{cursor:pointer;color:#64748b}}
.bbox-coords-details summary:hover{{color:#475569}}
.bbox-coords{{font-family:ui-monospace,monospace;font-size:.68rem;color:#64748b;margin-top:4px;line-height:1.4}}
.side-badge{{flex-shrink:0;font-size:.68rem;font-weight:600;padding:3px 8px;border-radius:4px}}
.side-badge.missing{{background:#fef2f2;color:#b91c1c}}
.side-badge.extra{{background:#eff6ff;color:#1d4ed8}}
.unmatched-header{{padding:10px 12px;font-weight:600;font-size:.82rem;color:#334155;border-bottom:1px solid #e2e8f0;margin-bottom:4px}}
.no-diffs{{padding:32px;text-align:center;color:#94a3b8;font-size:.85rem}}
.sec-item{{flex-direction:column;gap:6px}}
.sec-desc{{display:flex;flex-direction:column;gap:4px;width:100%}}
.sec-label{{font-weight:600;font-size:.82rem;color:#1e293b}}
.sec-detail{{font-size:.75rem;color:#64748b;line-height:1.45}}
.diff-details{{white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.78rem;background:#f8fafc;padding:12px;border-radius:6px;line-height:1.6;color:#334155;overflow-y:auto;flex:1;min-height:0}}
.vlm-diff-block{{margin-top:8px;padding:7px 10px;background:#f8f7ff;border-left:3px solid #6366f1;border-radius:0 4px 4px 0}}
.vlm-diff-key{{font-weight:700;font-size:.72rem;color:#6366f1;text-transform:uppercase;letter-spacing:.04em;margin-bottom:3px}}
.vlm-diff-row{{font-size:.78rem;color:#334155;line-height:1.45}}
.vlm-label{{font-weight:600;color:#475569}}
.vlm-val{{color:#334155}}
.vlm-diff-scores{{display:flex;gap:6px;margin-top:4px;flex-wrap:wrap}}
.vlm-score-badge{{font-size:.7rem;font-weight:600;padding:2px 8px;border-radius:4px;background:#ede9fe;color:#4f46e5}}
</style>
</head>
<body data-iou-thresh="{iou_thresh}" data-matched-leaves="{matched_leaves}" data-section-count="{sec_count}" data-unmatched-orig-sections="{unmatched_orig_secs}" data-unmatched-gen-sections="{unmatched_gen_secs}">
<div class="hdr">
  <h1>Conversion Report &ndash; {_esc(matching_type.title())} Matching</h1>
  <div class="scores">
    <div class="sc"><div class="v" id="headerLeafIou">{avg_leaf_iou:.4f}</div><div class="l">Leaf IoU</div></div>
    <div class="sc"><div class="v" id="headerSectionIou">{avg_section_iou:.4f}</div><div class="l">Section IoU</div></div>
    <div class="sc"><div class="v">{len(leaf_diffs)}</div><div class="l">Leaf Pairs</div></div>
    <div class="sc"><div class="v">{len(section_diffs)}</div><div class="l">Section Pairs</div></div>
    {extra_stats_html}
    {vlm_css_similarity_html}
    <button class="recompute-btn" id="recomputeIouBtn" title="Recompute IoU scores from current HTML">Recompute IoU</button>
  </div>
</div>

<div class="wrap">
  <div class="col">
    <div class="col-hdr">Original HTML</div>
    <div class="panel-tabs">
      <button class="panel-tab" data-panel="orig" data-view="source">Source</button>
      <button class="panel-tab active" data-panel="orig" data-view="preview">HTML Preview</button>
    </div>
    <div class="panel-content" id="orig-source">
      <div class="toolbar">
        <input type="file" id="origFileInput" accept=".html,.htm" style="display:none">
        <button class="toolbar-btn" id="origLoad" title="Load File">&#128194;</button>
        <button class="toolbar-btn" id="origCopy" title="Copy">&#128203;</button>
        <button class="toolbar-btn" id="origDownload" title="Download">&#128190;</button>
      </div>
      <div class="code-editor-wrap"><textarea id="origCode"></textarea></div>
    </div>
    <div class="panel-content active" id="orig-preview">
      <div class="toolbar">
        <button class="toolbar-btn" id="origDownloadPrev" title="Download">&#128190;</button>
      </div>
      <div class="preview-wrap" id="origPreviewWrap">
        <div class="iframe-zoom-wrap" id="origZoomWrap">
          <iframe id="origPreview" sandbox="allow-same-origin"></iframe>
        </div>
      </div>
    </div>
  </div>

  <div class="col">
    <div class="col-hdr">Converted HTML</div>
    <div class="panel-tabs">
      <button class="panel-tab" data-panel="gen" data-view="source">Source</button>
      <button class="panel-tab active" data-panel="gen" data-view="preview">HTML Preview</button>
    </div>
    <div class="panel-content" id="gen-source">
      <div class="toolbar">
        <button class="toolbar-btn" id="genCopy" title="Copy">&#128203;</button>
        <button class="toolbar-btn" id="genDownload" title="Download">&#128190;</button>
      </div>
      <div class="code-editor-wrap"><textarea id="genCode"></textarea></div>
    </div>
    <div class="panel-content active" id="gen-preview">
      <div class="toolbar">
        <button class="toolbar-btn" id="genDownloadPrev" title="Download">&#128190;</button>
      </div>
      <div class="preview-wrap" id="genPreviewWrap">
        <div class="iframe-zoom-wrap" id="genZoomWrap">
          <iframe id="genPreview" sandbox="allow-same-origin"></iframe>
        </div>
      </div>
    </div>
  </div>

  <div class="col">
    <div class="tab-bar">
      <button class="tab-btn active" data-tab="sections">Sections ({len(section_diffs)})</button>
      <button class="tab-btn" data-tab="leaves">Leaves ({len(leaf_diffs)})</button>
      <button class="tab-btn" data-tab="unmatched">Unmatched ({n_unmatched})</button>
    </div>
    <div class="tab-content active" id="tab-sections"><div class="diff-list">{section_items_html}</div></div>
    <div class="tab-content" id="tab-leaves"><div class="diff-list">{leaf_items_html}</div></div>
    <div class="tab-content" id="tab-unmatched"><div class="diff-list">{unmatched_items_html}</div></div>
  </div>
</div>

<script>
(function() {{
  const origHtml = {orig_html_js};
  const genHtml = {gen_html_js};

  const origCodeEl = document.getElementById('origCode');
  const genCodeEl = document.getElementById('genCode');
  const origPreview = document.getElementById('origPreview');
  const genPreview = document.getElementById('genPreview');
  const origPreviewWrap = document.getElementById('origPreviewWrap');
  const genPreviewWrap = document.getElementById('genPreviewWrap');
  const origZoomWrap = document.getElementById('origZoomWrap');
  const genZoomWrap = document.getElementById('genZoomWrap');
  const HIGHLIGHT_CLASS = 'conv-report-bbox-highlight';
  const IFRAME_VIEWPORT_WIDTH = 1280;

  function updateIframeZoom() {{
    [origPreviewWrap, genPreviewWrap].forEach(function(wrap) {{
      var zoomWrap = wrap.querySelector('.iframe-zoom-wrap');
      if (!zoomWrap) return;
      var w = wrap.clientWidth || 0;
      var scale = w > 0 ? Math.min(1, w / IFRAME_VIEWPORT_WIDTH) : 0.3;
      zoomWrap.style.transform = 'scale(' + scale + ')';
      var iframe = zoomWrap.querySelector('iframe');
      var iframeHeight = 3000;
      if (iframe) {{
        try {{
          var doc = iframe.contentDocument;
          if (doc && doc.documentElement) {{
            // Read actual content height and explicitly set iframe element height
            // so it doesn't clip content at the min-height boundary.
            iframeHeight = Math.max(doc.documentElement.scrollHeight, doc.body ? doc.body.scrollHeight : 0, 3000);
            iframe.style.height = iframeHeight + 'px';
          }}
        }} catch (e) {{}}
      }}
      zoomWrap.style.height = Math.ceil(iframeHeight * scale) + 'px';
    }});
  }}

  updateIframeZoom();
  window.addEventListener('resize', updateIframeZoom);
  if (typeof ResizeObserver !== 'undefined') {{
    [origPreviewWrap, genPreviewWrap].forEach(function(wrap) {{
      new ResizeObserver(updateIframeZoom).observe(wrap);
    }});
  }}
  origPreview.addEventListener('load', updateIframeZoom);
  genPreview.addEventListener('load', updateIframeZoom);

  var origEditor = CodeMirror.fromTextArea(origCodeEl, {{
    mode: 'xml',
    lineNumbers: true,
    lineWrapping: true,
    indentUnit: 2,
    theme: 'default',
    gutter: true,
  }});
  var genEditor = CodeMirror.fromTextArea(genCodeEl, {{
    mode: 'xml',
    lineNumbers: true,
    lineWrapping: true,
    indentUnit: 2,
    theme: 'default',
    gutter: true,
  }});

  origEditor.setValue(origHtml);
  genEditor.setValue(genHtml);

  function getEditor(which) {{ return which === 'orig' ? origEditor : genEditor; }}

  function updatePreview(which) {{
    var code = getEditor(which).getValue();
    var iframe = which === 'orig' ? origPreview : genPreview;
    iframe.srcdoc = code;
  }}

  var debounceTimers = {{}};
  function debounceUpdate(which) {{
    clearTimeout(debounceTimers[which]);
    debounceTimers[which] = setTimeout(function() {{ updatePreview(which); }}, 300);
  }}

  origEditor.on('change', function() {{ debounceUpdate('orig'); }});
  genEditor.on('change', function() {{ debounceUpdate('gen'); }});

  document.getElementById('recomputeIouBtn').addEventListener('click', recomputeAllIous);

  function getBboxFromIframe(iframe, xpathsJson) {{
    try {{
      var doc = iframe.contentDocument;
      if (!doc || !doc.body) return null;
      var xpaths = JSON.parse(xpathsJson);
      if (!xpaths || xpaths.length === 0) return null;
      var allNodes = [];
      for (var i = 0; i < xpaths.length; i++) {{
        var nodes = getElementsByXpath(doc, xpaths[i]);
        allNodes.push.apply(allNodes, nodes);
      }}
      return getUnionRect(allNodes);
    }} catch (e) {{ return null; }}
  }}

  function bboxIou(a, b) {{
    var l1 = a.left !== undefined ? a.left : a.x;
    var t1 = a.top !== undefined ? a.top : a.y;
    var l2 = b.left !== undefined ? b.left : b.x;
    var t2 = b.top !== undefined ? b.top : b.y;
    if (!a || !b) return null;
    var ix1 = Math.max(l1, l2);
    var iy1 = Math.max(t1, t2);
    var ix2 = Math.min(l1 + a.width, l2 + b.width);
    var iy2 = Math.min(t1 + a.height, t2 + b.height);
    if (ix2 <= ix1 || iy2 <= iy1) return 0;
    var inter = (ix2 - ix1) * (iy2 - iy1);
    var areaA = a.width * a.height;
    var areaB = b.width * b.height;
    var union = areaA + areaB - inter;
    return union > 0 ? inter / union : 0;
  }}

  function toRel(bbox, secBbox, sw, sh) {{
    return {{
      left: (bbox.left - secBbox.left) / sw,
      top: (bbox.top - secBbox.top) / sh,
      width: bbox.width / sw,
      height: bbox.height / sh
    }};
  }}

  function bboxIouSectionOnly(a, b) {{
    if (!a || !b || a.width <= 0 || a.height <= 0 || b.width <= 0 || b.height <= 0) return null;
    var ra = {{ left: 0, top: 0, width: a.width, height: a.height }};
    var rb = {{ left: 0, top: 0, width: b.width, height: b.height }};
    return bboxIou(ra, rb);
  }}

  function computeLeafIousWithAdjustment(leafItems, rectOrigFn, rectGenFn, origSecFn, genSecFn) {{
    var sectionGroups = {{}};
    leafItems.forEach(function(item) {{
      var secKey = (item.getAttribute('data-orig-sec-xpaths') || '') + '||' + (item.getAttribute('data-gen-sec-xpaths') || '');
      if (!sectionGroups[secKey]) sectionGroups[secKey] = [];
      sectionGroups[secKey].push(item);
    }});
    var resultMap = new WeakMap();
    for (var sk in sectionGroups) {{
      var group = sectionGroups[sk];
      var entries = [];
      group.forEach(function(item) {{
        var origXpaths = item.getAttribute('data-orig-xpaths');
        var genXpaths = item.getAttribute('data-gen-xpaths');
        if (!origXpaths || !genXpaths) return;
        var rectOrig = rectOrigFn(item);
        var rectGen = rectGenFn(item);
        var origSec = origSecFn(item);
        var genSec = genSecFn(item);
        if (!rectOrig || !rectGen || !origSec || !genSec) return;
        var osw = origSec.width || 1, osh = origSec.height || 1;
        var gsw = genSec.width || 1, gsh = genSec.height || 1;
        var origRel = toRel(rectOrig, origSec, osw, osh);
        var genRel = toRel(rectGen, genSec, gsw, gsh);
        entries.push({{ item: item, origRel: origRel, genRel: genRel }});
      }});
      for (var i = 0; i < entries.length; i++) {{
        var or = entries[i].origRel;
        var gr = entries[i].genRel;
        var iou = bboxIou(or, gr);
        resultMap.set(entries[i].item, iou);
        // Cumulative coordinate adjustment disabled — raw relative bboxes used directly.
        // var heightDiff = or.height - gr.height;
        // var widthDiff = or.width - gr.width;
        // var yDiff = or.top - gr.top;
        // var xDiff = or.left - gr.left;
        // var grBottom = gr.top + gr.height;
        // var grRight = gr.left + gr.width;
        // for (var j = i + 1; j < entries.length; j++) {{
        //   var fut = adjustedGen[j];
        //   var hOverlap = fut.left < gr.left + gr.width && fut.left + fut.width > gr.left;
        //   var vOverlap = fut.top < gr.top + gr.height && fut.top + fut.height > gr.top;
        //   if (hOverlap && fut.top + fut.height > grBottom) fut.top += (heightDiff + yDiff);
        //   if (vOverlap && fut.left + fut.width > grRight) fut.left += (widthDiff + xDiff);
        // }}
      }}
    }}
    return resultMap;
  }}

  function setIouBadgeClass(item, iou) {{
    item.classList.remove('low','mid','high','na');
    if (iou === null || iou === undefined) {{ item.classList.add('na'); return; }}
    if (iou < 0.3) item.classList.add('low');
    else if (iou < 0.7) item.classList.add('mid');
    else item.classList.add('high');
  }}

  function getOrigSecForItem(item) {{
    var xpaths = item.getAttribute('data-orig-sec-xpaths');
    if (xpaths) return getBboxFromIframe(origPreview, xpaths);
    var ox = item.getAttribute('data-orig-sec-ox');
    if (ox !== null) return {{ left: parseFloat(item.getAttribute('data-orig-sec-ox')), top: parseFloat(item.getAttribute('data-orig-sec-oy')), width: parseFloat(item.getAttribute('data-orig-sec-ow')), height: parseFloat(item.getAttribute('data-orig-sec-oh')) }};
    return null;
  }}
  function getGenSecForItem(item) {{
    var xpaths = item.getAttribute('data-gen-sec-xpaths');
    if (xpaths) return getBboxFromIframe(genPreview, xpaths);
    var gx = item.getAttribute('data-gen-sec-gx');
    if (gx !== null) return {{ left: parseFloat(item.getAttribute('data-gen-sec-gx')), top: parseFloat(item.getAttribute('data-gen-sec-gy')), width: parseFloat(item.getAttribute('data-gen-sec-gw')), height: parseFloat(item.getAttribute('data-gen-sec-gh')) }};
    return null;
  }}

  function _doRecomputeIous() {{
      var items = document.querySelectorAll('.diff-item');
      var leafIous = [];
      var sectionIous = [];
      var leafItems = [];
      items.forEach(function(item) {{
        if (item.classList.contains('sec-item')) leafItems.push(null);
        else leafItems.push(item);
      }});
      leafItems = leafItems.filter(function(i) {{ return i !== null; }});
      var leafItemsWithBadge = leafItems.filter(function(i) {{ return i.querySelector('.iou-badge'); }});
      var leafIouMap = {{}};
      if (leafItemsWithBadge.length > 0) {{
        function rectOrigFn(item) {{ return getBboxFromIframe(origPreview, item.getAttribute('data-orig-xpaths')); }}
        function rectGenFn(item) {{ return getBboxFromIframe(genPreview, item.getAttribute('data-gen-xpaths')); }}
        leafIouMap = computeLeafIousWithAdjustment(leafItemsWithBadge, rectOrigFn, rectGenFn, getOrigSecForItem, getGenSecForItem);
      }}
      items.forEach(function(item) {{
        var origXpaths = item.getAttribute('data-orig-xpaths');
        var genXpaths = item.getAttribute('data-gen-xpaths');
        var badge = item.querySelector('.iou-badge');
        if (!origXpaths || !genXpaths || !badge) return;
        var iou = null;
        if (item.classList.contains('sec-item')) {{
          var rectOrig = getBboxFromIframe(origPreview, origXpaths);
          var rectGen = getBboxFromIframe(genPreview, genXpaths);
          iou = bboxIouSectionOnly(rectOrig, rectGen);
          if (iou !== null) sectionIous.push(iou);
        }} else {{
          // Use Python-computed IoU embedded in data-python-iou (authoritative)
          var pyIou = item.getAttribute('data-python-iou');
          iou = pyIou !== null ? parseFloat(pyIou) : null;
          if (iou !== null) leafIous.push(iou);
        }}
        if (iou !== null) {{
          badge.textContent = iou.toFixed(4);
        }} else {{
          badge.textContent = '-';
        }}
        setIouBadgeClass(item, iou);
      }});
      var leafAvg = leafIous.length ? leafIous.reduce(function(a,b){{return a+b}},0) / leafIous.length : 0;
      var sectionAvg = sectionIous.length ? sectionIous.reduce(function(a,b){{return a+b}},0) / sectionIous.length : 0;
      var leafEl = document.getElementById('headerLeafIou');
      var sectionEl = document.getElementById('headerSectionIou');
      if (leafEl) leafEl.textContent = leafAvg.toFixed(4);
      if (sectionEl) sectionEl.textContent = sectionAvg.toFixed(4);
      var thresh = parseFloat(document.body.dataset.iouThresh || '0.95');
      var leafAbove = leafIous.filter(function(v){{ return v >= thresh; }}).length;
      var secAbove = sectionIous.filter(function(v){{ return v >= thresh; }}).length;
      var leafPct = leafIous.length ? (100 * leafAbove / leafIous.length) : 0;
      var secPct = sectionIous.length ? (100 * secAbove / sectionIous.length) : 0;
      var leafPctEl = document.getElementById('leafAbovePct');
      var leafLabelEl = document.getElementById('leafAboveLabel');
      var secPctEl = document.getElementById('secAbovePct');
      var secLabelEl = document.getElementById('secAboveLabel');
      if (leafPctEl) leafPctEl.textContent = leafPct.toFixed(1) + '%';
      if (leafLabelEl) leafLabelEl.textContent = 'Leaves >= ' + thresh + ' IoU (' + leafAbove + '/' + leafIous.length + ')';
      if (secPctEl) secPctEl.textContent = secPct.toFixed(1) + '%';
      if (secLabelEl) secLabelEl.textContent = 'Sections >= ' + thresh + ' IoU (' + secAbove + ')';
      try {{ window.dispatchEvent(new CustomEvent('iouRecomputeDone')); }} catch (e) {{}}
  }}

  function _iframeReady(iframe) {{
    try {{
      var doc = iframe.contentDocument;
      return doc && doc.body && doc.body.children.length > 0;
    }} catch (e) {{ return false; }}
  }}

  function _waitForIframesAndRun(fn) {{
    function poll() {{
      if (_iframeReady(origPreview) && _iframeReady(genPreview)) {{
        fn();
      }} else {{
        setTimeout(poll, 100);
      }}
    }}
    poll();
  }}

  function recomputeAllIous() {{
    updatePreview('orig');
    updatePreview('gen');
    _waitForIframesAndRun(_doRecomputeIous);
  }}

  function getElementsByXpath(doc, xpath) {{
    try {{
      const result = doc.evaluate(xpath, doc, null, XPathResult.ORDERED_NODE_ITERATOR_TYPE, null);
      const nodes = [];
      let n;
      while ((n = result.iterateNext())) nodes.push(n);
      return nodes;
    }} catch (e) {{ return []; }}
  }}

  function getUnionRect(nodes) {{
    if (!nodes || nodes.length === 0) return null;
    const rects = nodes.map(function(n) {{ return n.getBoundingClientRect(); }});
    const vp = nodes[0].ownerDocument.defaultView;
    let minX = rects[0].left + vp.scrollX, minY = rects[0].top + vp.scrollY;
    let maxX = rects[0].right + vp.scrollX, maxY = rects[0].bottom + vp.scrollY;
    for (let i = 1; i < rects.length; i++) {{
      minX = Math.min(minX, rects[i].left + vp.scrollX);
      minY = Math.min(minY, rects[i].top + vp.scrollY);
      maxX = Math.max(maxX, rects[i].right + vp.scrollX);
      maxY = Math.max(maxY, rects[i].bottom + vp.scrollY);
    }}
    return {{ left: minX, top: minY, width: maxX - minX, height: maxY - minY }};
  }}

  function injectHighlight(iframe, xpathsJson, borderColor, wrap) {{
    var doc = iframe.contentDocument;
    var win = iframe.contentWindow;
    if (!doc || !doc.body || !win) return;
    doc.querySelectorAll('.' + HIGHLIGHT_CLASS).forEach(function(el) {{ el.remove(); }});
    try {{
      doc.body.style.position = doc.body.style.position || 'relative';
      var xpaths = JSON.parse(xpathsJson);
      if (!xpaths || xpaths.length === 0) return;
      var allNodes = [];
      for (var i = 0; i < xpaths.length; i++) {{
        var nodes = getElementsByXpath(doc, xpaths[i]);
        allNodes.push.apply(allNodes, nodes);
      }}
      var rect = getUnionRect(allNodes);
      if (!rect) return;
      var bodyRect = doc.body.getBoundingClientRect();
      var scrollX = win.scrollX || doc.documentElement.scrollLeft || 0;
      var scrollY = win.scrollY || doc.documentElement.scrollTop || 0;
      var bodyTop = bodyRect.top + scrollY;
      var bodyLeft = bodyRect.left + scrollX;
      var adjLeft = rect.left - bodyLeft;
      var adjTop = rect.top - bodyTop;
      var x = Math.round(rect.left);
      var y = Math.round(rect.top);
      var w = Math.round(rect.width);
      var h = Math.round(rect.height);
      var label = doc.createElement('div');
      label.className = HIGHLIGHT_CLASS;
      label.textContent = '(' + x + ', ' + y + ', ' + w + ', ' + h + ')';
      label.style.cssText = 'position:absolute;left:' + adjLeft + 'px;top:' + Math.max(0, adjTop - 20) + 'px;font:11px/1.2 -apple-system,BlinkMacSystemFont,sans-serif;color:#fff;background:' + borderColor + ';padding:2px 6px;border-radius:3px;pointer-events:none;z-index:1000000;white-space:nowrap';
      doc.body.appendChild(label);
      var div = doc.createElement('div');
      div.className = HIGHLIGHT_CLASS;
      var bg = borderColor === '#dc2626' ? 'rgba(220,38,38,.12)' : 'rgba(37,99,235,.12)';
      div.style.cssText = 'position:absolute;left:' + adjLeft + 'px;top:' + adjTop + 'px;width:' + rect.width + 'px;height:' + rect.height + 'px;border:3px solid ' + borderColor + ';background:' + bg + ';pointer-events:none;z-index:999999;border-radius:2px;box-sizing:border-box';
      doc.body.appendChild(div);
      var zoomWrap = wrap.querySelector('.iframe-zoom-wrap');
      var scale = zoomWrap ? (parseFloat(zoomWrap.style.transform.replace('scale(', '')) || 1) : 1;
      var scaledTop = rect.top * scale;
      var viewportH = wrap.clientHeight;
      var scrollTop = Math.max(0, scaledTop - Math.floor(viewportH / 3));
      wrap.scrollTo({{ top: scrollTop, left: 0, behavior: 'smooth' }});
    }} catch (e) {{}}
  }}

  function removeHighlights(iframe) {{
    try {{
      const doc = iframe.contentDocument;
      if (doc) doc.querySelectorAll('.' + HIGHLIGHT_CLASS).forEach(function(el) {{ el.remove(); }});
    }} catch (e) {{}}
  }}

  window.showBoxes = function(el) {{
    const origXpaths = el.getAttribute('data-orig-xpaths');
    const genXpaths = el.getAttribute('data-gen-xpaths');
    if (!origXpaths || !genXpaths) return;
    document.querySelectorAll('[data-panel="orig"]').forEach(function(b) {{ b.classList.remove('active'); }});
    document.querySelectorAll('#orig-source,#orig-preview').forEach(function(c) {{ c.classList.remove('active'); }});
    document.querySelector('[data-panel="orig"][data-view="preview"]').classList.add('active');
    document.getElementById('orig-preview').classList.add('active');
    document.querySelectorAll('[data-panel="gen"]').forEach(function(b) {{ b.classList.remove('active'); }});
    document.querySelectorAll('#gen-source,#gen-preview').forEach(function(c) {{ c.classList.remove('active'); }});
    document.querySelector('[data-panel="gen"][data-view="preview"]').classList.add('active');
    document.getElementById('gen-preview').classList.add('active');
    function doHighlight() {{
      injectHighlight(origPreview, origXpaths, '#dc2626', origPreviewWrap);
      injectHighlight(genPreview, genXpaths, '#2563eb', genPreviewWrap);
    }}
    if (_iframeReady(origPreview) && _iframeReady(genPreview)) {{
      doHighlight();
    }} else {{
      updatePreview('orig');
      updatePreview('gen');
      _waitForIframesAndRun(doHighlight);
    }}
  }};

  window.hideBoxes = function() {{
    removeHighlights(origPreview);
    removeHighlights(genPreview);
  }};

  document.querySelectorAll('.panel-tab').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var panel = btn.dataset.panel;
      var view = btn.dataset.view;
      document.querySelectorAll('[data-panel="' + panel + '"]').forEach(function(b) {{ b.classList.remove('active'); }});
      ['source','preview'].forEach(function(v) {{
        var c = document.getElementById(panel + '-' + v);
        if (c) c.classList.remove('active');
      }});
      document.getElementById(panel + '-' + view).classList.add('active');
      btn.classList.add('active');
      if (view === 'preview') {{
        updatePreview(panel);
        setTimeout(updateIframeZoom, 50);
      }}
      else {{ setTimeout(function() {{ getEditor(panel).refresh(); }}, 50); }}
    }});
  }});

  function downloadHtml(which) {{
    var code = getEditor(which).getValue();
    var name = which === 'orig' ? 'original.html' : 'converted.html';
    var blob = new Blob([code], {{ type: 'text/html;charset=utf-8' }});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = name;
    a.click();
    URL.revokeObjectURL(a.href);
  }}

  document.getElementById('origDownload').addEventListener('click', function() {{ downloadHtml('orig'); }});
  document.getElementById('origDownloadPrev').addEventListener('click', function() {{ downloadHtml('orig'); }});
  document.getElementById('genDownload').addEventListener('click', function() {{ downloadHtml('gen'); }});
  document.getElementById('genDownloadPrev').addEventListener('click', function() {{ downloadHtml('gen'); }});

  document.getElementById('origCopy').addEventListener('click', function() {{
    var t = document.createElement('textarea');
    t.value = origEditor.getValue();
    document.body.appendChild(t);
    t.select();
    document.execCommand('copy');
    document.body.removeChild(t);
  }});
  document.getElementById('genCopy').addEventListener('click', function() {{
    var t = document.createElement('textarea');
    t.value = genEditor.getValue();
    document.body.appendChild(t);
    t.select();
    document.execCommand('copy');
    document.body.removeChild(t);
  }});

  document.getElementById('origLoad').addEventListener('click', function() {{
    document.getElementById('origFileInput').click();
  }});
  document.getElementById('origFileInput').addEventListener('change', function(e) {{
    var f = e.target.files[0];
    if (!f) return;
    var r = new FileReader();
    r.onload = function() {{ origEditor.setValue(r.result); updatePreview('orig'); }};
    r.readAsText(f);
    e.target.value = '';
  }});

  updatePreview('orig');
  updatePreview('gen');
  origEditor.refresh();
  genEditor.refresh();

  document.querySelectorAll('.tab-btn').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      document.querySelectorAll('.tab-btn').forEach(function(b) {{ b.classList.remove('active'); }});
      document.querySelectorAll('.tab-content').forEach(function(c) {{ c.classList.remove('active'); }});
      btn.classList.add('active');
      document.getElementById('tab-' + btn.dataset.tab).classList.add('active');
      window.hideBoxes();
    }});
  }});
}})();
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(report_html)

    # Load report in browser, trigger Recompute IoU, and overwrite with browser-rendered values
    _apply_recompute_ious_and_save(output_path)

    logger.info("Report saved to %s", output_path)
    return


def _apply_recompute_ious_and_save(report_path: str) -> None:
    """Load the report in Playwright, trigger Recompute IoU, and overwrite the file
    with the updated HTML containing browser-rendered IoU values."""
    from playwright.sync_api import sync_playwright

    report_path = os.path.abspath(report_path)
    file_url = Path(report_path).as_uri()

    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        context = new_context(browser)
        page = context.new_page()
        try:
            goto_and_settle(page, file_url)
            page.wait_for_selector("#recomputeIouBtn", state="visible", timeout=10000)

            # Set up listener for recompute-done event before triggering
            page.evaluate(
                """
                window._iouRecomputeDone = false;
                window.addEventListener('iouRecomputeDone', function() {
                    window._iouRecomputeDone = true;
                }, {once: true});
            """
            )
            page.click("#recomputeIouBtn")
            page.wait_for_function(
                "window._iouRecomputeDone === true", timeout=15000
            )

            updated_html = page.content()
            with open(report_path, "w", encoding="utf-8") as fh:
                fh.write(updated_html)
            logger.info("Report updated with recomputed IoU values (browser-rendered)")
        except Exception as e:
            raise RuntimeError(f"could not apply Recompute IoU before save: {e}") from e
        finally:
            browser.close()


# ═══════════════════════════════════════════════════════════════════════════
#  CLI & main pipeline
# ═══════════════════════════════════════════════════════════════════════════

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a conversion visualization report comparing the "
                    "original HTML with the generated HTML.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        help="Output directory produced by dom_utils.py "
             "(contains original_analysis/, output.html, etc.).",
        default="/mnt/localssd/parul/image-critic-data/25_OPTUM_FF_EM_2025_WF14791319_First Fill Actions"
    )
    parser.add_argument(
        "--matching-types",
        nargs="+",
        choices=["heuristic", "embedding", "vlm"],
        default=["heuristic", "embedding", "vlm"],
        help="Which matching types to run (default: all three).",
    )
    parser.add_argument(
        "--thresh-height",
        type=int,
        default=2900,
        help="Threshold height for section detection (default: 2900).",
    )
    parser.add_argument(
        "--max-leaves",
        type=int,
        default=None,
        help="Maximum number of leaves for section detection (default: None).",
    )
    parser.add_argument(
        "--original-html-file-path",
        type=str,
        default=None,
        help="Path to original HTML file. Used when output-dir/original_analysis/original.html "
             "does not exist; creates original_analysis and copies the file there.",
    )
    parser.add_argument(
        "--generated-html-file-path",
        type=str,
        default=None,
        help="Path to generated HTML file. Used when output-dir/output.html does not exist; "
             "copies the file to output-dir/output.html so generated_analysis can be prepared.",
    )
    parser.add_argument(
        "--original-url",
        type=str,
        default=None,
        help="Public URL of the original HTML page. Playwright will fetch and fully render "
             "it, then save the result as original_analysis/original.html. Takes precedence "
             "over --original-html-file-path.",
    )
    parser.add_argument(
        "--generated-url",
        type=str,
        default=None,
        help="Public URL of the generated HTML page. Playwright will fetch and fully render "
             "it, then save the result as output.html. Takes precedence over "
             "--generated-html-file-path.",
    )
    parser.add_argument(
        "--run-llm-diffs",
        action="store_true",
        default=False,
        help="Run LLM-based diff generation on matched nodes (populates the 'LLM Diffs' tab).",
    )
    parser.add_argument(
        "--ai-provider",
        type=str,
        default="gpt41",
        help="AI provider for LLM diff generation (default: gpt41).",
    )
    parser.add_argument(
        "--use-fragment-sectioning",
        action="store_true",
        default=False,
        help="Use fragment approach based section detection (default: False).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir

    # ── Validate paths; use optional file paths if standard locations missing ──
    orig_analysis_dir = os.path.join(output_dir, "original_analysis")
    orig_html_path = os.path.join(orig_analysis_dir, "original.html")
    gen_html_path = os.path.join(output_dir, "output.html")

    # ── Fetch from URLs if provided (takes precedence over file path args) ──
    os.makedirs(orig_analysis_dir, exist_ok=True)
    os.makedirs(output_dir, exist_ok=True)
    if args.original_url:
        fetch_url_as_html(args.original_url, orig_html_path)
    if args.generated_url:
        fetch_url_as_html(args.generated_url, gen_html_path)

    orig_analysis_needed = False
    if not os.path.exists(orig_html_path):
        if args.original_html_file_path and os.path.exists(args.original_html_file_path):
            os.makedirs(orig_analysis_dir, exist_ok=True)
            shutil.copy2(args.original_html_file_path, orig_html_path)
            logger.info("Copied %s -> %s", args.original_html_file_path, orig_html_path)
            orig_analysis_needed = True
        else:
            raise FileNotFoundError(
                f"Original HTML not found: {orig_html_path}. "
                "Provide --original-html-file-path or --original-url."
            )
    elif len(os.listdir(orig_analysis_dir)) <= 3:
        orig_analysis_needed = True

    if not os.path.exists(gen_html_path):
        if args.generated_html_file_path and os.path.exists(args.generated_html_file_path):
            shutil.copy2(args.generated_html_file_path, gen_html_path)
            logger.info("Copied %s -> %s", args.generated_html_file_path, gen_html_path)
        else:
            raise FileNotFoundError(
                f"Generated HTML not found: {gen_html_path}. "
                "Provide --generated-html-file-path or --generated-url."
            )

    # ── Step 1: prepare analysis (screenshots & bboxes) ────────────────────
    gen_analysis_dir = os.path.join(output_dir, "generated_analysis")
    if orig_analysis_needed:
        prepare_html_analysis(orig_html_path, orig_analysis_dir, "original")
    if not os.path.isdir(gen_analysis_dir) or len(os.listdir(gen_analysis_dir)) <= 3:
        prepare_html_analysis(gen_html_path, gen_analysis_dir, "generated")
    else:
        logger.info("Generated analysis directory already populated - skipping capture.")

    # ── Step 2: full-page screenshots (base64) ───────────────────────────
    # When URLs were provided use them directly so Playwright loads the live
    # page (with all remote resources) rather than the saved HTML snapshot.
    logger.info("Taking full-page screenshots for the report ...")
    orig_b64, orig_dims = take_full_page_screenshot_b64(args.original_url or orig_html_path)
    gen_b64, gen_dims = take_full_page_screenshot_b64(args.generated_url or gen_html_path)

    # Save screenshots to disk so VLM matching later can reference them by path.
    # Each goes into its own subdirectory with a page_dimensions.json so
    # create_stacked_*_visualization can compute the correct scale factor.
    vlm_workspace = os.path.join(output_dir, "vlm_workspace")
    os.makedirs(vlm_workspace, exist_ok=True)

    orig_screenshot_dir = os.path.join(vlm_workspace, "orig_screenshot")
    os.makedirs(orig_screenshot_dir, exist_ok=True)
    orig_screenshot_path = os.path.join(orig_screenshot_dir, "full_page.png")
    with open(orig_screenshot_path, "wb") as fh:
        fh.write(base64.b64decode(orig_b64))
    with open(os.path.join(orig_screenshot_dir, "page_dimensions.json"), "w") as fh:
        json.dump(orig_dims, fh)

    gen_screenshot_dir = os.path.join(vlm_workspace, "gen_screenshot")
    os.makedirs(gen_screenshot_dir, exist_ok=True)
    gen_screenshot_path = os.path.join(gen_screenshot_dir, "full_page.png")
    with open(gen_screenshot_path, "wb") as fh:
        fh.write(base64.b64decode(gen_b64))
    with open(os.path.join(gen_screenshot_dir, "page_dimensions.json"), "w") as fh:
        json.dump(gen_dims, fh)

    # Load page dimensions captured during element analysis.
    def _load_page_dims(analysis_dir: str) -> Tuple[float, float]:
        pd_path = os.path.join(analysis_dir, "page_dimensions.json")
        if os.path.exists(pd_path):
            with open(pd_path) as fh:
                pd = json.load(fh)
            if "pageWidth" in pd and "pageHeight" in pd:
                return pd["pageWidth"], pd["pageHeight"]
        raise RuntimeError(f"page dimensions missing or invalid: {pd_path}")

    orig_pw, orig_ph = _load_page_dims(orig_analysis_dir)
    gen_pw, gen_ph = _load_page_dims(gen_analysis_dir)
    
    # ── Step 3: build visual trees ────────────────────────────────────────
    logger.info("Building original template's visual tree ...")
    with open(orig_html_path, "r", encoding="utf-8") as fh:
        orig_html = fh.read()

    orig_nodes, _ = build_visual_tree(orig_analysis_dir, orig_html)

    logger.info("Detecting original template's section nodes ...")
    if args.use_fragment_sectioning:
        logger.info("Detecting original template's section nodes using fragment approach ...")
        orig_section_nodes, _ = _get_section_nodes_v2(
            orig_nodes, orig_pw, orig_ph, thresh_height=args.thresh_height
        )
    else:
        orig_section_nodes, _ = get_section_nodes(
            orig_nodes, orig_pw, orig_ph, thresh_height=args.thresh_height, max_leaves=args.max_leaves,
        )
    with open(gen_html_path, "r", encoding="utf-8") as fh:
        gen_html = fh.read()

    # Load section matching from JSON if available; otherwise build gen first and use heuristic
    matched_sections_from_json = load_section_matching_from_json(
        output_dir, orig_section_nodes
    )

    if matched_sections_from_json is not None:
        matched_gen_section_nodes = [
            (_get_union_bbox([orig_section_nodes[j]["bbox"] for j in matched_sections_from_json[i][0]])["width"],
            matched_sections_from_json[i][1])
            for i in range(len(matched_sections_from_json))
        ]
        matched_gen_section_node_xpaths = {
            item: orig_bbox_width
            for orig_bbox_width, xpath_list in matched_gen_section_nodes
            for item in xpath_list
        }
        logger.info("Section matching loaded from section_structure_xpaths.json")
    else:
        matched_gen_section_node_xpaths = None
        logger.info(
            "section_structure_xpaths.json not found - building gen tree without "
            "section filtering; each matching type will use its own section matcher."
        )

    logger.info("Building generated template's visual tree ...")
    gen_nodes, _ = build_visual_tree(gen_analysis_dir, gen_html, matched_gen_section_node_xpaths)

    # ── Step 4: section detection ─────────────────────────────────────────
    logger.info("Detecting generated template's section nodes ...")
    if args.use_fragment_sectioning:
        logger.info("Detecting generated template's section nodes using fragment approach ...")
        gen_section_nodes, _ = _get_section_nodes_v2(
            gen_nodes, gen_pw, gen_ph, thresh_height=args.thresh_height,
        )
    else:
        gen_section_nodes, _ = get_section_nodes(
            gen_nodes, gen_pw, gen_ph, thresh_height=args.thresh_height, max_leaves=args.max_leaves,
        )
    logger.info(
        "Sections: %d original, %d generated",
        len(orig_section_nodes), len(gen_section_nodes),
    )

    # ── Step 5: matching + report for each requested type ─────────────────
    image_name = Path(output_dir).stem
    for mt in args.matching_types:
        logger.info("═" * 60)
        logger.info("Running %s matching ...", mt.upper())
        logger.info("═" * 60)

        leaf_diffs, section_diffs, avg_leaf, avg_sec, extra_stats = run_matching(
            mt, orig_nodes, gen_nodes,
            orig_section_nodes, gen_section_nodes,
            orig_analysis_dir, gen_analysis_dir,
            output_dir,
            orig_screenshot_path, gen_screenshot_path,
        )

        logger.info(
            "%s - Leaf IoU: %.4f | Section IoU: %.4f | Leaf Pairs: %d | Section Pairs: %d"
            " | Leaves>=%.2f: %d/%d (%.1f%%) | Sections>=%.2f: %d (%.1f%%)",
            mt.upper(), avg_leaf, avg_sec, len(leaf_diffs), len(section_diffs),
            extra_stats["iou_threshold"],
            extra_stats["leaf_above_thresh"], extra_stats["matched_leaves"],
            extra_stats["leaf_above_pct"],
            extra_stats["iou_threshold"],
            extra_stats["sec_above_thresh"], extra_stats["sec_above_pct"],
        )

        # ── Step 5b (optional): LLM-based diff generation ────────────────
        verified_diffs_pretty = None
        if args.run_llm_diffs:
            logger.info("Running LLM-based diff generation for %s matching ...", mt.upper())
            try:
                import dotenv
                dotenv.load_dotenv(Path(__file__).resolve().parent.parent / ".env")
            except Exception:
                pass
            section_matches = [(sd["orig"], sd["gen"]) for sd in section_diffs]
            verified_diffs_pretty = asyncio.run(
                process_dom_tree_for_report(
                    orig_nodes, gen_nodes,
                    leaf_diffs,
                    section_matches,
                    orig_analysis_dir, gen_analysis_dir,
                    ai_provider=args.ai_provider,
                )
            )
            logger.info("LLM diff generation complete for %s matching.", mt.upper())

        report_name = f"{image_name}_conversion_report_{mt}.html"
        report_path = os.path.join(output_dir, report_name)

        generate_html_report(
            orig_html, gen_html,
            orig_b64, gen_b64,
            orig_dims, gen_dims,
            leaf_diffs, section_diffs,
            mt,
            avg_leaf, avg_sec,
            report_path,
            extra_stats=extra_stats,
            verified_diffs_pretty=verified_diffs_pretty,
        )

    logger.info("═" * 60)
    logger.info("All reports generated successfully!")
    logger.info("═" * 60)


if __name__ == "__main__":
    main()
