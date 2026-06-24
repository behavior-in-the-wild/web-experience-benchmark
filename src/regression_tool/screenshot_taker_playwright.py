# This particular code files uses playwright to take screenshots of a webpage given HTML content
# Contributor: Aditya Basu (adbasu@adobe.com)


"""
This file takes the screenshot of a web-elements given HTML content and xpaths
It may take screenshots of multiple elements at once asynchronously or synchronously given the preference
"""

"""
Things to do:
Write separate functions for taking screenshots of multiple elements at once asynchronously or synchronously given the preference

Synchronously would be taking screenshots of one xpath at once, and calling this function in a for loop
ASynchronously would be using non-blocking IO to take screenshots of the xpaths
"""

import base64
import logging
from typing import Dict, Any, List, Iterable, Union, Tuple
from playwright.sync_api import sync_playwright, Error as PlaywrightError, Page
from playwright.async_api import async_playwright, Error as PlaywrightError
import argparse
import asyncio
import json
from pathlib import Path, PosixPath

def save_json(d, output_path: str):
    """
    Save a dictionary to a JSON file. If the file exists,
    append the new data to the existing content.
    """
    output_path = Path(output_path)
    
    # Convert string input to dict/list if needed
    if isinstance(d, str):
        d = json.loads(d)
    
    # Read existing data if file exists
    if output_path.exists():
        try:
            with open(output_path, 'r') as f:
                existing_data = json.load(f)
                
            # Combine data
            if isinstance(existing_data, list) and isinstance(d, list):
                d = existing_data + d
            elif isinstance(existing_data, dict) and isinstance(d, dict):
                existing_data.update(d)
                d = existing_data
        except:
            pass  # If reading fails, just overwrite
    
    # Create directory if it doesn't exist
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'w') as f:
        json.dump(d, f, indent=4, ensure_ascii=False)


# JavaScript code to replace all images inside an element with gray placeholders
# This is used for "reduced" screenshots to minimize visual complexity while preserving layout
REPLACE_IMAGES_WITH_GRAY_PLACEHOLDER_JS = """
element => {
    const replacedImages = [];
    
    // Find all img elements inside this element (and the element itself if it's an img)
    const images = element.tagName === 'IMG' ? [element] : element.querySelectorAll('img');
    
    images.forEach((img, index) => {
        // Get the computed dimensions of the image
        const computedStyle = window.getComputedStyle(img);
        const width = img.offsetWidth || parseInt(computedStyle.width) || img.naturalWidth || 100;
        const height = img.offsetHeight || parseInt(computedStyle.height) || img.naturalHeight || 100;
        
        // Store original info for reference
        replacedImages.push({
            index: index,
            originalSrc: img.src,
            width: width,
            height: height
        });
        
        // Create a gray placeholder using a data URI (1x1 gray pixel stretched)
        // Gray color: #808080 (RGB 128, 128, 128)
        const grayDataUri = 'data:image/svg+xml,' + encodeURIComponent(
            `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}">` +
            `<rect width="100%" height="100%" fill="#CCCCCC"/>` +
            `<text x="50%" y="50%" dominant-baseline="middle" text-anchor="middle" ` +
            `font-family="sans-serif" font-size="12" fill="#666666">${width}X${height}</text>` +
            `</svg>`
        );
        
        // Replace the image source with the gray placeholder
        img.src = grayDataUri;
        
        // Also handle srcset if present
        if (img.srcset) {
            img.srcset = '';
        }
        
        // Ensure dimensions are preserved
        img.style.width = width + 'px';
        img.style.height = height + 'px';
        img.style.minWidth = width + 'px';
        img.style.minHeight = height + 'px';
    });
    
    // Also handle background images on div/span elements
    const elementsWithBgImage = element.querySelectorAll('*');
    elementsWithBgImage.forEach((el) => {
        const computedStyle = window.getComputedStyle(el);
        const bgImage = computedStyle.backgroundImage;
        if (bgImage && bgImage !== 'none' && bgImage.startsWith('url(')) {
            const width = el.offsetWidth || 100;
            const height = el.offsetHeight || 100;
            
            replacedImages.push({
                type: 'background-image',
                originalBgImage: bgImage,
                width: width,
                height: height
            });
            
            // Replace with solid gray background
            el.style.backgroundImage = 'none';
            el.style.backgroundColor = '#CCCCCC';
        }
    });
    
    return replacedImages;
}
"""


