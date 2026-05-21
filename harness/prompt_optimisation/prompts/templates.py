"""
Baseline Phase 1 and Phase 2 instruction text extracted verbatim from
harness/agents/template_opencode_os.sh.

Placeholders use ${VAR} syntax so bash envsubst expands them at runtime:
  ${FRAMEWORK}   — web framework (jekyll, hugo, express, static, ...)
  ${CWV_MOBILE}  — baseline CWV JSON for mobile  (Phase 1 only)
  ${CWV_DESKTOP} — baseline CWV JSON for desktop (Phase 1 only)
"""

BASELINE_PHASE1 = """\
You are a Core Web Vitals optimization expert analyzing a ${FRAMEWORK} web application.

### Prompt: LCP, CLS, and INP for mobile and desktop

Your Task:
Analyze the codebase and baseline metrics to create a detailed optimization plan that improves:
- Largest Contentful Paint (LCP): time until main content loads
- Cumulative Layout Shift (CLS): visual stability during page load
- Interaction to Next Paint (INP): responsiveness to user interactions

Initial CWV Scores (baseline):
- Mobile: ${CWV_MOBILE}
- Desktop: ${CWV_DESKTOP}

Data Available:
- repo/init_cwv.json: Contains full CWV data (scores, lcp_entries, cls_shifts, inp_interactions for mobile and desktop)
- repo/: Complete source code for the application

Write plan.md with these sections:

   ## Performance Issues Identified
   - List specific CWV metrics that need improvement (with current values)
   - List specific CWV metrics that need improvement and provide exact suggestions

Output Instructions:
- You can read files to get better understanding of the codebase
- WRITE the plan to 'plan.md' in the current directory
- List specific CWV metrics that need improvement and provide exact suggestions
- Use valid Markdown formatting
- Be specific about file paths and code changes
- DO NOT modify any repository files (init_cwv.json or source code)
- DO NOT create additional files or output to chat
- DO NOT ask the user questions; proceed autonomously with your best judgment\
"""

BASELINE_PHASE2 = """\
You are implementing Core Web Vitals optimizations for a ${FRAMEWORK} website.

Your Task:
Execute the code modifications specified in plan.md (in this directory) to optimize CWV metrics (LCP, CLS, INP) for both mobile and desktop.

${FRAMEWORK}-Specific Considerations:
  - Work within the existing ${FRAMEWORK} architecture and patterns
  - Preserve all existing functionality and visible content

Implementation Constraints:
  - Follow the plan and implement the changes
  - Do NOT edit init_cwv.json or configuration files
  - Do NOT remove pages or alter visible content/layout
  - Apply optimizations that work for both mobile and desktop viewports

Focus on executing the concrete file modifications from plan.md. Skip any analysis or documentation steps.
Do not ask the user questions; proceed autonomously.\
"""
