/**
 * Technical context for Hugo static site generator
 */
export const HugoContext = `
You know the following about Hugo static sites.

### Characteristics

- Hugo is a Go-based static site generator known for extremely fast build times
- Uses Go templating with \`{{ }}\` syntax and template functions
- Project structure: \`content/\` for content, \`layouts/\` for templates, \`static/\` for assets
- Configuration in \`config.toml\`, \`config.yaml\`, or \`config.json\` (or \`hugo.toml\`)
- Themes in \`themes/\` directory with their own layouts and assets
- Asset pipeline via Hugo Pipes for SCSS, PostCSS, fingerprinting, minification
- Built-in image processing: resize, crop, fit, fill, filter
- Build output goes to \`public/\` folder
- Supports shortcodes for reusable content components

### Common Optimizations

#### LCP

- Use Hugo's image processing to generate optimized images: \`.Resize\`, \`.Fit\`, \`.Fill\`
- Preload critical images in \`baseof.html\` using \`<link rel="preload">\`
- Use \`loading="eager"\` and \`fetchpriority="high"\` on hero/LCP images
- Inline critical CSS using Hugo Pipes: \`resources.ToCSS | resources.Minify | resources.Fingerprint\`
- Use Hugo's \`resources.PostCSS\` for CSS optimization
- Generate WebP images with fallbacks using \`<picture>\` element
- Preconnect to external CDNs serving fonts or critical resources
- Use \`async\` or \`defer\` on non-critical scripts

#### CLS

- Use Hugo's image processing to get dimensions: \`.Width\`, \`.Height\`
- Always output width/height attributes on images
- Reserve space for ads and dynamic content with CSS aspect-ratio
- Configure web font loading with \`font-display: swap\`
- Use size-adjusted fallback fonts to prevent layout shifts
- Avoid inserting content dynamically above existing elements

#### INP

- Defer non-critical JavaScript with \`defer\` attribute
- Use Hugo Pipes to bundle and minify JS: \`resources.Concat | resources.Minify\`
- Break up long tasks using requestIdleCallback
- Load analytics/tracking scripts asynchronously
- Use event delegation for repetitive handlers
- Debounce scroll and resize event handlers

### Hugo-Specific Patterns

- Use Hugo Pipes for asset processing: \`{{ resources.Get "css/main.scss" | toCSS | minify | fingerprint }}\`
- Leverage built-in image processing instead of external tools
- Use \`partialCached\` for expensive partial renders
- Configure \`minify\` in config for HTML/CSS/JS minification
- Use \`--gc\` and \`--minify\` flags in production builds
- Generate responsive images with \`srcset\` using Hugo's image functions
- Use \`resources.Fingerprint\` for cache busting

### Anti-patterns

- Do not skip Hugo's built-in image processing for manual workflows
- Avoid storing large unprocessed images in \`static/\` (use \`assets/\` instead)
- Do not inline large CSS files - use Hugo Pipes to extract and link
- Avoid synchronous third-party scripts in the head
- Do not use heavy JavaScript frameworks for simple interactivity
- Avoid unoptimized theme assets - audit before adopting
`;
