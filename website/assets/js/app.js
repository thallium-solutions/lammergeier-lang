/* ============================================================
   Lammergeier Lang — site script
   Single-page app: landing + docs viewer with search.
   Uses marked (CDN) for markdown + highlight.js (CDN) for syntax
   highlighting. Client-side routing via location.hash so the site
   ships as pure static files (GitHub Pages / Netlify / any CDN).
   ============================================================ */

// ── Configuration ────────────────────────────────────────────

const DOCS = [
    { id: "syntax",          file: "content/SYNTAX.md",
      title: "Syntax reference",
      summary: "Complete language surface: declarations, types, control flow, pattern matching, go! blocks, LAMMERGEIER.* aliases." },
    { id: "transpilation",   file: "content/TRANSPILATION.md",
      title: "Transpilation rules",
      summary: "How every Lam construct lowers to Go. Authoritative mapping used by the compiler pipeline." },
    { id: "stdlib",          file: "content/stdlib.md",
      title: "Standard library",
      summary: "Full reference for every lam* module — classes, methods, examples, cheatsheet index." },
    { id: "server_plugins",  file: "content/server_plugins.md",
      title: "Server plugin guide",
      summary: "Authoring plugins for lamserver: hook ladder, encapsulation, decorators, testing with inject()." },
    { id: "installation",    file: "content/installation.md",
      title: "Installing Lammergeier",
      summary: "The toolchain installer (install.sh): prerequisites, flags, where lamc and the LSP land, editor extension wiring, upgrading, uninstalling." },
    { id: "package_manager", file: "content/package_manager.md",
      title: "Package manager",
      summary: "lamc install / uninstall / publish end-to-end. Spec syntax, the lockfile, transitive resolution, conflict detection, Go-module pins, the SemVer / API-diff gate." },
    { id: "third_party",     file: "content/third_party_libraries.md",
      title: "Authoring libraries",
      summary: "Library format and registry spec: lamlib.toml, scoped @scope/name imports, on-disk layout, registry protocol, API-diff rules." },
    { id: "readme",          file: "content/README.md",
      title: "Project README",
      summary: "Project intro, features, quick start, running the test suite, editor support." },
    { id: "contributing",    file: "content/CONTRIBUTING.md",
      title: "Contributing",
      summary: "How to add features, stdlib modules, tests, and documentation without drifting from project conventions." },
];

// ── Utilities ────────────────────────────────────────────────

function $(sel, root = document) { return root.querySelector(sel); }
function $$(sel, root = document) { return Array.from(root.querySelectorAll(sel)); }

function slugify(s) {
    return s.toLowerCase()
            .replace(/[^\w\s-]/g, "")
            .trim()
            .replace(/\s+/g, "-")
            .replace(/-+/g, "-");
}

function escapeHtml(s) {
    return s.replace(/[&<>"']/g, c => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;",
        "\"": "&quot;", "'": "&#39;",
    }[c]));
}

