// G3(c) determinism — the nine local ESLint rules (one per pattern) used by
// check-test-determinism.mjs. Rule ids: determinism/p1-time-limit ... determinism/p9-repo-write.
//
// Every rule reads the whole file at `Program` and walks the AST itself, so nothing here depends on
// ESLint visiting nodes in a particular order. Which kind of file is being linted comes from
// `context.settings.determinism.kind`: 'test' (test files and test-only helpers), 'config'
// (vite / vitest / playwright config) or 'gate' (quality-gates scripts written in JS).
//
// There is no switch to turn a hit off: no ignore comment is honoured (the checker runs ESLint with
// allowInlineConfig: false), no baseline, no environment variable. The only exemptions are the coded
// constructs named per pattern below.
import fs from 'node:fs'
import path from 'node:path'

// ---------------------------------------------------------------- AST helpers

const SKIP_KEYS = new Set(['parent', 'loc', 'range', 'tokens', 'comments', 'leadingComments', 'trailingComments'])
const FN_TYPES = new Set(['ArrowFunctionExpression', 'FunctionExpression', 'FunctionDeclaration'])
const LOOP_TYPES = new Set(['ForStatement', 'ForInStatement', 'ForOfStatement', 'WhileStatement', 'DoWhileStatement'])
const TEST_ROOTS = new Set(['it', 'test', 'bench', 'describe', 'suite', 'beforeAll', 'beforeEach', 'afterAll', 'afterEach'])
const HOOKS = new Set(['beforeAll', 'beforeEach', 'afterAll', 'afterEach'])
const BODY_ROOTS = new Set(['it', 'test', 'bench', 'beforeAll', 'beforeEach', 'afterAll', 'afterEach'])
const TEST_LEVEL = new Set(['it', 'test', 'bench'])
const GLOBAL_OBJECTS = new Set(['globalThis', 'window', 'self', 'global'])

function isNode(v) {
  return v !== null && typeof v === 'object' && typeof v.type === 'string'
}

function isFunctionNode(n) {
  return isNode(n) && FN_TYPES.has(n.type)
}

function unwrap(n) {
  while (
    isNode(n) &&
    (n.type === 'ChainExpression' ||
      n.type === 'TSAsExpression' ||
      n.type === 'TSNonNullExpression' ||
      n.type === 'TSSatisfiesExpression' ||
      n.type === 'TSTypeAssertion' ||
      n.type === 'ParenthesizedExpression')
  ) {
    n = n.expression
  }
  return n
}

function children(node) {
  const out = []
  for (const key of Object.keys(node)) {
    if (SKIP_KEYS.has(key)) continue
    const v = node[key]
    if (Array.isArray(v)) {
      for (const c of v) if (isNode(c)) out.push(c)
    } else if (isNode(v)) {
      out.push(v)
    }
  }
  return out
}

// All nodes under `root` (root included), iterative so a very deep expression cannot overflow the stack.
function subtreeNodes(root) {
  const out = []
  const stack = [root]
  while (stack.length > 0) {
    const n = stack.pop()
    out.push(n)
    const kids = children(n)
    for (let i = kids.length - 1; i >= 0; i--) stack.push(kids[i])
  }
  return out
}

function subtreeHas(root, predicate) {
  return subtreeNodes(root).some(predicate)
}

function propName(m) {
  if (!isNode(m) || m.type !== 'MemberExpression') return null
  if (!m.computed && m.property.type === 'Identifier') return m.property.name
  if (m.computed && m.property.type === 'Literal' && typeof m.property.value === 'string') return m.property.value
  return null
}

function keyName(p) {
  if (!isNode(p) || p.type !== 'Property') return null
  if (!p.computed && p.key.type === 'Identifier') return p.key.name
  if (p.key.type === 'Literal' && (typeof p.key.value === 'string' || typeof p.key.value === 'number')) return String(p.key.value)
  return null
}

function hasKey(objectNode, names) {
  return objectNode.properties.some((p) => {
    const k = keyName(p)
    return k !== null && names.has(k)
  })
}

function calleeRoot(n) {
  n = unwrap(n)
  if (!isNode(n)) return null
  if (n.type === 'Identifier') return n.name
  if (n.type === 'MemberExpression') return calleeRoot(n.object)
  if (n.type === 'CallExpression') return calleeRoot(n.callee)
  return null
}

function isIdent(n, name) {
  n = unwrap(n)
  return isNode(n) && n.type === 'Identifier' && n.name === name
}

// `name` or `globalThis.name` / `window.name` / `self.name`
function isGlobalRef(n, name) {
  n = unwrap(n)
  if (!isNode(n)) return false
  if (n.type === 'Identifier') return n.name === name
  if (n.type === 'MemberExpression' && propName(n) === name) {
    const o = unwrap(n.object)
    return isNode(o) && o.type === 'Identifier' && GLOBAL_OBJECTS.has(o.name)
  }
  return false
}

// A call `<obj>.<prop>(...)` where obj is one of `objs` (identifier names) and prop one of `props`.
function isNamedMemberCall(n, objs, props) {
  if (!isNode(n) || n.type !== 'CallExpression') return false
  const c = unwrap(n.callee)
  if (!isNode(c) || c.type !== 'MemberExpression') return false
  const p = propName(c)
  const o = unwrap(c.object)
  return p !== null && props.has(p) && isNode(o) && o.type === 'Identifier' && objs.has(o.name)
}

function isEachTable(call) {
  const c = unwrap(call.callee)
  if (!isNode(c) || c.type !== 'MemberExpression') return false
  const p = propName(c)
  return p === 'each' || p === 'for'
}

// A call to it / test / describe / hooks (or a member chain on them), but not the `.each(table)` call
// whose arguments are data, not options.
function isTestCall(n) {
  if (!isNode(n) || n.type !== 'CallExpression') return false
  const root = calleeRoot(n.callee)
  return root !== null && TEST_ROOTS.has(root) && !isEachTable(n)
}

function isNumberNode(n) {
  n = unwrap(n)
  if (!isNode(n)) return false
  if (n.type === 'Literal') return typeof n.value === 'number'
  if (n.type === 'UnaryExpression' && (n.operator === '-' || n.operator === '+')) return isNumberNode(n.argument)
  return false
}

function isZeroLiteral(n) {
  n = unwrap(n)
  return isNode(n) && n.type === 'Literal' && n.value === 0
}

// ---------------------------------------------------------------- per-file index

const INDEX = new WeakMap()

