"""
This script uses certain deterministic rules, to convert any input HTML to Acrite editor compatible format.
Rule-1: The immediate child of the <body> tag should be a <div> tag with class "acr-container" or "container". 
        If there are many children (none of them have class "acr-container"/ "container")/ or child doesn't have this class,
        then create a new <div> tag with class "acr-container" wrapping all the children.
        If at least one chid has class "acr-container" or "container", then add "acr-container" class to all other children as well.

Rule-2: Check all the elements with class "acr-container"/ "container", their immediate children should have class "acr-structure" or "structure".
        If any child element doesn't have class "acr-structure" or "structure", then add a new <div> tag with class "acr-structure"
        wrapping that element.

Rule-3: Check all the elements with class "acr-structure" or "structure", their immediate parent class should be "acr-container" or "container".
        If any element doesn't have class "acr-container" or "container" as parent, then add a new <div> tag with class "acr-container"
        wrapping that element.

Rule-4: Check all the elements with class "acr-fragment", their immediate parent class should be either "colspan"
        or "container-wrapper". If not , then add a new <div> tag with class "container-wrapper" wrapping that element.

Rule-5: Every fragment's ancestor should be "acr-structure" is taken care of by Rules 1 and 2.

Given an input HTML file path, the script creates a modified Acrite compatible HTML and writes it at the given output file path.
"""

import argparse
import glob
import os
import sys
from bs4 import BeautifulSoup


# ---------------------------------------------------------------------------
# CSS class constants (mirrored from check_Acrite_compatibility.jsx)
# ---------------------------------------------------------------------------
CONTAINER_CLASSES = {"acr-container", "container"}
STRUCTURE_CLASSES = {"acr-structure", "structure"}
FRAGMENT_CLASS = "acr-fragment"
CONTAINER_WRAPPER_CLASS = "container-wrapper"


def _get_classes(tag):
    """Safely get a tag's class list (always returns a list)."""
    if tag is None or not hasattr(tag, "get"):
        return []
    classes = tag.get("class", [])
    if isinstance(classes, str):
        return classes.split()
    return list(classes) if classes else []


def _has_any_class(tag, class_set):
    """Check if a tag has any class from the given set."""
    return bool(class_set & set(_get_classes(tag)))


def _is_element(node):
    """Return True if the node is an actual HTML element (not text/comment)."""
    return node is not None and node.name is not None


def _element_children(tag):
    """Return only element children (skip text nodes, comments, whitespace)."""
    return [c for c in tag.children if _is_element(c)]


# ---------------------------------------------------------------------------
# Rule implementations
# ---------------------------------------------------------------------------

def apply_rule_1(soup):
    """
    Rule 1: body's immediate children must include an acr-container.
    
    - If NO child has class acr-container/container:
        Wrap ALL body children in a single new <div class="acr-container">.
    - If at least one child already has acr-container/container:
        Add "acr-container" class to every other immediate child that lacks it.
    """
    body = soup.find("body")
    if not body:
        return 0

    children = _element_children(body)
    if not children:
        return 0

    has_container = any(_has_any_class(c, CONTAINER_CLASSES) for c in children)
    changes = 0

    if has_container:
        # Add acr-container to siblings that don't have it
        for child in children:
            if not _has_any_class(child, CONTAINER_CLASSES):
                classes = _get_classes(child)
                classes.append("acr-container")
                child["class"] = classes
                changes += 1
    else:
        # Wrap everything in a new acr-container
        wrapper = soup.new_tag("div")
        wrapper["class"] = ["acr-container"]
        # Collect all body contents (elements + text nodes)
        contents = list(body.children)
        for node in contents:
            wrapper.append(node.extract())
        body.append(wrapper)
        changes += 1

    return changes


def apply_rule_2(soup):
    """
    Rule 2: Every immediate child of an acr-container/container must be
    an acr-structure/structure. If not, wrap it in <div class="acr-structure">.
    """
    changes = 0
    # Re-query each iteration because wrapping modifies the tree
    containers = soup.find_all(
        lambda tag: tag.name is not None and _has_any_class(tag, CONTAINER_CLASSES)
    )

    for container in containers:
        for child in _element_children(container):
            if not _has_any_class(child, STRUCTURE_CLASSES):
                wrapper = soup.new_tag("div")
                wrapper["class"] = ["acr-structure"]
                child.wrap(wrapper)
                changes += 1

    return changes


def apply_rule_3(soup):
    """
    Rule 3: Every acr-structure/structure element's immediate parent must have
    class acr-container/container. If not, wrap the structure in
    <div class="acr-container">.
    """
    changes = 0
    structures = soup.find_all(
        lambda tag: tag.name is not None and _has_any_class(tag, STRUCTURE_CLASSES)
    )

    for structure in structures:
        parent = structure.parent
        if parent and _is_element(parent) and not _has_any_class(parent, CONTAINER_CLASSES):
            wrapper = soup.new_tag("div")
            wrapper["class"] = ["acr-container"]
            structure.wrap(wrapper)
            changes += 1

    return changes


def apply_rule_4(soup):
    """
    Rule 4: Every acr-fragment's immediate parent must have "colspan" in its
    class string, or have the class "container-wrapper". If not, wrap the
    fragment in <div class="container-wrapper">.
    """
    changes = 0
    fragments = soup.find_all(
        lambda tag: tag.name is not None and FRAGMENT_CLASS in _get_classes(tag)
    )

    for fragment in fragments:
        parent = fragment.parent
        if not (parent and _is_element(parent)):
            continue

        parent_classes = _get_classes(parent)
        parent_class_str = " ".join(parent_classes)
        in_colspan = "colspan" in parent_class_str
        in_wrapper = CONTAINER_WRAPPER_CLASS in parent_classes

        if not in_colspan and not in_wrapper:
            wrapper = soup.new_tag("div")
            wrapper["class"] = [CONTAINER_WRAPPER_CLASS]
            fragment.wrap(wrapper)
            changes += 1

    return changes


