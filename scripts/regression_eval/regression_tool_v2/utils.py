"""
Utility functions for the critique module.
Contains helper functions for XPath manipulation, file I/O, image processing,
DOM tree operations, and prompt loading.
"""

import os
import json
import csv
import base64
import hashlib
import re
import logging
from mimetypes import guess_type
from pathlib import Path
from lxml import html, etree
from bs4 import BeautifulSoup
import numpy as np

# Setup console-only logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# =============================================================================
# Prompt Loading
# =============================================================================

PROMPTS_DIR = Path(__file__).parent.parent / "assets" / "prompts"

def load_prompt(prompt_name: str) -> str:
    """Load a prompt from the assets/prompts directory."""
    prompt_path = PROMPTS_DIR / prompt_name / "system.txt"
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_path}")
    with open(prompt_path, "r", encoding="utf-8") as f:
        return f.read()


# =============================================================================
# General Utilities
# =============================================================================

def hash_string(s: str) -> str:
    """Generate MD5 hash of a string."""
    return hashlib.md5(s.encode()).hexdigest()


def save_json(data, filepath):
    """Save data as JSON to the specified filepath."""
    filepath = Path(filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4)
    print(f"Saved JSON to {filepath}")


def load_json_content(filepath: Path) -> dict:
    """Load JSON content from a file. Returns empty dict if file doesn't exist or is invalid."""
    if filepath.exists():
        with open(filepath, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                logger.warning(f"Could not decode JSON from {filepath}. Returning empty dict.")
                return {}
    return {}


def clean_json_response(llm_output: str) -> str:
    """Clean LLM JSON output by removing markdown code fences."""
    if llm_output.startswith("```json"):
        llm_output = llm_output[7:]  
    if llm_output.endswith("```"):
        llm_output = llm_output[:-3]
    llm_output = llm_output.strip()
    return llm_output


# =============================================================================
# XPath Utilities
# =============================================================================

def transform_xpath(xpath: str) -> str:
    """Transform an XPath to a sanitized filename-safe format."""
    if '|' in xpath:
        xpath = xpath.replace('|/', '|')
    xpath = xpath.lstrip('/').replace('/', '__')
    xpath = re.sub(r'(\w+)\[(\d+)\]', r'\1_\2', xpath)
    return xpath

def reverse_transform_xpath(transformed_xpath: str) -> str:
    """
    Reverse the transformation to get back the original XPath format.
    
    Args:
        transformed_xpath: Filename-safe XPath string (e.g., 'html__body__div_2__span_1')
        
    Returns:
        Original XPath format (e.g., '/html/body/div[2]/span[1]')
    
    Examples:
        'html__body__div_2__span_1' -> '/html/body/div[2]/span[1]'
        'html__body__h6_1' -> '/html/body/h6[1]'  (tag name h6 contains digit)
        'html__body__tr_4__td_1__h6_1' -> '/html/body/tr[4]/td[1]/h6[1]'
    """
    # Replace underscore-digit patterns with bracket notation
    # Use [a-zA-Z]+ instead of \w+ because \w includes underscores
    # Pattern: letters followed by optional digits (tag name like h6, h1, div) + underscore + index
    # Use [a-zA-Z]+\d* to handle tag names that contain digits (h1, h2, h6, etc.)
    xpath = re.sub(r'([a-zA-Z]+\d*)_(\d+)', r'\1[\2]', transformed_xpath)
    # Replace double underscores with forward slashes
    xpath = xpath.replace('__', '/')
    # Add leading forward slash
    xpath = '/' + xpath
    if '|' in xpath:
        xpath = xpath.replace('|', '|/')
    return xpath


def get_element_xpath(element) -> str:
    """
    Generate standard XPath for an lxml element with 1-based indexing.
    This matches the standard XPath notation expected by browsers and Playwright.
    """
    components = []
    child = element
    
    while child is not None:
        parent = child.getparent()
        
        if parent is None:
            # Root element
            components.insert(0, child.tag)
            break
        
        # Get only element children (filter out text nodes, comments, etc.)
        element_children = [c for c in parent if isinstance(c.tag, str)]
        
        # Count preceding siblings with the same tag (only element nodes)
        index = 1
        for sibling in element_children:
            if sibling == child:
                break
            if sibling.tag == child.tag:
                index += 1
        
        # Count total siblings with same tag (only element nodes)
        siblings_with_same_tag = sum(1 for s in element_children if s.tag == child.tag)
        
        if siblings_with_same_tag > 1:
            components.insert(0, f"{child.tag}[{index}]")
        else:
            components.insert(0, child.tag)
        
        child = parent
    
    return '/' + '/'.join(components)


def collect_xpaths(tree, base_xpath: str) -> list:
    """
    Collect xpaths of all elements under base_xpath using DFS.
    
    Args:
        tree: lxml HTML tree
        base_xpath: Base XPath to start collection from
        
    Returns:
        List of XPath strings for all descendants of the base element
    """
    xpaths = []
    validation_failures = []
    
    # Find the base element using the xpath
    try:
        base_elements = tree.xpath(base_xpath)
        if not base_elements:
            logger.warning(f"No element found for base XPath: {base_xpath}")
            return xpaths
        
        # Use the first matching element
        base_element = base_elements[0]
        logger.info(f"Found base element: {base_element.tag}")
    except etree.XPathEvalError as e:
        logger.error(f"Invalid XPath '{base_xpath}': {e}")
        return xpaths
    
    def dfs(element):
        """Recursively collect XPaths via DFS."""
        # Skip text nodes, comments, and processing instructions
        if not isinstance(element.tag, str):
            return
        
        xpath = get_element_xpath(element)
        
        # Validate that the generated XPath can find the element
        try:
            found_elements = tree.xpath(xpath)
            if found_elements:
                # Check if the first found element is the same as our element
                is_match = (found_elements[0] is element)  # Use 'is' for identity check
                if is_match:
                    xpaths.append(xpath)
                else:
                    validation_failures.append({
                        'xpath': xpath,
                        'tag': element.tag,
                        'found': len(found_elements),
                        'reason': 'Different element returned'
                    })
                    logger.debug(f"XPath validation failed: '{xpath}' found different element")
            else:
                validation_failures.append({
                    'xpath': xpath,
                    'tag': element.tag,
                    'found': 0,
                    'reason': 'No elements found'
                })
                logger.debug(f"XPath validation failed: '{xpath}' found no elements")
        except etree.XPathEvalError as e:
            validation_failures.append({
                'xpath': xpath,
                'tag': element.tag,
                'error': str(e),
                'reason': 'XPath syntax error'
            })
            logger.debug(f"Generated invalid XPath '{xpath}': {e}")
        
        # Traverse all child elements
        for child in element:
            dfs(child)
    
    # Start DFS from the base element
    dfs(base_element)
    
    logger.info(f"Collected {len(xpaths)} valid XPaths from base element")
    
    if validation_failures:
        logger.warning(f"Failed to validate {len(validation_failures)} XPaths")
        
        # Write validation failures to /tmp/xpath_validation_failures.txt for debugging
        try:
            with open('/tmp/xpath_validation_failures.txt', 'w') as f:
                f.write(f"Total validation failures: {len(validation_failures)}\n")
                f.write("=" * 80 + "\n\n")
                f.write("First 10 validation failures:\n")
                f.write("-" * 80 + "\n\n")
                
                for i, failure in enumerate(validation_failures[:10], 1):
                    f.write(f"Failure #{i}:\n")
                    f.write(f"  XPath: {failure.get('xpath', 'N/A')}\n")
                    f.write(f"  Tag: {failure.get('tag', 'N/A')}\n")
                    f.write(f"  Reason: {failure.get('reason', 'N/A')}\n")
                    f.write(f"  Found elements: {failure.get('found', 'N/A')}\n")
                    if 'error' in failure:
                        f.write(f"  Error: {failure.get('error')}\n")
                    f.write("\n")
                
                f.write("=" * 80 + "\n")
                f.write(f"\nTotal valid XPaths collected: {len(xpaths)}\n")
                if xpaths:
                    f.write(f"\nFirst 10 valid XPaths:\n")
                    f.write("-" * 80 + "\n")
                    for i, xpath in enumerate(xpaths[:10], 1):
                        f.write(f"{i}. {xpath}\n")
            
            logger.info("Validation failures written to /tmp/xpath_validation_failures.txt")
        except Exception as e:
            logger.error(f"Failed to write validation failures to file: {e}")
        
        # Log first few failures for debugging
        for failure in validation_failures[:10]:
            logger.warning(f"  Failed: {failure}")
    
    # Log sample XPaths for verification
    if xpaths:
        logger.info(f"Sample XPaths (first 5): {xpaths[:5]}")
    
    return xpaths


# =============================================================================
# Image/Base64 Utilities
# =============================================================================

def local_image_to_data_url(image_path) -> str:
    """Convert a local image file to a base64 data URL."""
    mime_type, _ = guess_type(str(image_path))
    if mime_type is None:
        mime_type = 'application/octet-stream'
    with open(image_path, "rb") as image_file:
        base64_encoded_data = base64.b64encode(image_file.read()).decode('utf-8')
    return f"data:{mime_type};base64,{base64_encoded_data}"


def image_to_base64(image_path) -> str:
    """Convert an image file to a base64 data URL (PNG format)."""
    if not os.path.exists(image_path):
        return None
    try:
        with open(image_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode('utf-8')
        return f"data:image/png;base64,{encoded_string}"
    except Exception as e:
        print(f"Error converting image {image_path} to base64: {e}")
        return None


# =============================================================================
# Style Comparison Utilities
# =============================================================================

def compute_style_diff(original_styles: dict, translated_styles: dict) -> dict:
    """Compute differences between original and translated computed styles."""
    diff = {}
    for key in original_styles:
        orig_value = original_styles[key]
        trans_value = translated_styles.get(key, None)
        if trans_value is None or orig_value != trans_value:
            diff[key] = {"original": orig_value, "translated": trans_value}
    return diff


# =============================================================================
# DOM Tree Utilities
# =============================================================================

def build_dom_tree(csv_file_path: Path) -> dict:
    """Build a DOM tree structure from a matched screenshots CSV file."""
    nodes_dict = {}
    if not csv_file_path.exists():
        logger.error(f"CSV file for building DOM tree not found: {csv_file_path}")
        return nodes_dict 

    with open(csv_file_path, "r", encoding="utf-8") as csvfile:
        reader = csv.reader(csvfile)

        for i, row in enumerate(reader):
            if len(row) < 3:
                logger.warning(f"Skipping malformed row {i+1} in {csv_file_path}: {row}")
                continue
            
            original_file_png, translated_file_png, similarity_score_str = row[0], row[1], row[2]
            
            original_file_png = Path(original_file_png).name
            translated_file_png = Path(translated_file_png).name

            try:
                score_val = float(similarity_score_str)
            except ValueError:
                logger.warning(f"Invalid similarity score '{similarity_score_str}' for {original_file_png}. Defaulting to 0.0.")
                score_val = 0.0
            
            node_id_str = original_file_png[:-4] if original_file_png.lower().endswith(".png") else original_file_png
            translated_node_id_str = translated_file_png[:-4] if translated_file_png.lower().endswith(".png") else translated_file_png

            nodes_dict[node_id_str] = {
                "id": node_id_str,
                "translated_id": translated_node_id_str, 
                "original_file_png": original_file_png,
                "translated_file_png": translated_file_png, 
                "similarity_score": score_val,
                "children": []
            }
    
    # Build parent-child relationships
    for current_node_id in list(nodes_dict.keys()): 
        if "__" in current_node_id: 
            parent_id_candidate = "__".join(current_node_id.split("__")[:-1])
            if parent_id_candidate in nodes_dict:
                nodes_dict[parent_id_candidate]["children"].append(nodes_dict[current_node_id])
    
    return nodes_dict


def calculate_heights(nodes_map: dict) -> dict:
    """Calculate the height of each node in the DOM tree."""
    heights_cache = {}
    processing_stack = set()  # For cycle detection

    def get_node_height(node_id_str):
        if node_id_str in heights_cache:
            return heights_cache[node_id_str]
        if node_id_str in processing_stack:
            logger.warning(f"Cycle detected involving node {node_id_str} during height calculation.")
            return -2  # Special value for cycle

        processing_stack.add(node_id_str)
        current_node_data = nodes_map.get(node_id_str)
        if not current_node_data:
            logger.warning(f"Node {node_id_str} referenced but not found in nodes_map for height calculation.")
            processing_stack.remove(node_id_str)
            return -1  # Node not found

        if not current_node_data.get("children"):  # Leaf node
            height_val = 0
        else:
            max_child_h = -1
            valid_child_exists = False
            for child_node_data in current_node_data["children"]:
                child_id_str = child_node_data.get("id")
                if not child_id_str or child_id_str not in nodes_map:
                    logger.warning(f"Child node {child_id_str} not found for parent {node_id_str}. Skipping for height calc.")
                    continue
                
                child_h = get_node_height(child_id_str)
                if child_h == -2:  # Cycle detected below
                    processing_stack.remove(node_id_str)
                    return -2  # Propagate cycle error
                if child_h == -1:  # Child node itself wasn't found or had issues
                    continue
                
                valid_child_exists = True
                max_child_h = max(max_child_h, child_h)
            
            height_val = (1 + max_child_h) if valid_child_exists else 0

        heights_cache[node_id_str] = height_val
        if node_id_str in processing_stack:
            processing_stack.remove(node_id_str)
        return height_val

    # Calculate height for all nodes
    all_node_ids_list = list(nodes_map.keys())
    for n_id in all_node_ids_list:
        if n_id not in heights_cache:
            get_node_height(n_id)

    # Add height to node data
    nodes_with_heights_info = {}
    for n_id, node_d in nodes_map.items():
        calculated_h = heights_cache.get(n_id, -1)
        if calculated_h < 0:
            logger.warning(f"Node {n_id} ended with invalid height {calculated_h}. Setting effective height to -1.")
            node_d['height'] = -1
        else:
            node_d['height'] = calculated_h
        nodes_with_heights_info[n_id] = node_d
    
    return nodes_with_heights_info


# =============================================================================
# HTML Parsing Utilities
# =============================================================================

def contains_image(html_content: str) -> bool:
    """Check if HTML content contains an img tag."""
    soup = BeautifulSoup(html_content, 'html.parser')
    return soup.find('img') is not None


def get_bbox_center(bbox: dict) -> tuple:
    """Get the center of a bounding box."""
    if bbox is None:
        return None
    if 'x' not in bbox or 'y' not in bbox or 'width' not in bbox or 'height' not in bbox:
        return None
    return np.array([bbox['x'] + bbox['width'] / 2, bbox['y'] + bbox['height'] / 2])

def get_union_bbox(bboxes: list[dict]) -> dict:
    """Get the union of a list of bounding boxes."""
    if not bboxes:
        return None
    x1 = min(bbox['x'] for bbox in bboxes)
    y1 = min(bbox['y'] for bbox in bboxes)
    x2 = max(bbox['x'] + bbox['width'] for bbox in bboxes)
    y2 = max(bbox['y'] + bbox['height'] for bbox in bboxes)
    return {'x': x1, 'y': y1, 'width': x2 - x1, 'height': y2 - y1}