function getIndex(program) {
  let idx = INDEX.get(program)
  if (idx) return idx
  const parent = new WeakMap()
  const nodes = []
  const stack = [[program, null]]
  while (stack.length > 0) {
    const [n, p] = stack.pop()
    parent.set(n, p)
    nodes.push(n)
    for (const c of children(n)) stack.push([c, n])
  }
  const imports = new Map()
  for (const stmt of program.body) {
    if (stmt.type !== 'ImportDeclaration' || stmt.importKind === 'type') continue
    const source = String(stmt.source.value)
    for (const spec of stmt.specifiers) {
      if (spec.importKind === 'type') continue
      let imported = 'default'
      if (spec.type === 'ImportSpecifier') imported = spec.imported.type === 'Identifier' ? spec.imported.name : String(spec.imported.value)
      else if (spec.type === 'ImportNamespaceSpecifier') imported = '*'
      imports.set(spec.local.name, { source, imported, node: stmt })
    }
  }
  idx = { program, parent, nodes, imports, timers: null, spies: null, promiseTimers: null }
  INDEX.set(program, idx)
  return idx
}

function ancestors(node, idx) {
  const out = []
  let cur = idx.parent.get(node)
  while (cur) {
    out.push(cur)
    cur = idx.parent.get(cur)
  }
  return out
}

// The it/test/describe/hook call that receives `fn` as one of its arguments, or null.
function callbackCall(fn, idx) {
  const p = idx.parent.get(fn)
  if (p && p.type === 'CallExpression' && p.arguments.includes(fn) && isTestCall(p)) return p
  return null
}

function containerOf(call, idx) {
  for (const a of ancestors(call, idx)) {
    if (isFunctionNode(a)) {
      const c = callbackCall(a, idx)
      if (c && !HOOKS.has(calleeRoot(c.callee))) return a
    }
  }
  return idx.program
}

// The scope a statement takes effect in: the test or describe callback it sits in; for a
// beforeEach/beforeAll hook, the describe (or file) that registered the hook; null for after-hooks.
function effectiveScope(node, idx) {
  for (const a of ancestors(node, idx)) {
    if (!isFunctionNode(a)) continue
    const call = callbackCall(a, idx)
    if (!call) continue
    const root = calleeRoot(call.callee)
    if (HOOKS.has(root)) return root.startsWith('after') ? null : containerOf(call, idx)
    return a
  }
  return idx.program
}

function scopeChain(node, idx) {
  const chain = []
  for (const a of ancestors(node, idx)) {
    if (!isFunctionNode(a)) continue
    const call = callbackCall(a, idx)
    if (call && !HOOKS.has(calleeRoot(call.callee))) chain.push(a)
  }
  chain.push(idx.program)
  return chain
}

function isTestLevelScope(scope, idx) {
  if (scope === idx.program) return false
  const call = callbackCall(scope, idx)
  return call !== null && TEST_LEVEL.has(calleeRoot(call.callee))
}

function insideTestBody(node, idx) {
  for (const a of ancestors(node, idx)) {
    if (!isFunctionNode(a)) continue
    const call = callbackCall(a, idx)
    if (call && BODY_ROOTS.has(calleeRoot(call.callee))) return true
  }
  return false
}

function insideCall(node, idx, predicate) {
  for (const a of ancestors(node, idx)) {
    if (isFunctionNode(a)) {
      const p = idx.parent.get(a)
      if (p && p.type === 'CallExpression' && p.arguments.includes(a) && predicate(p)) return true
    }
  }
  return false
}

// ---------------------------------------------------------------- clock reads, fake timers, spies

function isClockRead(n) {
  if (!isNode(n)) return false
  if (n.type === 'NewExpression') {
    return isGlobalRef(n.callee, 'Date') && n.arguments.length === 0
  }
  if (n.type !== 'CallExpression') return false
  const c = unwrap(n.callee)
  if (!isNode(c)) return false
  if (c.type === 'Identifier') return c.name === 'Date'
  if (c.type !== 'MemberExpression') return false
  const p = propName(c)
  if (p === 'now') return isGlobalRef(c.object, 'Date') || isGlobalRef(c.object, 'performance')
  if (p === 'hrtime') return isGlobalRef(c.object, 'process')
  if (p === 'bigint') {
    const o = unwrap(c.object)
    return isNode(o) && o.type === 'MemberExpression' && propName(o) === 'hrtime' && isGlobalRef(o.object, 'process')
  }
  return false
}

const TIMER_FNS = new Set(['useFakeTimers', 'useRealTimers', 'setSystemTime'])
const VI = new Set(['vi', 'vitest'])

function getTimers(idx) {
  if (idx.timers) return idx.timers
  const out = []
  for (const n of idx.nodes) {
    if (!isNamedMemberCall(n, VI, TIMER_FNS)) continue
    const fnName = propName(unwrap(n.callee))
    const scope = effectiveScope(n, idx)
    if (scope === null) continue
    const arg = n.arguments[0] ? unwrap(n.arguments[0]) : null
    if (fnName === 'useFakeTimers') {
      let pinned = false
      if (arg && arg.type === 'ObjectExpression') {
        const nowProp = arg.properties.find((p) => keyName(p) === 'now')
        pinned = Boolean(nowProp) && !subtreeHas(nowProp.value, isClockRead)
      }
      out.push({ call: n, kind: 'fake', scope, pinned, fixed: false })
    } else if (fnName === 'useRealTimers') {
      out.push({ call: n, kind: 'real', scope, pinned: false, fixed: false })
    } else {
      out.push({ call: n, kind: 'sys', scope, pinned: false, fixed: Boolean(arg) && !subtreeHas(arg, isClockRead) })
    }
  }
  idx.timers = out
  return out
}

// faked: some enclosing scope installed fake timers and never switches back to real ones itself.
// pinned: that scope (or one above it) also fixed the system time to a value that reads no clock.
function fakedState(node, idx) {
  const timers = getTimers(idx)
  let faked = false
  let pinnedByFake = false
  let pinnedBySys = false
  for (const scope of scopeChain(node, idx)) {
    const here = timers.filter((t) => t.scope === scope)
    const testLevel = isTestLevelScope(scope, idx)
    const before = (t) => !testLevel || t.call.range[0] < node.range[0]
    // a switch back to real timers ends the freeze only for what comes after it (a test may clean up at its end)
    if (here.some((t) => t.kind === 'real' && before(t))) continue
    const fakes = here.filter((t) => t.kind === 'fake' && before(t))
    if (fakes.length > 0) {
      faked = true
      if (fakes.some((t) => t.pinned)) pinnedByFake = true
    }
    if (here.some((t) => t.kind === 'sys' && t.fixed && before(t))) pinnedBySys = true
  }
  // A clock fixed by a literal `vi.setSystemTime(<fixed>)` counts on its own (vitest then mocks Date alone); a
  // `vi.useFakeTimers({ now: <fixed> })` counts only as the fake timers that carry it.
  return { faked, pinned: (faked && pinnedByFake) || pinnedBySys }
}

