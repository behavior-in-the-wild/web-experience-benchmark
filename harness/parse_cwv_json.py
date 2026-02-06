#!/usr/bin/env python3
"""
Parse CWV JSON output from cwv_benchmark.py and print a summary.
Handles log lines that may appear before the JSON in the output.

Usage: python3 parse_cwv_json.py /path/to/cwv_output.json
"""
import json
import re
import sys

def rating(metric, val):
    thresholds = {
        'LCP': (2500, 4000),
        'CLS': (0.1, 0.25),
        'INP': (200, 500),
        'TTFB': (800, 1800),
    }
    if metric not in thresholds or val is None:
        return ''
    good, poor = thresholds[metric]
    if val <= good:
        return 'GOOD'
    elif val <= poor:
        return 'NEEDS IMPROVEMENT'
    else:
        return 'POOR'

def main():
    if len(sys.argv) < 2:
        print("Usage: parse_cwv_json.py <json_file>", file=sys.stderr)
        sys.exit(1)
    
    try:
        with open(sys.argv[1]) as f:
            content = f.read()
        
        # Find the JSON object (starts with { and ends with })
        match = re.search(r'\{.*\}', content, re.DOTALL)
        if not match:
            print('No JSON found in output')
            sys.exit(0)
        
        d = json.loads(match.group())
        a = d.get('aggregated', {})
        
        lcp = a.get('LCP_p75')
        cls_val = a.get('CLS_median')
        inp = a.get('INP_p75')
        ttfb = a.get('TTFB_median')
        fcp = a.get('FCP_median')
        
        lines = []
        lines.append(f'LCP (p75): {lcp} ms [{rating("LCP", lcp)}]' if lcp else 'LCP: N/A')
        lines.append(f'CLS (median): {cls_val} [{rating("CLS", cls_val)}]' if cls_val is not None else 'CLS: N/A')
        lines.append(f'INP (p75): {inp} ms [{rating("INP", inp)}]' if inp else 'INP: N/A')
        lines.append(f'TTFB (median): {ttfb} ms [{rating("TTFB", ttfb)}]' if ttfb else 'TTFB: N/A')
        lines.append(f'FCP (median): {fcp} ms' if fcp else 'FCP: N/A')
        print('\n'.join(lines))
        
    except Exception as e:
        print(f'Error parsing CWV: {e}')
        sys.exit(1)

if __name__ == '__main__':
    main()
