/**
 * Technical context for Vue.js applications
 */
export const VueContext = `
You know the following about Vue.js applications.

### Characteristics

- Vue.js is a progressive JavaScript framework for building user interfaces
- Single-file components (.vue) with template, script, and style sections
- Project structure: \`src/components/\`, \`src/views/\`, \`src/router/\`, \`src/store/\`
- Configuration via \`vite.config.js\` (Vite) or \`vue.config.js\` (Vue CLI)
- Uses Vue Router for client-side routing
- State management via Pinia (Vue 3) or Vuex (Vue 2)
- Build tools: Vite (recommended), Vue CLI, or webpack
- Composition API (Vue 3) or Options API for component logic

### Common Optimizations

#### LCP

- Use \`v-if\` instead of \`v-show\` for content not immediately needed
- Lazy load routes with dynamic imports: \`() => import('./views/Page.vue')\`
- Preload critical component chunks
- Use \`<Suspense>\` with async components for better loading states
- Inline critical CSS or use critical CSS extraction plugins
- Preload LCP images with \`<link rel="preload">\`
- Use \`fetchpriority="high"\` on hero images
- Optimize bundle splitting to load critical code first
- Use Vite's built-in code splitting and tree shaking

#### CLS

- Set explicit dimensions on images (\`width\`, \`height\` attributes)
- Use CSS \`aspect-ratio\` for responsive containers
- Reserve space for async content (skeleton loaders)
- Use \`v-once\` for static content that won't change
- Configure font loading with \`font-display: swap\`
- Avoid v-if/v-show toggles that cause layout shifts above viewport

#### INP

- Use \`computed\` properties to avoid expensive recalculations
- Debounce/throttle input handlers with lodash or custom utilities
- Use \`shallowRef\` and \`shallowReactive\` for large datasets
- Virtualize long lists with \`vue-virtual-scroller\` or similar
- Break up long tasks using \`nextTick\` or \`requestIdleCallback\`
- Lazy load heavy components with \`defineAsyncComponent\`
- Use \`v-memo\` directive for expensive template sections

### Vue-Specific Patterns

- Use async components: \`defineAsyncComponent(() => import('./HeavyComponent.vue'))\`
- Leverage Vite's automatic code splitting
- Configure \`build.rollupOptions.output.manualChunks\` for optimal splitting
- Use \`<KeepAlive>\` to cache component state but watch memory usage
- Lazy load images with \`loading="lazy"\` or vue-lazyload
- Use \`<Teleport>\` for modals to avoid layout impact
- Configure \`build.cssCodeSplit: true\` for per-route CSS

### Anti-patterns

- Avoid reactive wrappers on large immutable data
- Do not use deep watching unnecessarily - use specific watchers
- Avoid inline handlers with complex logic - use methods
- Do not render large lists without virtualization
- Avoid hydration mismatches in SSR mode
- Do not import entire libraries - use tree-shakeable imports
- Avoid synchronous blocking operations in lifecycle hooks
`;
