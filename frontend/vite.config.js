import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  base: '/ui/',
  build: {
    outDir: '../app/ui',
    emptyOutDir: true,
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-utils/noRepoWrites.ts'],
    // EXPLICIT scope — do NOT fall back to vitest's default recursive glob. That default would
    // also pick up any gitignored scratch test dropped under frontend/tmp/ (see .gitignore) and
    // fail the gate on a file nobody meant to ship — the exact defect this line exists to close
    // (found in a sibling repo's quality-gate rollout, 2026-08-27). Only files inside src/ are
    // ever gate inputs.
    include: ['src/**/*.{test,spec}.{js,mjs,cjs}'],
    exclude: ['node_modules/**', 'dist/**', 'coverage/**', 'tmp/**'],
    // G3 vacuous-gate fix (see .claude/CLAUDE.md `## Code quality gates`): this used to be
    // `passWithNoTests: true` with the justification "0 test files today, 0 tests must be a
    // PASS". That made a zero-test-file result indistinguishable from a genuinely green suite —
    // `npm run gate:g3` reported PASS forever, vacuously, guarding nothing. Real tests now exist
    // (src/views/JobsView.spec.js et al.), so that justification no longer applies: from this
    // point on, matching zero test files can only mean either every test was deleted (a real
    // regression) or the `include` glob above broke — both cases must hard-FAIL, never pass
    // silently. Leaving this at vitest's own default (false) is deliberate, not an oversight —
    // do not re-add `passWithNoTests: true` without adding a NEW gate that separately checks
    // "at least one test file exists," or the vacuous-pass defect just moves one layer over.
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov'],
      include: ['src/**'],
      exclude: ['src/**/*.{test,spec}.{js,mjs,cjs}'],
    },
  },
})
