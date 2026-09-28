---
name: 67ify
description: Convert local image files into 67ify-style animated GIFs by calling the deployed 67ify REST API. Use when the user asks an agent to turn an image, emoji, avatar, sticker, or other image file into a 67 or 55 GIF using the 67ify API, or when integrating with the `/api/convert` endpoint.
---

> Adapted from gorkie's `67ify` skill ([techwithanirudh/gorkie](https://github.com/techwithanirudh/gorkie), AGPL-3.0).
> In coolton: run the `curl` in the sandbox (`run_linux_command`); get a Slack image there first with
> `download_attachments_to_sandbox` or `get_slack_file_tool`, and send the GIF back with `upload_file_from_sandbox`.

# Use 67ify API

## Overview

Use the 67ify REST API to convert an uploaded image into an animated GIF. The
API is unauthenticated and accepts either `mode=67` or `mode=55`.

## Inputs

Require:

- API base URL, such as `https://67ify.vercel.app`.
- Local input image path.
- Local output GIF path.

Optional:

- Mode: `67` or `55`. Default to `67` if the user does not specify one.

## Workflow

1. Use the deployed public instance, `https://67ify.vercel.app`, as the API URL.
2. Resolve the input image path and output GIF path.
3. Call the API directly with `curl` (see below).
4. Confirm the output file exists and is non-empty before reporting success.

## Call the API

```bash
curl --silent --show-error --fail \
  --request POST '<api-base-url>/api/convert' \
  --form 'image=@<input-image>' \
  --form 'mode=<67|55>' \
  --output <output.gif>
```

Example:

```bash
curl --silent --show-error --fail \
  --request POST 'https://67ify.vercel.app/api/convert' \
  --form 'image=@./input.png' \
  --form 'mode=67' \
  --output ./output.gif
```

## API Contract

Multipart fields:

- `image`: uploaded image file.
- `mode`: `67` or `55`.

Successful response:

- Status: `200`
- Content-Type: `image/gif`
- Body: generated GIF bytes

Common errors:

- `400`: missing image field.
- `413`: upload body exceeds 8 MB.
- `415`: unsupported body type.
- `500`: conversion failed.
