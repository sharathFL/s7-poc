// Package acq runs one supervised goroutine per (device, protocol): persistent connection,
// bounded timeouts, exponential-backoff reconnect, latest-value cache and bounded history.
package acq

import (
	"context"
	"errors"
	"log/slog"
	"math"
	"sort"
	"sync"
	"time"

	"s7poc/internal/config"
	"s7poc/internal/driver"
	"s7poc/internal/model"
)

type ErrEntry struct {
	Time     string `json:"time"`
	Protocol string `json:"protocol"`
	Message  string `json:"message"`
}

type Channel struct {
	Dev  config.Device
	Drv  driver.Driver
	Kind string

	mu          sync.Mutex
	connected   bool
	readsOK     int
	readsFail   int
	reconnects  int
	lastSuccess time.Time
	lastError   string
	lats        []float64
	errs        []ErrEntry
}

type key struct{ dev, kind, tag string }

type Acquisition struct {
	cfg      *config.Config
	Devices  map[string]config.Device
	Order    []string
	Channels map[[2]string]*Channel
	mu       sync.RWMutex
	latest   map[key]model.Reading
	history  map[key][]model.Reading
	cancel   context.CancelFunc
	wg       sync.WaitGroup
}

func New(cfg *config.Config, factory func(config.Device) []driver.Driver) *Acquisition {
	a := &Acquisition{cfg: cfg, Devices: map[string]config.Device{}, Channels: map[[2]string]*Channel{},
		latest: map[key]model.Reading{}, history: map[key][]model.Reading{}}
	for _, d := range cfg.Devices {
		a.Devices[d.ID] = d
		a.Order = append(a.Order, d.ID)
		for _, drv := range factory(d) {
			a.Channels[[2]string{d.ID, drv.Kind()}] = &Channel{Dev: d, Drv: drv, Kind: drv.Kind()}
		}
	}
	return a
}

func (a *Acquisition) Start(ctx context.Context) {
	ctx, a.cancel = context.WithCancel(ctx)
	for _, ch := range a.Channels {
		a.wg.Add(1)
		go func() { defer a.wg.Done(); a.run(ctx, ch) }()
	}
}

func (a *Acquisition) Stop() {
	if a.cancel != nil {
		a.cancel()
	}
	a.wg.Wait()
	for _, ch := range a.Channels {
		ch.Drv.Close()
	}
}

func (a *Acquisition) store(ch *Channel, r model.Reading) {
	k := key{r.DeviceID, ch.Kind, r.Tag}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.latest[k] = r
	h := append(a.history[k], r)
	if n := a.cfg.API.HistorySize; len(h) > n {
		h = h[len(h)-n:]
	}
	a.history[k] = h
}

func (ch *Channel) recordErr(msg string) {
	ch.lastError = msg
	ch.errs = append(ch.errs, ErrEntry{model.FormatTime(time.Now()), ch.Drv.Protocol(), msg})
	if len(ch.errs) > 50 {
		ch.errs = ch.errs[1:]
	}
}

