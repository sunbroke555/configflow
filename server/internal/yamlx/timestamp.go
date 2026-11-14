package yamlx

import (
	"fmt"
	"time"
)

// Timestamp is the datetime.date or datetime.datetime PyYAML builds for a
// plain scalar such as `2001-12-14` or `2001-12-14 21:59:43.10 -5`. It is not
// a jsonx value, so jsonx's encoders reject it just as json.dumps raises
// TypeError for those objects; Dump writes it back as PyYAML does.
type Timestamp struct {
	Year, Month, Day int
	// HasTime is false for a date.
	HasTime              bool
	Hour, Minute, Second int
	Microsecond          int
	HasZone              bool
	ZoneOffsetSeconds    int
}

// String is date.isoformat() or datetime.isoformat(" ").
func (t Timestamp) String() string {
	s := fmt.Sprintf("%04d-%02d-%02d", t.Year, t.Month, t.Day)
	if !t.HasTime {
		return s
	}
	s += fmt.Sprintf(" %02d:%02d:%02d", t.Hour, t.Minute, t.Second)
	if t.Microsecond != 0 {
		s += fmt.Sprintf(".%06d", t.Microsecond)
	}
	if t.HasZone {
		offset := t.ZoneOffsetSeconds
		sign := '+'
		if offset < 0 {
			sign = '-'
			offset = -offset
		}
		s += fmt.Sprintf("%c%02d:%02d", sign, offset/3600, offset%3600/60)
		if offset%60 != 0 {
			s += fmt.Sprintf(":%02d", offset%60)
		}
	}
	return s
}

// Time returns the instant as time.Time (a date is midnight UTC; a datetime
// without zone is taken as UTC, Python's naive datetime has no zone).
func (t Timestamp) Time() time.Time {
	loc := time.UTC
	if t.HasZone {
		loc = time.FixedZone("", t.ZoneOffsetSeconds)
	}
	return time.Date(t.Year, time.Month(t.Month), t.Day, t.Hour, t.Minute, t.Second, t.Microsecond*1000, loc)
}
