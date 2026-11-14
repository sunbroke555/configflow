package agents

import (
	"fmt"
	"math"
	"strings"
	"time"

	"configflow/internal/taskstatus"
)

// clock is time.Now, replaceable in tests.
var clock = time.Now

// isoformat is datetime.isoformat() of a naive (local wall clock) or aware
// time: microseconds only when non-zero, "+HH:MM" for aware times.
func isoformat(t time.Time, aware bool) string {
	text := fmt.Sprintf("%04d-%02d-%02dT%02d:%02d:%02d", t.Year(), int(t.Month()), t.Day(), t.Hour(), t.Minute(), t.Second())
	if micro := t.Nanosecond() / 1000; micro != 0 {
		text += fmt.Sprintf(".%06d", micro)
	}
	if aware {
		_, offset := t.Zone()
		sign := '+'
		if offset < 0 {
			sign, offset = '-', -offset
		}
		text += fmt.Sprintf("%c%02d:%02d", sign, offset/3600, offset/60%60)
		if offset%60 != 0 {
			text += fmt.Sprintf(":%02d", offset%60)
		}
	}
	return text
}

// nowISO is datetime.now().isoformat(): the local wall clock, naive.
func nowISO() string { return isoformat(clock(), false) }

// utcNowISO is datetime.now(timezone.utc).isoformat() ("...+00:00").
func utcNowISO() string { return isoformat(clock().UTC(), true) }

// utcNowZ is datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z').
func utcNowZ() string { return strings.TrimSuffix(utcNowISO(), "+00:00") + "Z" }

// fromISO is datetime.fromisoformat(value): the instant and whether the text
// was naive. A naive time is a local wall clock, as Python's .timestamp() and
// naive arithmetic against datetime.now() treat it.
func fromISO(value string) (moment time.Time, naive bool, ok bool) {
	parsed, ok := taskstatus.FromISOFormat(value)
	if !ok {
		return time.Time{}, false, false
	}
	// FromISOFormat takes a naive text as UTC; tell the two apart by whether an
	// offset can still be appended (only a naive date-time or date accepts one).
	_, withOffset := taskstatus.FromISOFormat(value + "+00:00")
	_, dateOnly := taskstatus.FromISOFormat(value + "T00:00:00+00:00")
	if !withOffset && !dateOnly {
		return parsed, false, true
	}
	return time.Date(parsed.Year(), parsed.Month(), parsed.Day(), parsed.Hour(), parsed.Minute(),
		parsed.Second(), parsed.Nanosecond(), time.Local), true, true
}

// pyTimestamp is the float .timestamp() of an instant.
func pyTimestamp(t time.Time) float64 {
	return float64(t.Unix()) + float64(t.Nanosecond())/1e9
}

// fromTimestamp is datetime.fromtimestamp(ts) (local, naive) truncated to
// microseconds as Python rounds it (round-half-even of the fraction).
func fromTimestamp(ts float64) time.Time {
	seconds := math.Floor(ts)
	micro := math.RoundToEven((ts - seconds) * 1e6)
	if micro >= 1e6 {
		seconds++
		micro -= 1e6
	}
	return time.Unix(int64(seconds), int64(micro)*1000).In(time.Local)
}

// nowTimestamp is time.time().
func nowTimestamp() float64 { return pyTimestamp(clock()) }
