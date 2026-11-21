package driver

import (
	"context"
	"fmt"
	"time"

	plc4go "github.com/apache/plc4x/plc4go/pkg/api"
	apiModel "github.com/apache/plc4x/plc4go/pkg/api/model"
	"github.com/apache/plc4x/plc4go/pkg/api/values"

	"s7poc/internal/model"
)

// bytesOf flattens a plc4go BYTE array value (or a single byte) into a []byte.
func bytesOf(v values.PlcValue) []byte {
	if v.IsList() {
		items := v.GetList()
		b := make([]byte, len(items))
		for i, it := range items {
			b[i] = it.GetUint8()
		}
		return b
	}
	return []byte{v.GetUint8()}
}

// Info is shown under "driver_info" in the channel status.
func (s *S7) Info() map[string]any {
	mode := s.dev.S7.ReadMode
	if mode == "" {
		mode = "items"
	}
	info := map[string]any{"read_mode": mode}
	s.mu.Lock()
	if s.plan != nil {
		info["spans"] = len(s.plan.spans)
	}
	s.mu.Unlock()
	return info
}

// readMerged reads every span with ONE plc4go request (one byte-array item per span) and decodes
// the tags from the returned bytes. Compare its latency with read_mode: items on the real PLC.
func (s *S7) readMerged(ctx context.Context, c plc4go.PlcConnection) ([]model.Reading, error) {
	s.mu.Lock()
	p := s.plan
	s.mu.Unlock()
	if p == nil {
		var err error
		if p, err = buildPlan(s.dev.Tags, s.dev.S7.MaxSpan); err != nil {
			return nil, fmt.Errorf("invalid S7 tag configuration: %w", err)
		}
		s.mu.Lock()
		s.plan = p
		s.mu.Unlock()
	}
	if len(p.spans) == 0 {
		return nil, nil
	}
	rb := c.ReadRequestBuilder()
	for _, sp := range p.spans {
		rb.AddTagAddress(sp.name, sp.address())
	}
	req, err := rb.Build()
	if err != nil {
		return nil, fmt.Errorf("invalid S7 span configuration: %w", err)
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(s.dev.S7.TimeoutMS)*time.Millisecond)
	defer cancel()
	t0 := time.Now()
	var res apiModel.PlcReadRequestResult
	select {
	case res = <-req.Execute(ctx):
	case <-ctx.Done():
		return nil, fmt.Errorf("%w: S7 read timeout", ErrComm)
	}
	lat := float64(time.Since(t0).Microseconds()) / 1000
	if res.GetErr() != nil {
		return nil, fmt.Errorf("%w: S7 read failed: %v", ErrComm, res.GetErr())
	}
	resp := res.GetResponse()
	now := time.Now()
	var out []model.Reading
	for _, sp := range p.spans {
		code := resp.GetResponseCode(sp.name)
		var buf []byte
		if code == apiModel.PlcResponseCode_OK {
			buf = bytesOf(resp.GetValue(sp.name))
		}
		for _, m := range sp.members {
			md := map[string]any{"address": m.tag.S7Address, "read_mode": "merged", "span": sp.name}
			r := model.Reading{DeviceID: s.dev.ID, Protocol: "s7comm", Tag: m.tag.Name, Datatype: m.typ,
				Timestamp: now, LatencyMS: &lat, Metadata: md}
			switch {
			case code != apiModel.PlcResponseCode_OK:
				r.Quality, r.Error = model.Invalid, "response code "+code.String()
			case m.rel+m.size > len(buf):
				r.Quality, r.Error = model.Invalid, fmt.Sprintf("short response: %d of %d bytes", len(buf), sp.length)
			default:
				v, derr := decodeS7(m.typ, buf[m.rel:m.rel+m.size], m.bit)
				if derr != nil {
					r.Quality, r.Error = model.Invalid, derr.Error()
				} else {
					r.Quality, r.Value = model.Good, v
				}
			}
			out = append(out, r)
		}
	}
	return out, nil
}
