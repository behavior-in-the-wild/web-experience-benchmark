/**
 * Technical context for Flask server-rendered applications
 */
export const FlaskContext = `
You know the following about Flask server-rendered applications.

### Characteristics

- Flask is a lightweight Python web framework (WSGI)
- Uses Jinja2 templating engine with \`{{ }}\` and \`{% %}\` syntax
- Project structure: \`templates/\`, \`static/\`, \`app.py\` or package structure
- Static assets served from \`static/\` directory via \`url_for('static', ...)\`
- Configuration via \`config.py\` or environment variables
- Blueprint pattern for modular application structure
- Extensions: Flask-Caching, Flask-Compress, Flask-Assets

### Common Optimizations

#### LCP

- Enable gzip compression with Flask-Compress: \`Compress(app)\`
- Use CDN for static assets to reduce TTFB
- Preload critical images in templates with \`<link rel="preload">\`
- Set \`fetchpriority="high"\` on LCP images
- Inline critical CSS in base template
- Use Flask-Caching for expensive view functions
- Minimize database queries in templates
- Use \`send_file()\` with proper cache headers
- Configure reverse proxy (nginx) for static file serving

#### CLS

- Always include width/height on images in Jinja templates
- Reserve space for dynamic content with CSS
- Configure font loading with \`font-display: swap\`
- Use size-adjusted fallback fonts
- Cache dimension calculations for dynamic images

#### INP

- Minimize JavaScript sent to client
- Use defer/async on script tags in templates
- Debounce client-side event handlers
- Avoid large synchronous client-side operations
- Use event delegation for repeated handlers
- Load third-party scripts asynchronously

### Flask-Specific Patterns

- Use Flask-Compress for automatic compression:
  \`\`\`python
  from flask_compress import Compress
  Compress(app)
  \`\`\`
- Configure static file caching:
  \`\`\`python
  app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000
  \`\`\`
- Use Flask-Assets for CSS/JS bundling:
  \`\`\`python
  from flask_assets import Environment, Bundle
  assets = Environment(app)
  \`\`\`
- Implement view caching with Flask-Caching
- Use \`url_for('static', filename=..., _external=True)\` with CDN prefix
- Template caching with \`TEMPLATES_AUTO_RELOAD = False\` in production

### Server Performance

- Use Gunicorn or uWSGI in production (not Flask dev server)
- Configure worker count based on CPU cores
- Use async views with Flask async support (Flask 2.0+)
- Implement database connection pooling
- Use Redis for session storage
- Profile with Flask-Profiler or py-spy

### Anti-patterns

- Do not use Flask dev server in production
- Avoid blocking I/O in request handlers
- Do not serve static files through Flask in production (use nginx)
- Avoid template rendering bottlenecks - cache expensive renders
- Do not skip compression middleware
- Avoid N+1 queries in templates - prefetch data
- Do not store sessions in cookies for large data
`;
