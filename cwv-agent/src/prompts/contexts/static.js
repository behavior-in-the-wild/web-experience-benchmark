/**
 * Technical context for Static HTML websites
 * (vanilla HTML/CSS/JS without a specific framework)
 */
export const StaticHTMLContext = `
You know the following about static HTML websites.

### Characteristics

- Pure HTML, CSS, and JavaScript without build tools or frameworks
- Files are served directly by web servers (Apache, Nginx) or CDNs
- No server-side processing - all content is pre-written HTML
- Styling via plain CSS or simple preprocessors
- JavaScript may be vanilla or include small libraries (no heavy frameworks)
- May be hosted on any static hosting: GitHub Pages, Netlify, Vercel, S3, etc.
- Simple file structure with HTML files at root or in folders

### Common Optimizations

#### LCP

- Preload critical images using \`<link rel="preload" as="image">\`
- Use \`fetchpriority="high"\` on the LCP image element
- Use \`loading="eager"\` for above-the-fold images
- Use \`loading="lazy"\` for all below-the-fold images
- Inline critical CSS in \`<style>\` tags in the \`<head>\`
- Defer non-critical CSS using \`media="print" onload="this.media='all'"\`
- Minimize render-blocking resources in the \`<head>\`
- Use modern image formats (WebP, AVIF) with fallbacks
- Optimize images: compress, resize appropriately, use srcset
- Preconnect to external domains: \`<link rel="preconnect" href="...">\`
- Use a CDN for faster asset delivery

#### CLS

- Always specify width and height attributes on images and videos
- Use CSS aspect-ratio for responsive media containers
- Reserve space for ads, embeds, and dynamic content
- Use \`font-display: swap\` for web fonts
- Define fallback fonts with matching metrics (size-adjust)
- Avoid inserting content above existing viewport content
- Set explicit dimensions on iframes

#### INP

- Use \`defer\` attribute on all non-critical scripts
- Use \`async\` for independent scripts like analytics
- Place scripts at the end of \`<body>\` when not using defer/async
- Minimize JavaScript bundle sizes
- Break up long tasks using \`setTimeout\` or \`requestIdleCallback\`
- Use event delegation for repeated event handlers
- Debounce/throttle expensive handlers (scroll, resize, input)
- Avoid synchronous layout reads followed by writes (layout thrashing)
- Consider using Web Workers for CPU-intensive operations

### Best Practices

- Minify HTML, CSS, and JavaScript files
- Enable gzip/Brotli compression on the server
- Set appropriate Cache-Control headers for static assets
- Use content hashing in filenames for cache busting
- Optimize and compress all images before deployment
- Use SVG for icons and simple graphics
- Concatenate CSS/JS files to reduce HTTP requests (or use HTTP/2)
- Remove unused CSS rules
- Remove unused JavaScript code

### Common Issues to Check

- Unoptimized images (wrong format, oversized dimensions)
- Missing image dimensions causing layout shifts
- Render-blocking CSS/JS in the head
- Synchronous third-party scripts (analytics, ads, widgets)
- Missing preload/preconnect hints for critical resources
- Unminified assets increasing transfer size
- Missing compression (gzip/Brotli) on server
- Short or missing cache headers

### Anti-patterns

- Do not load large JavaScript frameworks for simple interactivity
- Avoid too many external HTTP requests
- Do not use blocking scripts in the \`<head>\`
- Avoid unoptimized images (large file size, wrong dimensions)
- Do not rely on JavaScript for critical content rendering
- Avoid viewport-dependent layout calculations without proper handling
`;
