package agents

// Port of backend/agents/idle_rounds.py: a periodic background round may
// skip while nothing it depends on changed.
//
// A round that found nothing to do, and that looked only at the stored
// configuration, finds the same nothing until the configuration is committed
// again. Such a round records the generation it started from; while that
// generation is current, the following rounds return without reading the
// Agents. A round with work in flight, or one that depends on the clock or on
// a device, reports that it was not idle, and then every following round runs.

import (
	"context"
	"runtime"
	"sync"
	"time"
	"weak"

	"configflow/internal/repo"
	"configflow/internal/store"
)

// RecheckSeconds is RECHECK_SECONDS: an idle round is still repeated this
// often, for anything a loop reads that is not in the configuration database.
const RecheckSeconds = 600

type idleRound struct {
	head store.Head
	at   float64
}

// IdleRounds is IdleRounds(clock=...). The zero value is not usable; use
// NewIdleRounds.
type IdleRounds struct {
	clock func() float64
	mu    sync.Mutex
	// Keyed weakly like Python's WeakKeyDictionary: nothing here keeps a
	// discarded repository (and its cached document) alive.
	idle map[weak.Pointer[repo.Repository]]idleRound
}

var processStart = time.Now()

// NewIdleRounds returns the tracker; clock nil is time.monotonic (seconds).
func NewIdleRounds(clock func() float64) *IdleRounds {
	if clock == nil {
		clock = func() float64 { return time.Since(processStart).Seconds() }
	}
	return &IdleRounds{clock: clock, idle: map[weak.Pointer[repo.Repository]]idleRound{}}
}

// Due is due(repository): the current generation when a round has to run,
// ok=false while the last idle round still holds. Take it before the round
// reads anything, so a commit during the round makes the next one run again.
func (r *IdleRounds) Due(ctx context.Context, repository *repo.Repository) (head store.Head, run bool, err error) {
	head, err = repository.Head(ctx)
	if err != nil {
		return store.Head{}, false, err
	}
	r.mu.Lock()
	idle, ok := r.idle[weak.Make(repository)]
	r.mu.Unlock()
	if ok && idle.head == head {
		if elapsed := r.clock() - idle.at; elapsed >= 0 && elapsed < RecheckSeconds {
			return store.Head{}, false, nil
		}
	}
	return head, true, nil
}

// Done is done(repository, head, idle): record a finished round; idle is
// whether it found nothing to do or wait for.
func (r *IdleRounds) Done(repository *repo.Repository, head store.Head, idle bool) {
	key := weak.Make(repository)
	r.mu.Lock()
	defer r.mu.Unlock()
	if !idle {
		delete(r.idle, key)
		return
	}
	if _, known := r.idle[key]; !known {
		runtime.AddCleanup(repository, func(key weak.Pointer[repo.Repository]) {
			r.mu.Lock()
			delete(r.idle, key)
			r.mu.Unlock()
		}, key)
	}
	r.idle[key] = idleRound{head: head, at: r.clock()}
}

// Len is the number of remembered repositories (len(_idle), for tests).
func (r *IdleRounds) Len() int {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.idle)
}
