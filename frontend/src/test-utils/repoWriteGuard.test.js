// @vitest-environment node
/**
 * Tests of `repoWriteGuard.ts`, the runtime write guard behind `noRepoWrites.ts` (G3(c), pattern P9).
 *
 * Every refusal case runs over stand-in functions that record their calls and write nothing, so even a guard that let a
 * repo path through could not write inside the repo. Real files are written only under the OS temp folder, in a folder
 * each case creates and removes. Each case installs the guard under its own marker key, then puts back every function
 * it replaced (on `fs`, on `fs.promises` and, through `syncBuiltinESMExports()`, on the named ESM exports), so no case
 * sees another case's state. The file runs in the node environment: `URL` and `Uint8Array` must be the ones `fs` knows.
 *
 * A case's paths (`fixture`): `repoRoot` is the repo root as the guard finds it (first folder from the working folder
 * upwards with a `.git` entry); `tmpDir` is a folder the case created under the OS temp folder and `tmpFile` a file path
 * inside it; `repoFile` is a file path inside the repo that is not a runner output; `outsideFile` is a path outside the
 * repo and outside the temp folder; `relativeName` is a path relative to the working folder, which is inside the repo;
 * `packageJson` is the `package.json` of the package the tests run in (a real file inside the repo).
 */
import fs, { writeFileSync as boundWriteFileSync } from 'node:fs';
import { open as boundOpen } from 'node:fs/promises';
import { syncBuiltinESMExports } from 'node:module';
import os from 'node:os';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { installRepoWriteGuard } from './repoWriteGuard';

