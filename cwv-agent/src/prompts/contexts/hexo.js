/**
 * Technical context for Hexo static site generator
 */
export const HexoContext = `
You know the following about Hexo static sites.

### Characteristics

- Hexo is a Node.js-based static site generator, popular for blogs
- Uses EJS, Pug, Nunjucks, or Swig templating engines (EJS is default)
- Project structure: \`source/\` for content, \`themes/\` for templates, \`scaffolds/\` for templates
- Configuration in \`_config.yml\` and theme-specific \`themes/<name>/_config.yml\`
- Assets go in \`source/\` and are copied to \`public/\` on build
- Supports asset folders per post with \`post_asset_folder: true\`
- Common plugins: hexo-generator-feed, hexo-browsersync, hexo-asset-image
- Build output is in \`public/\` folder
- Extensible through plugins and themes from npm

### Common Optimizations

#### LCP

- Preload hero/banner images in theme layouts using \`<link rel="preload">\`
- Use explicit width/height on images to prevent layout shifts
- Minimize render-blocking CSS - inline critical styles in \`<head>\`
- Use \`hexo-filter-optimize\` plugin for asset optimization
- Configure image lazy loading with \`loading="lazy"\` for below-fold images
- Use \`loading="eager"\` and \`fetchpriority="high"\` for LCP images
- Preconnect to external CDNs serving fonts or critical resources
- Optimize theme CSS - remove unused selectors
- Use modern image formats (WebP) via \`hexo-image-sizes\` or similar

#### CLS

- Always include width and height on img and video elements
- Reserve space for ads and dynamic content
- Use CSS aspect-ratio for responsive images/videos
- Configure web font loading with \`font-display: swap\`
- Add size-adjusted fallback fonts to minimize FOIT/FOUT shifts
- Avoid dynamically injecting content above existing elements

#### INP

- Defer non-essential JavaScript: use \`defer\` attribute
- Use \`hexo-filter-optimize\` to concatenate and minify JS
- Break up long tasks with requestIdleCallback
- Minimize theme JavaScript complexity
- Load analytics and tracking scripts asynchronously
- Use event delegation for comment systems and interactive elements
- Debounce scroll and resize event handlers

### Hexo-Specific Patterns

- Use \`hexo-all-minifier\` for HTML, CSS, JS, and image minification
- Configure \`hexo-asset-pipeline\` for asset optimization
- Use CDN URLs via \`url_for()\` helper with CDN configuration
- Leverage \`hexo-renderer-marked\` options for optimized HTML output
- Use \`hexo-generator-sitemap\` and \`hexo-generator-feed\` for SEO
- Configure caching headers in deployment platform (Netlify, Vercel, etc.)
- Use \`hexo-filter-responsive-images\` for srcset generation

### Theme Optimization

- Audit theme CSS and remove unused styles (PurgeCSS)
- Minimize theme JS - many themes ship with unused jQuery plugins
- Replace heavy libraries (jQuery) with vanilla JS when possible
- Optimize theme images and icons (SVG sprites, icon fonts → inline SVG)
- Use CSS Grid/Flexbox instead of heavy layout libraries

### Anti-patterns

- Do not load jQuery just for simple DOM operations
- Avoid unoptimized theme assets - audit before using third-party themes
- Do not use synchronous analytics/tracking in the head
- Avoid multiple render-blocking stylesheets
- Do not skip image optimization before adding to posts
- Avoid heavy client-side rendering for static content
`;
