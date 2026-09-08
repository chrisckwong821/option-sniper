# Public dashboard stream failure after production rollout

## Impact

The public browser displayed `Stream error`, blank IV expansion/bucket sigma,
and a September 4 fallback ranking instead of current live movers. The backend
was receiving stock/options data and computing IV observations; the browser
was not applying them. Staging did not have the same cached helper response.

## Root cause

`app.js` called `SigmaMath.isReopeningOvernight`, but the production CDN cached
an older `live-calculations.js` without that function. Its manually assigned
query version had not changed when the helper changed. The public response
was a Cloudflare cache HIT with a four-hour max-age and September 6 content.
A fresh query URL returned the correct helper. The message-handler catch
converted the resulting exception into `Stream error`, before IV updates ran.

## Why release verification missed it

CI tested matching local files. Deployment checked origin readiness and assets,
not the exact CDN-served dependency set executing in a browser. Public HTTP 200
was insufficient to verify live rendering. Backend health therefore passed
while the public UI failed. The initial rollout also encountered a separate
live-options credential mismatch; that was corrected before the successful
retry and is not the cause of this browser failure.

## Correction and prevention

All dashboard JavaScript and CSS references now use their SHA-256 content hash
(first 16 hex characters) as the query version. A CI test checks every script
and stylesheet reference against its file, failing if a changed asset keeps
an old URL. Existing cached URLs need not be purged to serve the new release.
Release verification must inspect the public live-movers view in a browser,
including fresh prices and populated IV expansion/bucket scores, rather than
relying on backend health alone. Automated public-browser smoke testing remains
a follow-up; this patch does not claim to automate that check.

## Data subscriptions

Both live stock ranking and live IV calculation use the backend's stock-price
feed (EQUS.MINI). Option quotes use OPRA.PILLAR separately. Production and
staging have independent connections; identical upstream credentials do not
guarantee identical quote arrival times. Historical credentials remain separate.
No market-data redownload or subscription change is needed for this fix.
