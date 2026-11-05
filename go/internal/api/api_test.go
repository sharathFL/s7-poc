// MOCK-DRIVER tests: exercise acquisition + API with simulated data. They prove the software
// pipeline only; they say nothing about the behaviour of a real S7-1500.
package api

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"s7poc/internal/acq"
	"s7poc/internal/config"
	"s7poc/internal/driver"
	"s7poc/internal/model"
)

const yml = `
api: {stale_after_factor: 3}
devices:
  - id: dev1
    ip: 127.0.0.1
    mock: true
    s7comm: {polling_ms: 100}
    opcua: {polling_ms: 100}
    tags:
      - {name: Temperature, type: REAL, s7_address: DB10.DBD0, opcua_node: "ns=2;s=Temperature"}
      - {name: MotorRunning, type: BOOL, s7_address: DB10.DBX12.0, opcua_node: "ns=2;s=MotorRunning"}
      - {name: ProductionCount, type: DINT, s7_address: DB10.DBD14, opcua_node: "ns=2;s=ProductionCount"}
`

func setup(t *testing.T, f func(config.Device) []driver.Driver) (*acq.Acquisition, *httptest.Server) {
	cfg, err := config.Parse([]byte(yml), false)
	if err != nil {
		t.Fatal(err)
	}
	a := acq.New(cfg, f)
	a.Start(context.Background())
	t.Cleanup(a.Stop)
	srv := httptest.NewServer((&Server{A: a, Dash: "/nonexistent"}).Handler())
	t.Cleanup(srv.Close)
	time.Sleep(400 * time.Millisecond)
	return a, srv
}

func get(t *testing.T, url string, out any) int {
	r, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer r.Body.Close()
	if out != nil {
		_ = json.NewDecoder(r.Body).Decode(out)
	}
	return r.StatusCode
}

func TestUnifiedStructure(t *testing.T) {
	_, s := setup(t, driver.Factory)
	var rs []map[string]any
	if c := get(t, s.URL+"/devices/dev1/readings", &rs); c != 200 || len(rs) != 6 {
		t.Fatalf("code=%d n=%d", c, len(rs))
	}
	for _, r := range rs {
		for _, k := range []string{"device_id", "protocol", "tag", "value", "datatype", "timestamp", "quality", "metadata"} {
			if _, ok := r[k]; !ok {
				t.Fatalf("missing %s", k)
			}
		}
		if r["quality"] != "GOOD" {
			t.Fatalf("quality %v", r["quality"])
		}
	}
}

func TestErrorsAndNoWrite(t *testing.T) {
	_, s := setup(t, driver.Factory)
	if c := get(t, s.URL+"/devices/zzz/status", nil); c != 404 {
		t.Fatal(c)
	}
	if c := get(t, s.URL+"/devices/dev1/tags/Nope/latest", nil); c != 404 {
		t.Fatal(c)
	}
	if c := get(t, s.URL+"/devices/dev1/readings?protocol=modbus", nil); c != 422 {
		t.Fatal(c)
	}
	for _, m := range []string{"POST", "PUT", "DELETE", "PATCH"} {
		for _, p := range []string{"/devices", "/devices/dev1/tags", "/comparison"} {
			req, _ := http.NewRequest(m, s.URL+p, nil)
			r, _ := http.DefaultClient.Do(req)
			if r.StatusCode != 405 {
				t.Fatalf("%s %s => %d", m, p, r.StatusCode)
			}
		}
	}
}

func TestComparison(t *testing.T) {
	_, s := setup(t, driver.Factory)
	var c map[string]any
	get(t, s.URL+"/comparison?device_id=dev1", &c)
	sum := c["summary"].(map[string]any)
	// mock protocols sample 150 ms apart, so a counter/bool may legitimately tick between reads
	if sum["compared"].(float64) != 3 || sum["matching"].(float64) < 2 || sum["type_mismatches"].(float64) != 0 {
		t.Fatalf("%v", sum)
	}
}

func TestOutageAndRecovery(t *testing.T) {
	a, _ := setup(t, func(d config.Device) []driver.Driver {
		return []driver.Driver{driver.NewMock(d, "s7comm", &driver.Outage{After: 500 * time.Millisecond, For: time.Second})}
	})
	if r := a.Latest("dev1", "Temperature", "")[0]; r.Quality != model.Good {
		t.Fatalf("expected GOOD, got %v", r.Quality)
	}
	time.Sleep(900 * time.Millisecond) // inside outage
	if r := a.Latest("dev1", "Temperature", "")[0]; r.Quality != model.CommFailure {
		t.Fatalf("expected COMM_FAILURE, got %v", r.Quality)
	}
	if a.DeviceStatus("dev1")["state"] != "OFFLINE" {
		t.Fatal("expected OFFLINE")
	}
	time.Sleep(3 * time.Second) // outage over + backoff
	if r := a.Latest("dev1", "Temperature", "")[0]; r.Quality != model.Good {
		t.Fatalf("expected recovery, got %v", r.Quality)
	}
	if ch := a.Channels[[2]string{"dev1", "s7comm"}]; ch.Drv == nil || a.ChannelStatus(ch)["reconnects"].(int) < 1 {
		t.Fatal("expected a reconnect")
	}
}