/** Which argument positions of each function name a target path (the spec in the module's header). */
const TARGETS = {
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
/** The names that also have a `...Sync` form. */
const SYNC_NAMES = ['writeFile', 'appendFile', 'mkdir', 'mkdtemp', 'rm', 'rmdir', 'unlink', 'truncate', 'copyFile', 'cp', 'rename', 'symlink', 'link'];

/** Every guarded function: which table holds it, its name and its target positions. */
const FORMS = [
    ...Object.entries(TARGETS).map(([name, indexes]) => ({ table: 'fs', name, indexes })),
    ...SYNC_NAMES.map(name => ({ table: 'fs', name: `${name}Sync`, indexes: TARGETS[name] ?? [] })),
    ...Object.entries(TARGETS)
        .filter(([name]) => name !== 'createWriteStream')
        .map(([name, indexes]) => ({ table: 'promises', name, indexes })),
];
/** One case per function and per target position. */
const REFUSAL_CASES = FORMS.flatMap(form => form.indexes.map(index => ({ ...form, index })));
/** The three forms of `open`. */
const OPEN_FORMS = [
    { table: 'fs', name: 'open' },
    { table: 'fs', name: 'openSync' },
    { table: 'promises', name: 'open' },
];
const WRITE_FLAGS = ['w', 'a', 'r+', 'wx', 'ax', 'w+', 'a+', fs.constants.O_WRONLY, fs.constants.O_RDWR, fs.constants.O_CREAT, fs.constants.O_APPEND, fs.constants.O_TRUNC];
const READ_FLAGS = ['r', 'rs', fs.constants.O_RDONLY, undefined, null, {}];
const WRITE_FLAG_CASES = OPEN_FORMS.flatMap(form => WRITE_FLAGS.map(flags => ({ ...form, flags })));
const READ_FLAG_CASES = OPEN_FORMS.flatMap(form => READ_FLAGS.map(flags => ({ ...form, flags })));

/** The value every stand-in returns. */
const STAND_IN_RESULT = 'stand-in result';
/** The once-per-worker marker key these tests install under (not the key `noRepoWrites.ts` uses). */
const MARKER_KEY = 'misaka.repoWriteGuard.test.installed';

const fsTable = fs;
const promisesTable = fs.promises;
const registry = globalThis;
const tableOf = name => (name === 'fs' ? fsTable : promisesTable);

/** The names `repoWriteGuard.ts` may replace on `fs`, and on `fs.promises`. */
const FS_NAMES = [...Object.keys(TARGETS), ...SYNC_NAMES.map(name => `${name}Sync`), 'open', 'openSync'];
const PROMISES_NAMES = [...Object.keys(TARGETS).filter(name => name !== 'createWriteStream'), 'open'];

let fixture;
let saved = [];
let standIns = {};

/**
 * The repo root the way the guard defines it: the first folder from the working folder upwards that holds `.git`.
 */
const findRoot = () => {
    let current = path.resolve(process.cwd());
    while (!fs.existsSync(path.join(current, '.git'))) {
        const parent = path.dirname(current);
        expect(parent).not.toBe(current);
        current = parent;
    }
    return current;
};

/** Call `table[name](...args)`. */
const call = (table, name, args) => table[name](...args);

/** The message the guard throws. */
const refusal = (label, name, absolute) =>
    `${label}: ${name}() would write ${absolute}, which is inside the repo and outside a temp folder`;

/** The options of one install. */
const options = (overrides = {}) => ({
    label: 'testGuard',
    installedKey: MARKER_KEY,
    runnerOutput: [],
    ...overrides,
});

/** Replace every guarded function with a stand-in that records its calls and writes nothing. */
const useStandIns = () => {
    for (const [tableName, names] of [['fs', FS_NAMES], ['promises', PROMISES_NAMES]]) {
        for (const name of names) {
            const standIn = vi.fn(() => STAND_IN_RESULT);
            tableOf(tableName)[name] = standIn;
            standIns[`${tableName}.${name}`] = standIn;
        }
    }
};

/** The stand-in of `table.name`. */
const standInOf = (table, name) => {
    const standIn = standIns[`${table}.${name}`];
    expect(standIn).toBeDefined();
    return standIn;
};

/** An argument list for a function whose positions in `positions` hold `value` and whose others hold the temp file. */
const argumentsFor = (positions, value) =>
    Array.from({ length: Math.max(...positions) + 1 }, (_, position) => (positions.includes(position) ? value : fixture.tmpFile));

/** A write through the stand-in `fs.writeFileSync`, which the installed guard wraps. */
const guardedWrite = target => call(fsTable, 'writeFileSync', [target, 'data']);

beforeEach(() => {
    saved = [];
    standIns = {};
    for (const [tableName, names] of [['fs', FS_NAMES], ['promises', PROMISES_NAMES]]) {
        for (const name of names) {
            const table = tableOf(tableName);
            saved.push({ table, name, descriptor: Object.getOwnPropertyDescriptor(table, name) });
        }
    }
    const repoRoot = findRoot();
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'repo-write-guard-'));
    fixture = {
        repoRoot,
        tmpDir,
        tmpFile: path.join(tmpDir, 'out.txt'),
        repoFile: path.join(repoRoot, 'guard-probe-folder', 'out.txt'),
        outsideFile: path.join(path.dirname(repoRoot), 'guard-outside-probe-folder', 'out.txt'),
        relativeName: 'guard-relative-probe.txt',
        packageJson: path.join(process.cwd(), 'package.json'),
    };
});

afterEach(() => {
    vi.restoreAllMocks();
    for (const { table, name, descriptor } of saved) {
        if (descriptor === undefined) {
            delete table[name];
        } else {
            Object.defineProperty(table, name, descriptor);
        }
    }
    syncBuiltinESMExports();
    delete registry[Symbol.for(MARKER_KEY)];
    fs.rmSync(fixture.tmpDir, { recursive: true, force: true });
});