func sleep(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

func (a *Acquisition) run(ctx context.Context, ch *Channel) {
	backoff := time.Second
	poll := time.Duration(math.Max(50, float64(ch.Drv.PollingMS()))) * time.Millisecond
	log := slog.With("device", ch.Dev.ID, "protocol", ch.Kind)
	for ctx.Err() == nil {
		err := func() error {
			if !ch.Drv.Connected() {
				if err := ch.Drv.Connect(ctx); err != nil {
					return err
				}
				ch.mu.Lock()
				ch.connected = true
				if ch.lastError != "" || ch.readsFail > 0 {
					ch.reconnects++
				}
				ch.mu.Unlock()
				backoff = time.Second
				log.Info("connected")
			}
			t0 := time.Now()
			rs, err := ch.Drv.Read(ctx)
			if err != nil {
				return err
			}
			lat := float64(time.Since(t0).Microseconds()) / 1000
			bad := ""
			for _, r := range rs {
				a.store(ch, r)
				if r.Quality != model.Good && bad == "" {
					bad = r.Tag + ": " + r.Error
				}
			}
			ch.mu.Lock()
			ch.readsOK++
			ch.lastSuccess = time.Now()
			ch.lats = append(ch.lats, lat)
			if len(ch.lats) > 500 {
				ch.lats = ch.lats[1:]
			}
			ch.lastError = ""
			if bad != "" {
				ch.recordErr("item invalid: " + bad)
			}
			ch.mu.Unlock()
			return nil
		}()
		if err == nil {
			sleep(ctx, poll)
			continue
		}
		if ctx.Err() != nil || errors.Is(err, context.Canceled) {
			return
		}
		msg := err.Error()
		if len(msg) > 400 {
			msg = msg[:400]
		}
		log.Warn("channel failure", "error", msg)
		ch.mu.Lock()
		ch.readsFail++
		ch.connected = false
		ch.recordErr(msg)
		ch.mu.Unlock()
		a.failAll(ch, msg)
		ch.Drv.Close()
		sleep(ctx, backoff)
		if backoff *= 2; backoff > 30*time.Second {
			backoff = 30 * time.Second
		}
	}
}

func (a *Acquisition) failAll(ch *Channel, msg string) {
	now := time.Now()
	for _, t := range ch.Dev.Tags {
		a.mu.RLock()
		prev, ok := a.latest[key{ch.Dev.ID, ch.Kind, t.Name}]
		a.mu.RUnlock()
		r := model.Reading{DeviceID: ch.Dev.ID, Protocol: ch.Drv.Protocol(), Tag: t.Name, Datatype: t.Type,
			Timestamp: now, Quality: model.CommFailure, Error: msg}
		if ok {
			r.Datatype, r.Value, r.Metadata = prev.Datatype, prev.Value, prev.Metadata
		}
		a.store(ch, r)
	}
}

func (a *Acquisition) effective(ch *Channel, r model.Reading) model.Reading {
	if r.Quality == model.Good {
		limit := time.Duration(a.cfg.API.StaleAfterFactor*float64(ch.Drv.PollingMS())) * time.Millisecond
		if limit < time.Second {
			limit = time.Second
		}
		if time.Since(r.Timestamp) > limit {
			r.Quality, r.Error = model.Stale, "no fresh reading"
		}
	}
	return r
}

// Latest returns the newest reading per (kind, tag), filtered; kind/tag may be empty.
func (a *Acquisition) Latest(dev, tag, kind string) []model.Reading {
	a.mu.RLock()
	defer a.mu.RUnlock()
	var out []model.Reading
	for k, r := range a.latest {
		if k.dev == dev && (tag == "" || k.tag == tag) && (kind == "" || k.kind == kind) {
			out = append(out, a.effective(a.Channels[[2]string{k.dev, k.kind}], r))
		}
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Tag != out[j].Tag {
			return out[i].Tag < out[j].Tag
		}
		return out[i].Protocol < out[j].Protocol
	})
	return out
}

func (a *Acquisition) History(dev, tag, kind string, limit int) []model.Reading {
	a.mu.RLock()
	defer a.mu.RUnlock()
	var out []model.Reading
	for k, h := range a.history {
		if k.dev == dev && (tag == "" || k.tag == tag) && (kind == "" || k.kind == kind) {
			out = append(out, h...)
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Timestamp.Before(out[j].Timestamp) })
	if len(out) > limit {
		out = out[len(out)-limit:]
	}
	return out
}

type ChannelStatus map[string]any

func (a *Acquisition) ChannelStatus(ch *Channel) ChannelStatus {
	ch.mu.Lock()
	defer ch.mu.Unlock()
	total := ch.readsOK + ch.readsFail
	var rate any
	if total > 0 {
		rate = math.Round(float64(ch.readsOK)/float64(total)*10000) / 10000
	}
	lat := map[string]any{"avg": nil, "p95": nil, "max": nil}
	if n := len(ch.lats); n > 0 {
		s := append([]float64(nil), ch.lats...)
		sort.Float64s(s)
		sum := 0.0
		for _, v := range s {
			sum += v
		}
		r2 := func(v float64) float64 { return math.Round(v*100) / 100 }
		p := int(float64(n) * 0.95)
		if p >= n {
			p = n - 1
		}
		lat = map[string]any{"avg": r2(sum / float64(n)), "p95": r2(s[p]), "max": r2(s[n-1])}
	}
	var ls any
	if !ch.lastSuccess.IsZero() {
		ls = model.FormatTime(ch.lastSuccess)
	}
	var le any
	if ch.lastError != "" {
		le = ch.lastError
	}
	recent := ch.errs
	if len(recent) > 10 {
		recent = recent[len(recent)-10:]
	}
	if recent == nil {
		recent = []ErrEntry{}
	}
	return ChannelStatus{"protocol": ch.Kind, "driver": ch.Drv.Protocol(), "connected": ch.connected,
		"mock": ch.Dev.Mock, "polling_ms": ch.Drv.PollingMS(), "last_success": ls, "last_error": le,
		"reconnects": ch.reconnects, "recent_errors": recent, "reads_ok": ch.readsOK,
		"reads_fail": ch.readsFail, "success_rate": rate, "latency_ms": lat}
}

func (a *Acquisition) DeviceStatus(id string) map[string]any {
	d := a.Devices[id]
	chans := map[string]any{}
	n, conn := 0, 0
	for k, ch := range a.Channels {
		if k[0] != id {
			continue
		}
		st := a.ChannelStatus(ch)
		chans[k[1]] = st
		n++
		if st["connected"].(bool) {
			conn++
		}
	}
	state := "OFFLINE"
	if n > 0 && conn == n {
		state = "ONLINE"
	} else if conn > 0 {
		state = "DEGRADED"
	}
	return map[string]any{"device_id": d.ID, "ip": d.IP, "mock": d.Mock, "state": state, "channels": chans}
}
