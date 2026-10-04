#!/usr/bin/env node
// G3(b) — "newly added/changed test files must not contain zero assertions."
//
// Deliberately scoped to the DIFF, not the whole repo — see G1's own header for why (same
// reasoning: a repo-wide sweep would also fail on any pre-existing assertion-free test, a
// different, bigger problem than "don't let a new one in"). This repo has 0 test files today,
// so this gate is infrastructure for whenever the first test lands, not a check with anything
// to prove against real content yet — proven instead with a planted canary (see
// .claude/CLAUDE.md -> `## Code quality gates` for the exit-code evidence).
//
// Reuses @vitest/eslint-plugin's `expect-expect` rule rather than a hand-rolled parser.
//
// CRITICAL — do not regress this: the override config below sets NO custom parser, which is
// correct ONLY because this repo has zero TypeScript (0 .ts files, no tsconfig — verified at
// install time). ESLint's default `espree` parser cannot parse TS syntax; on a repo that HAS
// TypeScript, omitting `languageOptions.parser: tseslint.parser` reproduces the exact defect
// documented in misaka_site2.0's copy of this file: a typed test file fails to parse, ESLint
// reports one FATAL message with `ruleId: null`, and because this script originally only
// counted `ruleId === 'vitest/expect-expect'` messages, that fatal error was silently ignored
// and a zero-assertion typed test sailed through as a false PASS. This copy avoids the whole
// failure class by treating ANY `msg.fatal` as a violation (see below) — so if TypeScript is
// ever adopted here, wire the TS parser AND this fatal-message guard stays as a second layer of
// defense, not a replacement for it.
import { ESLint } from 'eslint'
import path from 'node:path'
import vitestPlugin from '@vitest/eslint-plugin'
import { getChangedFiles, getStagedFiles, resolveBaseRef } from './lib/git-diff.mjs'

const cwd = process.cwd()
const TEST_FILE_RE = /\.(test|spec)\.[cm]?jsx?$/
const EXTENSIONS = ['js', 'mjs', 'cjs']
// `--staged` (the commit-time step): only the test files staged for the next commit.
const stagedMode = process.argv.slice(2).includes('--staged')

async function main() {
  const baseRef = stagedMode ? 'the index' : resolveBaseRef(cwd)
  const stagedTests = getStagedFiles(cwd, EXTENSIONS).filter((f) => TEST_FILE_RE.test(f))
  const changed = stagedMode
    ? stagedTests
    : getChangedFiles(cwd, baseRef, EXTENSIONS).filter((f) => TEST_FILE_RE.test(f))

  if (changed.length === 0 && stagedTests.length > 0) {
    // The staged list holds test files but the diff listed none: the listing is broken, so nothing was checked.
    console.error(
      `[G3b] FAIL — 0 changed test files vs ${baseRef} while ${stagedTests.length} test file(s) are staged ` +
        `(${stagedTests.join(', ')}): the changed-file listing is wrong, nothing was checked.`,
    )
    return 1
  }

  if (changed.length === 0) {
    console.log(`[G3b] no new/changed test files vs ${baseRef} — nothing to check.`)
    return 0
  }

  const eslint = new ESLint({
    cwd,
    overrideConfigFile: true, // ignore eslint.config.js entirely — this is a single-rule pass
    overrideConfig: {
      files: ['**/*.{js,mjs,cjs}'],
      plugins: { vitest: vitestPlugin },
      rules: { 'vitest/expect-expect': 'error' },
      languageOptions: {
        ecmaVersion: 'latest',
        sourceType: 'module',
      },
    },
  })

  const absFiles = changed.map((f) => path.resolve(cwd, f))
  const results = await eslint.lintFiles(absFiles)

  let violations = 0
  for (const result of results) {
    for (const msg of result.messages) {
      // Belt-and-suspenders: a parse failure means the assertion rule never ran, i.e. this file
      // was NOT actually checked — that must fail loud, never pass silently.
      if (msg.fatal) {
        violations++
        console.error(`${path.relative(cwd, result.filePath)}:${msg.line} PARSE ERROR (file not checked): ${msg.message}`)
        continue
      }
      if (msg.ruleId === 'vitest/expect-expect') {
        violations++
        console.error(`${path.relative(cwd, result.filePath)}:${msg.line} ${msg.message}`)
      }
    }
  }

  if (violations > 0) {
    console.error(`\n[G3b] FAIL — ${violations} test block(s) with zero assertions in ${changed.length} changed test file(s) (base ${baseRef}).`)
    return 1
  }

  console.log(`[G3b] PASS — ${changed.length} changed test file(s), all test blocks assert something (base ${baseRef}).`)
  return 0
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    console.error('[G3b] gate crashed:', err)
    process.exit(1)
  })
