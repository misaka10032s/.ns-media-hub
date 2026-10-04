/**
 * Runtime write guard for tests (G3(c), pattern P9): the guard logic that `noRepoWrites.ts`, vitest's `setupFiles` entry,
 * installs. The logic lives here and not in the setup file because vitest never measures a setup file for coverage, so a
 * test can import this module and be measured.
 *
 * A static check cannot see a file written by product code that a test calls. So every `fs` and
 * `fs/promises` function that creates, changes, moves or removes a file is wrapped here: when its
 * target resolves inside the repo and not under `os.tmpdir()`, it throws instead of writing.
 * The runner's own outputs (given as `runnerOutput`) are allowed.
 *
 * The wrappers are installed on the CommonJS `fs` object and on `fs.promises`, and then `syncBuiltinESMExports()`
 * copies them to the named ESM exports of `node:fs` and `node:fs/promises`, so `import { writeFileSync } from
 * 'node:fs'` and `import { open } from 'node:fs/promises'`, bound before or after this module ran, are guarded too.
 * `open` (callback, sync and promise forms) is checked by its flags: a write flag (`w`, `a`, `r+`, `wx`, `ax`, or numeric
 * `O_WRONLY` / `O_RDWR` / `O_CREAT` / `O_APPEND` / `O_TRUNC`) on a repo path throws, so a `FileHandle` that can write
 * (`write`, `writeFile`, `appendFile`, `truncate`) can only come from an allowed path. Reads are never touched.
 */
import fs from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

type AnyFunction = (...args: unknown[]) => unknown;
type FunctionTable = Record<string, unknown>;

/** The values one install needs. */
export interface RepoWriteGuardOptions {
    /** Prefix of every error message the guard throws, such as `noRepoWrites`. */
    label: string;
    /** The key of the once-per-worker marker kept in the global registry (`Symbol.for(installedKey)`). */
    installedKey: string;
    /** Repo-relative paths, with forward slashes, a write may reach inside the repo (the runner's own outputs). */
    runnerOutput: readonly RegExp[];
}

/** What the wrappers of one install share. */
interface GuardContext {
    label: string;
    repoRoot: string;
    tmpRoot: string;
    runnerOutput: readonly RegExp[];
}

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
 * The repo root: the first folder, walking up from the current working folder, that holds a `.git` entry
 * (a folder in a main tree, a file in a worktree). `import.meta.url` is not used: under vitest it is not a `file:` URL.
 * @param label prefix of the error message
 * @throws Error naming the start folder when no folder up to the filesystem root holds a `.git` entry
 */
const findRepoRoot = (label: string): string => {
    const start = path.resolve(process.cwd());
    let current = start;
    for (;;) {
        if (fs.existsSync(path.join(current, '.git'))) {
            return current;
        }
        const parent = path.dirname(current);
        if (parent === current) {
            throw new Error(`${label}: no .git entry found in ${start} or any folder above it`);
        }
        current = parent;
    }
};

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
 * @param context the install's roots and runner-output patterns
 * @param absolute absolute path
 */
const isAllowed = (context: GuardContext, absolute: string): boolean => {
    if (isInside(context.tmpRoot, absolute)) {
        return true;
    }
    if (!isInside(context.repoRoot, absolute)) {
        return true;
    }
    const relative = path.relative(context.repoRoot, absolute).split(path.sep).join('/');
    return context.runnerOutput.some(pattern => pattern.test(relative));
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
        const writing = fs.constants.O_WRONLY | fs.constants.O_RDWR | fs.constants.O_CREAT | fs.constants.O_APPEND | fs.constants.O_TRUNC;
        return (flags & writing) !== 0;
    }
    return false;
};

/**
 * Replace `table[name]` with a version that checks the target path first.
 * @param context the install's roots, runner-output patterns and error label
 * @param table the object that holds the function
 * @param name function name
 * @param indexes argument positions that hold target paths
 * @param onlyWhenWriting true for `open`, which writes only when its flags say so
 */
const guard = (context: GuardContext, table: FunctionTable, name: string, indexes: number[], onlyWhenWriting: boolean): void => {
    const original = table[name];
    if (typeof original !== 'function') {
        return;
    }
    table[name] = (...args: unknown[]): unknown => {
        if (!onlyWhenWriting || opensForWrite(args[1])) {
            for (const index of indexes) {
                const absolute = targetOf(args[index]);
                if (absolute !== null && !isAllowed(context, absolute)) {
                    throw new Error(`${context.label}: ${name}() would write ${absolute}, which is inside the repo and outside a temp folder`);
                }
            }
        }
        return (original as AnyFunction)(...args);
    };
};

/**
 * Install the wrappers once per worker.
 * @param options the error label, the once-per-worker marker key and the runner-output patterns
 * @throws Error when no folder from the working folder up holds a `.git` entry
 */
export const installRepoWriteGuard = (options: RepoWriteGuardOptions): void => {
    const context: GuardContext = {
        label: options.label,
        repoRoot: findRepoRoot(options.label),
        tmpRoot: path.resolve(os.tmpdir()),
        runnerOutput: options.runnerOutput,
    };
    const installed = Symbol.for(options.installedKey);
    const registry = globalThis as unknown as Record<symbol, unknown>;
    if (registry[installed]) {
        return;
    }
    registry[installed] = true;
    const callbackFs = fs as unknown as FunctionTable;
    const promisesFs = fs.promises as unknown as FunctionTable;
    for (const name of Object.keys(TARGET_ARGUMENTS)) {
        const indexes = TARGET_ARGUMENTS[name];
        if (indexes === undefined) {
            continue;
        }
        guard(context, callbackFs, name, indexes, false);
        guard(context, promisesFs, name, indexes, false);
        if (SYNC_NAMES.includes(name)) {
            guard(context, callbackFs, `${name}Sync`, indexes, false);
        }
    }
    guard(context, callbackFs, 'open', [0], true);
    guard(context, callbackFs, 'openSync', [0], true);
    guard(context, promisesFs, 'open', [0], true);
    // After every patch above: copy the guarded functions to the named ESM exports of node:fs and node:fs/promises.
    syncBuiltinESMExports();
};
