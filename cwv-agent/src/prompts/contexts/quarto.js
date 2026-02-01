/**
 * Technical context for Quarto publishing system
 */
export const QuartoContext = `
You know the following about Quarto publications and websites.

### Characteristics

- Quarto is an open-source scientific publishing system
- Builds on Pandoc for document conversion
- Uses YAML frontmatter and Markdown content
- Project structure: \`_quarto.yml\` config, \`.qmd\` source files
- Outputs: HTML, PDF, Word, presentations, books, websites
- Supports R, Python, Julia, and Observable JS computations
- Built-in support for Jupyter notebooks
- Static site generation for websites and blogs

### Common Optimizations

#### LCP

- Preload critical images with \`<link rel="preload">\`
- Use Quarto's figure options for image dimensions
- Optimize code output rendering - limit display rows
- Minimize render-blocking CSS from themes
- Use \`freeze: true\` to cache computation outputs
- Inline critical CSS for above-the-fold content
- Set \`format: html:\` options for optimized output

#### CLS

- Always specify figure dimensions in chunk options
- Use \`fig-width\` and \`fig-height\` for plots
- Reserve space for code outputs with fixed dimensions
- Configure font loading properly in theme
- Avoid dynamic content insertion above existing elements

#### INP

- Minimize JavaScript in the output
- Use \`execute: cache: true\` to avoid regenerating content
- Defer non-critical scripts
- Use static output where interactivity isn't needed
- Minimize Observable JS reactivity for simple displays

### Quarto-Specific Patterns

- Configure \`_quarto.yml\` for site-wide optimization
- Use \`html:\` format options:
  - \`embed-resources: true\` for self-contained HTML
  - \`minimal: true\` for reduced HTML/CSS
  - \`toc-depth:\` to limit TOC rendering
- Use \`freeze: true\` in YAML to cache computations
- Optimize figure output with chunk options:
  - \`fig-format: "svg"\` for scalable graphics
  - \`fig-dpi: 96\` for web-optimized images
- Use \`code-fold: true\` to hide code by default
- Configure \`lightbox: true\` for image galleries

### Computation Optimization

- Cache expensive computations with \`cache: true\`
- Use \`freeze: auto\` for incremental rendering
- Limit output rows with \`df_print:\` options
- Use static images instead of interactive plots when possible
- Profile compute-heavy documents and optimize
- Use \`include: false\` for setup chunks

### Anti-patterns

- Do not embed large datasets in the HTML output
- Avoid uncached expensive computations
- Do not use interactive widgets unless necessary
- Avoid unoptimized high-resolution figures
- Do not skip image dimension specifications
- Avoid loading heavy JavaScript libraries for simple displays
`;
