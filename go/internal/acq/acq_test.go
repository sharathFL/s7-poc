// Harness tests for connection handling. They use fake drivers, so they prove the supervisor
// logic (deadline, backoff state, manual control) and say nothing about a real S7-1500.
package acq

import (
	"context"
	"sync"
	"testing"
	"time"

	"s7poc/internal/config"
	"s7poc/internal/driver"
	"s7poc/internal/model"
)

const yml = `
api: {stale_after_factor: 3, read_deadline_ms: 300}
devices:
  - id: dev1
    ip: 127.0.0.1
    s7comm: {polling_ms: 100}
    tags:
      - {name: T, type: INT, s7_address: DB1.DBW0}
`

// fake is a driver whose Read can be made to hang (ignoring ctx, like a library that drops
// its timeout options) or fail.
type fake struct {
	mu       sync.Mutex
	conn     bool
	hang     chan struct{} // when non-nil, Read blocks until it is closed
	connects int
}

func (f *fake) Protocol() string { return "fake" }
func (f *fake) Kind() string     { return "s7comm" }
func (f *fake) PollingMS() int   { return 100 }
func (f *fake) Connected() bool  { f.mu.Lock(); defer f.mu.Unlock(); return f.conn }
func (f *fake) Connect(context.Context) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.conn = true
	f.connects++
	return nil
}
func (f *fake) Close() { f.mu.Lock(); f.conn = false; f.mu.Unlock() }
func (f *fake) Read(context.Context) ([]model.Reading, error) {
	f.mu.Lock()
	h := f.hang
	f.mu.Unlock()
	if h != nil {
		<-h
	}
	return []model.Reading{{DeviceID: "dev1", Protocol: "fake", Tag: "T", Datatype: "INT",
		Value: 1, Timestamp: time.Now(), Quality: model.Good}}, nil
}
func (f *fake) Browse(context.Context, string, int) ([]map[string]any, error) { return nil, nil }
func (f *fake) setHang(h chan struct{})                                       { f.mu.Lock(); f.hang = h; f.mu.Unlock() }
func (f *fake) connectCount() int                                             { f.mu.Lock(); defer f.mu.Unlock(); return f.connects }

func start(t *testing.T, f *fake) *Acquisition {
	cfg, err := config.Parse([]byte(yml), false)
	if err != nil {
		t.Fatal(err)
	}
	a := New(cfg, func(config.Device) []driver.Driver { return []driver.Driver{f} })
	a.Start(context.Background())
	t.Cleanup(a.Stop)
	return a
}

func state(a *Acquisition) string {
	return a.ChannelStatus(a.Channels[[2]string{"dev1", "s7comm"}])["state"].(string)
}

func waitFor(t *testing.T, what string, d time.Duration, cond func() bool) {
	t.Helper()
	end := time.Now().Add(d)
	for time.Now().Before(end) {
		if cond() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("timeout waiting for %s", what)
}

func TestHungReadIsCutByDeadline(t *testing.T) {
	f := &fake{}
	a := start(t, f)
	waitFor(t, "CONNECTED", 2*time.Second, func() bool { return state(a) == StateConnected })

	hang := make(chan struct{})
	t.Cleanup(func() { close(hang) })
	f.setHang(hang) // next Read blocks forever and ignores ctx
	waitFor(t, "BACKOFF after hung read", 2*time.Second, func() bool { return state(a) == StateBackoff })

	st := a.ChannelStatus(a.Channels[[2]string{"dev1", "s7comm"}])
	if st["consecutive_failures"].(int) < 1 {
		t.Fatalf("status %v", st)
	}
	waitFor(t, "COMM_FAILURE", time.Second, func() bool {
		rs := a.Latest("dev1", "T", "")
		return len(rs) == 1 && rs[0].Quality == model.CommFailure
	})
	h := a.ConnectionHealth()
	if h["status"] != "down" {
		t.Fatalf("health %v", h["status"])
	}
}

func TestManualDisconnectHoldsAndReconnectRecovers(t *testing.T) {
	f := &fake{}
	a := start(t, f)
	waitFor(t, "CONNECTED", 2*time.Second, func() bool { return state(a) == StateConnected })

	if n, err := a.Control("dev1", "s7comm", "disconnect"); n != 1 || err != nil {
		t.Fatal(n, err)
	}
	waitFor(t, "MANUAL_HOLD", 2*time.Second, func() bool { return state(a) == StateManualHold })
	c := f.connectCount()
	time.Sleep(1500 * time.Millisecond) // longer than the first backoff: must NOT reconnect by itself
	if state(a) != StateManualHold || f.connectCount() != c {
		t.Fatalf("held channel reconnected: state=%s connects %d->%d", state(a), c, f.connectCount())
	}
	waitFor(t, "COMM_FAILURE while held", time.Second, func() bool {
		rs := a.Latest("dev1", "T", "")
		return len(rs) == 1 && rs[0].Quality == model.CommFailure
	})

	if _, err := a.Control("dev1", "s7comm", "reconnect"); err != nil {
		t.Fatal(err)
	}
	waitFor(t, "CONNECTED again", 2*time.Second, func() bool { return state(a) == StateConnected })
	if f.connectCount() <= c {
		t.Fatal("no new connect after reconnect")
	}
}

func TestManualReconnectDropsLiveConnection(t *testing.T) {
	f := &fake{}
	a := start(t, f)
	waitFor(t, "CONNECTED", 2*time.Second, func() bool { return state(a) == StateConnected })
	c := f.connectCount()
	a.Control("dev1", "", "reconnect")
	waitFor(t, "second connect", 2*time.Second, func() bool { return f.connectCount() > c })
}

func TestControlRejectsBadAction(t *testing.T) {
	f := &fake{}
	a := start(t, f)
	if _, err := a.Control("dev1", "", "write"); err == nil {
		t.Fatal("bad action accepted")
	}
	if n, _ := a.Control("nope", "", "reconnect"); n != 0 {
		t.Fatal("matched unknown device")
	}
}
