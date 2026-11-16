#!/usr/bin/env bash
# What does the TV remote send over HDMI-CEC? (COUCH-9)
#
# Run on the Pi, with the TV on and showing Couch, and the TV remote in your hand:
#
#     sudo ./scripts/cec-button-survey.sh            # every button
#     sudo ./scripts/cec-button-survey.sh --volume   # only the volume buttons (sound on the jack)
#
# It watches the CEC bus (cec-ctl --monitor-all, or --monitor where the adapter can't see
# everything) and asks for one button at a time. Press the button on the TV remote; the
# script waits for the first frame from the TV, collects everything until the bus has been
# quiet for a moment, and moves on. Some buttons are asked for twice: once as a quick press
# and once held for about 3 seconds, to see whether the TV repeats a held button and whether
# it sends a release. A button the TV doesn't forward times out after 12 seconds and is
# written down as "nothing". Press Enter at a prompt to skip that button.
#
# couchd keeps running: monitoring only listens. couchd's own log lines go to journal.log
# (with log.level = "debug" in /etc/couch/config.toml, each button also logs a
# "remote button" line there).
#
# Results go to ./cec-survey-<date>/:
#   summary.md   one row per button: what arrived, release, repeat gap, time to release
#   monitor.log  every line cec-ctl printed, each with a millisecond timestamp
#   journal.log  couchd's log during the survey
# Paste summary.md into docs/tv-sony-kdl40v55.md.

set -euo pipefail

DEVICE=/dev/cec0
WAIT_FIRST=12  # seconds to wait for the first frame after asking
QUIET_TAP=1.5  # a press is over after this long without a frame...
QUIET_HOLD=2.0 # ...a hold after this long
API=http://127.0.0.1:8765

usage() {
	sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
	exit "${1:-0}"
}

ONLY_VOLUME=false
SUMMARISE=
while (($#)); do
	case $1 in
	--volume) ONLY_VOLUME=true ;;
	--summarise) # for tests: one summary row for a monitor.log, then exit
		SUMMARISE=$2
		shift
		;;
	--device)
		DEVICE=$2
		shift
		;;
	-h | --help) usage ;;
	*) usage 2 ;;
	esac
	shift
done

# One summary row for what arrived after line $1.
summarise() {
	tail -n +"$(($1 + 1))" "${2:-$MON}" | awk '
		function addrs(line) {  # "0 to 4" (mawk has no capture groups)
			if (match(line, /\([0-9a-f]+ to [0-9a-f]+\)/)) return substr(line, RSTART + 1, RLENGTH - 2)
			return "?"
		}
		function flush() {
			if (head == "") return
			if (head ~ /USER_CONTROL_PRESSED/) {
				presses++
				if (first_press == "") first_press = hts
				if (last_press != "") { gap = hts - last_press; gaps = gaps (gaps == "" ? "" : ",") gap }
				last_press = hts
				codes[ui]++
				if (index(to, addrs(head)) == 0) to = to (to == "" ? "" : ", ") addrs(head)
			} else if (head ~ /USER_CONTROL_RELEASED/) {
				releases++
				if (first_release == "" && first_press != "") first_release = hts
			} else if (head ~ /Received from TV/ && head !~ /REPORT_POWER_STATUS|CEC_VERSION|REPORT_PHYSICAL_ADDR|DEVICE_VENDOR_ID|SET_OSD_NAME/) {
				op = head; sub(/.*\): /, "", op); sub(/ \(0x.*/, "", op); sub(/:$/, "", op)
				others[op " (" addrs(head) ")" (detail == "" ? "" : " " detail)]++
			}
			head = ""; ui = ""; detail = ""
		}
		{
			ts = $1; line = substr($0, length($1) + 2)
			if (line ~ /^\t/) {
				sub(/^\t+/, "", line)
				if (line ~ /^ui-cmd:/) { ui = line; sub(/^ui-cmd: /, "", ui) }
				else if (line !~ /^(Raw|Sequence|Tx|Rx|Timestamp)/) detail = detail (detail == "" ? "" : ", ") line
				next
			}
			flush()
			if (line ~ /^(Received|Transmitted)/) { head = line; hts = ts }
		}
		END {
			flush()
			pressed = ""
			for (c in codes) pressed = pressed (pressed == "" ? "" : "; ") c (codes[c] > 1 ? " x" codes[c] : "")
			other = ""
			for (o in others) other = other (other == "" ? "" : "; ") o (others[o] > 1 ? " x" others[o] : "")
			if (pressed == "" && other == "") pressed = "nothing"
			rel = releases + 0
			held = (first_press != "" && first_release != "") ? (first_release - first_press) " ms" : "-"
			if (gaps == "") gaps = "-"
			printf "%s | %s | %s | %s | %s | %s", pressed, to, rel, gaps, held, other
		}'
}

