#!/usr/bin/env node
// Commit-time test step: run only the tests related to the files staged for the next commit
// (`vitest related --run <staged source and test files>`), never the whole suite. The whole suite runs once at
// the end of a task: `npm run gate:l0` (gate:g3:test). Vitest's own `related` lookup follows the import graph from
// each staged file to the test files that load it; a staged test file runs itself.
//
// No staged frontend source or test file, or no test related to them: this prints that and passes.
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { getStagedFiles } from './lib/git-diff.mjs'

const cwd = process.cwd()
const SOURCE_EXTENSIONS = ['ts', 'tsx', 'js', 'mjs', 'cjs', 'vue']

function main() {
  const files = getStagedFiles(cwd, SOURCE_EXTENSIONS).filter((f) => !f.startsWith('quality-gates/'))
  if (files.length === 0) {
    console.log('[commit] no staged frontend source or test file — no related test to run.')
    return 0
  }
  // Vitest's own entry file, run by this Node: no shell and no .cmd shim, so no console window opens.
  const vitest = path.resolve(cwd, 'node_modules', 'vitest', 'vitest.mjs')
  if (!fs.existsSync(vitest)) {
    console.error(`[commit] FAIL — ${vitest} does not exist: install the frontend dependencies first.`)
    return 1
  }
  const result = spawnSync(
    process.execPath,
    [vitest, 'related', '--run', '--passWithNoTests', ...files],
    { cwd, stdio: 'inherit', windowsHide: true },
  )
  if (result.error) {
    console.error('[commit] FAIL — vitest did not start:', result.error)
    return 1
  }
  if (result.status === 0) console.log(`[commit] PASS — related tests of ${files.length} staged file(s).`)
  return result.status ?? 1
}

process.exit(main())
