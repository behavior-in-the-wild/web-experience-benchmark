/**
 * Technical context for Pelican static site generator
 */
export const PelicanContext = `
You know the following about Pelican static sites.

### Characteristics

- Pelican is a Python-based static site generator, popular for blogs
- Uses Jinja2 templating with \`{{ }}\` and \`{% %}\` syntax
- Content in Markdown, reStructuredText, or AsciiDoc
- Project structure: \`content/\`, \`themes/\`, \`output/\`, \`pelicanconf.py\`
- Configuration in \`pelicanconf.py\` (dev) and \`publishconf.py\` (prod)
- Themes in \`themes/\` directory with templates, static, and CSS
- Plugin system for extending functionality
- Build output goes to \`output/\` folder

### Common Optimizations

#### LCP

- Preload hero images in theme templates with \`<link rel="preload">\`
- Use explicit width/height on images in templates
- Inline critical CSS in base template
- Minimize render-blocking CSS from themes
- Use \`pelican-minify\` plugin for HTML/CSS/JS minification
- Configure image optimization in build pipeline
- Use \`pelican-assets\` or \`webassets\` for asset bundling
- Set \`loading="eager"\` and \`fetchpriority="high"\` on LCP images

#### CLS

- Always include width/height on img elements in templates
- Reserve space for dynamic content with CSS aspect-ratio
- Configure font loading with \`font-display: swap\`
- Use size-adjusted fallback fonts to prevent layout shifts
- Ensure theme CSS sets explicit dimensions on media containers

#### INP

- Defer non-critical JavaScript in templates
- Minimize theme JavaScript - many themes include unused jQuery
- Use vanilla JS instead of jQuery for simple interactions
- Load third-party scripts asynchronously
- Debounce scroll and resize event handlers
- Use event delegation for repetitive handlers

### Pelican-Specific Patterns

- Configure \`OUTPUT_RETENTION\` for incremental builds
- Use \`pelican-assets\` for CSS/JS bundling and minification:
  \`\`\`python
  PLUGINS = ['pelican-assets', ...]
  \`\`\`
- Configure \`STATIC_PATHS\` for asset handling
- Use \`pelican-image-process\` for automatic image optimization
- Add cache busting with \`CACHE_BUSTING\` settings
- Configure \`RELATIVE_URLS = False\` in publishconf.py
- Use \`pelican-minify\` for output minification

### Theme Optimization

- Audit theme templates for unused CSS/JS
- Remove jQuery if only used for simple DOM operations
- Optimize theme images (compress, proper formats)
- Minimize the number of HTTP requests for assets
- Use CSS Grid/Flexbox instead of heavy layout frameworks
- Inline small SVGs, sprite larger icon sets

### Anti-patterns

- Do not use unoptimized themes without auditing
- Avoid storing large unprocessed images in content/
- Do not skip image optimization before publishing
- Avoid loading heavy JavaScript libraries for simple features
- Do not use inline blocking scripts in templates
- Avoid themes with excessive CSS/JS dependencies
- Do not skip minification in production builds
`;