describe('a write inside the repo is refused', () => {
    it.each(REFUSAL_CASES)('$table.$name refuses a repo path at argument $index with the guard message', ({ table, name, index }) => {
        useStandIns();
        installRepoWriteGuard(options());
        expect(() => call(tableOf(table), name, argumentsFor([index], fixture.repoFile))).toThrow(new Error(refusal('testGuard', name, fixture.repoFile)));
        expect(standInOf(table, name)).not.toHaveBeenCalled();
    });

    it.each(FORMS.filter(form => form.indexes[0] !== 0))('$table.$name does not check argument 0, which is not a target', ({ table, name, indexes }) => {
        useStandIns();
        installRepoWriteGuard(options());
        const args = argumentsFor(indexes, fixture.tmpFile);
        args[0] = fixture.repoFile;
        expect(call(tableOf(table), name, args)).toBe(STAND_IN_RESULT);
        expect(standInOf(table, name).mock.calls).toEqual([args]);
    });

    it('rename refuses a repo path at either position and allows two temp paths', () => {
        useStandIns();
        installRepoWriteGuard(options());
        expect(() => call(fsTable, 'renameSync', [fixture.repoFile, fixture.tmpFile])).toThrow(new Error(refusal('testGuard', 'renameSync', fixture.repoFile)));
        expect(() => call(fsTable, 'renameSync', [fixture.tmpFile, fixture.repoFile])).toThrow(new Error(refusal('testGuard', 'renameSync', fixture.repoFile)));
        expect(call(fsTable, 'renameSync', [fixture.tmpFile, fixture.tmpFile])).toBe(STAND_IN_RESULT);
        expect(standInOf('fs', 'renameSync').mock.calls).toEqual([[fixture.tmpFile, fixture.tmpFile]]);
    });

    it('the repo root itself is refused', () => {
        useStandIns();
        installRepoWriteGuard(options());
        expect(() => call(fsTable, 'rmSync', [fixture.repoRoot])).toThrow(new Error(refusal('testGuard', 'rmSync', fixture.repoRoot)));
    });

    it('a folder name that starts with two dots inside the repo is still inside the repo', () => {
        useStandIns();
        installRepoWriteGuard(options());
        const dotted = path.join(fixture.repoRoot, '..hidden', 'out.txt');
        expect(() => guardedWrite(dotted)).toThrow(new Error(refusal('testGuard', 'writeFileSync', dotted)));
    });

    it('a path with dot-dot segments is judged by where it resolves', () => {
        useStandIns();
        installRepoWriteGuard(options());
        const staysInside = path.join(fixture.repoRoot, 'a-folder', '..', 'inside-probe.txt');
        const resolvedInside = path.join(fixture.repoRoot, 'inside-probe.txt');
        expect(() => guardedWrite(staysInside)).toThrow(new Error(refusal('testGuard', 'writeFileSync', resolvedInside)));
        const escapes = path.join(fixture.repoRoot, 'a-folder', '..', '..', 'escaped-probe.txt');
        expect(guardedWrite(escapes)).toBe(STAND_IN_RESULT);
        expect(standInOf('fs', 'writeFileSync').mock.calls).toEqual([[escapes, 'data']]);
    });

    it('the label of the install prefixes the message', () => {
        useStandIns();
        installRepoWriteGuard(options({ label: 'otherLabel' }));
        expect(() => guardedWrite(fixture.repoFile)).toThrow(new Error(refusal('otherLabel', 'writeFileSync', fixture.repoFile)));
    });
});