if [[ -n $SUMMARISE ]]; then
	summarise 0 "$SUMMARISE"
	echo
	exit 0
fi

if ((EUID != 0)); then
	echo "Monitoring CEC needs root; rerunning with sudo."
	exec sudo "$0" "$@"
fi
command -v cec-ctl >/dev/null || {
	echo "cec-ctl is missing: sudo apt install v4l-utils" >&2
	exit 1
}

OUT="$PWD/cec-survey-$(date +%Y%m%d-%H%M)"
mkdir -p "$OUT"
MON="$OUT/monitor.log"
SUMMARY="$OUT/summary.md"
: >"$MON"

now_ms() { date +%s%3N; }

# --- the monitor --------------------------------------------------------------------------

stamp() {
	local line
	while IFS= read -r line; do
		printf '%s %s\n' "$(now_ms)" "$line"
	done
}

PIDS=()
cleanup() {
	for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
	wait 2>/dev/null || true
	if [[ -n ${SUDO_USER:-} ]]; then chown -R "$SUDO_USER": "$OUT" 2>/dev/null || true; fi
}
trap cleanup EXIT

start_monitor() {
	local mode=$1
	(stdbuf -oL cec-ctl -d "$DEVICE" "$mode" --show-raw 2>&1 | stamp >>"$MON") &
	PIDS+=($!)
	sleep 1.5
	if grep -qiE "not supported|failed|error" "$MON"; then
		kill "${PIDS[-1]}" 2>/dev/null || true
		unset 'PIDS[-1]'
		return 1
	fi
}

MONITOR_MODE=--monitor-all
if ! start_monitor --monitor-all; then
	echo "This adapter can't monitor all traffic; watching what's addressed to Couch instead."
	: >"$MON"
	MONITOR_MODE=--monitor
	start_monitor --monitor || {
		cat "$MON" >&2
		exit 1
	}
fi
if command -v journalctl >/dev/null; then
	(journalctl -u couchd -f -n 0 -o short-precise >"$OUT/journal.log" 2>&1) &
	PIDS+=($!)
fi

# --- reading what arrived -----------------------------------------------------------------

lines() { wc -l <"$MON"; }

# Frame headlines from the TV after line $1 (ignoring couchd's power polls and their replies).
tv_frames_after() {
	tail -n +"$(($1 + 1))" "$MON" | grep -E '^[0-9]+ Received from TV' |
		grep -vE 'REPORT_POWER_STATUS|CEC_VERSION|REPORT_PHYSICAL_ADDR|DEVICE_VENDOR_ID|SET_OSD_NAME' || true
}

# Wait for the TV to send something, then until it's been quiet for $2 seconds.
# Prints "nothing" or "ok"; returns early on Enter (prints "skipped").
collect() {
	local start=$1 quiet=$2 deadline last_count count last_change
	deadline=$(($(now_ms) + WAIT_FIRST * 1000))
	while [[ -z $(tv_frames_after "$start") ]]; do
		if read -r -t 0.1 _; then
			echo skipped
			return
		fi
		if (($(now_ms) > deadline)); then
			echo nothing
			return
		fi
	done
	last_count=$(lines)
	last_change=$(now_ms)
	local quiet_ms
	quiet_ms=$(awk -v q="$quiet" 'BEGIN { print int(q * 1000) }')
	while (($(now_ms) - last_change < quiet_ms)); do
		sleep 0.1
		count=$(lines)
		if ((count != last_count)); then
			last_count=$count
			last_change=$(now_ms)
		fi
	done
	echo ok
}

row() {
	printf '| %s | %s | %s |\n' "$1" "$2" "$3" >>"$SUMMARY"
}

ask() { # name, "tap"|"hold"|"wait", hint, quiet seconds
	local name=$1 how=$2 hint=${3:-} start result quiet=$QUIET_TAP verb
	if [[ $how == hold ]]; then
		verb="HOLD  $name  for about 3 seconds, then let go"
		quiet=$QUIET_HOLD
	elif [[ $how == wait ]]; then
		verb=$name
	else
		verb="Press  $name  once"
	fi
	printf '\n\033[1m%s\033[0m%s\n' "$verb" "${hint:+   ($hint)}"
	printf '  (Enter skips it)\n'
	quiet=${4:-$quiet}
	start=$(lines)
	result=$(collect "$start" "$quiet")
	case $result in
	skipped)
		echo "  skipped"
		row "$name" "$how" "skipped | | | | |"
		;;
	nothing)
		echo "  nothing arrived in ${WAIT_FIRST}s: the TV doesn't forward it"
		row "$name" "$how" "nothing | | | | |"
		;;
	*)
		local summary
		summary=$(summarise "$start")
		echo "  got: ${summary%% |*}"
		row "$name" "$how" "$summary"
		;;
	esac
}


