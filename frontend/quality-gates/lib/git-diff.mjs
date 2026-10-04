// Shared diff-scoping helper for the diff-based gates (G1 lint-diff, G3b assertion-presence).
// Ported verbatim (in spirit) from misaka_site2.0/frontend/quality-gates/lib/git-diff.mjs —
// keep the two in sync in spirit; this repo's copy differs only in having no G4/G5/G6 callers
// that need it (G4 here is repo-wide via madge, not diff-scoped — see check-import-cycles.mjs).
//
// Scope model (deliberately simple): the base ref is the merge-base with `main` (this repo's
// main branch), and "changed" means "working tree right now vs that merge base" (git diff
// <base>, no upper bound). That single definition covers committed-on-branch AND staged AND
// unstaged changes in one pass — what both the pre-commit hook (about to be committed) and a
// manual L0/L1 run actually want. Override with QUALITY_BASE_REF for a narrower comparison.
import { execFileSync } from 'node:child_process'

// A commit hook inherits GIT_DIR (and, for a partial commit, GIT_INDEX_FILE) from git: a child `git` that sees them
// lists the wrong folder's files and returns an empty diff, which every diff-scoped gate reads as "nothing changed".
// So every call strips every GIT_* variable and names its folder with `-C <cwd>`: no gate script that lists changed
// or staged files depends on the process folder or on GIT_DIR, GIT_WORK_TREE or GIT_INDEX_FILE.
export function git(args, cwd) {
  const env = { ...process.env }
  for (const key of Object.keys(env)) {
    if (key.startsWith('GIT_')) delete env[key]
  }
  return execFileSync('git', ['-C', cwd, ...args], {
    cwd,
    encoding: 'utf-8',
    maxBuffer: 64 * 1024 * 1024,
    env,
    windowsHide: true,
  })
}

/** Resolve the base ref to diff against. */
export function resolveBaseRef(cwd) {
  if (process.env.QUALITY_BASE_REF && process.env.QUALITY_BASE_REF.trim()) {
    return process.env.QUALITY_BASE_REF.trim()
  }
  try {
    const base = git(['merge-base', 'HEAD', 'main'], cwd).trim()
    if (base) return base
  } catch {
    // `main` not reachable (e.g. detached/shallow) — fall through.
  }
  try {
    return git(['rev-parse', 'HEAD~1'], cwd).trim()
  } catch {
    return git(['rev-parse', 'HEAD'], cwd).trim() // single-commit repo: yields an empty diff
  }
}

/** git's own prefix for `cwd` relative to the repo top-level, e.g. "frontend/". */
export function repoPrefix(cwd) {
  return git(['rev-parse', '--show-prefix'], cwd).trim()
}

function stripPrefix(p, prefix) {
  return prefix && p.startsWith(prefix) ? p.slice(prefix.length) : p
}

/**
 * List changed files (added/copied/modified/renamed — never deleted) under `cwd`, filtered to
 * the given extensions, returned as paths relative to `cwd` (not the repo top-level).
 */
export function getChangedFiles(cwd, baseRef, extensions) {
  const prefix = repoPrefix(cwd)
  const patterns = extensions.map((e) => `*.${e}`)
  let out
  try {
    out = git(['diff', '--name-only', '--diff-filter=ACMR', baseRef, '--', ...patterns], cwd)
  } catch (err) {
    throw new Error(`git diff failed against base ref "${baseRef}": ${err.message}`, { cause: err })
  }
  return out
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
    .map((p) => p.replace(/\\/g, '/'))
    .map((p) => stripPrefix(p, prefix))
}

/**
 * List the files staged for the next commit (added/copied/modified/renamed — never deleted) under `cwd`, filtered
 * to the given extensions, returned as paths relative to `cwd`.
 */
export function getStagedFiles(cwd, extensions) {
  const prefix = repoPrefix(cwd)
  const patterns = extensions.map((e) => `*.${e}`)
  const out = git(['diff', '--cached', '--name-only', '--diff-filter=ACMR', '--', ...patterns], cwd)
  return out
    .split('\n')
    .map((s) => s.trim())
    .filter(Boolean)
    .map((p) => p.replace(/\\/g, '/'))
    .map((p) => stripPrefix(p, prefix))
}

/**
 * Map<relPath, Set<lineNumber>> of lines added/changed on the "new" side for the given files.
 * Pure deletions contribute no lines (nothing new to require lint/assertions for).
 * With `staged` the lines are those of the index against HEAD (what the next commit adds); `baseRef` is then unused.
 */
export function getChangedLineRanges(cwd, baseRef, files, staged = false) {
  const result = new Map()
  if (files.length === 0) return result
  const prefix = repoPrefix(cwd)
  const against = staged ? ['--cached'] : [baseRef]
  const diffOut = git(['diff', '--unified=0', '--diff-filter=ACMR', ...against, '--', ...files], cwd)
  let currentFile = null
  for (const line of diffOut.split('\n')) {
    if (line.startsWith('+++ ')) {
      const raw = line.slice(4).trim()
      if (raw === '/dev/null') {
        currentFile = null
        continue
      }
      const cleaned = stripPrefix(raw.replace(/^b\//, '').replace(/\\/g, '/'), prefix)
      currentFile = cleaned
      if (!result.has(currentFile)) result.set(currentFile, new Set())
      continue
    }
    if (line.startsWith('@@ ') && currentFile) {
      const m = /@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/.exec(line)
      if (m) {
        const startLine = parseInt(m[1], 10)
        const count = m[2] === undefined ? 1 : parseInt(m[2], 10)
        for (let i = 0; i < count; i++) result.get(currentFile).add(startLine + i)
      }
    }
  }
  return result
}
