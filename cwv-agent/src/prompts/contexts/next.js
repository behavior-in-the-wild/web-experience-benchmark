/**
 * Technical context for Next.js applications
 */
export const NextContext = `
You know the following about Next.js applications.

### Characteristics

- Next.js is a React framework with built-in SSR, SSG, and app routing
- App Router (Next.js 13+): \`app/\` directory with \`page.tsx\`, \`layout.tsx\`
- Pages Router (legacy): \`pages/\` directory
- Built-in image optimization via \`next/image\`
- API routes for serverless functions
- Configuration via \`next.config.js\` or \`next.config.mjs\`
- Server Components (default in App Router) and Client Components
- Automatic code splitting and prefetching

### Common Optimizations

#### LCP

- Use \`next/image\` component with \`priority\` prop for LCP images
- Configure \`sizes\` prop for responsive images
- Use Server Components to reduce client JavaScript
- Leverage static generation (\`generateStaticParams\`) where possible
- Use streaming SSR with \`loading.tsx\` for faster TTFB
- Preload fonts with \`next/font\`
- Configure \`images.remotePatterns\` for external image optimization
- Use \`fetchPriority="high"\` via Image component's \`priority\` prop

#### CLS

- \`next/image\` automatically handles width/height
- Reserve space for dynamic content with skeleton components
- Use \`next/font\` with \`display: swap\` for optimal font loading
- Font subsetting is automatic with \`next/font\`
- Use \`placeholder="blur"\` for progressive image loading
- Avoid client-side only content above the fold

#### INP

- Minimize \`"use client"\` components - keep interactivity targeted
- Use Server Actions for form submissions
- Virtualize long lists with react-window
- Use \`useTransition\` for non-urgent updates
- Debounce search inputs and expensive handlers
- Lazy load client components with \`dynamic()\`
- Use \`React.memo\` for expensive render prevention

### Next.js-Specific Patterns

- Use \`next/image\` instead of \`<img>\` tags:
  \`\`\`jsx
  <Image src="/hero.jpg" width={1200} height={600} priority alt="Hero" />
  \`\`\`
- Use \`next/font\` for zero-layout-shift fonts:
  \`\`\`jsx
  import { Inter } from 'next/font/google'
  const inter = Inter({ subsets: ['latin'] })
  \`\`\`
- Use dynamic imports for heavy client components:
  \`\`\`jsx
  const Chart = dynamic(() => import('./Chart'), { ssr: false })
  \`\`\`
- Configure \`next.config.js\` for image domains
- Use Route Handlers for API endpoints
- Implement Incremental Static Regeneration (ISR) with \`revalidate\`

### Anti-patterns

- Do not use \`<img>\` tags - always use \`next/image\`
- Avoid \`"use client"\` on layouts - push it down to leaves
- Do not fetch data in client components if SSR is possible
- Avoid large client-side state - consider Server Components
- Do not skip \`priority\` prop on LCP images
- Avoid importing fonts via CSS - use \`next/font\`
- Do not disable automatic static optimization without reason
- Avoid blocking hydration with expensive client-side computation
`;
