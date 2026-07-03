#!/usr/bin/env bash
# =============================================================================
# Regenerate the bundled ambient music beds in assets/music/ from scratch.
# =============================================================================
# The beds are SYNTHESIZED with ffmpeg (layered detuned sines + filtered
# noise), so they are self-authored and license-clean for monetized channels.
#
# - assets/music/bible/  : three warm reverent pads (root+5th+octave voicings,
#                          slow swells, chorus shimmer)
# - assets/music/scifi/  : three dark drones (deep detuned roots, noise "air",
#                          a faint slowly-pulsing high tone)
# - assets/music/horror/ : same drones (the mood fits both dark niches)
#
# Mastering: loudnorm I=-14 LUFS / TP=-2 — commercial-track loudness, because
# the mixer's background_music_volume (0.15) is calibrated for that. Upper
# layers (400-800 Hz) exist specifically so the beds survive PHONE SPEAKERS,
# which roll off hard below ~200 Hz.
#
# Usage:  ./tools/generate_music.sh          (run from the repo root)
# =============================================================================
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
mkdir -p assets/music/bible assets/music/scifi assets/music/horror

norm="loudnorm=I=-14:TP=-2"
fade="afade=t=in:d=4,afade=t=out:st=115:d=5"
dfade="afade=t=in:d=5,afade=t=out:st=114:d=6"

echo "Generating warm pads (bible)..."
ffmpeg -y -v error -f lavfi -i "aevalsrc=0.18*sin(2*PI*110*t)*(0.55+0.45*sin(2*PI*0.05*t))+0.18*sin(2*PI*110.4*t)*(0.55+0.45*sin(2*PI*0.045*t+1))+0.14*sin(2*PI*165*t)*(0.5+0.5*sin(2*PI*0.06*t+2))+0.13*sin(2*PI*220.2*t)*(0.5+0.5*sin(2*PI*0.035*t+4))+0.10*sin(2*PI*440*t)*(0.4+0.6*sin(2*PI*0.02*t))+0.05*sin(2*PI*660*t)*(0.35+0.65*sin(2*PI*0.03*t+2)):d=120:s=44100" \
  -af "lowpass=f=2200,chorus=0.6:0.9:55|63:0.35|0.30:0.25|0.35:2|1.8,$fade,$norm" \
  -c:a libmp3lame -b:a 160k assets/music/bible/pad_dawn_A.mp3

ffmpeg -y -v error -f lavfi -i "aevalsrc=0.18*sin(2*PI*98*t)*(0.55+0.45*sin(2*PI*0.04*t))+0.17*sin(2*PI*98.35*t)*(0.55+0.45*sin(2*PI*0.05*t+2))+0.13*sin(2*PI*147*t)*(0.5+0.5*sin(2*PI*0.055*t+1))+0.12*sin(2*PI*196*t)*(0.5+0.5*sin(2*PI*0.03*t+3))+0.09*sin(2*PI*392*t)*(0.4+0.6*sin(2*PI*0.025*t))+0.05*sin(2*PI*588*t)*(0.3+0.7*sin(2*PI*0.02*t+1)):d=120:s=44100" \
  -af "lowpass=f=2000,chorus=0.6:0.9:60|70:0.32|0.28:0.3|0.4:2.2|1.5,$fade,$norm" \
  -c:a libmp3lame -b:a 160k assets/music/bible/pad_still_waters_G.mp3

ffmpeg -y -v error -f lavfi -i "aevalsrc=0.16*sin(2*PI*130.8*t)*(0.55+0.45*sin(2*PI*0.06*t))+0.16*sin(2*PI*131.2*t)*(0.55+0.45*sin(2*PI*0.05*t+1.5))+0.12*sin(2*PI*196.2*t)*(0.5+0.5*sin(2*PI*0.04*t+2.5))+0.12*sin(2*PI*261.6*t)*(0.45+0.55*sin(2*PI*0.03*t+4))+0.09*sin(2*PI*523.2*t)*(0.35+0.65*sin(2*PI*0.018*t+1))+0.04*sin(2*PI*784*t)*(0.3+0.7*sin(2*PI*0.022*t)):d=120:s=44100" \
  -af "lowpass=f=2400,chorus=0.55:0.9:50|58:0.35|0.30:0.28|0.38:1.8|1.4,$fade,$norm" \
  -c:a libmp3lame -b:a 160k assets/music/bible/pad_morning_light_C.mp3

echo "Generating dark drones (scifi + horror)..."
ffmpeg -y -v error -f lavfi -i "aevalsrc=0.26*sin(2*PI*41.2*t)*(0.6+0.4*sin(2*PI*0.03*t))+0.24*sin(2*PI*41.7*t)*(0.6+0.4*sin(2*PI*0.026*t+2))+0.16*sin(2*PI*82.4*t)*(0.5+0.5*sin(2*PI*0.05*t+1))+0.10*sin(2*PI*164.8*t)*(0.4+0.6*sin(2*PI*0.04*t+3))+0.08*sin(2*PI*617*t)*(0.2+0.8*pow(sin(2*PI*0.011*t)\,8)):d=120:s=44100" \
  -f lavfi -i "anoisesrc=color=brown:d=120:a=0.30:s=44100" \
  -filter_complex "[1:a]lowpass=f=500,volume=0.6[air];[0:a][air]amix=inputs=2:duration=first,$dfade,$norm" \
  -c:a libmp3lame -b:a 160k assets/music/scifi/drone_hollow_E.mp3

ffmpeg -y -v error -f lavfi -i "aevalsrc=0.26*sin(2*PI*36.7*t)*(0.6+0.4*sin(2*PI*0.022*t))+0.23*sin(2*PI*37.1*t)*(0.6+0.4*sin(2*PI*0.03*t+3))+0.15*sin(2*PI*73.4*t)*(0.5+0.5*sin(2*PI*0.04*t+1))+0.09*sin(2*PI*146.8*t)*(0.4+0.6*sin(2*PI*0.05*t+2))+0.07*sin(2*PI*880*t)*(0.12+0.88*pow(sin(2*PI*0.007*t+1)\,12)):d=120:s=44100" \
  -f lavfi -i "anoisesrc=color=brown:d=120:a=0.28:s=44100" \
  -filter_complex "[1:a]lowpass=f=450,volume=0.6[air];[0:a][air]amix=inputs=2:duration=first,$dfade,$norm" \
  -c:a libmp3lame -b:a 160k assets/music/scifi/drone_signal_D.mp3

ffmpeg -y -v error -f lavfi -i "aevalsrc=0.24*sin(2*PI*49*t)*(0.6+0.4*sin(2*PI*0.035*t))+0.22*sin(2*PI*49.5*t)*(0.6+0.4*sin(2*PI*0.028*t+2))+0.14*sin(2*PI*98*t)*(0.45+0.55*sin(2*PI*0.06*t))+0.09*sin(2*PI*196*t)*(0.35+0.65*sin(2*PI*0.045*t+1))+0.07*sin(2*PI*733*t)*(0.15+0.85*pow(sin(2*PI*0.009*t+2)\,10)):d=120:s=44100" \
  -f lavfi -i "anoisesrc=color=pink:d=120:a=0.20:s=44100" \
  -filter_complex "[1:a]lowpass=f=600,volume=0.5[air];[0:a][air]amix=inputs=2:duration=first,$dfade,$norm" \
  -c:a libmp3lame -b:a 160k assets/music/scifi/drone_static_G.mp3

cp assets/music/scifi/drone_*.mp3 assets/music/horror/
echo "Done. Beds written to assets/music/{bible,scifi,horror}/"
