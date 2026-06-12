from __future__ import annotations

import base64
from typing import Any

from playwright.sync_api import sync_playwright

from browser_config import launch_chromium, new_context, set_content_and_settle


def capture_dom_geometry(html_content: str, *, is_reduced: bool = False) -> dict[str, Any]:
    """Capture DOM geometry in one browser-side pass.

    The returned shape intentionally matches the subset consumed by
    screenshot_taker.save_successful_results().
    """
    with sync_playwright() as pw:
        browser = launch_chromium(pw)
        try:
            context = new_context(browser)
            page = context.new_page()
            set_content_and_settle(page, html_content)
            if is_reduced:
                page.evaluate(_REPLACE_IMAGES_WITH_GRAY_PLACEHOLDERS)

            geometry = page.evaluate(_GEOMETRY_JS)
            screenshot = page.screenshot(full_page=True)
            geometry["full_page_screenshot"] = base64.b64encode(screenshot).decode("utf-8")
            if is_reduced:
                geometry["full_page_screenshot_reduced"] = geometry["full_page_screenshot"]
            return geometry
        finally:
            browser.close()


_REPLACE_IMAGES_WITH_GRAY_PLACEHOLDERS = """() => {
    document.querySelectorAll('img, picture, svg, canvas, video').forEach(el => {
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) return;
        const placeholder = document.createElement('div');
        placeholder.style.width = rect.width + 'px';
        placeholder.style.height = rect.height + 'px';
        placeholder.style.background = '#d1d5db';
        placeholder.style.display = getComputedStyle(el).display === 'inline' ? 'inline-block' : 'block';
        placeholder.setAttribute('data-regression-placeholder', 'image');
        el.replaceWith(placeholder);
    });
}"""


_GEOMETRY_JS = """() => {
    const STYLE_FIELDS = [
        'display', 'visibility', 'opacity', 'position', 'background-image',
        'font-size', 'font-family', 'font-weight', 'color', 'overflow',
        'z-index', 'transform'
    ];

    function elementXPath(el) {
        const parts = [];
        let node = el;
        while (node && node.nodeType === Node.ELEMENT_NODE) {
            const tag = node.tagName.toLowerCase();
            const parent = node.parentElement;
            if (!parent) {
                parts.unshift(tag);
                break;
            }
            const sameTag = Array.from(parent.children).filter(c => c.tagName === node.tagName);
            if (sameTag.length > 1) {
                parts.unshift(`${tag}[${sameTag.indexOf(node) + 1}]`);
            } else {
                parts.unshift(tag);
            }
            node = parent;
        }
        return '/' + parts.join('/');
    }

    function computedStyleObject(el) {
        const style = window.getComputedStyle(el);
        const out = {};
        for (const prop of STYLE_FIELDS) {
            out[prop] = style.getPropertyValue(prop);
        }
        return out;
    }

    function bboxFor(el, xpath, pageWidth, pageHeight) {
        const rect = el.getBoundingClientRect();
        let width = rect.width;
        let height = rect.height;
        let x = rect.left + window.scrollX;
        let y = rect.top + window.scrollY;
        let synthetic = false;

        if (xpath === '/html/body' && (width <= 0 || height <= 0)) {
            x = 0;
            y = 0;
            width = pageWidth;
            height = pageHeight;
            synthetic = true;
        }
        return {x, y, width, height, synthetic_body_bbox: synthetic};
    }

    const doc = document.documentElement;
    const body = document.body;
    const pageWidth = Math.max(
        doc ? doc.scrollWidth : 0,
        body ? body.scrollWidth : 0,
        doc ? doc.clientWidth : 0
    );
    const pageHeight = Math.max(
        doc ? doc.scrollHeight : 0,
        body ? body.scrollHeight : 0,
        doc ? doc.clientHeight : 0
    );

    const elements = body ? [document.documentElement, body, ...body.querySelectorAll('*')] : [];
    const success = [];
    const displayNone = [];
    let syntheticBody = false;

    for (const el of elements) {
        const xpath = elementXPath(el);
        const style = computedStyleObject(el);
        const bbox = bboxFor(el, xpath, pageWidth, pageHeight);
        if (bbox.synthetic_body_bbox) syntheticBody = true;
        if (style.display.trim().toLowerCase() === 'none') {
            displayNone.push(xpath);
        }

        success.push({
            selector: `xpath/${xpath}`,
            outerHTML: el.outerHTML || '',
            computedStyle: style,
            bbox,
            scroll_x: window.scrollX,
            scroll_y: window.scrollY,
            is_display_none: style.display.trim().toLowerCase() === 'none',
            tag: el.tagName.toLowerCase(),
            textLength: (el.innerText || '').trim().length,
            hasImage: !!el.querySelector('img,svg,canvas,picture,video') ||
                ['img', 'svg', 'canvas', 'picture', 'video'].includes(el.tagName.toLowerCase())
        });
    }

    return {
        success,
        error: [],
        display_none_xpaths: displayNone,
        page_dimensions: {pageWidth, pageHeight},
        capture_metadata: {
            backend: 'dom_geometry',
            node_count: success.length,
            synthetic_body_bbox: syntheticBody
        }
    };
}"""
