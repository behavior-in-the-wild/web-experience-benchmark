/**
 * Technical context for Express.js server-rendered applications
 */
export const ExpressContext = `
You know the following about Express.js server-rendered applications.

### Characteristics

- Express.js is a minimal Node.js web framework for server-side applications
- Server-side rendering with template engines (EJS, Pug, Handlebars, etc.)
- Project structure: \`routes/\`, \`views/\`, \`public/\`, \`middleware/\`
- Static assets served from \`public/\` directory via \`express.static()\`
- Configuration via environment variables and config files
- Middleware pattern for request processing
- Can serve API endpoints and rendered HTML pages

### Common Optimizations

#### LCP

- Enable gzip/Brotli compression: \`app.use(compression())\`
- Use CDN for static assets to reduce TTFB
- Preload critical images in rendered HTML with \`<link rel="preload">\`
- Set \`fetchpriority="high"\` on LCP images
- Inline critical CSS in the template output
- Use caching middleware (Redis, memory) for rendered pages
- Minimize database queries blocking response
- Use streaming responses (\`res.write()\`) for progressive rendering
- Set proper Cache-Control headers for static assets

#### CLS

- Always include width/height on images in templates
- Reserve space for dynamic content with CSS
- Configure font loading with \`font-display: swap\`
- Use size-adjusted fallback fonts
- Generate dimensions for images during processing

#### INP

- Minimize JavaScript sent to client
- Use defer/async on script tags in templates
- Debounce client-side event handlers
- Avoid large synchronous client-side operations
- Use event delegation for repeated handlers
- Load third-party scripts asynchronously

### Express-Specific Patterns

- Use \`compression()\` middleware for gzip/Brotli
- Configure \`express.static()\` with \`maxAge\` for caching
- Use \`helmet()\` middleware for security headers
- Template engine optimization: precompile views
- Use response caching with \`apicache\` or Redis
- Configure \`etag\` for conditional requests
- Use \`express-static-gzip\` for pre-compressed assets
- Set \`Cache-Control\` headers on static routes

### Server Performance

- Use clustering with Node.js cluster module
- Implement response caching for expensive renders
- Use async/await properly to avoid blocking
- Pool database connections
- Monitor and optimize slow middleware
- Use PM2 or similar for process management
- Implement request timeouts

### Anti-patterns

- Do not block the event loop with synchronous operations
- Avoid rendering large datasets without pagination
- Do not serve static assets without caching headers
- Avoid inline blocking scripts in templates
- Do not skip compression middleware
- Avoid waterfall database queries - use parallel/batch
- Do not store session data in memory in production
`;
