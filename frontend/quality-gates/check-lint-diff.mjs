#!/usr/bin/env node
// G1 — "0 new ESLint warnings/errors on changed lines."
//
// This repo has a pre-existing backlog of 541 ESLint warnings (measured 2026-08-27, all from
// eslint-plugin-vue's stylistic `flat/recommended` rules — max-attributes-per-line,
// singleline-html-element-content-newline, html-self-closing — none are errors, none touch
// logic). A bare `eslint . --max-warnings=0` would fail on day one for every contributor,
// forever, regardless of what they touched — so this gate uses LINE-range diff-scoping
// (same model as misaka_site2.0's recipe, which hit the identical problem at a larger scale):
// lint changed files, but only fail on messages whose line is actually inside the diff's
// changed lines. A brand-new file (absent at baseRef) has no "pre-existing" lines by
// definition, so every line in it counts.
//
// Scope: only files git tracks in the diff are ever considered — a gitignored scratch file
// can never enter `git diff` in the first place, so it cannot reach this gate (see
// lib/git-diff.mjs's own docstring for the untracked-file caveat, which cuts the other way and
// doesn't apply here).
import { ESLint } from 'eslint'
import path from 'node:path'
import { getChangedFiles, getChangedLineRanges, getStagedFiles, git, resolveBaseRef, repoPrefix } from './lib/git-diff.mjs'

const cwd = process.cwd()
// `--staged` (the commit-time step): only the files staged for the next commit, only their lines the commit adds.
const staged = process.argv.slice(2).includes('--staged')

function fileExistsAtRef(file, baseRef, prefix) {
  try {
    git(['cat-file', '-e', `${baseRef}:${prefix}${file}`], cwd)
    return true
  } catch {
    return false
  }
}

async function main() {
  const baseRef = staged ? 'HEAD' : resolveBaseRef(cwd)
  const extensions = ['vue', 'js', 'mjs', 'cjs']
  const changed = staged ? getStagedFiles(cwd, extensions) : getChangedFiles(cwd, baseRef, extensions)

  if (changed.length === 0) {
    console.log(`[G1] no new/changed lintable files vs ${baseRef} — nothing to check.`)
    return 0
  }

  const prefix = repoPrefix(cwd)
  const changedLines = getChangedLineRanges(cwd, baseRef, changed, staged)
  const eslint = new ESLint({ cwd }) // uses this repo's own eslint.config.js
  const absFiles = changed.map((f) => path.resolve(cwd, f))
  const results = await eslint.lintFiles(absFiles)

  let errorCount = 0
  let warningCount = 0
  for (const result of results) {
    const relFile = path.relative(cwd, result.filePath).replace(/\\/g, '/')
    const isNewFile = !fileExistsAtRef(relFile, baseRef, prefix)
    const lineSet = changedLines.get(relFile) ?? new Set()

    for (const msg of result.messages) {
      if (!isNewFile && !lineSet.has(msg.line)) continue // pre-existing, unrelated to this diff
      const sev = msg.severity === 2 ? 'error' : 'warning'
      if (msg.severity === 2) errorCount++
      else warningCount++
      console.error(`${relFile}:${msg.line}:${msg.column} ${sev} ${msg.message} (${msg.ruleId ?? 'n/a'})`)
    }
  }

  const total = errorCount + warningCount
  if (total > 0) {
    console.error(`\n[G1] FAIL — ${errorCount} error(s), ${warningCount} warning(s) on changed lines across ${changed.length} changed file(s) (base ${baseRef}).`)
    return 1
  }

  console.log(`[G1] PASS — ${changed.length} changed file(s), 0 errors/warnings on changed lines (base ${baseRef}).`)
  return 0
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    console.error('[G1] gate crashed:', err)
    process.exit(1)
  })
