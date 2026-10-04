/**
 * Runtime write guard for tests (G3(c), pattern P9). Meant for vitest's `setupFiles`: it makes any test whose code writes
 * inside the repo and outside the temp folder throw. The guard logic is in `repoWriteGuard.ts`; this file only installs it
 * with the values of this package (vitest never measures a setup file for coverage, so the logic does not live here).
 */
import { installRepoWriteGuard } from './repoWriteGuard';

installRepoWriteGuard({
    label: 'noRepoWrites',
    installedKey: 'misaka.noRepoWrites.installed',
    runnerOutput: [
        /(^|\/)coverage(\/|$)/,
        /(^|\/)node_modules\/\.vite[^/]*(\/|$)/,
        /(^|\/)node_modules\/\.vitest[^/]*(\/|$)/,
    ],
});
