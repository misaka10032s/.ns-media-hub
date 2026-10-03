/**
 * Runtime write guard for tests (G3(c), pattern P9). Meant for vitest's `setupFiles`.
 *
 * A static check cannot see a file written by product code that a test calls. So every `fs` and
 * `fs/promises` function that creates, changes, moves or removes a file is wrapped here: when its
 * target resolves inside the repo and not under `os.tmpdir()`, it throws instead of writing.
 * The runner's own outputs (`coverage/`, `node_modules/.vite*`, `node_modules/.vitest*`) are allowed.
 *
 * The wrappers are installed on the CommonJS `fs` object and then `syncBuiltinESMExports()` copies
 * them to the named ESM exports, so `import { writeFileSync } from 'node:fs'` is guarded too.
 */
import fs from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

type AnyFunction = (...args: unknown[]) => unknown;
type FunctionTable = Record<string, unknown>;

/**
 * The repo root: the first folder, walking up from the current working folder, that holds a `.git` entry
 * (a folder in a main tree, a file in a worktree). `import.meta.url` is not used: under vitest it is not a `file:` URL.
 * @throws Error naming the start folder when no folder up to the filesystem root holds a `.git` entry
 */
const findRepoRoot = (): string => {
    const start = path.resolve(process.cwd());
    let current = start;
    for (;;) {
        if (fs.existsSync(path.join(current, '.git'))) {
            return current;
        }
        const parent = path.dirname(current);
        if (parent === current) {
            throw new Error(`noRepoWrites: no .git entry found in ${start} or any folder above it`);
        }
        current = parent;
    }
};
const REPO_ROOT = findRepoRoot();
const TMP_ROOT = path.resolve(os.tmpdir());
const RUNNER_OUTPUT = [
    /(^|\/)coverage(\/|$)/,
    /(^|\/)node_modules\/\.vite[^/]*(\/|$)/,
    /(^|\/)node_modules\/\.vitest[^/]*(\/|$)/,
];
const INSTALLED = Symbol.for('misaka.noRepoWrites.installed');

/** Which argument positions of each function name a target path. */
const TARGET_ARGUMENTS: Record<string, number[]> = {
    writeFile: [0],
    appendFile: [0],
    mkdir: [0],
    mkdtemp: [0],
    rm: [0],
    rmdir: [0],
    unlink: [0],
    truncate: [0],
    createWriteStream: [0],
    copyFile: [1],
    cp: [1],
    rename: [0, 1],
    symlink: [1],
    link: [1],
};
const SYNC_NAMES = ['writeFile', 'appendFile', 'mkdir', 'mkdtemp', 'rm', 'rmdir', 'unlink', 'truncate', 'copyFile', 'cp', 'rename', 'symlink', 'link'];

/**
 * Whether `child` is `parent` itself or lies under it.
 * @param parent absolute folder
 * @param child absolute path
 */
const isInside = (parent: string, child: string): boolean => {
    const relative = path.relative(parent, child);
    return relative === '' || (relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
};

/**
 * The absolute path an fs argument points at, or null for a file descriptor or anything else.
 * @param argument first or second argument of an fs call
 */
const targetOf = (argument: unknown): string | null => {
    if (typeof argument === 'string') {
        return path.resolve(argument);
    }
    if (argument instanceof URL) {
        return argument.protocol === 'file:' ? path.resolve(fileURLToPath(argument)) : null;
    }
    if (argument instanceof Uint8Array) {
        return path.resolve(new TextDecoder().decode(argument));
    }
    return null;
};

/**
 * Whether a write to `absolute` is allowed: under the temp folder, outside the repo, or a runner output.
 * @param absolute absolute path
 */
const isAllowed = (absolute: string): boolean => {
    if (isInside(TMP_ROOT, absolute)) {
        return true;
    }
    if (!isInside(REPO_ROOT, absolute)) {
        return true;
    }
    const relative = path.relative(REPO_ROOT, absolute).split(path.sep).join('/');
    return RUNNER_OUTPUT.some(pattern => pattern.test(relative));
};

/**
 * Whether the `flags` argument of `open` asks for writing.
 * @param flags string flags such as `'w'`, or numeric flags
 */
const opensForWrite = (flags: unknown): boolean => {
    if (typeof flags === 'string') {
        return /[wa+]/.test(flags);
    }
    if (typeof flags === 'number') {
        const writing = fs.constants.O_WRONLY | fs.constants.O_RDWR | fs.constants.O_CREAT;
        return (flags & writing) !== 0;
    }
    return false;
};

/**
 * Replace `table[name]` with a version that checks the target path first.
 * @param table the object that holds the function
 * @param name function name
 * @param indexes argument positions that hold target paths
 * @param onlyWhenWriting true for `open`, which writes only when its flags say so
 */
const guard = (table: FunctionTable, name: string, indexes: number[], onlyWhenWriting: boolean): void => {
    const original = table[name];
    if (typeof original !== 'function') {
        return;
    }
    table[name] = (...args: unknown[]): unknown => {
        if (!onlyWhenWriting || opensForWrite(args[1])) {
            for (const index of indexes) {
                const absolute = targetOf(args[index]);
                if (absolute !== null && !isAllowed(absolute)) {
                    throw new Error(`noRepoWrites: ${name}() would write ${absolute}, which is inside the repo and outside a temp folder`);
                }
            }
        }
        return (original as AnyFunction)(...args);
    };
};

/** Install the wrappers once per worker. */
const install = (): void => {
    const registry = globalThis as unknown as Record<symbol, unknown>;
    if (registry[INSTALLED]) {
        return;
    }
    registry[INSTALLED] = true;
    const callbackFs = fs as unknown as FunctionTable;
    const promisesFs = fs.promises as unknown as FunctionTable;
    for (const name of Object.keys(TARGET_ARGUMENTS)) {
        const indexes = TARGET_ARGUMENTS[name];
        if (indexes === undefined) {
            continue;
        }
        guard(callbackFs, name, indexes, false);
        guard(promisesFs, name, indexes, false);
        if (SYNC_NAMES.includes(name)) {
            guard(callbackFs, `${name}Sync`, indexes, false);
        }
    }
    guard(callbackFs, 'open', [0], true);
    guard(callbackFs, 'openSync', [0], true);
    syncBuiltinESMExports();
};

install();