const MOCK_METHODS = new Set(['mockReturnValue', 'mockReturnValueOnce', 'mockImplementation', 'mockImplementationOnce'])

function getSpies(idx) {
  if (idx.spies) return idx.spies
  const out = []
  for (const n of idx.nodes) {
    if (!isNamedMemberCall(n, VI, new Set(['spyOn']))) continue
    const a0 = n.arguments[0]
    const a1 = n.arguments[1]
    if (!a0 || !a1 || !isIdent(a0, 'Math') || a1.type !== 'Literal' || a1.value !== 'random') continue
    let mocked = false
    const p = idx.parent.get(n)
    if (p && p.type === 'MemberExpression' && p.object === n && MOCK_METHODS.has(propName(p) ?? '')) {
      mocked = true
    } else if (p && p.type === 'VariableDeclarator' && p.init === n && p.id.type === 'Identifier') {
      const varName = p.id.name
      mocked = idx.nodes.some((m) => {
        if (m.type !== 'CallExpression') return false
        const c = unwrap(m.callee)
        return isNode(c) && c.type === 'MemberExpression' && isIdent(c.object, varName) && MOCK_METHODS.has(propName(c) ?? '')
      })
    }
    if (!mocked) continue
    const scope = effectiveScope(n, idx)
    if (scope !== null) out.push({ call: n, scope })
  }
  idx.spies = out
  return out
}

function isRandomSpied(node, idx) {
  const spies = getSpies(idx)
  for (const scope of scopeChain(node, idx)) {
    const testLevel = isTestLevelScope(scope, idx)
    if (spies.some((s) => s.scope === scope && (!testLevel || s.call.range[0] < node.range[0]))) return true
  }
  return false
}

// ---------------------------------------------------------------- reporting

function report(context, node, tag) {
  const line = node.loc.start.line
  const text = (context.sourceCode.lines[line - 1] ?? '').trim().slice(0, 160)
  context.report({ node, message: `[${tag}] ${text}` })
}

function settingsOf(context) {
  const s = (context.settings && context.settings.determinism) || {}
  return { kind: s.kind ?? 'test', root: s.root ?? process.cwd() }
}

function baseName(context) {
  return path.basename(context.filename).toLowerCase()
}

function makeRule(analyze) {
  return {
    meta: { type: 'problem', schema: [] },
    create(context) {
      return {
        Program(program) {
          analyze(program, context, getIndex(program), settingsOf(context))
        },
      }
    },
  }
}

// ---------------------------------------------------------------- P1 / P2 shared tables

const TEST_TIMEOUT_KEYS = new Set(['timeout'])
const RETRY_KEYS = new Set(['retry', 'retries'])
const CONFIG_LIMIT_KEYS = new Set(['testTimeout', 'hookTimeout', 'teardownTimeout'])
const PLAYWRIGHT_LIMIT_KEYS = new Set(['timeout', 'globalTimeout', 'actionTimeout', 'navigationTimeout'])
const GATE_LIMIT_FLAG = /^--(?:test|hook|teardown)Timeout\b/
const GATE_RETRY_FLAG = /^--retry\b/
const SPAWN_FNS = new Set(['spawn', 'spawnSync', 'exec', 'execSync', 'execFile', 'execFileSync', 'fork'])
const RUNNER_TOKEN = /(?:^|[\s/\\])(?:vitest|jest|playwright|stryker|pytest|mvn)(?:$|[\s./\\-])|npm(?:\s+run)?\s+test|dotnet\s+test/i

function importsPlaywright(idx) {
  for (const [, v] of idx.imports) {
    if (v.source === '@playwright/test' || v.source === 'playwright') return true
  }
  return false
}

function stringPieces(root) {
  const out = []
  for (const n of subtreeNodes(root)) {
    if (n.type === 'Literal' && typeof n.value === 'string') out.push(n.value)
    else if (n.type === 'TemplateElement') out.push(n.value.cooked ?? '')
  }
  return out
}

function spawnsRunner(call) {
  const c = unwrap(call.callee)
  let name = null
  if (isNode(c) && c.type === 'Identifier') name = c.name
  else if (isNode(c) && c.type === 'MemberExpression') name = propName(c)
  if (name === null || !SPAWN_FNS.has(name)) return false
  const joined = call.arguments.flatMap((a) => stringPieces(a)).join(' ')
  return RUNNER_TOKEN.test(joined)
}

// Names of functions in this file that start a test runner, directly or through another local function.
function runnerFunctions(idx) {
  const fns = new Map()
  for (const n of idx.nodes) {
    if (n.type === 'FunctionDeclaration' && n.id) fns.set(n.id.name, n)
    else if (n.type === 'VariableDeclarator' && n.id.type === 'Identifier' && n.init && isFunctionNode(unwrap(n.init))) fns.set(n.id.name, unwrap(n.init))
  }
  const runners = new Set()
  let changed = true
  while (changed) {
    changed = false
    for (const [name, fn] of fns) {
      if (runners.has(name)) continue
      const hit = subtreeNodes(fn).some((m) => {
        if (m.type !== 'CallExpression') return false
        if (spawnsRunner(m)) return true
        const c = unwrap(m.callee)
        return isNode(c) && c.type === 'Identifier' && runners.has(c.name)
      })
      if (hit) {
        runners.add(name)
        changed = true
      }
    }
  }
  return runners
}

// ---------------------------------------------------------------- P1 — raised or custom time limit

function p1(program, context, idx, { kind }) {
  const file = baseName(context)
  if (kind === 'config') {
    const pw = file.startsWith('playwright.config')
    for (const n of idx.nodes) {
      if (n.type !== 'Property') continue
      const k = keyName(n)
      if (k === null) continue
      if (CONFIG_LIMIT_KEYS.has(k) || (pw && PLAYWRIGHT_LIMIT_KEYS.has(k))) report(context, n, 'P1 time-limit')
    }
    return
  }
  if (kind === 'gate') {
    for (const n of idx.nodes) {
      if (n.type === 'Literal' && typeof n.value === 'string' && GATE_LIMIT_FLAG.test(n.value)) report(context, n, 'P1 time-limit')
      else if (n.type === 'TemplateElement' && GATE_LIMIT_FLAG.test((n.value.cooked ?? '').trimStart())) report(context, n, 'P1 time-limit')
    }
    if (file.startsWith('mutation-diff')) checkNotKilled(program, context, idx)
    return
  }
  const pw = importsPlaywright(idx)
  for (const n of idx.nodes) {
    if (n.type === 'CallExpression') {
      if (isTestCall(n)) {
        const root = calleeRoot(n.callee)
        let seenFn = false
        for (const a of n.arguments) {
          const u = unwrap(a)
          if (isFunctionNode(u)) {
            seenFn = true
            continue
          }
          if (isNumberNode(u)) report(context, a, 'P1 time-limit')
          else if (u.type === 'ObjectExpression' && hasKey(u, TEST_TIMEOUT_KEYS)) report(context, a, 'P1 time-limit')
          else if (seenFn && root !== 'bench') report(context, a, 'P1 time-limit')
        }
      }
      if (isNamedMemberCall(n, new Set(['test']), new Set(['setTimeout', 'slow']))) report(context, n, 'P1 time-limit')
      if (isNamedMemberCall(n, VI, new Set(['setConfig']))) report(context, n, 'P1 time-limit')
    } else if (pw && n.type === 'Property') {
      const k = keyName(n)
      if (k !== null && PLAYWRIGHT_LIMIT_KEYS.has(k)) report(context, n, 'P1 time-limit')
    }
  }
}

