---
name: cf-wrangler
description: "Deploy a website or Worker to Cloudflare with `wrangler deploy --temporary` — no Cloudflare account, no login, no OAuth. USE FOR: make a website, build a website, deploy a worker, host this page, put this online. The deployment is live for ~60 minutes and the user claims it (or it expires) via the claim URL wrangler prints. DO NOT USE FOR: the HTML embed tool, which is only for quick inline previews."
---

# Deploy to Cloudflare with `wrangler deploy --temporary`

When the user wants a real website they can visit (not a quick preview), deploy a Cloudflare
Worker with Wrangler's `--temporary` flag. This needs **no Cloudflare account, no login, no OAuth**
— Cloudflare provisions a throwaway account for the deployment, keeps it live ~60 minutes, and
prints a claim URL so the user can claim it as their own before it expires.

Do all of this INSIDE the Linux sandbox (`run_linux_command`). The sandbox already has node + npm.

## Golden rule
Always give the user the **claim URL** alongside the preview URL. Without the claim URL they cannot
keep the site after ~60 minutes.

## Steps

1. **Make sure wrangler is available** (the sandbox persists installs, so this is usually one-time):
   ```bash
   npm i -g wrangler@latest 2>/dev/null; npx wrangler@latest --version
   ```

2. **Scaffold a Worker project** in a fresh directory:
   ```bash
   mkdir -p /home/user/sites/<site-name> && cd /home/user/sites/<site-name>
   npx wrangler@latest init --yes
   ```
   For a plain static site (HTML/CSS/JS), set up the project like this:
   ```bash
   cd /home/user/sites/<site-name>
   mkdir -p public src
   ```
   Write `wrangler.toml`:
   ```toml
   name = "<site-name>"
   main = "src/index.ts"
   compatibility_date = "2026-07-01"

   [assets]
   directory = "./public"
   ```
   Put `index.html` (and CSS/JS) in `public/`. Keep `src/index.ts` minimal (it just handles
   non-static routes; assets are served from `public/`).

3. **Deploy with `--temporary`**:
   ```bash
   cd /home/user/sites/<site-name>
   npx wrangler@latest deploy --temporary
   ```
   Parse the output for two things:
   - the **preview URL** (e.g. `https://<site-name>.<subdomain>.workers.dev`), and
   - the **claim URL** (a `dash.cloudflare.com` link Cloudflare prints so the user can claim the
     temporary account and make the deployment permanent).

4. **Verify it's live**: `curl -sL <preview-url>` and confirm you get the page content.

5. **Iterate** (optional): edit files, then re-run `npx wrangler@latest deploy --temporary` from the
   same directory — it reuses the same temporary account within the 60-minute window, so no
   duplicate claim URLs.

6. **Report to the user**: post both URLs in the thread. Say something like:
   - "Site is live at `<preview-url>`"
   - "Claim it here before it expires in ~60 minutes: `<claim-url>`"
   - If the user never claims it, the deployment is auto-deleted after ~60 minutes.

## Pitfalls
- **`--temporary` is hidden from `--help`.** It's surfaced dynamically: an unauthenticated
  `wrangler deploy` prints "rerun with `--temporary`". Don't conclude the flag is missing just
  because `--help` omits it.
- **Version matters.** The flag landed in Wrangler 4.102.0. Always invoke `npx wrangler@latest` so
  a stale global install can't silently lack it.
- **Credentials break it.** `--temporary` errors out if any Cloudflare auth is present (OAuth,
  `CLOUDFLARE_API_TOKEN`, etc.). The coolton sandbox is unauthenticated, so this shouldn't happen —
  if it does, `wrangler logout` first.
- **Stale curl after redeploy.** `workers.dev` edge-caches briefly; right after a redeploy `curl`
  may show the old body. Re-curl (or add `?v=2`) before concluding a redeploy failed.
- **Rate limiting.** Creating temp accounts too fast fails. Prefer re-deploying (reuses the cached
  temp account) rather than forcing new accounts.

## Rules
- Never run `wrangler login` or try to use a personal Cloudflare token — `--temporary` is the whole
  point and requires no auth.
- Always pass `--temporary`. A plain `wrangler deploy` without it will hang at an interactive login.
- Always surface the claim URL. A deployment without the claim URL cannot be kept.
- Use `wrangler deploy` (Workers) with a `public/` assets dir for static sites; the flow is the same.
- If a deploy fails, report the error verbatim and fix the config — do not fall back to the HTML
  embed tool unless the user explicitly wants a quick inline preview only.
