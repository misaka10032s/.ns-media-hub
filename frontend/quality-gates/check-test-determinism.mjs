#!/usr/bin/env node
// G3(c) — determinism: no test, test helper, test config or gate script may contain a pattern whose
// result can differ between runs of the same code (cluster-conventions `## Code quality gates`, G3 row).
//
// The nine patterns (rule ids determinism/p1-time-limit ... p9-repo-write, in lib/determinism-rules.mjs):
//   P1 a raised or custom time limit (and P1b: a time-limited mutant counted as killed)
//   P2 a retry            P3 a real sleep or polling      P4 a real clock read
//   P5 unseeded randomness  P6 a dynamic import or module reset inside a test body or hook
//   P7 a real router that lazy-loads pages   P8 any skip, and a silent early return
//   P9 a write to a real repo path
//
// Whole scope on every run (not diff-scoped, unlike G3(b)): it reads no base ref, so a wrong base
// cannot make it vacuous, and a pattern cannot re-enter through a rename or an edit outside the diff.
//
// There is NO way to switch a hit off: no baseline file, no allow-list file, no ignore comment (ESLint
// runs with allowInlineConfig: false), no environment variable, no flag that skips a rule. The only
// flag is --root, which picks the package folder to scan. The only exemptions are the coded constructs
// each rule names. A file that does not parse counts as a hit (`P0 parse error`, file:line of the error), because
// it was not checked (the same rule G3(b) follows), and the run exits non-zero.
//
// Usage: node quality-gates/check-test-determinism.mjs [--root <package folder>]   (default: cwd)
import { ESLint } from 'eslint'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import tseslint from 'typescript-eslint'
import { plugin } from './lib/determinism-rules.mjs'

// The checker never scans itself: its own files and its canary folder hold the pattern text on purpose.
// Paths are compared by resolved real path (case folded on Windows), so another working folder or a
// drive-letter case cannot hide a match.
const fold = (p) => (process.platform === 'win32' ? p.toLowerCase() : p)
const resolveReal = (p) => (fs.existsSync(p) ? fs.realpathSync(p) : path.resolve(p))
const CHECKER_DIR = path.dirname(fileURLToPath(import.meta.url))
const OWN_FILES = new Set([
  fileURLToPath(import.meta.url),
  path.join(CHECKER_DIR, 'lib', 'determinism-rules.mjs'),
].map((p) => fold(resolveReal(p))))
const OWN_CANARY_DIR = fold(resolveReal(path.join(CHECKER_DIR, 'determinism-canaries')))

const CODE_EXT = /\.[cm]?[jt]sx?$/
const TEST_FILE_RE = /\.(?:test|spec)\.[cm]?[jt]sx?$/
const SRC_TEST_TS_RE = /^src\/(?:.*\/)?test\.ts$/
const HELPER_RE = /\.testUtils\.[cm]?[jt]sx?$/
const CONFIG_RE = /^(?:vite|vitest|playwright)\.config\.[cm]?[jt]s$|^vitest\.workspace\.[cm]?[jt]s$/
// Skip list: dependency and tool folders only. A folder named build, env, bin or dist never drops a test file,
// and coverage output is skipped only outside a test tree (a test folder, or a file named like a test).
const DEPENDENCY_SEGMENTS = new Set(['node_modules', '.venv', 'venv', '.git', '.stryker-tmp', '__pycache__', 'site-packages', '.tox', 'obj', 'target'])
const COVERAGE_SEGMENTS = new Set(['coverage'])
const TEST_FOLDERS = new Set(['tests', 'test', '.test', '__tests__'])
const ALL_RULES = Object.fromEntries(Object.keys(plugin.rules).map((r) => [`determinism/${r}`, 'error']))
const TEST_COMMAND = /\b(?:npm\s+(?:run\s+)?(?:test|gate[\w:.-]*)|npx\s+vitest|vitest|jest|playwright|stryker|pytest|run\.py|dotnet\s+test|mvn\b|gate-[\w-]+\.(?:sh|ps1))/i

function parseArgs(argv) {
  let root = '.'
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i]
    if (a === '--root' && i + 1 < argv.length) root = argv[++i]
    else if (a.startsWith('--root=')) root = a.slice('--root='.length)
    else throw new Error(`unknown argument "${a}" (the only option is --root <package folder>)`)
  }
  return path.resolve(process.cwd(), root)
}

// Every git call runs with -C <scan root> and without GIT_DIR, GIT_WORK_TREE and GIT_INDEX_FILE: a commit
// hook inherits GIT_DIR, and the file listing then comes out relative to the wrong folder.
function gitEnv() {
  const env = { ...process.env }
  for (const name of ['GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE']) delete env[name]
  return env
}

function git(root, args) {
  return execFileSync('git', ['-C', root, ...args], {
    cwd: root,
    env: gitEnv(),
    encoding: 'utf8',
    maxBuffer: 512 * 1024 * 1024,
  })
}