// P1b — a time-limited or crashed mutant must count as NOT killed.
function checkNotKilled(program, context, idx) {
  const REQUIRED = ['Survived', 'NoCoverage', 'Timeout', 'RuntimeError']
  let found = false
  for (const n of idx.nodes) {
    if (n.type !== 'VariableDeclarator' || n.id.type !== 'Identifier' || n.id.name !== 'NOT_KILLED') continue
    found = true
    const init = unwrap(n.init)
    const arr = init && init.type === 'NewExpression' ? unwrap(init.arguments[0]) : null
    const values = arr && arr.type === 'ArrayExpression' ? arr.elements.filter((e) => isNode(e) && e.type === 'Literal').map((e) => e.value) : []
    const missing = REQUIRED.filter((r) => !values.includes(r))
    if (missing.length > 0) {
      const line = n.loc.start.line
      const text = (context.sourceCode.lines[line - 1] ?? '').trim().slice(0, 160)
      context.report({ node: n, message: `[P1b time-limited-pass] ${text} (NOT_KILLED lacks ${missing.join(', ')})` })
    }
  }
  if (!found) {
    context.report({ node: program, loc: { line: 1, column: 0 }, message: '[P1b time-limited-pass] no NOT_KILLED set found in this mutation file (cannot prove Timeout counts as not killed)' })
  }
}

// ---------------------------------------------------------------- P2 — retry

// A loop repeats until success when it counts (a for or while whose test compares a counter) or when its body
// leaves or restarts it (break, continue, return; not inside a nested function). A loop over a list that runs the
// runner once per item with no exit is not a retry.
function retryShaped(loop) {
  if (loop.type !== 'ForInStatement' && loop.type !== 'ForOfStatement') {
    const t = loop.test ? unwrap(loop.test) : null
    if (isNode(t) && t.type === 'BinaryExpression') return true
  }
  const stack = [loop.body]
  while (stack.length > 0) {
    const m = stack.pop()
    if (m.type === 'BreakStatement' || m.type === 'ContinueStatement' || m.type === 'ReturnStatement') return true
    if (isFunctionNode(m)) continue
    for (const c of children(m)) stack.push(c)
  }
  return false
}

function p2(program, context, idx, { kind }) {
  if (kind === 'config') {
    for (const n of idx.nodes) {
      if (n.type !== 'Property') continue
      const k = keyName(n)
      if (k !== null && RETRY_KEYS.has(k)) report(context, n, 'P2 retry')
    }
    return
  }
  if (kind === 'gate') {
    for (const n of idx.nodes) {
      if (n.type === 'Literal' && typeof n.value === 'string' && GATE_RETRY_FLAG.test(n.value)) report(context, n, 'P2 retry')
    }
    const runners = runnerFunctions(idx)
    for (const n of idx.nodes) {
      if (!LOOP_TYPES.has(n.type) || !retryShaped(n)) continue
      const hit = subtreeNodes(n.body).some((m) => {
        if (m.type !== 'CallExpression') return false
        if (spawnsRunner(m)) return true
        const c = unwrap(m.callee)
        return isNode(c) && c.type === 'Identifier' && runners.has(c.name)
      })
      if (hit) report(context, n, 'P2 retry')
    }
    return
  }
  for (const n of idx.nodes) {
    if (n.type !== 'CallExpression' || !isTestCall(n)) continue
    for (const a of n.arguments) {
      const u = unwrap(a)
      if (u.type === 'ObjectExpression' && hasKey(u, RETRY_KEYS)) report(context, a, 'P2 retry')
    }
  }
}

// ---------------------------------------------------------------- P3 — real sleep or polling

function hasLoopAncestor(node, idx) {
  for (const a of ancestors(node, idx)) {
    if (isFunctionNode(a) && callbackCall(a, idx)) return false
    if (LOOP_TYPES.has(a.type)) return true
  }
  return false
}

function testingLibraryNames(idx) {
  const names = new Map()
  for (const [local, v] of idx.imports) {
    if (v.source.startsWith('@testing-library/')) names.set(local, v.imported)
  }
  return names
}

// ---- sleeps through node:timers/promises (setTimeout, setInterval, scheduler.wait), under any imported name

const TIMERS_PROMISES_SOURCES = new Set(['node:timers/promises', 'timers/promises'])
const PROMISE_TIMER_FNS = new Set(['setTimeout', 'setInterval'])

// The module a `require('x')` or `import('x')` expression loads, or null.
function loadedModule(expr) {
  let e = unwrap(expr)
  if (isNode(e) && e.type === 'AwaitExpression') e = unwrap(e.argument)
  if (!isNode(e)) return null
  if (e.type === 'ImportExpression') {
    const s = unwrap(e.source)
    return isNode(s) && s.type === 'Literal' && typeof s.value === 'string' ? s.value : null
  }
  if (e.type === 'CallExpression' && isIdent(e.callee, 'require')) {
    const a0 = e.arguments[0]
    return a0 && a0.type === 'Literal' && typeof a0.value === 'string' ? a0.value : null
  }
  return null
}