# ---------------------------------------------------------------------------
# Main conversion function
# ---------------------------------------------------------------------------

def make_acrite_compatible(html_string):
    """
    Apply all Acrite compatibility rules to an HTML string and return
    the modified HTML.

    Rules are applied in order: 1 → 3 → 2 → 4.
    
    Rule 1 ensures a top-level container exists.
    Rule 3 ensures every structure has a container parent (may create nested containers).
    Rule 2 ensures every container's children are structures (handles new containers from Rule 3).
    Rule 4 ensures every fragment sits inside a colspan or container-wrapper.
    """
    soup = BeautifulSoup(html_string, "html5lib")

    total_changes = 0

    # Rule 1: body > acr-container
    changes = apply_rule_1(soup)
    total_changes += changes

    # Rule 2: container children must be structures
    changes = apply_rule_2(soup)
    total_changes += changes

    # Rule 3: structure parent must be container (may create new containers)
    changes = apply_rule_3(soup)
    total_changes += changes

    # Rule 4: fragment parent must be colspan or container-wrapper
    changes = apply_rule_4(soup)
    total_changes += changes

    return str(soup), total_changes


def convert_file(input_path, output_path):
    """Read an HTML file, make it Acrite-compatible, and write the result."""
    with open(input_path, "r", encoding="utf-8") as f:
        html_string = f.read()

    result_html, total_changes = make_acrite_compatible(html_string)

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(result_html)

    return total_changes


# ---------------------------------------------------------------------------
# File resolution — supports single files, directories, and glob/wildcard patterns
# ---------------------------------------------------------------------------

def resolve_html_files(input_pattern):
    """
    Resolve an input path into a list of .html/.htm file paths.
    Accepts:
      - A single file path:   "template.html"
      - A directory path:     "/path/to/templates/"
      - A glob/wildcard:      "/path/to/templates/*.html"
                              "/data/**/*.html"  (recursive)

    Returns:
        list[str]: sorted list of absolute file paths.
    """
    resolved = os.path.abspath(input_pattern)

    # 1) Existing single file
    if os.path.isfile(resolved):
        return [resolved]

    # 2) Existing directory → all .html/.htm files recursively
    if os.path.isdir(resolved):
        files = []
        for root, _, names in os.walk(resolved):
            for name in names:
                if name.lower().endswith((".html", ".htm")):
                    files.append(os.path.join(root, name))
        return sorted(files)

    # 3) Glob / wildcard pattern
    if any(c in input_pattern for c in "*?[]{}"):
        matches = sorted(glob.glob(input_pattern, recursive=True))
        return [
            os.path.abspath(f)
            for f in matches
            if os.path.isfile(f) and f.lower().endswith((".html", ".htm"))
        ]

    # Nothing matched
    return []


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Convert HTML file(s) to Acrite-compatible format.",
        epilog="""Examples:
  python make_html_acrite_compatible.py --input input.html --output output.html
  python make_html_acrite_compatible.py --input /path/to/input_dir/ --output /path/to/output_dir/
  python make_html_acrite_compatible.py --input '/data/htmls/*.html' --output /path/to/output_dir/
  python make_html_acrite_compatible.py --input '/data/**/*.html' --output /path/to/output_dir/
""",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--input",
        help="Input HTML file, directory, or glob/wildcard pattern (e.g. '/path/*.html').",
        default="/mnt/localssd/parul/image-critic-data/screenshots_subset_300_generated_htmls/*/*.html",
    )
    parser.add_argument(
        "--output",
        help="Output HTML file (single-file mode) or output directory (batch mode).",
        default="/mnt/localssd/parul/image-critic-data/screenshots_subset_300_generated_htmls_fixed"
    )
    args = parser.parse_args()

    files = resolve_html_files(args.input)

    if not files:
        print(f"Error: No HTML files found matching '{args.input}'", file=sys.stderr)
        sys.exit(1)

    output_path = os.path.abspath(args.output)

    # --- Single-file mode ---
    if len(files) == 1 and os.path.isfile(os.path.abspath(args.input)):
        # If output looks like a directory (trailing slash or existing dir), put file inside it
        if args.output.endswith(os.sep) or os.path.isdir(output_path):
            os.makedirs(output_path, exist_ok=True)
            out_file = os.path.join(output_path, os.path.basename(files[0]))
        else:
            out_file = output_path

        changes = convert_file(files[0], out_file)
        print(f"Done. {changes} transformation(s) applied.")
        print(f"  Input:  {files[0]}")
        print(f"  Output: {out_file}")
        return

    # --- Batch mode (multiple files) ---
    os.makedirs(output_path, exist_ok=True)

    print(f"Processing {len(files)} file(s) → {output_path}\n")

    total_changes = 0
    num_changed_files = 0
    for filepath in files:
        filename = os.path.basename(filepath)
        out_file = os.path.join(output_path, filename)
        try:
            changes = convert_file(filepath, out_file)
            total_changes += changes
            if changes > 0:
                num_changed_files += 1
            status = f"{changes} change(s)" if changes > 0 else "no changes"
            print(f"  {filename}: {status}")
        except Exception as e:
            print(f"  {filename}: ERROR - {e}", file=sys.stderr)

    print(f"\nDone. {len(files)} file(s) processed, {total_changes} total transformation(s) across {num_changed_files} files.")


if __name__ == "__main__":
    main()
