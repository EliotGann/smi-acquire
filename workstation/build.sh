#!/usr/bin/env bash
# Build against the same React installation as the SMI Finch checkout.
set -eu
cd "$(dirname "$0")"
finch=$(cd "${FINCH_FRONTEND:-../../smi-finch-vibes/frontend}" && pwd)
NODE_PATH="$finch/node_modules" "$finch/node_modules/.bin/esbuild" app.jsx \
    --bundle --minify --define:process.env.NODE_ENV='"production"' \
    --alias:finch-header="$finch/dist/components/FinchAppLayout/FinchHeader.js" \
    --outfile=dist/app.js
cp index.html style.css dist/