describe('how an argument is read as a path', () => {
    it('a relative string is resolved against the working folder, which is inside the repo', () => {
        useStandIns();
        installRepoWriteGuard(options());
        const absolute = path.resolve(fixture.relativeName);
        expect(() => guardedWrite(fixture.relativeName)).toThrow(new Error(refusal('testGuard', 'writeFileSync', absolute)));
    });

    it('a file: URL is read as its path: a repo URL is refused, a temp URL is allowed', () => {
        useStandIns();
        installRepoWriteGuard(options());
        expect(() => guardedWrite(pathToFileURL(fixture.repoFile))).toThrow(new Error(refusal('testGuard', 'writeFileSync', fixture.repoFile)));
        const tmpUrl = pathToFileURL(fixture.tmpFile);
        expect(guardedWrite(tmpUrl)).toBe(STAND_IN_RESULT);
        expect(standInOf('fs', 'writeFileSync').mock.calls).toEqual([[tmpUrl, 'data']]);
    });

    it('a URL that is not file: is not a path and passes through', () => {
        useStandIns();
        installRepoWriteGuard(options());
        const httpUrl = new URL('http://example.invalid/out.txt');
        expect(guardedWrite(httpUrl)).toBe(STAND_IN_RESULT);
        expect(standInOf('fs', 'writeFileSync').mock.calls).toEqual([[httpUrl, 'data']]);
    });

    it('a byte-array path is decoded: a repo path is refused, a temp path is allowed', () => {
        useStandIns();
        installRepoWriteGuard(options());
        expect(() => guardedWrite(Buffer.from(fixture.repoFile))).toThrow(new Error(refusal('testGuard', 'writeFileSync', fixture.repoFile)));
        const tmpBytes = Buffer.from(fixture.tmpFile);
        expect(guardedWrite(tmpBytes)).toBe(STAND_IN_RESULT);
        expect(standInOf('fs', 'writeFileSync').mock.calls).toEqual([[tmpBytes, 'data']]);
    });

    it('a file descriptor, undefined and an object are not paths and pass through', () => {
        useStandIns();
        installRepoWriteGuard(options());
        const plainObject = {};
        expect([guardedWrite(3), guardedWrite(undefined), guardedWrite(plainObject)]).toEqual([STAND_IN_RESULT, STAND_IN_RESULT, STAND_IN_RESULT]);
        expect(standInOf('fs', 'writeFileSync').mock.calls).toEqual([[3, 'data'], [undefined, 'data'], [plainObject, 'data']]);
    });
});

