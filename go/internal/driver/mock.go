package driver

import (
	"context"
	"fmt"
	"math"
	"strings"
	"sync"
	"time"

	"s7poc/internal/config"
	"s7poc/internal/model"
)

// SimulatedValue is a deterministic function of (tag, time). Identical to the Python mock.
// This is NOT an S7-1500 emulation.
func SimulatedValue(tag, dtype string, t float64) any {
	h := 0
	for _, c := range tag {
		h += int(c)
	}
	switch strings.ToUpper(dtype) {
	case "BOOL":
		return (int(math.Floor(t/5))+h)%2 == 0
	case "DINT", "INT", "UINT", "UDINT", "WORD", "DWORD":
		return int(t) + h*10
	}
	base := map[string]float64{"Temperature": 35, "Pressure": 4.2, "MotorSpeed": 1450}[tag]
	if base == 0 {
		base = 10 + float64(h%50)
	}
	return math.Round((base+math.Sin(t/10+float64(h))*base*0.05)*1000) / 1000
}

var opcuaNames = map[string]string{"REAL": "Float", "LREAL": "Double", "BOOL": "Boolean", "INT": "Int16", "UINT": "UInt16",
	"DINT": "Int32", "UDINT": "UInt32", "WORD": "UInt16", "DWORD": "UInt32"}

type Outage struct{ After, For time.Duration }

type Mock struct {
	dev    config.Device
	kind   string
	outage *Outage
	t0     time.Time
	mu     sync.Mutex
	conn   bool
}

func NewMock(d config.Device, kind string, o *Outage) *Mock {
	return &Mock{dev: d, kind: kind, outage: o, t0: time.Now()}
}

func (m *Mock) Protocol() string { return "mock-" + m.kind }
func (m *Mock) Kind() string     { return m.kind }
func (m *Mock) PollingMS() int {
	if m.kind == "s7comm" {
		return m.dev.S7.PollingMS
	}
	return m.dev.OpcUa.PollingMS
}
func (m *Mock) Connected() bool { m.mu.Lock(); defer m.mu.Unlock(); return m.conn }
func (m *Mock) Close()          { m.mu.Lock(); m.conn = false; m.mu.Unlock() }
func (m *Mock) down() bool {
	if m.outage == nil {
		return false
	}
	e := time.Since(m.t0)
	return e >= m.outage.After && e < m.outage.After+m.outage.For
}
func (m *Mock) Connect(context.Context) error {
	if m.down() {
		return fmt.Errorf("%w: mock: simulated network outage", ErrComm)
	}
	m.mu.Lock()
	m.conn = true
	m.mu.Unlock()
	return nil
}

func (m *Mock) Read(context.Context) ([]model.Reading, error) {
	if m.down() {
		m.Close()
		return nil, fmt.Errorf("%w: mock: simulated network outage", ErrComm)
	}
	t := float64(time.Now().UnixNano()) / 1e9
	lat := 4.0
	if m.kind == "opcua" {
		t += 0.15 // the two protocols sample at slightly different instants
		lat = 9.0
	}
	var out []model.Reading
	for _, tag := range m.dev.Tags {
		md := map[string]any{"simulated": true}
		if m.kind == "s7comm" {
			md["address"] = tag.S7Address
		} else {
			md["node_id"] = tag.OpcuaNode
		}
		dt := strings.ToUpper(tag.Type)
		if m.kind == "opcua" {
			if n, ok := opcuaNames[dt]; ok {
				dt = n
			}
		}
		out = append(out, model.Reading{DeviceID: m.dev.ID, Protocol: m.Protocol(), Tag: tag.Name,
			Datatype: dt, Value: SimulatedValue(tag.Name, tag.Type, t),
			Timestamp: time.Now(), Quality: model.Good, LatencyMS: model.Ptr(lat), Metadata: md})
	}
	return out, nil
}

func (m *Mock) Browse(_ context.Context, _ string, _ int) ([]map[string]any, error) {
	if m.kind != "opcua" {
		return nil, fmt.Errorf("mock s7comm cannot browse")
	}
	var out []map[string]any
	for _, t := range m.dev.Tags {
		out = append(out, map[string]any{"node_id": t.OpcuaNode, "browse_name": t.Name,
			"node_class": "Variable", "path": "/Simulated/" + t.Name, "datatype": t.Type})
	}
	return out, nil
}
