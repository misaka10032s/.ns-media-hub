#!/usr/bin/env node
// Commit level (`npm run gate:commit`, run by the pre-commit hook): within 10 seconds, only what the staged files' change
// class needs (cluster-conventions `## Testing timing`: a change's class comes from what it touches, a diff spanning
// classes gets the union, a check whose input the change cannot reach does not run). Whole suites, type-check, build, import
// cycles, coverage and mutation never run here: they run once, at the end of the task (`npm run gate:l1`).
//
// It lists the staged files (`git diff --cached --name-status -M`), puts each into one class by path, in this order (the first
// match wins; a staged delete or rename of a code or test file counts as SETUP):
//   setup    test runner config and the vitest setup files           lint, determinism
//   tooling  quality-gates/**, the lint and mutation config, the hook  lint, determinism
//   test     test files and test helpers                             lint, determinism, assertion check
//   docs     *.md, *.mdx, *.txt, docs/**                             nothing
//   style    *.css, *.scss, *.sass, *.less                           nothing
//   wording  the locale files                                        the i18n check (when the package has one)
//   code     everything else                                         lint
// prints one line `[commit] classes: ...`, starts the steps the classes need at the same time as separate child processes
// (output buffered and printed in a fixed order), waits for all of them, and fails if any failed. Only then, and only when
// TESTS_AT_COMMIT allows, it runs the related tests (`run-related-tests.mjs`).
//
// One source, copied per JS package: a copy edits only the constants block below.
import { spawn, spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { git, repoPrefix } from './lib/git-diff.mjs'

// --- constants block: the only part a copy changes ---------------------------------------------------------------------
// Class patterns match a package-relative path with forward slashes ('../' for the repo hook, which sits above the package).
const SETUP_PATTERNS = [
  /^package\.json$/,
  /^(?:package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml)$/,
  /^vite\.config\.[^/]+$/,
  /^vitest\.config\.[^/]+$/,
  /^vitest\.workspace\.[^/]+$/,
  /(?:^|\/)tsconfig[^/]*\.json$/,
  /^src\/test-utils\/noRepoWrites\.ts$/,
]
const TOOLING_PATTERNS = [
  /^quality-gates\//,
  /(?:^|\/)determinism-canaries\//,
  /^eslint\.config\.[^/]+$/,
  /^stryker[^/]*\.config\.[^/]+$/,
  /^(?:\.\.\/)*\.githooks\/pre-commit$/,
]
const TEST_PATTERNS = [
  /\.(?:test|spec)\.[cm]?js$/,
  /^src\/test-utils\//,
]
// This package has no locale files, so no file is class wording and no i18n step exists.
const WORDING_PATTERNS = []
// A staged file of this kind that is a test file (the assertion check reads these).
const TEST_FILE_PATTERN = /\.(?:test|spec)\.[cm]?js$/
// The extensions this package's `check-lint-diff.mjs` lints (it has no TypeScript in its lint scope).
const LINTABLE_PATTERN = /\.(?:vue|js|mjs|cjs)$/
// Steps: [script path relative to the package, arguments]; null when the package has no such step.
const LINT_STEP = ['quality-gates/check-lint-diff.mjs', ['--staged']]
const ASSERTION_STEP = ['quality-gates/check-test-assertions.mjs', ['--staged']]
const DETERMINISM_SCRIPT = 'quality-gates/check-test-determinism.mjs'
const I18N_STEP = null
// Related tests at commit (cluster-conventions `## Testing timing`, rule R of the design): false when no recorded run of this
// package's test step fits the 10-second commit.
const TESTS_AT_COMMIT = false
const RELATED_TESTS_SCRIPT = 'quality-gates/run-related-tests.mjs'
// The most related test files one commit may run (rule C of the design); null = no cap.
const TEST_FILE_CAP = null
// --- end of the constants block ---------------------------------------------------------------------------------------

const cwd = process.cwd()
// The longest command line one determinism call gets (Windows allows 32767 characters); a longer list is split into calls.
const MAX_ARGUMENT_CHARS = 24000

const CLASS_ORDER = ['code', 'test', 'setup', 'tooling', 'docs', 'style', 'wording']

/** The class of a package-relative path, by the first matching rule (status is not looked at here). */
function classOf(rel) {
  if (SETUP_PATTERNS.some((re) => re.test(rel))) return 'setup'
  if (TOOLING_PATTERNS.some((re) => re.test(rel))) return 'tooling'
  if (TEST_PATTERNS.some((re) => re.test(rel))) return 'test'
  if (/\.(?:md|mdx)$/i.test(rel) || (/\.txt$/i.test(rel) && !/(?:^|\/)requirements[^/]*\.txt$/.test(rel)) || /^docs\//.test(rel)) return 'docs'
  if (/\.(?:css|scss|sass|less)$/i.test(rel)) return 'style'
  if (WORDING_PATTERNS.some((re) => re.test(rel))) return 'wording'
  return 'code'
}

/**
 * The staged entries that belong to this package, each { status, rel, cls }: paths relative to the package folder.
 * Files outside the package are dropped, except the repo's pre-commit hook (its path comes out as '../.githooks/pre-commit').
 * A staged delete (D) or rename (R) of a code or test file is class setup.
 */
function stagedEntries() {
  const out = git(['diff', '--cached', '--name-status', '-M', '-z'], cwd)
  const fields = out.split('\0')
  const prefix = repoPrefix(cwd).replace(/\\/g, '/')
  const depth = prefix === '' ? 0 : prefix.split('/').filter(Boolean).length
  const entries = []
  for (let i = 0; i < fields.length && fields[i] !== ''; ) {
    const status = fields[i][0]
    const paths = status === 'R' || status === 'C' ? [fields[i + 1], fields[i + 2]] : [fields[i + 1]]
    i += 1 + paths.length
    const top = paths[paths.length - 1].replace(/\\/g, '/')
    let rel = null
    if (prefix === '' || top.startsWith(prefix)) rel = top.slice(prefix.length)
    else if (top === '.githooks/pre-commit') rel = `${'../'.repeat(depth)}${top}`
    if (rel === null) continue
    let cls = classOf(rel)
    if ((status === 'D' || status === 'R') && (cls === 'code' || cls === 'test')) cls = 'setup'
    entries.push({ status, rel, cls })
  }
  return entries
}

/** Start `node <script> <args>` in the package folder; resolves { name, code, output, seconds } when it ends. */
function startStep(name, script, args) {
  const started = Date.now()
  return new Promise((resolve) => {
    const chunks = []
    const child = spawn(process.execPath, [path.resolve(cwd, script), ...args], { cwd, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] })
    child.stdout.on('data', (data) => chunks.push({ stream: 'stdout', data }))
    child.stderr.on('data', (data) => chunks.push({ stream: 'stderr', data }))
    child.on('error', (err) => {
      chunks.push({ stream: 'stderr', data: `${name} did not start: ${err.message}\n` })
      resolve({ name, code: 1, chunks, seconds: (Date.now() - started) / 1000 })
    })
    child.on('close', (code) => resolve({ name, code: code ?? 1, chunks, seconds: (Date.now() - started) / 1000 }))
  })
}

/** Split `files` into groups whose joined length stays under MAX_ARGUMENT_CHARS. */
function chunkFiles(files) {
  const groups = []
  let current = []
  let length = 0
  for (const file of files) {
    if (current.length > 0 && length + file.length + 1 > MAX_ARGUMENT_CHARS) {
      groups.push(current)
      current = []
      length = 0
    }
    current.push(file)
    length += file.length + 1
  }
  if (current.length > 0) groups.push(current)
  return groups
}

async function main() {
  const entries = stagedEntries()
  const counts = Object.fromEntries(CLASS_ORDER.map((c) => [c, 0]))
  for (const e of entries) counts[e.cls]++
  console.log(`[commit] classes: ${CLASS_ORDER.map((c) => `${c}=${counts[c]}`).join(' ')}`)

  // A deleted path has nothing to lint or scan (a rename's entry holds the new path).
  const alive = entries.filter((e) => e.status !== 'D')
  const steps = []
  if (LINT_STEP && alive.some((e) => ['test', 'code', 'setup', 'tooling'].includes(e.cls) && LINTABLE_PATTERN.test(e.rel))) {
    steps.push(startStep('lint', LINT_STEP[0], LINT_STEP[1]))
  }
  const scanned = alive.filter((e) => ['test', 'setup', 'tooling'].includes(e.cls)).map((e) => e.rel)
  chunkFiles(scanned).forEach((group, index, groups) => {
    steps.push(startStep(groups.length > 1 ? `determinism ${index + 1}/${groups.length}` : 'determinism', DETERMINISM_SCRIPT, ['--files', ...group]))
  })
  if (ASSERTION_STEP && alive.some((e) => e.cls === 'test' && TEST_FILE_PATTERN.test(e.rel))) {
    steps.push(startStep('assertions', ASSERTION_STEP[0], ASSERTION_STEP[1]))
  }
  if (I18N_STEP && alive.some((e) => e.cls === 'wording')) {
    steps.push(startStep('i18n-check', I18N_STEP[0], I18N_STEP[1]))
  }

  // All steps run at the same time; their output is printed in the order they were started.
  const results = await Promise.all(steps)
  let failed = false
  for (const r of results) {
    for (const { stream, data } of r.chunks) process[stream].write(data)
    console.log(`[commit] ${r.name} ${r.code === 0 ? 'PASSED' : 'FAILED'} (${r.seconds.toFixed(1)} s)`)
    if (r.code !== 0) failed = true
  }
  if (failed) return 1

  if (!TESTS_AT_COMMIT) return 0
  const setupFile = entries.find((e) => e.cls === 'setup')
  if (setupFile) {
    console.log(`[commit] ${setupFile.rel} is test setup: the tests move to the end-of-task run`)
    return 0
  }
  const related = alive.filter((e) => e.cls === 'test' || e.cls === 'code')
  if (related.length === 0) return 0
  if (!fs.existsSync(path.resolve(cwd, RELATED_TESTS_SCRIPT))) {
    console.error(`[commit] FAIL — ${RELATED_TESTS_SCRIPT} does not exist.`)
    return 1
  }
  if (TEST_FILE_CAP !== null && related.length > TEST_FILE_CAP) {
    console.log(`[commit] ${related.length} related test files > ${TEST_FILE_CAP}: the tests move to the end-of-task run`)
    return 0
  }
  const run = spawnSync(process.execPath, [path.resolve(cwd, RELATED_TESTS_SCRIPT), ...related.map((e) => e.rel)], { cwd, stdio: 'inherit', windowsHide: true })
  if (run.error) {
    console.error('[commit] FAIL — the related tests did not start:', run.error)
    return 1
  }
  return run.status ?? 1
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    console.error('[commit] gate crashed:', err)
    process.exit(1)
  })
