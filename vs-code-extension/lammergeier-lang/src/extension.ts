// Lammergeier VS Code extension entry point.
//
// Spawns the lammergeier-lsp launcher over stdio and wires it to a
// LanguageClient that drives diagnostics, hover, completion,
// goto-definition, and document symbols for *.lam files. Settings live
// under the "lammergeier.lsp.*" / "lammergeier.trace.server" prefixes.

import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';
import {
    workspace,
    window,
    commands,
    ExtensionContext,
    OutputChannel,
    ConfigurationChangeEvent,
} from 'vscode';
import {
    LanguageClient,
    LanguageClientOptions,
    ServerOptions,
    StateChangeEvent,
    TransportKind,
    State,
} from 'vscode-languageclient/node';

let client: LanguageClient | undefined;
let output: OutputChannel | undefined;

function isExecutableFile(p: string): boolean {
    try {
        const st = fs.statSync(p);
        if (!st.isFile()) return false;
        // On Windows the executable bit isn't meaningful; .ps1 / .cmd
        // launchers also wouldn't have +x set. Trust the extension instead.
        if (process.platform === 'win32') return true;
        // eslint-disable-next-line no-bitwise
        return (st.mode & 0o111) !== 0;
    } catch {
        return false;
    }
}

function whichOnPath(cmd: string): string | undefined {
    // Walk $PATH looking for an executable named `cmd`. Returns the
    // first hit, or undefined. Mirrors what spawn() does on Linux/macOS
    // but lets us *report* what we searched on failure.
    const pathEnv = process.env.PATH ?? '';
    const sep = process.platform === 'win32' ? ';' : ':';
    for (const dir of pathEnv.split(sep)) {
        if (!dir) continue;
        const candidate = path.join(dir, cmd);
        if (isExecutableFile(candidate)) return candidate;
    }
    return undefined;
}

function findInRepo(cmd: string): string | undefined {
    // Auto-discover `bin/<cmd>` by walking up from every workspace
    // folder (and the active editor's file, if any). Caps the walk at
    // 8 levels so a misconfigured root can't melt the CPU.
    const seeds: string[] = [];
    for (const folder of workspace.workspaceFolders ?? []) {
        seeds.push(folder.uri.fsPath);
    }
    const active = window.activeTextEditor?.document.uri.fsPath;
    if (active) seeds.push(path.dirname(active));

    for (const seed of seeds) {
        let dir = seed;
        for (let i = 0; i < 8; i++) {
            const candidate = path.join(dir, 'bin', cmd);
            if (isExecutableFile(candidate)) return candidate;
            const parent = path.dirname(dir);
            if (parent === dir) break;
            dir = parent;
        }
    }
    return undefined;
}

function findNearExtension(cmd: string, extensionPath: string): string | undefined {
    // The installer symlinks this extension directory into the editor's
    // extensions folder. VS Code launched from a desktop icon often has
    // a minimal PATH, so resolve the symlink and find the checkout-local
    // bin/<cmd> from the extension itself.
    const seeds = [extensionPath];
    try {
        seeds.push(fs.realpathSync(extensionPath));
    } catch {
        // Keep the unresolved extensionPath seed.
    }

    for (const seed of seeds) {
        const candidates = [
            path.join(seed, '..', '..', 'bin', cmd),
            path.join(seed, '..', 'bin', cmd),
            path.join(seed, 'bin', cmd),
        ];
        for (const candidate of candidates) {
            if (isExecutableFile(candidate)) return candidate;
        }
    }
    return undefined;
}

interface ResolvedLauncher {
    command: string;
    /** Where we found it ("absolute", "workspace", "extension", "PATH", or "unresolved"). */
    source: 'absolute' | 'workspace' | 'extension' | 'PATH' | 'unresolved';
}

function resolveServerPath(raw: string, extensionPath: string): ResolvedLauncher {
    // Tilde expansion + ${workspaceFolder} substitution.
    let expanded = raw;
    if (expanded.startsWith('~')) {
        expanded = path.join(os.homedir(), expanded.slice(1));
    }
    const folders = workspace.workspaceFolders;
    if (folders && folders.length > 0) {
        expanded = expanded.replace(/\$\{workspaceFolder\}/g, folders[0].uri.fsPath);
    }

    // If the user gave a path-like value (absolute or contains a
    // separator) we honour it verbatim — they know what they want.
    const looksLikePath =
        path.isAbsolute(expanded) ||
        expanded.includes('/') ||
        (process.platform === 'win32' && expanded.includes('\\'));

    if (looksLikePath) {
        return { command: expanded, source: 'absolute' };
    }

    // Bare name: try the workspace first (developer checkout), then
    // PATH (system install). Only fall back to the bare name if both
    // miss; that gives the OS one last shot before we surface ENOENT.
    const inRepo = findInRepo(expanded);
    if (inRepo) return { command: inRepo, source: 'workspace' };

    const nearExtension = findNearExtension(expanded, extensionPath);
    if (nearExtension) return { command: nearExtension, source: 'extension' };

    const onPath = whichOnPath(expanded);
    if (onPath) return { command: onPath, source: 'PATH' };

    return { command: expanded, source: 'unresolved' };
}