function highlightSnippet(text, tokens) {
    // Naive but effective: case-insensitive mark each query token.
    let out = escapeHtml(text);
    for (const t of tokens) {
        if (t.length < 2) continue;
        const re = new RegExp("(" + t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi");
        out = out.replace(re, "<mark>$1</mark>");
    }
    return out;
}

// ── Markdown rendering ───────────────────────────────────────

// Configure marked once the lib is ready.
function configureMarked() {
    if (!window.marked) return;
    window.marked.setOptions({
        gfm: true,
        breaks: false,
        headerIds: true,
        mangle: false,
    });
    // Custom renderer: make heading IDs slug-friendly and attach
    // click-to-anchor handlers via the generated id. The anchor href
    // points back at the currently-displayed doc (``#/docs/<page>``)
    // with a ``?h=<slug>`` query so the router can scroll-restore
    // after a page refresh.
    const renderer = new window.marked.Renderer();
    renderer.heading = (text, level) => {
        const plain = text.replace(/<[^>]+>/g, "");
        const slug = slugify(plain);
        // ``location.hash`` already begins with ``#`` — stripping it
        // prevents the double-hash bug that would otherwise break
        // the link (``##/docs/syntax&h=…``).
        const base = (location.hash || "#/").replace(/^#/, "").split("?")[0];
        return `<h${level} id="${slug}">` +
               `<a href="#${base}?h=${slug}">${text}</a>` +
               `</h${level}>`;
    };
    window.marked.use({ renderer });
}

async function fetchMarkdown(file) {
    const resp = await fetch(file, { cache: "no-cache" });
    if (!resp.ok) {
        throw new Error(`Failed to load ${file}: HTTP ${resp.status}`);
    }
    return await resp.text();
}

function renderMarkdown(md, contextPage) {
    if (!window.marked) return `<pre>${escapeHtml(md)}</pre>`;
    let html = window.marked.parse(md);
    // Rewrite intra-doc anchor hrefs (e.g. ``href="#basic-types"`` from
    // a markdown TOC) into the SPA's ``#/docs/<page>?h=<slug>`` form so
    // clicking *and* right-click-copy-link *and* refresh all work. The
    // ``[^/]`` guard skips hrefs that already point at the SPA router
    // (``#/docs/...``) — those are the links produced by the heading
    // renderer above and by build.sh's cross-doc rewriter.
    if (contextPage) {
        html = html.replace(
            /href="#([^"/?#][^"]*)"/g,
            (_, slug) => `href="#/docs/${contextPage}?h=${slug}"`
        );
    }
    return html;
}

function applyHighlight(root) {
    if (!window.hljs) return;
    $$("pre code", root).forEach(block => {
        try {
            window.hljs.highlightElement(block);
        } catch (e) { /* ignore */ }
    });
}

// ── Router ───────────────────────────────────────────────────

function currentRoute() {
    // Format: #/<view>[/<page>][?h=<heading-slug>]
    const raw = location.hash.replace(/^#/, "") || "/";
    const [pathPart, queryPart = ""] = raw.split("?");
    const parts = pathPart.split("/").filter(Boolean);
    const query = new URLSearchParams(queryPart);
    return {
        view: parts[0] || "home",
        page: parts[1] || null,
        heading: query.get("h") || null,
    };
}

async function render() {
    // Defensive branch: a bare in-page anchor (``#some-slug``, not
    // ``#/view/page``) should scroll the *current* page to that id,
    // not be treated as a view name and bounce the user back home.
    // The renderMarkdown post-processor rewrites our own TOCs into
    // the canonical ``#/docs/<page>?h=<slug>`` form, but this catches
    // any stragglers (external links, old bookmarks, future authors).
    const rawHash = location.hash;
    if (rawHash && !rawHash.startsWith("#/")) {
        const target = document.getElementById(rawHash.slice(1));
        if (target) {
            target.scrollIntoView({ block: "start", behavior: "auto" });
            return;
        }
    }
    const route = currentRoute();
    const app = $("#app");
    app.classList.remove("docs-mode");
    updateActiveNav(route);

    if (route.view === "home") {
        app.innerHTML = renderHome();
        applyHighlight(app);
        return;
    }

    if (route.view === "docs") {
        app.classList.add("docs-mode");
        app.innerHTML = renderDocsShell(route.page);
        const target = route.page
            ? DOCS.find(d => d.id === route.page)
            : DOCS[0];
        if (!target) {
            $("#doc-content").innerHTML =
                `<div class="loading">Unknown document.</div>`;
            return;
        }
        // Separate the fetch step from the render step so a template
        // error can't be mistaken for a network failure (the old
        // ``Cannot set properties of null`` bug: a dangling
        // ``$("#doc-title")`` reference threw *after* a successful
        // fetch and got rethrown as "Unable to load").
        let md;
        try {
            md = await fetchMarkdown(target.file);
        } catch (err) {
            $("#doc-content").innerHTML =
                `<div class="loading">Unable to load <code>${escapeHtml(target.file)}</code>.<br>${escapeHtml(String(err.message))}</div>`;
            return;
        }
        $("#doc-content").innerHTML =
            `<div class="content">${renderMarkdown(md, target.id)}</div>`;
        applyHighlight($("#doc-content"));
        document.title = `${target.title} · Lammergeier Lang`;
        // Scroll to heading if the route asked for one, otherwise
        // reset to the top on navigation.
        if (route.heading) {
            requestAnimationFrame(() => {
                const h = document.getElementById(route.heading);
                if (h) h.scrollIntoView({ block: "start", behavior: "auto" });
            });
        } else {
            window.scrollTo(0, 0);
        }
        return;
    }

    // Unknown route → home.
    location.hash = "#/";
}

function updateActiveNav(route) {
    $$(".site-header nav a[data-view]").forEach(a => {
        a.classList.toggle("active", a.dataset.view === route.view);
    });
}

// ── Views ────────────────────────────────────────────────────

function renderHome() {
    // Open feature rows: concise proof points without card chrome.
    const featureBlocks = [
        ["Lam syntax has its own center",
         "It borrows the pleasant parts: Python-like flow, JavaScript-familiar method names, C-style braces where they make structure obvious, and explicit types where programs need contracts."],
        ["Go is the target, not the costume",
         "<code>lamc</code> emits readable Go and then lets <code>go build</code> do what it is good at: fast builds, native binaries, cross-compilation, and deployment without a language runtime."],
        ["The stdlib is broad by default",
         "<code>lamserver</code>, plugins, JWT, schemas, SQL, migrations, Redis, SMTP, WebSockets, TUS uploads, cron, structured logging, numerics, and dataframes all ship as <code>lam*</code> modules."],
        ["Raw Go stays one block away",
         "<code>go! { ... }</code> lets you call goroutines, channels, and Go packages directly. Use Lam for the application shape and drop into Go where the ecosystem already solved the problem."],
        ["Tooling belongs to the language",
         "The repo includes the compiler, package manager, LSP, diagnostics, completion, hover, goto-definition, document symbols, and editor extension wiring."],
        ["Packages are reproducible",
         "<code>lamc install</code> handles registry, git, and local paths with scoped names, lockfiles, transitive resolution, Go-module pins, conflict detection, and API-diff checks."],
    ];

    // Hero sample: a fully-functional HTTP service with CORS,
    // Helmet-equivalent security headers, structured logging, and
    // graceful shutdown — under thirty lines, no fastify.js overhead.
    const heroSample = `from lamserver        import Server, Request, Response, HttpError
from lamserver_plugins import requestLog, rateLimit
from lamerrors        import Result

func getUser(req: Request, res: Response) {
    id: str = req.params[":id"]
    if id == "" {
        raise HttpError.badRequest("missing :id")
    }
    res.json({"id": id, "name": f"user-{id}"})
}

func main() {
    srv: Server = Server()

    requestLog(srv)                       # built-in plugin
    rateLimit(srv, 100, 60_000)           # 100 req / minute / IP
    srv.useCors()
    srv.useSecurityHeaders({})            # Helmet-class defaults

    srv.get("/users/:id", getUser)
    srv.listen(8080)
}`;

    // Tour cards: each highlights a distinct, idiomatic pattern.
    // Six cards lay out as a 2-column grid (3 rows). The order is
    // chosen so reading top-to-bottom, left-to-right walks the user
    // from data shape → control flow → error handling → escape hatch.
    const tourCards = [
        {
            heading: "Typed functions, named arguments",
            blurb: `Every binding is annotated; every call site can be positional, keyword, or both. Defaults are evaluated at call time, just like Python.`,
            code: `func greet(name: str = "world", times: int = 1) -> str {
    out: str = ""
    for i in range(times) {
        out += f"hello, {name}! "
    }
    return out
}

func main() {
    print(greet())                  # hello, world!
    print(greet(name = "alice"))    # hello, alice!
    print(greet("bob", times = 3))  # hello, bob! …
}`,
        },
        {
            heading: "List, dict & generator comprehensions",
            blurb: `Python-style comprehensions over lists, dicts, and sets — plus lazy generator expressions in <code>(…)</code>. All lower to plain Go loops with no allocator overhead.`,
            code: `func main() {
    squares: list[int] = [x*x for x in range(10)]

    # Filter + multi-clause: cartesian product
    pairs: list[int] = [x*y for x in range(3)
                            for y in range(3)
                            if x != y]

    even_sq: dict[int, int] = {i: i*i for i in range(20)
                                       if i % 2 == 0}

    # Generator expression — lazy, no list allocated
    for n in (x*x for x in range(5)) {
        print(n)                    # 0, 1, 4, 9, 16
    }
}`,
        },
        {
            heading: "Try / catch / raise",
            blurb: `Exceptions for programmer-mistake territory. <code>try / catch</code> mirrors Python; <code>raise</code> and <code>throw</code> are aliases. Compiles to Go panic / recover under the hood.`,
            code: `func validate(n: int) -> int {
    if n < 0 {
        raise ValueError("n must be non-negative")
    }
    return n * 2
}

func main() {
    try {
        v: int = validate(-1)
        print(v)
    } catch e {
        print(f"caught: {e}")       # caught: n must be non-negative
    } finally {
        print("done")
    }
}`,
        },
        {
            heading: "Result + the ? propagation operator",
            blurb: `Recoverable failures use <code>Result</code>. <code>?</code> short-circuits a function on <code>Result.Err</code>; <code>do / catch</code> contains it locally.`,
            code: `from lamerrors import Result, Error

func parsePort(s: str) -> Result {
    n: int = parseInt(s)?           # bubble parse errors up
    if n < 1 or n > 65535 {
        return Result.Err(Error("range", "port out of bounds"))
    }
    return Result.Ok(n)
}

func main() {
    do {
        p: int = parsePort("8080")?
        print(f"listening on {p}")
    } catch err {
        print(f"bad port: {err.message}")
    }
}`,
        },
        {
            heading: "List combinators — map / filter / reduce",
            blurb: `Lists carry the usual combinators straight out of the box: <code>.map</code>, <code>.filter</code>, <code>.reduce</code>, <code>.any</code>, <code>.all</code>, <code>.foreach</code>. Every callback is a <code>lambda</code>; no helper library needed.`,
            code: `func main() {
    nums: list[int] = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

    squares: list[int] = nums.map(lambda x: x * x)
    big:     list[int] = squares.filter(lambda x: x > 10)
    total:   int       = big.reduce(lambda acc, x: acc + x, 0)

    print(big)     # [16 25 36 49 64 81 100]
    print(total)   # 371
}

# Reach for the lamiter stdlib module when you need a lazy pipeline
# (one item at a time, with short-circuiting via .take() / .takeWhile()).`,
        },
        {
            heading: "Drop into raw Go when you have to",
            blurb: `<code>go!</code> blocks let you inline Go for goroutines, channels, or any external package. The transpiler stitches the surrounding scopes together for you.`,
            code: `func crunch(items: list[int]) -> int {
    total: int = 0
    go! {
        var mu sync.Mutex
        var wg sync.WaitGroup
        for _, n := range items {
            wg.Add(1)
            go func(v int) {
                defer wg.Done()
                mu.Lock()
                total += v * v
                mu.Unlock()
            }(n)
        }
        wg.Wait()
    }
    return total
}`,
        },
    ];

    const renderTourCard = (c) => `
        <article class="tour-card">
            <h3>${c.heading}</h3>
            <p>${c.blurb}</p>
            <pre><code class="language-python">${escapeHtml(c.code)}</code></pre>
        </article>
    `;

    // Concrete, decision-friendly cases.
    const whenCards = [
        ["Backend services that need to be small, fast, and self-contained",
         "Use Lammergeier when the service wants Fastify-like ergonomics but should deploy as one binary."],
        ["Glue code that touches multiple Go libraries",
         "Keep the Lam code readable and call the Go package directly from <code>go! { ... }</code>."],
        ["Numerical / data work where you want pandas ergonomics on a real binary",
         "<code>lamdata</code> and <code>lamarray</code> wrap Go data and numeric libraries without shipping a Python process."],
        ["Internal CLIs and operational tools",
         "Typed argument parsing, logging, SQL, migrations, JWT, Redis, and cron in a single deployable executable."],
        ["When *not* to use Lammergeier",
         "Skip it for desktop GUIs, mobile apps, browser front-ends, or Python-only libraries you cannot replace yet."],
        ["Migrating off Node + TypeScript",
         "The <code>lamserver</code> API is intentionally familiar, while the output is a Go binary instead of a Node service."],
    ];

    return `
    <section class="hero">
        <div class="text">
            <h1><span class="accent">Lammergeier</span><br>write fast, Go fast.</h1>
            <br><br><br>
            <div class="hero-points">
                <span>typed Lam syntax</span>
                <span>readable Go output</span>
                <span>single-binary deploys</span>
            </div>
            <div class="cta">
                <a class="primary" href="#/docs/installation">Install and run it</a>
                <a class="ghost"   href="#/docs/syntax">Read the language tour</a>
                <a class="ghost"   href="https://github.com/thallium-solutions/lammergeier-lang" target="_blank" rel="noopener">GitHub →</a>
            </div>
        </div>
        <div class="media">
            <img src="assets/images/logo.png" alt="Lammergeier Lang logo">
        </div>
    </section>

    <section class="band">
        <h2>What is Lammergeier?</h2><br>
        <p class="band-lede">
            Lammergeier is a programming language that aims to be sugary and fast. Who said you couldn't have both?<br>
            The Pythonish/Javaish syntax gets transpiled in Go-lang and then compiled, no VMs, no fuss.
        </p><br>
        <div class="prose-2col">
            <div>
                <p>
                    Lam, for short, mixes:<br>
                    <br>• An indentation-friendly flow
                    <br>• A familiar method-call style
                    <br>• Braces for clear block boundaries
                    <br>• F-strings
                    <br>• Comprehensions
                    <br>• Pattern matching
                    <br>• Classes
                    <br>• Async/await
                    <br>• And explicit type annotations.
                </p>
                <p>
                    The compiler lowers Lam to human-readable Go and
                    then builds it with the Go toolchain. There is no
                    Lam runtime to install beside your app.
                </p>
            </div>
            <div>
                <p>
                    The standard library is part of the core pitch:<br>
                    <br>• HTTP servers
                    <br>• Plugins
                    <br>• Data tools
                    <br>• Persistence
                    <br>• Auth
                    <br>• Queues
                    <br>• Encoding
                    <br>• Time
                    <br>• Networking
                    <br>• Testing
                    <br>• Operational helpers
                    <br>live under <code>lib/</code>.
                </p>
                <p>
                    When you need the Go ecosystem directly, use
                    <code>go! { ... }</code>. Lam code and Go code can
                    live in the same source file.
                </p>
            </div>
        </div>
    </section>

    <section class="band alt">
        <h2>What makes Lam different</h2>
        <div class="feature-list">
            ${featureBlocks.map(([title, body]) => `
                <article class="feature">
                    <h3>${title}</h3>
                    <p>${body}</p>
                </article>`).join("")}
        </div>
    </section>

    <section class="band">
        <h2>A real HTTP service before your coffee cools</h2>
        <div class="hero-sample">
            <p class="muted">
                CORS, security headers, request logging, rate limits,
                routed handlers, and structured <code>HttpError</code>
                helpers come from the stdlib.
            </p>
            <pre><code class="language-python">${escapeHtml(heroSample)}</code></pre>
        </div>
    </section>

    <section class="band alt">
        <h2>Tour the language</h2>
        <p class="band-lede">
            A quick look at typed functions, comprehensions, control
            flow, Result propagation, list combinators, and raw Go
            interop.
        </p>
        <div class="tour">
            ${tourCards.map(renderTourCard).join("")}
        </div>
        <p class="band-cta">
            See <a href="#/docs/syntax">the syntax reference</a> for the
            complete language surface, or
            <a href="#/docs/stdlib">the stdlib reference</a> for every
            module and method.
        </p>
    </section>

    <section class="band">
        <h2>How it's made</h2>
        <p class="band-lede">
            Lam is small enough to inspect and practical enough to ship.
        </p>
        <div class="pipeline">
            <div class="step">
                <span class="num">1</span>
                <h3>Parse</h3>
                <p>
                    A Lark LALR grammar (<code>lammergeier.lark</code>)
                    turns <code>.lam</code> source into an AST. The same
                    grammar drives the LSP.
                </p>
            </div>
            <div class="step">
                <span class="num">2</span>
                <h3>Check</h3>
                <p>
                    Semantic passes catch undefined names, duplicate
                    members, return mismatches, and misplaced flow
                    statements.
                </p>
            </div>
            <div class="step">
                <span class="num">3</span>
                <h3>Transpile</h3>
                <p>
                    Visitors in <code>compiler/visitors/</code> emit Go
                    source one Lam construct at a time. The mapping is
                    documented and snapshot-tested.
                </p>
            </div>
            <div class="step">
                <span class="num">4</span>
                <h3>Build</h3>
                <p>
                    The generated <code>main.go</code> and
                    <code>go.mod</code> go through <code>go build</code>.
                    You get the same artefacts as a Go project.
                </p>
            </div>
        </div>
        <p class="band-cta">
            Want to see the Go for any program?
            <code>lamc your.lam --emit-go</code> prints it and stops.
        </p>
    </section>

    <section class="band alt">
        <h2>When to use Lammergeier</h2>
        <p class="band-lede">
            Pick it when the code should stay expressive but the
            deployment target should look like Go.
        </p>
        <div class="feature-list compact">
            ${whenCards.map(([title, body]) => `
                <article class="feature">
                    <h3>${title}</h3>
                    <p>${body}</p>
                </article>`).join("")}
        </div>
    </section>

    <section class="band">
        <h2>Get started</h2>
        <div class="getstarted">
            <div>
                <h3>Install the toolchain</h3>
<pre><code class="language-bash">git clone https://github.com/thallium-solutions/lammergeier-lang.git
cd lammergeier-lang
./install.sh                            # auto: sys-wide if writable, else ~/.local/bin
./install.sh --with-editor all          # also wire up VS Code / Cursor / Windsurf
lamc --help</code></pre>
                <p class="band-cta">
                    Full installer reference:
                    <a href="#/docs/installation">docs / Installing Lammergeier</a>.
                </p>
            </div>
            <div>
                <h3>Add a third-party library</h3>
<pre><code class="language-bash">lamc install                             # read lamlib.toml, install everything
lamc install lamwebp@1.2.0              # add to ./extlibs + lamlib.toml + lockfile
lamc install ./local-checkout           # local path
lamc install --frozen --offline          # CI / Docker: lockfile is law, no network
lamc uninstall lamwebp</code></pre>
                <p class="band-cta">
                    Full package-manager reference:
                    <a href="#/docs/package_manager">docs / Package manager</a>.
                </p>
            </div>
        </div>
    </section>

    <section class="band alt">
        <h2>Documentation</h2>
        <div class="grid">
            ${DOCS.map(d => `
                <a class="card" href="#/docs/${d.id}" style="border-bottom:none; display:block; color:inherit;">
                    <h3>${d.title}</h3>
                    <p>${d.summary}</p>
                </a>`).join("")}
        </div>
    </section>
    `;
}

function renderDocsShell(activePage) {
    const sections = [
        ["Get started", ["readme", "installation"]],
        ["Language", ["syntax", "transpilation"]],
        ["Standard Library", ["stdlib", "server_plugins"]],
        ["Packages", ["package_manager", "third_party"]],
        ["Project", ["contributing"]],
    ];
    const sidebar = sections.map(([label, ids]) => `
        <h4>${label}</h4>
        <ul>
            ${ids.map(id => {
                const d = DOCS.find(x => x.id === id);
                if (!d) return "";
                const cls = activePage === id ? "active" : "";
                return `<li><a href="#/docs/${id}" class="${cls}">${d.title}</a></li>`;
            }).join("")}
        </ul>
    `).join("");

    return `
    <div class="docs">
        <aside>${sidebar}</aside>
        <main>
            <div id="doc-content">
                <div class="loading">Loading…</div>
            </div>
        </main>
    </div>
    `;
}

// ── Search ───────────────────────────────────────────────────

let searchIndex = null;   // Array of { doc, section, text, url }
let indexLoading = null;  // Promise for lazy single-load

async function buildSearchIndex() {
    if (searchIndex) return searchIndex;
    if (indexLoading) return indexLoading;
    indexLoading = (async () => {
        const entries = [];
        // Load every doc in parallel. Each doc is split into sections
        // keyed on its H2 / H3 headings; the text under each heading
        // becomes one searchable entry.
        const docsToIndex = DOCS;
        const sources = await Promise.all(docsToIndex.map(async d => {
            try {
                return [d, await fetchMarkdown(d.file)];
            } catch (e) {
                return [d, null];
            }
        }));
        for (const [doc, md] of sources) {
            if (!md) continue;
            const sections = splitIntoSections(md);
            for (const s of sections) {
                const url = `#/docs/${doc.id}` +
                    (s.slug ? `?h=${s.slug}` : "");
                entries.push({
                    docTitle: doc.title,
                    sectionTitle: s.title,
                    text: s.body,
                    url,
                });
            }
        }
        searchIndex = entries;
        return entries;
    })();
    return indexLoading;
}

function splitIntoSections(md) {
    // Strip code fences so code doesn't pollute section bodies with
    // implementation noise.
    const cleaned = md.replace(/```[\s\S]*?```/g, "");
    const lines = cleaned.split("\n");
    const sections = [];
    let current = { title: "Introduction", slug: "", body: "" };
    for (const line of lines) {
        const h = line.match(/^(#{1,4})\s+(.*)/);
        if (h) {
            if (current.body.trim()) sections.push(current);
            current = {
                title: h[2].replace(/[`*_]/g, "").trim(),
                slug: slugify(h[2].replace(/[`*_]/g, "")),
                body: "",
            };
        } else {
            current.body += line + " ";
        }
    }
    if (current.body.trim()) sections.push(current);
    // Trim body whitespace.
    return sections.map(s => ({ ...s, body: s.body.replace(/\s+/g, " ").trim() }));
}

function scoreMatch(entry, tokens) {
    let score = 0;
    const title = entry.sectionTitle.toLowerCase();
    const docTitle = entry.docTitle.toLowerCase();
    const body = entry.text.toLowerCase();
    for (const t of tokens) {
        if (!t) continue;
        // Title matches dominate.
        if (title.includes(t))    score += 10;
        if (docTitle.includes(t)) score += 5;
        // Body matches count per occurrence (capped).
        let idx = 0, hits = 0;
        while ((idx = body.indexOf(t, idx)) !== -1 && hits < 8) {
            hits++;
            idx += t.length;
        }
        score += hits;
        // Required-token filter: if any token is absent from
        // title+docTitle+body, demote hard so AND-matching wins.
        if (!title.includes(t) && !docTitle.includes(t) && !body.includes(t)) {
            score -= 20;
        }
    }
    return score;
}

function makeSnippet(text, tokens) {
    const lower = text.toLowerCase();
    let bestIdx = -1, bestTok = tokens[0] || "";
    for (const t of tokens) {
        if (!t) continue;
        const i = lower.indexOf(t);
        if (i !== -1 && (bestIdx === -1 || i < bestIdx)) {
            bestIdx = i;
            bestTok = t;
        }
    }
    const start = Math.max(0, bestIdx - 40);
    const end = Math.min(text.length, (bestIdx === -1 ? 120 : bestIdx) + 120);
    const prefix = start > 0 ? "… " : "";
    const suffix = end < text.length ? " …" : "";
    return prefix + highlightSnippet(text.slice(start, end), tokens) + suffix;
}

function runSearch(query) {
    const el = $("#search-results");
    const tokens = query.toLowerCase().split(/\s+/).filter(t => t.length >= 2);
    if (!tokens.length || !searchIndex) {
        el.classList.remove("open");
        el.innerHTML = "";
        return;
    }
    const ranked = searchIndex
        .map(e => ({ e, s: scoreMatch(e, tokens) }))
        .filter(x => x.s > 0)
        .sort((a, b) => b.s - a.s)
        .slice(0, 20);
    if (!ranked.length) {
        el.innerHTML = `<div class="search-empty">No matches for “${escapeHtml(query)}”.</div>`;
        el.classList.add("open");
        return;
    }
    el.innerHTML = ranked.map(({ e }) => `
        <a class="result" href="${e.url}">
            <span class="title">${escapeHtml(e.sectionTitle)}</span>
            <span class="crumbs">${escapeHtml(e.docTitle)}</span>
            <span class="snippet">${makeSnippet(e.text, tokens)}</span>
        </a>
    `).join("");
    el.classList.add("open");
}

function installSearchHandlers() {
    const input = $("#search-input");
    const results = $("#search-results");
    let debounceId = null;

    input.addEventListener("input", () => {
        clearTimeout(debounceId);
        const q = input.value.trim();
        if (!q) {
            results.classList.remove("open");
            results.innerHTML = "";
            return;
        }
        debounceId = setTimeout(async () => {
            await buildSearchIndex();
            runSearch(q);
        }, 120);
    });
    input.addEventListener("focus", () => {
        if (input.value.trim()) {
            runSearch(input.value.trim());
        }
    });
    // Pre-warm the index on first focus.
    input.addEventListener("focus", () => buildSearchIndex(), { once: true });

    // Dismiss on outside click.
    document.addEventListener("click", (ev) => {
        if (!ev.target.closest(".search-wrap")) {
            results.classList.remove("open");
        }
    });

    // Clear the input once a result is clicked (mirrors user intent).
    results.addEventListener("click", (ev) => {
        const a = ev.target.closest("a.result");
        if (!a) return;
        input.value = "";
        results.classList.remove("open");
    });

    // Keyboard: arrow keys + Enter.
    let highlighted = -1;
    input.addEventListener("keydown", (ev) => {
        const items = $$(".result", results);
        if (!items.length) return;
        if (ev.key === "ArrowDown") {
            ev.preventDefault();
            highlighted = Math.min(items.length - 1, highlighted + 1);
        } else if (ev.key === "ArrowUp") {
            ev.preventDefault();
            highlighted = Math.max(0, highlighted - 1);
        } else if (ev.key === "Enter") {
            if (highlighted >= 0 && highlighted < items.length) {
                ev.preventDefault();
                items[highlighted].click();
                return;
            }
        } else if (ev.key === "Escape") {
            results.classList.remove("open");
            input.blur();
            return;
        } else {
            highlighted = -1;
            return;
        }
        items.forEach((el, i) => el.classList.toggle("highlight", i === highlighted));
        items[highlighted]?.scrollIntoView({ block: "nearest" });
    });
}

// ── Boot ─────────────────────────────────────────────────────

function init() {
    configureMarked();
    installSearchHandlers();
    window.addEventListener("hashchange", render);
    render();
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
} else {
    init();
}