// named: local name -> 'setTimeout' | 'setInterval' | 'scheduler'; spaces: locals that hold the whole module.
function timersPromisesBindings(idx) {
  if (idx.promiseTimers) return idx.promiseTimers
  const named = new Map()
  const spaces = new Set()
  for (const [local, v] of idx.imports) {
    if (!TIMERS_PROMISES_SOURCES.has(v.source)) continue
    if (v.imported === '*' || v.imported === 'default') spaces.add(local)
    else named.set(local, v.imported)
  }
  for (const n of idx.nodes) {
    if (n.type !== 'VariableDeclarator' || !n.init) continue
    const mod = loadedModule(n.init)
    if (mod === null || !TIMERS_PROMISES_SOURCES.has(mod)) continue
    if (n.id.type === 'Identifier') spaces.add(n.id.name)
    else if (n.id.type === 'ObjectPattern') {
      for (const p of n.id.properties) {
        const k = p.type === 'Property' ? keyName(p) : null
        if (k !== null && p.value.type === 'Identifier') named.set(p.value.name, k)
      }
    }
  }
  idx.promiseTimers = { named, spaces }
  return idx.promiseTimers
}

// 'setTimeout' | 'setInterval' | 'wait' when the callee `c` is one of those timers, else null.
function promiseTimerFn(c, bindings) {
  if (c.type === 'Identifier') {
    const fn = bindings.named.get(c.name)
    return fn === 'setTimeout' || fn === 'setInterval' ? fn : null
  }
  if (c.type !== 'MemberExpression') return null
  const p = propName(c)
  const o = unwrap(c.object)
  if (!isNode(o)) return null
  if (p === 'wait') {
    if (o.type === 'Identifier' && bindings.named.get(o.name) === 'scheduler') return 'wait'
    if (o.type === 'MemberExpression' && propName(o) === 'scheduler') {
      const base = unwrap(o.object)
      if (isNode(base) && base.type === 'Identifier' && bindings.spaces.has(base.name)) return 'wait'
    }
    return null
  }
  if (p !== null && PROMISE_TIMER_FNS.has(p) && o.type === 'Identifier' && bindings.spaces.has(o.name)) return p
  return null
}

function p3(program, context, idx, { kind }) {
  if (kind !== 'test') return
  const tl = testingLibraryNames(idx)
  const promiseTimers = timersPromisesBindings(idx)
  for (const n of idx.nodes) {
    if (LOOP_TYPES.has(n.type)) {
      const cond = n.type === 'ForStatement' ? n.test : n.type === 'ForInStatement' || n.type === 'ForOfStatement' ? n.right : n.test
      if (cond && subtreeHas(cond, isClockRead)) report(context, n, 'P3 sleep-or-poll')
      continue
    }
    if (n.type !== 'CallExpression') continue
    const c = unwrap(n.callee)
    if (!isNode(c)) continue
    const promiseFn = promiseTimerFn(c, promiseTimers)
    if (promiseFn !== null) {
      // the delay is the first argument here; one zero-delay yield stays allowed, as with the global setTimeout
      if (promiseFn === 'setInterval' || !isZeroLiteral(n.arguments[0]) || hasLoopAncestor(n, idx)) report(context, n, 'P3 sleep-or-poll')
      continue
    }
    let timerName = null
    if (c.type === 'Identifier') timerName = c.name
    else if (c.type === 'MemberExpression') {
      const o = unwrap(c.object)
      if (isNode(o) && o.type === 'Identifier' && GLOBAL_OBJECTS.has(o.name)) timerName = propName(c)
    }
    if (timerName === 'setTimeout' || timerName === 'setInterval') {
      const delay = n.arguments[1]
      const zero = n.arguments.length < 2 || isZeroLiteral(delay)
      const needs = timerName === 'setInterval' || !zero || hasLoopAncestor(n, idx)
      if (needs && !fakedState(n, idx).faked) report(context, n, 'P3 sleep-or-poll')
      continue
    }
    if (timerName === 'requestAnimationFrame') {
      const inPromise = ancestors(n, idx).some((a) => a.type === 'NewExpression' && isIdent(a.callee, 'Promise'))
      if (inPromise) report(context, n, 'P3 sleep-or-poll')
      continue
    }
    if (isNamedMemberCall(n, VI, new Set(['waitFor', 'waitUntil'])) || isNamedMemberCall(n, new Set(['expect']), new Set(['poll']))) {
      report(context, n, 'P3 sleep-or-poll')
      continue
    }
    if (c.type === 'Identifier' && tl.has(c.name)) {
      const imported = tl.get(c.name)
      if (imported === 'waitFor' || imported === 'waitForElementToBeRemoved' || /^find(All)?By/.test(imported)) report(context, n, 'P3 sleep-or-poll')
      continue
    }
    if (c.type === 'MemberExpression') {
      const p = propName(c)
      if (p === 'waitForTimeout') {
        report(context, n, 'P3 sleep-or-poll')
        continue
      }
      if (p === 'waitForLoadState') {
        const a0 = n.arguments[0]
        if (a0 && a0.type === 'Literal' && a0.value === 'networkidle') report(context, n, 'P3 sleep-or-poll')
        continue
      }
      if (p !== null && /^find(All)?By/.test(p)) {
        const o = unwrap(c.object)
        const fromScreen = isNode(o) && o.type === 'Identifier' && tl.get(o.name) === 'screen'
        const fromWithin = isNode(o) && o.type === 'CallExpression' && isNode(unwrap(o.callee)) && unwrap(o.callee).type === 'Identifier' && tl.get(unwrap(o.callee).name) === 'within'
        if (fromScreen || fromWithin) report(context, n, 'P3 sleep-or-poll')
      }
    }
  }
}

// ---------------------------------------------------------------- P4 — a real clock read

function p4(program, context, idx, { kind }) {
  if (kind !== 'test') return
  for (const n of idx.nodes) {
    if (!isClockRead(n)) continue
    const inSetSystemTime = ancestors(n, idx).some((a) => isNamedMemberCall(a, VI, new Set(['setSystemTime'])))
    if (inSetSystemTime) {
      report(context, n, 'P4 clock-read')
      continue
    }
    if (!fakedState(n, idx).pinned) report(context, n, 'P4 clock-read')
  }
}

// ---------------------------------------------------------------- P5 — unseeded randomness

function p5(program, context, idx, { kind }) {
  if (kind !== 'test') return
  for (const n of idx.nodes) {
    if (n.type !== 'CallExpression') continue
    const c = unwrap(n.callee)
    if (!isNode(c) || c.type !== 'MemberExpression') continue
    const p = propName(c)
    if (p === 'random' && isGlobalRef(c.object, 'Math')) {
      if (!isRandomSpied(n, idx)) report(context, n, 'P5 unseeded-random')
    } else if (p === 'getRandomValues' && isGlobalRef(c.object, 'crypto')) {
      report(context, n, 'P5 unseeded-random')
    }
  }
}

// ---------------------------------------------------------------- P6 — dynamic import or module reset in a test body

const BUILTIN_SPECIFIERS = new Set(['fs', 'path', 'url', 'os'])

function isMockFactoryCall(call) {
  return isNamedMemberCall(call, VI, new Set(['mock', 'doMock', 'hoisted']))
}

