#!/usr/bin/env bash
# The whole Riggle API in three curl calls: upload, poll, download.
#
#   export RIGGLE_API_KEY=rgl_...        # https://app.riggle3d.com/developers/
#   ./curl.sh character.glb              # GLB or VRM to VRM, 10 API tokens
#   ./curl.sh --image hero.png           # picture to VRM, 25 API tokens
#   ./curl.sh --image hero.png --fix     # ...redrawn first if the check finds a problem (+2)
#   ./curl.sh --image hero.png --force   # ...skipping the picture check
#
# A picture is checked before anything is charged. One that is not a whole,
# front-facing humanoid is refused with 422 picture_rejected and a list of
# problems; send another, or the same one with --fix or --force.
#
# Needs curl and jq. Reference: https://riggle3d.com/en/guides/vrm-api
set -euo pipefail

API=${RIGGLE_API_URL:-https://app.riggle3d.com/api/v1}
: "${RIGGLE_API_KEY:?Set RIGGLE_API_KEY (keys: https://app.riggle3d.com/developers/)}"
auth="Authorization: Bearer $RIGGLE_API_KEY"

# A model goes in the `file` field, a picture in the `image` field.
field=file
if [ "${1:-}" = "--image" ]; then field=image; shift; fi
input=${1:?usage: curl.sh character.glb | curl.sh --image hero.png [--fix|--force]}
extra=()
case "${2:-}" in
  --fix) extra=(-F fix=true) ;;      # pictures only
  --force) extra=(-F force=true) ;;  # pictures only
esac
out="${input%.*}.vrm"

# 1. Start the conversion. Tokens are taken here, and returned if it fails.
#    The Idempotency-Key makes a retry return this conversion, not a second one.
created=$(curl -s -X POST "$API/conversions" \
  -H "$auth" \
  -H "Idempotency-Key: $(uuidgen 2>/dev/null || date +%s%N)" \
  ${extra[@]+"${extra[@]}"} \
  -F "$field=@$input")
id=$(echo "$created" | jq -r '.id // empty')
if [ -z "$id" ]; then
  # Every error is {"error": {"code": "...", "message": "..."}}
  # picture_rejected also carries .error.problems: [{code, message}, ...]
  echo "$created" | jq -r '"Error: \(.error.code): \(.error.message)", (.error.problems[]? | "  - \(.message)")' >&2
  exit 1
fi
echo "Conversion $id started" >&2

# 2. Poll every 4 seconds until it is no longer processing.
#    A rigged model takes 1-3 minutes, a picture 3-6.
while :; do
  conversion=$(curl -s "$API/conversions/$id" -H "$auth")
  status=$(echo "$conversion" | jq -r .status)
  [ "$status" != processing ] && break
  echo "  $(echo "$conversion" | jq -r .stage)" >&2
  sleep 4
done

if [ "$status" != succeeded ]; then
  echo "$conversion" | jq -r '"Failed: \(.error.message // .error // .status) (tokens returned: \(.refunded))"' >&2
  exit 1
fi

# Warnings mean the avatar was built, but the preview deserves a look.
echo "$conversion" | jq -r '.warnings[]? | "Warning: \(.)"' >&2

# 3. Download the VRM (and, if you like, the video of every expression).
curl -sf -o "$out" "$API/conversions/$id/vrm" -H "$auth"
echo "Saved $out" >&2
# curl -sf -o "${input%.*}.mp4" "$API/conversions/$id/preview.mp4" -H "$auth"

# The redraw on its own (2 API tokens) is a job of the same shape: it gives
# back a full-body front view on a plain background, with no check first.
# Tokens are returned if no picture comes back (status failed, refunded true).
#
#   fix=$(curl -s -X POST "$API/pictures/fix" -H "$auth" \
#     -H "Idempotency-Key: $(uuidgen)" -F image=@sheet.png | jq -r .id)
#   until [ "$(curl -s "$API/pictures/fix/$fix" -H "$auth" | jq -r .status)" != processing ]
#   do sleep 4; done
#   curl -sf -o fixed.png "$API/pictures/fix/$fix/image" -H "$auth"
