#!/usr/bin/env bash
# Render media/voiceover/segments/*.txt to MP3 via the ElevenLabs TTS API.
#
#   ELEVENLABS_API_KEY=sk_... ./generate.sh            # all segments -> out/
#   ELEVENLABS_API_KEY=sk_... ./generate.sh 04 06      # only segments 04 and 06
#   ELEVENLABS_API_KEY=sk_... ./generate.sh --list-voices
#
# VOICE_ID and MODEL_ID are overridable; defaults are Adam (deep narrator)
# on the multilingual v2 model, ElevenLabs' highest-quality tier.
set -euo pipefail
cd "$(dirname "$0")"

: "${ELEVENLABS_API_KEY:?set ELEVENLABS_API_KEY (elevenlabs.io -> My Account -> API Keys)}"
VOICE_ID="${VOICE_ID:-pNInz6obpgDQGcFmaJgB}"
MODEL_ID="${MODEL_ID:-eleven_multilingual_v2}"

if [[ "${1:-}" == "--list-voices" ]]; then
  curl -sS --fail-with-body https://api.elevenlabs.io/v1/voices \
    -H "xi-api-key: ${ELEVENLABS_API_KEY}" \
    | jq -r '.voices[] | "\(.voice_id)  \(.name)"'
  exit 0
fi

# No args renders every segment; numeric args select by two-digit prefix.
files=()
if [[ $# -eq 0 ]]; then
  files=(segments/*.txt)
else
  for n in "$@"; do files+=(segments/"$n"_*.txt); done
fi

mkdir -p out
for f in "${files[@]}"; do
  name="$(basename "$f" .txt)"
  echo "-> ${name}"
  jq -Rs --arg model "$MODEL_ID" \
    '{text: ., model_id: $model,
      voice_settings: {stability: 0.5, similarity_boost: 0.75, style: 0.35}}' \
    < "$f" \
  | curl -sS --fail-with-body -X POST \
      "https://api.elevenlabs.io/v1/text-to-speech/${VOICE_ID}?output_format=mp3_44100_128" \
      -H "xi-api-key: ${ELEVENLABS_API_KEY}" \
      -H "content-type: application/json" \
      --data @- -o "out/${name}.mp3"
done
echo "done — MP3s in $(pwd)/out/"