describe('a write outside the repo, in the temp folder or to a runner output is allowed', () => {
    it.each(FORMS)('$table.$name passes a temp path at every target position through to the original', ({ table, name, indexes }) => {
        useStandIns();
        installRepoWriteGuard(options());
        const args = argumentsFor(indexes, fixture.tmpFile);
        expect(call(tableOf(table), name, args)).toBe(STAND_IN_RESULT);
        expect(standInOf(table, name).mock.calls).toEqual([args]);
    });

    it.each(FORMS)('$table.$name passes a path outside the repo and outside the temp folder through', ({ table, name, indexes }) => {
        useStandIns();
        installRepoWriteGuard(options());
        const args = argumentsFor(indexes, fixture.outsideFile);
        expect(call(tableOf(table), name, args)).toBe(STAND_IN_RESULT);
        expect(standInOf(table, name).mock.calls).toEqual([args]);
    });

    it('the temp folder itself is allowed', () => {
        useStandIns();
        installRepoWriteGuard(options());
        expect(call(fsTable, 'mkdirSync', [os.tmpdir()])).toBe(STAND_IN_RESULT);
        expect(standInOf('fs', 'mkdirSync').mock.calls).toEqual([[os.tmpdir()]]);
    });

    it('a sibling folder whose name starts with the repo folder name is outside the repo', () => {
        useStandIns();
        installRepoWriteGuard(options());
        const sibling = `${fixture.repoRoot}-sibling${path.sep}out.txt`;
        expect(guardedWrite(sibling)).toBe(STAND_IN_RESULT);
        expect(standInOf('fs', 'writeFileSync').mock.calls).toEqual([[sibling, 'data']]);
    });

    it('real writes under the temp folder succeed through every wrapped route, including a binding made before the guard', async () => {
        installRepoWriteGuard(options());
        const second = path.join(fixture.tmpDir, 'second.txt');
        const third = path.join(fixture.tmpDir, 'third.txt');
        const fourth = path.join(fixture.tmpDir, 'fourth.txt');
        const fifth = path.join(fixture.tmpDir, 'fifth.txt');
        fs.writeFileSync(fixture.tmpFile, 'sync');
        await fs.promises.writeFile(second, 'promise');
        const handle = await boundOpen(third, 'w');
        await handle.writeFile('handle');
        await handle.close();
        boundWriteFileSync(fourth, 'bound');
        fs.appendFileSync(fifth, 'one');
        fs.appendFileSync(fifth, 'two');
        fs.mkdirSync(path.join(fixture.tmpDir, 'made', 'deeper'), { recursive: true });
        expect([
            fs.readFileSync(fixture.tmpFile, 'utf8'),
            fs.readFileSync(second, 'utf8'),
            fs.readFileSync(third, 'utf8'),
            fs.readFileSync(fourth, 'utf8'),
            fs.readFileSync(fifth, 'utf8'),
            fs.statSync(path.join(fixture.tmpDir, 'made', 'deeper')).isDirectory(),
        ]).toEqual(['sync', 'promise', 'handle', 'bound', 'onetwo', true]);
    });

    it('a runner output inside the repo is allowed, anything else inside the repo is refused', () => {
        useStandIns();
        installRepoWriteGuard(options({ runnerOutput: [/^guard-runner-out(\/|$)/, /^second-runner-out\//] }));
        const first = path.join(fixture.repoRoot, 'guard-runner-out', 'nested', 'out.txt');
        const second = path.join(fixture.repoRoot, 'second-runner-out', 'out.txt');
        const other = path.join(fixture.repoRoot, 'not-runner-out', 'out.txt');
        expect(guardedWrite(first)).toBe(STAND_IN_RESULT);
        expect(guardedWrite(second)).toBe(STAND_IN_RESULT);
        expect(() => guardedWrite(other)).toThrow(new Error(refusal('testGuard', 'writeFileSync', other)));
        expect(standInOf('fs', 'writeFileSync').mock.calls).toEqual([[first, 'data'], [second, 'data']]);
    });

    it('with no runner output every repo path is refused, including one named like a runner output', () => {
        useStandIns();
        installRepoWriteGuard(options({ runnerOutput: [] }));
        const named = path.join(fixture.repoRoot, 'guard-runner-out', 'out.txt');
        expect(() => guardedWrite(named)).toThrow(new Error(refusal('testGuard', 'writeFileSync', named)));
    });
});

describe('open is judged by its flags', () => {
    it.each(WRITE_FLAG_CASES)('$table.$name with flags $flags is refused for a repo path', ({ table, name, flags }) => {
        useStandIns();
        installRepoWriteGuard(options());
        expect(() => call(tableOf(table), name, [fixture.repoFile, flags])).toThrow(new Error(refusal('testGuard', name, fixture.repoFile)));
        expect(standInOf(table, name)).not.toHaveBeenCalled();
    });

    it.each(WRITE_FLAG_CASES)('$table.$name with flags $flags is allowed for a temp path', ({ table, name, flags }) => {
        useStandIns();
        installRepoWriteGuard(options());
        expect(call(tableOf(table), name, [fixture.tmpFile, flags])).toBe(STAND_IN_RESULT);
        expect(standInOf(table, name).mock.calls).toEqual([[fixture.tmpFile, flags]]);
    });

    it.each(READ_FLAG_CASES)('$table.$name with flags $flags is allowed for a repo path', ({ table, name, flags }) => {
        useStandIns();
        installRepoWriteGuard(options());
        expect(call(tableOf(table), name, [fixture.repoFile, flags])).toBe(STAND_IN_RESULT);
        expect(standInOf(table, name).mock.calls).toEqual([[fixture.repoFile, flags]]);
    });

    it('open called with only a path (no flags) is allowed', () => {
        useStandIns();
        installRepoWriteGuard(options());
        expect(call(fsTable, 'openSync', [fixture.repoFile])).toBe(STAND_IN_RESULT);
        expect(standInOf('fs', 'openSync').mock.calls).toEqual([[fixture.repoFile]]);
    });

    it('open with a write flag on a file descriptor is not a path and passes through', () => {
        useStandIns();
        installRepoWriteGuard(options());
        expect(call(fsTable, 'openSync', [7, 'w'])).toBe(STAND_IN_RESULT);
        expect(standInOf('fs', 'openSync').mock.calls).toEqual([[7, 'w']]);
    });

    it('a real read-only open of a repo file succeeds and a real write open of it is refused', () => {
        installRepoWriteGuard(options());
        const descriptor = fs.openSync(fixture.packageJson, 'r');
        fs.closeSync(descriptor);
        expect(descriptor).toBeGreaterThan(2);
        expect(() => fs.openSync(fixture.packageJson, 'r+')).toThrow(new Error(refusal('testGuard', 'openSync', fixture.packageJson)));
    });

    it('a real read of a repo file is untouched', () => {
        installRepoWriteGuard(options());
        expect(JSON.parse(fs.readFileSync(fixture.packageJson, 'utf8'))).toHaveProperty('name');
    });
});

describe('the named ESM exports of node:fs and node:fs/promises are guarded', () => {
    it('a named import of writeFileSync, bound before the guard, is refused for a repo path and allowed for a temp path', () => {
        useStandIns();
        installRepoWriteGuard(options());
        expect(() => boundWriteFileSync(fixture.repoFile, 'data')).toThrow(new Error(refusal('testGuard', 'writeFileSync', fixture.repoFile)));
        expect(boundWriteFileSync(fixture.tmpFile, 'data')).toBe(STAND_IN_RESULT);
        expect(standInOf('fs', 'writeFileSync').mock.calls).toEqual([[fixture.tmpFile, 'data']]);
    });

    it('a named import of open from node:fs/promises, bound before the guard, judges by flags', () => {
        useStandIns();
        installRepoWriteGuard(options());
        expect(() => boundOpen(fixture.repoFile, 'w')).toThrow(new Error(refusal('testGuard', 'open', fixture.repoFile)));
        expect(boundOpen(fixture.repoFile, 'r')).toBe(STAND_IN_RESULT);
        expect(standInOf('promises', 'open').mock.calls).toEqual([[fixture.repoFile, 'r']]);
    });
});

describe('installing', () => {
    it('sets the once-per-worker marker under Symbol.for(installedKey)', () => {
        useStandIns();
        expect(registry[Symbol.for(MARKER_KEY)]).toBeUndefined();
        installRepoWriteGuard(options());
        expect(registry[Symbol.for(MARKER_KEY)]).toBe(true);
    });

    it('installing again under the same key wraps nothing a second time', () => {
        useStandIns();
        installRepoWriteGuard(options());
        const afterFirst = FS_NAMES.map(name => fsTable[name]);
        const promisesAfterFirst = PROMISES_NAMES.map(name => promisesTable[name]);
        installRepoWriteGuard(options({ label: 'secondInstall', runnerOutput: [/.*/] }));
        FS_NAMES.forEach((name, position) => expect(fsTable[name]).toBe(afterFirst[position]));
        PROMISES_NAMES.forEach((name, position) => expect(promisesTable[name]).toBe(promisesAfterFirst[position]));
        expect(() => guardedWrite(fixture.repoFile)).toThrow(new Error(refusal('testGuard', 'writeFileSync', fixture.repoFile)));
    });

    it('a marker that is already set means nothing is replaced', () => {
        useStandIns();
        registry[Symbol.for(MARKER_KEY)] = true;
        installRepoWriteGuard(options());
        expect(fsTable['writeFileSync']).toBe(standInOf('fs', 'writeFileSync'));
        expect(promisesTable['writeFile']).toBe(standInOf('promises', 'writeFile'));
        expect(guardedWrite(fixture.repoFile)).toBe(STAND_IN_RESULT);
    });

    it('a different key installs its own wrappers', () => {
        useStandIns();
        const stand = standInOf('fs', 'writeFileSync');
        installRepoWriteGuard(options({ installedKey: `${MARKER_KEY}.other` }));
        expect(fsTable['writeFileSync']).not.toBe(stand);
        expect(registry[Symbol.for(`${MARKER_KEY}.other`)]).toBe(true);
        expect(registry[Symbol.for(MARKER_KEY)]).toBeUndefined();
        delete registry[Symbol.for(`${MARKER_KEY}.other`)];
    });

    it('every guarded function is replaced and no other function is', () => {
        useStandIns();
        const before = FS_NAMES.map(name => fsTable[name]);
        const promisesBefore = PROMISES_NAMES.map(name => promisesTable[name]);
        const untouched = [fsTable['readFileSync'], fsTable['statSync'], fsTable['readdirSync'], fsTable['chmodSync'], promisesTable['readFile'], promisesTable['stat']];
        installRepoWriteGuard(options());
        FS_NAMES.forEach((name, position) => expect(fsTable[name]).not.toBe(before[position]));
        PROMISES_NAMES.forEach((name, position) => expect(promisesTable[name]).not.toBe(promisesBefore[position]));
        expect([fsTable['readFileSync'], fsTable['statSync'], fsTable['readdirSync'], fsTable['chmodSync'], promisesTable['readFile'], promisesTable['stat']]).toEqual(untouched);
    });

    it('a function that does not exist is skipped without an error', () => {
        useStandIns();
        fsTable['link'] = undefined;
        promisesTable['symlink'] = undefined;
        installRepoWriteGuard(options());
        expect([fsTable['link'], promisesTable['symlink']]).toEqual([undefined, undefined]);
        expect(() => call(fsTable, 'unlinkSync', [fixture.repoFile])).toThrow(new Error(refusal('testGuard', 'unlinkSync', fixture.repoFile)));
    });

    it('finds the repo root by walking up from the working folder to the nearest .git entry', () => {
        useStandIns();
        const nested = path.join(fixture.repoRoot, 'nested-probe');
        const cwdSpy = vi.spyOn(process, 'cwd').mockReturnValue(path.join(nested, 'deeper'));
        const existsSpy = vi.spyOn(fs, 'existsSync').mockImplementation(candidate => candidate === path.join(nested, '.git'));
        installRepoWriteGuard(options());
        cwdSpy.mockRestore();
        existsSpy.mockRestore();
        const insideNested = path.join(nested, 'out.txt');
        const outsideNested = path.join(fixture.repoRoot, 'other-probe', 'out.txt');
        expect(() => guardedWrite(insideNested)).toThrow(new Error(refusal('testGuard', 'writeFileSync', insideNested)));
        expect(guardedWrite(outsideNested)).toBe(STAND_IN_RESULT);
    });

    it('throws naming the start folder when no folder up to the filesystem root holds a .git entry', () => {
        useStandIns();
        const start = path.join(path.parse(os.tmpdir()).root, 'no-git-probe', 'deeper');
        const cwdSpy = vi.spyOn(process, 'cwd').mockReturnValue(start);
        const existsSpy = vi.spyOn(fs, 'existsSync').mockReturnValue(false);
        let thrown;
        try {
            installRepoWriteGuard(options());
        } catch (error) {
            thrown = error;
        }
        cwdSpy.mockRestore();
        existsSpy.mockRestore();
        expect(thrown).toEqual(new Error(`testGuard: no .git entry found in ${path.resolve(start)} or any folder above it`));
        expect(registry[Symbol.for(MARKER_KEY)]).toBeUndefined();
        expect(guardedWrite(fixture.repoFile)).toBe(STAND_IN_RESULT);
    });
});
