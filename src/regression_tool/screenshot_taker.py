"""
Screenshot capture module for the critique system.
Captures element screenshots from HTML content using the Playwright-based screenshot service.
"""

import sys
import json
import base64
import hashlib
import logging
from pathlib import Path
from lxml import html, etree
import html5lib
import io
import time

# Enable nested asyncio event loops (required when calling from async context)
import nest_asyncio
nest_asyncio.apply()

from utils import (
    transform_xpath,
    get_element_xpath,
    collect_xpaths,
)

from screenshot_taker_playwright import take_screenshots_xpaths_from_html

# Setup console-only logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

def _safe_filename(name: str, max_length: int = 200) -> str:
    """Truncate a transformed XPath filename to max_length, appending an MD5 suffix for uniqueness."""
    if len(name) <= max_length:
        return name
    name_hash = hashlib.md5(name.encode()).hexdigest()[:8]
    return f"{name[:max_length - 9]}_{name_hash}"  # -9 = underscore + 8-char hash


def save_successful_results(results, output_folder):
    success_count = 0
    # Load existing xpath mapping so retry runs merge cleanly
    mapping_path = output_folder / "xpath_mapping.json"
    xpath_mapping = {}
    if mapping_path.exists():
        try:
            with open(mapping_path, "r") as f:
                xpath_mapping = json.load(f)
        except Exception:
            pass

    for item in results.get("success", []):
        try:
            # Extract xpath from selector (format: "xpath/...")
            xpath = item["selector"].replace("xpath/", "")
            sanitized_name = _safe_filename(transform_xpath(xpath))

            xpath_mapping[sanitized_name] = xpath

            # Save computed style
            json_path = output_folder / f"{sanitized_name}.json"
            with open(json_path, "w") as f:
                json.dump(item["computedStyle"], f, indent=4)

            # Save outer HTML
            html_path = output_folder / f"{sanitized_name}.html"
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(item["outerHTML"])

            # Save bbox
            bbox_path = output_folder / f"{sanitized_name}.bbox.json"
            with open(bbox_path, "w") as f:
                json.dump({"bbox": item["bbox"], "scroll_x": item["scroll_x"], "scroll_y": item["scroll_y"]}, f, indent=4)

            success_count += 1
            logger.info(f"Successfully processed: {xpath}")

        except Exception as e:
            logger.warning(f"Error saving data for element: {e}")

    if xpath_mapping:
        with open(mapping_path, "w") as f:
            json.dump(xpath_mapping, f, indent=2)

    # Save page dimensions
    page_dimensions = results.get("page_dimensions", None)
    if page_dimensions:
        with open(output_folder / "page_dimensions.json", "w") as f:
            json.dump(page_dimensions, f, indent=4)

    # Save full-page screenshot
    fp_b64 = results.get("full_page_screenshot")
    if fp_b64:
        with open(output_folder / "full_page.png", "wb") as f:
            f.write(base64.b64decode(fp_b64))

    # Save reduced full-page screenshot (when is_reduced was True)
    fp_reduced_b64 = results.get("full_page_screenshot_reduced")
    if fp_reduced_b64:
        with open(output_folder / "full_page_reduced.png", "wb") as f:
            f.write(base64.b64decode(fp_reduced_b64))

    # Save display-none xpaths (merge with existing file if present)
    display_none_xpaths = results.get("display_none_xpaths", [])
    if display_none_xpaths:
        display_none_path = output_folder / "display_none_xpaths.json"
        existing = []
        if display_none_path.exists():
            try:
                with open(display_none_path, "r") as f:
                    existing = json.load(f)
            except Exception:
                pass
        merged = list(dict.fromkeys(existing + display_none_xpaths))  # deduplicate, preserve order
        with open(display_none_path, "w") as f:
            json.dump(merged, f, indent=4)

    return success_count


