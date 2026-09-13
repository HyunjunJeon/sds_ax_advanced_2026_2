#!/usr/bin/env bash
# Real-binary dogfood for the steer-triggered bash fold.
#
# Launches the compiled gjc in a detached tmux session, asks the model to run a
# long foreground sleep, waits past STEER_FOLD_GRACE_MS, types a steer and
# presses Enter exactly like a user, then captures the pane at each phase.
#
# Every gate is causal and fail-closed:
#   - submission is proven by a rendered ` user` transcript turn;
#   - the running command is proven by the Bash card's own `$ <command>` line,
#     never by the echoed prompt;
#   - the fold is proven by the reason line AND the bound job id `bg_N`;
#   - the same-turn answer is proven by a NONCE the model is told to repeat,
#     which cannot match the user's own echoed text;
#   - the wake is proven by a visible->absent transition of the running-job
#     indicator plus the manager's completion receipt for the SAME job id.
#
# Every run writes `receipt.json` beside the captures binding the outcome to
# the executed binary (absolute path + SHA-256 of the bytes actually run) and
# listing only the captures THIS invocation wrote. The driver's own checkout
# state (git HEAD, bash.ts blob) is recorded as driver provenance, not as proof
# of what source produced the binary. The receipt is finalized on every exit,
# including unhandled failures, by the EXIT trap.
#
# Usage: scripts/dogfood/steer-fold-tmux.sh [<gjc binary>] [<out dir>]
# Env:   GJC_DOGFOOD_MODEL — model passed as --model (default gpt-5.6-sol). The
#        run must not depend on whatever the user's config default is that day.
set -euo pipefail