class SequentialElementScreenshotTaker:
    """
    Given HTML content, this class takes the screenshot of a web-element given HTML content and xpaths
    It takes screenshots of elements one by one
    """

    def __init__(self, html_content: str):
        self.html_content = html_content
        pass
    
    def take_screenshot_of_element(
        self, 
        page: Page, 
        xpath: str, 
        timeout_ms: int,
        is_reduced: bool = False
    ) -> Dict[str, Any]:
        """
        Process one XPath on an already-loaded page (sync).
        
        Args:
            page: Playwright Page object
            xpath: XPath selector for the element
            timeout_ms: Timeout in milliseconds
            is_reduced: If True, replace all image elements inside this element with 
                       gray placeholders of the same size before taking the screenshot
        """

        try:
            element = page.locator(f"xpath={xpath}").first
            # bounding_box() returns None immediately for hidden elements — no 10s stall.
            # TimeoutError is only raised when the element is absent from the DOM entirely.
            bbox = element.bounding_box(timeout=timeout_ms)
            scroll_x = page.evaluate("window.pageXOffset")
            scroll_y = page.evaluate("window.pageYOffset")

            computed_style = element.evaluate(
                """
                element => {
                    const style = window.getComputedStyle(element);
                    const out = {};
                    for (let i = 0; i < style.length; i++) {
                        const prop = style[i];
                        out[prop] = style.getPropertyValue(prop);
                    }
                    return out;
                }
                """
            )
            outer_html = element.evaluate("el => el.outerHTML")

            is_display_none = computed_style.get("display", "").strip().lower() == "none"

            if bbox is None or (bbox.get("width", 0) == 0 and bbox.get("height", 0) == 0):
                logging.debug(f"Zero/no bbox for XPath '{xpath}' — element is hidden")

            return {
                "success": True,
                "data": {
                    "selector": f"xpath/{xpath}",
                    "outerHTML": outer_html,
                    "computedStyle": computed_style,
                    "bbox": bbox,
                    "scroll_x": scroll_x,
                    "scroll_y": scroll_y,
                    "is_display_none": is_display_none,
                },
            }

        except PlaywrightError as e:
            msg = f"Playwright Error for XPath '{xpath}': {e}"
            if "Timeout" in str(e):
                msg = f"Timeout Error: Element not found in DOM for XPath: {xpath}"
            logging.error(msg)
            return {
                "success": False,
                "data": {"selector": f"xpath/{xpath}", "error": msg.split(":", 1)[1].strip(), "is_display_none": False},
            }
        except Exception as e:
            msg = f"Unexpected Error processing XPath '{xpath}': {e}"
            logging.error(msg, exc_info=True)
            return {"success": False, "data": {"selector": f"xpath/{xpath}", "error": msg, "is_display_none": False}}


    def take_screenshots_xpaths_from_html(
        self,
        html_content: str,
        xpaths: Union[str, Iterable[str]],
        *,
        # base_url: str = "about:blank",
        device_scale_factor: float = 1.0,
        wait_until: str = "load",          # "load" | "domcontentloaded" | "networkidle" | "commit"
        extra_wait_ms: int = 0,            # let webfonts/animations settle if needed
        timeout_ms: int = 10_000,
        is_reduced: bool = False,          # if True, replace images with gray placeholders
        html_file_path: str = None,        # if set, load via file:// URI instead of set_content()
        viewport_width: int = 1280,        # CSS pixel width for the Playwright viewport
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Render `html_content` and collect bbox/metadata for each XPath, plus a
        single full-page screenshot (and optionally a reduced variant).

        Returns a dict with:
        - success: list of per-element data dicts (outerHTML, computedStyle, bbox, ...)
        - error: list of error data dicts
        - page_dimensions: {pageWidth, pageHeight}
        - full_page_screenshot: base64 PNG of the full page
        - full_page_screenshot_reduced: (only when is_reduced=True) base64 PNG with images replaced by gray placeholders
        """
        if isinstance(xpaths, str):
            xpaths = [xpaths]

        results = {"success": [], "error": [], "display_none_xpaths": []}

        with sync_playwright() as p:
            browser = p.chromium.launch()

            # Creating new context to take sharper screenshots
            context = browser.new_context(
                device_scale_factor=device_scale_factor,
                viewport={"width": viewport_width, "height": 900},
            )
            # Load a new page
            page = context.new_page()
            if html_file_path:
                from pathlib import Path as _Path
                page.goto(_Path(html_file_path).as_uri(), wait_until="domcontentloaded")
            else:
                page.set_content(html_content)
                page.wait_for_load_state("domcontentloaded")

            # Set HTML and body element heights to auto
            page_dimensions = page.evaluate("""() => {
                const htmlElement = document.documentElement;
                const bodyElement = document.body;
                if (htmlElement) {
                    htmlElement.style.height = 'auto';
                }
                if (bodyElement) {
                    bodyElement.style.height = 'auto';
                }
                const pageWidth = Math.max(
                    bodyElement.scrollWidth,
                    htmlElement.scrollWidth,
                    bodyElement.clientWidth
                );
                const pageHeight = Math.max(
                    bodyElement.scrollHeight,
                    htmlElement.scrollHeight,
                    bodyElement.clientHeight
                );
                return {
                    "pageWidth": pageWidth,
                    "pageHeight": pageHeight,
                }
            }""")

            results["page_dimensions"] = page_dimensions

            if extra_wait_ms:
                page.wait_for_timeout(extra_wait_ms)

            # Process each XPath and sort results
            for xpath in xpaths:
                result = self.take_screenshot_of_element(page, xpath, timeout_ms)
                if result["success"]:
                    results["success"].append(result["data"])
                    if result["data"].get("is_display_none"):
                        results["display_none_xpaths"].append(xpath)
                else:
                    results["error"].append(result["data"])
                    if result["data"].get("is_display_none"):
                        results["display_none_xpaths"].append(xpath)

            fp_bytes = page.screenshot(full_page=True)
            results["full_page_screenshot"] = base64.b64encode(fp_bytes).decode("utf-8")

            if is_reduced:
                page.evaluate("() => { (" + REPLACE_IMAGES_WITH_GRAY_PLACEHOLDER_JS + ")(document.body); }")
                fp_reduced_bytes = page.screenshot(full_page=True)
                results["full_page_screenshot_reduced"] = base64.b64encode(fp_reduced_bytes).decode("utf-8")

            browser.close()

            return results

    

class ParallelElementScreenshotTaker:
    """
    Takes screenshots of multiple elements at once synchronously
    """
    def __init__(self, html_content: str):
        self.html_content = html_content
        pass


    async def take_screenshot_of_element(
        self, 
        page: Page, 
        xpath: str, 
        timeout_ms: int,
    ) -> Dict[str, Any]:
        """
        Process one XPath on an already-loaded page (async).
        
        Args:
            page: Playwright Page object
            xpath: XPath selector for the element
            timeout_ms: Timeout in milliseconds
        """

        try:
            element = page.locator(f"xpath={xpath}").first
            # bounding_box() returns None immediately for hidden elements — no 10s stall.
            # TimeoutError is only raised when the element is absent from the DOM entirely.
            bbox = await element.bounding_box(timeout=timeout_ms)

            computed_style = await element.evaluate(
                """
                element => {
                    const style = window.getComputedStyle(element);
                    const out = {};
                    for (let i = 0; i < style.length; i++) {
                        const prop = style[i];
                        out[prop] = style.getPropertyValue(prop);
                    }
                    return out;
                }
                """
            )
            outer_html = await element.evaluate("el => el.outerHTML")
            scroll_x = await page.evaluate("window.pageXOffset")
            scroll_y = await page.evaluate("window.pageYOffset")

            is_display_none = computed_style.get("display", "").strip().lower() == "none"

            if bbox is None or (bbox.get("width", 0) == 0 and bbox.get("height", 0) == 0):
                logging.debug(f"Zero/no bbox for XPath '{xpath}' — element is hidden")

            return {
                "success": True,
                "data": {
                    "selector": f"xpath/{xpath}",
                    "outerHTML": outer_html,
                    "computedStyle": computed_style,
                    "bbox": bbox,
                    "scroll_x": scroll_x,
                    "scroll_y": scroll_y,
                    "is_display_none": is_display_none,
                },
            }

        except PlaywrightError as e:
            msg = f"Playwright Error for XPath '{xpath}': {e}"
            if "Timeout" in str(e):
                msg = f"Timeout Error: Element not found in DOM for XPath: {xpath}"
            logging.error(msg)
            return {
                "success": False,
                "data": {"selector": f"xpath/{xpath}", "error": msg.split(":", 1)[1].strip(), "is_display_none": False},
            }
        except Exception as e:
            msg = f"Unexpected Error processing XPath '{xpath}': {e}"
            logging.error(msg, exc_info=True)
            return {"success": False, "data": {"selector": f"xpath/{xpath}", "error": msg, "is_display_none": False}}


    async def take_screenshots_xpaths_from_html(
        self,
        html_content: str,
        xpaths: Union[str, Iterable[str]],
        *,
        # base_url: str = "about:blank",
        device_scale_factor: float = 1.0,
        wait_until: str = "load",          # "load" | "domcontentloaded" | "networkidle" | "commit"
        extra_wait_ms: int = 0,            # let webfonts/animations settle if needed
        timeout_ms: int = 10_000,
        is_reduced: bool = False,          # if True, replace images with gray placeholders
        html_file_path: str = None,        # if set, load via file:// URI instead of set_content()
        viewport_width: int = 1280,        # CSS pixel width for the Playwright viewport
    ) -> List[Dict[str, Any]]:
        """
        Render `html_content` and collect bbox/metadata for each XPath, plus a
        single full-page screenshot (and optionally a reduced variant).

        Returns a dict with:
        - success: list of per-element data dicts (outerHTML, computedStyle, bbox, ...)
        - error: list of error data dicts
        - page_dimensions: {pageWidth, pageHeight}
        - full_page_screenshot: base64 PNG of the full page
        - full_page_screenshot_reduced: (only when is_reduced=True) base64 PNG with images replaced by gray placeholders
        """
        if isinstance(xpaths, str):
            xpaths = [xpaths]

        results = {"success": [], "error": [], "display_none_xpaths": []}
        browser = None

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch()

                # Creating new context to take sharper screenshots
                context = await browser.new_context(
                    device_scale_factor=device_scale_factor,
                    viewport={"width": viewport_width, "height": 900},
                )
                # Load a new page
                page = await context.new_page()
                if html_file_path:
                    from pathlib import Path as _Path
                    await page.goto(_Path(html_file_path).as_uri(), wait_until="domcontentloaded")
                else:
                    await page.set_content(html_content)
                    await page.wait_for_load_state("domcontentloaded")

                # Set HTML and body element heights to auto
                page_dimensions = await page.evaluate("""() => {
                    const htmlElement = document.documentElement;
                    const bodyElement = document.body;
                    if (htmlElement) {
                        htmlElement.style.height = 'auto';
                    }
                    if (bodyElement) {
                        bodyElement.style.height = 'auto';
                    }

                    const pageWidth = Math.max(
                        bodyElement.scrollWidth,
                        htmlElement.scrollWidth,
                        bodyElement.clientWidth
                    );
                    const pageHeight = Math.max(
                        bodyElement.scrollHeight,
                        htmlElement.scrollHeight,
                        bodyElement.clientHeight
                    );

                    return {
                        "pageWidth": pageWidth,
                        "pageHeight": pageHeight,
                    }
                }""")

                results["page_dimensions"] = page_dimensions

                if extra_wait_ms:
                    await page.wait_for_timeout(extra_wait_ms)


                # Process all XPaths concurrently
                xpath_results = await asyncio.gather(
                    *(self.take_screenshot_of_element(page, xpath, timeout_ms) for xpath in xpaths),
                    return_exceptions=True
                )

                # Process results
                for result in xpath_results:
                    if isinstance(result, dict):
                        if result["success"]:
                            results["success"].append(result["data"])
                            if result["data"].get("is_display_none"):
                                results["display_none_xpaths"].append(
                                    result["data"]["selector"].replace("xpath/", "")
                                )
                        else:
                            results["error"].append(result["data"])
                            if result["data"].get("is_display_none"):
                                results["display_none_xpaths"].append(
                                    result["data"]["selector"].replace("xpath/", "")
                                )
                    else:  # Exception occurred
                        logging.error(f"Unexpected error during parallel processing: {result}")

                fp_bytes = await page.screenshot(full_page=True)
                results["full_page_screenshot"] = base64.b64encode(fp_bytes).decode("utf-8")

                if is_reduced:
                    await page.evaluate("() => { (" + REPLACE_IMAGES_WITH_GRAY_PLACEHOLDER_JS + ")(document.body); }")
                    fp_reduced_bytes = await page.screenshot(full_page=True)
                    results["full_page_screenshot_reduced"] = base64.b64encode(fp_reduced_bytes).decode("utf-8")

                await browser.close()
                browser = None

                return results
        
        except PlaywrightError as e:
            error_message = f"Fatal Playwright Error: {e}"
            logging.critical(error_message)
            for xpath in xpaths:
                if not any(item['selector'] == f"xpath/{xpath}" for item in results['success']) and \
                not any(item['selector'] == f"xpath/{xpath}" for item in results['error']):
                    results["error"].append({
                        "selector": f"xpath/{xpath}",
                        "error": "Failed due to browser/page setup error."
                    })

        except Exception as e:
            error_message = f"Fatal Unexpected Error: {e}"
            logging.critical(error_message, exc_info=True)
            for xpath in xpaths:
                if not any(item['selector'] == f"xpath/{xpath}" for item in results['success']) and \
                not any(item['selector'] == f"xpath/{xpath}" for item in results['error']):
                    results["error"].append({
                        "selector": f"xpath/{xpath}",
                        "error": "Failed due to unexpected setup error."
                    })
        finally:
            if browser:
                logging.warning("Closing browser due to incomplete operation or error.")
                await browser.close()

        return results


class ElementScreenshotTaker:
    """
    Takes HTML content, locators of the web elements for which screenshot needs to be taken, and a boolean parameter
    """

    def __init__(self, html_content: str = None, html_file_path: str = None):
        self.html_content = html_content
        self.html_file_path = html_file_path

    def call_sequential(
        self,
        xpaths: List[str],
        is_reduced: bool = False,
        viewport_width: int = 1280,
    ):
        sequential_screenshot_taker = SequentialElementScreenshotTaker(self.html_content)
        return sequential_screenshot_taker.take_screenshots_xpaths_from_html(
            self.html_content, xpaths, is_reduced=is_reduced, html_file_path=self.html_file_path,
            viewport_width=viewport_width,
        )

    def call_parallel(
        self,
        xpaths: List[str],
        is_reduced: bool = False,
        viewport_width: int = 1280,
    ):
        parallel_screenshot_taker = ParallelElementScreenshotTaker(self.html_content)
        return asyncio.run(
            parallel_screenshot_taker.take_screenshots_xpaths_from_html(
                self.html_content, xpaths, is_reduced=is_reduced, html_file_path=self.html_file_path,
                viewport_width=viewport_width,
            )
        )
        # loop = asyncio.get_running_loop()
        # results = loop.run_in_executor(
        #     None,
        #     lambda: parallel_screenshot_taker.take_screenshots_xpaths_from_html(self.html_content, xpaths)
        # )


# function to be imported
def take_screenshots_xpaths_from_html(
    html_content: str,
    xpaths: List[str],
    is_sequential: bool = False,  # by default we use non-blocking IO to take screenshots of different web elements parallely
    is_reduced: bool = False,     # if True, replace images with gray placeholders
    html_file_path: str = None,   # if set, load via file:// URI (avoids set_content timeout on large/complex pages)
    viewport_width: int = 1280,   # CSS pixel width for the Playwright viewport
) -> List[Dict[str, Any]]:  # returns: List of dictionaries with screenshot and metadata for each xpath
    """
    Takes HTML content, locators of the web elements for which screenshot needs to be taken.

    Args:
        html_content: The HTML content to render
        xpaths: List of XPath selectors for elements to screenshot
        is_sequential: If True, take screenshots sequentially; if False, take them in parallel
        is_reduced: If True, replace all image elements inside each target element with
                   gray placeholders of the same size before taking the screenshot.
                   This reduces visual complexity while preserving layout.
        html_file_path: Optional path to the source HTML file on disk. When provided the
                        page is loaded via a file:// URI (page.goto) instead of set_content(),
                        which correctly resolves relative asset paths in "Web Page, Complete"
                        saves and avoids set_content() timeouts on large/complex pages.
        viewport_width: CSS pixel width for the Playwright viewport (default 1280).

    Returns: List[Dict[str, Any]] with screenshot data for each xpath
    """
    assert html_content is not None, "HTML content must be provided"

    element_screenshot_taker = ElementScreenshotTaker(html_content, html_file_path=html_file_path)

    if is_sequential:
        return element_screenshot_taker.call_sequential(xpaths, is_reduced=is_reduced, viewport_width=viewport_width)
    else:
        return element_screenshot_taker.call_parallel(xpaths, is_reduced=is_reduced, viewport_width=viewport_width)


def store_responses(
    xpaths: List[str],
    responses: Dict,
    output_paths: List[str],
):
    # Creating a successful response dictionary
    successful_response_dict = {}
    for successful_response in responses["success"]:
        successful_response_dict[successful_response["selector"]] = successful_response

    for xpath, output_path in zip(xpaths, output_paths):
        if f"xpath/{xpath}" in successful_response_dict:
            screenshot_response = successful_response_dict[f"xpath/{xpath}"]

            # Storing the decoded image from the base64 string output
            img_bytes = base64.b64decode(screenshot_response["screenshot"])
            img_outpath = Path(output_path/"screenshot.png")
            img_outpath.write_bytes(img_bytes)
            # Store the computed style
            computed_style = screenshot_response["computedStyle"]
            save_json(computed_style, output_path/"computed_style.json")

            # Store the entire HTML
            outer_html = screenshot_response["outerHTML"]
            open(Path(output_path)/"outer_html.html", "w").write(outer_html)

            # Store the entire screenshot response
            save_json(screenshot_response, output_path/"entire_screenshot_response.json")

            # Store additional metadata
            metadata = {
                "id": str(output_path.name),
                "absolute_path": str(output_path),
                "xpath": xpath,
                "mode": "html"
            }
            save_json(metadata, output_path/"metadata.json")

            # print(f"Screenshot saved to {output_path}")


# function which can be imported as well
def capture_and_store_screenshots_for_xpaths_from_html(
    html_content: str,
    xpaths: List[str],
    output_paths: List[str],
    is_sequential: bool = False,
    is_reduced: bool = False
):
    """
    Capture screenshots and store them along with metadata.
    
    Args:
        html_content: The HTML content to render
        xpaths: List of XPath selectors for elements to screenshot
        output_paths: List of output paths for storing results
        is_sequential: If True, take screenshots sequentially; if False, take them in parallel
        is_reduced: If True, replace all image elements with gray placeholders before screenshot
    """
    results = take_screenshots_xpaths_from_html(
        html_content=html_content, 
        xpaths=xpaths, 
        is_sequential=is_sequential,
        is_reduced=is_reduced
    )
    store_responses(xpaths=xpaths, responses=results, output_paths=output_paths)