# --- the survey ---------------------------------------------------------------------------

{
	echo "# CEC button survey, $(date '+%Y-%m-%d %H:%M')"
	echo
	echo "cec-ctl $MONITOR_MODE on $DEVICE. Times are measured on the Pi, from the first"
	echo "frame; \"repeat gaps\" are the ms between repeated User Control Pressed frames."
	echo
	echo '| Button | How | Arrived (ui-cmd) | To | Releases | Repeat gaps (ms) | Press → release | Other frames |'
	echo '| --- | --- | --- | --- | --- | --- | --- | --- |'
} >"$SUMMARY"

cat <<EOF

CEC button survey. Point the TV remote at the TV, which should be on and showing Couch.
Results: $OUT

Pressing a button can make Couch do things (open an app, go Home). That's fine; if you
end up somewhere else, carry on: the survey only listens.
EOF

volume_section() {
	local state previous
	echo
	echo "Volume buttons. The TV only sends them to Couch while Couch is its Audio System,"
	echo "which it is while sound is on the headphone jack or Bluetooth (COUCH-11)."
	previous=$(curl -s "$API/api/state" | python3 -c 'import json,sys; print(json.load(sys.stdin)["audio"]["current"] or "")' 2>/dev/null || true)
	if [[ -n $previous && $previous != tv:* && $previous != tv ]]; then
		echo "Sound is on $previous already."
	elif curl -s -XPOST "$API/api/command/audio.set_output" -d '{"output": "jack"}' | grep -q '"ok": *true'; then
		echo "Moved sound to the headphone jack for this part (it goes back to $previous after)."
	else
		echo "Couldn't move sound to the jack through couchd; pick it yourself (Red button)."
	fi
	sleep 3
	state=$(curl -s "$API/api/state" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tv"].get("system_audio"))' 2>/dev/null || echo "?")
	echo "couchd says System Audio Mode is: $state"
	row "(tv.system_audio = $state)" "" " | | | | |"
	ask "VOLUME +" tap "the TV's own volume bar should not move; its speakers are muted"
	ask "VOLUME +" hold
	ask "VOLUME −" tap
	ask "VOLUME −" hold
	ask "MUTE" tap
	ask "MUTE" tap "again, to unmute"
	if [[ -n $previous && $previous != "$(curl -s "$API/api/state" | python3 -c 'import json,sys; print(json.load(sys.stdin)["audio"]["current"])' 2>/dev/null)" ]]; then
		curl -s -XPOST "$API/api/command/audio.set_output" -d "{\"output\": \"$previous\"}" >/dev/null || true
		echo "Sound is back on $previous."
	fi
}

if $ONLY_VOLUME; then
	volume_section
else
	for b in UP DOWN LEFT RIGHT; do ask "$b" tap; done
	ask "OK (the middle button)" tap
	ask "RETURN" tap
	ask "HOME" tap "Sony's own menu may open; press HOME again to close it after"
	ask "OPTIONS" tap
	ask "SYNC MENU" tap "Sony's Bravia Sync menu may open; close it after"
	ask "GUIDE" tap
	ask "i+ / INFO / DISPLAY" tap
	for b in RED GREEN YELLOW BLUE; do ask "$b" tap; done
	ask "PLAY" tap
	ask "PAUSE" tap
	ask "STOP" tap
	ask "REWIND <<" tap
	ask "FAST FORWARD >>" tap
	ask "PREVIOUS |<<" tap
	ask "NEXT >>|" tap
	for n in 1 2 3 4 5 6 7 8 9 0; do ask "digit $n" tap; done
	ask "PROG + (channel up)" tap
	ask "PROG − (channel down)" tap
	ask "TEXT / teletext" tap "if it opens teletext, press it again"
	ask "SUBTITLE" tap
	ask "AUDIO (A/B)" tap
	ask "THEATRE" tap
	ask "WIDE" tap "the picture shape may change; press WIDE until it's back"
	ask "VOLUME + (sound on the TV)" tap "the TV handles this itself; expect nothing"

	echo
	echo "Now the same buttons held down, to see repeats and releases."
	ask "DOWN" hold
	ask "RIGHT" hold
	ask "OK" hold
	ask "RETURN" hold
	ask "FAST FORWARD >>" hold

	volume_section

	echo
	echo "Last: inputs and power, for the timing notes."
	ask "Press INPUT, pick another input, then come back to Couch's input (HDMI 1)" wait \
		"the survey waits until the bus has been quiet for 15 s" 15
	ask "Press POWER (the TV goes off), wait 10 s, then POWER again" wait \
		"Couch should come back on screen; the survey waits for 20 s of quiet" 20
fi

echo
echo "Done. Summary: $SUMMARY"
echo
cat "$SUMMARY"
