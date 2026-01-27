/**
 * Technical context for React applications
 */
export const ReactContext = `
You know the following about React applications.

### Characteristics

- React is a JavaScript library for building component-based user interfaces
- Uses JSX syntax combining JavaScript and HTML-like markup
- Project structure: \`src/components/\`, \`src/pages/\`, \`src/hooks/\`, \`src/context/\`
- State management via useState, Context API, Redux, Zustand, or Jotai
- Uses React Router for client-side routing (for SPAs)
- Build tools: Vite, Create React App (deprecated), or webpack
- Server-side rendering via frameworks like Next.js or Remix
- React 18+ features: concurrent rendering, Suspense, transitions

### Common Optimizations

#### LCP

- Use \`React.lazy()\` and \`Suspense\` for code splitting
- Preload critical component chunks with \`/* webpackPreload: true */\`
- Use streaming SSR (\`renderToPipeableStream\`) for faster TTFB
- Preload LCP images with \`<link rel="preload">\`
- Set \`fetchpriority="high"\` on hero/LCP images
- Inline critical CSS or use CSS-in-JS critical extraction
- Lazy load below-fold components
- Optimize bundle size with tree shaking and dead code elimination

#### CLS

- Set explicit width/height on images
- Use CSS \`aspect-ratio\` for responsive media containers
- Reserve space for async content with skeleton components
- Avoid conditional rendering that shifts content (use opacity/visibility)
- Configure font loading with \`font-display: swap\`
- Use placeholders for dynamically loaded content

#### INP

- Use \`useMemo\` and \`useCallback\` to avoid expensive recalculations
- Virtualize long lists with \`react-window\` or \`react-virtualized\`
- Use \`useTransition\` for non-urgent state updates
- Use \`useDeferredValue\` for expensive derived values
- Debounce/throttle input handlers
- Break up long renders with \`startTransition\`
- Use \`memo()\` to prevent unnecessary re-renders
- Avoid inline object/function creation in JSX props

### React-Specific Patterns

- Dynamic imports: \`const Component = React.lazy(() => import('./Component'))\`
- Use \`React.memo\` for pure functional components
- Leverage automatic batching in React 18
- Use \`Profiler\` API to identify render bottlenecks
- Configure webpack's \`splitChunks\` or Vite's \`manualChunks\`
- Use \`ErrorBoundary\` components for graceful error handling
- Consider Server Components (React 19+) for reduced client JS

### Anti-patterns

- Avoid state updates in useEffect causing render loops
- Do not use index as key in lists that can reorder
- Avoid prop drilling - use Context or state management
- Do not render large lists without virtualization
- Avoid creating new object references on every render
- Do not use layout effects (\`useLayoutEffect\`) for non-DOM work
- Avoid synchronous blocking operations in render
- Do not ignore React 18 concurrent features for interactive apps
`;