def capture_element_screenshots(
    html_content: str,
    output_folder: Path,
    target_xpath_str: str = None,
    target_selector_str: str = None,
    is_reduced: bool = False,
    html_file_path: str = None,
    viewport_width: int = 1280,
) -> tuple[bool, str]:
    """
    Capture screenshots and data for all elements under the target xpath/selector.
    Uses the screenshot service from common module.

    Args:
        html_content: HTML content to process
        output_folder: Directory to save screenshots and data
        target_xpath_str: Target XPath (optional)
        target_selector_str: Target CSS selector (optional)
        is_reduced: Whether to reduce the screenshot by replacing images with gray placeholders (optional)
    Returns:
        tuple: (success, error_message)
    """
    logger.info(f"Processing HTML content for output to {output_folder}")

    if not target_xpath_str and not target_selector_str:
        error_msg = "Either target_xpath_str or target_selector_str must be provided."
        logger.error(error_msg)
        return False, error_msg
    
    if target_xpath_str and target_selector_str:
        error_msg = "Only one of target_xpath_str or target_selector_str should be provided."
        logger.error(error_msg)
        return False, error_msg

    # Parse HTML to collect XPaths
    # Use html5lib parser for browser-like parsing (adds implicit tbody, etc.)
    # This ensures XPaths match what Playwright sees in the browser
    try:
        try:
            # Parse with html5lib using lxml backend, without XML namespaces
            doc = html5lib.parse(
                io.BytesIO(html_content.encode('utf-8')), 
                treebuilder="lxml", 
                namespaceHTMLElements=False
            )
            tree = doc.getroot()
            logger.info("Using html5lib parser for browser-like HTML parsing")
        except ImportError:
            tree = html.fromstring(html_content)
            logger.warning("html5lib not available, using lxml parser (may not match browser structure)")
    except Exception as e_parse:
        error_msg = f"Failed to parse HTML content: {e_parse}"
        logger.error(error_msg)
        return False, error_msg

    # Determine the base XPath from which to collect sub-elements
    base_xpath_for_collection = None
    if target_xpath_str:
        base_xpath_for_collection = target_xpath_str
    elif target_selector_str:
        try:
            # Use lxml's built-in cssselect() method (no extra dependency needed)
            selected_elements = tree.cssselect(target_selector_str)
            if selected_elements:
                selected_element = selected_elements[0]
                base_xpath_for_collection = get_element_xpath(selected_element)
                logger.info(f"Resolved selector '{target_selector_str}' to XPath: {base_xpath_for_collection}")
            else:
                error_msg = f"Could not find element with selector: '{target_selector_str}'"
                logger.error(error_msg)
                return False, error_msg
        except Exception as e_select:
            error_msg = f"Exception resolving selector '{target_selector_str}': {e_select}"
            logger.error(error_msg)
            return False, error_msg
    
    if not base_xpath_for_collection:
        error_msg = f"Could not determine a base XPath for collection."
        logger.error(error_msg)
        return False, error_msg

    # Collect all XPaths under the base_xpath_for_collection
    xpaths_to_capture = collect_xpaths(tree, base_xpath_for_collection)
    
    if not xpaths_to_capture:
        logger.warning(f"No sub-XPaths collected for base '{base_xpath_for_collection}'. Using base XPath only.")
        xpaths_to_capture = [base_xpath_for_collection]
    else:
        logger.info(f"Found {len(xpaths_to_capture)} XPaths to capture screenshots for")

    # Use the screenshot service to capture all elements
    # Note: nest_asyncio allows parallel mode to work within async context
    try:
        results = take_screenshots_xpaths_from_html(
            html_content=html_content,
            xpaths=xpaths_to_capture,
            is_sequential=False,  # Use parallel mode for speed (nest_asyncio handles nested loops)
            is_reduced=is_reduced,
            html_file_path=html_file_path,
            viewport_width=viewport_width,
        )
    except Exception as e:
        error_msg = f"Screenshot service failed: {e}"
        logger.error(error_msg)
        return False, error_msg

    # Process successful results and save to output folder
    success_count = save_successful_results(results, output_folder)

    # Log any errors

    if results.get("error", None):
        error_xpaths = []
        for item in results.get("error", []):
            xpath_ = item["selector"].replace("xpath/", "")
            if xpath_.split("/")[-1].startswith("br[") or xpath_.split("/")[-1] == "br":
                continue
            # re-run for erroneous xpaths
            error_xpaths.append(xpath_)
            logger.warning(f"Rerunning for : {item.get('selector')} - {item.get('error')}")

        time.sleep(5)
        logger.debug("Recomputing screenshots for potentially valid elements with timeouts...")
        
        results_recomputed = take_screenshots_xpaths_from_html(
            html_content=html_content,
            xpaths=error_xpaths,
            is_sequential=False,  # Use parallel mode for speed (nest_asyncio handles nested loops)
            is_reduced=is_reduced,
            html_file_path=html_file_path,
            viewport_width=viewport_width,
        )
        success_count_recomputed = save_successful_results(results_recomputed, output_folder)
    
    else:
        success_count_recomputed = 0

    if success_count + success_count_recomputed == 0:
        error_msg = "No elements were successfully captured."
        logger.error(error_msg)
        return False, error_msg

    logger.info(f"Successfully captured {success_count + success_count_recomputed} elements to {output_folder}")
    return True, None


if __name__ == "__main__":
    filepath="/mnt/localssd/parul/image-critic-data/Optum_Acrites/25_OPTUM_FF_EM_2025_WF14791319_First Fill Actions/output.html"
    html_content = open(filepath, "r").read()
    xpath = "/html/body/div/div[3]"
    print(capture_element_screenshots(
        html_content=html_content,
        output_folder=Path("/mnt/localssd/parul/"),
        target_xpath_str=xpath,
    ))
    # results = take_screenshots_xpaths_from_html(html_content, [xpath], is_sequential=False)

    # with open("/mnt/localssd/parul/image-critic-data/webdiff_dataset/teacher_critic/internvl3/11342_temp0/test_results.json", "w+") as f:
    #     json.dump(results, f, indent=4)
    # screenshot = results["success"][0]["screenshot"]
    # img_bytes = base64.b64decode(screenshot)
    # with open("/mnt/localssd/parul/image-critic-data/webdiff_dataset/teacher_critic/internvl3/11342_temp0/test_screenshot.png", "wb") as f:
    #     f.write(img_bytes)
    # print(results)
