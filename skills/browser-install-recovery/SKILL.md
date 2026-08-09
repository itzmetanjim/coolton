---
name: browser-install-recovery
description: 'Recovers agent-browser when Chromium fails to launch because required Linux libraries are missing. USE FOR: browser install/startup errors such as libglib, libnspr, missing shared libraries, or Chrome exited early. DO NOT USE FOR: website interaction after the browser launches.'
---

# Browser Install Recovery

# browser install recovery

## when to use
Use this when `agent-browser open` fails before a page loads with errors like `Chrome exited early`, `DevToolsActivePort`, `libglib-2.0.so.0`, `libnspr4.so`, or another missing shared library.

## workflow
1. Read the exact stderr; preserve the error in the final report if recovery fails.
2. Run the supported dependency installer first:
   ```bash
   agent-browser install --with-deps
   ```
3. Retry the original `agent-browser open` command, keeping any `--allowed-domains` restriction.
4. If it still fails, install the specific missing runtime package with apt in noninteractive mode, then retry:
   ```bash
   sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq
   sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq <package>
   ```
   Common mappings include `libglib-2.0.so.0` → `libglib2.0-0` and `libnspr4.so` → `libnspr4`.
5. After the browser launches, use the normal snapshot/read workflow. Do not treat a successful browser launch as proof that the target website or API worked.
6. Close the browser session when finished:
   ```bash
   agent-browser close
   ```

## safety
- Never bypass domain restrictions or enter credentials unless the user requested it.
- Do not silently fall back to an unrelated browser or web fetch if agent-browser fails; report the exact error.
- Avoid logging secrets or putting credentials in shell history.

## acceptance examples
- For a missing `libglib-2.0.so.0`, run `agent-browser install --with-deps`, retry, then install `libglib2.0-0` only if needed.
- For a missing `libnspr4.so`, run the dependency installer or install `libnspr4`, then retry.
- For a successful launch, continue with page inspection rather than claiming the requested website task is complete.