function listFiles(root) {
  const out = git(root, ['ls-files', '-z', '--cached', '--others', '--exclude-standard'])
  return out
    .split('\0')
    .filter(Boolean)
    .map((f) => f.replace(/\\/g, '/'))
    .filter((f) => fs.existsSync(path.join(root, f)))
}

function isCheckerOwn(abs) {
  const real = fold(fs.realpathSync(abs))
  return OWN_FILES.has(real) || real.startsWith(OWN_CANARY_DIR + path.sep)
}

function classify(rel) {
  const segments = rel.split('/')
  const base = segments[segments.length - 1]
  const isTestPattern = TEST_FILE_RE.test(rel) || SRC_TEST_TS_RE.test(rel)
  if (segments.some((s) => DEPENDENCY_SEGMENTS.has(s))) return null
  if (!isTestPattern && !segments.slice(0, -1).some((s) => TEST_FOLDERS.has(s)) && segments.some((s) => COVERAGE_SEGMENTS.has(s))) return null
  const gateIdx = segments.findIndex((s) => s.startsWith('quality-gates'))
  if (CODE_EXT.test(rel)) {
    if (isTestPattern || HELPER_RE.test(rel) || segments.includes('test-utils') || segments.slice(0, -1).includes('.test')) return 'test'
    if (CONFIG_RE.test(base)) return 'config'
    if (gateIdx >= 0 && /\.[cm]?js$/.test(rel)) return 'gate'
    return null
  }
  if (rel === 'package.json') return 'package'
  if (gateIdx >= 0 && /\.(?:sh|ps1|cmd|bat)$/.test(rel)) return 'shell'
  return null
}

function lineOf(text, index) {
  let line = 1
  for (let i = 0; i < index && i < text.length; i++) if (text.charCodeAt(i) === 10) line++
  return line
}

// package.json scripts: no raised limit, no retry flag.
function scanPackageJson(abs) {
  const text = fs.readFileSync(abs, 'utf8')
  const hits = []
  let pkg
  try {
    pkg = JSON.parse(text)
  } catch (err) {
    return [{ line: 1, tag: 'P0', text: `[P0 parse error] package.json does not parse (file not checked): ${err.message}` }]
  }
  for (const [name, command] of Object.entries(pkg.scripts ?? {})) {
    const at = Math.max(text.indexOf(`"${name}"`), 0)
    const line = lineOf(text, at)
    if (/--(?:testTimeout|hookTimeout|teardownTimeout)\b/.test(command)) hits.push({ line, tag: 'P1', text: `[P1 time-limit] "${name}": "${command}"` })
    if (/--retry\b/.test(command)) hits.push({ line, tag: 'P2', text: `[P2 retry] "${name}": "${command}"` })
  }
  return hits
}

