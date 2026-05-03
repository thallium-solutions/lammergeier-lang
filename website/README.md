# Lammergeier Lang — website

A single-page static site that ships the language's documentation with
client-side search and lammergeier-themed visuals. No build tool, no
dependencies at runtime beyond two CDN-hosted libraries (`marked` for
Markdown and `highlight.js` for code highlighting).

## Layout

```
website/
├── index.html                 # single-page app shell
├── build.sh                   # copies docs/ → content/ (idempotent)
├── README.md                  # this file
├── assets/
│   ├── css/main.css           # theme + layout
│   ├── js/app.js              # hash router, renderer, search
│   └── images/                # logo + mascotte (copied from ../images)
└── content/                   # populated by build.sh — authoritative
    ├── SYNTAX.md              #   source-of-truth lives in docs/ and
    ├── TRANSPILATION.md       #   is copied here so the site can be
    ├── stdlib.md              #   deployed standalone.
    ├── server_plugins.md
    ├── third_party_libraries.md
    ├── README.md
    └── CONTRIBUTING.md
```

## Rebuild after editing docs

```bash
./website/build.sh
```

The script copies every Markdown file it needs from `docs/` and the
project root into `website/content/`, then rewrites cross-doc links
(`SYNTAX.md` → `#/docs/syntax` inside the SPA, repo-internal paths like
`../lib/foo.lam` → absolute GitHub URLs).

## Local preview

```bash
python3 -m http.server --directory website 8765
```

Open <http://localhost:8765/>. The hash-based router means deep links
like `#/docs/stdlib?h=lamdata` work out of the box — no server-side
routing required.

## Deployment

The contents of `website/` are a self-contained static site. Any host
that serves static files is enough:

- **GitHub Pages** — point Pages at `/website` on the `main` branch.
- **Netlify / Vercel / Cloudflare Pages** — set the publish directory
  to `website/`.
- **Nginx / Apache / any CDN** — upload `website/`, no further config
  needed. MIME types for `.html`, `.css`, `.js`, `.md` are fine out of
  the box on every sensible default.

Remember to re-run `./website/build.sh` before deploying if the source
docs changed — the site reads the copies under `content/`, not the
originals in `docs/`.

## Search

The search index is built client-side on first focus of the search
box. Each doc is split on its H2/H3/H4 headings so results link
straight to the relevant section via the hash router's `?h=<slug>`
parameter. Nothing is pre-compiled; if the docs grow substantially
this can be swapped for a pre-built JSON index without changing the
HTML.

## Theme

Colour palette tracks the project logo: slate-indigo surfaces,
bone-ivory text, ochre/amber accents. Typography uses the system sans
stack for body text and a Palatino-style serif for headings. The CSS
stays in a single file (`assets/css/main.css`) so it's easy to read
and fork.
