/**
 * Default technical context for unknown or unspecified frameworks
 * Provides generic web performance guidance applicable to any web page
 */
export const DefaultContext = `
You are analyzing a web page for Core Web Vitals optimization. The specific framework or technology stack has not been identified, so provide general web performance guidance.

### General Approach

Apply fundamental web performance principles that work across all web technologies:

### LCP (Largest Contentful Paint) Optimizations

**Resource Loading:**
- Preload critical resources: \`<link rel="preload" as="image/style/font" href="...">\`
- Use \`fetchpriority="high"\` on the LCP element (usually hero image or main heading)
- Preconnect to critical origins: \`<link rel="preconnect" href="...">\`
- Reduce server response time (TTFB) - consider CDN, caching, or server optimization

**Render Blocking:**
- Inline critical CSS in the \`<head>\`
- Defer non-critical CSS using media query technique or JavaScript
- Use \`defer\` or \`async\` on non-critical scripts
- Minimize render-blocking resources before first paint

**Image Optimization:**
- Use modern formats (WebP, AVIF) with fallbacks
- Properly size images - don't serve oversized images
- Use \`loading="lazy"\` for below-fold images
- Use \`loading="eager"\` for above-fold/LCP images
- Compress images appropriately

### CLS (Cumulative Layout Shift) Optimizations

**Dimension Stability:**
- Always specify width and height on images and videos
- Use CSS \`aspect-ratio\` for responsive containers
- Reserve space for ads, embeds, and dynamic content
- Set explicit dimensions on iframes

**Font Loading:**
- Use \`font-display: swap\` for web fonts
- Preload critical fonts
- Use fallback fonts with similar metrics (size-adjust)

**Dynamic Content:**
- Avoid inserting content above existing content
- Use transforms instead of layout-affecting properties for animations
- Reserve space for lazy-loaded content

### INP (Interaction to Next Paint) Optimizations

**JavaScript Execution:**
- Break up long tasks (>50ms) using \`setTimeout\`, \`requestIdleCallback\`, or \`scheduler.yield()\`
- Defer non-critical JavaScript execution
- Use web workers for CPU-intensive operations
- Minimize main thread blocking

**Event Handling:**
- Debounce/throttle expensive handlers (scroll, resize, input)
- Use event delegation for repeated handlers
- Avoid layout thrashing (interleaved reads/writes)
- Keep event handlers fast and focused

**Third-party Scripts:**
- Load analytics and tracking asynchronously
- Use \`async\` or \`defer\` on third-party scripts
- Consider lazy loading third-party widgets
- Self-host critical third-party resources when possible

### General Best Practices

- Enable text compression (gzip/Brotli) on the server
- Set appropriate Cache-Control headers for static assets
- Use HTTP/2 or HTTP/3 for multiplexing
- Minimize the number of HTTP requests
- Remove unused CSS and JavaScript
- Minify HTML, CSS, and JavaScript

### Anti-patterns

- Avoid unoptimized images (wrong format, oversized)
- Do not use blocking scripts in the \`<head>\`
- Avoid layout-causing property changes during animations
- Do not ignore Core Web Vitals data from real users (CrUX)
- Avoid loading resources that aren't needed on initial page load
`;