function blankComments(text) {
  return text
    .split('\n')
    .map((l) => (/^\s*(?:#|REM\s|::)/i.test(l) ? '' : l))
    .join('\n')
}

function shellStyle(abs) {
  if (/\.ps1$/i.test(abs)) return 'ps'
  if (/\.(?:cmd|bat)$/i.test(abs)) return 'cmd'
  return 'sh'
}

// The text of the balanced {...} block that opens at or after line `i` (PowerShell loop body).
function braceBody(lines, i) {
  const rest = lines.slice(i).join('\n')
  const open = rest.indexOf('{')
  if (open < 0) return ''
  let depth = 0
  for (let k = open; k < rest.length; k++) {
    if (rest[k] === '{') depth++
    else if (rest[k] === '}' && --depth === 0) return rest.slice(open, k)
  }
  return rest.slice(open)
}

// Shell, PowerShell and cmd gate scripts and the git hook: no limit flag, no retry flag, no loop that
// runs a gate or test command again, no `cmd || cmd`.
function scanShell(abs) {
  const style = shellStyle(abs)
  const lines = blankComments(fs.readFileSync(abs, 'utf8')).split('\n')
  const hits = []
  const add = (i, tag, label) => hits.push({ line: i + 1, tag, text: `[${label}] ${lines[i].trim().slice(0, 160)}` })
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i]
    if (/--(?:testTimeout|hookTimeout|teardownTimeout|blame-hang-timeout)\b/.test(l)) add(i, 'P1', 'P1 time-limit')
    if (/--retry\b/.test(l)) add(i, 'P2', 'P2 retry')
    const dbl = /^(.*?\S)\s*\|\|\s*\1\s*$/.exec(l)
    if (dbl && TEST_COMMAND.test(dbl[1])) add(i, 'P2', 'P2 retry')
    if (style === 'sh' && /^\s*(?:for|while|until)\b/.test(l)) {
      if (/\bdone\b/.test(l)) {
        if (TEST_COMMAND.test(l)) add(i, 'P2', 'P2 retry')
        continue
      }
      let depth = 1
      const body = []
      for (let j = i + 1; j < lines.length && depth > 0; j++) {
        if (/^\s*(?:for|while|until)\b/.test(lines[j]) && !/\bdone\b/.test(lines[j])) depth++
        else if (/^\s*done\b/.test(lines[j])) depth--
        if (depth > 0) body.push(lines[j])
      }
      if (TEST_COMMAND.test(body.join('\n'))) add(i, 'P2', 'P2 retry')
    } else if (style === 'ps' && /^\s*(?:foreach|for|while|do)\b/i.test(l)) {
      if (TEST_COMMAND.test(braceBody(lines, i))) add(i, 'P2', 'P2 retry')
    } else if (style === 'cmd' && /^\s*for\s/i.test(l)) {
      const body = [l]
      if (/\(\s*$/.test(l)) {
        for (let j = i + 1; j < lines.length && !/^\s*\)/.test(lines[j]); j++) body.push(lines[j])
      }
      if (TEST_COMMAND.test(body.join('\n'))) add(i, 'P2', 'P2 retry')
    }
  }
  return hits
}

async function lintGroup(root, kind, files) {
  if (files.length === 0) return []
  const eslint = new ESLint({
    cwd: root,
    overrideConfigFile: true, // ignore the repo's eslint config entirely: this is a fixed rule set
    allowInlineConfig: false, // no `eslint-disable` comment can switch a hit off
    overrideConfig: {
      files: ['**/*.{js,mjs,cjs,ts,mts,cts,tsx,jsx}'],
      plugins: { determinism: plugin },
      rules: ALL_RULES,
      settings: { determinism: { kind, root } },
      languageOptions: {
        parser: tseslint.parser,
        parserOptions: { ecmaVersion: 'latest', sourceType: 'module' },
      },
    },
  })
  return eslint.lintFiles(files.map((f) => path.resolve(root, f)))
}

function toPosix(p) {
  return p.split(path.sep).join('/')
}

async function main() {
  const root = parseArgs(process.argv.slice(2))
  const files = listFiles(root)
  const groups = { test: [], config: [], gate: [], package: [], shell: [] }
  for (const rel of files) {
    const kind = classify(rel)
    if (kind && !isCheckerOwn(path.resolve(root, rel))) groups[kind].push(rel)
  }

  const hits = []
  let checked = 0
  const addHit = (absOrRel, line, tag, text) => {
    const shown = toPosix(path.relative(process.cwd(), path.resolve(root, absOrRel)))
    hits.push({ key: `${shown}:${line}:${tag}`, shown, line, text })
  }

  for (const kind of ['test', 'config', 'gate']) {
    const results = await lintGroup(root, kind, groups[kind])
    checked += groups[kind].length
    for (const result of results) {
      for (const msg of result.messages) {
        if (msg.fatal) {
          addHit(result.filePath, msg.line ?? 1, 'P0', `[P0 parse error] file not checked: ${msg.message}`)
        } else if (msg.ruleId && msg.ruleId.startsWith('determinism/')) {
          addHit(result.filePath, msg.line, /^\[(\S+)/.exec(msg.message)?.[1] ?? msg.ruleId, msg.message)
        } else if (!msg.ruleId && /^File ignored/.test(msg.message)) {
          addHit(result.filePath, 1, 'IGNORED', `[P0 parse error] file not checked (ignored by ESLint): ${msg.message}`)
        }
      }
    }
  }

  for (const rel of groups.package) {
    checked++
    for (const h of scanPackageJson(path.resolve(root, rel))) addHit(rel, h.line, h.tag, h.text)
  }
  const shellFiles = groups.shell.map((rel) => path.resolve(root, rel))
  const top = git(root, ['rev-parse', '--show-toplevel']).trim()
  const hook = path.join(top, '.githooks', 'pre-commit')
  if (fs.existsSync(hook)) shellFiles.push(hook)
  for (const abs of shellFiles) {
    checked++
    for (const h of scanShell(abs)) addHit(abs, h.line, h.tag, h.text)
  }

  const seen = new Set()
  const unique = hits.filter((h) => !seen.has(h.key) && seen.add(h.key))
  unique.sort((a, b) => (a.shown === b.shown ? a.line - b.line : a.shown < b.shown ? -1 : 1))
  for (const h of unique) console.error(`${h.shown}:${h.line} ${h.text}`)

  // The git hook sits at the repo top, outside the scan root's own scope, so it does not count here.
  const inScope = Object.values(groups).reduce((sum, list) => sum + list.length, 0)
  if (inScope === 0) {
    console.error(`[G3c] FAIL — 0 files in scope under ${root}: nothing was checked, so this run proves nothing.`)
    return 1
  }
  if (unique.length > 0) {
    console.error(`\n[G3c] FAIL — ${unique.length} hit(s) in ${checked} file(s) checked.`)
    return 1
  }
  console.log(`[G3c] PASS — ${checked} file(s) checked, 0 hits.`)
  return 0
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    console.error('[G3c] gate crashed:', err)
    process.exit(1)
  })
