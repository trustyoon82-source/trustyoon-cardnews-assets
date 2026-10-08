# trustyoon-cardnews-assets

Instagram card-news assets and the cloud publisher for @trustyoon_official.

- `product-images/` — product photos used by the card-news templates.
- `queue/<NNNN>-<slug>/` — rendered 4:5 carousels waiting to be posted (`01.jpg`–`06.jpg`, `item.json` with caption and alt text). Pushed by the PC after the weekly batch (`sync_cardnews_cloud.py --push`).
- `receipts/` — what was posted, written by the publisher and pulled back by the PC (`sync_cardnews_cloud.py --pull`).
- `state.json` — the last post, used to keep it to one post per KST day.
- `.github/workflows/publish.yml` — posts the next queued carousel at 18:00 KST through the Instagram API (Instagram Login). Needs the `IG_ACCESS_TOKEN` secret.
- `.github/workflows/refresh-token.yml` — refreshes the 60-day token weekly. Needs the `SECRETS_PAT` secret (fine-grained, this repo, Secrets: read and write).

Images are served to Instagram from `raw.githubusercontent.com`, which is why this repository is public.