function p6(program, context, idx, { kind }) {
  if (kind !== 'test') return
  for (const n of idx.nodes) {
    let hit = false
    if (n.type === 'ImportExpression') {
      const src = unwrap(n.source)
      const builtin = isNode(src) && src.type === 'Literal' && typeof src.value === 'string' && (src.value.startsWith('node:') || BUILTIN_SPECIFIERS.has(src.value))
      hit = !builtin
    } else if (isNamedMemberCall(n, VI, new Set(['importActual', 'importMock', 'resetModules']))) {
      hit = true
    }
    if (!hit) continue
    if (!insideTestBody(n, idx)) continue
    if (insideCall(n, idx, isMockFactoryCall)) continue
    report(context, n, 'P6 dynamic-import')
  }
}

// ---------------------------------------------------------------- P7 — a real router that lazy-loads pages

const ROUTES_MODULE = /(^|\/)router(\/(routes|index))?(\.[cm]?[jt]s)?$/
const ROUTER_INDEX_MODULE = /(^|\/)router(\/index)?(\.[cm]?[jt]s)?$/
const STUB_HELPER_MODULE = /stubRouteComponents\.testUtils(\.[cm]?[jt]s)?$/

// The initialiser of a local `const name = ...` visible from `node`, searched outwards; null when the
// name is a parameter, an import or not found.
function resolveVar(name, node, idx) {
  for (const a of ancestors(node, idx)) {
    if (isFunctionNode(a) && a.params.some((prm) => subtreeHas(prm, (m) => m.type === 'Identifier' && m.name === name))) return null
    const body = Array.isArray(a.body) ? a.body : a.type === 'BlockStatement' ? a.body : null
    if (!body) continue
    for (const stmt of body) {
      const decl = stmt.type === 'ExportNamedDeclaration' && stmt.declaration ? stmt.declaration : stmt
      if (decl.type === 'VariableDeclaration') {
        for (const d of decl.declarations) {
          if (d.id.type === 'Identifier' && d.id.name === name && d.init) return d.init
        }
      } else if (decl.type === 'FunctionDeclaration' && decl.id && decl.id.name === name) {
        return decl
      }
    }
  }
  return null
}

function hasLazyComponent(root) {
  return subtreeNodes(root).some((n) => {
    if (n.type !== 'Property') return false
    const k = keyName(n)
    if (k !== 'component' && k !== 'components') return false
    return subtreeNodes(n.value).some((m) => isFunctionNode(m) && subtreeHas(m, (x) => x.type === 'ImportExpression'))
  })
}

function routesAreReal(valueNode, idx, depth) {
  const v = unwrap(valueNode)
  if (!isNode(v) || depth > 5) return false
  if (v.type === 'CallExpression' && isIdent(v.callee, 'stubRouteComponents')) {
    const imp = idx.imports.get('stubRouteComponents')
    if (imp && STUB_HELPER_MODULE.test(imp.source)) return false
    return v.arguments[0] ? routesAreReal(v.arguments[0], idx, depth + 1) : false
  }
  if (v.type === 'Identifier') {
    const imp = idx.imports.get(v.name)
    if (imp && ROUTES_MODULE.test(imp.source) && !/^vue-router$/.test(imp.source)) return true
    const init = resolveVar(v.name, valueNode, idx)
    return init ? routesAreReal(init, idx, depth + 1) : false
  }
  if (v.type === 'ArrayExpression' || isFunctionNode(v)) return hasLazyComponent(v)
  if (v.type === 'CallExpression') {
    const c = unwrap(v.callee)
    if (isNode(c) && c.type === 'Identifier') {
      const init = resolveVar(c.name, valueNode, idx)
      if (init && (isFunctionNode(unwrap(init)) || unwrap(init).type === 'ArrayExpression')) return hasLazyComponent(unwrap(init))
    }
  }
  return false
}