# ---- Phase 0: evidence channel first. Nothing fallible runs before the output
# directory is owned, prior evidence is purged, receipt fields have explicit
# incomplete values, and the EXIT finalizer is armed.
OUT="${2:-$PWD/artifacts/steer-fold/dogfood}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
rm -f "$OUT"/*.ansi "$OUT"/*.txt "$OUT"/receipt.json
BIN="${1:-$PWD/packages/coding-agent/dist/gjc}"
BIN_SHA256="incomplete"; BIN_SIZE="null"; BIN_POLICY_SEAM_HITS="null"
SELF="incomplete"; DRIVER_HEAD="incomplete"; BASH_TS_BLOB="incomplete"
SESSION="gjc-dogfood-$$"; WORK=""
NONCE="steerack$(printf '%04x' $((RANDOM * RANDOM % 65536)))"
MODEL="${GJC_DOGFOOD_MODEL:-gpt-5.6-sol}"
OUTCOME="running"; EXIT_CODE=""; JOB=""; NONCE_STATE="unanswered"
OWNED_CAPTURES=()
RECEIPT_FAILED=0
# Dedicated exit code: the behavioral run may have passed but its evidence
# could not be persisted, which is a distinct, reportable failure.
EXIT_RECEIPT_UNPERSISTED=75

json_str() { python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$1"; }

# Writes the receipt atomically. Returns non-zero on ANY encoding, write, or
# rename failure so callers (and finalize) can refuse to report success
# without durable evidence.
receipt() {
	local caps="" c tmp="$OUT/.receipt.json.$$"
	for c in "${OWNED_CAPTURES[@]+"${OWNED_CAPTURES[@]}"}"; do caps="${caps:+$caps,}$(json_str "$c")" || return 1; done
	local recorded_at binary_path binary_sha self head blob session work nonce outcome exit_code job answered
	recorded_at="$(json_str "$(date -u +%Y-%m-%dT%H:%M:%SZ)")" || return 1
	binary_path="$(json_str "$BIN")" || return 1
	binary_sha="$(json_str "$BIN_SHA256")" || return 1
	self="$(json_str "$SELF")" || return 1
	head="$(json_str "$DRIVER_HEAD")" || return 1
	blob="$(json_str "$BASH_TS_BLOB")" || return 1
	session="$(json_str "$SESSION")" || return 1
	work="$(json_str "$WORK")" || return 1
	nonce="$(json_str "$NONCE")" || return 1
	local model; model="$(json_str "$MODEL")" || return 1
	outcome="$(json_str "$OUTCOME")" || return 1
	exit_code="$(json_str "$EXIT_CODE")" || return 1
	job="$(json_str "$JOB")" || return 1
	answered="$(json_str "$NONCE_STATE")" || return 1
	{
		printf '{\n  "schemaVersion": 1,\n  "kind": "steer-fold-tmux-dogfood",\n'
		printf '  "recordedAt": %s,\n' "$recorded_at"
		printf '  "binary": { "absolutePath": %s, "sha256": %s, "sizeBytes": %s, "toolInterruptPolicySeamHits": %s },\n' "$binary_path" "$binary_sha" "$BIN_SIZE" "$BIN_POLICY_SEAM_HITS"
		printf '  "driverCheckout": { "path": %s, "repoHead": %s, "bashTsBlob": %s, "note": "state of the checkout that ran the driver; not proof of what source produced the binary" },\n' "$self" "$head" "$blob"
		printf '  "session": { "tmux": %s, "cwd": %s, "nonce": %s, "model": %s },\n' "$session" "$work" "$nonce" "$model"
		printf '  "outcome": %s, "exitCode": %s, "jobId": %s, "steerAnswered": %s,\n' "$outcome" "$exit_code" "$job" "$answered"
		printf '  "captures": [%s]\n}\n' "$caps"
	} > "$tmp" || return 1
	python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$tmp" || { rm -f "$tmp"; return 1; }
	mv -f "$tmp" "$OUT/receipt.json" || return 1
}

# Best-effort wrapper for non-terminal checkpoints: remembers a failure so the
# final exit can refuse to claim success without durable evidence.
checkpoint() { receipt || { RECEIPT_FAILED=1; echo "warn: receipt checkpoint failed" >&2; }; }

finalize() {
	local rc=$?
	# The process exit status is authoritative. A latched OUTCOME/EXIT_CODE
	# that disagrees with it (e.g. PASS latched, then the receipt write failed)
	# must not be persisted as-is.
	if [ "$OUTCOME" = "running" ]; then OUTCOME="fail:unhandled"; fi
	if [ "$RECEIPT_FAILED" = 1 ]; then (( rc != 0 )) || rc=$EXIT_RECEIPT_UNPERSISTED; fi
	if (( rc != 0 )) && { [ "$OUTCOME" = "pass" ] || [ "$EXIT_CODE" != "$rc" ]; }; then
		[ "$OUTCOME" = "pass" ] && OUTCOME="fail:exit-after-pass"
		EXIT_CODE="$rc"
	fi
	[ -n "$EXIT_CODE" ] || EXIT_CODE="$rc"
	if ! receipt; then
		echo "FAIL: receipt could not be persisted; evidence for this run is not durable" >&2
		# A behavioral success without durable evidence is not a success.
		(( rc != 0 )) || rc=$EXIT_RECEIPT_UNPERSISTED
		# Best-effort second attempt so the receipt, if it lands at all, carries the final rc.
		EXIT_CODE="$rc"; [ "$OUTCOME" = "pass" ] && OUTCOME="fail:receipt-unpersisted"
		receipt || true
	elif [ "$RECEIPT_FAILED" = 1 ]; then
		echo "FAIL: an earlier receipt checkpoint failed; evidence for this run is not durable" >&2
	fi
	tmux kill-session -t "$SESSION" 2>/dev/null || echo "warn: tmux session $SESSION already gone" >&2
	sleep 1
	[ -z "$WORK" ] || rm -rf "$WORK" 2>/dev/null || echo "warn: could not remove $WORK" >&2
	exit "$rc"
}
trap finalize EXIT
receipt   # initial receipt: outcome=running, provenance=incomplete

# ---- Phase 1: provenance. Every failure below now lands in finalize with the
# incomplete fields still visible in the receipt.
BIN="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$BIN")"
[ -x "$BIN" ] || { echo "not an executable: $BIN" >&2; exit 64; }
WORK="$(mktemp -d "${TMPDIR:-/tmp}/gjc-dogfood.XXXXXX")"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
REPO="$(cd "$(dirname "$SELF")/../.." && pwd)"
BIN_SHA256="$(shasum -a 256 "$BIN" | cut -d' ' -f1)"
BIN_SIZE="$(python3 -c 'import os,sys; print(os.path.getsize(sys.argv[1]))' "$BIN")"
DRIVER_HEAD="$(git -C "$REPO" rev-parse HEAD)"
BASH_TS_BLOB="$(git -C "$REPO" rev-parse "HEAD:packages/coding-agent/src/tools/bash.ts")"
# Observed discriminator for THIS lineage only: the fix removed two
# `getToolInterruptPolicy` seams (declaration + SDK provider), so a fixed build
# has fewer matching lines than a pre-fix build of the same base (2 vs 4).
# grep exit 1 is "no match" (a legitimate 0); anything >1 is a real error.
BIN_POLICY_SEAM_HITS="$(grep -c -a 'getToolInterruptPolicy' "$BIN")" || { rc=$?; (( rc == 1 )) || { echo "grep failed on $BIN (rc=$rc)" >&2; exit 70; }; BIN_POLICY_SEAM_HITS=0; }

pane() { tmux capture-pane -t "$SESSION" -p -S -400; }

capture() {
	# Keep escape sequences: the ultragoal PTY gate needs terminal control codes.
	tmux capture-pane -t "$SESSION" -p -e -S -400 > "$OUT/$1.ansi"
	pane > "$OUT/$1.txt"
	OWNED_CAPTURES+=("$1.ansi")
}

fail() { # <code> <label> <message>
	# Record the failure state FIRST so the receipt is right even if the pane is gone.
	OUTCOME="fail:$2"; EXIT_CODE="$1"; checkpoint
	capture "fail-$2" 2>/dev/null || echo "warn: could not capture pane for $2" >&2
	checkpoint
	echo "FAIL: $3" >&2
	exit "$1"
}

wait_for() { # <pattern> <timeout-seconds> <label>
	local deadline=$(( $(date +%s) + $2 ))
	while :; do
		if pane | grep -qE -- "$1"; then return 0; fi
		if (( $(date +%s) > deadline )); then fail 1 "timeout-$3" "timed out waiting for $3 (/$1/)"; fi
		sleep 0.5
	done
}

cd "$WORK"
git init -q .
checkpoint
tmux new-session -d -s "$SESSION" -x 140 -y 45 "$BIN --model $MODEL"
wait_for 'ready' 60 composer-ready
sleep 3

CMD="printf 'foreground sleep started\\n'; sleep 120; printf 'foreground sleep finished\\n'"
PROMPT="Run exactly this shell command in the foreground with the bash tool and a 180 second timeout, do not background it and do not add async: $CMD"
tmux send-keys -t "$SESSION" -l "$PROMPT"
sleep 0.5
tmux send-keys -t "$SESSION" C-m
wait_for '^ user$' 30 prompt-submitted
# The Bash card renders the command on its own `$ ` line; the echoed prompt never has that prefix.
wait_for '^\s*│ \$ printf .foreground sleep started' 120 bash-card
sleep 3
capture 1-bash-running

# Past STEER_FOLD_GRACE_MS (2000 ms) with margin, like a human typing after ~8 s.
sleep 8
STEER="Reply with exactly the word $NONCE and nothing else."
tmux send-keys -t "$SESSION" -l "$STEER"
sleep 0.3
tmux send-keys -t "$SESSION" C-m
sleep 2
capture 2-steer-sent

# Fold: reason line + bound job id.
wait_for 'Folded into background job bg_[0-9]+ because a user steer arrived' 30 folded
JOB="$(pane | grep -oE 'Folded into background job bg_[0-9]+' | head -1 | grep -oE 'bg_[0-9]+')"
[ -n "$JOB" ] || fail 2 no-job-id "fold line present but no job id"
capture 3-folded; checkpoint
if pane | grep -q "Tool execution was aborted"; then fail 3 aborted "bash was aborted"; fi

# The running-job indicator must be VISIBLE (the job is alive after the fold)…
wait_for '(Background: 1 background bash|1 job running)' 30 job-visible
# …and the model must answer the steer in the same turn: the nonce appears in
# an assistant turn AFTER the steer's own ` user` turn. Count occurrences: the
# echoed steer text contains the nonce once; the answer adds a second.
deadline=$(( $(date +%s) + 90 ))
while (( $(pane | grep -c "$NONCE") < 2 )); do
	if (( $(date +%s) > deadline )); then fail 4 steer-unanswered "model never repeated nonce $NONCE"; fi
	sleep 1
done
NONCE_STATE="answered"; capture 4-steer-answered; checkpoint
if pane | grep -q "Tool execution was aborted"; then fail 3 aborted-late "bash was aborted after the steer"; fi

# Wake: the SAME job's completion receipt, then the indicator absent.
wait_for "Background job completed \[bash\] $JOB" 175 job-completed
deadline=$(( $(date +%s) + 60 ))
while pane | grep -qE '(Background: 1 background bash|1 job running)'; do
	if (( $(date +%s) > deadline )); then fail 5 indicator-stuck "running-job indicator never cleared after $JOB completed"; fi
	sleep 1
done
sleep 4
capture 5-job-finished
if pane | grep -q "Tool execution was aborted"; then fail 3 aborted-final "abort text present at the end"; fi

OUTCOME="pass"; EXIT_CODE=0
# The final receipt is part of PASS. finalize re-derives OUTCOME/EXIT_CODE from
# the real exit status, so a failure here can never be persisted as pass/0.
receipt || exit $EXIT_RECEIPT_UNPERSISTED
echo "PASS: fold=$JOB, no abort, steer answered (nonce $NONCE), $JOB completed and indicator cleared" >&2
echo "$OUT"
