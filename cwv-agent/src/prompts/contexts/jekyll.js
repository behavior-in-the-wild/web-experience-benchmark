/**
 * Technical context for Jekyll static site generator
 */
export const JekyllContext = `
You know the following about Jekyll static sites.

### Characteristics

- Jekyll is a Ruby-based static site generator commonly hosted on GitHub Pages
- Files are compiled at build time into static HTML, CSS, and JavaScript
- Uses Liquid templating language with \`{% %}\` and \`{{ }}\` syntax
- Project structure includes \`_layouts/\`, \`_includes/\`, \`_posts/\`, \`_data/\`, \`_sass/\`
- Configuration is in \`_config.yml\`
- Assets are typically in \`assets/\` folder with SCSS support via \`_sass/\`
- Common plugins: jekyll-feed, jekyll-seo-tag, jekyll-sitemap, jekyll-paginate
- GitHub Pages has a limited set of allowed plugins for security
- Static assets are served directly by the hosting platform (GitHub Pages, Netlify, etc.)
- No server-side processing at runtime - everything is pre-built

### Common Optimizations

#### LCP

- Ensure hero images have explicit width and height attributes
- Preload critical images using \`<link rel="preload">\` in the head
- Use responsive images with srcset for optimal sizing
- Minimize CSS in \`<head>\` - move non-critical styles to separate files
- Inline critical CSS for above-the-fold content in the layout
- Use \`loading="eager"\` for LCP images, \`loading="lazy"\` for below-fold
- Minimize the number of blocking stylesheets in the head
- Consider inlining small fonts or using system font stack
- Preconnect to external domains serving critical resources
- Optimize image formats: use WebP with fallbacks

#### CLS

- Always specify width and height on images and video elements
- Reserve space for dynamically loaded content (ads, embeds)
- Use CSS aspect-ratio boxes for responsive media
- Avoid inserting content above existing content dynamically
- Use font-display: swap with size-adjusted fallback fonts
- Ensure web fonts have matching fallback font metrics
- Set explicit dimensions on iframe embeds

#### INP

- Defer non-critical JavaScript using \`defer\` attribute
- Minimize JavaScript bundle size through tree shaking
- Break up long tasks using requestIdleCallback or setTimeout
- Avoid heavy DOM manipulation on user interactions
- Use event delegation for multiple similar event handlers
- Debounce/throttle expensive event handlers (scroll, resize)
- Consider using Web Workers for CPU-intensive operations
- Minimize third-party scripts and load them asynchronously

### Jekyll-Specific Patterns

- Use Jekyll's \`asset_path\` filter for cache-busting: \`{{ '/assets/main.css' | asset_path }}\`
- Leverage \`jekyll-assets\` gem for asset pipeline optimization
- Use \`include_cached\` for frequently used includes
- Minimize Liquid template complexity - prefer includes over inline logic
- Use \`jekyll-compress-html\` layout to minify HTML output
- Configure \`sass: style: compressed\` in _config.yml for CSS minification
- Use \`jekyll-minifier\` for additional optimization

### Anti-patterns

- Do not use too many plugins - each adds build complexity
- Avoid large \`_data\` files that slow build and increase page weight
- Do not inline large SVGs repeatedly - use includes or external files
- Avoid synchronous third-party scripts in the head
- Do not rely on JavaScript for critical above-the-fold rendering
- Avoid unoptimized images in posts - process them before adding
`;