function aliasMap(root) {
  const map = new Map()
  for (const f of ['vite.config.ts', 'vitest.config.ts', 'vite.config.mts', 'vite.config.js']) {
    const p = path.join(root, f)
    if (!fs.existsSync(p)) continue
    const text = fs.readFileSync(p, 'utf8')
    const re = /['"](@[\w-]*|~)['"]\s*:\s*fileURLToPath\(\s*new URL\(\s*['"]([^'"]+)['"]/g
    let m
    while ((m = re.exec(text)) !== null) map.set(m[1], path.resolve(root, m[2]))
  }
  return map
}

function resolveSpecifier(spec, fromDir, aliases) {
  let abs = null
  if (spec.startsWith('.')) abs = path.resolve(fromDir, spec)
  else {
    for (const [alias, target] of aliases) {
      if (spec === alias) {
        abs = target
        break
      }
      if (spec.startsWith(alias + '/')) {
        abs = path.join(target, spec.slice(alias.length + 1))
        break
      }
    }
  }
  if (abs === null) return null
  return abs.replace(/\\/g, '/').replace(/\.(?:[cm]?[jt]sx?)$/, '').replace(/\/index$/, '')
}

function findModuleFile(abs) {
  const exts = ['.ts', '.mts', '.js', '.mjs']
  for (const e of exts) if (fs.existsSync(abs + e)) return abs + e
  for (const e of exts) if (fs.existsSync(path.join(abs, 'index' + e))) return path.join(abs, 'index' + e)
  return null
}

function p7(program, context, idx, { kind, root }) {
  if (kind !== 'test') return
  const createRouterNames = new Set()
  const vueRouterNamespaces = new Set()
  for (const [local, v] of idx.imports) {
    if (v.source !== 'vue-router') continue
    if (v.imported === 'createRouter') createRouterNames.add(local)
    else if (v.imported === '*' || v.imported === 'default') vueRouterNamespaces.add(local)
  }
  for (const n of idx.nodes) {
    if (n.type !== 'CallExpression') continue
    const c = unwrap(n.callee)
    const isCreate =
      (isNode(c) && c.type === 'Identifier' && createRouterNames.has(c.name)) ||
      (isNode(c) && c.type === 'MemberExpression' && propName(c) === 'createRouter' && isNode(unwrap(c.object)) && unwrap(c.object).type === 'Identifier' && vueRouterNamespaces.has(unwrap(c.object).name))
    if (!isCreate) continue
    const opts = n.arguments[0] ? unwrap(n.arguments[0]) : null
    if (!opts || opts.type !== 'ObjectExpression') continue
    const prop = opts.properties.find((p) => keyName(p) === 'routes')
    if (!prop) continue
    if (routesAreReal(prop.value, idx, 0)) report(context, n, 'P7 real-router')
  }

  // A router instance or factory imported from the app's own router/index, in a file that does not
  // vi.mock every page that router/index loads lazily.
  const aliases = aliasMap(root)
  const testDir = path.dirname(context.filename)
  const mocked = new Set()
  for (const n of idx.nodes) {
    if (!isNamedMemberCall(n, VI, new Set(['mock', 'doMock']))) continue
    const a0 = n.arguments[0]
    if (a0 && a0.type === 'Literal' && typeof a0.value === 'string') {
      const r = resolveSpecifier(a0.value, testDir, aliases)
      if (r !== null) mocked.add(r)
    }
  }
  for (const stmt of idx.program.body) {
    if (stmt.type !== 'ImportDeclaration' || stmt.importKind === 'type') continue
    const source = String(stmt.source.value)
    if (!ROUTER_INDEX_MODULE.test(source)) continue
    const abs = resolveSpecifier(source, testDir, aliases)
    if (abs === null) continue
    const file = findModuleFile(abs)
    if (file === null) continue
    const used = stmt.specifiers.some((spec) => {
      if (spec.importKind === 'type') return false
      const name = spec.local.name
      return idx.nodes.some((m) => {
        if (m.type !== 'CallExpression') return false
        const c = unwrap(m.callee)
        if (isNode(c) && c.type === 'Identifier') return c.name === name
        return isNode(c) && c.type === 'MemberExpression' && isIdent(c.object, name)
      })
    })
    if (!used) continue
    const text = fs.readFileSync(file, 'utf8')
    const re = /import\(\s*['"`]([^'"`]+)['"`]\s*\)/g
    let m
    let unmocked = false
    while ((m = re.exec(text)) !== null) {
      const r = resolveSpecifier(m[1], path.dirname(file), aliases)
      if (r === null || !mocked.has(r)) unmocked = true
    }
    if (unmocked) report(context, stmt, 'P7 real-router')
  }
}

// ---------------------------------------------------------------- P8 — any skip, and a silent early return

const SKIP_PROPS = new Set(['skip', 'skipIf', 'runIf', 'todo', 'only', 'fails', 'fixme'])
const SKIP_ROOTS = new Set(['it', 'test', 'describe', 'suite'])
const ENV_KEYS = new Set(['include', 'exclude'])

function isAssertCall(n) {
  if (!isNode(n) || n.type !== 'CallExpression') return false
  const root = calleeRoot(n.callee)
  if (root !== null && (root.startsWith('expect') || root.startsWith('assert'))) return true
  const c = unwrap(n.callee)
  const p = isNode(c) && c.type === 'MemberExpression' ? propName(c) : null
  return p !== null && (p.startsWith('expect') || p.startsWith('assert'))
}

function consequentReturns(stmt) {
  const cons = stmt.consequent
  if (cons.type === 'ReturnStatement') return true
  if (cons.type === 'BlockStatement') return cons.body.some((s) => s.type === 'ReturnStatement')
  return false
}

function p8(program, context, idx, { kind }) {
  if (kind === 'config') {
    for (const n of idx.nodes) {
      if (n.type !== 'Property') continue
      const k = keyName(n)
      if (k === null || !ENV_KEYS.has(k)) continue
      const dependsOnEnv = subtreeHas(n.value, (m) => m.type === 'MemberExpression' && propName(m) === 'env' && isGlobalRef(m.object, 'process'))
      if (dependsOnEnv) report(context, n, 'P8 skip')
    }
    return
  }
  if (kind !== 'test') return
  for (const n of idx.nodes) {
    if (n.type === 'MemberExpression') {
      const p = propName(n)
      const root = calleeRoot(n)
      if (p !== null && SKIP_PROPS.has(p) && root !== null && SKIP_ROOTS.has(root)) report(context, n, 'P8 skip')
      continue
    }
    if (n.type !== 'CallExpression' || !isTestCall(n)) continue
    const root = calleeRoot(n.callee)
    if (root !== 'it' && root !== 'test') continue
    const fn = n.arguments.map(unwrap).find((a) => isFunctionNode(a))
    if (!fn || fn.body.type !== 'BlockStatement') continue
    // vitest's per-test context: `skip()` / `ctx.skip()`
    const first = fn.params[0]
    const skipNames = new Set()
    const ctxNames = new Set()
    if (first && first.type === 'Identifier') ctxNames.add(first.name)
    if (first && first.type === 'ObjectPattern') {
      for (const prop of first.properties) {
        if (prop.type === 'Property' && keyName(prop) === 'skip' && prop.value.type === 'Identifier') skipNames.add(prop.value.name)
      }
    }
    for (const m of subtreeNodes(fn.body)) {
      if (m.type !== 'CallExpression') continue
      const c = unwrap(m.callee)
      if (isNode(c) && c.type === 'Identifier' && skipNames.has(c.name)) report(context, m, 'P8 skip')
      else if (isNode(c) && c.type === 'MemberExpression' && propName(c) === 'skip' && isNode(unwrap(c.object)) && unwrap(c.object).type === 'Identifier' && ctxNames.has(unwrap(c.object).name)) report(context, m, 'P8 skip')
    }
    // a return at the top level of the test before its first assertion
    let seenAssert = false
    for (const stmt of fn.body.body) {
      const hasAssert = subtreeHas(stmt, isAssertCall)
      if (!seenAssert && !hasAssert) {
        if (stmt.type === 'ReturnStatement' || (stmt.type === 'IfStatement' && consequentReturns(stmt))) report(context, stmt, 'P8 skip')
      }
      if (hasAssert) seenAssert = true
    }
  }
}

// ---------------------------------------------------------------- P9 — a write to a real repo path

const FS_MODULES = new Set(['fs', 'node:fs', 'fs/promises', 'node:fs/promises', 'fs-extra'])
const WRITE_FNS = new Set([
  'writeFile', 'writeFileSync', 'appendFile', 'appendFileSync', 'mkdir', 'mkdirSync', 'mkdtemp', 'mkdtempSync',
  'rm', 'rmSync', 'rmdir', 'rmdirSync', 'unlink', 'unlinkSync', 'rename', 'renameSync', 'copyFile', 'copyFileSync',
  'cp', 'cpSync', 'createWriteStream', 'truncate', 'truncateSync', 'outputFile', 'outputFileSync', 'ensureDir',
  'ensureDirSync', 'remove', 'removeSync', 'emptyDir', 'emptyDirSync', 'move', 'moveSync', 'copy', 'copySync',
])
const DEST_ONLY_FNS = new Set(['copyFile', 'copyFileSync', 'cp', 'cpSync', 'copy', 'copySync'])
const BOTH_FNS = new Set(['rename', 'renameSync', 'move', 'moveSync'])
const PATH_FNS = new Set(['join', 'resolve', 'normalize', 'dirname', 'basename', 'relative', 'format', 'fileURLToPath', 'pathToFileURL', 'toNamespacedPath'])
const SQLITE_RELATIVE = /sqlite:\/{3}(?!\/|:memory:)[^\s'"`]/

function isRelativeLiteral(s) {
  if (typeof s !== 'string' || s.length === 0) return false
  if (s.startsWith('/') || s.startsWith('\\') || s.startsWith('~')) return false
  if (/^[A-Za-z]:[\\/]/.test(s)) return false
  if (/^[A-Za-z][\w+.-]*:/.test(s)) return false
  return true
}

function combineTaint(list) {
  if (list.includes('tmp')) return 'tmp'
  if (list.includes('repo')) return 'repo'
  return null
}

// `first` is true when `node` is the first component of the path being built: a relative string
// literal only makes a path repo-relative there (`path.join(dir, 'a.txt')` is not, whatever `dir` is).
function taintOf(node, idx, depth, first = true) {
  node = unwrap(node)
  if (!isNode(node) || depth > 6) return null
  switch (node.type) {
    case 'Literal':
      return first && typeof node.value === 'string' && isRelativeLiteral(node.value) ? 'repo' : null
    case 'TemplateLiteral': {
      const parts = node.expressions.map((e, i) => taintOf(e, idx, depth + 1, first && i === 0 && !(node.quasis[0] && node.quasis[0].value.cooked)))
      const lead = node.quasis[0] ? node.quasis[0].value.cooked : ''
      if (first && lead && isRelativeLiteral(lead)) parts.push('repo')
      return combineTaint(parts)
    }
    case 'Identifier': {
      if (node.name === '__dirname' || node.name === '__filename') return 'repo'
      const init = resolveVar(node.name, node, idx)
      return init ? taintOf(init, idx, depth + 1, first) : null
    }
    case 'MemberExpression': {
      const o = unwrap(node.object)
      if (isNode(o) && o.type === 'MetaProperty') return 'repo'
      return null
    }
    case 'AwaitExpression':
      return taintOf(node.argument, idx, depth + 1, first)
    case 'BinaryExpression':
      return node.operator === '+' ? combineTaint([taintOf(node.left, idx, depth + 1, first), taintOf(node.right, idx, depth + 1, false)]) : null
    case 'LogicalExpression':
      return combineTaint([taintOf(node.left, idx, depth + 1, first), taintOf(node.right, idx, depth + 1, first)])
    case 'ConditionalExpression':
      return combineTaint([taintOf(node.consequent, idx, depth + 1, first), taintOf(node.alternate, idx, depth + 1, first)])
    case 'NewExpression':
      return isIdent(node.callee, 'URL') ? combineTaint(node.arguments.map((a, i) => taintOf(a, idx, depth + 1, first && i === 0))) : null
    case 'CallExpression': {
      const c = unwrap(node.callee)
      let name = null
      if (isNode(c) && c.type === 'Identifier') name = c.name
      else if (isNode(c) && c.type === 'MemberExpression') name = propName(c)
      if (name === 'cwd' && isNode(c) && c.type === 'MemberExpression' && isGlobalRef(c.object, 'process')) return 'repo'
      if (name === 'tmpdir' || name === 'mkdtemp' || name === 'mkdtempSync') return 'tmp'
      if (name !== null && PATH_FNS.has(name)) return combineTaint(node.arguments.map((a, i) => taintOf(a, idx, depth + 1, first && i === 0)))
      return null
    }
    default:
      return null
  }
}

function isFsModuleExpr(e) {
  e = unwrap(e)
  if (!isNode(e)) return false
  if (e.type === 'AwaitExpression') return isFsModuleExpr(e.argument)
  if (e.type === 'ImportExpression') return e.source.type === 'Literal' && FS_MODULES.has(String(e.source.value))
  if (e.type === 'CallExpression' && isIdent(e.callee, 'require')) {
    const a0 = e.arguments[0]
    return Boolean(a0) && a0.type === 'Literal' && FS_MODULES.has(String(a0.value))
  }
  return false
}

function isFsObject(o, idx) {
  o = unwrap(o)
  if (!isNode(o)) return false
  if (o.type === 'MemberExpression') return propName(o) === 'promises' && isFsObject(o.object, idx)
  if (o.type !== 'Identifier') return false
  const imp = idx.imports.get(o.name)
  if (imp) return FS_MODULES.has(imp.source)
  const init = resolveVar(o.name, o, idx)
  return init ? isFsModuleExpr(init) : false
}

function fsWriteName(call, idx) {
  const c = unwrap(call.callee)
  if (!isNode(c)) return null
  if (c.type === 'Identifier') {
    const imp = idx.imports.get(c.name)
    if (imp && FS_MODULES.has(imp.source) && WRITE_FNS.has(imp.imported)) return imp.imported
    return null
  }
  if (c.type === 'MemberExpression') {
    const p = propName(c)
    if (p !== null && WRITE_FNS.has(p) && isFsObject(c.object, idx)) return p
  }
  return null
}

function p9(program, context, idx, { kind }) {
  if (kind !== 'test') return
  for (const n of idx.nodes) {
    if (n.type === 'Literal' && typeof n.value === 'string' && SQLITE_RELATIVE.test(n.value)) {
      report(context, n, 'P9 repo-write')
      continue
    }
    if (n.type === 'TemplateElement' && SQLITE_RELATIVE.test(n.value.cooked ?? '')) {
      report(context, n, 'P9 repo-write')
      continue
    }
    if (n.type !== 'CallExpression') continue
    const fname = fsWriteName(n, idx)
    if (fname === null) continue
    const args = n.arguments
    let targets
    if (DEST_ONLY_FNS.has(fname)) targets = args[1] ? [args[1]] : []
    else if (BOTH_FNS.has(fname)) targets = args.slice(0, 2)
    else targets = args[0] ? [args[0]] : []
    const hit = targets.some((t) => taintOf(t, idx, 0) === 'repo')
    if (hit) report(context, n, 'P9 repo-write')
  }
}

// ---------------------------------------------------------------- plugin

export const rules = {
  'p1-time-limit': makeRule(p1),
  'p2-retry': makeRule(p2),
  'p3-sleep-or-poll': makeRule(p3),
  'p4-clock-read': makeRule(p4),
  'p5-unseeded-random': makeRule(p5),
  'p6-dynamic-import': makeRule(p6),
  'p7-real-router': makeRule(p7),
  'p8-skip': makeRule(p8),
  'p9-repo-write': makeRule(p9),
}

export const plugin = { rules }
