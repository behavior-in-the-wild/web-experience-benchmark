"""
Screenshot capture module for the critique system.
Captures element screenshots from HTML content using the Playwright-based screenshot service.
"""

import sys
import json
import base64
import logging
import re
from pathlib import Path

from geometry_capture import capture_dom_geometry

# Setup console-only logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


def transform_xpath(xpath: str) -> str:
    if '|' in xpath:
        xpath = xpath.replace('|/', '|')
    xpath = xpath.lstrip('/').replace('/', '__')
    return re.sub(r'(\w+)\[(\d+)\]', r'\1_\2', xpath)


def save_successful_results(results, output_folder):
    success_count = 0
    for item in results.get("success", []):
        try:
            # Extract xpath from selector (format: "xpath/...")
            xpath = item["selector"].replace("xpath/", "")
            sanitized_name = transform_xpath(xpath)

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

    capture_metadata = results.get("capture_metadata")
    if capture_metadata:
        with open(output_folder / "capture_metadata.json", "w") as f:
            json.dump(capture_metadata, f, indent=4)

    return success_count


def capture_element_screenshots(
    html_content: str,
    output_folder: Path,
    target_xpath_str: str = None,
    target_selector_str: str = None,
    is_reduced: bool = False,
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

    try:
        results = capture_dom_geometry(html_content=html_content, is_reduced=is_reduced)
        success_count = save_successful_results(results, output_folder)
    except Exception as exc:
        error_msg = f"DOM geometry capture failed: {exc}"
        logger.error(error_msg)
        return False, error_msg

    if success_count == 0:
        error_msg = "No elements were successfully captured."
        logger.error(error_msg)
        return False, error_msg

    logger.info(f"Successfully captured {success_count} geometry records to {output_folder}")
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