async function startClient(context: ExtensionContext): Promise<void> {
    const cfg = workspace.getConfiguration('lammergeier');
    if (!cfg.get<boolean>('lsp.enabled', true)) {
        output?.appendLine('LSP disabled via lammergeier.lsp.enabled');
        return;
    }

    const rawPath = cfg.get<string>('lsp.path', 'lammergeier-lsp');
    const resolved = resolveServerPath(rawPath, context.extensionPath);
    const command = resolved.command;
    const args = cfg.get<string[]>('lsp.args', []);
    const logFile = cfg.get<string>('lsp.logFile', '');

    output?.appendLine(
        `Resolved "lammergeier.lsp.path" ("${rawPath}") via ${resolved.source} -> ${command}`,
    );

    // Pre-flight: bail out *before* spawn if we can prove the launcher
    // isn't there, so the user gets one focused error instead of three.
    if (resolved.source === 'unresolved') {
        const pathEnv = process.env.PATH ?? '(empty)';
        output?.appendLine(
            `Could not locate "${rawPath}". Searched workspace bin/, extension checkout, and PATH=${pathEnv}`,
        );
        await offerLauncherFix(rawPath);
        return;
    }
    if (path.isAbsolute(command) && !isExecutableFile(command)) {
        output?.appendLine(`Configured launcher "${command}" is missing or not executable.`);
        await offerLauncherFix(command);
        return;
    }

    const env: Record<string, string> = { ...process.env } as Record<string, string>;
    if (logFile) env.LAMMERGEIER_LSP_LOG = logFile;

    const serverOptions: ServerOptions = {
        run: { command, args, transport: TransportKind.stdio, options: { env } },
        debug: { command, args, transport: TransportKind.stdio, options: { env } },
    };

    const clientOptions: LanguageClientOptions = {
        documentSelector: [
            { scheme: 'file', language: 'lammergeier' },
            { scheme: 'untitled', language: 'lammergeier' },
        ],
        synchronize: {
            fileEvents: workspace.createFileSystemWatcher('**/*.lam'),
        },
        initializationOptions: {
            suppressExpectedDiagnostics: cfg.get<boolean>('lsp.suppressExpectedDiagnostics', false),
        },
        outputChannel: output,
    };

    client = new LanguageClient(
        'lammergeier',
        'Lammergeier Language Server',
        serverOptions,
        clientOptions,
    );

    client.onDidChangeState((e: StateChangeEvent) => {
        output?.appendLine(`LSP state: ${State[e.oldState]} -> ${State[e.newState]}`);
    });

    try {
        await client.start();
        output?.appendLine(`LSP started (${command} ${args.join(' ')})`);
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        output?.appendLine(`LSP failed to start: ${msg}`);
        if (/ENOENT/.test(msg)) {
            await offerLauncherFix(command);
        } else {
            window.showErrorMessage(
                `Lammergeier LSP failed to start: ${msg}. ` +
                `Check the Output → Lammergeier panel for details.`,
            );
        }
    }
}

async function offerLauncherFix(attempted: string): Promise<void> {
    // One actionable error: opens the relevant settings entry in one
    // click. Suppresses spammy duplicates by offering the same options
    // every time — vscode debounces identical message boxes.
    const choice = await window.showErrorMessage(
        `Lammergeier: launcher "${attempted}" not found. ` +
        `Set "lammergeier.lsp.path" to the absolute path of "bin/lammergeier-lsp", ` +
        `or add the repo's bin/ directory to your PATH.`,
        'Open Settings',
        'Show Output',
    );
    if (choice === 'Open Settings') {
        await commands.executeCommand('workbench.action.openSettings', 'lammergeier.lsp.path');
    } else if (choice === 'Show Output') {
        output?.show(true);
    }
}

async function stopClient(): Promise<void> {
    if (!client) return;
    try {
        await client.stop();
    } catch (err) {
        output?.appendLine(`LSP stop error: ${err instanceof Error ? err.message : String(err)}`);
    }
    client = undefined;
}

export async function activate(context: ExtensionContext): Promise<void> {
    output = window.createOutputChannel('Lammergeier');
    context.subscriptions.push(output);

    context.subscriptions.push(
        commands.registerCommand('lammergeier.restartServer', async () => {
            output?.appendLine('Restart requested.');
            await stopClient();
            await startClient(context);
        }),
    );

    context.subscriptions.push(
        workspace.onDidChangeConfiguration(async (e: ConfigurationChangeEvent) => {
            if (
                e.affectsConfiguration('lammergeier.lsp.enabled') ||
                e.affectsConfiguration('lammergeier.lsp.path') ||
                e.affectsConfiguration('lammergeier.lsp.args') ||
                e.affectsConfiguration('lammergeier.lsp.logFile') ||
                e.affectsConfiguration('lammergeier.lsp.suppressExpectedDiagnostics')
            ) {
                output?.appendLine('Configuration changed; restarting LSP.');
                await stopClient();
                await startClient(context);
            }
        }),
    );

    await startClient(context);
}

export async function deactivate(): Promise<void> {
    await stopClient();
}
